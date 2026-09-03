"""What a student may be told about their own paper while it is being graded.

Two rules carry this file.

**A genuine 0.00 is a graded question.** The count is
`marks_obtained IS NOT NULL` and never truthiness -- testing the score itself
would silently reclassify every real zero as ungraded, which is the exact C6
confusion the rest of the grading code exists to keep out. A NULL, which is
what a failed question carries, is not graded and must not be counted.

**A partial mark is not a result.** While a run is moving the student sees a
phase, a count and a percentage. They do not see any individual score, any
reasoning, any failure code or any provider text -- so a half-finished run can
never be read as a grade.

No network, no provider, no quota.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from backend.grading.aggregation import ExamResultStatus
from backend.models.files import AnswerScript
from backend.models.tables import (
    Classroom, Enrollment, EnrollmentStatus, Exam, ExamResult,
    Question, QuestionResponse, Role,
)
from backend.models.users import User

from .conftest import as_user

STATUS_URL = "/exams/{exam_id}/my-grading-status"


@pytest.fixture
async def exam_world(db, tmp_path):
    """One exam with 5 questions, one enrolled student, and a bystander student."""
    prof = User(email="p@s.test", hashed_password="x", full_name="Prof", is_professor=True)
    me = User(email="me@s.test", hashed_password="x", full_name="Me", is_professor=False)
    other = User(email="other@s.test", hashed_password="x", full_name="Other", is_professor=False)
    outsider = User(email="out@s.test", hashed_password="x", full_name="Out", is_professor=False)
    db.add_all([prof, me, other, outsider])
    await db.commit()
    for u in (prof, me, other, outsider):
        await db.refresh(u)

    room = Classroom(name="C", subject="CS", owner_id=prof.id, class_code="SGSTAT")
    db.add(room)
    await db.commit()
    await db.refresh(room)

    db.add_all([
        Enrollment(student_id=me.id, classroom_id=room.id,
                   status=EnrollmentStatus.ACCEPTED, role=Role.STUDENT),
        Enrollment(student_id=other.id, classroom_id=room.id,
                   status=EnrollmentStatus.ACCEPTED, role=Role.STUDENT),
    ])
    exam = Exam(title="E", classroom_id=room.id, author_id=prof.id, exam_stage=5)
    db.add(exam)
    await db.commit()
    await db.refresh(exam)

    questions = [
        Question(exam_id=exam.id, question_number=i, text=f"q{i}", max_marks=5)
        for i in range(1, 6)
    ]
    db.add_all(questions)
    await db.commit()
    for q in questions:
        await db.refresh(q)

    return {"prof": prof, "me": me, "other": other, "outsider": outsider,
            "room": room, "exam": exam, "questions": questions, "tmp": tmp_path}


async def _add_script(db, world, student, tmp_path):
    path = tmp_path / f"script_{student.id}.pdf"
    path.write_bytes(b"%PDF-1.4 synthetic")
    db.add(AnswerScript(title="s.pdf", file_path=str(path),
                        exam_id=world["exam"].id, student_id=student.id))
    await db.commit()


async def _add_responses(db, world, student, marks):
    """One response per mark, in question order. `None` means grading failed."""
    for question, mark in zip(world["questions"], marks):
        db.add(QuestionResponse(question_id=question.id, student_id=student.id,
                                answer_text="a", marks_obtained=mark))
    await db.commit()


async def _status(client, world, user):
    return await client.get(
        STATUS_URL.format(exam_id=world["exam"].id), headers=as_user(user)
    )


# ---------------------------------------------------------------------------
# phases, in the order a paper travels through them
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_submission_yet(client, exam_world):
    r = await _status(client, exam_world, exam_world["me"])
    body = r.json()
    assert r.status_code == 200
    assert body["phase"] == "not_submitted"
    assert body["total_questions"] == 5
    assert body["graded_questions"] == 0
    assert body["progress_percent"] == 0
    assert body["result_ready"] is False


@pytest.mark.asyncio
async def test_submitted_but_grading_not_started(client, exam_world, db):
    """A script exists and no responses do: waiting for the instructor to start.

    This phase also covers automatic preparation actually running -- nothing
    persisted separates the two, and the endpoint says so rather than guessing.
    """
    await _add_script(db, exam_world, exam_world["me"], exam_world["tmp"])

    body = (await _status(client, exam_world, exam_world["me"])).json()

    assert body["phase"] == "waiting_for_grading"
    assert body["graded_questions"] == 0
    assert body["result_ready"] is False


@pytest.mark.asyncio
async def test_responses_exist_but_none_graded_is_grading_at_zero(client, exam_world, db):
    await _add_script(db, exam_world, exam_world["me"], exam_world["tmp"])
    await _add_responses(db, exam_world, exam_world["me"], [None] * 5)

    body = (await _status(client, exam_world, exam_world["me"])).json()

    assert body["phase"] == "grading"
    assert body["graded_questions"] == 0
    assert body["progress_percent"] == 0


@pytest.mark.asyncio
async def test_one_of_five_graded(client, exam_world, db):
    await _add_script(db, exam_world, exam_world["me"], exam_world["tmp"])
    await _add_responses(db, exam_world, exam_world["me"], [4, None, None, None, None])

    body = (await _status(client, exam_world, exam_world["me"])).json()

    assert body["phase"] == "grading"
    assert (body["graded_questions"], body["total_questions"]) == (1, 5)
    assert body["progress_percent"] == 20


@pytest.mark.asyncio
async def test_three_of_five_graded_is_sixty_percent(client, exam_world, db):
    await _add_script(db, exam_world, exam_world["me"], exam_world["tmp"])
    await _add_responses(db, exam_world, exam_world["me"], [3, 4, 5, None, None])

    body = (await _status(client, exam_world, exam_world["me"])).json()

    assert (body["graded_questions"], body["total_questions"]) == (3, 5)
    assert body["progress_percent"] == 60


@pytest.mark.asyncio
async def test_a_genuine_zero_counts_as_graded(client, exam_world, db):
    """0.00 is a mark a student earned. Truthiness would erase it."""
    await _add_script(db, exam_world, exam_world["me"], exam_world["tmp"])
    await _add_responses(db, exam_world, exam_world["me"], [0, 0, 0, None, None])

    body = (await _status(client, exam_world, exam_world["me"])).json()

    assert body["graded_questions"] == 3, "three real zeros are three graded questions"
    assert body["progress_percent"] == 60


@pytest.mark.asyncio
async def test_a_fractional_mark_counts_as_graded(client, exam_world, db):
    await _add_script(db, exam_world, exam_world["me"], exam_world["tmp"])
    await _add_responses(db, exam_world, exam_world["me"], [0.5, 2.25, None, None, None])

    body = (await _status(client, exam_world, exam_world["me"])).json()

    assert body["graded_questions"] == 2
    assert body["progress_percent"] == 40


@pytest.mark.asyncio
async def test_a_null_failure_does_not_count_as_graded(client, exam_world, db):
    await _add_script(db, exam_world, exam_world["me"], exam_world["tmp"])
    await _add_responses(db, exam_world, exam_world["me"], [5, None, 5, None, 5])

    body = (await _status(client, exam_world, exam_world["me"])).json()

    assert body["graded_questions"] == 3, "the two NULLs are not progress"
    assert body["phase"] == "grading"


@pytest.mark.asyncio
async def test_all_graded_but_no_final_result_is_finalizing(client, exam_world, db):
    await _add_script(db, exam_world, exam_world["me"], exam_world["tmp"])
    await _add_responses(db, exam_world, exam_world["me"], [1, 2, 3, 4, 5])

    body = (await _status(client, exam_world, exam_world["me"])).json()

    assert body["phase"] == "finalizing"
    assert body["progress_percent"] == 100
    assert body["result_ready"] is False, "not final until aggregation says so"


@pytest.mark.asyncio
async def test_a_final_exam_result_is_complete(client, exam_world, db):
    await _add_script(db, exam_world, exam_world["me"], exam_world["tmp"])
    await _add_responses(db, exam_world, exam_world["me"], [1, 2, 3, 4, 5])
    db.add(ExamResult(exam_id=exam_world["exam"].id, student_id=exam_world["me"].id,
                      marks_obtained=15, status=ExamResultStatus.GRADED))
    await db.commit()

    body = (await _status(client, exam_world, exam_world["me"])).json()

    assert body["phase"] == "complete"
    assert body["result_ready"] is True


@pytest.mark.asyncio
async def test_an_incomplete_result_asks_for_instructor_review(client, exam_world, db):
    """Aggregation ran and found an ungraded response. A settled outcome."""
    await _add_script(db, exam_world, exam_world["me"], exam_world["tmp"])
    await _add_responses(db, exam_world, exam_world["me"], [1, None, 3, 4, 5])
    db.add(ExamResult(exam_id=exam_world["exam"].id, student_id=exam_world["me"].id,
                      marks_obtained=13, status=ExamResultStatus.GRADING_INCOMPLETE))
    await db.commit()

    body = (await _status(client, exam_world, exam_world["me"])).json()

    assert body["phase"] == "needs_attention"
    assert body["result_ready"] is False


# ---------------------------------------------------------------------------
# whose paper this is, and what may not leak
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_student_sees_only_their_own_progress(client, exam_world, db):
    """The other student is fully graded; that must not show up as mine."""
    await _add_script(db, exam_world, exam_world["other"], exam_world["tmp"])
    await _add_responses(db, exam_world, exam_world["other"], [5, 5, 5, 5, 5])

    body = (await _status(client, exam_world, exam_world["me"])).json()

    assert body["phase"] == "not_submitted"
    assert body["graded_questions"] == 0, "another student's marks are not my progress"


@pytest.mark.asyncio
async def test_the_route_takes_no_student_id_to_impersonate_with(client, exam_world, db):
    """There is no id to pass: the rows are pinned to the caller."""
    await _add_script(db, exam_world, exam_world["other"], exam_world["tmp"])
    await _add_responses(db, exam_world, exam_world["other"], [5, 5, 5, 5, 5])

    r = await client.get(
        STATUS_URL.format(exam_id=exam_world["exam"].id)
        + f"?student_id={exam_world['other'].id}",
        headers=as_user(exam_world["me"]),
    )

    assert r.json()["graded_questions"] == 0, "a query parameter must not select somebody else"


@pytest.mark.asyncio
async def test_a_user_enrolled_in_nothing_is_refused(client, exam_world):
    r = await _status(client, exam_world, exam_world["outsider"])
    assert r.status_code in (403, 404)


@pytest.mark.asyncio
async def test_an_anonymous_caller_is_refused(client, exam_world):
    r = await client.get(STATUS_URL.format(exam_id=exam_world["exam"].id))
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_no_marks_reasons_or_failure_codes_are_returned(client, exam_world, db):
    """The body carries counts and a phase. Nothing else may ride along."""
    await _add_script(db, exam_world, exam_world["me"], exam_world["tmp"])
    for question, mark, reason, code in zip(
        exam_world["questions"],
        [3, None, 5, None, 4],
        ["SECRET-REASONING", "SECRET-REASONING", "x", "y", "z"],
        [None, "malformed_json", None, "timeout", None],
    ):
        db.add(QuestionResponse(
            question_id=question.id, student_id=exam_world["me"].id,
            answer_text="STUDENT-ANSWER-TEXT", marks_obtained=mark,
            reasoning=reason, grading_error_code=code,
        ))
    await db.commit()

    r = await _status(client, exam_world, exam_world["me"])
    raw = r.text

    assert set(r.json().keys()) == {
        "phase", "total_questions", "graded_questions",
        "progress_percent", "result_ready", "message",
    }
    for leak in ("SECRET-REASONING", "STUDENT-ANSWER-TEXT",
                 "malformed_json", "timeout", "marks_obtained"):
        assert leak not in raw, f"{leak} must never reach a student"
    # A partial total is a mark. Neither the individual scores nor their sum.
    for score in ("3", "5", "4"):
        assert f'"{score}"' not in raw


@pytest.mark.asyncio
async def test_the_message_is_a_safe_sentence_for_a_failed_run(client, exam_world, db):
    await _add_script(db, exam_world, exam_world["me"], exam_world["tmp"])
    await _add_responses(db, exam_world, exam_world["me"], [1, None, 3, 4, 5])
    db.add(ExamResult(exam_id=exam_world["exam"].id, student_id=exam_world["me"].id,
                      marks_obtained=13, status=ExamResultStatus.GRADING_INCOMPLETE))
    await db.commit()

    body = (await _status(client, exam_world, exam_world["me"])).json()

    assert body["message"] == (
        "Grading is taking longer than expected. Your instructor has been notified."
    )


@pytest.mark.asyncio
async def test_an_exam_with_no_questions_does_not_divide_by_zero(client, exam_world, db):
    empty = Exam(title="empty", classroom_id=exam_world["room"].id,
                 author_id=exam_world["prof"].id, exam_stage=1)
    db.add(empty)
    await db.commit()
    await db.refresh(empty)

    r = await client.get(STATUS_URL.format(exam_id=empty.id),
                         headers=as_user(exam_world["me"]))

    body = r.json()
    assert body["total_questions"] == 0
    assert body["progress_percent"] == 0


# ---------------------------------------------------------------------------
# the grading trigger stays the instructor's
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_this_endpoint_starts_no_grading(client, exam_world, db):
    """Reading progress must never create work -- it is a GET over stored rows."""
    await _add_script(db, exam_world, exam_world["me"], exam_world["tmp"])

    for _ in range(3):
        await _status(client, exam_world, exam_world["me"])

    rows = (await db.execute(
        select(QuestionResponse).join(Question, Question.id == QuestionResponse.question_id)
        .where(Question.exam_id == exam_world["exam"].id)
    )).scalars().all()
    assert rows == [], "polling must not prepare, grade, or aggregate anything"
