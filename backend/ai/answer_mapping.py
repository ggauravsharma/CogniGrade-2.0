"""The answer-mapping task: which of THIS exam's questions did the student answer?

The stage the product was missing. An uploaded answer script and a set of
`questions` rows existed; nothing turned the first into per-question content, so
`enqueue_processing`'s readiness gate refused every automatic run and the only
way forward was a student cutting the script up in the crop editor. That is a
human-first workflow standing in the middle of an AI-first product.

WHAT THIS MODULE IS, AND IS NOT
-------------------------------
It is the CONTRACT and the deterministic gate: what a provider is asked for,
and what is allowed through. It holds no provider concept, performs no IO and
touches no database -- the same shape as `backend/ai/segmentation.py`.

It is NOT segmentation. Nothing here produces geometry, regions or crops. A
mapped answer is text assigned to an existing question, which is exactly what
`GradingEvidence.has_student_evidence` already accepts, so the whole grading
pipeline downstream is untouched.

THE EXAM'S QUESTIONS ARE AUTHORITATIVE
--------------------------------------
`allowed_numbers` comes from the `questions` rows, which came from the question
paper. A number the model returns that is not in that set is DISCARDED and
reported, never created. That is the same rule the question-paper ingestion
learned the hard way: a five-question paper must not become seven because a
model said so.

OMISSION IS MEANINGFUL
----------------------
A question with no entry means "not attempted", and it stays that way: no row
is created for it, so grading skips it and aggregation treats it as skipped.
An entry with an empty answer would say "attempted, wrote nothing", which is a
different fact the model is not in a position to assert, so it is rejected too.
Keeping those apart is what stops a preparation gap from becoming a zero.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

#: Same tolerance as `backend/grading/result.py`: a provider that ignores the
#: JSON mime-type request should degrade to a parse we can still validate
#: strictly, not to a silent nothing.
_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


class AnswerMappingError(Exception):
    """A mapping response arrived but cannot be used.

    Carries a provider-neutral `code`, like `GradingResponseError`, so a caller
    can record WHY preparation produced nothing without ever storing or showing
    the model's own words.
    """

    def __init__(self, code: str, message: str, *, raw: Optional[str] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        #: Kept for logging at the boundary only. Never persisted, never shown.
        self.raw = raw


@dataclass(frozen=True)
class MappedAnswer:
    """One of the exam's questions, and the student's answer to it, as text."""

    question_number: int
    answer_text: str


@dataclass(frozen=True)
class AnswerMapping:
    """Everything the gate let through, and everything it did not."""

    answers: Tuple[MappedAnswer, ...] = ()
    #: Numbers the model returned that this exam does not have. Reported so an
    #: operator can see the model drifted, never acted on.
    rejected_numbers: Tuple[int, ...] = ()
    #: Numbers that appeared more than once; the FIRST entry wins.
    duplicate_numbers: Tuple[int, ...] = ()
    #: Numbers whose entry carried no usable text.
    empty_numbers: Tuple[int, ...] = ()

    @property
    def has_answers(self) -> bool:
        return bool(self.answers)


def _coerce_question_number(value: Any) -> Optional[int]:
    """`31`, `"31"`, `"Q31"`, `31.0` -> 31. Anything else -> None.

    Tolerant about shape, strict about identity: whatever comes back still has
    to match a number the exam actually has.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        match = re.search(r"-?\d+", value)
        if match:
            try:
                return int(match.group(0))
            except ValueError:
                return None
    return None


def _extract_json_object(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z]*\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped).strip()
    if stripped.startswith("{"):
        return stripped
    match = _JSON_OBJECT.search(stripped)
    if match:
        return match.group(0)
    raise AnswerMappingError(
        "not_json", "response did not contain a JSON object", raw=text
    )


def parse_answer_mapping(
    raw_text: Optional[str], *, allowed_numbers: Iterable[int]
) -> AnswerMapping:
    """Validate a mapping response against the exam's own question numbers.

    Raises `AnswerMappingError` when the response is unusable as a whole.
    Returns an `AnswerMapping` when it is usable -- possibly with zero answers,
    which is a legitimate outcome (a blank script) and is the caller's decision
    to act on, not a parse error.
    """
    allowed: Set[int] = {int(n) for n in allowed_numbers}
    if not allowed:
        raise AnswerMappingError(
            "no_allowed_questions", "the exam has no questions to map answers to"
        )

    if raw_text is None or not str(raw_text).strip():
        raise AnswerMappingError("empty_response", "provider returned an empty response")

    payload_text = _extract_json_object(str(raw_text))
    try:
        payload = json.loads(payload_text)
    except (TypeError, ValueError) as exc:
        raise AnswerMappingError(
            "malformed_json", f"response was not valid JSON: {exc}", raw=raw_text
        )

    if not isinstance(payload, Mapping):
        raise AnswerMappingError(
            "wrong_schema", "response JSON was not an object", raw=raw_text
        )
    entries = payload.get("answers")
    if entries is None:
        raise AnswerMappingError(
            "answers_missing", "response JSON has no 'answers' field", raw=raw_text
        )
    if not isinstance(entries, (list, tuple)):
        raise AnswerMappingError(
            "wrong_schema", "'answers' was not a list", raw=raw_text
        )

    answers: List[MappedAnswer] = []
    seen: Set[int] = set()
    rejected: List[int] = []
    duplicates: List[int] = []
    empty: List[int] = []

    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        number = _coerce_question_number(entry.get("question_number"))
        if number is None:
            continue
        if number not in allowed:
            # The exam does not have this question. Recorded, discarded, and
            # never a reason to create one.
            rejected.append(number)
            continue
        if number in seen:
            duplicates.append(number)
            continue
        text = entry.get("answer")
        text = "" if text is None else str(text).strip()
        if not text:
            # "Attempted but wrote nothing" is not something a mapping pass can
            # assert. Dropped, so the question stays unattempted rather than
            # becoming an empty answer that grading would score.
            empty.append(number)
            continue
        seen.add(number)
        answers.append(MappedAnswer(question_number=number, answer_text=text))

    answers.sort(key=lambda a: a.question_number)
    return AnswerMapping(
        answers=tuple(answers),
        rejected_numbers=tuple(sorted(set(rejected))),
        duplicate_numbers=tuple(sorted(set(duplicates))),
        empty_numbers=tuple(sorted(set(empty))),
    )
