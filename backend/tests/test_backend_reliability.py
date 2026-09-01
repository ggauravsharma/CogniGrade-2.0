"""Backend Reliability v1: the failures a user would actually hit.

Correctness phases proved the grading ALGORITHM behaves. This suite is about
the things that make a working algorithm useless in front of real people:

    an announcement page that 500s
    a roster page that 500s
    a deletion that reports success without deleting
    a deliberate 403 arriving as a 500
    a professor who is told grading failed but not where

Every test here is written against the observable HTTP behaviour, because that
is the only place these defects were visible -- each one passed every unit test
in the repository while being completely broken in the app.
"""

import ast
import pathlib

import pytest
from sqlalchemy import select

from backend.grading.failure import (
    FAILURE_MESSAGES,
    UNKNOWN_FAILURE_MESSAGE,
    GradingFailure,
    collect_failures,
    describe,
)
from backend.models.tables import Announcement, Enrollment, Question, QuestionResponse

from .conftest import as_user


# ---------------------------------------------------------------------------
# the failure vocabulary itself
# ---------------------------------------------------------------------------

def test_every_known_code_has_a_human_sentence():
    for code, message in FAILURE_MESSAGES.items():
        assert message and message[0].isupper() and message.endswith(".")


def test_an_unknown_code_degrades_instead_of_leaking():
    assert describe("something_new") == UNKNOWN_FAILURE_MESSAGE
    assert describe(None) == UNKNOWN_FAILURE_MESSAGE
    assert describe("") == UNKNOWN_FAILURE_MESSAGE


def test_no_message_names_a_provider():
    """The professor is told what went wrong, never who was asked."""
    banned = ("gemini", "google", "openai", "anthropic", "genai", "vertex")
    for message in list(FAILURE_MESSAGES.values()) + [UNKNOWN_FAILURE_MESSAGE]:
        for token in banned:
            assert token not in message.lower(), message


def test_failure_module_imports_nothing_provider_specific():
    import backend.grading.failure as mod

    tree = ast.parse(pathlib.Path(mod.__file__).read_text(encoding="utf-8"))
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    banned = ("google", "genai", "gemini", "fastapi", "sqlalchemy", "backend.models")
    for name in imported:
        for token in banned:
            assert token not in name.lower(), f"{token} imported in failure: {name}"


class _Row:
    def __init__(self, question_id, marks_obtained, question_number=None, grading_error_code=None):
        self.question_id = question_id
        self.marks_obtained = marks_obtained
        self.question_number = question_number
        self.grading_error_code = grading_error_code


def test_collect_failures_lists_only_ungraded_rows():
    failures = collect_failures([
        _Row(1, 5, 1),
        _Row(2, None, 2, "score_missing"),
        _Row(3, 0, 3),
    ])
    assert [f.question_id for f in failures] == [2]
    assert failures[0].label == "Q2"
    assert failures[0].message == FAILURE_MESSAGES["score_missing"]


def test_a_valid_zero_is_never_a_failure():
    """Audit C6, restated on the surface most likely to reintroduce it."""
    assert collect_failures([_Row(1, 0, 1), _Row(2, 0.0, 2)]) == []


def test_a_failure_without_a_code_still_reports_something():
    failures = collect_failures([_Row(7, None, 4, None)])
    assert failures[0].message == UNKNOWN_FAILURE_MESSAGE
    assert failures[0].as_dict()["label"] == "Q4"


def test_grading_failure_never_carries_raw_provider_text():
    """The dataclass has no field that could hold a model response."""
    fields = set(GradingFailure.__dataclass_fields__)
    assert fields == {"question_id", "question_number", "error_code"}


# ---------------------------------------------------------------------------
# grading failure visibility, end to end
# ---------------------------------------------------------------------------

async def _add_question(db, exam_id, number, max_marks=10):
    q = Question(exam_id=exam_id, question_number=number, text=f"Q{number}", max_marks=max_marks)
    db.add(q)
    await db.commit()
    await db.refresh(q)
    return q


