from typing import Optional, List, Dict
from fastapi import APIRouter, Depends, HTTPException, Form
from fastapi.responses import JSONResponse
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload
from datetime import datetime, timezone
from sqlalchemy.dialects.postgresql import TIMESTAMP
from backend.database import get_db
from backend.models.tables import Exam, ExamResult, Enrollment, Question, QuestionResponse
from backend.models.files import AnswerScript
from backend.models.users import User
from backend.utils.security import get_current_user_required
from backend.utils.marks_input import parse_mark_input
from backend.grading.aggregation import (
    ExamResultStatus,
    aggregate_student_result,
    log_incomplete,
)
from backend.grading.failure import GradingFailure, describe, UNEXPECTED_ERROR
from backend.routers.exams import (
    EXAM_STAGE_GRADED, EXAM_STAGE_GRADING, set_exam_stage,
)
from backend.auth.policies import (
    ExamContext,
    require_exam_manager,
    require_exam_participant,
    require_question_access_for_student,
    require_question_in_exam,
    require_self_or_exam_manager,
)
from backend.routers.geminiAPI import grade_question, grade_question_with_diagram, extract_single_answer_text
import logging
import math
import re

logger = logging.getLogger(__name__)
router = APIRouter(tags=["exam-stats"])

@router.get("/exams/{exam_id}/stats")
async def get_exam_stats(exam_id: int,
                        db: AsyncSession = Depends(get_db),
    ctx: ExamContext = Depends(require_exam_manager),
                        current_user: User = Depends(get_current_user_required)):
    if not current_user.is_professor:
        raise HTTPException(status_code=403, detail="Access denied")
    
    result = await db.execute(select(Exam).where(Exam.id == exam_id))
    exam = result.scalars().first()
    if not exam:
        raise HTTPException(status_code=404, detail="Exam not found")
    
    result = await db.execute(select(Enrollment)
        .options(selectinload(Enrollment.student))
        .where(
            Enrollment.classroom_id == exam.classroom_id,
            Enrollment.status == "accepted",
            Enrollment.role == "student"
        )
    )
    enrollments = result.scalars().all()

    # Which questions have no validated mark, and why -- for every student in
    # one query rather than one per student, so surfacing the reason does not
    # make this endpoint slower per head. The rule for "failed" is the same one
    # aggregation uses (no mark at all), so the list can never contradict the
    # status shown beside it, and a legitimate 0 is never listed (audit C6).
    failed_rows = await db.execute(
        select(
            QuestionResponse.student_id,
            QuestionResponse.question_id,
            Question.question_number,
            QuestionResponse.grading_error_code,
        )
        .join(Question, Question.id == QuestionResponse.question_id)
        .where(
            Question.exam_id == exam_id,
            QuestionResponse.marks_obtained.is_(None),
        )
        .order_by(Question.question_number)
    )
    failures_by_student: Dict[int, List[dict]] = {}
    for student_id, question_id, question_number, error_code in failed_rows.all():
        failures_by_student.setdefault(student_id, []).append(
            GradingFailure(
                question_id=question_id,
                question_number=question_number,
                error_code=error_code,
            ).as_dict()
        )

    students = []
    total_answer_scripts = 0
    graded_scripts = 0
    for enrollment in enrollments:
        student = enrollment.student
        result = await db.execute(select(ExamResult).where(
            ExamResult.exam_id == exam_id,
            ExamResult.student_id == student.id
        ))
        result = result.scalars().first()
        total_marks = result.marks_obtained if result and result.marks_obtained is not None else 0
        percentage = round((total_marks / exam.points_possible) * 100, 2) if exam.points_possible else 0
        # A result with an incomplete grading run does carry a running total, but
        # that total is partial. It is reported to the professor (progress is
        # useful) and flagged, never counted as a finished script.
        status = result.status if result else ExamResultStatus.PENDING
        is_final = status in ExamResultStatus.FINAL
        grading_failures = failures_by_student.get(student.id, [])
        students.append({
            "id": student.id,
            "email": student.email,
            "name": student.full_name,
            "roll": getattr(student, "entry_number", None),
            "total_marks": total_marks,
            "percentage": percentage,
            "status": status,
            "is_final": is_final,
            # Manager-only endpoint, so this is professor-facing detail. Codes
            # and short sentences only -- never the provider's raw output.
            "grading_failures": grading_failures,
            "failed_question_labels": [f["label"] for f in grading_failures],
        })
        if is_final:
            graded_scripts += 1
        total_answer_scripts += 1

    # The distribution is a claim about achieved scores, so a partial total must
    # not enter it -- it would understate the cohort and move the mean.
    # Half-mark buckets. `points_possible` is now NUMERIC, so the bucket COUNT
    # has to be derived rather than assumed to be an int -- and a total that
    # lands between buckets (2.25) is floored into the bucket it belongs to and
    # clamped, so a mark above the nominal maximum cannot index off the end.
    bucket_count = int(math.floor(float(exam.points_possible or 0) * 2)) + 1
    buckets = [0] * bucket_count
    excluded_from_distribution = 0
    for s in students:
        if not s["is_final"]:
            excluded_from_distribution += 1
            continue
        bucket = int(math.floor(float(s["total_marks"]) * 2))
        bucket = max(0, min(bucket, bucket_count - 1))
        buckets[bucket] += 1
    distribution = {
        "labels": [f"{i/2}" for i in range(bucket_count)],
        "data": buckets
    }
    grading_progress = graded_scripts / total_answer_scripts if total_answer_scripts else 0
    return JSONResponse({
        "students": students,
        "grading_progress": grading_progress,
        "marks_distribution": distribution,
        "excluded_from_distribution": excluded_from_distribution
    })

