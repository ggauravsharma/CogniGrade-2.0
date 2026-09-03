"""Turning an uploaded answer script into something per-question to grade.

THE STAGE THE PRODUCT DID NOT HAVE
----------------------------------
`enqueue_processing` refuses to grade a student with no `question_responses`,
and it is right to: `aggregate_student_result` finalises when every response
that EXISTS carries a mark, so with zero rows that rule is vacuously true and
the paper would be stamped `graded` with a fabricated `0.0`. But nothing
created those rows automatically. Every live creator was a person -- the crop
editor above all -- so an AI-first product asked the student to cut their own
script into pieces before the AI was allowed to look at it.

This module closes that gap, and closes it at the smallest possible point:
`GradingEvidence.has_student_evidence` is already satisfied by
`answer_text` alone, so a response carrying recognised text is enough for the
whole existing grading pipeline. No segmentation, no geometry, no crops, no new
persisted format, and not one line of grading changed.

WHAT IT WILL NOT DO
-------------------
* It will not create a `Question`. The exam's rows are authoritative; a number
  the model returns that the exam does not have is discarded by
  `backend/ai/answer_mapping.py` and reported here.
* It will not create an empty response to satisfy the readiness gate. That
  would move the vacuous-aggregation bug rather than fix it: a row with no
  answer is a question grading would score, and scoring nothing is how a
  fabricated zero is born.
* It will not touch a response that already exists. A row means the paper was
  already prepared -- by the crop editor, by a teacher's correction, or by an
  earlier run -- and re-preparation must not overwrite a mark, a manual edit or
  a legacy crop. Preparation is for UNPREPARED papers, which also makes
  re-running it safe with no unique constraint to lean on.
* It will not decide that an unanswered question is worth zero. A question with
  no entry gets no row, so grading skips it and aggregation treats it as
  skipped -- exactly what happens today when a student leaves a question out.

PROVIDER-NEUTRAL. This module names a task, not a vendor: it calls
`ai_services.map_answer_script`, which resolves whatever adapter the deployment
configured for `AITask.ANSWER_MAPPING`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, Optional, Sequence, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.ai import services as ai_services
from backend.ai.answer_mapping import AnswerMappingError, parse_answer_mapping
from backend.ai.documents import DocumentNormalisationError
from backend.ai.errors import ProviderError
from backend.models.files import AnswerScript
from backend.models.tables import Question, QuestionResponse

logger = logging.getLogger(__name__)

#: Provider-neutral reasons preparation produced nothing. Same discipline as
#: `backend/grading/failure.py`: a short code, never a provider name and never
#: the model's own text.
NO_QUESTIONS = "no_questions"
NO_ANSWER_SCRIPT = "no_answer_script"
SCRIPT_UNREADABLE = "answer_script_unreadable"
MAPPING_UNAVAILABLE = "answer_mapping_unavailable"
MAPPING_INVALID = "answer_mapping_invalid"
NO_ANSWERS_MAPPED = "no_answers_mapped"

#: Nothing to do, and that is fine.
ALREADY_PREPARED = "already_prepared"
PREPARED = "prepared"


@dataclass(frozen=True)
class PreparationOutcome:
    """How far automatic preparation got, in the domain's own vocabulary."""

    status: str
    #: Response rows this run created.
    created: int = 0
    #: Response rows that already existed and were left exactly as they were.
    kept: int = 0
    #: Question numbers the model named that this exam does not have.
    rejected_numbers: Tuple[int, ...] = ()

    @property
    def ready(self) -> bool:
        """Whether there is now anything for grading to work on.

        Deliberately not `status == PREPARED`: a paper prepared by the crop
        editor is just as ready, and a run that mapped two of five questions is
        ready for those two.
        """
        return (self.created + self.kept) > 0


async def _existing_response_question_ids(
    exam_id: int, student_id: int, db: AsyncSession
) -> Dict[int, int]:
    """`question_id -> response id` for what this student already has."""
    rows = await db.execute(
        select(QuestionResponse.question_id, QuestionResponse.id)
        .join(Question, Question.id == QuestionResponse.question_id)
        .where(
            Question.exam_id == exam_id,
            QuestionResponse.student_id == student_id,
        )
    )
    return {question_id: response_id for question_id, response_id in rows.all()}