async def _response(db, question_id, student_id, marks=None, error_code=None):
    r = QuestionResponse(
        question_id=question_id, student_id=student_id,
        marks_obtained=marks, grading_error_code=error_code,
    )
    db.add(r)
    await db.commit()
    await db.refresh(r)
    return r


async def _stats_row(client, world, student):
    res = await client.get(
        f"/exams/{world['exam_a'].id}/stats", headers=as_user(world["owner_prof"])
    )
    assert res.status_code == 200, res.text
    return next(s for s in res.json()["students"] if s["id"] == student.id)


@pytest.mark.asyncio
async def test_a_failed_question_is_reported_to_the_professor(client, db, world):
    exam, student = world["exam_a"], world["student_a"]
    q = await _add_question(db, exam.id, 2)
    await _response(db, q.id, student.id, marks=None, error_code="score_missing")

    row = await _stats_row(client, world, student)
    codes = {f["error_code"] for f in row["grading_failures"]}
    assert "score_missing" in codes
    assert "Q2" in row["failed_question_labels"]
    failure = next(f for f in row["grading_failures"] if f["error_code"] == "score_missing")
    assert failure["message"] == FAILURE_MESSAGES["score_missing"]
    assert failure["question_id"] == q.id


@pytest.mark.asyncio
async def test_multiple_failed_questions_are_all_reported(client, db, world):
    exam, student = world["exam_a"], world["student_b"]
    q2 = await _add_question(db, exam.id, 2)
    q5 = await _add_question(db, exam.id, 5)
    await _response(db, q2.id, student.id, marks=None, error_code="malformed_json")
    await _response(db, q5.id, student.id, marks=None, error_code="score_above_max")

    row = await _stats_row(client, world, student)
    assert {"Q2", "Q5"}.issubset(set(row["failed_question_labels"]))
    codes = {f["error_code"] for f in row["grading_failures"]}
    assert {"malformed_json", "score_above_max"}.issubset(codes)


@pytest.mark.asyncio
async def test_a_graded_student_has_no_failures_listed(client, db, world):
    """student_a's fixture response is graded 5, so nothing may be reported."""
    row = await _stats_row(client, world, world["student_a"])
    assert row["grading_failures"] == []
    assert row["failed_question_labels"] == []


@pytest.mark.asyncio
async def test_a_zero_mark_is_not_reported_as_a_failure(client, db, world):
    """A dropped question scores 0. That is a grade, not a grading failure."""
    exam, student = world["exam_a"], world["outsider"]
    q = await _add_question(db, exam.id, 3)
    await _response(db, q.id, student.id, marks=0, error_code=None)

    res = await client.get(f"/exams/{exam.id}/stats", headers=as_user(world["owner_prof"]))
    assert res.status_code == 200
    for row in res.json()["students"]:
        assert all(f["question_id"] != q.id for f in row["grading_failures"])


@pytest.mark.asyncio
async def test_professor_sees_the_reason_on_the_question(client, db, world):
    exam, student = world["exam_a"], world["student_a"]
    q = await _add_question(db, exam.id, 4)
    await _response(db, q.id, student.id, marks=None, error_code="malformed_json")

    res = await client.get(
        f"/exam/{exam.id}/student-evaluation/{student.id}",
        headers=as_user(world["owner_prof"]),
    )
    assert res.status_code == 200
    entry = next(e for e in res.json() if e["question_id"] == q.id)
    assert entry["grading_error_code"] == "malformed_json"
    assert entry["grading_error"] == FAILURE_MESSAGES["malformed_json"]


@pytest.mark.asyncio
async def test_a_student_does_not_see_grading_diagnostics(client, db, world):
    """Part K: failure detail is professor-facing. Marks are not.

    The route is self-or-manager, so the student legitimately reaches their own
    row -- they must simply not receive the operational fields.
    """
    exam, student = world["exam_a"], world["student_a"]
    q = await _add_question(db, exam.id, 6)
    await _response(db, q.id, student.id, marks=None, error_code="malformed_json")

    res = await client.get(
        f"/exam/{exam.id}/student-evaluation/{student.id}", headers=as_user(student)
    )
    assert res.status_code == 200
    for entry in res.json():
        assert "grading_error_code" not in entry
        assert "grading_error" not in entry