# ---------------------------------------------------------------------------
# 2. Students Performance (Calculated from the ExamResult table)
# ---------------------------------------------------------------------------
# @router.get("/exam/{exam_id}/students-performance")
# async def get_students_performance(
#     exam_id: int,
#     db: AsyncSession = Depends(get_db),
#     current_user: User = Depends(get_current_user_required)
# ):
#     exam = db.query(Exam).filter(Exam.id == exam_id).first()
#     if not exam:
#         raise HTTPException(status_code=404, detail="Exam not found")
#     enrollments = db.query(Enrollment).filter(
#         Enrollment.classroom_id == exam.classroom_id,
#         Enrollment.status == "accepted",
#         Enrollment.role == "student"
#     ).all()
#     performance = []
#     for enrollment in enrollments:
#         student = enrollment.student
#         result = db.query(ExamResult).filter(
#             ExamResult.exam_id == exam_id,
#             ExamResult.student_id == student.id
#         ).first()
#         total_marks = result.marks_obtained if result and result.marks_obtained is not None else 0
#         percentage = (total_marks / exam.points_possible * 100) if exam.points_possible else 0
#         performance.append({
#             "student_id": student.id,
#             "name": student.full_name,
#             "roll_number": getattr(student, "entry_number", None),
#             "total_marks": total_marks,
#             "percentage": percentage
#         })
#     return JSONResponse(performance)

@router.patch("/exams/{exam_id}/student/{student_id}/question/{question_id}/update")
async def edit_marks(
    exam_id: int,
    question_id: int,
    student_id: int,
    payload: dict,
    db: AsyncSession = Depends(get_db),
    ctx: ExamContext = Depends(require_question_in_exam),
    current_user: User = Depends(get_current_user_required)
):
    """
    Endpoint to manually edit the marks for a student's response to a question.
    Receives the new marks and optional reasoning from form data. After updating,
    it calls the internal function to update the overall exam result.
    """
    # The manual-edit control posts `marksInput.value`, i.e. a string such as
    # "1.5". Normalising here means a bad value is a 400 naming the field
    # instead of a driver error at flush time, and a fractional value survives.
    grade = parse_mark_input(payload.get("grade"), field="grade")
    # Ensure the professor is making the request
    if not current_user.is_professor:
        raise HTTPException(status_code=403, detail="Access denied")

    result = await db.execute(select(QuestionResponse).where(
        QuestionResponse.question_id == question_id,
        QuestionResponse.student_id == student_id
    ))
    response = result.scalars().first()
    
    if not response:
        raise HTTPException(status_code=404, detail="Response not found for this student and question.")
    
    response.marks_obtained = grade
    if grade is not None:
        # The professor has graded it by hand; whatever the provider failed at
        # is no longer true of this response.
        response.grading_error_code = None
    await db.commit()
    
    await add_exam_result_internal(exam_id, student_id, db) #, current_user)
    
    return {"message": "Marks updated successfully"}

