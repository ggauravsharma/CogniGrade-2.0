"""Exam aggregation and grading-failure state (audit C6).

The invariant under test, stated once:

    A student must never receive a completed/graded exam result if one or more
    required grading decisions failed.

The bug this replaces was hard to see because it looked defensive::

    total_marks = sum(r.marks_obtained for r in responses
                      if r.marks_obtained is not None)
    exam_result.status = "graded"

Filtering out the NULL marks did not skip those questions -- it scored them
zero, and then declared the exam graded anyway.
"""

import json

import pytest
from sqlalchemy import select

from backend.grading.aggregation import (
    AggregationResult,
    ExamResultStatus,
    aggregate_student_result,
)
from backend.models.tables import ExamResult, Question, QuestionResponse
from backend.routers.examStats import add_exam_result_internal

from .conftest import as_user


class _Row:
    """Minimal stand-in for a QuestionResponse row."""

    def __init__(self, question_id, marks_obtained):
        self.question_id = question_id
        self.marks_obtained = marks_obtained


# ---------------------------------------------------------------------------
# the aggregation rule itself
# ---------------------------------------------------------------------------

def test_all_marked_is_complete():
    agg = aggregate_student_result(
        expected_question_ids=[1, 2],
        responses=[_Row(1, 4), _Row(2, 6)],
    )
    assert agg.total_score == 10
    assert agg.complete is True
    assert agg.is_final is True
    assert agg.status == ExamResultStatus.GRADED
    assert agg.ungraded_question_ids == []


def test_zero_is_a_grade_not_a_gap():
    """0.0 is a real mark. Only None means "no validated result"."""
    agg = aggregate_student_result(
        expected_question_ids=[1, 2],
        responses=[_Row(1, 0), _Row(2, 0)],
    )
    assert agg.total_score == 0
    assert agg.graded_count == 2
    assert agg.complete is True
    assert agg.status == ExamResultStatus.GRADED


def test_a_single_missing_mark_blocks_finalisation():
    agg = aggregate_student_result(
        expected_question_ids=[1, 2, 3],
        responses=[_Row(1, 4), _Row(2, None), _Row(3, 6)],
    )
    assert agg.complete is False
    assert agg.is_final is False
    assert agg.status == ExamResultStatus.GRADING_INCOMPLETE
    assert agg.ungraded_question_ids == [2]
    # Partial progress is preserved and reported, just never called final.
    assert agg.total_score == 10
    assert agg.graded_count == 2


def test_missing_mark_is_not_counted_as_zero():
    """The heart of C6: a failed question must not silently contribute 0."""
    failed = aggregate_student_result(
        expected_question_ids=[1, 2],
        responses=[_Row(1, 5), _Row(2, None)],
    )
    scored_zero = aggregate_student_result(
        expected_question_ids=[1, 2],
        responses=[_Row(1, 5), _Row(2, 0)],
    )
    # The totals coincide -- that is exactly why the old bug was invisible.
    assert failed.total_score == scored_zero.total_score
    # The states must not.
    assert failed.status != scored_zero.status
    assert failed.is_final is False and scored_zero.is_final is True


def test_question_with_no_response_does_not_block():
    """An unattempted question is not a grading failure.

    The database cannot distinguish "the student wrote nothing" from "grading
    was never attempted", so an absent row is reported but does not make the
    exam permanently unfinalisable.
    """
    agg = aggregate_student_result(
        expected_question_ids=[1, 2, 3],
        responses=[_Row(1, 4), _Row(2, 6)],
    )
    assert agg.complete is True
    assert agg.questions_without_response == [3]
    assert agg.ungraded_question_ids == []


def test_no_responses_at_all_is_complete_and_zero():
    agg = aggregate_student_result(expected_question_ids=[1, 2], responses=[])
    assert agg.total_score == 0
    assert agg.complete is True
    assert agg.questions_without_response == [1, 2]


def test_fractional_marks_are_not_truncated():
    """Partial credit must survive aggregation (domain is float; C7 is storage)."""
    agg = aggregate_student_result(
        expected_question_ids=[1, 2, 3],
        responses=[_Row(1, 0.5), _Row(2, 1.5), _Row(3, 2.25)],
    )
    assert agg.total_score == pytest.approx(4.25)
    assert agg.complete is True


def test_every_mark_missing_reports_all_ids():
    agg = aggregate_student_result(
        expected_question_ids=[7, 8],
        responses=[_Row(7, None), _Row(8, None)],
    )
    assert agg.complete is False
    assert agg.ungraded_question_ids == [7, 8]
    assert agg.graded_count == 0
    assert agg.total_score == 0


