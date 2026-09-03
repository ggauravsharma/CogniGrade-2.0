"""Deleting a course removes the course, and nothing that outlives it.

Two claims are worth more than the rest, and both are tested against a world
that contains a SECOND classroom sharing its users with the first:

  * a course's data reaches ten tables and the disk, and one call removes all of
    it -- the cascade is the database's, verified here rather than assumed from
    the ORM's `cascade=` strings;
  * a user is never course-owned. A professor who owned the deleted course and a
    student who was enrolled in it both survive, along with everything they have
    in the classroom that was not deleted.

Files are written into a temporary root, never into `./uploads`, and never from
a real student upload.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import func, select

from backend.classrooms.deletion import collect_owned_file_paths, delete_classroom
from backend.models.files import AnswerScript, Material, FileTypeEnum
from backend.models.tables import (
    Announcement,
    Assignment,
    Classroom,
    DocumentRegion,
    Enrollment,
    EnrollmentStatus,
    Exam,
    ExamResult,
    Question,
    QuestionResponse,
    Role,
    Submission,
)
from backend.models.users import User
from backend.storage.paths import delete_files_within_root, resolve_within_root

from .conftest import as_user


async def _count(db, model, **filters):
    stmt = select(func.count()).select_from(model)
    for column, value in filters.items():
        stmt = stmt.where(getattr(model, column) == value)
    return (await db.execute(stmt)).scalar_one()


async def _exists(db, model, ident):
    return (await db.execute(select(model).where(model.id == ident))).scalars().first() is not None


# ---------------------------------------------------------------------------
# a two-course world that SHARES its people
# ---------------------------------------------------------------------------

@pytest.fixture
def course_files(tmp_path):
    """A temporary upload root holding one file per course."""
    root = tmp_path / "uploads"
    (root / "a").mkdir(parents=True)
    (root / "b").mkdir(parents=True)
    files = {
        "a_material": root / "a" / "qp.pdf",
        "a_script": root / "a" / "script.pdf",
        "a_crop": root / "a" / "crop.png",
        "a_ms_image": root / "a" / "ms.png",
        "a_submission": root / "a" / "hw.pdf",
        "b_material": root / "b" / "other_qp.pdf",
        "outside": tmp_path / "not_in_the_root.pdf",
    }
    for path in files.values():
        path.write_bytes(b"x")
    return {"root": root, **{k: v for k, v in files.items()}}


@pytest.fixture
async def two_courses(db, course_files):
    """Classroom A (to be deleted) and Classroom B, sharing owner and student."""
    prof = User(email="p@x.test", hashed_password="x", full_name="Prof", is_professor=True)
    other_prof = User(email="o@x.test", hashed_password="x", full_name="Other", is_professor=True)
    student = User(email="s@x.test", hashed_password="x", full_name="Student", is_professor=False)
    db.add_all([prof, other_prof, student])
    await db.commit()
    for u in (prof, other_prof, student):
        await db.refresh(u)

    a = Classroom(name="Course A", subject="CS", owner_id=prof.id, class_code="DELAAA")
    b = Classroom(name="Course B", subject="CS", owner_id=prof.id, class_code="KEEPBB")
    db.add_all([a, b])
    await db.commit()
    await db.refresh(a)
    await db.refresh(b)

    db.add_all([
        Enrollment(student_id=student.id, classroom_id=a.id,
                   status=EnrollmentStatus.ACCEPTED, role=Role.STUDENT),
        Enrollment(student_id=student.id, classroom_id=b.id,
                   status=EnrollmentStatus.ACCEPTED, role=Role.STUDENT),
    ])
    exam_a = Exam(title="A exam", classroom_id=a.id, author_id=prof.id, exam_stage=6)
    exam_b = Exam(title="B exam", classroom_id=b.id, author_id=prof.id, exam_stage=6)
    assign_a = Assignment(title="A hw", classroom_id=a.id, author_id=prof.id)
    ann_a = Announcement(title="A note", content="A note", classroom_id=a.id, author_id=prof.id)
    db.add_all([exam_a, exam_b, assign_a, ann_a])
    await db.commit()
    for row in (exam_a, exam_b, assign_a, ann_a):
        await db.refresh(row)

    q_a = Question(exam_id=exam_a.id, question_number=1, text="A q1", max_marks=5,
                   ms_text_images='["%s"]' % str(course_files["a_ms_image"]).replace("\\", "\\\\"))
    q_b = Question(exam_id=exam_b.id, question_number=1, text="B q1", max_marks=5)
    db.add_all([q_a, q_b])
    await db.commit()
    await db.refresh(q_a)
    await db.refresh(q_b)

    resp_a = QuestionResponse(
        question_id=q_a.id, student_id=student.id, marks_obtained=3,
        ans_text_images='["%s"]' % str(course_files["a_crop"]).replace("\\", "\\\\"),
    )
    resp_b = QuestionResponse(question_id=q_b.id, student_id=student.id, marks_obtained=4)
    script_a = AnswerScript(title="a", file_path=str(course_files["a_script"]),
                            exam_id=exam_a.id, student_id=student.id)
    mat_a = Material(title="qp", file_path=str(course_files["a_material"]),
                     related_exam_id=exam_a.id, author_id=prof.id,
                     file_type=FileTypeEnum.question_paper)
    mat_b = Material(title="qp b", file_path=str(course_files["b_material"]),
                     related_exam_id=exam_b.id, author_id=prof.id,
                     file_type=FileTypeEnum.question_paper)
    sub_a = Submission(assignment_id=assign_a.id, student_id=student.id,
                       file_path=str(course_files["a_submission"]))
    res_a = ExamResult(exam_id=exam_a.id, student_id=student.id, marks_obtained=3, status="graded")
    res_b = ExamResult(exam_id=exam_b.id, student_id=student.id, marks_obtained=4, status="graded")
    db.add_all([resp_a, resp_b, script_a, mat_a, mat_b, sub_a, res_a, res_b])
    await db.commit()
    for row in (resp_a, resp_b, script_a, mat_a, mat_b, sub_a, res_a, res_b):
        await db.refresh(row)

    region_a = DocumentRegion(
        exam_id=exam_a.id, answer_script_id=script_a.id, page_index=0,
        region_type="answer", geometry_kind="rect", geometry="[0,0,1,1]",
        question_id=q_a.id, reading_order=0, status="accepted",
    )
    db.add(region_a)
    await db.commit()
    await db.refresh(region_a)

    return {
        "prof": prof, "other_prof": other_prof, "student": student,
        "a": a, "b": b, "exam_a": exam_a, "exam_b": exam_b,
        "assign_a": assign_a, "ann_a": ann_a,
        "q_a": q_a, "q_b": q_b, "resp_a": resp_a, "resp_b": resp_b,
        "script_a": script_a, "mat_a": mat_a, "mat_b": mat_b,
        "sub_a": sub_a, "res_a": res_a, "res_b": res_b, "region_a": region_a,
    }


# ---------------------------------------------------------------------------
# path safety -- before anything is allowed to delete a file
# ---------------------------------------------------------------------------

def test_a_path_outside_the_root_is_refused(course_files):
    assert resolve_within_root(str(course_files["outside"]), root=course_files["root"]) is None


def test_a_traversal_path_is_refused(course_files):
    escape = str(course_files["root"] / ".." / "not_in_the_root.pdf")
    assert resolve_within_root(escape, root=course_files["root"]) is None


@pytest.mark.parametrize("value", [None, "", "   "])
def test_an_empty_path_is_refused(value, course_files):
    assert resolve_within_root(value, root=course_files["root"]) is None


def test_delete_refuses_to_touch_anything_outside_the_root(course_files):
    outside = course_files["outside"]
    deleted, skipped = delete_files_within_root(
        [str(outside), str(course_files["a_material"])], root=course_files["root"]
    )
    assert outside.exists(), "a file outside the storage root must never be unlinked"
    assert not course_files["a_material"].exists()
    assert (deleted, skipped) == (1, 1)


def test_cleanup_never_raises_on_a_missing_file(course_files):
    missing = course_files["root"] / "a" / "gone.pdf"
    deleted, skipped = delete_files_within_root([str(missing)], root=course_files["root"])
    assert (deleted, skipped) == (0, 1)


# ---------------------------------------------------------------------------
# what the cascade removes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_deleting_a_course_removes_the_course_and_its_owned_rows(two_courses, db):
    w = two_courses
    a_id, exam_a_id, q_a_id = w["a"].id, w["exam_a"].id, w["q_a"].id

    await delete_classroom(w["a"], db)

    assert not await _exists(db, Classroom, a_id)
    assert await _count(db, Enrollment, classroom_id=a_id) == 0
    assert await _count(db, Exam, classroom_id=a_id) == 0
    assert await _count(db, Assignment, classroom_id=a_id) == 0
    assert await _count(db, Announcement, classroom_id=a_id) == 0
    assert await _count(db, Question, exam_id=exam_a_id) == 0
    assert await _count(db, QuestionResponse, question_id=q_a_id) == 0
    assert await _count(db, ExamResult, exam_id=exam_a_id) == 0
    assert await _count(db, AnswerScript, exam_id=exam_a_id) == 0
    assert await _count(db, Material, related_exam_id=exam_a_id) == 0
    assert await _count(db, DocumentRegion, exam_id=exam_a_id) == 0
    assert await _count(db, Submission, assignment_id=w["assign_a"].id) == 0


# ---------------------------------------------------------------------------
# what survives -- the point of a classroom-scoped delete
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_people_survive_the_course(two_courses, db):
    """A user is never course-owned. Deleting a course must not delete anybody."""
    w = two_courses
    prof_id, student_id = w["prof"].id, w["student"].id

    await delete_classroom(w["a"], db)

    assert await _exists(db, User, prof_id), "the owning professor must survive"
    assert await _exists(db, User, student_id), "an enrolled student must survive"


@pytest.mark.asyncio
async def test_the_other_course_is_untouched(two_courses, db):
    w = two_courses
    b_id, exam_b_id = w["b"].id, w["exam_b"].id
    q_b_id, resp_b_id, res_b_id, mat_b_id = (
        w["q_b"].id, w["resp_b"].id, w["res_b"].id, w["mat_b"].id
    )

    await delete_classroom(w["a"], db)

    assert await _exists(db, Classroom, b_id)
    assert await _count(db, Enrollment, classroom_id=b_id) == 1, "B's enrolment stays"
    assert await _exists(db, Exam, exam_b_id)
    assert await _exists(db, Question, q_b_id)
    assert await _exists(db, QuestionResponse, resp_b_id)
    assert await _exists(db, ExamResult, res_b_id)
    assert await _exists(db, Material, mat_b_id)


@pytest.mark.asyncio
async def test_the_shared_students_marks_in_the_other_course_are_unchanged(two_courses, db):
    """The same person's work in Course B keeps its exact value."""
    w = two_courses
    resp_b_id, res_b_id = w["resp_b"].id, w["res_b"].id

    await delete_classroom(w["a"], db)

    resp = (await db.execute(
        select(QuestionResponse).where(QuestionResponse.id == resp_b_id))).scalars().first()
    result = (await db.execute(
        select(ExamResult).where(ExamResult.id == res_b_id))).scalars().first()
    assert float(resp.marks_obtained) == 4.0
    assert float(result.marks_obtained) == 4.0
    assert result.status == "graded"


