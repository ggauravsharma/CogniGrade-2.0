"""Task services: what the application asks for, in its own vocabulary.

Routes call these. They do not call providers, do not build prompts, do not
know a model name, and do not hold a provider file handle.

    grade_answer(...)              -> (GradingResult, raw_text)
    recognise_answer_images(...)   -> str
    extract_document_text(...)     -> str

Each one resolves its task's settings, builds a versioned prompt, runs the
configured provider under a bounded retry, logs one telemetry line, and returns
a DOMAIN value. The provider is an implementation detail from here up.

WHERE THE LINE IS
-----------------
This module owns orchestration: which task, which prompt, retry, telemetry.
It does NOT own grading semantics. Score validation, bounds, NaN rejection and
the zero-versus-missing distinction stay in `backend.grading.result`, which is
the domain's own contract and is deliberately untouched by this phase.
"""

from __future__ import annotations

import logging
import time
from typing import Optional, Sequence, Tuple

from backend.ai.config import TaskSettings, get_task_settings
from backend.ai.contracts import AITask, FilePart, ProviderRequest, ProviderResponse, TextPart
from backend.ai.documents import visible_pages
from backend.ai.errors import ProviderError
from backend.ai.prompts import (
    GRADING_PROMPT_VERSION,
    build_answer_mapping_prompt,
    build_answer_recognition_prompt,
    build_batch_answer_recognition_prompt,
    build_document_extraction_prompt,
    build_grading_prompt,
    build_label_extraction_prompt,
    build_marking_scheme_recognition_prompt,
)
from backend.ai.providers import get_provider
from backend.ai.retry import run_with_retries
from backend.ai.telemetry import log_invocation
from backend.grading.result import GradingResult, parse_grading_response

logger = logging.getLogger(__name__)


async def run_task(
    request: ProviderRequest,
    *,
    settings: Optional[TaskSettings] = None,
    exam_id: Optional[int] = None,
    student_id: Optional[int] = None,
    question_id: Optional[int] = None,
) -> ProviderResponse:
    """Run one AI task end to end: configure, retry, time, log.

    The single choke point every task goes through, so retry policy and
    telemetry cannot drift apart between tasks or be forgotten by a new one.
    """
    resolved = settings or get_task_settings(request.task)
    provider = get_provider(resolved.provider)
    started = time.monotonic()
    attempts = 0

    def _count(attempt: int) -> None:
        nonlocal attempts
        attempts = attempt

    async def _attempt() -> ProviderResponse:
        return await provider.run_text_task(request, resolved)

    try:
        response, attempts = await run_with_retries(
            _attempt, settings=resolved, on_attempt=_count
        )
    except ProviderError as exc:
        log_invocation(
            task=request.task, provider=resolved.provider, model=resolved.model,
            prompt_version=request.prompt_version,
            duration_ms=int((time.monotonic() - started) * 1000),
            attempts=attempts or 1, success=False, error_category=exc.category,
            file_count=len(request.file_paths), exam_id=exam_id,
            student_id=student_id, question_id=question_id,
        )
        raise

    log_invocation(
        task=request.task, provider=resolved.provider, model=resolved.model,
        prompt_version=request.prompt_version,
        duration_ms=int((time.monotonic() - started) * 1000),
        attempts=attempts, success=True,
        file_count=len(request.file_paths), exam_id=exam_id,
        student_id=student_id, question_id=question_id,
        # Why generation stopped, and how much it produced. A truncated
        # response succeeds HERE and fails in the decoder, so without these two
        # the log says `success=True` and the mark says `malformed_json` with
        # nothing connecting them.
        finish_reason=response.finish_reason,
        output_tokens=response.output_tokens,
    )
    return ProviderResponse(
        text=response.text, provider=response.provider, model=response.model,
        task=response.task, prompt_version=response.prompt_version,
        attempts=attempts, duration_ms=response.duration_ms,
        uploaded_file_count=response.uploaded_file_count, warnings=response.warnings,
        finish_reason=response.finish_reason,
        input_tokens=response.input_tokens, output_tokens=response.output_tokens,
    )


# ---------------------------------------------------------------------------
# grading
# ---------------------------------------------------------------------------

async def grade_answer(
    *,
    question_text: str,
    student_answer: str,
    max_marks,
    marking_scheme: Optional[str] = None,
    ideal_answer: Optional[str] = None,
    exam_id: Optional[int] = None,
    student_id: Optional[int] = None,
    question_id: Optional[int] = None,
) -> Tuple[GradingResult, str]:
    """Grade a text-only answer. Returns one VALIDATED result, or raises.

    The result is the domain's own `GradingResult`, produced by
    `parse_grading_response`, so bounds, NaN rejection and the
    zero-versus-missing rule are enforced exactly as before this layer existed.

    Raises `GradingResponseError` when a response arrives but cannot be
    validated, and `ProviderError` when the call itself failed. Callers must
    keep those apart: neither is a zero (audit C6).
    """
    prompt, version = build_grading_prompt(
        question_text=question_text,
        student_answer=student_answer,
        max_marks=max_marks,
        marking_scheme=marking_scheme,
        ideal_answer=ideal_answer,
    )
    request = ProviderRequest(
        task=AITask.GRADING,
        parts=(TextPart(prompt),),
        expects_json=True,
        prompt_version=version,
    )
    response = await run_task(
        request, exam_id=exam_id, student_id=student_id, question_id=question_id
    )
    return parse_grading_response(response.text, max_marks=max_marks), response.text


