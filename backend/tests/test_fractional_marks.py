"""Fractional marks survive the whole path (audit C7).

The invariant, stated once:

    A mark of 1.5 written anywhere in CogniGrade reads back as 1.5.

Before this phase every score column was `Integer`, so partial credit -- which
`GradingResult.score` has been able to express since Correctness Foundation v2
-- could not be persisted. Half a mark was taken off the student between the
grader and the database, silently.

These tests cover the four places a mark can enter the system (a grading
provider, a professor's manual edit, a bulk full-marks/drop action, and a
re-evaluation) and the two places it leaves (aggregation into an exam total,
and the JSON the API returns).

The C6 invariants from Correctness Foundation v3 are re-asserted here against
the new column type, because the whole point of that phase was the difference
between a zero and a missing mark, and a type change is exactly the kind of
work that blurs it.
"""

import json
import pathlib
from decimal import Decimal

import pytest
from sqlalchemy import Numeric, select

from backend.grading.aggregation import ExamResultStatus, aggregate_student_result
from backend.grading.marks import (
    MARKS_PRECISION,
    MARKS_SCALE,
    MARKS_MAX,
    InvalidMarkError,
    to_decimal,
    to_number,
)
from backend.models.numeric import Marks
from backend.models.tables import (
    Assignment,
    Exam,
    ExamResult,
    Question,
    QuestionResponse,
    Submission,
)
from backend.routers.examStats import add_exam_result_internal

from .conftest import as_user


# ---------------------------------------------------------------------------
# the normalisation rule itself
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "given,expected",
    [
        (0, Decimal("0.00")),
        (3, Decimal("3.00")),
        (0.5, Decimal("0.50")),
        (1.5, Decimal("1.50")),
        (2.25, Decimal("2.25")),
        ("1.5", Decimal("1.50")),
        ("  2.25 ", Decimal("2.25")),
        (Decimal("0.5"), Decimal("0.50")),
    ],
)
def test_to_decimal_normalises_every_accepted_shape(given, expected):
    assert to_decimal(given) == expected


def test_none_is_not_a_mark_and_stays_none():
    """The C6 distinction, at the lowest level it exists."""
    assert to_decimal(None) is None
    assert to_number(None) is None


def test_empty_string_is_no_mark_not_a_zero():
    """Clearing the input box means "not graded", not "scored nothing"."""
    assert to_decimal("") is None
    assert to_decimal("   ") is None


def test_zero_is_a_mark():
    assert to_decimal(0) == Decimal("0.00")
    assert to_number(0) == 0.0
    assert to_number(Decimal("0.00")) == 0.0
    # ... and is emphatically not None, which is what C6 turns on.
    assert to_decimal(0) is not None
    assert to_number(Decimal("0.00")) is not None


def test_float_drift_is_quantised_away_at_the_boundary():
    """0.1 + 0.2 must not reach the database as 0.30000000000000004."""
    assert to_decimal(0.1 + 0.2) == Decimal("0.30")


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), "nan", "inf"])
def test_non_finite_values_are_rejected(bad):
    with pytest.raises(InvalidMarkError) as exc:
        to_decimal(bad)
    assert exc.value.code == "mark_not_finite"


@pytest.mark.parametrize("bad", [True, False])
def test_booleans_are_not_marks(bad):
    with pytest.raises(InvalidMarkError) as exc:
        to_decimal(bad)
    assert exc.value.code == "mark_not_numeric"


@pytest.mark.parametrize("bad", ["abc", "1.5.5", object(), [1]])
def test_non_numeric_values_are_rejected(bad):
    with pytest.raises(InvalidMarkError) as exc:
        to_decimal(bad)
    assert exc.value.code == "mark_not_numeric"


def test_values_beyond_the_column_are_rejected_not_wrapped():
    with pytest.raises(InvalidMarkError) as exc:
        to_decimal(MARKS_MAX + 1)
    assert exc.value.code == "mark_out_of_range"


def test_marks_module_imports_nothing_provider_specific():
    """A mark is a domain value; its rules must not depend on a vendor or the web layer.

    Parsed with `ast` rather than by matching line prefixes, so that prose in a
    docstring that happens to begin with "from ..." cannot be mistaken for an
    import.
    """
    import ast

    import backend.grading.marks as mod

    tree = ast.parse(pathlib.Path(mod.__file__).read_text(encoding="utf-8"))
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")

    banned = ("google", "genai", "gemini", "fastapi", "sqlalchemy", "backend.models")
    for name in imported:
        for token in banned:
            assert token not in name.lower(), f"{token} imported in marks: {name}"