def test_result_is_immutable():
    agg = aggregate_student_result(expected_question_ids=[1], responses=[_Row(1, 1)])
    assert isinstance(agg, AggregationResult)
    with pytest.raises(Exception):
        agg.total_score = 99  # type: ignore[misc]


def test_status_vocabulary_is_provider_independent():
    for value in ExamResultStatus.ALL:
        assert "gemini" not in value.lower()
        assert "google" not in value.lower()
    assert ExamResultStatus.FINAL == (ExamResultStatus.GRADED,)
    assert ExamResultStatus.GRADING_INCOMPLETE not in ExamResultStatus.FINAL


def test_aggregation_module_imports_nothing_provider_specific():
    """Aggregation is a domain rule; it must not depend on a vendor or the web layer."""
    import pathlib

    import backend.grading.aggregation as mod

    source = pathlib.Path(mod.__file__).read_text(encoding="utf-8")
    import_lines = [
        ln for ln in source.splitlines()
        if ln.startswith("import ") or ln.startswith("from ")
    ]
    banned = ("google", "genai", "gemini", "fastapi", "sqlalchemy", "backend.models")
    for line in import_lines:
        for token in banned:
            assert token not in line.lower(), f"{token} imported in aggregation: {line}"


# ---------------------------------------------------------------------------
# the historical bug, reproduced
# ---------------------------------------------------------------------------

def test_previous_implementation_would_have_reported_graded():
    """Documents the exact old behaviour so the regression cannot come back."""
    responses = [_Row(1, 5), _Row(2, None)]

    # The code as it stood at bb536cd:
    old_total = sum(r.marks_obtained for r in responses if r.marks_obtained is not None)
    old_status = "graded"

    new = aggregate_student_result(expected_question_ids=[1, 2], responses=responses)

    assert old_total == new.total_score                        # same number ...
    assert old_status == ExamResultStatus.GRADED
    assert new.status == ExamResultStatus.GRADING_INCOMPLETE   # ... different claim
    assert new.is_final is False


# ---------------------------------------------------------------------------
# persistence: add_exam_result_internal
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
async def test_complete_grading_is_stored_as_graded(db, world):
    exam, student = world["exam_a"], world["student_a"]
    response = await add_exam_result_internal(exam.id, student.id, db)
    body = json.loads(response.body)

    assert body["result"]["status"] == ExamResultStatus.GRADED
    assert body["result"]["is_final"] is True
    assert body["result"]["marks_obtained"] == 5
    assert body["result"]["graded_at"] is not None

    row = await _fetch_result(db, exam.id, student.id)
    assert row.status == ExamResultStatus.GRADED
    assert row.graded_at is not None


@pytest.mark.asyncio
async def test_failed_question_never_produces_a_graded_result(db, world):
    """The end-to-end statement of the invariant."""
    exam, student = world["exam_a"], world["student_a"]
    q2 = await _add_question(db, exam.id, 2)
    # Grading ran for q2 and produced nothing valid -- Correctness v2 leaves the
    # mark NULL rather than writing a fabricated zero.
    db.add(QuestionResponse(question_id=q2.id, student_id=student.id, marks_obtained=None))
    await db.commit()

    response = await add_exam_result_internal(exam.id, student.id, db)
    body = json.loads(response.body)

    assert body["result"]["status"] == ExamResultStatus.GRADING_INCOMPLETE
    assert body["result"]["is_final"] is False
    assert body["result"]["ungraded_question_ids"] == [q2.id]

    row = await _fetch_result(db, exam.id, student.id)
    assert row.status == ExamResultStatus.GRADING_INCOMPLETE
    # No timestamp: graded_at asserts "this is the student's grade".
    assert row.graded_at is None


@pytest.mark.asyncio
async def test_partial_marks_are_preserved_not_discarded(db, world):
    exam, student = world["exam_a"], world["student_a"]
    q2 = await _add_question(db, exam.id, 2)
    db.add(QuestionResponse(question_id=q2.id, student_id=student.id, marks_obtained=None))
    await db.commit()

    await add_exam_result_internal(exam.id, student.id, db)
    row = await _fetch_result(db, exam.id, student.id)
    # The 5 marks that WERE validated survive; nothing is zeroed out.
    assert row.marks_obtained == 5

    found = await db.execute(select(QuestionResponse).where(
        QuestionResponse.student_id == student.id,
    ))
    marks = sorted(
        r.marks_obtained for r in found.scalars().all() if r.marks_obtained is not None
    )
    assert marks == [5]


