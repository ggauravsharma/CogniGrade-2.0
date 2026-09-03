"""Permanently delete one classroom, and the files only it owned.

WHY THIS IS ONE OPERATION
-------------------------
A course's data reaches across ten tables and the disk. Asking the frontend to
call a sequence of endpoints would make "delete the course" a workflow that can
stop halfway, and a half-deleted course is worse than either outcome: its exams
still answer queries while its classroom row is gone. So the backend owns the
whole thing and exposes one call.

WHAT ACTUALLY DOES THE DELETING
-------------------------------
The DATABASE. Verified against the live schema rather than assumed from the ORM:
every foreign key from a classroom-owned table carries `ON DELETE CASCADE` --

    classrooms -> announcements, assignments, enrollments, exams, materials,
                  notifications, queries
    assignments -> submissions, materials, notifications, queries
    exams       -> answer_scripts, document_regions, exam_results, materials,
                   notifications, queries, questions
    questions   -> question_responses
    answer_scripts / materials -> document_regions

and `Classroom`'s relationships are all `passive_deletes=True`, so the ORM does
not load children into Python to delete them one by one -- it issues a single
`DELETE FROM classrooms` and lets the database do the cascade in one statement,
inside one transaction. SQLite honours it too, because `backend/database.py`
turns on `PRAGMA foreign_keys` for every connection. No migration was needed and
none was added.

WHAT IS DELIBERATELY NOT DELETED
--------------------------------
**Users.** Every foreign key involving a person points FROM a classroom-owned
row TO `users.id`, never the other way, so deleting a course cannot reach an
account. A professor who owned this course and a student who was enrolled in it
both survive, along with everything they have in any other classroom. That is
the whole reason this is a classroom-scoped delete and not a cascade from a
user.

FILES
-----
Rows disappear in one transaction; the files they named do not. Paths are
collected BEFORE the delete (afterwards there is nothing left to read them
from), and removed only AFTER the commit succeeds. Every path is re-checked
against the upload root, so a stored value that escapes it is skipped rather
than followed. Cleanup failure is logged and swallowed: the rows are already
gone, and reporting an error for a deletion that happened would be a lie.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Iterable, List, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.files import AnswerScript, Material
from backend.models.tables import (
    Assignment,
    Classroom,
    Exam,
    Question,
    QuestionResponse,
    Submission,
)
from backend.storage.paths import delete_files_within_root

logger = logging.getLogger(__name__)

#: Columns holding a JSON list of image paths. Crops written by the editor and
#: marking-scheme images live here rather than in a file table.
_QUESTION_IMAGE_COLUMNS = ("ms_text_images", "ms_table_images", "ms_diagram_images")
_RESPONSE_IMAGE_COLUMNS = ("ans_text_images", "ans_table_images", "ans_diagram_images")


@dataclass(frozen=True)
class ClassroomDeletionResult:
    """What happened, in counts only -- never a path and never a filename."""

    classroom_id: int
    files_deleted: int
    files_skipped: int


def _paths_from_json_column(value: Optional[str]) -> List[str]:
    """Read one `[...]` image column defensively.

    Auto-prepared responses leave these NULL, and a legacy row may hold text
    that is not JSON at all. Neither is a reason to abandon a deletion, so a
    column that will not parse contributes nothing instead of raising.
    """
    if not value or not str(value).strip():
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        logger.warning("unparseable image column skipped during deletion cleanup")
        return []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, str) and item.strip()]


async def collect_owned_file_paths(classroom_id: int, db: AsyncSession) -> List[str]:
    """Every stored path owned EXCLUSIVELY by this classroom.

    Ownership is established by walking the same foreign keys the cascade
    walks, so a file can only be listed here if its row is about to be deleted.
    Nothing is derived from a request value or from a filename pattern.
    """
    paths: List[str] = []

    exam_ids: Sequence[int] = (
        await db.execute(select(Exam.id).where(Exam.classroom_id == classroom_id))
    ).scalars().all()
    assignment_ids: Sequence[int] = (
        await db.execute(select(Assignment.id).where(Assignment.classroom_id == classroom_id))
    ).scalars().all()

    # Materials reach a classroom directly OR through one of its exams or
    # assignments; a material attached to neither is not this course's to remove.
    material_clauses = [Material.classroom_id == classroom_id]
    if exam_ids:
        material_clauses.append(Material.related_exam_id.in_(exam_ids))
    if assignment_ids:
        material_clauses.append(Material.related_assignment_id.in_(assignment_ids))
    from sqlalchemy import or_

    paths += (
        await db.execute(select(Material.file_path).where(or_(*material_clauses)))
    ).scalars().all()

    if assignment_ids:
        paths += (
            await db.execute(
                select(Submission.file_path).where(Submission.assignment_id.in_(assignment_ids))
            )
        ).scalars().all()

    if exam_ids:
        paths += (
            await db.execute(
                select(AnswerScript.file_path).where(AnswerScript.exam_id.in_(exam_ids))
            )
        ).scalars().all()

        questions = (
            await db.execute(select(Question).where(Question.exam_id.in_(exam_ids)))
        ).scalars().all()
        question_ids = [q.id for q in questions]
        for question in questions:
            for column in _QUESTION_IMAGE_COLUMNS:
                paths += _paths_from_json_column(getattr(question, column, None))

        if question_ids:
            responses = (
                await db.execute(
                    select(QuestionResponse).where(QuestionResponse.question_id.in_(question_ids))
                )
            ).scalars().all()
            for response in responses:
                for column in _RESPONSE_IMAGE_COLUMNS:
                    paths += _paths_from_json_column(getattr(response, column, None))

    # Deduplicate while keeping order: two rows may legitimately name one file,
    # and unlinking it twice would count a phantom failure.
    seen = set()
    unique: List[str] = []
    for path in paths:
        if path and path not in seen:
            seen.add(path)
            unique.append(path)
    return unique


async def delete_classroom(classroom: Any, db: AsyncSession) -> ClassroomDeletionResult:
    """Delete `classroom` and its owned files. Rows first, atomically; files after.

    The caller has already authorised this and loaded the row -- ownership is a
    routing concern, not this function's.
    """
    classroom_id = classroom.id
    file_paths = await collect_owned_file_paths(classroom_id, db)

    try:
        await db.delete(classroom)
        await db.commit()
    except Exception:
        # Nothing is half-deleted: the cascade is one statement in one
        # transaction, and rolling back leaves every row exactly as it was.
        # No file has been touched yet, which is why cleanup comes second.
        await db.rollback()
        logger.exception("classroom deletion rolled back: classroom_id=%s", classroom_id)
        raise

    deleted, skipped = delete_files_within_root(file_paths)
    if skipped:
        # Counts only. A stored filename can name a student.
        logger.warning(
            "classroom file cleanup incomplete: classroom_id=%s removed=%s skipped=%s",
            classroom_id, deleted, skipped,
        )
    return ClassroomDeletionResult(
        classroom_id=classroom_id, files_deleted=deleted, files_skipped=skipped
    )
