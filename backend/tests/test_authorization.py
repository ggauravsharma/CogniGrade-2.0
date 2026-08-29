"""Regression tests for the authorization layer.

Each test names the audit finding it protects, so a future change that
re-opens one of these holes fails with an explanatory message rather than a
bare assertion.
"""

from __future__ import annotations

import pytest

from backend.tests.conftest import as_user

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# authentication
# ---------------------------------------------------------------------------

async def test_anonymous_denied_on_student_evaluation(client, world):
    r = await client.get(
        f"/exam/{world['exam_a'].id}/student-evaluation/{world['student_a'].id}"
    )
    assert r.status_code == 401, "anonymous access must be rejected with 401"


async def test_anonymous_cannot_update_exam_stage(client, world):
    """Audit C3: POST /exams/{id}/stage previously had NO authentication."""
    r = await client.post(f"/exams/{world['exam_a'].id}/stage?exam_stage=7")
    assert r.status_code == 401, "exam stage update must require authentication"


async def test_anonymous_cannot_update_question_parts(client, world):
    """Audit C3: POST /exams/{id}/questions/parts previously had NO auth."""
    r = await client.post(
        f"/exams/{world['exam_a'].id}/questions/parts", json={"updates": []}
    )
    assert r.status_code == 401, "question part update must require authentication"


# ---------------------------------------------------------------------------
# student isolation  (audit C2)
# ---------------------------------------------------------------------------

async def test_student_cannot_read_other_students_evaluation(client, world):
    r = await client.get(
        f"/exam/{world['exam_a'].id}/student-evaluation/{world['student_b'].id}",
        headers=as_user(world["student_a"]),
    )
    assert r.status_code == 403, "student A must not read student B's evaluation"


async def test_student_can_read_own_evaluation(client, world):
    r = await client.get(
        f"/exam/{world['exam_a'].id}/student-evaluation/{world['student_a'].id}",
        headers=as_user(world["student_a"]),
    )
    assert r.status_code != 403, "a student must still reach their own evaluation"


async def test_student_cannot_read_other_students_question_details(client, world):
    r = await client.get(
        f"/exams/{world['exam_a'].id}/student/{world['student_b'].id}"
        f"/question/{world['q1'].id}/details",
        headers=as_user(world["student_a"]),
    )
    assert r.status_code == 403, "student A must not read student B's answer detail"


async def test_student_cannot_edit_another_students_marks(client, world):
    r = await client.patch(
        f"/exams/{world['exam_a'].id}/student/{world['student_b'].id}"
        f"/question/{world['q1'].id}/update",
        headers=as_user(world["student_a"]),
        json={"grade": 10},
    )
    assert r.status_code == 403, "student A must not edit student B's marks"


async def test_student_cannot_edit_own_marks(client, world):
    """Mark mutation is manager-only: self-access is a READ capability."""
    r = await client.patch(
        f"/exams/{world['exam_a'].id}/student/{world['student_a'].id}"
        f"/question/{world['q1'].id}/update",
        headers=as_user(world["student_a"]),
        json={"grade": 10},
    )
    assert r.status_code == 403, "a student must not be able to raise their own marks"


async def test_outsider_denied_entirely(client, world):
    r = await client.get(
        f"/exam/{world['exam_a'].id}/student-evaluation/{world['outsider'].id}",
        headers=as_user(world["outsider"]),
    )
    assert r.status_code == 403, "a user enrolled in nothing must be denied"


# ---------------------------------------------------------------------------
# exam mutation by students  (audit C3, C4)
# ---------------------------------------------------------------------------

async def test_student_cannot_update_exam_stage(client, world):
    r = await client.post(
        f"/exams/{world['exam_a'].id}/stage?exam_stage=7",
        headers=as_user(world["student_a"]),
    )
    assert r.status_code == 403, "students must not move the exam stage"