# ---------------------------------------------------------------------------

# 3. Detailed Student Evaluation (Question-wise breakdown)

# ---------------------------------------------------------------------------

@router.get("/exam/{exam_id}/student-evaluation/{student_id}")
async def get_student_evaluation(
    exam_id: int,
    student_id: int,
    db: AsyncSession = Depends(get_db),
    ctx: ExamContext = Depends(require_self_or_exam_manager),
    current_user: User = Depends(get_current_user_required)
):
    result = await db.execute(select(Question).where(Question.exam_id == exam_id))
    questions = result.scalars().all()
    evaluation = []
    for q in questions:
        result = await db.execute(select(QuestionResponse).where(
            QuestionResponse.question_id == q.id,
            QuestionResponse.student_id == student_id
        ))
        response = result.scalars().first()
        entry = {
            "question_id": q.id,
            "question_number": q.question_number,
            "text": q.text[:50] + "..." if len(q.text) > 50 else q.text,
            "full_question_text": q.text,
            "student_response": response.answer_text if response else None,
            "reasoning": response.reasoning if response else None,
            "ideal_answer": q.ideal_answer,
            "marking_scheme": q.ideal_marking_scheme,
            "marks_obtained": response.marks_obtained if response else None,
            "max_marks": q.max_marks
        }
        # Why grading produced nothing is operational detail for whoever has to
        # fix it. This route is `require_self_or_exam_manager`, so a student can
        # legitimately reach their own row -- they get the marks, not the
        # diagnostics. Only a manager sees the failure fields at all.
        if ctx.is_manager:
            error_code = response.grading_error_code if response else None
            has_mark = response is not None and response.marks_obtained is not None
            entry["grading_error_code"] = None if has_mark else error_code
            entry["grading_error"] = (
                None if (has_mark or error_code is None) else describe(error_code)
            )
        evaluation.append(entry)
    return JSONResponse(evaluation)

# ---------------------------------------------------------------------------
# 4. Update Question or Student Response (Manual Edit)
# ---------------------------------------------------------------------------

@router.patch("/exams/{exam_id}/questions/{question_id}")
async def update_question(
    exam_id: int,
    question_id: int,
    update_data: dict,
    db: AsyncSession = Depends(get_db),
    ctx: ExamContext = Depends(require_question_in_exam),
    current_user: User = Depends(get_current_user_required)
):
    result = await db.execute(select(Question).where(
        Question.id == question_id,
        Question.exam_id == exam_id
    ))
    question = result.scalars().first()
    if not question:
        raise HTTPException(status_code=404, detail="Question not found.")
    if "text" in update_data:
        question.text = update_data["text"]
    if "ideal_answer" in update_data:
        question.ideal_answer = update_data["ideal_answer"]
    if "ideal_marking_scheme" in update_data:
        question.ideal_marking_scheme = update_data["ideal_marking_scheme"]
    await db.commit()
    await db.refresh(question)
    return JSONResponse({
        "success": True,
        "question": {
            "id": question.id,
            "text": question.text,
            "ideal_answer": question.ideal_answer,
            "ideal_marking_scheme": question.ideal_marking_scheme
        }
    })

@router.patch("/exam/{exam_id}/question/{question_id}/student/{student_id}/update")
async def update_student_response(
    exam_id: int,
    question_id: int,
    student_id: int,
    update_data: dict,
    db: AsyncSession = Depends(get_db),
    ctx: ExamContext = Depends(require_question_in_exam),
    current_user: User = Depends(get_current_user_required)
):
    result = await db.execute(select(QuestionResponse).where(
        QuestionResponse.question_id == question_id,
        QuestionResponse.student_id == student_id
    ))
    response = result.scalars().first()
    # Same normalisation as edit_marks: a JSON client may send 1.5, "1.5" or 1.
    marks_given = "marks_obtained" in update_data
    new_marks = parse_mark_input(update_data.get("marks_obtained"), field="marks_obtained")
    if not response:
        response = QuestionResponse(
            question_id=question_id,
            student_id=student_id,
            answer_text=update_data.get("response", ""),
            marks_obtained=new_marks,
            reasoning=update_data.get("reasoning", "")
        )
        db.add(response)
    else:
        response.answer_text = update_data.get("response", response.answer_text)
        response.marks_obtained = new_marks if marks_given else response.marks_obtained
    if response.marks_obtained is not None:
        response.grading_error_code = None
        response.reasoning = update_data.get("reasoning", response.reasoning)
    await db.commit()
    await add_exam_result_internal(exam_id, student_id, db) #, current_user)
    return {"message": "Updated successfully"}

