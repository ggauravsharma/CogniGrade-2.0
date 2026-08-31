"""What a professor is told when grading a question did not produce a mark.

THE PROBLEM
-----------
Correctness v2 made grading failures explicit at the question level (no mark is
written when the provider response cannot be validated) and Correctness v3 made
the exam result honour that (`grading_incomplete`, never finalised). Both are
correct and both are silent: `grade_exam_logic` computes `failed_questions` and
throws it away -- `tasks.py` discards the return value -- so a professor sees

    Exam result: grading incomplete

with no way to learn WHICH question failed or why, and no way to recover except
re-running the whole exam blindly.

WHAT THIS MODULE IS
-------------------
The vocabulary for that answer, and nothing more. It maps a machine-readable
`error_code` -- the one `GradingResponseError` already carries -- onto a short
sentence a human can act on, and defines `GradingFailure`, the shape the API
returns.

DELIBERATELY NOT A JOB SYSTEM
-----------------------------
No event ledger, no attempt history, no observability platform. One nullable
code column on the response row, cleared the moment a valid mark arrives. The
current state of grading is a fact about the response, not a stream of events,
and treating it as a fact is what makes retries self-healing (see
`clear_on_success` below).

PROVIDER NEUTRALITY
-------------------
No SDK, FastAPI, SQLAlchemy or `backend.models` import (asserted by a
token-level test, as in `result.py`, `aggregation.py` and `marks.py`). The
codes name what went wrong with a GRADING RESULT -- "no score in the response",
"score outside the allowed range" -- never who produced it. A local VLM, an
ensemble or a human grading service failing validation lands on the same code
and reads the same sentence. There is no `gemini_error` here and there must
never be one.

WHAT IS NOT SAID
----------------
The message is derived from the code, never from the provider's text. Raw
model output can be long, can echo the student's answer, and is not something
to render in a classroom UI, so it is logged and never persisted or returned.
An unknown code degrades to a generic sentence rather than leaking anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

#: `error_code` -> a sentence safe to show a professor.
#:
#: Keys are exactly the codes `GradingResponseError` raises today. Anything not
#: listed falls through to UNKNOWN_FAILURE_MESSAGE, so adding a code elsewhere
#: degrades gracefully instead of breaking a page.
FAILURE_MESSAGES: Dict[str, str] = {
    "empty_response": "The grading model returned no usable text.",
    "malformed_json": "The grading response was not valid JSON.",
    "wrong_schema": "The grading response did not have the expected fields.",
    "score_missing": "The grading response contained no score.",
    "score_not_numeric": "The grading response contained a score that is not a number.",
    "score_not_finite": "The grading response contained a score that is not a finite number.",
    "score_negative": "The grading response contained a negative score.",
    "score_above_max": "The grading response scored above the question's maximum marks.",
    #: Not raised by the response validator: recorded when the grading call
    #: itself failed (transport, timeout, provider unavailable) rather than
    #: returning something unparseable.
    "provider_unavailable": "The grading service could not be reached.",
    #: The catch-all recorded when a grading attempt raised something the
    #: grading path did not anticipate. The detail is in the logs, not here.
    "grading_error": "Grading failed for an unexpected reason.",
}

UNKNOWN_FAILURE_MESSAGE = "Grading did not produce a valid result."

#: Recorded when a grading attempt fails in a way that is not a validation
#: failure. Kept as a constant so call sites cannot invent spellings.
PROVIDER_UNAVAILABLE = "provider_unavailable"
UNEXPECTED_ERROR = "grading_error"


def describe(error_code: Optional[str]) -> str:
    """A short, safe sentence for one failure code."""
    if not error_code:
        return UNKNOWN_FAILURE_MESSAGE
    return FAILURE_MESSAGES.get(error_code, UNKNOWN_FAILURE_MESSAGE)


@dataclass(frozen=True)
class GradingFailure:
    """One question that has no validated mark, and why.

    `question_number` is what the professor actually recognises ("Q4");
    `question_id` is what a client would act on. `error_code` is stable and
    machine-readable, `message` is derived from it for display.
    """

    question_id: int
    question_number: Optional[int]
    error_code: Optional[str]

    @property
    def message(self) -> str:
        return describe(self.error_code)

    @property
    def label(self) -> str:
        """"Q4" when the number is known, else a stable fallback."""
        return f"Q{self.question_number}" if self.question_number is not None else f"#{self.question_id}"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "question_id": self.question_id,
            "question_number": self.question_number,
            "label": self.label,
            "error_code": self.error_code,
            "message": self.message,
        }


def collect_failures(rows: Any) -> list:
    """Build the failure list for one student from their response rows.

    A row counts as failed when it has NO validated mark. That is the same rule
    aggregation uses, and reusing it is the point: the list a professor sees can
    never disagree with the status they see beside it.

    `marks_obtained == 0` is a grade, so a valid zero is NEVER listed -- audit
    C6, restated here because this is exactly the kind of surface that would
    quietly reintroduce the confusion.

    Rows are duck-typed on `question_id`, `marks_obtained`, `grading_error_code`
    and an optional `question_number`, so ORM rows and test stubs both work and
    this module needs no model import.
    """
    failures = []
    for row in rows:
        if getattr(row, "marks_obtained", None) is not None:
            continue  # graded, including a legitimate 0
        failures.append(
            GradingFailure(
                question_id=getattr(row, "question_id", None),
                question_number=getattr(row, "question_number", None),
                error_code=getattr(row, "grading_error_code", None),
            )
        )
    return failures