# ---------------------------------------------------------------------------
# schema/model consistency
# ---------------------------------------------------------------------------

SCORE_COLUMNS = [
    (QuestionResponse, "marks_obtained"),
    (ExamResult, "marks_obtained"),
    (Question, "max_marks"),
    (Submission, "grade"),
    (Assignment, "points_possible"),
    (Exam, "points_possible"),
]


@pytest.mark.parametrize("model,column", SCORE_COLUMNS)
def test_score_columns_are_no_longer_integer(model, column):
    col = model.__table__.columns[column]
    assert isinstance(col.type, Marks), f"{model.__name__}.{column} is {col.type!r}"
    assert isinstance(col.type.impl, Numeric)
    assert col.type.impl.precision == MARKS_PRECISION
    assert col.type.impl.scale == MARKS_SCALE


@pytest.mark.parametrize("model,column", SCORE_COLUMNS)
def test_score_columns_compile_to_numeric_on_postgresql(model, column):
    """The check SQLite cannot make.

    SQLite has dynamic typing: it will happily store 1.5 in a column declared
    INTEGER, which is why the rest of this suite could not have caught C7 on
    its own. PostgreSQL -- production -- enforces the declared type, so assert
    the DDL that would actually be emitted there.
    """
    from sqlalchemy.dialects import postgresql

    ddl = model.__table__.columns[column].type.compile(dialect=postgresql.dialect())
    assert ddl.upper().replace(" ", "") == f"NUMERIC({MARKS_PRECISION},{MARKS_SCALE})"


def test_no_score_column_is_named_after_a_provider():
    """Provider-agnostic storage: any grader writes the same columns."""
    from backend.database import Base

    banned = ("gemini", "google", "genai", "openai", "llm", "model_score")
    for table in Base.metadata.tables.values():
        for column in table.columns:
            name = column.name.lower()
            for token in banned:
                assert token not in name, f"{table.name}.{column.name} names a provider"


# ---------------------------------------------------------------------------
# persistence: does the number come back?
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [(0, 0.0), (0.5, 0.5), (1.5, 1.5), (2.25, 2.25), (3, 3.0)])
@pytest.mark.asyncio
async def test_a_mark_survives_write_and_read(db, world, value, expected):
    q = world["q1"]
    response = QuestionResponse(
        question_id=q.id, student_id=world["outsider"].id, marks_obtained=value
    )
    db.add(response)
    await db.commit()
    db.expunge_all()

    found = await db.execute(select(QuestionResponse).where(QuestionResponse.id == response.id))
    row = found.scalars().first()
    assert row.marks_obtained == expected
    # The application boundary hands back a plain float, so JSON encoding and
    # arithmetic in the routers keep working without a Decimal in sight.
    assert isinstance(row.marks_obtained, float)


@pytest.mark.asyncio
async def test_a_missing_mark_reads_back_as_none_not_zero(db, world):
    response = QuestionResponse(
        question_id=world["q1"].id, student_id=world["outsider"].id, marks_obtained=None
    )
    db.add(response)
    await db.commit()
    db.expunge_all()

    found = await db.execute(select(QuestionResponse).where(QuestionResponse.id == response.id))
    assert found.scalars().first().marks_obtained is None


@pytest.mark.asyncio
async def test_a_fractional_max_marks_survives(db, world):
    q = Question(exam_id=world["exam_a"].id, question_number=9, text="Q9", max_marks=2.5)
    db.add(q)
    await db.commit()
    db.expunge_all()

    found = await db.execute(select(Question).where(Question.id == q.id))
    assert found.scalars().first().max_marks == 2.5


@pytest.mark.asyncio
async def test_a_string_mark_from_a_client_is_stored_as_a_number(db, world):
    """The manual-edit UI posts strings; the column type is the last line of defence."""
    response = QuestionResponse(
        question_id=world["q1"].id, student_id=world["outsider"].id, marks_obtained="1.5"
    )
    db.add(response)
    await db.commit()
    db.expunge_all()

    found = await db.execute(select(QuestionResponse).where(QuestionResponse.id == response.id))
    assert found.scalars().first().marks_obtained == 1.5


# ---------------------------------------------------------------------------
# aggregation
# ---------------------------------------------------------------------------

async def _add_question(db, exam_id, number, max_marks=10):
    q = Question(exam_id=exam_id, question_number=number, text=f"Q{number}", max_marks=max_marks)
    db.add(q)
    await db.commit()
    await db.refresh(q)
    return q