@pytest.mark.asyncio
async def test_grading_the_missing_question_recovers_the_result(db, world):
    """Incomplete is a recoverable state, not a dead end."""
    exam, student = world["exam_a"], world["student_a"]
    q2 = await _add_question(db, exam.id, 2)
    missing = QuestionResponse(question_id=q2.id, student_id=student.id, marks_obtained=None)
    db.add(missing)
    await db.commit()

    await add_exam_result_internal(exam.id, student.id, db)
    row = await _fetch_result(db, exam.id, student.id)
    assert row.status == ExamResultStatus.GRADING_INCOMPLETE

    # A professor supplies the missing mark (drop_question, give_full_marks and
    # a successful re-grade all end up here).
    missing.marks_obtained = 3
    await db.commit()
    await add_exam_result_internal(exam.id, student.id, db)

    await db.refresh(row)
    assert row.status == ExamResultStatus.GRADED
    assert row.marks_obtained == 8
    assert row.graded_at is not None


@pytest.mark.asyncio
async def test_a_finalised_result_can_be_demoted_by_a_later_failure(db, world):
    """Status is recomputed from facts, never latched on."""
    exam, student = world["exam_a"], world["student_a"]
    await add_exam_result_internal(exam.id, student.id, db)
    row = await _fetch_result(db, exam.id, student.id)
    assert row.status == ExamResultStatus.GRADED

    # A re-evaluation nulls the mark and then fails to produce a new one.
    world["resp_a"].marks_obtained = None
    await db.commit()
    await add_exam_result_internal(exam.id, student.id, db)

    await db.refresh(row)
    assert row.status == ExamResultStatus.GRADING_INCOMPLETE
    assert row.graded_at is None


@pytest.mark.asyncio
async def test_zero_marks_still_finalise(db, world):
    """A student who genuinely scored 0 must still get a graded result."""
    exam, student = world["exam_a"], world["student_a"]
    world["resp_a"].marks_obtained = 0
    await db.commit()

    await add_exam_result_internal(exam.id, student.id, db)
    row = await _fetch_result(db, exam.id, student.id)
    assert row.status == ExamResultStatus.GRADED
    assert row.marks_obtained == 0
    assert row.graded_at is not None


@pytest.mark.asyncio
async def test_incomplete_result_is_logged_with_ids_only(db, world, caplog):
    exam, student = world["exam_a"], world["student_a"]
    q2 = await _add_question(db, exam.id, 2)
    db.add(QuestionResponse(question_id=q2.id, student_id=student.id, marks_obtained=None))
    await db.commit()

    with caplog.at_level("ERROR"):
        await add_exam_result_internal(exam.id, student.id, db)

    messages = [r.getMessage() for r in caplog.records]
    assert any("exam grading incomplete" in m for m in messages)
    # No student content in the log.
    assert not any(student.email in m for m in messages)


# ---------------------------------------------------------------------------
# the surfaces a human actually sees
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_student_can_read_their_own_submission_status(client, world):
    """Regression: Security v1 gated this student endpoint on exam MANAGER."""
    res = await client.get(
        f"/exams/{world['exam_a'].id}/submission_status",
        headers=as_user(world["student_a"]),
    )
    assert res.status_code == 200
    assert res.json()["status"] in ExamResultStatus.ALL


@pytest.mark.asyncio
async def test_submission_status_reports_incomplete_as_not_final(client, db, world):
    exam, student = world["exam_a"], world["student_a"]
    q2 = await _add_question(db, exam.id, 2)
    db.add(QuestionResponse(question_id=q2.id, student_id=student.id, marks_obtained=None))
    await db.commit()
    await add_exam_result_internal(exam.id, student.id, db)

    res = await client.get(
        f"/exams/{exam.id}/submission_status", headers=as_user(student),
    )
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == ExamResultStatus.GRADING_INCOMPLETE
    assert body["is_final"] is False


@pytest.mark.asyncio
async def test_submission_status_with_no_row_is_pending_and_not_final(client, world):
    res = await client.get(
        f"/exams/{world['exam_a'].id}/submission_status",
        headers=as_user(world["student_b"]),
    )
    assert res.status_code == 200
    assert res.json() == {"status": ExamResultStatus.PENDING, "is_final": False}


@pytest.mark.asyncio
async def test_outsider_still_cannot_read_submission_status(client, world):
    res = await client.get(
        f"/exams/{world['exam_a'].id}/submission_status",
        headers=as_user(world["outsider"]),
    )
    assert res.status_code == 403