# ---------------------------------------------------------------------------
# files
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_courses_own_files_are_collected(two_courses, db, course_files):
    """Collected from the same foreign keys the cascade walks, before deleting."""
    paths = await collect_owned_file_paths(two_courses["a"].id, db)
    for key in ("a_material", "a_script", "a_crop", "a_ms_image", "a_submission"):
        assert str(course_files[key]) in paths, f"{key} should be owned by course A"
    assert str(course_files["b_material"]) not in paths, "course B's file is not ours to delete"


@pytest.mark.asyncio
async def test_owned_files_are_deleted_and_other_courses_files_are_not(
    two_courses, db, course_files, monkeypatch
):
    monkeypatch.setattr("backend.classrooms.deletion.delete_files_within_root",
                        lambda paths: delete_files_within_root(paths, root=course_files["root"]))

    outcome = await delete_classroom(two_courses["a"], db)

    for key in ("a_material", "a_script", "a_crop", "a_ms_image", "a_submission"):
        assert not course_files[key].exists(), f"{key} should have been removed"
    assert course_files["b_material"].exists(), "course B's file must survive"
    assert course_files["outside"].exists(), "nothing outside the root is ever touched"
    assert outcome.files_deleted == 5


@pytest.mark.asyncio
async def test_a_file_cleanup_failure_does_not_resurrect_the_rows(two_courses, db, monkeypatch):
    """Rows are already gone by then; a cleanup problem must not undo that."""
    a_id = two_courses["a"].id

    def _all_skipped(paths):
        return 0, len(list(paths))

    monkeypatch.setattr("backend.classrooms.deletion.delete_files_within_root", _all_skipped)

    outcome = await delete_classroom(two_courses["a"], db)

    assert outcome.files_deleted == 0 and outcome.files_skipped > 0
    assert not await _exists(db, Classroom, a_id), "the deletion still stands"