async def _fetch_result(db, exam_id, student_id):
    found = await db.execute(select(ExamResult).where(
        ExamResult.exam_id == exam_id,
        ExamResult.student_id == student_id,
    ))
    return found.scalars().first()


@pytest.mark.asyncio
async def test_fractional_marks_aggregate_to_a_fractional_total(db, world):
    """1.5 + 2.25 + 0 == 3.75, stored, and final."""
    exam, student = world["exam_a"], world["outsider"]
    q1 = await _add_question(db, exam.id, 11, max_marks=2)
    q2 = await _add_question(db, exam.id, 12, max_marks=3)
    q3 = await _add_question(db, exam.id, 13, max_marks=5)
    db.add_all([
        QuestionResponse(question_id=q1.id, student_id=student.id, marks_obtained=1.5),
        QuestionResponse(question_id=q2.id, student_id=student.id, marks_obtained=2.25),
        QuestionResponse(question_id=q3.id, student_id=student.id, marks_obtained=0),
    ])
    await db.commit()

    response = await add_exam_result_internal(exam.id, student.id, db)
    body = json.loads(response.body)

    assert body["result"]["marks_obtained"] == 3.75
    assert body["result"]["complete"] is True
    assert body["result"]["is_final"] is True
    assert body["result"]["status"] == ExamResultStatus.GRADED

    db.expunge_all()
    row = await _fetch_result(db, exam.id, student.id)
    assert row.marks_obtained == 3.75


@pytest.mark.asyncio
async def test_the_stored_total_is_the_exact_decimal_not_float_drift(db, world):
    """0.1 + 0.2 aggregates to a total the database records as 0.30."""
    exam, student = world["exam_a"], world["outsider"]
    q1 = await _add_question(db, exam.id, 21, max_marks=1)
    q2 = await _add_question(db, exam.id, 22, max_marks=1)
    db.add_all([
        QuestionResponse(question_id=q1.id, student_id=student.id, marks_obtained=0.1),
        QuestionResponse(question_id=q2.id, student_id=student.id, marks_obtained=0.2),
    ])
    await db.commit()

    await add_exam_result_internal(exam.id, student.id, db)
    db.expunge_all()
    row = await _fetch_result(db, exam.id, student.id)
    assert row.marks_obtained == 0.3


def test_aggregation_of_fractions_is_exact_in_the_domain():
    class _Row:
        def __init__(self, qid, mark):
            self.question_id = qid
            self.marks_obtained = mark

    agg = aggregate_student_result(
        expected_question_ids=[1, 2, 3],
        responses=[_Row(1, 1.5), _Row(2, 2.25), _Row(3, 0)],
    )
    assert agg.total_score == 3.75
    assert agg.complete is True
    assert agg.graded_count == 3


# ---------------------------------------------------------------------------
# C6 regression: the type change must not blur zero and missing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_missing_mark_still_blocks_finalisation(db, world):
    exam, student = world["exam_a"], world["outsider"]
    q1 = await _add_question(db, exam.id, 31, max_marks=2)
    q2 = await _add_question(db, exam.id, 32, max_marks=2)
    db.add_all([
        QuestionResponse(question_id=q1.id, student_id=student.id, marks_obtained=1.5),
        QuestionResponse(question_id=q2.id, student_id=student.id, marks_obtained=None),
    ])
    await db.commit()

    body = json.loads((await add_exam_result_internal(exam.id, student.id, db)).body)
    assert body["result"]["status"] == ExamResultStatus.GRADING_INCOMPLETE
    assert body["result"]["is_final"] is False
    assert body["result"]["graded_at"] is None
    # The partial total is still 1.5 -- reported, never presented as final.
    assert body["result"]["marks_obtained"] == 1.5


@pytest.mark.asyncio
async def test_a_fractional_zero_finalises_like_any_other_mark(db, world):
    """Decimal("0.00") is a grade. It must not be mistaken for a gap."""
    exam, student = world["exam_a"], world["outsider"]
    q1 = await _add_question(db, exam.id, 41, max_marks=2)
    db.add(QuestionResponse(question_id=q1.id, student_id=student.id, marks_obtained=0.0))
    await db.commit()

    body = json.loads((await add_exam_result_internal(exam.id, student.id, db)).body)
    assert body["result"]["status"] == ExamResultStatus.GRADED
    assert body["result"]["is_final"] is True
    assert body["result"]["marks_obtained"] == 0.0


