"""Provider-neutral grading result, and strict validation of provider output.

A model response is UNTRUSTED INPUT. The system must never interpret
"I could not understand the model response" as "the student scored zero".

WHAT THIS REPLACES
------------------
Grading previously parsed free text of the shape::

    Grade: 3
    Reason: some prose

with, in two byte-identical copies::

    grade_str = grade_line.split("Grade:")[1].strip()
    try:
        grade = float(grade_str.split()[0].split('/')[0])
    except ValueError:
        pass                      # <- grade silently stays None

Every failure mode of that parser ended in a `None` score that was
indistinguishable from "not graded yet":

  * the response did not contain "Grade:"          -> None, no error
  * the score would not parse as a number          -> None, no error
  * the score was out of range                     -> None, no error
  * `split()[0]` on an empty string                -> IndexError, became a 500
  * "nan" / "inf" parsed successfully as floats and PASSED the range check,
    because `nan < 0` and `nan > max_marks` are both False

and the caller then wrote that None straight over a student's marks.

PROVIDER NEUTRALITY
-------------------
Nothing here imports a model SDK, FastAPI or SQLAlchemy. `GradingResult` is
what any grading provider must produce -- Gemini today, an open VLM, a
specialist grader or an ensemble later. JSON decoding lives here because JSON
is not provider-specific; the provider-specific part (how a particular SDK is
asked for JSON) stays in the router.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)

# A score may exceed max_marks by at most this much before it is rejected, to
# absorb binary floating-point representation error (e.g. 0.1+0.2 == 0.30000000000000004).
# Anything within tolerance is snapped to max_marks exactly; anything beyond is
# an explicit failure, never a silent clamp.
SCORE_EPSILON = 1e-6


class GradingResponseError(Exception):
    """The provider's response could not be turned into a valid result.

    Carries a machine-readable `code` so callers and logs can distinguish
    "the model returned prose" from "the model returned a score of -4", without
    parsing the message text.
    """

    def __init__(self, code: str, message: str, *, raw: Optional[str] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        # Truncated: raw model output can be long, and it may echo student work.
        self.raw = (raw or "")[:2000] or None

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"GradingResponseError(code={self.code!r}, message={self.message!r})"


@dataclass(frozen=True)
class GradingResult:
    """One validated grade for one question.

    `score` is a float so that partial credit -- 0.5, 1.5, 2.25 -- is
    representable in the domain. NOTE: the database columns are still Integer
    (audit C7), so persisting a fractional score is a separate, unsolved
    problem. This contract is deliberately correct even though storage is not.
    """

    score: float
    reason: str
    max_marks: Optional[float] = None

    def __post_init__(self) -> None:
        # Defence in depth: the factory below is the intended entry point, but
        # a directly-constructed instance must not be able to hold nonsense.
        if not isinstance(self.score, (int, float)) or isinstance(self.score, bool):
            raise GradingResponseError("score_not_numeric", "score must be a number")
        if not math.isfinite(self.score):
            raise GradingResponseError("score_not_finite", "score must be finite")
        if self.score < 0:
            raise GradingResponseError("score_negative", "score must not be negative")


def _coerce_score(value: Any) -> float:
    """Accept a JSON number, or a numeric string, and reject everything else.

    `bool` is excluded explicitly: in Python `True` is an int, and silently
    grading a student `1` because the model emitted `true` would be absurd.
    """
    if isinstance(value, bool):
        raise GradingResponseError("score_not_numeric", "score must be a number, not a boolean")
    if isinstance(value, (int, float)):
        score = float(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise GradingResponseError("score_missing", "score was an empty string")
        # A bare fraction such as "3/5" is tolerated because the previous
        # prompt invited it; only the numerator is meaningful.
        if "/" in text:
            text = text.split("/", 1)[0].strip()
        try:
            score = float(text)
        except (TypeError, ValueError):
            raise GradingResponseError(
                "score_not_numeric", f"score {value!r} is not a number"
            )
    else:
        raise GradingResponseError(
            "score_not_numeric", f"score must be a number, got {type(value).__name__}"
        )

    # float() happily accepts "nan", "inf" and "-inf". The old range check let
    # every one of them through, because comparisons with NaN are always False.
    if not math.isfinite(score):
        raise GradingResponseError("score_not_finite", "score must be finite")
    return score


def build_grading_result(
    *, score: Any, reason: Any, max_marks: Optional[Any] = None
) -> GradingResult:
    """Validate a candidate score/reason pair into a GradingResult.

    Bounds are enforced deterministically here rather than trusted to the
    model. A score above `max_marks` by more than SCORE_EPSILON is an explicit
    failure; within tolerance it is snapped to `max_marks` exactly.
    """
    if score is None:
        raise GradingResponseError("score_missing", "response contained no score")
    value = _coerce_score(score)

    if value < 0:
        raise GradingResponseError(
            "score_negative", f"score {value} is negative"
        )

    limit: Optional[float] = None
    if max_marks is not None:
        try:
            limit = float(max_marks)
        except (TypeError, ValueError):
            limit = None
        if limit is not None and math.isfinite(limit):
            if value > limit + SCORE_EPSILON:
                raise GradingResponseError(
                    "score_above_max",
                    f"score {value} exceeds max marks {limit}",
                )
            if value > limit:
                value = limit  # within tolerance: snap, and say so in the log
                logger.debug("score snapped to max_marks within float tolerance")
        else:
            limit = None

    if reason is None:
        reason_text = ""
    elif isinstance(reason, str):
        reason_text = reason.strip()
    else:
        reason_text = str(reason).strip()

    if not reason_text:
        raise GradingResponseError(
            "reason_missing", "response contained no reason for the score"
        )

    return GradingResult(score=value, reason=reason_text, max_marks=limit)


_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json_object(text: str) -> str:
    """Recover the JSON object from a response that may carry wrapping.

    With `response_mime_type="application/json"` the body should already be
    bare JSON. This tolerates a fenced block or a stray sentence around it,
    because a provider that quietly ignores the mime-type request should
    degrade to a parse we can still validate strictly -- not to a silent None.
    It does NOT tolerate absent JSON.
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        # ```json ... ```  ->  ...
        stripped = re.sub(r"^```[a-zA-Z]*\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped).strip()
    if stripped.startswith("{"):
        return stripped
    match = _JSON_OBJECT.search(stripped)
    if match:
        return match.group(0)
    raise GradingResponseError(
        "not_json", "response did not contain a JSON object", raw=text
    )


def parse_grading_response(
    raw_text: Optional[str], *, max_marks: Optional[Any] = None
) -> GradingResult:
    """Turn a provider's raw response body into a validated GradingResult.

    Raises GradingResponseError for every malformed case. There is no return
    value that means "failed" -- a caller cannot accidentally treat failure as
    a score, which is exactly how the previous parser produced silent zeros.
    """
    if raw_text is None or not str(raw_text).strip():
        raise GradingResponseError("empty_response", "provider returned an empty response")

    payload_text = _extract_json_object(str(raw_text))
    try:
        payload = json.loads(payload_text)
    except (TypeError, ValueError) as exc:
        raise GradingResponseError(
            "malformed_json", f"response was not valid JSON: {exc}", raw=raw_text
        )

    if not isinstance(payload, Mapping):
        raise GradingResponseError(
            "wrong_schema", "response JSON was not an object", raw=raw_text
        )

    if "score" not in payload:
        raise GradingResponseError(
            "score_missing", "response JSON has no 'score' field", raw=raw_text
        )

    try:
        return build_grading_result(
            score=payload.get("score"),
            reason=payload.get("reason"),
            max_marks=max_marks,
        )
    except GradingResponseError as exc:
        # Re-raise with the raw body attached for debugging, preserving the code.
        raise GradingResponseError(exc.code, exc.message, raw=raw_text) from exc


# The shape requested from the provider. Kept here so the prompt text and the
# validator cannot drift apart; it describes JSON, not any particular SDK.
GRADING_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "score": {"type": "number"},
        "reason": {"type": "string"},
    },
    "required": ["score", "reason"],
}

GRADING_OUTPUT_INSTRUCTION = (
    'Respond with JSON only: {"score": <number, 0 to %s>, '
    '"reason": "<one short paragraph>"}'
)


def output_instruction(max_marks: Any) -> str:
    """The single sentence appended to a grading prompt to request the schema."""
    return GRADING_OUTPUT_INSTRUCTION % (max_marks,)