@pytest.mark.asyncio
async def test_an_unparseable_image_column_does_not_abort_the_deletion(two_courses, db):
    w = two_courses
    w["resp_a"].ans_text_images = "not json at all"
    await db.commit()
    a_id = w["a"].id

    await delete_classroom(w["a"], db)

    assert not await _exists(db, Classroom, a_id)


# ---------------------------------------------------------------------------
# authorization, through the real route
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_owner_can_delete_the_course(client, two_courses, db):
    w = two_courses
    r = await client.delete(f"/classes/{w['a'].id}", headers=as_user(w["prof"]))
    assert r.status_code == 200
    assert r.json()["success"] is True
    assert not await _exists(db, Classroom, w["a"].id)


@pytest.mark.asyncio
async def test_an_enrolled_student_cannot_delete_the_course(client, two_courses, db):
    w = two_courses
    r = await client.delete(f"/classes/{w['a'].id}", headers=as_user(w["student"]))
    assert r.status_code == 403
    assert await _exists(db, Classroom, w["a"].id), "a refused delete must change nothing"


@pytest.mark.asyncio
async def test_a_professor_of_another_course_cannot_delete_it(client, two_courses, db):
    w = two_courses
    r = await client.delete(f"/classes/{w['a'].id}", headers=as_user(w["other_prof"]))
    assert r.status_code in (403, 404)
    assert await _exists(db, Classroom, w["a"].id)