# ---------------------------------------------------------------------------
# 5. Send for Re-evaluation (Reset marks, re-grade, and update ExamResult)
# ---------------------------------------------------------------------------
async def _reevaluate_one_response(
    *, exam_id: int, question: Question, student_id: int, response: QuestionResponse,
    db: AsyncSession, current_user: User,
) -> dict:
    """Re-grade ONE response without ever putting its existing mark at risk.

    THE BUG THIS REPLACES
    ---------------------
    All three re-evaluation routes opened with::

        previous_marks = response.marks_obtained
        response.marks_obtained = None
        response.reasoning = "Sent for re-evaluation"
        await db.commit()                      # <- mark already gone, on disk

    and only restored `previous_marks` if the grading call RETURNED a failure
    dict. Anything that RAISED in between -- and `extract_single_answer_text`
    raised `TypeError` on every automatically prepared response, because its
    `ans_text_images` is NULL -- escaped past the restore, leaving a correctly
    graded answer with `marks_obtained = NULL` and a 500 for the professor.
    Aggregation then read that NULL as "not graded" and un-finalised the exam.

    THE RULE NOW
    ------------
    Nothing is cleared up front. The previous mark, reason and failure code stay
    exactly as they were until a REPLACEMENT has been produced and validated,
    and are written back verbatim on every failure path -- returned failure or
    raised exception. So a re-evaluation can improve a mark or leave it alone;
    it can no longer destroy one.

    Grading semantics are unchanged: a genuine 0 is a valid replacement (the
    check is `status == "graded"`, never truthiness of the score), fractional
    marks pass through untouched, and a failure never writes a score.
    """
    previous_marks = response.marks_obtained
    previous_reasoning = response.reasoning
    previous_error_code = response.grading_error_code

    def _restore() -> None:
        """Put the row back exactly as it was found.

        The inner grading route persists on its own -- `_persist_and_report`
        writes a mark, `_record_grading_failure` writes a code -- so after a
        failed attempt the row may carry a stale code beside a still-valid
        mark. Restoring all three fields keeps the invariant that a validated
        mark and a failure code are mutually exclusive.
        """
        response.marks_obtained = previous_marks
        response.reasoning = previous_reasoning
        response.grading_error_code = previous_error_code

    try:
        # Auto-prepared responses have no crops; this is a no-op for them.
        await extract_single_answer_text({
            "exam_id": exam_id,
            "student_id": student_id,
            "question_id": question.id,
        }, db, current_user)

        result = await grade_question_with_diagram({
            "exam_id": exam_id,
            "student_id": student_id,
            "question_id": question.id,
            "ideal_answer": question.ideal_answer,
            "marking_scheme": question.ideal_marking_scheme,
        }, db, current_user)
    except HTTPException as exc:
        _restore()
        await db.commit()
        # `exc.detail` is this application's own text, never a provider body.
        logger.warning(
            "re-evaluation aborted, previous mark kept: question_id=%s student_id=%s status=%s",
            question.id, student_id, exc.status_code,
        )
        return {"status": "reevaluation_failed", "error_code": "reevaluation_unavailable",
                "grade": previous_marks}
    except Exception:
        _restore()
        await db.commit()
        # Logged with ids only; the traceback stays in the log, never in a body.
        logger.exception(
            "re-evaluation raised, previous mark kept: question_id=%s student_id=%s",
            question.id, student_id,
        )
        return {"status": "reevaluation_failed", "error_code": UNEXPECTED_ERROR,
                "grade": previous_marks}

    if result.get("status") == "graded":
        response.marks_obtained = result.get("grade")
        response.reasoning = result.get("reasoning")
        response.grading_error_code = None
        await db.commit()
        return {"status": "graded", "grade": response.marks_obtained}

    # A returned failure. The mark was never cleared, so there is nothing to
    # rescue -- only the stale state the inner route may have written.
    _restore()
    await db.commit()
    return {"status": "reevaluation_failed",
            "error_code": result.get("error_code"),
            "grade": previous_marks}


