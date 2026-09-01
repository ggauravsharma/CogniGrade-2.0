import os
import uuid
from typing import List, Optional
import logging
import json
import re
import time
from types import SimpleNamespace
import aiofiles  # Added for async file operations

from fastapi import APIRouter, File, UploadFile, Depends, HTTPException, Form
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from backend.database import get_db
from backend.models.files import AnswerScript, Material, FileTypeEnum
from backend.models.users import User
from backend.utils.security import get_current_user_required
from backend.models.tables import QuestionResponse, Question
from backend.ai import services as ai_services
from backend.ai.concurrency import run_bounded
from backend.ai.config import get_task_settings
from backend.ai.contracts import AITask, FilePart, TextPart
from backend.ai.errors import ProviderError
from backend.ai.prompts.grading import (
    REFERENCE_IMAGE_HEADING,
    STUDENT_IMAGE_HEADING,
)
from backend.grading.region_evidence import build_evidence as build_region_aware_evidence
from backend.regions.cropping import CropWorkspace, RegionEvidenceError
from backend.grading.failure import UNEXPECTED_ERROR
from backend.grading.result import GradingResponseError, output_instruction
from backend.auth.policies import (
    ExamContext,
    assert_exam_manager,
    require_exam_manager,
)

# The Gemini SDK, the API keys and the model objects now live in
# backend/ai/providers/gemini.py. What stood here was:
#
#   * a module-global `genai.configure()` loop whose "rotation" was a no-op --
#     configure() sets a MODULE-GLOBAL key, so every model in the list used
#     whichever key was configured LAST;
#   * a global mutable `call_count` guarded by a threading lock AND an asyncio
#     lock, which is exactly the shared request state that would have to be
#     unpicked before grading could run concurrently;
#   * a hardcoded model name, so no task could be pointed at a different model.
#
# This module now asks `backend.ai.services` for a task and gets a domain value
# back. It never sees a model, a key or a provider file handle.

UPLOAD_DIRECTORY = "./uploads"
os.makedirs(UPLOAD_DIRECTORY, exist_ok=True)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["gemini-api"])

def extract_leaves(labels: List[str]) -> List[str]:
    """
    Given a flat list of hierarchical labels (e.g. ["1", "1.1", "1.1.a", ...]),
    return only those labels without children in the list.
    """
    leaves = []
    for lbl in labels:
        if '.' not in lbl or not any(other != lbl and other.startswith(lbl + '.') for other in labels):
            leaves.append(lbl)
    return leaves

@router.post("/extract-text")
async def upload_and_extract(
    files: List[UploadFile] = File(...),
    exam_id: int = Form(...),
    file_type: str = Form(...),
    student_id: Optional[int] = Form(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required)
):
    results = []
    # AUTHORIZATION: exam_id is a form field, so this is an imperative check.
    # Uploading and extracting exam documents is a manager-only action.
    await assert_exam_manager(exam_id, current_user, db)
    try:
        file_type_enum = FileTypeEnum(file_type)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid file type provided.")
    for file in files:
        try:
            if file_type_enum in [FileTypeEnum.question_paper, FileTypeEnum.solution_script, FileTypeEnum.marking_scheme]:
                result = await db.execute(select(Material).where(
                    Material.title == file.filename,
                    Material.related_exam_id == exam_id,
                    Material.file_type == file_type_enum
                ))
                existing = result.scalars().first()
            elif file_type_enum == FileTypeEnum.answer_sheet:
                if not student_id:
                    raise HTTPException(status_code=400, detail="student_id is required for answer_sheet.")
                result = await db.execute(select(AnswerScript).where(
                    AnswerScript.title == file.filename,
                    AnswerScript.exam_id == exam_id,
                    AnswerScript.student_id == student_id
                ))
                existing = result.scalars().first()
            else:
                existing = None

            # if existing and existing.extracted_text:
            #     results.append({"filename": file.filename, "text": existing.extracted_text})
            #     continue

            result = await db.execute(select(Question).where(Question.exam_id == exam_id))
            questions = result.scalars().all()
            if not questions:
                raise HTTPException(status_code=404, detail="Exam not found or has no questions")

            all_labels: List[str] = []
            for q in questions:
                raw = q.part_labels or ""
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, list):
                        labels = [str(item).strip() for item in parsed]
                    else:
                        raise ValueError
                except (ValueError, json.JSONDecodeError):
                    labels = [item.strip() for item in raw.split(',') if item.strip()]
                all_labels.append(str(q.question_number))
                all_labels.extend(labels)

            leaf_labels = extract_leaves(all_labels)
            leaf_labels.sort(key=lambda x: [int(part) if part.isdigit() else part.lower() for part in x.split('.')])

            file_id = str(uuid.uuid4())
            file_location = os.path.join(UPLOAD_DIRECTORY, f"{file_id}_{file.filename}")
            async with aiofiles.open(file_location, "wb") as f:
                content = await file.read()
                await f.write(content)

            logger.info(f"Uploading {file.filename} to Gemini...")

            print(leaf_labels)

            extracted_text = await ai_services.extract_document_text(
                file_location, leaf_labels=leaf_labels, exam_id=exam_id
            ) or "No text extracted."
            
            if existing:
                existing.extracted_text = extracted_text
                await db.commit()
                results.append({"filename": file.filename, "text": extracted_text})
            else:
                if file_type_enum in [FileTypeEnum.question_paper, FileTypeEnum.solution_script, FileTypeEnum.marking_scheme]:
                    new_material = Material(
                        title=file.filename,
                        description="",
                        file_path=file_location,
                        file_size=int(round(file.size, 0)),
                        link_url=None,
                        related_exam_id=exam_id,
                        author_id=current_user.id,
                        extracted_text=extracted_text,
                        file_type=file_type_enum
                    )
                    db.add(new_material)
                    await db.commit()
                    await db.refresh(new_material)
                    results.append({"filename": file.filename, "text": extracted_text})
                elif file_type_enum == FileTypeEnum.answer_sheet:
                    new_answer = AnswerScript(
                        title=file.filename,
                        file_path=file_location,
                        file_size=int(round(file.size, 0)),
                        exam_id=exam_id,
                        student_id=student_id,
                        extracted_text=extracted_text
                    )
                    db.add(new_answer)
                    await db.commit()
                    await db.refresh(new_answer)
                    results.append({"filename": file.filename, "text": extracted_text})
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error processing file {file.filename}: {str(e)}", exc_info=True)
            results.append({"filename": file.filename, "error": str(e)})
    
    return JSONResponse({"results": results})

