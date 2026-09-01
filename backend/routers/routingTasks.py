from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from backend.database import get_db
from backend.models.tables import Question, QuestionResponse
from backend.models.users import User
from backend.utils.security import get_current_user_required
from backend.auth.policies import (
    assert_self_or_exam_manager,
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

    `student_id` is optional and is honoured only for exam managers, matching
    the pattern already used by `/protected-files/exam/{id}/document/{type}`. A
    student may still start their own run and may not name anyone else.

    WHY THE READINESS CHECK IS NOT OPTIONAL
    ---------------------------------------
    `aggregate_student_result` finalises a result when every response that
    EXISTS carries a mark -- a question with no response row means the student
    skipped it, which must not block finalisation forever. With ZERO response
    rows that rule is vacuously satisfied, so a run for a student whose script
    has never been prepared would aggregate to `0.0`, stamp `graded`, and set
    `graded_at`: a fabricated final zero, which is precisely the distinction C6
    exists to protect. The old flow could not reach that state because the only
    trigger was the crop submit that creates the rows. Moving the trigger to a
    manager makes it reachable, so it is refused here instead.
    """
    ctx = await assert_self_or_exam_manager(
        exam_id, student_id if student_id is not None else current_user.id, current_user, db
    )

    if ctx.is_manager:
        target_student_id = student_id if student_id is not None else current_user.id
    else:
        # A student may only ever run their own paper. Naming anyone else was
        # already refused above; naming yourself explicitly is fine.
        target_student_id = current_user.id

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
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This answer script has no prepared responses to grade yet.",
        )

    process_and_grade_exam.delay(exam_id, target_student_id)
    return {
        "message": "AI grading started.",
        "exam_id": exam_id,
        "student_id": target_student_id,
    }
