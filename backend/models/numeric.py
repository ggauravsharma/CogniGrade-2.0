"""The single database boundary for mark-shaped columns.

`Marks` is the column type every score in CogniGrade uses. It stores
`NUMERIC(7, 2)` -- exact decimal, see `backend/grading/marks.py` for why -- and
converts in exactly two places:

    write   anything acceptable as a mark  ->  quantised Decimal
    read    Decimal (or a dialect's float) ->  float

That is the whole "Decimal vs float" policy. Application code, JSON encoders
and the aggregation module keep seeing plain numbers, and no router needs a
`float(...)` call of its own; the conversion cannot drift out of step because
there is only one copy of it.

PROVIDER NEUTRALITY
-------------------
The type is named for the domain concept, not for whoever produced the number.
A human grader, an open-weights model, an ensemble or a future specialist
grading service writes through the same column with the same guarantees.
"""

from __future__ import annotations

from sqlalchemy import Numeric
from sqlalchemy.types import TypeDecorator

from backend.grading.marks import (
    MARKS_PRECISION,
    MARKS_SCALE,
    to_decimal,
    to_number,
)


class Marks(TypeDecorator):
    """`NUMERIC(7, 2)` in the database, `float | None` in Python."""

    impl = Numeric(MARKS_PRECISION, MARKS_SCALE)
    #: The type carries no per-instance state, so SQLAlchemy may cache
    #: statements compiled against it.
    cache_ok = True

    def process_bind_param(self, value, dialect):
        # Raises InvalidMarkError for a value that is not a mark. Routes that
        # accept user input normalise first so the failure surfaces as a 400
        # rather than as a StatementError at flush time.
        return to_decimal(value)

    def process_result_value(self, value, dialect):
        # SQLite hands back a float here and Postgres a Decimal; both arrive as
        # the same float, so no caller has to know which database it is on.
        return to_number(value)