@router.post("/extract-question-labels")
async def extract_question_labels(
    files: List[UploadFile] = File(...),
    exam_id: int = Form(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    """
    Extract hierarchical question labels from uploaded question papers,
    build full prefix hierarchy, and insert into Questions.part_labels.
    """
    results = []
    # AUTHORIZATION: exam_id is a form field; label extraction rewrites the
    # exam's question structure and is manager-only.
    await assert_exam_manager(exam_id, current_user, db)
    for file in files:
        fid = str(uuid.uuid4())
        file_path = os.path.join(UPLOAD_DIRECTORY, f"{fid}_{file.filename}")
        async with aiofiles.open(file_path, "wb") as f:
            content = await file.read()
            await f.write(content)


        text = (await ai_services.extract_question_labels(file_path, exam_id=exam_id)).strip()

        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        raw_labels = []
        marks_dict = {}

        for ln in lines:
            if "Max Marks - " in ln:
                label_part, marks_part = ln.split("Max Marks -", 1)
                label = label_part.strip().rstrip("-").strip()
                try:
                    marks = int(marks_part.strip())
                except ValueError:
                    marks = 0
                # only store marks for truly top-level (no dot in label)
                if "." not in label:
                    marks_dict[label] = marks
                raw_labels.append(label)
            else:
                raw_labels.append(ln)

        full_labels = set()
        top_questions = set()

        for lbl in raw_labels:
            parts = lbl.split(".")
            if parts and len(parts[0]) < 3:
                top_questions.add(parts[0])
                for i in range(1, len(parts)):
                    prefix = ".".join(parts[: i + 1])
                    full_labels.add(prefix)

        def sort_key(s):
            return [int(p) if p.isdigit() else p for p in s.split(".")]

        ordered = sorted(full_labels, key=sort_key)
        top_questions = sorted(top_questions, key=lambda x: int(x))

        for qnum in top_questions:
            q_labels = [lbl for lbl in ordered if lbl.split(".")[0] == qnum]
            part_labels_json = json.dumps(q_labels)
            max_marks = marks_dict.get(qnum, 0)

            q = Question(
                exam_id=exam_id,
                question_number=int(qnum),
                text="",
                ideal_answer=None,
                ideal_marking_scheme=None,
                max_marks=max_marks,
                part_labels=part_labels_json,
            )
            db.add(q)
            await db.commit()
            await db.refresh(q)

            results.append({
                "question_number": q.question_number,
                "max_marks": q.max_marks,
                "part_labels": q.part_labels
            })

    return {"results": results}

# @router.post("/extract-question-labels")
# async def extract_question_labels(
#     files: List[UploadFile] = File(...),
#     exam_id: int = Form(...),
#     db: AsyncSession = Depends(get_db),
#     current_user: User = Depends(get_current_user_required),
# ):
#     """
#     Extract hierarchical question labels from uploaded question papers,
#     build full prefix hierarchy, and insert into Questions.part_labels.
#     """
#     results = []
#     for file in files:
#         # 2. Save upload locally
#         fid = str(uuid.uuid4())
#         file_path = os.path.join(UPLOAD_DIRECTORY, f"{fid}_{file.filename}")
#         with open(file_path, "wb") as f:
#             f.write(await file.read())

#         # 3. Upload to Gemini
#         ai_file = genai.upload_file(file_path, display_name=file.filename)

#         # 4. Send extraction prompt
#         prompt = """
# Extract the question labels from the given question paper in the form Question_Number.Part.Subpart, 
# that is, the parent questions are separated from their children by points '.'. 
# There may be multiple levels of hierarchy also.
# """
#         resp = get_model().generate_content((ai_file, prompt))
#         text = resp.text.strip() or ""
#         # Expect each label on its own line; split and clean
#         raw_labels = [line.strip() for line in text.splitlines() if line.strip()]

#         # 5. Build full-hierarchy list
#         full_labels = set()
#         top_questions = set()
#         for lbl in raw_labels:
#             if lbl[-1] == '.':
#                 lbl = lbl[:-1]
#             parts = lbl.split(".")
#             if len(parts[0]) < 3:
#                 top_questions.add(parts[0])
#                 # accumulate prefixes: e.g. ["4","1","a"] → "4.1", "4.1.a"
#                 for i in range(1, len(parts)):
#                     prefix = ".".join(parts[: i + 1])
#                     full_labels.add(prefix)
#         # sort by numerical ordering
#         def sort_key(s):
#             return [int(p) if p.isdigit() else p for p in s.split(".")]
#         ordered = sorted(full_labels, key=sort_key)
#         # print(ordered)
#         # 6. Determine top-level question numbers (e.g. 4 from "4.1.a")
#         top_questions = sorted(top_questions)

#         for qnum in top_questions:
#             # 7. Filter only labels under this question
#             q_labels = [lbl for lbl in ordered if lbl.split(".")[0] == qnum]

#             part_labels_json = json.dumps(q_labels)

#             # 8. Insert Question row
#             q = Question(
#                 exam_id=exam_id,
#                 question_number=qnum,
#                 text="",               # you may wish to fill this separately
#                 ideal_answer=None,
#                 ideal_marking_scheme=None,
#                 max_marks=0,           # default; adjust as needed
#                 part_labels=part_labels_json,
#             )
#             db.add(q)
#             await db.commit()
#             await db.refresh(q)
#             results.append({
#                 "question_number": q.question_number,
#                 "part_labels": q.part_labels
#             })

#     return {"results": results}

# ---------------------------------------------------------------------------
# grading provider boundary
# ---------------------------------------------------------------------------
#
# Everything Gemini-specific about producing a grade lives in these two
# helpers. They turn a provider response into a provider-neutral GradingResult
# (or raise GradingResponseError). No caller re-implements parsing, so the two
# grading routes and re-evaluation cannot drift apart again.

async def _persist_and_report(
    *, db, question_id, student_id, exam_id, result, raw_text
):
    """Write a VALIDATED result, then report it in the route's dict shape.

    Only reached when a GradingResult exists, so a malformed provider response
    can never overwrite a student's marks.
    """
    if question_id and student_id and exam_id:
        found = await db.execute(select(QuestionResponse).where(
            QuestionResponse.question_id == question_id,
            QuestionResponse.student_id == student_id
        ))
        existing_response = found.scalars().first()
        if existing_response:
            existing_response.marks_obtained = result.score
            existing_response.reasoning = result.reason
            # A valid mark and a failure code are mutually exclusive by
            # construction: clearing it in the same transaction as the write is
            # what makes a retry self-healing, and is why no stale failure can
            # outlive the failure it described.
            existing_response.grading_error_code = None
            await db.commit()
    return {
        "status": "graded",
        "grade": result.score,
        "reasoning": result.reason,
        "raw_response": raw_text,
    }


async def _record_grading_failure(db, *, question_id, student_id, error_code):
    """Persist WHY this response has no mark, so a professor can be told.

    Best effort by design: the caller is already on a failure path, and losing
    the diagnostic must never turn a reported grading failure into a 500. The
    mark itself is untouched -- a failure never writes a score, and in
    particular never writes a zero (audit C6/D).
    """
    if not (question_id and student_id):
        return
    try:
        found = await db.execute(select(QuestionResponse).where(
            QuestionResponse.question_id == question_id,
            QuestionResponse.student_id == student_id
        ))
        response = found.scalars().first()
        if response is not None:
            response.grading_error_code = error_code
            await db.commit()
    except Exception:
        logger.exception(
            "could not record grading failure code for question_id=%s student_id=%s",
            question_id, student_id,
        )


def _grading_failure(exc: GradingResponseError, *, question_id, student_id):
    """The explicit failure shape returned to callers.

    `grade` is None AND `status` is "grading_failed", so a caller that checks
    status cannot mistake a provider failure for a zero. Callers that write
    marks must check status; see examStats re-evaluation.
    """
    logger.error(
        "grading failed: code=%s question_id=%s student_id=%s detail=%s",
        exc.code, question_id, student_id, exc.message,
    )
    return {
        "status": "grading_failed",
        "grade": None,
        "reasoning": None,
        "error_code": exc.code,
        "error": exc.message,
        "raw_response": exc.raw,
    }


@router.post("/grade-question")
async def grade_question(
    request: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required)
):
    try:
        student_answer = request.get("student_answer")
        ideal_answer = request.get("ideal_answer")
        marking_scheme = request.get("marking_scheme")
        exam_id = request.get("exam_id")
        student_id = request.get("student_id")
        question_id = request.get("question_id")

        # AUTHORIZATION: exam_id arrives in the request body, so this cannot
        # be a path-parameter dependency. Grading is a manager-only action.
        if current_user is not None:
            await assert_exam_manager(exam_id, current_user, db)

        if not student_answer or (not ideal_answer and not marking_scheme):
            raise HTTPException(status_code=400, detail="Missing required parameters. Provide student_answer and at least one of ideal_answer or marking_scheme.")
        
        result = await db.execute(select(Question).where(
            Question.id == question_id,
            Question.exam_id == exam_id
        ))
        question = result.scalars().first()
        if not question:
            raise HTTPException(status_code=404, detail="Question not found.")
        
        # The prompt itself is built by backend/ai/prompts/grading.py; three
        # near-identical copies used to be assembled here inline.
        try:
            result, raw_text = await ai_services.grade_answer(
                question_text=question.text,
                student_answer=student_answer,
                max_marks=question.max_marks,
                marking_scheme=marking_scheme,
                ideal_answer=ideal_answer,
                exam_id=exam_id,
                student_id=student_id,
                question_id=question_id,
            )
        except ProviderError as exc:
            # The call itself failed -- transport, quota, timeout. That is not
            # a validation failure and emphatically not a zero: record the
            # provider-neutral category as the failure code and report it in
            # the same explicit shape.
            await _record_grading_failure(
                db, question_id=question_id, student_id=student_id,
                error_code=exc.category,
            )
            logger.error(
                "grading call failed: category=%s question_id=%s student_id=%s",
                exc.category, question_id, student_id,
            )
            return {
                "status": "grading_failed",
                "grade": None,
                "reasoning": None,
                "error_code": exc.category,
                "error": "the grading service could not be reached",
                "raw_response": None,
            }
        except GradingResponseError as exc:
            # An unparseable provider response is a FAILURE, never a zero.
            await _record_grading_failure(
                db, question_id=question_id, student_id=student_id, error_code=exc.code
            )
            return _grading_failure(exc, question_id=question_id, student_id=student_id)

        return await _persist_and_report(
            db=db, question_id=question_id, student_id=student_id, exam_id=exam_id,
            result=result, raw_text=raw_text,
        )
        
    except HTTPException:
        # A deliberate 403/404 raised above (question missing, caller not a
        # manager) must keep its status instead of being reported as a server
        # fault by the handler below.
        raise
    except Exception as e:
        logger.error(f"Error in grade_question: {str(e)}", exc_info=True)
        # The exam-wide run must be able to say WHICH question died, so record
        # a generic code before propagating. The detail stays in the log.
        await _record_grading_failure(
            db,
            question_id=request.get("question_id"),
            student_id=request.get("student_id"),
            error_code=UNEXPECTED_ERROR,
        )
        raise HTTPException(status_code=500, detail=f"Error grading question: {str(e)}")

def _build_diagram_prompt_parts(question, evidence, *, marking_scheme, ideal_answer):
        """Assemble the ordered prompt for a diagram/table grading call.

        PURE: no database, no provider, no network. Extracted from the route
        body so the exam-wide run can build every question's prompt up front
        on one session and then run the provider calls concurrently with no
        ORM object in sight. That separation is what makes bounded
        concurrency safe here -- see `grade_exam_logic`.

        The body is indented to EIGHT spaces, not four, and that is
        deliberate: the prompt literals below are multi-line f-strings whose
        continuation lines are part of the string. Re-indenting them would
        silently change the text sent to the model, so the block was moved
        verbatim rather than reformatted.
        """
        student_answer = evidence.student_answer_text

        # Two independent LISTS OF PATHS. They are never combined, and the
        # route no longer holds a provider file handle at all -- uploading,
        # deduplicating and deleting them is the adapter's job, which is what
        # finally gives the uploads a guaranteed cleanup path.
        reference_files = [FilePart(p) for p in evidence.reference_images.all_paths if p]
        student_files = [FilePart(p) for p in evidence.student_images.all_paths if p]

        # Labels are attached only when files are present, so an absent
        # marking-scheme image costs no extra tokens and, critically, is never
        # back-filled with the student's own image.
        reference_parts = (
            [TextPart(REFERENCE_IMAGE_HEADING)] + reference_files
            if reference_files else []
        )
        student_parts = (
            [TextPart(STUDENT_IMAGE_HEADING)] + student_files
            if student_files else []
        )
        # Presence and wording now come from the evidence structure itself,
        # so the description of what is attached can never disagree with the
        # files actually attached. The previous helper initialised
        # image_present=True and derived both sides from separate variables.
        ms_image_present = evidence.reference_images.has_any
        ans_image_present = evidence.student_images.has_any
        ms_attached = evidence.reference_images.descriptor()
        ans_attached = evidence.student_images.descriptor()

        if (marking_scheme or ms_image_present) and ideal_answer:
            prompt_content = [TextPart(f"""Question: {question.text}

This is the correct marking scheme: {f'{marking_scheme}, with' if marking_scheme else "look at"} {f" the attached {ms_attached}" if ms_image_present else ""}""")] + reference_parts + [TextPart(f"""

Ideal Answer: {ideal_answer}

Grade the following student answer: {f'{student_answer}, with' if student_answer else "look at"} {f" the attached {ans_attached}" if ans_image_present else ""}""")] + student_parts + [TextPart(f"""

If the marking scheme doesn't specify mark distribution, grade proportionally based on the level of correctness—giving higher marks for more accurate and complete answers, and lower marks for partially correct or incomplete ones. Don't be too strict, nor too lenient.

Maximum Marks Possible: {question.max_marks}.
{output_instruction(question.max_marks)}""")]
        elif (marking_scheme or ms_image_present):
            prompt_content = [TextPart(f"""Question: {question.text}

This is the correct marking scheme: {f'{marking_scheme}, with' if marking_scheme else "look at"} {f" the attached {ms_attached}" if ms_image_present else ""}""")] + reference_parts + [TextPart(f"""

Grade the following student answer: {f'{student_answer}, with' if student_answer else "look at"} {f" the attached {ans_attached}" if ans_image_present else ""}""")] + student_parts + [TextPart(f"""

If the marking scheme doesn't specify mark distribution, grade proportionally based on the level of correctness—giving higher marks for more accurate and complete answers, and lower marks for partially correct or incomplete ones. Don't be too strict, nor too lenient.
                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      
Maximum Marks Possible: {question.max_marks}.
{output_instruction(question.max_marks)}""")]
        elif ideal_answer:
            prompt_content = [TextPart(f"""Question: {question.text}

Ideal Answer: {ideal_answer}

Grade the following student answer: {f'{student_answer}, with' if student_answer else "look at"} {f" the attached {ans_attached}" if ans_image_present else ""}""")] + student_parts + [TextPart(f"""

Give marks proportionally based on the level of correctness—giving higher marks for more accurate and complete answers, and lower marks for partially correct or incomplete ones. Don't be too strict, nor too lenient.

Maximum Marks Possible: {question.max_marks}.
{output_instruction(question.max_marks)}""")]
        else:
            prompt_content = [TextPart(f"""Question: {question.text}

Grade the following student answer: {f'{student_answer}, with' if student_answer else "look at"} {f" the attached {ans_attached}" if ans_image_present else ""}""")] + student_parts + [TextPart(f"""

Give marks proportionally based on the level of correctness—giving higher marks for more accurate and complete answers, and lower marks for partially correct or incomplete ones. Don't be too strict, nor too lenient.
                                                                                                                                                                                                                                                                                                                                                 
Maximum Marks Possible: {question.max_marks}.

{output_instruction(question.max_marks)}""")]

        return prompt_content


@router.post("/grade-question-with-diagram")
async def grade_question_with_diagram(
    request: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required)
):
    print("diagram grade")
    try:
        ideal_answer = request.get("ideal_answer")
        marking_scheme = request.get("marking_scheme")
        exam_id = request.get("exam_id")
        student_id = request.get("student_id")
        question_id = request.get("question_id")

        # AUTHORIZATION: exam_id arrives in the request body, so this cannot
        # be a path-parameter dependency. Grading is a manager-only action.
        if current_user is not None:
            await assert_exam_manager(exam_id, current_user, db)

        result = await db.execute(select(QuestionResponse).where(
            QuestionResponse.question_id == question_id,
            QuestionResponse.student_id == student_id
        ))
        qr = result.scalars().first()

        # The question must be resolved BEFORE any upload work. The previous
        # order uploaded images first and only then discovered the question was
        # missing, and it dereferenced question.ms_* before the None check.
        result = await db.execute(select(Question).where(
            Question.id == question_id,
            Question.exam_id == exam_id
        ))
        question = result.scalars().first()
        if not question:
            raise HTTPException(status_code=404, detail="Question not found.")

        # Reference and student evidence are assembled into ONE immutable,
        # provider-neutral structure with two clearly separated sides. There is
        # no longer a single image list that could be spliced into both the
        # marking-scheme slot and the student-answer slot of the prompt.
        #
        # The student side prefers ACCEPTED structured regions, cropped from the
        # original page on demand, and falls back to the legacy crop paths. The
        # reference side is untouched either way -- audit C1.
        #
        # The workspace owns those generated crops; leaving its `with` block
        # deletes every one of them, on success and on failure alike.
        with CropWorkspace() as workspace:
          try:
            evidence, _evidence_result = await build_region_aware_evidence(
                question=question, question_response=qr,
                exam_id=exam_id, student_id=student_id, db=db,
                workspace=workspace,
                ideal_answer=ideal_answer, marking_scheme=marking_scheme,
            )
          except RegionEvidenceError as exc:
            # Accepted regions exist but their evidence could not be produced.
            # A PREPARATION failure, never a zero (audit C6).
            await _record_grading_failure(
                db, question_id=question_id, student_id=student_id,
                error_code=exc.code,
            )
            return {
                "status": "grading_failed", "grade": None, "reasoning": None,
                "error_code": exc.code,
                "error": "the student's answer evidence could not be prepared",
                "raw_response": None,
            }
          # (the prompt builder reads the answer text from `evidence` itself)
          if not evidence.has_student_evidence:
              raise HTTPException(status_code=400, detail="Missing required parameters. Provide student_answer and at least one of ideal_answer or marking_scheme.")

          prompt_content = _build_diagram_prompt_parts(
              question, evidence,
              marking_scheme=marking_scheme, ideal_answer=ideal_answer,
          )

          try:
              result, raw_text = await ai_services.grade_answer_with_parts(
                  prompt_content,
                  max_marks=question.max_marks,
                  exam_id=exam_id,
                  student_id=student_id,
                  question_id=question_id,
              )
          except ProviderError as exc:
              # The call itself failed -- transport, quota, timeout. That is not
              # a validation failure and emphatically not a zero: record the
              # provider-neutral category as the failure code and report it in
              # the same explicit shape.
              await _record_grading_failure(
                  db, question_id=question_id, student_id=student_id,
                  error_code=exc.category,
              )
              logger.error(
                  "grading call failed: category=%s question_id=%s student_id=%s",
                  exc.category, question_id, student_id,
              )
              return {
                  "status": "grading_failed",
                  "grade": None,
                  "reasoning": None,
                  "error_code": exc.category,
                  "error": "the grading service could not be reached",
                  "raw_response": None,
              }
          except GradingResponseError as exc:
              # An unparseable provider response is a FAILURE, never a zero.
              await _record_grading_failure(
                  db, question_id=question_id, student_id=student_id, error_code=exc.code
              )
              return _grading_failure(exc, question_id=question_id, student_id=student_id)

          return await _persist_and_report(
              db=db, question_id=question_id, student_id=student_id, exam_id=exam_id,
              result=result, raw_text=raw_text,
          )
        
    except HTTPException:
        # A deliberate 403/404 raised above (question missing, caller not a
        # manager) must keep its status instead of being reported as a server
        # fault by the handler below.
        raise
    except Exception as e:
        logger.error(f"Error in grade_question: {str(e)}", exc_info=True)
        # The exam-wide run must be able to say WHICH question died, so record
        # a generic code before propagating. The detail stays in the log.
        await _record_grading_failure(
            db,
            question_id=request.get("question_id"),
            student_id=request.get("student_id"),
            error_code=UNEXPECTED_ERROR,
        )
        raise HTTPException(status_code=500, detail=f"Error grading question: {str(e)}")

