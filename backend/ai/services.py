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
from backend.ai.errors import ProviderError
from backend.ai.prompts import (
    GRADING_PROMPT_VERSION,
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
    )
    return ProviderResponse(
        text=response.text, provider=response.provider, model=response.model,
        task=response.task, prompt_version=response.prompt_version,
        attempts=attempts, duration_ms=response.duration_ms,
        uploaded_file_count=response.uploaded_file_count, warnings=response.warnings,
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

async def extract_question_labels(
    document_path: str, *, exam_id: Optional[int] = None
) -> str:
    """Read the question-label hierarchy out of a question paper."""
    prompt, version = build_label_extraction_prompt()
    request = ProviderRequest.simple(
        task=AITask.LABEL_EXTRACTION,
        prompt=prompt,
        file_paths=(document_path,),
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
    """Read a marking scheme / solution script, guided by known labels."""
    prompt, version = build_document_extraction_prompt(leaf_labels)
    request = ProviderRequest.simple(
        task=AITask.DOCUMENT_EXTRACTION,
        prompt=prompt,
        file_paths=(document_path,),
        prompt_version=version,
    )
    response = await run_task(request, exam_id=exam_id)
    return response.text