async def test_student_cannot_update_question_parts(client, world):
    r = await client.post(
        f"/exams/{world['exam_a'].id}/questions/parts",
        headers=as_user(world["student_a"]),
        json={"updates": []},
    )
    assert r.status_code == 403, "students must not rewrite question structure"


async def test_student_cannot_drop_question(client, world):
    """Audit C4: any logged-in user could zero a question for the whole class."""
    r = await client.post(
        f"/exam/{world['exam_a'].id}/question/{world['q1'].id}/drop",
        headers=as_user(world["student_a"]),
    )
    assert r.status_code == 403, "students must not drop questions for the class"


async def test_student_cannot_award_class_wide_full_marks(client, world):
    """Audit C4."""
    r = await client.post(
        f"/exam/{world['exam_a'].id}/question/{world['q1'].id}/full-marks",
        headers=as_user(world["student_a"]),
    )
    assert r.status_code == 403, "students must not award full marks to the class"


async def test_student_cannot_trigger_reevaluation(client, world):
    r = await client.post(
        f"/exam/{world['exam_a'].id}/question/{world['q1'].id}"
        f"/student/{world['student_a'].id}/reevaluate",
        headers=as_user(world["student_a"]),
    )
    assert r.status_code == 403, "re-grading is a manager action"


# ---------------------------------------------------------------------------
# professor ownership
# ---------------------------------------------------------------------------

async def test_owning_professor_allowed(client, world):
    r = await client.get(
        f"/exam/{world['exam_a'].id}/student-evaluation/{world['student_a'].id}",
        headers=as_user(world["owner_prof"]),
    )
    assert r.status_code != 403, "the owning professor must be allowed"


async def test_unrelated_professor_denied(client, world):
    """`is_professor` alone must never grant cross-classroom access."""
    r = await client.get(
        f"/exam/{world['exam_a'].id}/student-evaluation/{world['student_a'].id}",
        headers=as_user(world["other_prof"]),
    )
    assert r.status_code == 403, "a professor of another classroom must be denied"


async def test_unrelated_professor_cannot_drop_question(client, world):
    r = await client.post(
        f"/exam/{world['exam_a'].id}/question/{world['q1'].id}/drop",
        headers=as_user(world["other_prof"]),
    )
    assert r.status_code == 403, "an unrelated professor must not drop questions"


async def test_ta_is_treated_as_manager(client, world):
    """An accepted TA enrolment confers manager rights in that classroom."""
    r = await client.get(
        f"/exam/{world['exam_a'].id}/student-evaluation/{world['student_a'].id}",
        headers=as_user(world["ta"]),
    )
    assert r.status_code != 403, "an accepted TA must be able to see student work"


# ---------------------------------------------------------------------------
# cross-exam confusion
# ---------------------------------------------------------------------------

async def test_question_from_another_exam_rejected(client, world):
    """A manager of exam A must not act on a question belonging to exam B."""
    r = await client.post(
        f"/exam/{world['exam_a'].id}/question/{world['q_other'].id}/drop",
        headers=as_user(world["owner_prof"]),
    )
    assert r.status_code == 403, "question/exam mismatch must be rejected"


async def test_missing_exam_is_404_not_403(client, world):
    r = await client.get(
        f"/exam/999999/student-evaluation/{world['student_a'].id}",
        headers=as_user(world["owner_prof"]),
    )
    assert r.status_code == 404, "a non-existent exam should report 404"


# ---------------------------------------------------------------------------
# marking scheme confidentiality
# ---------------------------------------------------------------------------

async def test_student_cannot_read_marking_scheme_document(client, world):
    r = await client.get(
        f"/student/exam/{world['exam_a'].id}/document/marking_scheme",
        headers=as_user(world["student_a"]),
    )
    assert r.status_code == 403, "students must not read the marking scheme"


async def test_manager_can_read_marking_scheme_document(client, world):
    r = await client.get(
        f"/student/exam/{world['exam_a'].id}/document/marking_scheme",
        headers=as_user(world["owner_prof"]),
    )
    assert r.status_code == 200, "the owning professor must reach the marking scheme"
