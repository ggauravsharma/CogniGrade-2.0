from fastapi import APIRouter, Depends, HTTPException, status, Request, Form
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession     # ASYNC
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload

from typing import Optional
from datetime import datetime, timezone
from sqlalchemy.dialects.postgresql import TIMESTAMP
from pydantic import BaseModel

import logging

from backend.database import get_db
from backend.models.tables import Classroom, Enrollment
from backend.models.users import User
from backend.utils.security import get_current_user_required
from backend.auth.policies import (
    ClassroomContext,
    assert_enrollment_manageable,
    require_classroom_participant,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["pplManagement"])

@router.get("/classes/{class_id}/people")
async def get_class_people(
    class_id: int,
    db: AsyncSession = Depends(get_db),
    ctx: ClassroomContext = Depends(require_classroom_participant),
    current_user: User = Depends(get_current_user_required),
):
    # Membership is required: this endpoint previously had NO authorization at
    # all, so any authenticated user could enumerate the full roster (names and
    # user ids) of any classroom in the database.
    classroom = ctx.classroom

    # The owner's name has to be SELECTED, not walked into.
    # `classroom.owner` is a lazy relationship and `ctx.classroom` was loaded by
    # a plain select, so reading `classroom.owner.full_name` triggered lazy IO
    # inside async context -- SQLAlchemy raises MissingGreenlet there, which
    # made this endpoint return 500 for every caller, member or not.
    # Fetched explicitly instead of by relationship access; no global lazy
    # configuration is changed, so nothing else in the app is affected.
    owner_name = None
    if classroom.owner_id is not None:
        found = await db.execute(
            select(User.full_name).where(User.id == classroom.owner_id)
        )
        owner_name = found.scalar_one_or_none()

    # Prepare professor info
    professor = {
        "user_id": classroom.owner_id,
        "full_name": owner_name,
        "role": "professor"
    }

    # Fetch TA enrollments. `selectinload(Enrollment.student)` for the same
    # reason: `e.student.full_name` below is another lazy access on the same
    # code path, and would fail identically once the owner lookup was fixed.
    result = await db.execute(select(Enrollment)
        .options(selectinload(Enrollment.student))
        .where(
            Enrollment.classroom_id == class_id,
            Enrollment.status == "accepted",
            Enrollment.role == "ta"
        )
    )
    ta_enrollments = result.scalars().all()

    # Fetch student enrollments
    result = await db.execute(select(Enrollment)
        .options(selectinload(Enrollment.student))
        .where(
            Enrollment.classroom_id == class_id,
            Enrollment.status == "accepted",
            Enrollment.role == "student"
        )
    )
    student_enrollments = result.scalars().all()
    
    def _member(e):
        return {
            "enrollment_id": e.id,
            "user_id": e.student_id,
            "full_name": e.student.full_name if e.student else None,
            "role": e.role.value if hasattr(e.role, 'value') else e.role
        }

    teachers = [professor] + [_member(e) for e in ta_enrollments]
    students = [_member(e) for e in student_enrollments]
    
    return JSONResponse({"success": True, "teachers": teachers, "students": students})

# REMOVED: a second `POST /enrollments/{enrollment_id}/remove`.
# `enrollments.remove_student` is registered first in main.py and was therefore
# the only one ever reachable; this copy was dead code that additionally called
# `db.delete(...)` without awaiting it, so had include order ever changed, the
# endpoint would have reported success without deleting anything.
#
# BEHAVIOUR NOTE, not a change: this dead copy allowed a TA to remove a plain
# student (`require_owner=False`), while the live handler is owner-only. Since
# it was unreachable, removing it changes nothing a client can observe. If TAs
# should be able to remove students, that is a product decision to make
# deliberately on `enrollments.remove_student`.

@router.post("/enrollments/{enrollment_id}/make-ta")
async def make_ta(enrollment_id: int, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user_required)):
    # OWNER-ONLY, scoped to the enrolment's own classroom. Promoting a member
    # to TA grants them MANAGER rights over that classroom, so this must not
    # itself be a manager-level action. Previously the global is_professor flag
    # was sufficient, which allowed a professor of any classroom to grant TA
    # rights inside any other classroom.
    enrollment, ctx = await assert_enrollment_manageable(
        enrollment_id, current_user, db, require_owner=True
    )

    if enrollment.role != "student":
        raise HTTPException(status_code=400, detail="Enrollment is not a student")
    
    enrollment.role = "ta"
    await db.commit()
    return JSONResponse({"success": True, "message": "Student promoted to TA"})

@router.post("/enrollments/{enrollment_id}/make-student")
async def make_student(enrollment_id: int, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user_required)):
    # OWNER-ONLY, scoped to the enrolment's own classroom (see make_ta).
    enrollment, ctx = await assert_enrollment_manageable(
        enrollment_id, current_user, db, require_owner=True
    )

    if enrollment.role != "ta":
        raise HTTPException(status_code=400, detail="Enrollment is not a TA")
    
    enrollment.role = "student"
    await db.commit()
    return JSONResponse({"success": True, "message": "TA demoted to Student"})