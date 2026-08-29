from fastapi import APIRouter, Depends, HTTPException, status, Request, Form
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession     # ASYNC
from sqlalchemy.future import select

from typing import Optional
from datetime import datetime, timezone
from sqlalchemy.dialects.postgresql import TIMESTAMP
from pydantic import BaseModel

import logging

from backend.database import get_db
from backend.models.tables import Classroom, Enrollment, Role
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

    # Prepare professor info
    professor = {
        "user_id": classroom.owner_id,
        "full_name": classroom.owner.full_name,
        "role": "professor"
    }
    
    # Fetch TA enrollments
    result = await db.execute(select(Enrollment).where(
        Enrollment.classroom_id == class_id,
        Enrollment.status == "accepted",
        Enrollment.role == "ta"
    ))
    ta_enrollments = result.scalars().all()
    
    # Fetch student enrollments
    result = await db.execute(select(Enrollment).where(
        Enrollment.classroom_id == class_id,
        Enrollment.status == "accepted",
        Enrollment.role == "student"
    ))
    student_enrollments = result.scalars().all()
    
    teachers = [professor] + [{
        "enrollment_id": e.id,
        "user_id": e.student_id,
        "full_name": e.student.full_name,
        "role": e.role.value if hasattr(e.role, 'value') else e.role
    } for e in ta_enrollments]
    
    students = [{
        "enrollment_id": e.id,
        "user_id": e.student_id,
        "full_name": e.student.full_name,
        "role": e.role.value if hasattr(e.role, 'value') else e.role
    } for e in student_enrollments]
    
    return JSONResponse({"success": True, "teachers": teachers, "students": students})

@router.post("/enrollments/{enrollment_id}/remove")
async def remove_student(enrollment_id: int, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user_required)):
    # Authorization is resolved against the enrolment's OWN classroom.
    # Previously `current_user.is_professor` alone granted access, which let a
    # professor of any classroom remove members from every other classroom.
    enrollment, ctx = await assert_enrollment_manageable(
        enrollment_id, current_user, db, require_owner=False
    )
    classroom = ctx.classroom

    # Existing product rule preserved: a TA may only remove plain students,
    # while the classroom owner may remove anyone.
    if not ctx.is_owner and enrollment.role != Role.STUDENT:
        logger.warning(
            "authz denied: TA attempted to remove a non-student enrolment "
            "(user_id=%s enrollment_id=%s)", current_user.id, enrollment_id
        )
        raise HTTPException(status_code=403, detail="Not authorized to remove this student")

    student_id = enrollment.student_id
    db.delete(enrollment)
    await db.commit()
    
    # (Optional) Send a notification if needed...
    
    return JSONResponse({"success": True, "message": "Student removed from class"})

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