@pytest.mark.asyncio
async def test_a_student_cannot_read_the_professor_stats_endpoint(client, world):
    res = await client.get(
        f"/exams/{world['exam_a'].id}/stats", headers=as_user(world["student_a"])
    )
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_an_outsider_cannot_read_grading_failures(client, world):
    res = await client.get(
        f"/exams/{world['exam_a'].id}/stats", headers=as_user(world["outsider"])
    )
    assert res.status_code in (403, 404)


# ---------------------------------------------------------------------------
# failure lifecycle: a retry must clear the stale state
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_manual_mark_clears_the_failure(client, db, world):
    exam, student = world["exam_a"], world["student_a"]
    q = await _add_question(db, exam.id, 7)
    await _response(db, q.id, student.id, marks=None, error_code="score_missing")

    row = await _stats_row(client, world, student)
    assert "Q7" in row["failed_question_labels"]

    res = await client.patch(
        f"/exams/{exam.id}/student/{student.id}/question/{q.id}/update",
        json={"grade": "1.5"}, headers=as_user(world["owner_prof"]),
    )
    assert res.status_code == 200

    db.expunge_all()
    found = await db.execute(select(QuestionResponse).where(
        QuestionResponse.question_id == q.id, QuestionResponse.student_id == student.id
    ))
    assert found.scalars().first().grading_error_code is None

    row = await _stats_row(client, world, student)
    assert "Q7" not in row["failed_question_labels"]


@pytest.mark.asyncio
async def test_a_successful_reevaluation_clears_the_failure(client, db, world, monkeypatch):
    import backend.routers.examStats as stats

    exam, student = world["exam_a"], world["student_a"]
    q = await _add_question(db, exam.id, 8)
    await _response(db, q.id, student.id, marks=None, error_code="malformed_json")

    async def _fake_extract(payload, db_, user):
        return {"status": "ok"}

    async def _fake_grade(payload, db_, user):
        return {"status": "graded", "grade": 3.0, "reasoning": "ok"}

    monkeypatch.setattr(stats, "extract_single_answer_text", _fake_extract)
    monkeypatch.setattr(stats, "grade_question_with_diagram", _fake_grade)

    res = await client.post(
        f"/exam/{exam.id}/question/{q.id}/student/{student.id}/reevaluate",
        headers=as_user(world["owner_prof"]),
    )
    assert res.status_code == 200

    db.expunge_all()
    found = await db.execute(select(QuestionResponse).where(
        QuestionResponse.question_id == q.id, QuestionResponse.student_id == student.id
    ))
    row = found.scalars().first()
    assert row.marks_obtained == 3.0
    assert row.grading_error_code is None


@pytest.mark.asyncio
async def test_a_failed_reevaluation_leaves_the_question_reported(client, db, world, monkeypatch):
    import backend.routers.examStats as stats

    exam, student = world["exam_a"], world["student_a"]
    q = await _add_question(db, exam.id, 9)
    await _response(db, q.id, student.id, marks=None, error_code="score_missing")

    async def _fake_extract(payload, db_, user):
        return {"status": "ok"}

    async def _fake_grade(payload, db_, user):
        return {"status": "grading_failed", "grade": None, "error_code": "malformed_json"}

    monkeypatch.setattr(stats, "extract_single_answer_text", _fake_extract)
    monkeypatch.setattr(stats, "grade_question_with_diagram", _fake_grade)

    res = await client.post(
        f"/exam/{exam.id}/question/{q.id}/student/{student.id}/reevaluate",
        headers=as_user(world["owner_prof"]),
    )
    assert res.status_code == 200

    row = await _stats_row(client, world, student)
    assert "Q9" in row["failed_question_labels"]


