"""Authorized serving of uploaded files.

REPLACES `app.mount("/uploads", StaticFiles(directory="uploads"))`, which served
every uploaded answer script, marking scheme and cropped answer image to anyone
who knew or guessed a URL, with no authentication at all.

DESIGN: THE CLIENT NEVER SENDS A PATH
-------------------------------------
Every route here is addressed by DOMAIN IDENTIFIER -- an exam id, a question id,
a response id -- and the file path is looked up in the database after the
caller's rights have been checked. Because no component of the path originates
from the request, path traversal is not merely filtered, it is structurally
impossible: there is no user-controlled string that reaches the filesystem.

`_resolve_within_root` is a second, independent guard applied to the path that
comes back OUT of the database, so that a poisoned or legacy DB value cannot
escape the upload root either. Defence in depth, because the first guarantee
depends on every future route continuing to follow the same pattern.

WHAT IS SERVED TO WHOM
----------------------
    question paper        exam managers and enrolled students
    answer script         the owning student, and exam managers
    marking scheme        exam managers ONLY
    solution script       exam managers ONLY
    cropped answer image  the owning student, and exam managers
    marking-scheme image  exam managers ONLY

A student may read their own work and the question paper. Nothing else.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path as FsPath
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from backend.auth.policies import (
    assert_can_access_question_material,
    assert_can_access_response,
    assert_exam_manager,
    assert_exam_participant,
    load_exam_context,
)
from backend.database import get_db
from backend.models.files import AnswerScript, FileTypeEnum, Material
from backend.models.users import User
from backend.utils.security import get_current_user_required

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/protected-files", tags=["protected-files"])

# Everything servable lives under here. Resolved once at import so that a later
# chdir cannot move the goalposts.
UPLOAD_ROOT = FsPath("./uploads").resolve()

# Document types a student is allowed to fetch for an exam they are enrolled in.
STUDENT_READABLE_DOC_TYPES = {"question_paper"}
MANAGER_ONLY_DOC_TYPES = {"marking_scheme", "solution_script"}

_MATERIAL_TYPES = {
    "question_paper": FileTypeEnum.question_paper,
    "solution_script": FileTypeEnum.solution_script,
    "marking_scheme": FileTypeEnum.marking_scheme,
}

_IMAGE_KINDS = {"text", "table", "diagram"}


def _resolve_within_root(stored_path: Optional[str]) -> FsPath:
    """Turn a stored path into a real file inside UPLOAD_ROOT, or refuse.

    Applied to values coming from the DATABASE, not from the request. Any path
    that escapes the upload root, does not exist, or is not a regular file is
    treated as absent rather than served.
    """
    if not stored_path:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")

    try:
        candidate = FsPath(stored_path).resolve()
    except (OSError, ValueError):
        logger.warning("unresolvable stored file path rejected")
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")

    try:
        common = os.path.commonpath([str(candidate), str(UPLOAD_ROOT)])
    except ValueError:
        # Different drives on Windows, or otherwise incomparable.
        logger.warning("stored file path outside upload root rejected")
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")

    if common != str(UPLOAD_ROOT):
        logger.warning("stored file path escaped upload root; refusing to serve")
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")

    if not candidate.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")

    return candidate


def _serve(path: FsPath) -> FileResponse:
    # inline rather than attachment so the existing PDF.js viewer keeps working
    return FileResponse(
        path,
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


# ---------------------------------------------------------------------------
# exam-level documents
# ---------------------------------------------------------------------------

@router.get("/exam/{exam_id}/document/{doc_type}")
async def get_exam_document(
    exam_id: int,
    doc_type: str,
    student_id: Optional[int] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    """Serve one exam document.

    `student_id` selects whose answer script to serve and is only honoured for
    exam managers; a student always receives their own.
    """
    doc = doc_type.lower()

    if doc == "answer_script":
        ctx = await load_exam_context(exam_id, current_user, db)
        if ctx.is_manager:
            target_student = student_id if student_id is not None else current_user.id
        else:
            if not ctx.is_enrolled_student:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                    detail="Not authorized")
            if student_id is not None and student_id != current_user.id:
                logger.warning(
                    "authz denied: student requested another student's answer script "
                    "(user_id=%s exam_id=%s)", current_user.id, exam_id
                )
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                    detail="Not authorized")
            target_student = current_user.id

        result = await db.execute(
            select(AnswerScript).where(
                AnswerScript.exam_id == exam_id,
                AnswerScript.student_id == target_student,
            )
        )
        script = result.scalars().first()
        if not script:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail="Answer script not found")
        return _serve(_resolve_within_root(script.file_path))

    if doc in MANAGER_ONLY_DOC_TYPES:
        await assert_exam_manager(exam_id, current_user, db)
    elif doc in STUDENT_READABLE_DOC_TYPES:
        await assert_exam_participant(exam_id, current_user, db)
    else:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Invalid document type")

    result = await db.execute(
        select(Material).where(
            Material.related_exam_id == exam_id,
            Material.file_type == _MATERIAL_TYPES[doc],
        )
    )
    material = result.scalars().first()
    if not material:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    return _serve(_resolve_within_root(material.file_path))


# ---------------------------------------------------------------------------
# individual stored documents, addressed by their own row id
#
# The route above answers "the marking scheme for exam 5" and returns the FIRST
# matching row. That is the right shape for a viewer that wants "the" document,
# but `POST /exam/save-files` deduplicates by (title, exam, type), so an exam
# genuinely can hold several question-paper files -- a scanned paper split
# across images is the ordinary case. Nothing could address the second one.
#
# These two routes close that gap without widening access: the id names a row,
# the exam is resolved FROM that row, and the capability required is the same
# one the doc_type route applies to the same file_type. A material id from
# another exam therefore grants nothing here that it would not grant there.
# ---------------------------------------------------------------------------

@router.get("/material/{material_id}")
async def get_material_file(
    material_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    """One exam material by its own id. Same capability table as by doc type."""
    result = await db.execute(select(Material).where(Material.id == material_id))
    material = result.scalars().first()
    if not material:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    file_type = getattr(material.file_type, "value", material.file_type)
    if file_type in MANAGER_ONLY_DOC_TYPES:
        await assert_exam_manager(material.related_exam_id, current_user, db)
    elif file_type in STUDENT_READABLE_DOC_TYPES:
        await assert_exam_participant(material.related_exam_id, current_user, db)
    else:
        # Assignment materials and anything else are not served here; they have
        # no capability rule in this module and must not inherit one by default.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    return _serve(_resolve_within_root(material.file_path))


@router.get("/answer-script/{answer_script_id}")
async def get_answer_script_file(
    answer_script_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    """One answer script by its own id. Owning student, or an exam manager."""
    result = await db.execute(
        select(AnswerScript).where(AnswerScript.id == answer_script_id)
    )
    script = result.scalars().first()
    if not script:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Answer script not found")

    # Deliberately the same two conditions the doc_type route applies to
    # `answer_script`, in the same order, rather than the looser `ctx.owns`:
    # a non-manager must be an ENROLLED student AND the owner. A student whose
    # enrolment has gone must not keep reading the script through a stale id.
    ctx = await load_exam_context(script.exam_id, current_user, db)
    if not ctx.is_manager:
        if not ctx.is_enrolled_student or script.student_id != current_user.id:
            logger.warning(
                "authz denied: answer script not readable by caller "
                "(user_id=%s answer_script_id=%s)", current_user.id, answer_script_id
            )
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")

    return _serve(_resolve_within_root(script.file_path))


# ---------------------------------------------------------------------------
# cropped region images
# ---------------------------------------------------------------------------

@router.get("/response/{response_id}/{kind}/{index}")
async def get_response_image(
    response_id: int,
    kind: str,
    index: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    """A cropped image from a student's answer. Owning student or manager."""
    if kind not in _IMAGE_KINDS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid image kind")

    qr = await assert_can_access_response(response_id, current_user, db)
    raw = getattr(qr, f"ans_{kind}_images", None)
    return _serve(_resolve_within_root(_nth_path(raw, index)))


@router.get("/question/{question_id}/marking-image/{kind}/{index}")
async def get_marking_scheme_image(
    question_id: int,
    kind: str,
    index: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    """A cropped marking-scheme image. Exam managers only -- never students."""
    if kind not in _IMAGE_KINDS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid image kind")

    question = await assert_can_access_question_material(question_id, current_user, db)
    raw = getattr(question, f"ms_{kind}_images", None)
    return _serve(_resolve_within_root(_nth_path(raw, index)))


def _nth_path(raw_json: Optional[str], index: int) -> Optional[str]:
    """Pull one path out of a stored JSON list, bounds-checked."""
    if not raw_json:
        return None
    try:
        paths = json.loads(raw_json)
    except (ValueError, TypeError):
        logger.warning("malformed image path list in database")
        return None
    if not isinstance(paths, list) or index < 0 or index >= len(paths):
        return None
    value = paths[index]
    return value if isinstance(value, str) else None
