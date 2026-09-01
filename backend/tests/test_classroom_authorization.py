"""Regression tests for classroom-scoped authorization (Security Foundation v2).

v1 closed the exam/student/question/file surface. These tests cover the
classroom surface: classrooms, enrolments, people management and announcements,
plus cross-resource integrity between a classroom id and a child resource id.

The `world` fixture already provides two classrooms with distinct owners, an
accepted TA, two accepted students and an outsider, so cross-class cases are
expressible without new scaffolding.
"""

from __future__ import annotations

import pytest

from backend.tests.conftest import as_user

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# classroom read access
# ---------------------------------------------------------------------------

async def test_anonymous_cannot_view_classroom(client, world):
    r = await client.get(f"/classes/{world['class_a'].id}")
    assert r.status_code == 401


async def test_enrolled_student_can_view_own_classroom(client, world):
    r = await client.get(
        f"/classes/{world['class_a'].id}", headers=as_user(world["student_a"])
    )
    assert r.status_code != 403, "an accepted student must reach their own classroom"


async def test_outsider_student_cannot_view_classroom(client, world):
    r = await client.get(
        f"/classes/{world['class_a'].id}", headers=as_user(world["outsider"])
    )
    assert r.status_code == 403, "a non-member must not read classroom content"


async def test_unrelated_professor_cannot_view_classroom(client, world):
    r = await client.get(
        f"/classes/{world['class_a'].id}", headers=as_user(world["other_prof"])
    )
    assert r.status_code == 403, "is_professor must not grant cross-classroom access"


async def test_owning_professor_can_view_classroom(client, world):
    r = await client.get(
        f"/classes/{world['class_a'].id}", headers=as_user(world["owner_prof"])
    )
    assert r.status_code != 403


async def test_accepted_ta_can_view_classroom(client, world):
    r = await client.get(
        f"/classes/{world['class_a'].id}", headers=as_user(world["ta"])
    )
    assert r.status_code != 403, "an accepted TA is a classroom manager"


# ---------------------------------------------------------------------------
# roster enumeration  (previously completely unauthenticated)
# ---------------------------------------------------------------------------

async def test_anonymous_cannot_enumerate_people(client, world):
    r = await client.get(f"/classes/{world['class_a'].id}/people")
    assert r.status_code == 401


async def test_outsider_cannot_enumerate_people(client, world):
    """`/classes/{id}/people` previously had NO authorization whatsoever."""
    r = await client.get(
        f"/classes/{world['class_a'].id}/people", headers=as_user(world["outsider"])
    )
    assert r.status_code == 403, "roster must not leak to non-members"


async def test_unrelated_professor_cannot_enumerate_people(client, world):
    r = await client.get(
        f"/classes/{world['class_a'].id}/people", headers=as_user(world["other_prof"])
    )
    assert r.status_code == 403


async def test_member_can_enumerate_people(client, world):
    """A member must not be REFUSED.

    NOTE: this endpoint currently returns 500 for everyone because it reads a
    lazy relationship (`classroom.owner.full_name`) in async context. That is a
    pre-existing runtime bug, present on HEAD and out of scope for this security
    phase, so the assertion is deliberately about the authorization decision
    rather than a 200.
    """
    r = await client.get(
        f"/classes/{world['class_a'].id}/people", headers=as_user(world["student_a"])
    )
    assert r.status_code not in (401, 403), "a member must not be refused"


async def test_class_members_endpoint_denies_outsider(client, world):
    r = await client.get(
        f"/classes/{world['class_a'].id}/members", headers=as_user(world["outsider"])
    )
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# classroom mutation
# ---------------------------------------------------------------------------

async def test_student_cannot_create_assignment(client, world):
    r = await client.post(
        f"/classes/{world['class_a'].id}/assignments",
        headers=as_user(world["student_a"]),
        json={"title": "x", "description": "y"},
    )
    assert r.status_code == 403, "students must not create assignments"


async def test_unrelated_professor_cannot_create_assignment(client, world):
    """Previously `current_user.is_professor` alone was accepted here."""
    r = await client.post(
        f"/classes/{world['class_a'].id}/assignments",
        headers=as_user(world["other_prof"]),
        json={"title": "x", "description": "y"},
    )
    assert r.status_code == 403, "an unrelated professor must not create assignments"


async def test_student_cannot_create_announcement(client, world):
    """Previously any accepted member -- including a student -- could post."""
    r = await client.post(
        f"/classes/{world['class_a'].id}/announcements",
        headers=as_user(world["student_a"]),
        json={"title": "t", "content": "c"},
    )
    assert r.status_code == 403, "students must not create announcements"


async def test_unrelated_professor_cannot_create_announcement(client, world):
    r = await client.post(
        f"/classes/{world['class_a'].id}/announcements",
        headers=as_user(world["other_prof"]),
        json={"title": "t", "content": "c"},
    )
    assert r.status_code == 403


async def test_manager_can_create_announcement(client, world):
    r = await client.post(
        f"/classes/{world['class_a'].id}/announcements",
        headers=as_user(world["owner_prof"]),
        json={"title": "t", "content": "c"},
    )
    assert r.status_code != 403, "the owning professor must be able to post"


# ---------------------------------------------------------------------------
# announcements read
# ---------------------------------------------------------------------------

async def test_member_can_read_announcements(client, world):
    """A member must not be REFUSED.

    NOTE: this endpoint currently returns 500 for everyone because it calls the
    synchronous `db.query(...)` on an AsyncSession. Pre-existing bug, present on
    HEAD, out of scope here; the assertion targets the authorization decision.
    """
    r = await client.get(
        f"/classes/{world['class_a'].id}/announcements",
        headers=as_user(world["student_a"]),
    )
    assert r.status_code not in (401, 403), "a member must not be refused"


