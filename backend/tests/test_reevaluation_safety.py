"""Re-evaluation must never destroy a mark, and the stage must follow the result.

TWO DEFECTS, BOTH FOUND ON THE LIVE EXAM 4.

1. `/reevaluate` cleared `marks_obtained` and COMMITTED before re-grading, then
   called `extract_single_answer_text`, which did `json.loads(qr.ans_text_images)`
   guarded only by `json.JSONDecodeError`. Every automatically prepared response
   has that column NULL, so `json.loads(None)` raised `TypeError` -- not a
   JSONDecodeError -- and escaped past the restore path. A correctly graded
   answer was left with no mark and the professor got a 500. All five of exam
   4's responses were in exactly that shape.

2. `add_exam_result_internal` finalised the RESULT but never touched
   `exam_stage`, which only `tasks._process_and_grade` wrote. Finalising through
   the supported route left `status=graded` with `graded_at` set beside an exam
   still reporting stage 6, "grading".

No network and no provider quota: the vendor call is replaced by the recording
provider, so the prompt, request assembly, retry loop and telemetry all stay real.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from backend.grading.aggregation import ExamResultStatus
from backend.models.tables import ExamResult, QuestionResponse
from backend.routers.exams import EXAM_STAGE_GRADED, EXAM_STAGE_GRADING
from backend.routers.examStats import add_exam_result_internal, send_for_reevaluation

GOOD = '{"score": 3, "reason": "re-graded, partially correct"}'


async def _reevaluate(world, db, response):
    """Call the real route function for one response."""
    return await send_for_reevaluation(
        exam_id=world["exam_a"].id,
        question_id=world["q1"].id,
        student_id=response.student_id,
        db=db,
        ctx=None,
        current_user=world["owner_prof"],
    )


async def _prepare_auto_response(db, response, *, marks, answer_text="the student's answer"):
    """Shape a row the way automatic preparation leaves it: text, NO crops."""
    response.answer_text = answer_text
    response.ans_text_images = None          # the exact live shape
    response.ans_table_images = None
    response.ans_diagram_images = None
    response.marks_obtained = marks
    response.reasoning = "original grading reason"
    response.grading_error_code = None
    await db.commit()
    await db.refresh(response)
    return response


# ---------------------------------------------------------------------------
# BUG 1 -- the crash, and the mark it used to destroy
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reevaluating_an_auto_prepared_response_does_not_crash(world, db, fake_provider):
    """`ans_text_images IS NULL` is the NORMAL auto-prepared shape, not an error."""
    fake_provider(body=GOOD)
    resp = await _prepare_auto_response(db, world["resp_b"], marks=7)
    assert resp.ans_text_images is None

    out = await _reevaluate(world, db, resp)

    assert out["status"] == "graded"
    await db.refresh(resp)
    assert resp.marks_obtained == 3, "the new grade should have been written"
    assert resp.grading_error_code is None


@pytest.mark.asyncio
async def test_a_failed_reevaluation_keeps_the_previous_mark(world, db, fake_provider):
    """The whole point: a failure must leave the student's grade exactly as it was."""
    fake_provider(body="I am unable to grade this answer.")   # -> not_json
    resp = await _prepare_auto_response(db, world["resp_b"], marks=7)

    out = await _reevaluate(world, db, resp)

    assert out["status"] == "reevaluation_failed"
    await db.refresh(resp)
    assert resp.marks_obtained == 7, "a failed re-evaluation must not clear the mark"
    assert resp.reasoning == "original grading reason", "the original reason survives too"
    assert resp.grading_error_code is None, "no stale failure code beside a valid mark"


@pytest.mark.asyncio
async def test_an_exception_during_reevaluation_keeps_the_previous_mark(
    world, db, fake_provider, monkeypatch
):
    """The regression itself: something RAISING must not leave a NULL behind.

    This is what `json.loads(None)` used to do. Any raise is now caught and the
    row is restored, so the class of bug is closed and not just this instance.
    """
    fake_provider(body=GOOD)
    resp = await _prepare_auto_response(db, world["resp_b"], marks=7)

    async def _boom(*args, **kwargs):
        raise TypeError("the JSON object must be str, bytes or bytearray, not NoneType")

    monkeypatch.setattr(
        "backend.routers.examStats.extract_single_answer_text", _boom
    )

    out = await _reevaluate(world, db, resp)

    assert out["status"] == "reevaluation_failed"
    await db.refresh(resp)
    assert resp.marks_obtained == 7, "an exception must never destroy a mark"
    assert resp.reasoning == "original grading reason"


@pytest.mark.asyncio
async def test_a_successful_reevaluation_replaces_mark_and_clears_the_failure_code(
    world, db, fake_provider
):
    fake_provider(body=GOOD)
    resp = await _prepare_auto_response(db, world["resp_b"], marks=7)
    resp.grading_error_code = "malformed_json"
    await db.commit()

    out = await _reevaluate(world, db, resp)

    assert out["status"] == "graded"
    await db.refresh(resp)
    assert resp.marks_obtained == 3
    assert resp.reasoning == "re-graded, partially correct"
    assert resp.grading_error_code is None