@router.post("/exam/{exam_id}/question/{question_id}/student/{student_id}/reevaluate")
async def send_for_reevaluation(
    exam_id: int,
    question_id: int,
    student_id: int,
    db: AsyncSession = Depends(get_db),
    ctx: ExamContext = Depends(require_question_in_exam),
    current_user: User = Depends(get_current_user_required)
):  
    result = await db.execute(select(QuestionResponse).where(
        QuestionResponse.question_id == question_id,
        QuestionResponse.student_id == student_id
    ))
    response = result.scalars().first()
    if not response:
        raise HTTPException(status_code=404, detail="Response not found")

    result = await db.execute(select(Question).where(Question.id == question_id))
    question = result.scalars().first()
    if not question:
        raise HTTPException(status_code=404, detail="Question not found")

    outcome = await _reevaluate_one_response(
        exam_id=exam_id, question=question, student_id=student_id,
        response=response, db=db, current_user=current_user,
    )
    await add_exam_result_internal(exam_id, student_id, db)

    if outcome["status"] == "graded":
        return {"message": "Re-evaluated and exam result updated", "status": "graded"}
    # A safe sentence derived from the code, never the provider's own text.
    return {
        "message": describe(outcome.get("error_code")) + " The previous mark was kept.",
        "status": "reevaluation_failed",
    }


@router.post("/exam/{exam_id}/student/{student_id}/reevaluate_all_questions")
async def send_all_for_reevaluation(
    exam_id: int,
    student_id: int,
    db: AsyncSession = Depends(get_db),
    ctx: ExamContext = Depends(require_exam_manager),
    current_user: User = Depends(get_current_user_required)
):
    # 1. fetch all questions for this exam
    questions_result = await db.execute(
        select(Question).where(Question.exam_id == exam_id)
    )
    questions = questions_result.scalars().all()
    if not questions:
        raise HTTPException(status_code=404, detail="No questions found for this exam")

    for question in questions:
        # 2a. fetch the student's response for this question
        resp_result = await db.execute(
            select(QuestionResponse).where(
                QuestionResponse.question_id == question.id,
                QuestionResponse.student_id == student_id
            )
        )
        response = resp_result.scalars().first()
        if not response:
            # skip or throw; here we choose to error out
            raise HTTPException(
                status_code=404,
                detail=f"Response not found for question {question.id}"
            )

        # Re-grade, never putting the existing mark at risk.
        await _reevaluate_one_response(
            exam_id=exam_id, question=question, student_id=student_id,
            response=response, db=db, current_user=current_user,
        )

    # 3. update the overall exam result once all questions are done
    await add_exam_result_internal(exam_id, student_id, db)

    return {"message": "All questions sent for re‑evaluation and exam result updated"}