async def grade_answer_with_parts(
    parts: Sequence[object],
    *,
    max_marks,
    exam_id: Optional[int] = None,
    student_id: Optional[int] = None,
    question_id: Optional[int] = None,
) -> Tuple[GradingResult, str]:
    """Grade from an ORDERED mix of text and image paths.

    Diagram/table grading places the marking-scheme images immediately after
    the marking-scheme text and the student's images immediately after the
    student's answer. Flattening that to "all files, then all text" would
    change what the model is asked, so the caller supplies the order and the
    adapter preserves it.

    AUDIT C1: the caller builds the two image sides separately and hands them
    over already positioned. This function never reorders, merges or reuses
    them, so there is still no single value that could reach both slots.
    """
    request = ProviderRequest(
        task=AITask.GRADING,
        parts=tuple(parts),
        expects_json=True,
        prompt_version=GRADING_PROMPT_VERSION,
    )
    response = await run_task(
        request, exam_id=exam_id, student_id=student_id, question_id=question_id
    )
    return parse_grading_response(response.text, max_marks=max_marks), response.text


# ---------------------------------------------------------------------------
# recognition
# ---------------------------------------------------------------------------

async def recognise_answer_images(
    image_paths: Sequence[str],
    *,
    batch: bool = False,
    exam_id: Optional[int] = None,
    student_id: Optional[int] = None,
    question_id: Optional[int] = None,
) -> str:
    """Read handwritten answer images into text."""
    prompt, version = (
        build_batch_answer_recognition_prompt() if batch
        else build_answer_recognition_prompt()
    )
    request = ProviderRequest.simple(
        task=AITask.ANSWER_RECOGNITION,
        prompt=prompt,
        file_paths=tuple(image_paths),
        prompt_version=version,
    )
    response = await run_task(
        request, exam_id=exam_id, student_id=student_id, question_id=question_id
    )
    return response.text


async def map_answer_script(
    script_path: str,
    *,
    question_numbers: Sequence[int],
    exam_id: Optional[int] = None,
    student_id: Optional[int] = None,
) -> str:
    """Read a WHOLE answer script and assign its answers to known questions.

    ONE provider call for the whole script: every visible page goes into a
    single request, because assigning an answer to a question needs the pages
    read together -- an answer running from the bottom of one page to the top
    of the next is the normal case, not the exception. Per-page calls would
    also multiply a scarce quota by the page count for no benefit.

    Goes through `visible_pages` for the same reason document extraction does:
    the script must be understood as the pages a reader sees, never as a file
    whose text layer can carry content the page does not show.

    Returns the raw response. Validation against the exam's own question
    numbers is `backend/ai/answer_mapping.py`'s job, and it is not optional.
    """
    prompt, version = build_answer_mapping_prompt(question_numbers)
    async with visible_pages(script_path) as page_paths:
        request = ProviderRequest.simple(
            task=AITask.ANSWER_MAPPING,
            prompt=prompt,
            file_paths=tuple(page_paths),
            prompt_version=version,
        )
        response = await run_task(request, exam_id=exam_id, student_id=student_id)
    return response.text


async def recognise_marking_scheme_images(
    image_paths: Sequence[str],
    *,
    key_lines: Sequence[str] = (),
    exam_id: Optional[int] = None,
) -> str:
    """Read marking-scheme images, each addressed by its key."""
    prompt, version = build_marking_scheme_recognition_prompt()
    if key_lines:
        prompt = f"{prompt}\nThe keys, in image order, are:\n" + "\n".join(key_lines)
    request = ProviderRequest.simple(
        task=AITask.MARKING_SCHEME_RECOGNITION,
        prompt=prompt,
        file_paths=tuple(image_paths),
        prompt_version=version,
    )
    response = await run_task(request, exam_id=exam_id)
    return response.text


# ---------------------------------------------------------------------------
# document extraction
# ---------------------------------------------------------------------------

# Both document tasks read an UPLOADED file, and both used to pass that file
# straight through. A PDF then reached the provider as a PDF, and a PDF shows
# less than it contains: a paper cut down to five questions still carried the
# text of the two that had been made invisible, so seven questions were
# extracted from a five-question paper. `visible_pages` renders the document
# and the task sees the pages instead. See backend/ai/documents.py.


async def extract_question_labels(
    document_path: str, *, exam_id: Optional[int] = None
) -> str:
    """Read the question-label hierarchy out of a question paper.

    Reads the paper's VISIBLE pages, which is the paper the person who uploaded
    it approved. Anything the document carries but does not show is not a
    question and must not become one.
    """
    prompt, version = build_label_extraction_prompt()
    async with visible_pages(document_path) as page_paths:
        request = ProviderRequest.simple(
            task=AITask.LABEL_EXTRACTION,
            prompt=prompt,
            file_paths=tuple(page_paths),
            prompt_version=version,
        )
        response = await run_task(request, exam_id=exam_id)
    return response.text


async def extract_document_text(
    document_path: str,
    *,
    leaf_labels: Sequence[str] = (),
    exam_id: Optional[int] = None,
) -> str:
    """Read a marking scheme / solution script, guided by known labels.

    Same rule as the question paper, and for the same reason: hidden text in a
    marking scheme would reach `ideal_marking_scheme` and be graded against.
    """
    prompt, version = build_document_extraction_prompt(leaf_labels)
    async with visible_pages(document_path) as page_paths:
        request = ProviderRequest.simple(
            task=AITask.DOCUMENT_EXTRACTION,
            prompt=prompt,
            file_paths=tuple(page_paths),
            prompt_version=version,
        )
        response = await run_task(request, exam_id=exam_id)
    return response.text
