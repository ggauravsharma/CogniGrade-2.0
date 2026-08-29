"""Provider-neutral aggregation of question marks into an exam result.

CogniGrade must distinguish

    the student earned 0          -> a grade
    grading produced no result    -> an incomplete grading operation

Only the first is a mark. The second must never be silently folded into a total.

WHAT THIS REPLACES
------------------
`add_exam_result_internal` previously did::

    total_marks = sum(r.marks_obtained for r in responses
                      if r.marks_obtained is not None)
    ...
    exam_result.status = "graded"

The filter looks defensive but is the bug: a response whose grading failed has
`marks_obtained = None`, is skipped by the sum, and therefore contributes
exactly zero to the total -- while the result is stamped "graded" regardless.
A provider failure became a silent zero on a student's transcript.

Correctness Foundation v2 made grading failures explicit at the question level
(no mark is written when the provider response cannot be validated). This module
makes the exam level honour that: a missing mark blocks finalisation instead of
being counted as nothing.

PROVIDER NEUTRALITY
-------------------
No model SDK, FastAPI or SQLAlchemy import. The inputs are question ids and
mark values, so this behaves identically whether grading was performed by
Gemini, an open VLM, a specialist grader, an ensemble, or a human. Completeness
is a deterministic domain fact, never a provider detail.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

logger = logging.getLogger(__name__)


class ExamResultStatus:
    """The values `ExamResult.status` may hold.

    `status` is a free Text column, so adding a value needs no migration, but
    every consumer must understand it -- see the frontend submission-status
    branch. Deliberately provider-independent: never `gemini_failed`.
    """

    PENDING = "pending"
    GRADED = "graded"
    #: Some question that has a submitted response has no validated mark. The
    #: total so far may be recorded, but it is NOT final.
    GRADING_INCOMPLETE = "grading_incomplete"

    ALL = (PENDING, GRADED, GRADING_INCOMPLETE)

    #: Statuses that mean "this total is final and may be shown as the result".
    FINAL = (GRADED,)


@dataclass(frozen=True)
class AggregationResult:
    """The outcome of aggregating one student's marks for one exam."""

    total_score: float
    complete: bool
    graded_count: int
    ungraded_question_ids: list[int] = field(default_factory=list)
    questions_without_response: list[int] = field(default_factory=list)

    @property
    def status(self) -> str:
        return ExamResultStatus.GRADED if self.complete else ExamResultStatus.GRADING_INCOMPLETE

    @property
    def is_final(self) -> bool:
        """True only when `total_score` may be presented as the student's result."""
        return self.complete


def aggregate_student_result(
    *,
    expected_question_ids: Sequence[int],
    responses: Iterable[Any],
) -> AggregationResult:
    """Total a student's marks and decide whether the result may be finalised.

    `responses` is any iterable of objects exposing `question_id` and
    `marks_obtained` -- ORM rows in production, plain stubs in tests.

    COMPLETENESS RULE
    -----------------
    A result is complete when every question response that EXISTS for this
    student carries a validated mark.

    A question with no response row at all is reported in
    `questions_without_response` but does NOT block finalisation. That
    distinction is deliberate and is the line Phase B asks for: an absent row
    means the student submitted no work for that question, whereas a row with
    `marks_obtained is None` means grading was expected to produce a mark and
    did not. Blocking on the former would make every exam with a skipped
    question permanently unfinalisable, which is not a grading failure.

    ZERO IS NOT MISSING
    -------------------
    `marks_obtained == 0` is a real grade and is counted. Only `None` is
    treated as absent. Nothing is cast to int, so fractional marks aggregate
    correctly once the columns stop being Integer (audit C7).
    """
    expected = list(expected_question_ids)
    seen_question_ids: set[int] = set()
    total: float = 0.0
    graded_count = 0
    ungraded: list[int] = []

    for response in responses:
        question_id = getattr(response, "question_id", None)
        if question_id is not None:
            seen_question_ids.add(question_id)

        mark = getattr(response, "marks_obtained", None)
        if mark is None:
            # No validated grading result. NOT a zero.
            ungraded.append(question_id)
            continue

        total += float(mark)
        graded_count += 1

    without_response = [q for q in expected if q not in seen_question_ids]

    return AggregationResult(
        total_score=total,
        complete=not ungraded,
        graded_count=graded_count,
        ungraded_question_ids=[q for q in ungraded if q is not None],
        questions_without_response=without_response,
    )


def log_incomplete(
    aggregation: AggregationResult, *, exam_id: int, student_id: int, previous_status: Optional[str]
) -> None:
    """Record an incomplete aggregation without leaking student content.

    Ids and counts only: no answer text, no model output, no file paths.
    """
    logger.error(
        "exam grading incomplete: exam_id=%s student_id=%s graded=%s ungraded=%s "
        "ungraded_question_ids=%s questions_without_response=%s status %s -> %s",
        exam_id,
        student_id,
        aggregation.graded_count,
        len(aggregation.ungraded_question_ids),
        aggregation.ungraded_question_ids,
        len(aggregation.questions_without_response),
        previous_status,
        aggregation.status,
    )