# ---------------------------------------------------------------------------
# manual grading, through the real endpoints
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_professor_can_enter_a_fractional_mark(client, db, world):
    """The exact path the UI uses: a PATCH carrying the input's string value."""
    exam, q, student = world["exam_a"], world["q1"], world["student_a"]

    res = await client.patch(
        f"/exams/{exam.id}/student/{student.id}/question/{q.id}/update",
        json={"grade": "1.5"},
        headers=as_user(world["owner_prof"]),
    )
    assert res.status_code == 200

    db.expunge_all()
    found = await db.execute(select(QuestionResponse).where(
        QuestionResponse.question_id == q.id,
        QuestionResponse.student_id == student.id,
    ))
    assert found.scalars().first().marks_obtained == 1.5


@pytest.mark.asyncio
async def test_a_manual_fractional_mark_reaches_the_exam_total(client, db, world):
    exam, q, student = world["exam_a"], world["q1"], world["student_a"]
    await client.patch(
        f"/exams/{exam.id}/student/{student.id}/question/{q.id}/update",
        json={"grade": 2.25},
        headers=as_user(world["owner_prof"]),
    )
    db.expunge_all()
    row = await _fetch_result(db, exam.id, student.id)
    assert row.marks_obtained == 2.25
    assert row.status == ExamResultStatus.GRADED


@pytest.mark.asyncio
async def test_a_nonsense_manual_mark_is_a_400_not_a_500(client, world):
    exam, q, student = world["exam_a"], world["q1"], world["student_a"]
    res = await client.patch(
        f"/exams/{exam.id}/student/{student.id}/question/{q.id}/update",
        json={"grade": "not a number"},
        headers=as_user(world["owner_prof"]),
    )
    assert res.status_code == 400
    assert "grade" in res.json()["detail"]


@pytest.mark.asyncio
async def test_update_student_response_keeps_a_fractional_mark(client, db, world):
    exam, q, student = world["exam_a"], world["q1"], world["student_b"]
    res = await client.patch(
        f"/exam/{exam.id}/question/{q.id}/student/{student.id}/update",
        json={"marks_obtained": "0.5", "response": "answer", "reasoning": "half"},
        headers=as_user(world["owner_prof"]),
    )
    assert res.status_code == 200

    db.expunge_all()
    found = await db.execute(select(QuestionResponse).where(
        QuestionResponse.question_id == q.id,
        QuestionResponse.student_id == student.id,
    ))
    assert found.scalars().first().marks_obtained == 0.5


@pytest.mark.asyncio
async def test_update_student_response_without_marks_leaves_them_alone(client, db, world):
    """Omitting the field must not null a mark -- that would look like a grading failure."""
    exam, q, student = world["exam_a"], world["q1"], world["student_b"]
    res = await client.patch(
        f"/exam/{exam.id}/question/{q.id}/student/{student.id}/update",
        json={"response": "edited text only"},
        headers=as_user(world["owner_prof"]),
    )
    assert res.status_code == 200

    db.expunge_all()
    found = await db.execute(select(QuestionResponse).where(
        QuestionResponse.question_id == q.id,
        QuestionResponse.student_id == student.id,
    ))
    assert found.scalars().first().marks_obtained == 7


# ---------------------------------------------------------------------------
# bulk professor actions
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_full_marks_awards_a_fractional_maximum(client, db, world):
    """A question worth 2.5 must award 2.5, not 2."""
    exam, student = world["exam_a"], world["student_a"]
    q = await _add_question(db, exam.id, 51, max_marks=2.5)
    db.add(QuestionResponse(question_id=q.id, student_id=student.id, marks_obtained=None))
    await db.commit()

    res = await client.post(
        f"/exam/{exam.id}/question/{q.id}/full-marks",
        headers=as_user(world["owner_prof"]),
    )
    assert res.status_code == 200

    db.expunge_all()
    found = await db.execute(select(QuestionResponse).where(
        QuestionResponse.question_id == q.id,
        QuestionResponse.student_id == student.id,
    ))
    assert found.scalars().first().marks_obtained == 2.5


@pytest.mark.asyncio
async def test_dropping_a_question_writes_a_real_zero(client, db, world):
    """Zero, not NULL: a dropped question is graded, so the exam can finalise."""
    exam, student = world["exam_a"], world["student_a"]
    q = await _add_question(db, exam.id, 61, max_marks=1.5)
    db.add(QuestionResponse(question_id=q.id, student_id=student.id, marks_obtained=None))
    await db.commit()

    res = await client.post(
        f"/exam/{exam.id}/question/{q.id}/drop",
        headers=as_user(world["owner_prof"]),
    )
    assert res.status_code == 200

    db.expunge_all()
    found = await db.execute(select(QuestionResponse).where(
        QuestionResponse.question_id == q.id,
        QuestionResponse.student_id == student.id,
    ))
    mark = found.scalars().first().marks_obtained
    assert mark == 0
    assert mark is not None


