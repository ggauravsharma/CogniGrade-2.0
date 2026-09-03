"""Who may start AI grading, and for whom (UI-3).

`POST /exam/{exam_id}/enqueue-processing` used to read `current_user.id` and
nothing else. The only caller in the product was the tail of the student's crop
submit, so the entire AI pipeline ran as a side effect of a student finishing an
annotation session, and an instructor calling the same route queued a run
against their OWN id -- which has no answer script and no responses.

The route now accepts an optional `student_id`, honoured only for exam managers,
matching the pattern `/protected-files/exam/{id}/document/{type}` already uses.
Two things have to hold for that to be safe, and both are tested here:

  1. Naming a student must grant nothing. A manager may act on their own exam's
     students and no one else; a student may act only on themselves.
  2. A run must be refused when there is NOTHING to read: no prepared responses
     AND no uploaded answer script. Zero responses alone is no longer a
     refusal -- `_process_and_grade` prepares them from the script itself (see
     backend/grading/preparation.py), which is what makes the flow AI-first
     instead of dependent on a student cutting their own paper up.

     The invariant the old check protected is untouched; it lives where
     aggregation happens. `aggregate_student_result` finalises when every
     response that EXISTS carries a mark, vacuously true of zero responses, so
     the task RETURNS BEFORE AGGREGATING unless preparation left something to
     grade. `test_offline_auto_pipeline.py` holds that end.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from backend.models.tables import Question, QuestionResponse
from backend.routers import routingTasks

from .conftest import as_user

pytestmark = pytest.mark.asyncio


@pytest.fixture
def enqueued(monkeypatch):
    """Record what would have been handed to Celery, without a broker."""
    calls = []

    class _Recorder:
        @staticmethod
        def delay(exam_id, student_id):
            calls.append((exam_id, student_id))

    monkeypatch.setattr(routingTasks, "process_and_grade_exam", _Recorder)
    return calls


def _url(exam_id, student_id=None):
    base = f"/exam/{exam_id}/enqueue-processing"
    return base if student_id is None else f"{base}?student_id={student_id}"


# ---------------------------------------------------------------------------
# the manager trigger -- the point of the change
# ---------------------------------------------------------------------------

async def test_manager_can_start_grading_for_an_enrolled_student(client, world, enqueued):
    r = await client.post(
        _url(world["exam_a"].id, world["student_a"].id),
        headers=as_user(world["owner_prof"]),
    )
    assert r.status_code == 200, r.text
    assert enqueued == [(world["exam_a"].id, world["student_a"].id)], (
        "the NAMED student must reach the pipeline, not the caller"
    )
    assert r.json()["student_id"] == world["student_a"].id


async def test_manager_run_targets_the_named_student_not_themselves(client, world, enqueued):
    """The bug the old route had: a manager queued a run against their own id."""
    await client.post(
        _url(world["exam_a"].id, world["student_b"].id),
        headers=as_user(world["owner_prof"]),
    )
    assert enqueued == [(world["exam_a"].id, world["student_b"].id)]
    assert world["owner_prof"].id not in [s for _, s in enqueued]


async def test_a_ta_manager_may_also_start_grading(client, world, enqueued):
    r = await client.post(
        _url(world["exam_a"].id, world["student_a"].id), headers=as_user(world["ta"])
    )
    assert r.status_code == 200, r.text
    assert enqueued == [(world["exam_a"].id, world["student_a"].id)]


# ---------------------------------------------------------------------------
# authorization
# ---------------------------------------------------------------------------

async def test_anonymous_cannot_start_grading(client, world, enqueued):
    r = await client.post(_url(world["exam_a"].id, world["student_a"].id))
    assert r.status_code == 401
    assert enqueued == []


async def test_unrelated_professor_cannot_start_grading(client, world, enqueued):
    r = await client.post(
        _url(world["exam_a"].id, world["student_a"].id),
        headers=as_user(world["other_prof"]),
    )
    assert r.status_code == 403, "authority over one exam must not carry into another"
    assert enqueued == []


async def test_outsider_cannot_start_grading(client, world, enqueued):
    r = await client.post(
        _url(world["exam_a"].id, world["student_a"].id), headers=as_user(world["outsider"])
    )
    assert r.status_code == 403
    assert enqueued == []


async def test_student_cannot_start_grading_for_another_student(client, world, enqueued):
    """A student naming someone else must be refused, not silently redirected."""
    r = await client.post(
        _url(world["exam_a"].id, world["student_b"].id), headers=as_user(world["student_a"])
    )
    assert r.status_code == 403
    assert enqueued == []


async def test_a_student_cannot_start_their_own_run(client, world, enqueued):
    """The product decision, enforced at the boundary rather than by the UI.

    A student submits, waits and watches. Starting a run spends provider quota
    and rewrites marks, so it is the instructor's action. This path USED to
    return 200 -- the student page never offered it, but a hidden button is not
    an authorization boundary and a hand-written POST was accepted.
    """
    r = await client.post(_url(world["exam_a"].id), headers=as_user(world["student_a"]))
    assert r.status_code == 403
    assert enqueued == [], "a refused call must queue nothing"


async def test_a_student_naming_themselves_explicitly_is_still_refused(client, world, enqueued):
    r = await client.post(
        _url(world["exam_a"].id, world["student_a"].id), headers=as_user(world["student_a"])
    )
    assert r.status_code == 403
    assert enqueued == []


async def test_manager_cannot_target_a_user_who_is_not_a_student_here(client, world, enqueued):
    """A manager's own exam does not make every user id a valid target."""
    r = await client.post(
        _url(world["exam_a"].id, world["outsider"].id), headers=as_user(world["owner_prof"])
    )
    assert r.status_code == 403
    assert enqueued == []


