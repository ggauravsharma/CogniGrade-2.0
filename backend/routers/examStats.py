from typing import Optional, List, Dict
from fastapi import APIRouter, Depends, HTTPException, Form
from fastapi.responses import JSONResponse
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
from backend.grading.failure import GradingFailure, describe
from backend.auth.policies import (
    ExamContext,
    require_exam_manager,
    require_exam_participant,
    require_question_access_for_student,
    require_question_in_exam,
    require_self_or_exam_manager,
)
from backend.routers.geminiAPI import grade_question, grade_question_with_diagram, extract_single_answer_text
import math
import re

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
@router.post("/exam/{exam_id}/question/{question_id}/student/{student_id}/reevaluate")
async def send_for_reevaluation(
    exam_id: int,
    question_id: int,
    student_id: int,
    db: AsyncSession = Depends(get_db),
    ctx: ExamContext = Depends(require_question_in_exam),
    current_user: User = Depends(get_current_user_required)
):  
    print("CALLED")
    result = await db.execute(select(QuestionResponse).where(
        QuestionResponse.question_id == question_id,
        QuestionResponse.student_id == student_id
    ))
    response = result.scalars().first()
    if not response:
        raise HTTPException(status_code=404, detail="Response not found")
    previous_marks = response.marks_obtained
    response.marks_obtained = None
    response.reasoning = "Sent for re-evaluation"
    await db.commit()
    result = await db.execute(select(Question).where(Question.id == question_id))
    question = result.scalars().first()
    await extract_single_answer_text({
        "exam_id": exam_id,
        "student_id": student_id,
        "question_id": question_id,
    }, db, current_user)
    result = await grade_question_with_diagram({
        "exam_id": exam_id,
        "student_id": student_id,
        "question_id": question_id,
        "ideal_answer": question.ideal_answer,
        "marking_scheme": question.ideal_marking_scheme
    }, db, current_user)
    if result.get("status") == "graded":
        response.marks_obtained = result.get("grade")
        response.reasoning = result.get("reasoning")
        response.grading_error_code = None
    else:
        # Provider failure: restore the mark this route nulled before
        # re-grading, so a failed re-evaluation is non-destructive rather than
        # leaving a NULL that aggregation would later count as zero.
        response.marks_obtained = previous_marks
        response.reasoning = (
            f"Re-evaluation failed to produce a valid grade "
            f"({result.get('error_code', 'unknown')}). Previous marks restored."
        )
    await db.commit()
    await add_exam_result_internal(exam_id, student_id, db) #, current_user)
    return {"message": "Sent for re-evaluation and exam result updated"}


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

        # 2b. mark it as pending re‑evaluation
        previous_marks = response.marks_obtained
        response.marks_obtained = None
        response.reasoning = "Sent for re-evaluation"
        await db.commit()

        # 2c. re‑extract answer text
        await extract_single_answer_text({
            "exam_id": exam_id,
            "student_id": student_id,
            "question_id": question.id,
        }, db, current_user)

        # 2d. re‑grade with diagram support
        grade = await grade_question_with_diagram({
            "exam_id": exam_id,
            "student_id": student_id,
            "question_id": question.id,
            "ideal_answer": question.ideal_answer,
            "marking_scheme": question.ideal_marking_scheme
        }, db, current_user)

        # 2e. update with the new grade, only if the provider produced one
        if grade.get("status") == "graded":
            response.marks_obtained = grade.get("grade")
            response.reasoning = grade.get("reasoning")
            response.grading_error_code = None
        else:
            response.marks_obtained = previous_marks
            response.reasoning = (
                f"Re-evaluation failed to produce a valid grade "
                f"({grade.get('error_code', 'unknown')}). Previous marks restored."
            )
        await db.commit()

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

        # 3b. Mark for re-evaluation
        previous_marks = response.marks_obtained
        response.marks_obtained = None
        response.reasoning = "Sent for re-evaluation"
        await db.commit()

        # 3c. Re-extract and grade
        await extract_single_answer_text({
            "exam_id": exam_id,
            "student_id": student_id,
            "question_id": question_id,
        }, db, current_user)

        grade = await grade_question_with_diagram({
            "exam_id": exam_id,
            "student_id": student_id,
            "question_id": question_id,
            "ideal_answer": question.ideal_answer,
            "marking_scheme": question.ideal_marking_scheme
        }, db, current_user)

        # 3d. Save results, only if the provider produced a valid grade
        if grade.get("status") == "graded":
            response.marks_obtained = grade.get("grade")
            response.reasoning = grade.get("reasoning")
            response.grading_error_code = None
        else:
            response.marks_obtained = previous_marks
            response.reasoning = (
                f"Re-evaluation failed to produce a valid grade "
                f"({grade.get('error_code', 'unknown')}). Previous marks restored."
            )
        await db.commit()

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

    if status is None:
        # no submission record yet
        return {"status": ExamResultStatus.PENDING, "is_final": False}

    # is_final is derived, so a caller never has to hardcode which status
    # strings mean "this is really the student's grade".
    return {"status": status, "is_final": status in ExamResultStatus.FINAL}