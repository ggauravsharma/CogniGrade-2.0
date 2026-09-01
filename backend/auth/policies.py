"""Central authorization policies for CogniGrade.

Authentication (who the user is) lives in `backend/utils/security.py`.
Authorization (what that user may touch) lives here, so that route handlers
never have to re-implement an ownership check and never accidentally omit one.

DOMAIN MODEL THIS IS BUILT ON
-----------------------------
There is no invented role system. The repository already expresses roles in
three places, and all three are honoured:

    User.is_professor          global flag, set at signup
    Classroom.owner_id         the professor who created the classroom
    Exam.author_id             the professor who created the exam
    Enrollment(student_id, classroom_id, status, role)
                               per-classroom membership; role is
                               student | ta | professor, status must be accepted

From those we derive exactly two capabilities against an exam:

    MANAGER   may read and modify the exam, its questions, and every student's
              work in it. True when the user owns the classroom, authored the
              exam, or holds an accepted professor/TA enrolment in the
              classroom. `is_professor` alone is NEVER sufficient -- a
              professor of one classroom has no rights over another.

    PARTICIPANT
              a student with an accepted `role=student` enrolment in the
              exam's classroom. May read their OWN work and nothing else.

ERROR SEMANTICS
---------------
    401  handled upstream by get_current_user_required
    403  authenticated, resource exists, caller lacks the capability
    404  resource does not exist

We deliberately do NOT return 404 to mask 403. Exam and classroom ids are
sequential and already enumerable to any enrolled member, so masking buys
little, while a truthful 403 makes misconfiguration debuggable. Denials are
logged with the acting user id and the resource id -- never with tokens,
file paths, or answer content.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from fastapi import Depends, HTTPException, Path, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from backend.database import get_db
from backend.models.tables import (
    Announcement,
    Assignment,
    Classroom,
    Enrollment,
    EnrollmentStatus,
    Exam,
    Question,
    QuestionResponse,
    Role,
    Submission,
)
from backend.models.users import User
from backend.utils.security import get_current_user_required

logger = logging.getLogger(__name__)

MANAGER_ROLES = (Role.PROFESSOR, Role.TA)


# ---------------------------------------------------------------------------
# context
# ---------------------------------------------------------------------------

@dataclass
class ExamContext:
    """Everything a handler needs to make an access decision, loaded once.

    Handlers receive this instead of re-querying the exam, which is why the
    policies below cost at most two queries no matter how many are chained.
    """

    user: User
    exam: Exam
    classroom: Optional[Classroom]
    is_manager: bool
    is_enrolled_student: bool

    @property
    def exam_id(self) -> int:
        return self.exam.id

    def owns(self, student_id: int) -> bool:
        """True if the caller may act on this student's work in this exam."""
        return self.is_manager or self.user.id == student_id


def _deny(reason: str, *, user_id: int, resource: str) -> HTTPException:
    logger.warning("authz denied: %s (user_id=%s resource=%s)", reason, user_id, resource)
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")


async def load_exam_context(
    exam_id: int, user: User, db: AsyncSession
) -> ExamContext:
    """Resolve the caller's capabilities against one exam. Two queries, max.

    Usable both as a building block for the dependencies below and directly
    from handlers whose exam_id arrives in a request body rather than the path.
    """
    result = await db.execute(select(Exam).where(Exam.id == exam_id))
    exam = result.scalars().first()
    if not exam:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Exam not found")

    classroom = None
    if exam.classroom_id is not None:
        result = await db.execute(
            select(Classroom).where(Classroom.id == exam.classroom_id)
        )
        classroom = result.scalars().first()

    is_manager = False
    is_enrolled_student = False

    if classroom is not None and classroom.owner_id == user.id:
        is_manager = True
    if exam.author_id == user.id:
        is_manager = True

    if exam.classroom_id is not None:
        result = await db.execute(
            select(Enrollment).where(
                Enrollment.classroom_id == exam.classroom_id,
                Enrollment.student_id == user.id,
                Enrollment.status == EnrollmentStatus.ACCEPTED,
            )
        )
        enrolment = result.scalars().first()
        if enrolment is not None:
            if enrolment.role in MANAGER_ROLES:
                is_manager = True
            elif enrolment.role == Role.STUDENT:
                is_enrolled_student = True

    return ExamContext(
        user=user,
        exam=exam,
        classroom=classroom,
        is_manager=is_manager,
        is_enrolled_student=is_enrolled_student,
    )