async def test_manager_cannot_target_another_manager(client, world, enqueued):
    """A TA is a manager, not a student, and has no script to grade."""
    r = await client.post(
        _url(world["exam_a"].id, world["ta"].id), headers=as_user(world["owner_prof"])
    )
    assert r.status_code == 403
    assert enqueued == []


async def test_manager_without_a_named_student_is_refused(client, world, enqueued):
    """Falling back to the caller's own id is what made the old route wrong."""
    r = await client.post(_url(world["exam_a"].id), headers=as_user(world["owner_prof"]))
    assert r.status_code == 403
    assert enqueued == [], "a manager must never queue a run against themselves"


async def test_cross_exam_student_is_refused(client, world, enqueued):
    """student_a belongs to exam A's classroom, not exam B's."""
    r = await client.post(
        _url(world["exam_b"].id, world["student_a"].id), headers=as_user(world["other_prof"])
    )
    assert r.status_code == 403
    assert enqueued == []


async def test_unknown_exam_is_404(client, world, enqueued):
    r = await client.post(_url(999999, world["student_a"].id), headers=as_user(world["owner_prof"]))
    assert r.status_code == 404
    assert enqueued == []


# ---------------------------------------------------------------------------
# the readiness guard  (C6)
# ---------------------------------------------------------------------------

async def _clear_scripts(db, exam_id, student_id):
    """Remove the uploaded script too, so there is genuinely nothing to read."""
    from backend.models.files import AnswerScript

    found = await db.execute(
        select(AnswerScript).where(
            AnswerScript.exam_id == exam_id, AnswerScript.student_id == student_id
        )
    )
    for row in found.scalars().all():
        await db.delete(row)
    await db.commit()


async def _clear_responses(db, exam_id, student_id):
    found = await db.execute(
        select(QuestionResponse)
        .join(Question, Question.id == QuestionResponse.question_id)
        .where(Question.exam_id == exam_id, QuestionResponse.student_id == student_id)
    )
    for row in found.scalars().all():
        await db.delete(row)
    await db.commit()