async def _latest_answer_script(
    exam_id: int, student_id: int, db: AsyncSession
) -> Optional[AnswerScript]:
    """The student's most recently uploaded script for this exam.

    Most recent by id: a re-upload should be what gets read, and `created_at`
    is nullable on this table while the primary key never is.
    """
    found = await db.execute(
        select(AnswerScript)
        .where(
            AnswerScript.exam_id == exam_id,
            AnswerScript.student_id == student_id,
        )
        .order_by(AnswerScript.id.desc())
    )
    return found.scalars().first()


async def prepare_student_responses(
    exam_id: int, student_id: int, db: AsyncSession
) -> PreparationOutcome:
    """Create this student's per-question responses from their answer script.

    A no-op when responses already exist, so it is safe to call on every run and
    safe to call twice. Persists in ONE transaction: either every mapped answer
    lands or none does, so a failure part-way cannot leave a half-prepared paper
    that looks ready.
    """
    questions = (await db.execute(
        select(Question).where(Question.exam_id == exam_id)
    )).scalars().all()
    if not questions:
        return PreparationOutcome(status=NO_QUESTIONS)

    existing = await _existing_response_question_ids(exam_id, student_id, db)
    if existing:
        # Already prepared -- by this stage on an earlier run, by the crop
        # editor, or by a teacher. Nothing is rewritten.
        return PreparationOutcome(status=ALREADY_PREPARED, kept=len(existing))

    script = await _latest_answer_script(exam_id, student_id, db)
    if script is None or not (script.file_path or "").strip():
        return PreparationOutcome(status=NO_ANSWER_SCRIPT)

    by_number = {q.question_number: q for q in questions if q.question_number is not None}
    allowed = sorted(by_number)

    try:
        raw = await ai_services.map_answer_script(
            script.file_path,
            question_numbers=allowed,
            exam_id=exam_id,
            student_id=student_id,
        )
    except DocumentNormalisationError as exc:
        # The script could not be reduced to visible pages. Named, not guessed,
        # and NOT retried against the raw file -- that would be the hidden-text
        # bug all over again.
        logger.error(
            "answer-script preparation could not render the document: exam_id=%s code=%s",
            exam_id, exc.code,
        )
        return PreparationOutcome(status=SCRIPT_UNREADABLE)
    except ProviderError as exc:
        # Category only. Never the provider's message, never a traceback.
        logger.error(
            "answer mapping failed: exam_id=%s category=%s", exam_id, exc.category
        )
        return PreparationOutcome(status=MAPPING_UNAVAILABLE)

    try:
        mapping = parse_answer_mapping(raw, allowed_numbers=allowed)
    except AnswerMappingError as exc:
        logger.error("answer mapping was unusable: exam_id=%s code=%s", exam_id, exc.code)
        return PreparationOutcome(status=MAPPING_INVALID)

    if mapping.rejected_numbers:
        # Loud, because it means the model named questions this exam does not
        # have -- and silently dropping that is how phantom questions start.
        logger.warning(
            "answer mapping named %d question number(s) this exam does not have; "
            "discarded: %s",
            len(mapping.rejected_numbers),
            ", ".join(str(n) for n in mapping.rejected_numbers),
        )
    if mapping.empty_numbers or mapping.duplicate_numbers:
        logger.info(
            "answer mapping dropped %d empty and %d duplicate entr(ies)",
            len(mapping.empty_numbers), len(mapping.duplicate_numbers),
        )

    if not mapping.has_answers:
        # A blank script, or a mapping that produced nothing usable. Either way
        # there is nothing to grade, and saying so is the honest answer -- the
        # caller must not go on to aggregate.
        logger.error(
            "answer mapping produced no usable answers: exam_id=%s", exam_id
        )
        return PreparationOutcome(
            status=NO_ANSWERS_MAPPED, rejected_numbers=mapping.rejected_numbers
        )

    # One transaction. `existing` is empty here, so there is nothing to collide
    # with and no duplicate can be created.
    rows = [
        QuestionResponse(
            question_id=by_number[answer.question_number].id,
            student_id=student_id,
            answer_text=answer.answer_text,
        )
        for answer in mapping.answers
    ]
    db.add_all(rows)
    await db.commit()

    logger.info(
        "answer-script preparation created %d response(s) for exam_id=%s", len(rows), exam_id
    )
    return PreparationOutcome(
        status=PREPARED, created=len(rows), rejected_numbers=mapping.rejected_numbers
    )