@pytest.mark.asyncio
async def test_an_anonymous_caller_cannot_delete_the_course(client, two_courses, db):
    w = two_courses
    r = await client.delete(f"/classes/{w['a'].id}")
    assert r.status_code == 401
    assert await _exists(db, Classroom, w["a"].id)


@pytest.mark.asyncio
async def test_deleting_twice_is_a_404_not_a_crash(client, two_courses):
    w = two_courses
    first = await client.delete(f"/classes/{w['a'].id}", headers=as_user(w["prof"]))
    second = await client.delete(f"/classes/{w['a'].id}", headers=as_user(w["prof"]))
    assert first.status_code == 200
    assert second.status_code == 404


@pytest.mark.asyncio
async def test_a_failure_reports_a_safe_sentence_not_a_traceback(
    client, two_courses, monkeypatch
):
    async def _boom(classroom, db):
        raise RuntimeError("postgres said something with a connection string in it")

    monkeypatch.setattr("backend.routers.classes.delete_classroom", _boom)

    r = await client.delete(f"/classes/{two_courses['a'].id}",
                            headers=as_user(two_courses["prof"]))

    assert r.status_code == 500
    body = r.text
    assert "connection string" not in body and "Traceback" not in body
    assert "Nothing was removed" in r.json()["detail"]