async def test_nothing_to_read_at_all_is_refused(client, db, world, enqueued):
    """No responses AND no script: no automatic stage could produce anything."""
    await _clear_responses(db, world["exam_a"].id, world["student_a"].id)
    await _clear_scripts(db, world["exam_a"].id, world["student_a"].id)

    r = await client.post(
        _url(world["exam_a"].id, world["student_a"].id), headers=as_user(world["owner_prof"])
    )
    assert r.status_code == 409, "a student with no paper at all must be refused"
    assert enqueued == [], "and must not reach Celery at all"


async def test_an_uploaded_script_with_no_responses_is_accepted(client, db, world, enqueued):
    """The AI-first change: preparation happens IN the job, not before it.

    This was a 409, and that 409 is what forced a student through the crop
    editor before the AI was allowed to look at their paper.
    """
    await _clear_responses(db, world["exam_a"].id, world["student_a"].id)

    r = await client.post(
        _url(world["exam_a"].id, world["student_a"].id), headers=as_user(world["owner_prof"])
    )
    assert r.status_code == 200, r.text
    assert enqueued == [(world["exam_a"].id, world["student_a"].id)]


async def test_a_student_is_refused_before_readiness_is_even_considered(client, db, world, enqueued):
    """Authorization first: a student gets 403, never a 409 about their paper.

    The refusal must not depend on -- or reveal -- whether there is anything to
    grade, so emptying the student's rows changes nothing about the answer.
    """
    await _clear_responses(db, world["exam_a"].id, world["student_a"].id)
    await _clear_scripts(db, world["exam_a"].id, world["student_a"].id)

    r = await client.post(_url(world["exam_a"].id), headers=as_user(world["student_a"]))
    assert r.status_code == 403
    assert enqueued == []


async def test_readiness_is_judged_per_student(client, db, world, enqueued):
    """Emptying one student must not block the other's run."""
    await _clear_responses(db, world["exam_a"].id, world["student_a"].id)
    await _clear_scripts(db, world["exam_a"].id, world["student_a"].id)

    blocked = await client.post(
        _url(world["exam_a"].id, world["student_a"].id), headers=as_user(world["owner_prof"])
    )
    allowed = await client.post(
        _url(world["exam_a"].id, world["student_b"].id), headers=as_user(world["owner_prof"])
    )
    assert blocked.status_code == 409
    assert allowed.status_code == 200, allowed.text
    assert enqueued == [(world["exam_a"].id, world["student_b"].id)]


async def test_readiness_does_not_count_another_exams_responses(client, db, world, enqueued):
    """The count is scoped to this exam, so work elsewhere cannot unblock it."""
    await _clear_responses(db, world["exam_a"].id, world["student_a"].id)
    await _clear_scripts(db, world["exam_a"].id, world["student_a"].id)
    db.add(QuestionResponse(question_id=world["q_other"].id, student_id=world["student_a"].id))
    await db.commit()

    r = await client.post(
        _url(world["exam_a"].id, world["student_a"].id), headers=as_user(world["owner_prof"])
    )
    assert r.status_code == 409
    assert enqueued == []


async def test_a_script_for_another_exam_does_not_unblock_this_one(client, db, world, enqueued):
    """The script check is scoped to this exam and this student, like the rest."""
    from backend.models.files import AnswerScript

    await _clear_responses(db, world["exam_a"].id, world["student_a"].id)
    await _clear_scripts(db, world["exam_a"].id, world["student_a"].id)
    db.add(AnswerScript(title="elsewhere.pdf", file_path="/tmp/elsewhere.pdf",
                        exam_id=world["exam_b"].id, student_id=world["student_a"].id))
    await db.commit()

    r = await client.post(
        _url(world["exam_a"].id, world["student_a"].id), headers=as_user(world["owner_prof"])
    )
    assert r.status_code == 409
    assert enqueued == []


# ---------------------------------------------------------------------------
# the student-facing state signal
# ---------------------------------------------------------------------------

