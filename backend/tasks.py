from celery import Celery
from sqlalchemy.ext.asyncio import AsyncSession
from asyncio import get_event_loop
from backend.database import AsyncSessionLocal
from backend.models.users import User
from backend.routers.geminiAPI import process_answer_text_images_logic, grade_exam_logic
from backend.routers.examStats import add_exam_result_internal, exam_result_is_final
from backend.routers.exams import EXAM_STAGE_GRADED, EXAM_STAGE_GRADING, set_exam_stage
import os
from dotenv import load_dotenv
load_dotenv()

# Use environment variable for broker URL, with a fallback for non-Docker local dev
broker_url = os.getenv("CELERY_BROKER_URL", "amqp://guest:guest@localhost:5672//")
celery_app = Celery(
    "tasks",
    broker=broker_url,
    backend="rpc://"  # Result backend, adjust if using a different one like Redis
)

@celery_app.task
def process_and_grade_exam(exam_id: int, student_id: int):
    """
    Celery task to process text images and grade the exam for a specific student.
    Runs asynchronously in the background.
    """
    loop = get_event_loop()
    loop.run_until_complete(_process_and_grade(exam_id, student_id))

async def _process_and_grade(exam_id: int, student_id: int):
    """Recognise, grade, aggregate, then record how far the exam actually got.

    THE FINAL STAGE IS CONDITIONAL, and that is the point. This used to end in
    an unconditional `set_exam_stage(exam_id, 7, db)` -- stage 7 being "Graded"
    in the vocabulary on `Exam.exam_stage` -- which ran even when aggregation
    had just written `grading_incomplete` with no `graded_at`. Two persisted
    records of the same fact then disagreed: the result said the paper was not
    finished and the exam said it was. That is the C6 failure mode one layer
    up, so the stage is now taken from the aggregation's own verdict.

    An incomplete run lands on GRADING rather than being left untouched: the
    paper genuinely reached grading and is waiting to be re-run, and re-running
    is self-healing (a valid mark clears the failure code that described its
    absence).

    NOT fixed here: `exam_stage` is EXAM-wide while this job is PER-student, so
    across many students the stage still reflects whoever ran last. A correct
    exam-wide signal needs an all-students completion query, which is a product
    decision rather than a bug fix.
    """
    async with AsyncSessionLocal() as db:
        await process_answer_text_images_logic(exam_id, student_id, db)
        await grade_exam_logic(exam_id, student_id, db)
        await add_exam_result_internal(exam_id, student_id, db)
        final = await exam_result_is_final(exam_id, student_id, db)
        await set_exam_stage(
            exam_id, EXAM_STAGE_GRADED if final else EXAM_STAGE_GRADING, db
        )