async def test_outsider_cannot_read_announcements(client, world):
    r = await client.get(
        f"/classes/{world['class_a'].id}/announcements",
        headers=as_user(world["outsider"]),
    )
    assert r.status_code == 403


async def test_anonymous_cannot_read_announcements(client, world):
    r = await client.get(f"/classes/{world['class_a'].id}/announcements")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# enrolment management
# ---------------------------------------------------------------------------

async def test_student_cannot_manage_enrollments(client, world):
    r = await client.get(
        f"/enrollments/manage/{world['class_a'].id}",
        headers=as_user(world["student_a"]),
    )
    assert r.status_code == 403


async def test_unrelated_professor_cannot_manage_enrollments(client, world):
    r = await client.get(
        f"/enrollments/manage/{world['class_a'].id}",
        headers=as_user(world["other_prof"]),
    )
    assert r.status_code == 403


async def test_owner_can_manage_enrollments(client, world):
    r = await client.get(
        f"/enrollments/manage/{world['class_a'].id}",
        headers=as_user(world["owner_prof"]),
    )
    assert r.status_code != 403


async def test_student_cannot_accept_own_enrollment(client, world, enrollment_ids):
    """A pending student must not be able to admit themselves."""
    r = await client.post(
        f"/enrollments/{enrollment_ids['pending_outsider']}/accept",
        headers=as_user(world["outsider"]),
    )
    assert r.status_code == 403, "a student must not accept their own enrolment"


async def test_student_cannot_remove_another_member(client, world, enrollment_ids):
    r = await client.post(
        f"/enrollments/{enrollment_ids['student_b']}/remove",
        headers=as_user(world["student_a"]),
    )
    assert r.status_code == 403, "students must not remove other members"


async def test_unrelated_professor_cannot_remove_member(client, world, enrollment_ids):
    r = await client.post(
        f"/enrollments/{enrollment_ids['student_b']}/remove",
        headers=as_user(world["other_prof"]),
    )
    assert r.status_code == 403, "an unrelated professor must not remove members"


# ---------------------------------------------------------------------------
# privilege escalation via role changes
# ---------------------------------------------------------------------------

async def test_student_cannot_promote_self_to_ta(client, world, enrollment_ids):
    """TA is a MANAGER role, so self-promotion would be privilege escalation."""
    r = await client.post(
        f"/enrollments/{enrollment_ids['student_a']}/make-ta",
        headers=as_user(world["student_a"]),
    )
    assert r.status_code == 403, "a student must not promote themselves to TA"


async def test_unrelated_professor_cannot_promote_to_ta(client, world, enrollment_ids):
    """Previously the global is_professor flag alone was sufficient here."""
    r = await client.post(
        f"/enrollments/{enrollment_ids['student_a']}/make-ta",
        headers=as_user(world["other_prof"]),
    )
    assert r.status_code == 403, "an unrelated professor must not grant TA rights"


async def test_ta_cannot_promote_another_student_to_ta(client, world, enrollment_ids):
    """Promotion is owner-only: a manager must not mint further managers."""
    r = await client.post(
        f"/enrollments/{enrollment_ids['student_a']}/make-ta",
        headers=as_user(world["ta"]),
    )
    assert r.status_code == 403, "a TA must not promote others to TA"


async def test_owner_can_promote_to_ta(client, world, enrollment_ids):
    r = await client.post(
        f"/enrollments/{enrollment_ids['student_a']}/make-ta",
        headers=as_user(world["owner_prof"]),
    )
    assert r.status_code != 403, "the classroom owner may promote a student to TA"


async def test_unrelated_professor_cannot_demote_ta(client, world, enrollment_ids):
    r = await client.post(
        f"/enrollments/{enrollment_ids['ta']}/make-student",
        headers=as_user(world["other_prof"]),
    )
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# cross-resource / cross-classroom integrity
# ---------------------------------------------------------------------------

async def test_owner_of_a_cannot_manage_enrollment_of_b(client, world, enrollment_ids):
    """class A's owner must not touch an enrolment that lives in class B."""
    r = await client.post(
        f"/enrollments/{enrollment_ids['in_class_b']}/remove",
        headers=as_user(world["owner_prof"]),
    )
    assert r.status_code == 403, "enrolment belongs to another classroom"


async def test_owner_of_a_cannot_promote_in_b(client, world, enrollment_ids):
    r = await client.post(
        f"/enrollments/{enrollment_ids['in_class_b']}/make-ta",
        headers=as_user(world["owner_prof"]),
    )
    assert r.status_code == 403


async def test_announcement_of_other_class_not_reachable(client, world, announcement_ids):
    """class_id = A with an announcement_id belonging to B must not resolve."""
    r = await client.put(
        f"/classes/{world['class_a'].id}/announcements/{announcement_ids['in_class_b']}",
        headers=as_user(world["owner_prof"]),
        json={"title": "hijack", "content": "hijack"},
    )
    assert r.status_code == 404, (
        "an announcement from another classroom must not be editable through "
        "this classroom's route"
    )


async def test_manage_enrollments_of_other_class_denied(client, world):
    r = await client.get(
        f"/enrollments/manage/{world['class_b'].id}",
        headers=as_user(world["owner_prof"]),
    )
    assert r.status_code == 403, "owner of A must not manage enrolments of B"


async def test_people_of_other_class_denied(client, world):
    r = await client.get(
        f"/classes/{world['class_b'].id}/people",
        headers=as_user(world["student_a"]),
    )
    assert r.status_code == 403


async def test_missing_classroom_is_404(client, world):
    r = await client.get("/classes/999999", headers=as_user(world["owner_prof"]))
    assert r.status_code == 404