@router.post("/exam/{exam_id}/question/{question_id}/reevaluate_all_students")
async def reevaluate_question_for_all_students(
    exam_id: int,
    question_id: int,
    db: AsyncSession = Depends(get_db),
    ctx: ExamContext = Depends(require_question_in_exam),
    current_user: User = Depends(get_current_user_required)
):
    # 1. Fetch the question and its exam
    q_result = await db.execute(select(Question).where(Question.id == question_id, Question.exam_id == exam_id))
    question = q_result.scalars().first()
    if not question:
        raise HTTPException(status_code=404, detail="Question not found")

    exam_result = await db.execute(select(Exam).where(Exam.id == exam_id))
    exam = exam_result.scalars().first()
    if not exam:
        raise HTTPException(status_code=404, detail="Exam not found")

    # 2. Get all enrolled students in the classroom
    enrollments_result = await db.execute(
        select(Enrollment).where(
            Enrollment.classroom_id == exam.classroom_id,
            Enrollment.role == "student",
            Enrollment.status == "accepted"
        )
    )
    enrollments = enrollments_result.scalars().all()

    if not enrollments:
        raise HTTPException(status_code=404, detail="No enrolled students found")

    # 3. Process each student's response
    for enrollment in enrollments:
        student_id = enrollment.student_id

        # 3a. Get their response for this question
        resp_result = await db.execute(
            select(QuestionResponse).where(
                QuestionResponse.question_id == question_id,
                QuestionResponse.student_id == student_id
            )
        )
        response = resp_result.scalars().first()
        if not response:
            continue  # Skip if no response exists

        # Re-grade, never putting the existing mark at risk.
        await _reevaluate_one_response(
            exam_id=exam_id, question=question, student_id=student_id,
            response=response, db=db, current_user=current_user,
        )

        # 3e. Update exam result for student
        await add_exam_result_internal(exam_id, student_id, db)

    return {"message": "Re-evaluated this question for all enrolled students"}

@router.get("/exam/{exam_id}/question-metrics")
async def get_question_metrics(
    exam_id: int,
    db: AsyncSession = Depends(get_db),
    ctx: ExamContext = Depends(require_exam_manager),
    current_user: User = Depends(get_current_user_required)
):
    result = await db.execute(select(Question).where(Question.exam_id == exam_id))
    questions = result.scalars().all()
    metrics = []
    for q in questions:
        result = await db.execute(select(QuestionResponse).where(QuestionResponse.question_id == q.id))
        responses = result.scalars().all()
        marks = [r.marks_obtained for r in responses if r.marks_obtained is not None]
        metrics.append({
            "question_id": q.id,
            "question_number": q.question_number,
            "text": q.text,
            "ideal_answer": q.ideal_answer,
            "max_marks": q.max_marks,
            "marks_distribution": marks
        })
    return JSONResponse(metrics)

# ---------------------------------------------------------------------------
# 7. Drop Question (Assign Zero Marks)
# ---------------------------------------------------------------------------
@router.post("/exam/{exam_id}/question/{question_id}/drop")
async def drop_question(
    exam_id: int,
    question_id: int,
    db: AsyncSession = Depends(get_db),
    ctx: ExamContext = Depends(require_question_in_exam),
    current_user: User = Depends(get_current_user_required)
):
    result = await db.execute(select(QuestionResponse).where(QuestionResponse.question_id == question_id))
    responses = result.scalars().all()
    for r in responses:
        r.marks_obtained = 0
        r.grading_error_code = None
        await add_exam_result_internal(exam_id, r.student_id, db) #, current_user)
        r.reasoning = "Question Dropped by professor"
    await db.commit()
    return {"message": "Question dropped"}

# ---------------------------------------------------------------------------
# 8. Award Full Marks (For Entire Class on a Question)
# ---------------------------------------------------------------------------
@router.post("/exam/{exam_id}/question/{question_id}/full-marks")
async def give_full_marks(
    exam_id: int,
    question_id: int,
    db: AsyncSession = Depends(get_db),
    ctx: ExamContext = Depends(require_question_in_exam),
    current_user: User = Depends(get_current_user_required)
):
    result = await db.execute(select(Question).where(Question.id == question_id))
    question = result.scalars().first()
    if not question:
        raise HTTPException(status_code=404, detail="Question not found")
    result = await db.execute(select(QuestionResponse).where(QuestionResponse.question_id == question_id))
    responses = result.scalars().all()
    for r in responses:
        r.marks_obtained = question.max_marks
        r.grading_error_code = None
        await add_exam_result_internal(exam_id, r.student_id, db) #, current_user)
        r.reasoning = "Full marks awarded by professor"
    await db.commit()
    return {"message": "Full marks awarded"}