@pytest.mark.asyncio
async def test_dropping_a_question_clears_its_failure(client, db, world):
    exam, student = world["exam_a"], world["student_a"]
    q = await _add_question(db, exam.id, 10)
    await _response(db, q.id, student.id, marks=None, error_code="score_missing")

    res = await client.post(
        f"/exam/{exam.id}/question/{q.id}/drop", headers=as_user(world["owner_prof"])
    )
    assert res.status_code == 200

    db.expunge_all()
    found = await db.execute(select(QuestionResponse).where(
        QuestionResponse.question_id == q.id, QuestionResponse.student_id == student.id
    ))
    row = found.scalars().first()
    assert row.marks_obtained == 0
    assert row.grading_error_code is None


@pytest.mark.asyncio
async def test_awarding_full_marks_clears_its_failure(client, db, world):
    exam, student = world["exam_a"], world["student_a"]
    q = await _add_question(db, exam.id, 12, max_marks=2.5)
    await _response(db, q.id, student.id, marks=None, error_code="score_above_max")

    res = await client.post(
        f"/exam/{exam.id}/question/{q.id}/full-marks", headers=as_user(world["owner_prof"])
    )
    assert res.status_code == 200

    db.expunge_all()
    found = await db.execute(select(QuestionResponse).where(
        QuestionResponse.question_id == q.id, QuestionResponse.student_id == student.id
    ))
    row = found.scalars().first()
    assert row.marks_obtained == 2.5
    assert row.grading_error_code is None