# ---------------------------------------------------------------------------
# imperative assertions -- for handlers whose ids come from the request body
# ---------------------------------------------------------------------------

async def assert_exam_manager(exam_id: int, user: User, db: AsyncSession) -> ExamContext:
    ctx = await load_exam_context(exam_id, user, db)
    if not ctx.is_manager:
        raise _deny("not an exam manager", user_id=user.id, resource=f"exam:{exam_id}")
    return ctx


async def assert_exam_participant(exam_id: int, user: User, db: AsyncSession) -> ExamContext:
    ctx = await load_exam_context(exam_id, user, db)
    if not (ctx.is_manager or ctx.is_enrolled_student):
        raise _deny("not a member of the exam's classroom",
                    user_id=user.id, resource=f"exam:{exam_id}")
    return ctx


async def assert_self_or_exam_manager(
    exam_id: int, student_id: int, user: User, db: AsyncSession
) -> ExamContext:
    ctx = await load_exam_context(exam_id, user, db)
    if ctx.is_manager:
        return ctx
    if ctx.is_enrolled_student and user.id == student_id:
        return ctx
    raise _deny("not the owning student and not an exam manager",
                user_id=user.id, resource=f"exam:{exam_id}/student:{student_id}")


# ---------------------------------------------------------------------------
# FastAPI dependencies -- for handlers whose ids are path parameters
# ---------------------------------------------------------------------------

async def require_professor(
    current_user: User = Depends(get_current_user_required),
) -> User:
    """Global professor flag only.

    Use this ONLY where no exam or classroom is in scope. Anywhere a specific
    resource exists, prefer a manager policy -- being a professor somewhere
    must not grant rights everywhere.
    """
    if not current_user.is_professor:
        raise _deny("not a professor", user_id=current_user.id, resource="global")
    return current_user