# ---------------------------------------------------------------------------
# 9. Get Grading Status (Based on Answer Scripts vs. Graded Responses)
# ---------------------------------------------------------------------------
@router.get("/exam/{exam_id}/grading-status")
async def get_grading_status(
    exam_id: int,
    db: AsyncSession = Depends(get_db),
    ctx: ExamContext = Depends(require_exam_manager),
    current_user: User = Depends(get_current_user_required)
):
    from sqlalchemy import func
    result = await db.execute(select(func.count()).select_from(AnswerScript).where(AnswerScript.exam_id == exam_id))
    total = result.scalar()
    result = await db.execute(select(Question.id).where(Question.exam_id == exam_id))
    question_ids = [row[0] for row in result.fetchall()]
    result = await db.execute(select(func.count()).select_from(QuestionResponse).where(
        QuestionResponse.question_id.in_(question_ids),
        QuestionResponse.marks_obtained.isnot(None)
    ).distinct(QuestionResponse.student_id))
    graded = result.scalar()
    return {"total": total, "graded": graded}

# ---------------------------------------------------------------------------
# 10. Add Exam Result (Called after each answer script is graded)
# ---------------------------------------------------------------------------
@router.post("/exam/{exam_id}/add-result")
async def add_exam_result(
    exam_id: int,
    student_id: Optional[int] = Form(None),
    db: AsyncSession = Depends(get_db),
    ctx: ExamContext = Depends(require_exam_manager),
    current_user: User = Depends(get_current_user_required)
):
    student_id = student_id or current_user.id
    return await add_exam_result_internal(exam_id, student_id, db)#, current_user)

async def exam_result_is_final(exam_id: int, student_id: int, db: AsyncSession) -> bool:
    """Whether this student's result has been finalised, read from the row.

    `add_exam_result_internal` returns a `JSONResponse`, which a background job
    would have to decode to learn what it decided. Asking the persisted row
    instead keeps ONE authority for the answer: the status the aggregation
    actually wrote.
    """
    found = await db.execute(select(ExamResult).where(
        ExamResult.exam_id == exam_id,
        ExamResult.student_id == student_id,
    ))
    result = found.scalars().first()
    return result is not None and result.status in ExamResultStatus.FINAL


async def add_exam_result_internal(exam_id: int, student_id: int, db: AsyncSession):#, current_user: User):
    result = await db.execute(select(Exam).where(Exam.id == exam_id))
    exam = result.scalars().first()
    if not exam:
        raise HTTPException(status_code=404, detail="Exam not found")
    
    result = await db.execute(select(Question.id).where(Question.exam_id == exam_id))
    question_ids = [row[0] for row in result.fetchall()]
    result = await db.execute(select(QuestionResponse).where(
        QuestionResponse.student_id == student_id,
        QuestionResponse.question_id.in_(question_ids)
    ))
    responses = result.scalars().all()

    # A response whose grading produced no validated mark carries None. The old
    # sum filtered those out, which made a provider failure contribute exactly
    # zero while the result was still stamped "graded" (audit C6). Completeness
    # is now decided deterministically before anything is finalised.
    aggregation = aggregate_student_result(
        expected_question_ids=question_ids,
        responses=responses,
    )
    total_marks = aggregation.total_score

    result = await db.execute(select(ExamResult).where(
        ExamResult.exam_id == exam_id,
        ExamResult.student_id == student_id
    ))
    exam_result = result.scalars().first()
    previous_status = exam_result.status if exam_result else None

    if not aggregation.complete:
        log_incomplete(
            aggregation, exam_id=exam_id, student_id=student_id,
            previous_status=previous_status,
        )

    # The running total is still recorded so partial progress is visible, but
    # graded_at is set only on a final result: a timestamp means "this is the
    # student's grade", and an incomplete run has not earned one.
    graded_at = datetime.now(timezone.utc) if aggregation.complete else None

    if exam_result:
        exam_result.marks_obtained = total_marks
  #      exam_result.graded_by = current_user.id
        exam_result.graded_at = graded_at
        exam_result.status = aggregation.status
    else:
        exam_result = ExamResult(
            exam_id=exam_id,
            student_id=student_id,
            marks_obtained=total_marks,
   #         graded_by=current_user.id,
            graded_at=graded_at,
            status=aggregation.status
        )
        db.add(exam_result)
    
    await db.commit()
    await db.refresh(exam_result)

    # The RESULT and the exam-wide STAGE must not disagree. Until now only
    # `tasks._process_and_grade` wrote the Graded stage, so finalising through
    # this path left a genuinely final result beside an exam still reporting
    # "grading" -- the same two-records-disagree failure the conditional stage
    # in `tasks.py` was introduced to stop, reappearing on the direct route.
    #
    # Promotion is driven by the aggregation's own verdict, never by a caller:
    # an incomplete run can never reach Graded. The demotion is deliberately
    # narrow -- only an exam already marked Graded is moved back, so a paper
    # that has not reached grading yet is not dragged forward to stage 6.
    #
    # STILL EXAM-WIDE, and this job is per-student (see `tasks.py`): across
    # several students the stage reflects whoever ran last. Unchanged here.
    if aggregation.complete:
        await set_exam_stage(exam_id, EXAM_STAGE_GRADED, db)
    elif exam.exam_stage == EXAM_STAGE_GRADED:
        await set_exam_stage(exam_id, EXAM_STAGE_GRADING, db)

    return JSONResponse({
        "success": True,
        "result": {
            "student_id": student_id,
            "marks_obtained": total_marks,
            "status": aggregation.status,
            "complete": aggregation.complete,
            "is_final": aggregation.is_final,
            "graded_count": aggregation.graded_count,
            "ungraded_question_ids": aggregation.ungraded_question_ids,
            "graded_at": exam_result.graded_at.isoformat() if exam_result.graded_at else None
        }
    })