async def extract_single_answer_text(
    request: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    exam_id = request.get("exam_id")
    student_id = request.get("student_id")
    question_id = request.get("question_id")
    result = await db.execute(
        select(QuestionResponse)
        .join(Question, Question.id == QuestionResponse.question_id)
        .where(
            Question.exam_id == exam_id,
            QuestionResponse.question_id == question_id,
            QuestionResponse.student_id == student_id
        )
    )
    qr = result.scalars().one_or_none()
    if not qr:
        raise HTTPException(404, "No answer found for this question/exam/user")

    try:
        img_paths = json.loads(qr.ans_text_images) or []
    except json.JSONDecodeError:
        raise HTTPException(400, "Invalid image-list format")
    
    if not img_paths or img_paths == []:
        return JSONResponse(
            status_code=200,
            content={"message": "Text extraction skipped"}
        )
    
    present = [path for path in img_paths if os.path.exists(path)]
    if not present:
        raise HTTPException(500, "No answer image is available to read")

    try:
        extracted_text = (await ai_services.recognise_answer_images(
            present, exam_id=exam_id, student_id=student_id, question_id=question_id
        )).strip()
    except ProviderError as exc:
        logger.error("answer recognition failed: category=%s", exc.category)
        raise HTTPException(502, "Text-extraction service error")

    if not extracted_text:
        raise HTTPException(204, "No text extracted")

    qr.answer_text = extracted_text
    db.add(qr)
    await db.commit()

    return JSONResponse(
        status_code=200,
        content={"message": "Text extracted successfully again"}
    )

async def process_answer_text_images_logic(exam_id: int, student_id: int, db: AsyncSession):
    # print("\n\n\n\nEXTRACTING answer_text IMAGES\n\n\n\n")
    result = await db.execute(select(Question).where(Question.exam_id == exam_id))
    questions = result.scalars().all()
    question_number_map = {
        q.id: str(q.question_number)
        for q in questions
    }

    result = await db.execute(select(QuestionResponse).where(
        QuestionResponse.question_id.in_(question_number_map.keys()),
        QuestionResponse.student_id == student_id
    ))
    responses = result.scalars().all()

    # print("\n\n\n\nfetch_all_responses: ", responses, end="\n\n\n\n")
    batch_entries = []
    for qr in responses:
        try:
            image_list = json.loads(qr.ans_text_images)
            print("image_list: ", image_list, end="\n\n\n\n")
        except Exception as e:
            logger.error(f"Failed to parse image list for {qr.id}: {e}")
            continue
        for img_idx, img_path in enumerate(image_list or []):
            print("img_path: ", img_path, end="\n\n")
            if os.path.exists(img_path):
                batch_entries.append({
                    "qr_id": qr.id,
                    "question_number": question_number_map[qr.question_id],
                    "img_path": img_path
                })

    def chunks(lst, n):
        for i in range(0, len(lst), n):
            yield lst[i:i+n]

    # print("batch_entries: ", batch_entries, end="\n\n\n\n")
    batches = list(chunks(batch_entries, 5))

    extraction_mapping = {}

    async def process_batch(batch):
        print("\n\n\n\nProcessing batch", end="\n\n\n\n")
        present = [e for e in batch if os.path.exists(e["img_path"])]
        if not present:
            return {}

        try:
            text = await ai_services.recognise_answer_images(
                [e["img_path"] for e in present],
                batch=True,
                exam_id=exam_id,
                student_id=student_id,
            )
        except ProviderError as exc:
            # One batch failing must not abandon the others.
            logger.error("batch answer recognition failed: category=%s", exc.category)
            return {}

        def parse_sections(txt):
            data = {}
            parts = re.split(r'Question Number\s*([^\r\n]+)', txt)
            for i in range(1, len(parts), 2):
                qnum = parts[i].strip()
                body = parts[i+1].strip()
                data[qnum] = body
            return data

        extracted = parse_sections(text)

        base_to_qr = {
            entry["question_number"]: entry["qr_id"]
            for entry in batch
        }

        batch_result = {}
        for qnum_str, body in extracted.items():
            m = re.match(r'(\d+)', qnum_str)
            if not m:
                continue
            base = m.group(1)
            if base in base_to_qr:
                qr_id = base_to_qr[base]
                batch_result.setdefault(qr_id, []).append(body)

        return batch_result

    # print("\n\n\n\nProcessing batches: ", batches, end="\n\n\n\n")
    # Bounded, not a bare gather: this used to put EVERY batch in flight at
    # once, so a 60-image paper opened twelve simultaneous provider calls. The
    # batches never touch the session (writes happen serially below), so the
    # only thing missing was a ceiling.
    recognition_limit = get_task_settings(AITask.ANSWER_RECOGNITION).max_concurrency
    outcomes = await run_bounded(
        batches, process_batch, limit=recognition_limit, label="answer_recognition"
    )
    for outcome in outcomes:
        if outcome.ok and isinstance(outcome.value, dict):
            for qr_id, answers in outcome.value.items():
                extraction_mapping.setdefault(qr_id, []).extend(answers)
        elif not outcome.ok:
            logger.error("answer recognition batch failed: %s", type(outcome.error).__name__)

    updated = 0
    try:
        for qr in responses:
            if qr.id in extraction_mapping:
                qr.answer_text = "\n\n".join(extraction_mapping[qr.id])
                db.add(qr)
                updated += 1
        await db.commit()
    except Exception as e:
        logger.error(f"DB write failed: {e}", exc_info=True)
        raise HTTPException(500, "Failed to update answers")

    return JSONResponse(
        {"message": f"Processed {updated} question responses successfully."},
        status_code=200
    )



@router.post("/{exam_id}/process-text-images/answer_script")
async def process_answer_text_image(
    exam_id: int,
    db: AsyncSession = Depends(get_db),
    ctx: ExamContext = Depends(require_exam_manager),
    current_user: User = Depends(get_current_user_required)
):
    await process_answer_text_images_logic(exam_id, current_user.id, db)
    return JSONResponse({"message": "Processed successfully"})


### DUPLICATION DONE TO SOME EXTENT, IMPROVE LATER ###
@router.post("/{exam_id}/process-text-images/marking_scheme")
async def process_marking_scheme_text_image(
    exam_id: int,
    db: AsyncSession = Depends(get_db),
    ctx: ExamContext = Depends(require_exam_manager),
    current_user: User = Depends(get_current_user_required)
):
    """
    For an exam, fetch all questions that have marking scheme images provided.
    For each Question that contains image paths (stored in the ms_text_images field), we:
      - Build a list of entries containing a unique key, associated Question id, and the image file path.
      - Group these entries into batches (e.g., 5 images per batch).
      - Upload all images in the batch concurrently.
      - Construct a composite prompt that includes each image’s unique key.
      - Send all images at once to the Gemini API and extract the marking scheme text.
      - Parse the API response (expected format: "Key: <unique_key> \n <extracted text>"). 
      - Map and store each extracted text into the corresponding Question's ideal_marking_scheme field.
    """
    # Retrieve all exam questions.
    result = await db.execute(select(Question).where(Question.exam_id == exam_id))
    questions = result.scalars().all()
   
    questions_with_images = [q for q in questions if q.ms_text_images]

    batch_entries = []
    for question in questions_with_images:
        try:
            image_list = json.loads(question.ms_text_images)
        except Exception:
            continue
        if not image_list or len(image_list) == 0:
            continue
        for idx, img_path in enumerate(image_list):
            if os.path.exists(img_path):
                key = f"{question.id}_{idx}"
                batch_entries.append({
                    'key': key,
                    'question_id': question.id,
                    'img_path': img_path
                })
    
    def chunks(lst, n):
        for i in range(0, len(lst), n):
            yield lst[i:i+n]

    batches = list(chunks(batch_entries, 5))
    
    extraction_mapping = {}

    async def process_batch(batch):
        batch_extraction_mapping = {}

        present = [e for e in batch if os.path.exists(e['img_path'])]
        if not present:
            return batch_extraction_mapping

        try:
            response_text = await ai_services.recognise_marking_scheme_images(
                [e['img_path'] for e in present],
                key_lines=[e['key'] for e in present],
                exam_id=exam_id,
            )
        except ProviderError as exc:
            logger.error("marking-scheme recognition failed: category=%s", exc.category)
            return batch_extraction_mapping

        if response_text:
            def parse_text(sample_text):
                extracted_data = {}
                # Example split assuming format "Key: <unique_key>" is used
                sections = re.split(r'Key:\s*(\w+_\d+)', sample_text)
                for i in range(1, len(sections), 2):
                    key = sections[i].strip()
                    content = sections[i+1].strip()
                    extracted_data[key] = content
                return extracted_data

            extracted_data = parse_text(response_text)
            
            for entry in present:
                key_str = entry['key']
                if key_str in extracted_data:
                    question_id = entry['question_id']
                    extracted_text = extracted_data[key_str]
                    batch_extraction_mapping.setdefault(question_id, []).append(extracted_text)
        
        return batch_extraction_mapping

    # Same ceiling as the answer batches above, for the same reason.
    scheme_limit = get_task_settings(AITask.MARKING_SCHEME_RECOGNITION).max_concurrency
    outcomes = await run_bounded(
        batches, process_batch, limit=scheme_limit, label="marking_scheme_recognition"
    )
    for outcome in outcomes:
        if outcome.ok and isinstance(outcome.value, dict):
            for question_id, texts in outcome.value.items():
                extraction_mapping.setdefault(question_id, []).extend(texts)
        elif not outcome.ok:
            logger.error("marking-scheme batch failed: %s", type(outcome.error).__name__)

    processed_count = 0
    try:
        for question in questions_with_images:
            if question.id in extraction_mapping:
                question.ideal_marking_scheme = "\n".join(extraction_mapping[question.id])
                db.add(question)
                processed_count += 1
    except Exception as e:
        logger.error(f"Error updating Question: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Error updating Question records.")

    await db.commit()

    return JSONResponse(
        status_code=200,
        content={"message": f"Processed marking scheme images for {processed_count} questions."}
    )


async def _grade_one_question_compute(plan):
    """Run ONE question's provider call. No database, no session, no ORM.

    This is the unit that runs concurrently, and it is safe to run concurrently
    precisely because it touches nothing shared: the prompt was built earlier,
    the provider adapter keeps no per-invocation state, and the result is a
    plain dict. Retry and timeout are the AI service's business and are NOT
    repeated here -- an outer retry would multiply the inner one.

    Returns the same dict shape the routes return, so persistence downstream is
    unchanged.
    """
    try:
        result, raw_text = await ai_services.grade_answer_with_parts(
            plan["parts"],
            max_marks=plan["max_marks"],
            exam_id=plan["exam_id"],
            student_id=plan["student_id"],
            question_id=plan["question_id"],
        )
    except ProviderError as exc:
        # Transport, quota or timeout, after the service layer already
        # exhausted its retries. Not a validation failure and not a zero.
        return {
            "status": "grading_failed", "grade": None, "reasoning": None,
            "error_code": exc.category, "raw_response": None,
        }
    except GradingResponseError as exc:
        # A response arrived and could not be validated. Also not a zero.
        return {
            "status": "grading_failed", "grade": None, "reasoning": None,
            "error_code": exc.code, "raw_response": exc.raw,
        }

    return {
        "status": "graded", "grade": result.score, "reasoning": result.reason,
        "error_code": None, "raw_response": raw_text,
    }


async def grade_exam_logic(exam_id: int, student_id: int, db: AsyncSession):
    """Grade every question of one student's paper.

    THREE PHASES, and the split is the whole point
    ----------------------------------------------
    Grading used to be one loop that read the database, called the provider and
    wrote the result, per question, on a single `AsyncSession`. Wrapping that in
    `asyncio.gather` would have been a data-corruption bug: an `AsyncSession` is
    NOT safe for concurrent use, and two coroutines sharing one interleave on
    the same connection.

        1. LOAD    serial, one session. Read the questions and responses and
                   build every prompt. Ends holding plain data -- no ORM
                   instance survives into phase 2.

        2. GRADE   concurrent, NO session. Provider calls only, bounded by a
                   semaphore, each failure isolated to its own question.

        3. PERSIST serial, one session. Write the outcomes in QUESTION ORDER,
                   then let the caller aggregate.

    So the session is never used by two coroutines at once, and concurrency
    buys wall-clock time without buying a new class of bug.

    Aggregation (`add_exam_result_internal`) still runs afterwards, in the
    caller, once every write has landed -- see backend/tasks.py. C6 decides
    completeness from the persisted marks, so it is unaffected by the order the
    provider happened to answer in.
    """
    result = await db.execute(select(Question).where(Question.exam_id == exam_id))
    questions = result.scalars().all()
    if not questions:
        raise HTTPException(status_code=404, detail="No questions found for this exam.")

    # Stable order for everything that follows: question_number, then id as a
    # tie-break. Completion order must never be visible in the output.
    questions = sorted(
        questions,
        key=lambda q: (q.question_number if q.question_number is not None else 0, q.id),
    )

    settings = get_task_settings(AITask.GRADING)
    started = time.monotonic()

    # One workspace for the whole run: crops generated for phase 1's prompts
    # must still exist when phase 2 uploads them, and every one of them is
    # deleted when this block exits -- on success, on failure, on cancellation.
    with CropWorkspace() as workspace:
        # ---- phase 1: LOAD (serial, one session) -------------------------------
        plans = []
        prep_failures = {}
        for question in questions:
            found = await db.execute(select(QuestionResponse).where(
                QuestionResponse.question_id == question.id,
                QuestionResponse.student_id == student_id,
            ))
            qr = found.scalars().first()

            try:
                evidence, _evidence_result = await build_region_aware_evidence(
                    question=question, question_response=qr,
                    exam_id=exam_id, student_id=student_id, db=db,
                    workspace=workspace,
                    ideal_answer=question.ideal_answer,
                    marking_scheme=question.ideal_marking_scheme,
                )
            except RegionEvidenceError as exc:
                # Accepted regions exist but could not be turned into evidence.
                # A preparation failure with its own code -- never a zero.
                prep_failures[question.id] = exc.code
                continue

            if not evidence.has_student_evidence:
                # Nothing to grade. Recorded as a preparation failure rather than
                # sent to a provider, and it still blocks finalisation via C6.
                prep_failures[question.id] = "no_student_evidence"
                continue

            plans.append({
                "question_id": question.id,
                "question_number": question.question_number,
                "max_marks": question.max_marks,
                "exam_id": exam_id,
                "student_id": student_id,
                "parts": _build_diagram_prompt_parts(
                    question, evidence,
                    marking_scheme=question.ideal_marking_scheme,
                    ideal_answer=question.ideal_answer,
                ),
            })

        # ---- phase 2: GRADE (concurrent, no session) ---------------------------
        outcomes = await run_bounded(
            plans,
            _grade_one_question_compute,
            limit=settings.max_concurrency,
            label="grading",
        )

        graded_by_question = {}
        for outcome in outcomes:
            question_id = outcome.item["question_id"]
            if outcome.ok:
                graded_by_question[question_id] = outcome.value
            else:
                # Belt and braces: `_grade_one_question_compute` already converts
                # the expected failures. Anything reaching here is unforeseen, and
                # it must still not abandon the other questions.
                logger.error(
                    "grading raised for exam_id=%s student_id=%s question_id=%s: %s",
                    exam_id, student_id, question_id, type(outcome.error).__name__,
                )
                graded_by_question[question_id] = {
                    "status": "grading_failed", "grade": None, "reasoning": None,
                    "error_code": UNEXPECTED_ERROR, "raw_response": None,
                }

        # ---- phase 3: PERSIST (serial, one session, question order) ------------
        results = []
        for question in questions:
            outcome = graded_by_question.get(question.id)
            if outcome is None:
                outcome = {
                    "status": "grading_failed", "grade": None, "reasoning": None,
                    "error_code": prep_failures.get(question.id, UNEXPECTED_ERROR),
                    "raw_response": None,
                }

            if outcome["status"] == "graded":
                await _persist_and_report(
                    db=db, question_id=question.id, student_id=student_id,
                    exam_id=exam_id,
                    result=SimpleNamespace(score=outcome["grade"], reason=outcome["reasoning"]),
                    raw_text=outcome["raw_response"],
                )
            else:
                await _record_grading_failure(
                    db, question_id=question.id, student_id=student_id,
                    error_code=outcome["error_code"],
                )

            results.append({
                "question_number": question.question_number,
                "question_id": question.id,
                "status": outcome["status"],
                "grade": outcome["grade"],
                "reasoning": outcome["reasoning"],
                "error_code": outcome["error_code"],
                "raw": outcome["raw_response"],
            })

    # A question whose provider response could not be validated has NO mark in
    # the database -- deliberately, so a failure is never scored as zero.
    # Completeness itself is derived from the persisted marks by
    # `aggregate_student_result`, not from this return value, so a run that
    # crashes outright is caught just as well as one that reports politely.
    failed_questions = [r for r in results if r.get("status") != "graded"]
    duration_ms = int((time.monotonic() - started) * 1000)

    if failed_questions:
        logger.error(
            "grading incomplete for exam_id=%s student_id=%s: %s of %s questions "
            "failed validation (%s)",
            exam_id, student_id, len(failed_questions), len(results),
            [(r["question_number"], r.get("error_code")) for r in failed_questions],
        )

    # Orchestration-level telemetry, so concurrency=1 and concurrency=3 can be
    # compared from logs without a code change. Ids and counts only: no answer
    # text, no marking scheme, no provider output.
    logger.info(
        "exam_grading_run exam_id=%s student_id=%s questions=%s concurrency=%s "
        "graded=%s failed=%s duration_ms=%s",
        exam_id, student_id, len(results), settings.max_concurrency,
        len(results) - len(failed_questions), len(failed_questions), duration_ms,
    )

    return {
        "exam_id": exam_id,
        "student_id": student_id,
        "graded_count": len(results) - len(failed_questions),
        "failed_count": len(failed_questions),
        "failed_questions": failed_questions,
        "results": results,
        "concurrency": settings.max_concurrency,
        "duration_ms": duration_ms,
    }

@router.post("/{exam_id}/grade-exam")
async def grade_exam(
    exam_id: int,
    db: AsyncSession = Depends(get_db),
    ctx: ExamContext = Depends(require_exam_manager),
    current_user: User = Depends(get_current_user_required)
):
    result = await grade_exam_logic(exam_id, current_user.id, db)
    return result