async def require_exam_manager(
    exam_id: int = Path(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
) -> ExamContext:
    return await assert_exam_manager(exam_id, current_user, db)


async def require_exam_participant(
    exam_id: int = Path(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
) -> ExamContext:
    return await assert_exam_participant(exam_id, current_user, db)


async def require_self_or_exam_manager(
    exam_id: int = Path(...),
    student_id: int = Path(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
) -> ExamContext:
    return await assert_self_or_exam_manager(exam_id, student_id, current_user, db)


async def require_question_in_exam(
    exam_id: int = Path(...),
    question_id: int = Path(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
) -> ExamContext:
    """Manager rights over the exam AND the question must belong to that exam.

    Without the second half, a manager of exam A could pass question_id from
    exam B and edit it, because most handlers look the question up by id alone.
    """
    ctx = await assert_exam_manager(exam_id, current_user, db)
    result = await db.execute(select(Question).where(Question.id == question_id))
    question = result.scalars().first()
    if not question:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Question not found")
    if question.exam_id != exam_id:
        raise _deny("question does not belong to this exam",
                    user_id=current_user.id, resource=f"exam:{exam_id}/question:{question_id}")
    return ctx


async def require_question_access_for_student(
    exam_id: int = Path(...),
    question_id: int = Path(...),
    student_id: int = Path(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
) -> ExamContext:
    """Read/act on one student's answer to one question."""
    ctx = await assert_self_or_exam_manager(exam_id, student_id, current_user, db)
    result = await db.execute(select(Question).where(Question.id == question_id))
    question = result.scalars().first()
    if not question:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Question not found")
    if question.exam_id != exam_id:
        raise _deny("question does not belong to this exam",
                    user_id=current_user.id, resource=f"exam:{exam_id}/question:{question_id}")
    return ctx


# ---------------------------------------------------------------------------
# response-level helper
# ---------------------------------------------------------------------------

async def assert_can_access_response(
    response_id: int, user: User, db: AsyncSession
) -> QuestionResponse:
    """Authorize access to a single QuestionResponse by its own id.

    Used by protected file serving, where the client supplies a response id
    rather than an exam id.
    """
    result = await db.execute(
        select(QuestionResponse).where(QuestionResponse.id == response_id)
    )
    qr = result.scalars().first()
    if not qr:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Response not found")

    result = await db.execute(select(Question).where(Question.id == qr.question_id))
    question = result.scalars().first()
    if not question:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Question not found")

    ctx = await load_exam_context(question.exam_id, user, db)
    if not ctx.owns(qr.student_id):
        raise _deny("not the owning student and not an exam manager",
                    user_id=user.id, resource=f"response:{response_id}")
    return qr


async def assert_can_access_question_material(
    question_id: int, user: User, db: AsyncSession
) -> Question:
    """Marking-scheme material is manager-only: it must not reach students."""
    result = await db.execute(select(Question).where(Question.id == question_id))
    question = result.scalars().first()
    if not question:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Question not found")
    await assert_exam_manager(question.exam_id, user, db)
    return question


# ===========================================================================
# CLASSROOM-SCOPED POLICIES  (Security Foundation v2)
# ===========================================================================
#
# The exam policies above resolve capabilities against an Exam. Everything
# below resolves them against a Classroom, which is the parent resource for
# enrolments, announcements, assignments and submissions.
#
# CAPABILITIES
#     OWNER        Classroom.owner_id == user.id. Reserved for
#                  ownership-sensitive actions: promoting a member to TA, and
#                  accepting / rejecting / removing enrolments. Kept
#                  deliberately narrow because promoting a TA grants MANAGER
#                  rights, so it must not itself be a manager-level action.
#     MANAGER      owner, or an accepted professor/TA enrolment. May create and
#                  modify classroom content.
#     PARTICIPANT  manager, or an accepted student enrolment. May read
#                  classroom content.
#
# `User.is_professor` is NEVER sufficient on its own. Several routes previously
# granted access on that flag alone, which let a professor of one classroom act
# in every other classroom in the database.


@dataclass
class ClassroomContext:
    user: User
    classroom: Classroom
    enrollment: Optional[Enrollment]
    is_owner: bool
    is_manager: bool
    is_participant: bool

    @property
    def classroom_id(self) -> int:
        return self.classroom.id


async def load_classroom_context(
    class_id: int, user: User, db: AsyncSession
) -> ClassroomContext:
    """Resolve the caller's capabilities against one classroom. Two queries."""
    result = await db.execute(select(Classroom).where(Classroom.id == class_id))
    classroom = result.scalars().first()
    if not classroom:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Class not found")

    result = await db.execute(
        select(Enrollment).where(
            Enrollment.classroom_id == class_id,
            Enrollment.student_id == user.id,
            Enrollment.status == EnrollmentStatus.ACCEPTED,
        )
    )
    enrolment = result.scalars().first()

    is_owner = classroom.owner_id == user.id
    is_manager = is_owner or (enrolment is not None and enrolment.role in MANAGER_ROLES)
    is_participant = is_manager or (
        enrolment is not None and enrolment.role == Role.STUDENT
    )

    return ClassroomContext(
        user=user,
        classroom=classroom,
        enrollment=enrolment,
        is_owner=is_owner,
        is_manager=is_manager,
        is_participant=is_participant,
    )


async def assert_classroom_participant(
    class_id: int, user: User, db: AsyncSession
) -> ClassroomContext:
    ctx = await load_classroom_context(class_id, user, db)
    if not ctx.is_participant:
        raise _deny("not a member of this classroom",
                    user_id=user.id, resource=f"classroom:{class_id}")
    return ctx


async def assert_classroom_manager(
    class_id: int, user: User, db: AsyncSession
) -> ClassroomContext:
    ctx = await load_classroom_context(class_id, user, db)
    if not ctx.is_manager:
        raise _deny("not a manager of this classroom",
                    user_id=user.id, resource=f"classroom:{class_id}")
    return ctx


async def assert_classroom_owner(
    class_id: int, user: User, db: AsyncSession
) -> ClassroomContext:
    ctx = await load_classroom_context(class_id, user, db)
    if not ctx.is_owner:
        raise _deny("not the owner of this classroom",
                    user_id=user.id, resource=f"classroom:{class_id}")
    return ctx


# --- FastAPI dependencies (class_id in the path) ---------------------------

async def require_classroom_participant(
    class_id: int = Path(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
) -> ClassroomContext:
    return await assert_classroom_participant(class_id, current_user, db)


async def require_classroom_manager(
    class_id: int = Path(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
) -> ClassroomContext:
    return await assert_classroom_manager(class_id, current_user, db)


async def require_classroom_owner(
    class_id: int = Path(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
) -> ClassroomContext:
    return await assert_classroom_owner(class_id, current_user, db)


# ---------------------------------------------------------------------------
# cross-resource integrity
# ---------------------------------------------------------------------------
#
# Each helper below resolves the PARENT classroom from the child resource
# itself, rather than trusting a class_id supplied alongside it. Where both ids
# are supplied they are checked against each other, so a caller cannot
# authorize against classroom A and then operate on a resource in classroom B.


async def assert_enrollment_manageable(
    enrollment_id: int, user: User, db: AsyncSession, *, require_owner: bool = True
) -> tuple[Enrollment, ClassroomContext]:
    """Load an enrolment and authorize against ITS OWN classroom.

    `require_owner` preserves the pre-existing semantics of the enrolment
    routes, which were already owner-only. Membership management is not
    widened to TAs by this security phase.
    """
    result = await db.execute(select(Enrollment).where(Enrollment.id == enrollment_id))
    enrolment = result.scalars().first()
    if not enrolment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Enrollment not found")

    if require_owner:
        ctx = await assert_classroom_owner(enrolment.classroom_id, user, db)
    else:
        ctx = await assert_classroom_manager(enrolment.classroom_id, user, db)
    return enrolment, ctx


async def assert_announcement_in_classroom(
    class_id: int, announcement_id: int, db: AsyncSession
) -> Announcement:
    """The announcement must belong to the classroom named in the path."""
    result = await db.execute(
        select(Announcement).where(
            Announcement.id == announcement_id,
            Announcement.classroom_id == class_id,
        )
    )
    announcement = result.scalars().first()
    if not announcement:
        # 404 rather than 403: confirming that the announcement exists in some
        # other classroom would leak cross-classroom information.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Announcement not found")
    return announcement


async def assert_assignment_access(
    assignment_id: int, user: User, db: AsyncSession, *, manager_only: bool = False
) -> tuple[Assignment, ClassroomContext]:
    """Authorize against the assignment's OWN classroom."""
    result = await db.execute(select(Assignment).where(Assignment.id == assignment_id))
    assignment = result.scalars().first()
    if not assignment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Assignment not found")
    if manager_only:
        ctx = await assert_classroom_manager(assignment.classroom_id, user, db)
    else:
        ctx = await assert_classroom_participant(assignment.classroom_id, user, db)
    return assignment, ctx


async def assert_submission_access(
    submission_id: int, user: User, db: AsyncSession, *, manager_only: bool = False
) -> tuple[Submission, ClassroomContext]:
    """Authorize a submission through submission -> assignment -> classroom.

    A student may reach their own submission; a manager may reach any in their
    classroom. Nobody else, regardless of the global professor flag.
    """
    result = await db.execute(select(Submission).where(Submission.id == submission_id))
    submission = result.scalars().first()
    if not submission:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Submission not found")

    result = await db.execute(
        select(Assignment).where(Assignment.id == submission.assignment_id)
    )
    assignment = result.scalars().first()
    if not assignment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Assignment not found")

    ctx = await load_classroom_context(assignment.classroom_id, user, db)
    if manager_only:
        if not ctx.is_manager:
            raise _deny("not a manager of the submission's classroom",
                        user_id=user.id, resource=f"submission:{submission_id}")
    else:
        if not (ctx.is_manager or submission.student_id == user.id):
            raise _deny("not the owning student and not a manager",
                        user_id=user.id, resource=f"submission:{submission_id}")
    return submission, ctx


async def require_announcement_in_classroom(
    class_id: int = Path(...),
    announcement_id: int = Path(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
) -> ClassroomContext:
    """Manager rights over the classroom AND the announcement must live in it.

    Deliberately a DEPENDENCY rather than an in-body check. Several handlers in
    classes.py wrap their entire body in `except Exception -> HTTPException(500)`,
    which swallows a deliberate 404 or 403 raised inside the handler and reports
    it as a server error. Running the cross-resource check as a dependency puts
    it outside that try block, so the correct status reaches the client.
    """
    ctx = await assert_classroom_manager(class_id, current_user, db)
    await assert_announcement_in_classroom(class_id, announcement_id, db)
    return ctx