# ---------------------------------------------------------------------------
# re-evaluation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reevaluation_stores_a_fractional_replacement_score(client, db, world, monkeypatch):
    """A re-grade that returns 1.5 must replace the old mark with 1.5."""
    import backend.routers.examStats as stats

    exam, q, student = world["exam_a"], world["q1"], world["student_a"]

    async def _fake_extract(payload, db_, user):
        return {"status": "ok"}

    async def _fake_grade(payload, db_, user):
        found = await db_.execute(select(QuestionResponse).where(
            QuestionResponse.question_id == payload["question_id"],
            QuestionResponse.student_id == payload["student_id"],
        ))
        row = found.scalars().first()
        row.marks_obtained = 1.5
        await db_.commit()
        return {"status": "graded", "grade": 1.5, "reasoning": "half credit"}

    monkeypatch.setattr(stats, "extract_single_answer_text", _fake_extract)
    monkeypatch.setattr(stats, "grade_question_with_diagram", _fake_grade)

    res = await client.post(
        f"/exam/{exam.id}/question/{q.id}/student/{student.id}/reevaluate",
        headers=as_user(world["owner_prof"]),
    )
    assert res.status_code == 200

    db.expunge_all()
    found = await db.execute(select(QuestionResponse).where(
        QuestionResponse.question_id == q.id,
        QuestionResponse.student_id == student.id,
    ))
    assert found.scalars().first().marks_obtained == 1.5


@pytest.mark.asyncio
async def test_a_failed_reevaluation_restores_a_fractional_mark(client, db, world, monkeypatch):
    """Correctness v2's non-destructive restore must survive the type change."""
    import backend.routers.examStats as stats

    exam, q, student = world["exam_a"], world["q1"], world["student_a"]

    found = await db.execute(select(QuestionResponse).where(
        QuestionResponse.question_id == q.id,
        QuestionResponse.student_id == student.id,
    ))
    row = found.scalars().first()
    row.marks_obtained = 2.25
    await db.commit()

    async def _fake_extract(payload, db_, user):
        return {"status": "ok"}

    async def _fake_grade(payload, db_, user):
        return {"status": "grading_failed", "grade": None, "error_code": "score_not_numeric"}

    monkeypatch.setattr(stats, "extract_single_answer_text", _fake_extract)
    monkeypatch.setattr(stats, "grade_question_with_diagram", _fake_grade)

    res = await client.post(
        f"/exam/{exam.id}/question/{q.id}/student/{student.id}/reevaluate",
        headers=as_user(world["owner_prof"]),
    )
    assert res.status_code == 200

    db.expunge_all()
    found = await db.execute(select(QuestionResponse).where(
        QuestionResponse.question_id == q.id,
        QuestionResponse.student_id == student.id,
    ))
    assert found.scalars().first().marks_obtained == 2.25


# ---------------------------------------------------------------------------
# the API surface
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_stats_endpoint_serialises_fractional_totals(client, db, world):
    """A Decimal would break JSONResponse; the boundary must hand over floats."""
    exam, student = world["exam_a"], world["student_a"]
    q = await _add_question(db, exam.id, 71, max_marks=2.5)
    db.add(QuestionResponse(question_id=q.id, student_id=student.id, marks_obtained=1.5))
    await db.commit()
    await add_exam_result_internal(exam.id, student.id, db)

    res = await client.get(f"/exams/{exam.id}/stats", headers=as_user(world["owner_prof"]))
    assert res.status_code == 200
    body = res.json()
    row = next(s for s in body["students"] if s["id"] == student.id)
    assert row["total_marks"] == 6.5      # 5 from the fixture response + 1.5
    assert row["is_final"] is True


@pytest.mark.asyncio
async def test_the_student_evaluation_endpoint_reports_fractional_marks(client, db, world):
    exam, q, student = world["exam_a"], world["q1"], world["student_a"]
    found = await db.execute(select(QuestionResponse).where(
        QuestionResponse.question_id == q.id,
        QuestionResponse.student_id == student.id,
    ))
    found.scalars().first().marks_obtained = 0.5
    await db.commit()

    res = await client.get(
        f"/exam/{exam.id}/student-evaluation/{student.id}",
        headers=as_user(world["owner_prof"]),
    )
    assert res.status_code == 200
    entry = next(e for e in res.json() if e["question_id"] == q.id)
    assert entry["marks_obtained"] == 0.5
    assert entry["max_marks"] == 10
