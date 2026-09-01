"""fractional marks: every score column becomes NUMERIC(7, 2)

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-31

AUDIT C7
--------
Every mark in CogniGrade was stored in an `Integer` column while the grading
domain has produced floats since Correctness Foundation v2 (`GradingResult.score`
is a float precisely so that partial credit is representable). A score of 1.5
therefore could not survive a write: PostgreSQL rejects it, and any path that
coerced it first stored 1 and silently took half a mark off the student. No
`create_all` can fix that -- it only ever creates missing tables -- so this is
the migration the whole Alembic foundation exists for.

WHY NUMERIC AND NOT DOUBLE PRECISION
------------------------------------
A transcript is an exact record. Binary floating point cannot represent 0.1, so
a stored total could read back as 3.7499999999999996. `NUMERIC(7, 2)` stores
the decimal the professor and the student both wrote down. Scale 2 covers
halves, quarters and hundredths; precision 7 allows up to 99999.99, two orders
of magnitude above any real exam total. See `backend/grading/marks.py`.

DATA PRESERVATION
-----------------
Widening `INTEGER` to `NUMERIC(7, 2)` is loss-free in both directions of value:
3 becomes 3.00 and 5 becomes 5.00. PostgreSQL performs this cast implicitly and
rewrites the table in place; no row is touched by application code, so a
partially graded exam keeps every mark it had.

DOWNGRADE IS GUARDED, NOT SILENT
--------------------------------
Going back to `INTEGER` cannot preserve 1.5. Rather than round a student's mark
behind their back, `downgrade()` first counts the non-integral values in every
affected column and REFUSES to run if it finds any, naming the columns and the
row counts. If the data is genuinely all whole numbers the downgrade proceeds
and is loss-free. That is the honest behaviour: a downgrade that quietly
rewrites grades would be worse than one that fails.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


#: (table, column, nullable) for every score column in the schema.
#: `Exam.points_possible` and `Assignment.points_possible` are included because
#: they are the maxima the per-question marks are measured against; leaving them
#: Integer would make an exam out of 37.5 unrepresentable while its questions
#: could hold fractions. Deliberately named for the domain -- marks, grades,
#: points -- never for a grading provider.
MARK_COLUMNS = (
    ("assignments", "points_possible", True),
    ("exam_results", "marks_obtained", True),
    ("exams", "points_possible", True),
    ("question_responses", "marks_obtained", True),
    ("questions", "max_marks", False),
    ("submissions", "grade", True),
)

MARKS_PRECISION = 7
MARKS_SCALE = 2


def _marks_type() -> sa.Numeric:
    # The migration names the plain SQLAlchemy type rather than the application's
    # `Marks` decorator: a migration must keep describing the schema it created
    # even if the application type is later renamed or its Python-side
    # conversion changes.
    return sa.Numeric(precision=MARKS_PRECISION, scale=MARKS_SCALE)


def _alter(table: str, column: str, *, to_type, from_type, nullable: bool, using: str = None) -> None:
    """Change one column's type, portably.

    SQLite cannot `ALTER COLUMN` at all, so alembic's batch mode rebuilds the
    table and copies the rows. PostgreSQL -- production -- takes the direct
    `ALTER TABLE ... TYPE` path, with an explicit `USING` clause where the cast
    is not implicit.
    """
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.alter_column(
                column,
                existing_type=from_type,
                type_=to_type,
                existing_nullable=nullable,
            )
        return

    kwargs = {}
    if using is not None:
        kwargs["postgresql_using"] = using
    op.alter_column(
        table,
        column,
        existing_type=from_type,
        type_=to_type,
        existing_nullable=nullable,
        **kwargs,
    )


def upgrade() -> None:
    for table, column, nullable in MARK_COLUMNS:
        _alter(
            table,
            column,
            from_type=sa.Integer(),
            to_type=_marks_type(),
            nullable=nullable,
            # int -> numeric is an implicit cast in PostgreSQL; stating it
            # anyway documents the intent and costs nothing.
            using=f"{column}::numeric({MARKS_PRECISION},{MARKS_SCALE})",
        )


def _fractional_row_counts(bind) -> list:
    """Columns that hold at least one value `INTEGER` could not represent.

    `col <> CAST(col AS INTEGER)` is true only for a non-integral value and is
    valid on both PostgreSQL and SQLite, so the guard works wherever the
    migration does.
    """
    offenders = []
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())
    for table, column, _nullable in MARK_COLUMNS:
        if table not in existing_tables:
            continue
        count = bind.execute(
            sa.text(
                f"SELECT COUNT(*) FROM {table} "
                f"WHERE {column} IS NOT NULL AND {column} <> CAST({column} AS INTEGER)"
            )
        ).scalar()
        if count:
            offenders.append((table, column, count))
    return offenders


def downgrade() -> None:
    bind = op.get_bind()
    offenders = _fractional_row_counts(bind)
    if offenders:
        detail = ", ".join(f"{t}.{c}: {n} row(s)" for t, c, n in offenders)
        raise RuntimeError(
            "Refusing to downgrade 0002: narrowing NUMERIC(7,2) back to INTEGER "
            f"would destroy fractional marks that exist in the database ({detail}). "
            "Round or clear those values deliberately first if this downgrade is "
            "really intended -- this migration will not do it for you."
        )

    for table, column, nullable in reversed(MARK_COLUMNS):
        _alter(
            table,
            column,
            from_type=_marks_type(),
            to_type=sa.Integer(),
            nullable=nullable,
            # numeric -> integer is only an assignment cast in PostgreSQL, so
            # USING is required here rather than merely documentary. The guard
            # above has already proved every remaining value is integral.
            using=f"{column}::integer",
        )
