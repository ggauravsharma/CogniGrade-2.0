from fastapi import APIRouter, Request, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from datetime import datetime, timezone
from sqlalchemy.dialects.postgresql import TIMESTAMP
from backend.database import get_db
from backend.models.tables import Enrollment
from backend.models.users import User
from backend.models.notifications import Notification, NotificationType
from backend.utils.security import get_current_user_required
from backend.auth.policies import (
    ClassroomContext,
    assert_enrollment_manageable,
    require_classroom_owner,
)

router = APIRouter(tags=["enrollments"])

# REMOVED: a second `POST /classes/join-class`.
# `classes.join_class` is registered first and was the only reachable one. The
# two implement different product rules -- this copy created a PENDING
# enrolment needing owner approval and notified the owner, while the live
# handler auto-accepts ("FOR NOW ALL ACCEPTED ON JOINING"). Only the live rule
# has ever run, so removing this changes nothing observable; it removes a trap
# where reordering main.py would silently switch the join flow back to
# approval-based.

@router.get("/enrollments/manage/{class_id}")
async def manage_enrollments(class_id: int, db: AsyncSession = Depends(get_db), ctx: ClassroomContext = Depends(require_classroom_owner), current_user: User = Depends(get_current_user_required)):
    # Owner-only, preserving the pre-existing semantics of this route.
    classroom = ctx.classroom

    result = await db.execute(select(Enrollment).where(
        Enrollment.classroom_id == class_id,
        Enrollment.status == "pending"
    ))
    pending_enrollments = result.scalars().all()
    pending_students = []
    for enrollment in pending_enrollments:
        result = await db.execute(select(User).where(User.id == enrollment.student_id))
        student = result.scalars().first()
        if student:
            pending_students.append({"student_id": student.id, "full_name": student.full_name, "enrollment_id": enrollment.id})
    
    result = await db.execute(select(Enrollment).where(
        Enrollment.classroom_id == class_id,
        Enrollment.status == "accepted"
    ))
    accepted_enrollments = result.scalars().all()
    enrolled_students = []
    for enrollment in accepted_enrollments:
        result = await db.execute(select(User).where(User.id == enrollment.student_id))
        student = result.scalars().first()
        if student:
            enrolled_students.append({"student_id": student.id, "full_name": student.full_name, "enrollment_id": enrollment.id})
    
    return JSONResponse({
        "success": True,
        "classroom": {"id": classroom.id, "name": classroom.name},
        "pending_students": pending_students,
        "enrolled_students": enrolled_students
    })

@router.post("/enrollments/{enrollment_id}/accept")
async def accept_enrollment(enrollment_id: int, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user_required)):
    # Cross-resource integrity: the classroom is resolved from the enrolment
    # itself, and ownership is checked against THAT classroom.
    enrollment, ctx = await assert_enrollment_manageable(
        enrollment_id, current_user, db, require_owner=True
    )
    classroom = ctx.classroom

    enrollment.status = "accepted"
    await db.commit()
    
    notification = Notification(
        type=NotificationType.ENROLLMENT_ACCEPTED,
        title="Enrollment Accepted",
        message=f"Your request to join {classroom.name} has been accepted",
        sender_id=current_user.id,
        recipient_id=enrollment.student_id,
        classroom_id=classroom.id,
        action_url=f"/classes/{classroom.id}",
        created_at=datetime.now(timezone.utc)
    )
    db.add(notification)
    await db.commit()
    
    return JSONResponse({"success": True, "message": "Enrollment accepted"})

@router.post("/enrollments/{enrollment_id}/reject")
async def reject_enrollment(enrollment_id: int, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user_required)):
    # Cross-resource integrity: the classroom is resolved from the enrolment
    # itself, and ownership is checked against THAT classroom.
    enrollment, ctx = await assert_enrollment_manageable(
        enrollment_id, current_user, db, require_owner=True
    )
    classroom = ctx.classroom

    enrollment.status = "rejected"
    await db.commit()
    
    notification = Notification(
        type=NotificationType.ENROLLMENT_REJECTED,
        title="Enrollment Rejected",
        message=f"Your request to join {classroom.name} has been rejected",
        sender_id=current_user.id,
        recipient_id=enrollment.student_id,
        classroom_id=classroom.id,
        created_at=datetime.now(timezone.utc)
    )
    db.add(notification)
    await db.commit()
    
    return JSONResponse({"success": True, "message": "Enrollment rejected"})

@router.post("/enrollments/{enrollment_id}/remove")
async def remove_student(enrollment_id: int, db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user_required)):
    # Cross-resource integrity: the classroom is resolved from the enrolment
    # itself, and ownership is checked against THAT classroom.
    enrollment, ctx = await assert_enrollment_manageable(
        enrollment_id, current_user, db, require_owner=True
    )
    classroom = ctx.classroom

    student_id = enrollment.student_id
    await db.delete(enrollment)
    await db.commit()
    
    notification = Notification(
        type=NotificationType.ENROLLMENT_REMOVED,
        title="Removed from Class",
        message=f"You have been removed from the class {classroom.name}",
        sender_id=current_user.id,
        recipient_id=student_id,
        classroom_id=classroom.id,
        action_url=f"/classes/{classroom.id}",
        created_at=datetime.now(timezone.utc)
    )
    db.add(notification)
    await db.commit()
    
    return JSONResponse({"success": True, "message": "Student removed from class"})