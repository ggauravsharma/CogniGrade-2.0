from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy.ext.asyncio import AsyncSession     # ASYNC
from sqlalchemy.future import select

from sqlalchemy import desc
from sqlalchemy.dialects.postgresql import TIMESTAMP
import logging

from backend.database import get_db
from backend.models.tables import Announcement, Classroom, Enrollment, Query
from backend.models.users import User
from backend.utils.security import get_current_user_required
from backend.auth.policies import (
    ClassroomContext,
    require_announcement_in_classroom,
    require_classroom_participant,
)
    
router = APIRouter(tags=["announcements"])
logger = logging.getLogger(__name__)

@router.get("/classes/{class_id}/announcements")
async def get_class_announcements(
    class_id: int,
    db: AsyncSession = Depends(get_db),
    ctx: ClassroomContext = Depends(require_classroom_participant),
    current_user: User = Depends(get_current_user_required)
):
    # Verify class exists and user has access
    result = await db.execute(select(Classroom).where(Classroom.id == class_id))
    classroom = result.scalars().first()    
    if not classroom:
        raise HTTPException(status_code=404, detail="Class not found")
    
    # Check if user is enrolled or is the owner
    if classroom.owner_id != current_user.id:
        result = await db.execute(select(Enrollment).where(
            Enrollment.classroom_id == class_id,
            Enrollment.student_id == current_user.id,
            Enrollment.status == "accepted"
        ))
        enrollment = result.scalars().first()
        if not enrollment:
            raise HTTPException(status_code=403, detail="You are not enrolled in this class")
    
    # Get announcements for the class.
    # `db.query(...)` is the SYNCHRONOUS Session API; on an AsyncSession it does
    # not exist, so this endpoint raised AttributeError and returned 500 for
    # every caller. Same ordering, same filter, async execution.
    result = await db.execute(
        select(Announcement)
        .where(Announcement.classroom_id == class_id)
        .order_by(desc(Announcement.created_at))
    )
    announcements_query = result.scalars().all()

    # Author names in ONE query instead of one per announcement -- the previous
    # per-row lookup was both sync-API and N+1.
    author_ids = {a.author_id for a in announcements_query if a.author_id is not None}
    authors = {}
    if author_ids:
        found = await db.execute(select(User).where(User.id.in_(author_ids)))
        authors = {u.id: u.full_name for u in found.scalars().all()}

    # Format announcements
    announcements_list = []
    for announcement in announcements_query:
        author_name = authors.get(announcement.author_id, "Unknown")
        
        # Check if user can edit (author or class owner)
        can_edit = announcement.author_id == current_user.id or classroom.owner_id == current_user.id
        
        announcements_list.append({
            "id": announcement.id,
            "title": announcement.title,
            "content": announcement.content,
            "created_at": announcement.created_at.isoformat() if announcement.created_at else None,
            "author_id": announcement.author_id,
            "author_name": author_name,
            "can_edit": can_edit
        })
    
    return JSONResponse({"success": True, "announcements": announcements_list})

# REMOVED: a second `POST /classes/{class_id}/announcements`.
# `classes.create_announcement` is registered first and was the only reachable
# one. This copy took form-encoded fields while the live handler takes JSON, so
# the two were never interchangeable anyway.
#
# FEATURE NOTE, not a change: this dead copy also notified every enrolled
# student, which the live handler does not do. Announcement notifications are
# therefore not currently sent -- reported rather than silently ported here,
# because starting to send notifications is a behaviour change and not this
# phase's job.

# REMOVED: a second `PUT /classes/{class_id}/announcements/{announcement_id}`.
# `classes.update_announcement` is registered first and was the only reachable
# one. The DELETE below is NOT duplicated and stays here.

@router.delete("/classes/{class_id}/announcements/{announcement_id}")
async def delete_announcement(
    class_id: int,
    announcement_id: int,
    db: AsyncSession = Depends(get_db),
    ctx: ClassroomContext = Depends(require_announcement_in_classroom),
    current_user: User = Depends(get_current_user_required)
):
    # Verify class exists
    result = await db.execute(select(Classroom).where(Classroom.id == class_id))
    classroom = result.scalars().first()    
    if not classroom:
        raise HTTPException(status_code=404, detail="Class not found")
    
    # Get announcement
    result = await db.execute(select(Announcement).where(
        Announcement.id == announcement_id,
        Announcement.classroom_id == class_id
    ))
    announcement = result.scalars().first()  
    if not announcement:
        raise HTTPException(status_code=404, detail="Announcement not found")
    
    # Check permissions (only author or class owner can delete)
    if announcement.author_id != current_user.id and classroom.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="You don't have permission to delete this announcement")
    
    # Get all queries related to this announcement (same sync-API bug as the
    # listing above: this line alone made every delete a 500).
    found = await db.execute(
        select(Query).where(Query.related_announcement_id == announcement_id)
    )
    queries = found.scalars().all()

    query_count = len(queries)

    try:
        # Explicitly delete all related queries first. `AsyncSession.delete` is
        # a COROUTINE -- unawaited it scheduled nothing, so the endpoint used to
        # report a successful delete while the rows were still there.
        for query in queries:
            await db.delete(query)

        # Delete the announcement
        await db.delete(announcement)
        await db.commit()

        return JSONResponse({
            "success": True,
            "message": "Announcement deleted",
            "deleted_queries_count": query_count
        })
    except Exception as e:
        await db.rollback()
        logger.error("Error deleting announcement id=%s: %s", announcement_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail="Error deleting announcement")
