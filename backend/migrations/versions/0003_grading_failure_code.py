"""grading failure code: why a response has no mark

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-31

A professor could already be told that an exam result is `grading_incomplete`,
but not which question failed or why -- `grade_exam_logic` computes
`failed_questions` and the Celery task discards it. This column is the smallest
thing that makes the answer durable.

ONE NULLABLE COLUMN, ON PURPOSE
-------------------------------
Not a job table, not an attempt log, not an event ledger. "This response has no
mark because X" is a fact about the response, and storing it as one is what
makes retries self-healing: writing a valid mark clears the code in the same
transaction, so a stale failure cannot outlive the failure it describes.

The value is a provider-neutral code (`score_missing`, `malformed_json`, ...),
never a provider name and never the model's raw text -- see
`backend/grading/failure.py`, which maps the code to the sentence shown.

Adding a nullable column is loss-free in both directions of data: existing rows
get NULL, which reads as "nothing went wrong", and every one of them is either
already graded or was never attempted. The downgrade drops it, losing only
diagnostic metadata -- no mark, no student work -- which is why it is safe to
run unguarded, unlike 0002.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "question_responses"
COLUMN = "grading_error_code"


def upgrade() -> None:
    # SQLite cannot ALTER, so batch mode rebuilds the table; on PostgreSQL this
    # is a plain ADD COLUMN, which does not rewrite the table.
    with op.batch_alter_table(TABLE, schema=None) as batch_op:
        batch_op.add_column(sa.Column(COLUMN, sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table(TABLE, schema=None) as batch_op:
        batch_op.drop_column(COLUMN)