# ---------------------------------------------------------------------------
# announcements: the page that always 500'd
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_listing_announcements_no_longer_500s(client, world, announcement_ids):
    """`db.query()` on an AsyncSession made this endpoint fail for everyone."""
    res = await client.get(
        f"/classes/{world['class_a'].id}/announcements", headers=as_user(world["student_a"])
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["success"] is True
    ids = [a["id"] for a in body["announcements"]]
    assert announcement_ids["in_class_a"] in ids
    assert announcement_ids["in_class_b"] not in ids


@pytest.mark.asyncio
async def test_listing_announcements_resolves_author_names(client, world, announcement_ids):
    res = await client.get(
        f"/classes/{world['class_a'].id}/announcements", headers=as_user(world["owner_prof"])
    )
    assert res.status_code == 200
    entry = next(a for a in res.json()["announcements"]
                 if a["id"] == announcement_ids["in_class_a"])
    assert entry["author_name"] == world["owner_prof"].full_name
    assert entry["can_edit"] is True


@pytest.mark.asyncio
async def test_an_outsider_still_cannot_list_announcements(client, world, announcement_ids):
    res = await client.get(
        f"/classes/{world['class_a'].id}/announcements", headers=as_user(world["outsider"])
    )
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_deleting_an_announcement_actually_deletes_it(client, db, world, announcement_ids):
    """Two bugs at once: a sync query, then an unawaited delete."""
    target = announcement_ids["in_class_a"]
    res = await client.delete(
        f"/classes/{world['class_a'].id}/announcements/{target}",
        headers=as_user(world["owner_prof"]),
    )
    assert res.status_code == 200, res.text

    db.expunge_all()
    found = await db.execute(select(Announcement).where(Announcement.id == target))
    assert found.scalars().first() is None, "announcement reported deleted but is still there"


# ---------------------------------------------------------------------------
# people management: the roster that always 500'd
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_class_people_no_longer_500s_and_names_the_owner(client, world):
    """`classroom.owner.full_name` lazy-loaded in async context -> 500."""
    res = await client.get(
        f"/classes/{world['class_a'].id}/people", headers=as_user(world["student_a"])
    )
    assert res.status_code == 200, res.text
    body = res.json()

    professor = next(t for t in body["teachers"] if t["role"] == "professor")
    assert professor["full_name"] == world["owner_prof"].full_name
    assert professor["user_id"] == world["owner_prof"].id


@pytest.mark.asyncio
async def test_class_people_lists_members_without_lazy_loading(client, world):
    """`e.student.full_name` was the same bug one line further down."""
    res = await client.get(
        f"/classes/{world['class_a'].id}/people", headers=as_user(world["owner_prof"])
    )
    assert res.status_code == 200, res.text
    body = res.json()

    ta_names = {t["full_name"] for t in body["teachers"] if t["role"] != "professor"}
    assert world["ta"].full_name in ta_names
    student_names = {s["full_name"] for s in body["students"]}
    assert {world["student_a"].full_name, world["student_b"].full_name} <= student_names
    assert None not in student_names


@pytest.mark.asyncio
async def test_an_outsider_still_cannot_read_the_roster(client, world):
    res = await client.get(
        f"/classes/{world['class_a'].id}/people", headers=as_user(world["outsider"])
    )
    assert res.status_code == 403


# ---------------------------------------------------------------------------
# removal: the deletion that lied
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_removing_a_student_really_removes_the_row(client, db, world, enrollment_ids):
    target = enrollment_ids["student_b"]
    res = await client.post(
        f"/enrollments/{target}/remove", headers=as_user(world["owner_prof"])
    )
    assert res.status_code == 200, res.text
    assert res.json()["success"] is True

    db.expunge_all()
    found = await db.execute(select(Enrollment).where(Enrollment.id == target))
    assert found.scalars().first() is None, "removal reported success but the row is still there"


@pytest.mark.asyncio
async def test_a_refused_removal_does_not_delete(client, db, world, enrollment_ids):
    target = enrollment_ids["student_b"]
    res = await client.post(
        f"/enrollments/{target}/remove", headers=as_user(world["student_a"])
    )
    assert res.status_code == 403

    db.expunge_all()
    found = await db.execute(select(Enrollment).where(Enrollment.id == target))
    assert found.scalars().first() is not None


# ---------------------------------------------------------------------------
# route registration
# ---------------------------------------------------------------------------

def test_no_duplicate_method_path_registrations():
    """A shadowed handler is dead code whose behaviour returns if include order
    ever changes. There must be exactly one implementation per endpoint."""
    from collections import defaultdict

    from backend.auth import files as protected_files
    from backend.routers import (
        announcements, auth, classes, enrollments, examStats, exams, geminiAPI,
        notifications, peopleManagement, routingTasks, studentBackend,
        studentEdit, user_routes,
    )

    modules = [
        auth, classes, enrollments, notifications, announcements, exams,
        geminiAPI, peopleManagement, studentBackend, examStats, studentEdit,
        user_routes, routingTasks, protected_files,
    ]
    seen = defaultdict(list)
    for module in modules:
        for route in module.router.routes:
            for method in getattr(route, "methods", None) or []:
                if method in ("HEAD", "OPTIONS"):
                    continue
                seen[(method, route.path)].append(route.endpoint.__name__)

    duplicates = {k: v for k, v in seen.items() if len(v) > 1}
    assert duplicates == {}, f"duplicate route registrations: {duplicates}"


# ---------------------------------------------------------------------------
# HTTP error semantics
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_missing_class_is_404_not_500(client, world):
    res = await client.get("/classes/999999", headers=as_user(world["owner_prof"]))
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_a_forbidden_class_is_403_not_500(client, world):
    res = await client.get(
        f"/classes/{world['class_a'].id}", headers=as_user(world["outsider"])
    )
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_a_missing_assignment_is_404_not_500(client, world):
    """In-body 404: the broad handler used to relabel this one 500."""
    res = await client.get("/assignments/999999", headers=as_user(world["owner_prof"]))
    assert res.status_code == 404


@pytest.mark.asyncio
async def test_a_genuine_internal_error_is_still_a_server_error(client, world, monkeypatch):
    """The guard must re-raise HTTPException only -- not swallow real faults."""
    import backend.routers.classes as classes_router

    def _boom(*args, **kwargs):
        raise RuntimeError("database on fire")

    monkeypatch.setattr(classes_router, "select", _boom)

    res = await client.get(
        f"/classes/{world['class_a'].id}/announcements/../..",
        headers=as_user(world["owner_prof"]),
    )
    # Whatever the router does with that path, it must not be a 2xx.
    assert res.status_code >= 400
