"""Provider-neutral normalisation of a mark value.

CogniGrade must be able to persist partial credit exactly::

    0      0.5      1.5      2.25      5

Audit C7: every marks column was `Integer`, so the fractional part of a score
was destroyed on write (or rejected outright by the driver). Fixing the column
type is necessary but not sufficient -- a mark arrives from many places and in
many shapes, and each of them has to land on the same value:

    a grading provider           -> float          (GradingResult.score)
    a professor typing in the UI -> str            ("1.5", from an <input>)
    a JSON API client            -> int | float
    a future human/ensemble path -> Decimal

This module is the ONE place that decides what those become. It is pure: no
SQLAlchemy, no FastAPI, no model SDK, no `backend.models` import (asserted by a
token-level test, as in `result.py` and `aggregation.py`). A mark is a domain
value, not a provider detail, so the same rules must hold whether the grade came
from Gemini, an open VLM, a specialist grader, an ensemble, or a human.

THE NUMERIC CONTRACT
--------------------
Storage is exact decimal, `NUMERIC(7, 2)`:

* **scale 2** covers the marking granularity institutions actually use --
  halves, quarters, tenths, and hundredths for percentage-derived rubrics. It
  does not represent exact thirds; 1/3 of a mark is 0.33, which is how such a
  scheme is written down anyway.
* **precision 7** allows -99999.99 .. 99999.99. Per-question marks are
  single or double digits and exam totals are in the hundreds, so this is two
  orders of magnitude of headroom without reserving storage for numbers the
  domain cannot produce.

Binary floating point is deliberately NOT the storage type: `REAL`/`DOUBLE`
cannot represent 0.1 exactly, and a transcript is a financial-grade record.

THE FLOAT BOUNDARY
------------------
Values are held as `Decimal` at the database boundary and handed to the
application as `float` (`to_number`). One direction, one place -- see
`backend/models/numeric.py`. The application layer therefore keeps working in
floats exactly as it did (`GradingResult.score` is a float, aggregation sums
floats, JSON encoders emit numbers), while what reaches the disk is exact
decimal quantised to the scale above. Any float drift a sum introduces
(0.1 + 0.2 == 0.30000000000000004) is quantised away at the write boundary
rather than accumulating in the database.

ZERO IS NOT MISSING
-------------------
`None` in, `None` out -- always. Correctness Foundation v3 depends on the
difference between "the student earned 0" and "grading produced no result",
and nothing here may blur it. `0`, `0.0`, `"0"` and `Decimal("0.00")` all
normalise to a real zero mark.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Optional

#: Total significant digits stored for a mark.
MARKS_PRECISION = 7
#: Digits after the decimal point. See "THE NUMERIC CONTRACT" above.
MARKS_SCALE = 2

#: The smallest representable difference between two marks: Decimal("0.01").
MARK_QUANTUM = Decimal(1).scaleb(-MARKS_SCALE)
#: Largest value the column can hold, derived from precision/scale rather than
#: written out, so changing either constant cannot leave this stale.
MARKS_MAX = Decimal(10) ** (MARKS_PRECISION - MARKS_SCALE) - MARK_QUANTUM
MARKS_MIN = -MARKS_MAX


class InvalidMarkError(ValueError):
    """A value was offered as a mark but cannot be one.

    Carries a machine-readable `code` so a route can turn it into a 400 without
    matching on message text -- the same convention as
    `GradingResponseError`.
    """

    def __init__(self, code: str, message: str, *, value: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.value = value

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"InvalidMarkError(code={self.code!r}, message={self.message!r})"


def to_decimal(value: Any) -> Optional[Decimal]:
    """Normalise any accepted mark shape to an exact quantised `Decimal`.

    `None` passes through as `None`: it means "no mark", which is not the same
    as zero and must never be invented.

    Accepts `int`, `float`, `Decimal`, and numeric `str` (the UI posts strings).
    Rejects `bool` explicitly -- in Python `True` is an `int`, and awarding a
    student one mark because something emitted `true` would be absurd; this
    mirrors `_coerce_score` in `result.py`.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise InvalidMarkError("mark_not_numeric", "a mark must be a number, not a boolean", value=value)

    if isinstance(value, Decimal):
        candidate = value
    elif isinstance(value, int):
        candidate = Decimal(value)
    elif isinstance(value, float):
        # str() first: Decimal(0.1) is 0.1000000000000000055511151231257827,
        # while Decimal("0.1") is the number the caller meant.
        candidate = Decimal(str(value))
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            # An empty input box is "no mark", not a zero. studentBackend
            # already renders a missing mark as "", so this round-trips.
            return None
        try:
            candidate = Decimal(text)
        except InvalidOperation:
            raise InvalidMarkError("mark_not_numeric", f"{value!r} is not a number", value=value)
    else:
        raise InvalidMarkError(
            "mark_not_numeric",
            f"a mark must be a number, got {type(value).__name__}",
            value=value,
        )

    if not candidate.is_finite():
        # Decimal("nan") and Decimal("Infinity") parse happily. A NaN also
        # defeats every range comparison, so it must be rejected by kind, not
        # by bounds -- the same trap Correctness v2 found in the old parser.
        raise InvalidMarkError("mark_not_finite", "a mark must be finite", value=value)

    quantised = candidate.quantize(MARK_QUANTUM, rounding=ROUND_HALF_UP)

    if quantised > MARKS_MAX or quantised < MARKS_MIN:
        raise InvalidMarkError(
            "mark_out_of_range",
            f"a mark must lie between {MARKS_MIN} and {MARKS_MAX}",
            value=value,
        )
    return quantised


def to_number(value: Any) -> Optional[float]:
    """The read side of the boundary: a stored mark as a JSON-safe `float`.

    `Decimal` is not JSON-serialisable and would break every `JSONResponse` in
    the exam routes, so the conversion happens once, here, instead of being
    scattered as `float(...)` calls across the routers.
    """
    if value is None:
        return None
    if isinstance(value, float):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bool):
        raise InvalidMarkError("mark_not_numeric", "a mark must be a number, not a boolean", value=value)
    if isinstance(value, int):
        return float(value)
    decimal_value = to_decimal(value)
    return None if decimal_value is None else float(decimal_value)