async def test_submission_status_reports_prepared(client, world):
    r = await client.get(
        f"/exams/{world['exam_a'].id}/submission_status", headers=as_user(world["student_a"])
    )
    assert r.status_code == 200
    body = r.json()
    assert body["prepared"] is True
    assert body["status"] == "pending"
    assert body["is_final"] is False


async def test_submission_status_reports_not_prepared(client, db, world):
    """`pending` alone cannot tell "not submitted" from "waiting to be graded"."""
    await _clear_responses(db, world["exam_a"].id, world["student_a"].id)

    r = await client.get(
        f"/exams/{world['exam_a'].id}/submission_status", headers=as_user(world["student_a"])
    )
    assert r.status_code == 200
    assert r.json()["prepared"] is False


async def test_submission_status_prepared_is_per_student(client, db, world):
    await _clear_responses(db, world["exam_a"].id, world["student_a"].id)

    a = await client.get(
        f"/exams/{world['exam_a'].id}/submission_status", headers=as_user(world["student_a"])
    )
    b = await client.get(
        f"/exams/{world['exam_a'].id}/submission_status", headers=as_user(world["student_b"])
    )
    assert a.json()["prepared"] is False
    assert b.json()["prepared"] is True


# ---------------------------------------------------------------------------
# a refused trigger changes nothing at all
# ---------------------------------------------------------------------------

async def _grading_fingerprint(db, exam_id, student_id):
    """Marks and failure codes for one student, in a stable order."""
    found = await db.execute(
        select(QuestionResponse)
        .join(Question, Question.id == QuestionResponse.question_id)
        .where(Question.exam_id == exam_id, QuestionResponse.student_id == student_id)
        .order_by(QuestionResponse.id)
    )
    return [
        (r.id, r.marks_obtained, r.grading_error_code)
        for r in found.scalars().all()
    ]


async def test_a_refused_student_call_leaves_grading_state_untouched(
    client, db, world, enqueued
):
    """403 means nothing happened: no job, no rows, no marks, no exam stage."""
    exam_id, student_id = world["exam_a"].id, world["student_a"].id
    before = await _grading_fingerprint(db, exam_id, student_id)
    stage_before = world["exam_a"].exam_stage

    r = await client.post(_url(exam_id), headers=as_user(world["student_a"]))

    assert r.status_code == 403
    assert enqueued == [], "no Celery task, so no provider call either"
    db.expire_all()
    assert await _grading_fingerprint(db, exam_id, student_id) == before
    await db.refresh(world["exam_a"])
    assert world["exam_a"].exam_stage == stage_before


async def test_a_refused_unrelated_professor_leaves_grading_state_untouched(
    client, db, world, enqueued
):
    exam_id, student_id = world["exam_a"].id, world["student_a"].id
    before = await _grading_fingerprint(db, exam_id, student_id)

    r = await client.post(_url(exam_id, student_id), headers=as_user(world["other_prof"]))

    assert r.status_code == 403
    assert enqueued == []
    db.expire_all()
    assert await _grading_fingerprint(db, exam_id, student_id) == before


async def test_a_refusal_says_nothing_about_the_other_student(client, world, enqueued):
    """The body must not reveal whether the named student or their paper exists."""
    real = await client.post(
        _url(world["exam_a"].id, world["student_b"].id), headers=as_user(world["student_a"])
    )
    invented = await client.post(
        _url(world["exam_a"].id, 999999), headers=as_user(world["student_a"])
    )

    assert real.status_code == invented.status_code == 403
    for body in (real.text, invented.text):
        for leak in ("script", "response", "marks", "graded", "b@x.test"):
            assert leak not in body.lower()
    assert enqueued == []


async def test_the_manager_path_still_works_after_the_tightening(client, world, enqueued):
    """The one caller that should work, still does."""
    r = await client.post(
        _url(world["exam_a"].id, world["student_a"].id), headers=as_user(world["owner_prof"])
    )
    assert r.status_code == 200, r.text
    assert enqueued == [(world["exam_a"].id, world["student_a"].id)]