@pytest.mark.asyncio
async def test_a_genuine_zero_is_a_valid_reevaluation_result(world, db, fake_provider):
    """0 is a grade. It must be written, and must not be read as "no result"."""
    fake_provider(body='{"score": 0, "reason": "nothing correct"}')
    resp = await _prepare_auto_response(db, world["resp_b"], marks=7)

    out = await _reevaluate(world, db, resp)

    assert out["status"] == "graded"
    await db.refresh(resp)
    assert resp.marks_obtained == 0
    assert resp.marks_obtained is not None, "a real zero is not a missing mark"
    assert resp.grading_error_code is None


@pytest.mark.asyncio
async def test_a_genuine_zero_already_stored_is_not_lost_by_a_failed_reevaluation(
    world, db, fake_provider
):
    fake_provider(body="prose, not a grade")
    resp = await _prepare_auto_response(db, world["resp_b"], marks=0)

    out = await _reevaluate(world, db, resp)

    assert out["status"] == "reevaluation_failed"
    await db.refresh(resp)
    assert resp.marks_obtained == 0, "a stored zero must survive, not become NULL"


@pytest.mark.asyncio
async def test_a_fractional_mark_survives_reevaluation(world, db, fake_provider):
    fake_provider(body='{"score": 2.5, "reason": "half credit"}')
    resp = await _prepare_auto_response(db, world["resp_b"], marks=7)

    out = await _reevaluate(world, db, resp)

    assert out["status"] == "graded"
    await db.refresh(resp)
    assert float(resp.marks_obtained) == 2.5


@pytest.mark.asyncio
async def test_a_failed_reevaluation_reports_a_safe_sentence_not_provider_text(
    world, db, fake_provider
):
    """Codes and sentences only -- never the provider's own body."""
    marker = "RAW-PROVIDER-TEXT-THAT-MUST-NOT-LEAK"
    fake_provider(body=marker)
    resp = await _prepare_auto_response(db, world["resp_b"], marks=7)

    out = await _reevaluate(world, db, resp)

    assert marker not in out["message"]
    assert "previous mark was kept" in out["message"]


# ---------------------------------------------------------------------------
# BUG 2 -- the stage must not disagree with the result
# ---------------------------------------------------------------------------

async def _result_for(db, world, student):
    found = await db.execute(select(ExamResult).where(
        ExamResult.exam_id == world["exam_a"].id,
        ExamResult.student_id == student.id,
    ))
    return found.scalars().first()


@pytest.mark.asyncio
async def test_a_final_result_moves_the_exam_to_graded(world, db):
    """Both of student A's and B's responses carry marks, so this is complete."""
    exam, student = world["exam_a"], world["student_a"]
    assert exam.exam_stage != EXAM_STAGE_GRADED

    await add_exam_result_internal(exam.id, student.id, db)

    await db.refresh(exam)
    result = await _result_for(db, world, student)
    assert result.status == ExamResultStatus.GRADED
    assert result.graded_at is not None
    assert exam.exam_stage == EXAM_STAGE_GRADED, "the stage must follow the result"


@pytest.mark.asyncio
async def test_a_zero_total_still_reaches_graded(world, db):
    """A student who scored 0 has still been graded; the stage must say so."""
    exam, student = world["exam_a"], world["student_a"]
    world["resp_a"].marks_obtained = 0
    await db.commit()

    await add_exam_result_internal(exam.id, student.id, db)

    await db.refresh(exam)
    result = await _result_for(db, world, student)
    assert float(result.marks_obtained) == 0.0
    assert result.status == ExamResultStatus.GRADED
    assert exam.exam_stage == EXAM_STAGE_GRADED


@pytest.mark.asyncio
async def test_an_incomplete_result_never_reaches_graded(world, db):
    exam, student = world["exam_a"], world["student_a"]
    original_stage = exam.exam_stage
    world["resp_a"].marks_obtained = None          # grading failed for this one
    await db.commit()

    await add_exam_result_internal(exam.id, student.id, db)

    await db.refresh(exam)
    result = await _result_for(db, world, student)
    assert result.status == ExamResultStatus.GRADING_INCOMPLETE
    assert result.graded_at is None
    assert exam.exam_stage != EXAM_STAGE_GRADED
    assert exam.exam_stage == original_stage, (
        "an exam that never reached grading must not be dragged forward either"
    )


@pytest.mark.asyncio
async def test_a_later_failure_demotes_an_exam_that_was_graded(world, db):
    """The stage is demoted only from GRADED, mirroring the result's own demotion."""
    exam, student = world["exam_a"], world["student_a"]
    await add_exam_result_internal(exam.id, student.id, db)
    await db.refresh(exam)
    assert exam.exam_stage == EXAM_STAGE_GRADED

    world["resp_a"].marks_obtained = None
    await db.commit()
    await add_exam_result_internal(exam.id, student.id, db)

    await db.refresh(exam)
    result = await _result_for(db, world, student)
    assert result.status == ExamResultStatus.GRADING_INCOMPLETE
    assert result.graded_at is None
    assert exam.exam_stage == EXAM_STAGE_GRADING


@pytest.mark.asyncio
async def test_a_fractional_total_finalises_exactly(world, db):
    exam, student = world["exam_a"], world["student_a"]
    world["resp_a"].marks_obtained = 2.25
    await db.commit()

    await add_exam_result_internal(exam.id, student.id, db)

    result = await _result_for(db, world, student)
    assert float(result.marks_obtained) == 2.25, "fractional marks must not be truncated"
    assert result.status == ExamResultStatus.GRADED
    await db.refresh(exam)
    assert exam.exam_stage == EXAM_STAGE_GRADED
