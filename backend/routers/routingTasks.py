from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from backend.database import get_db
from backend.models.files import AnswerScript
from backend.models.tables import Question, QuestionResponse
from backend.models.users import User
from backend.utils.security import get_current_user_required
from backend.auth.policies import (
    assert_exam_manager,
    assert_student_enrolled_in_exam,
)
from backend.tasks import process_and_grade_exam

router = APIRouter(tags=["routing-tasks"])


@router.post("/exam/{exam_id}/enqueue-processing")
async def enqueue_processing(
    exam_id: int,
    student_id: Optional[int] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    """Start recognition, grading and aggregation for ONE student's paper.

    WHOSE PAPER
    -----------
    This used to read `current_user.id` and nothing else, so the only caller who
    could ever start grading was the student themselves -- and a manager calling
    it queued a run against their OWN id, which has no answer script and no
    responses. Grading was therefore a side effect of a student finishing the
    crop step, which is the opposite of how the product describes itself.

    MANAGER ONLY, AND ENFORCED HERE
    -------------------------------
    Starting a run spends provider quota and rewrites a student's marks, so who
    may start one is a product decision, not a UI detail. The professor decides;
    a student submits, waits and watches.

    This was `assert_self_or_exam_manager`, which also let a student start their
    own paper. The student UI never offered it -- but a hidden button is not an
    authorization boundary, and the endpoint answered a hand-written POST just
    as happily. It is now `assert_exam_manager`: exam-SCOPED, so a professor who
    manages a different course is refused here too, and being `is_professor`
    somewhere grants nothing on this exam.

    `student_id` names whose paper to grade. The manager may name any student of
    THIS exam's classroom and nobody else -- `assert_student_enrolled_in_exam`
    is the second half, exactly as `require_question_in_exam` pairs with its
    own authorization.

    WHAT READINESS MEANS NOW
    ------------------------
    `aggregate_student_result` finalises a result when every response that
    EXISTS carries a mark -- a question with no response row means the student
    skipped it, which must not block finalisation forever. With ZERO response
    rows that rule is vacuously satisfied, so a run for a student whose paper
    was never prepared would aggregate to `0.0`, stamp `graded`, and set
    `graded_at`: a fabricated final zero, exactly the distinction C6 exists to
    protect.

    That invariant is untouched; it has moved to where aggregation actually
    happens. `_process_and_grade` now prepares responses from the uploaded
    script first and RETURNS BEFORE AGGREGATING if preparation produced
    nothing, so zero responses still cannot become a final zero.

    What this route refuses is therefore narrower and more honest: a run with
    nothing to read at all. No responses AND no uploaded script means no
    automatic stage could produce anything, so there is no point queueing a job
    -- and asking a student to cut their own script up first, which is what the
    old check effectively did, is not the AI-first flow this product describes.
    """
    await assert_exam_manager(exam_id, current_user, db)

    # A manager who names nobody falls through to their own id, which is not a
    # student of this exam, so the enrolment check below refuses it. That is the
    # same 403 as before and is deliberate: queueing a run against the caller is
    # what made the original route wrong.
    target_student_id = student_id if student_id is not None else current_user.id

    await assert_student_enrolled_in_exam(exam_id, target_student_id, current_user, db)

    prepared = await db.execute(
        select(func.count())
        .select_from(QuestionResponse)
        .join(Question, Question.id == QuestionResponse.question_id)
        .where(
            Question.exam_id == exam_id,
            QuestionResponse.student_id == target_student_id,
        )
    )
    if not prepared.scalar():
        # No responses yet is no longer a refusal: the job prepares them from
        # the uploaded script itself (see backend/grading/preparation.py). What
        # is still refused is a run with NOTHING to read -- there is no paper,
        # so there is nothing an automatic stage could turn into responses.
        #
        # The invariant the old check protected has not been relaxed, it has
        # moved to where aggregation actually happens: `_process_and_grade`
        # returns before aggregating unless preparation left something to grade,
        # so zero responses can still never become a fabricated 0.0.
        script = await db.execute(
            select(func.count())
            .select_from(AnswerScript)
            .where(
                AnswerScript.exam_id == exam_id,
                AnswerScript.student_id == target_student_id,
            )
        )
        if not script.scalar():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This student has no uploaded answer script to grade yet.",
            )

    process_and_grade_exam.delay(exam_id, target_student_id)
    return {
        "message": "AI grading started.",
        "exam_id": exam_id,
        "student_id": target_student_id,
    }