@router.get("/exams/{exam_id}/student/{student_id}/question/{question_id}/details")
async def get_student_question_details(
    exam_id: int,
    student_id: int,
    question_id: int,
    db: AsyncSession = Depends(get_db),
    ctx: ExamContext = Depends(require_question_access_for_student),
    current_user: User = Depends(get_current_user_required)
):
    result = await db.execute(select(Exam).where(Exam.id == exam_id))
    exam = result.scalars().first()
    if not exam:
        raise HTTPException(status_code=404, detail="Exam not found.")

    result = await db.execute(select(Question).where(
        Question.id == question_id,
        Question.exam_id == exam_id
    ))
    question = result.scalars().first()
    if not question:
        raise HTTPException(status_code=404, detail="Question not found in this exam.")

    result = await db.execute(select(QuestionResponse).where(
        QuestionResponse.student_id == student_id,
        QuestionResponse.question_id == question_id
    ))
    response = result.scalars().first()
    if not response:
        raise HTTPException(status_code=404, detail="Response not found for this student and question.")

    return {
        "grade": response.marks_obtained,
        "response": response.answer_text
    }


@router.get("/exams/{exam_id}/submission_status")
async def get_student_submission_status(
    exam_id: int,
    db: AsyncSession = Depends(get_db),
    # Participant, not manager: this endpoint returns ONLY the caller's own row
    # (student_id == current_user.id), and its sole caller is the student-facing
    # edit page. Security Foundation v1 gated it on require_exam_manager, which
    # made it 403 for exactly the students it exists to serve.
    ctx: ExamContext = Depends(require_exam_participant),
    current_user: User = Depends(get_current_user_required),
):
    q = select(ExamResult.status).where(
        ExamResult.exam_id    == exam_id,
        ExamResult.student_id == current_user.id
    )
    result = await db.execute(q)
    status: str | None = result.scalar_one_or_none()

    # Whether this student's script has been prepared into question responses.
    # `status` alone cannot say: it is PENDING both for a script nobody has
    # touched and for one that is submitted and waiting to be graded, and since
    # grading is started by the instructor those two are now days apart. Without
    # this the student page can only offer the preparation step again, which is
    # what made preparation look like the product.
    prepared_rows = await db.execute(
        select(func.count())
        .select_from(QuestionResponse)
        .join(Question, Question.id == QuestionResponse.question_id)
        .where(
            Question.exam_id == exam_id,
            QuestionResponse.student_id == current_user.id,
        )
    )
    prepared = bool(prepared_rows.scalar())

    if status is None:
        # no submission record yet
        return {
            "status": ExamResultStatus.PENDING,
            "is_final": False,
            "prepared": prepared,
        }

    # is_final is derived, so a caller never has to hardcode which status
    # strings mean "this is really the student's grade".
    return {
        "status": status,
        "is_final": status in ExamResultStatus.FINAL,
        "prepared": prepared,
    }