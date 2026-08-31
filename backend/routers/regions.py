"""Structured page regions: propose, view, correct, persist.

The smallest coherent surface for the workflow this phase exists to enable:

    the configured provider proposes POST   /answer-scripts/{id}/segmentation
    a human reads them               GET    /answer-scripts/{id}/regions
    a human draws one                POST   /answer-scripts/{id}/regions
    a human corrects/accepts one     PATCH  /regions/{id}
    a human rejects one              DELETE /regions/{id}
    a human fixes the order          POST   /answer-scripts/{id}/regions/reorder

No listing/editing endpoints were added for marking-scheme materials: the model
supports them, but nothing consumes them yet and an endpoint with no caller is
an untested liability.

AUTHORIZATION reuses the existing exam policies rather than reimplementing
them. Every route resolves the exam FROM THE ANSWER SCRIPT, so a caller
authorised on exam A can never reach a region belonging to exam B -- the
cross-resource rule the security foundations established.

NO PROVIDER NAMES appear in any path OR IN ANY REQUEST BODY. The segmentation
endpoint names the task; which adapter performs it is deployment configuration
(`resolve_segmentation_provider`). A client that could name the adapter could
name the development double, and stored synthetic regions are indistinguishable
from a model's afterwards.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.ai.errors import ProviderNotConfiguredError
from backend.ai.providers import resolve_segmentation_provider
from backend.ai.segmentation import SegmentationRequest, validate_predictions
from backend.auth.policies import assert_exam_manager, assert_exam_participant
from backend.database import get_db
from backend.models.files import AnswerScript
from backend.models.tables import DocumentRegion, Question
from backend.models.users import User
from backend.regions.schema import (
    InvalidRegionError,
    RegionSource,
    RegionStatus,
    validate_region,
)
from backend.utils.security import get_current_user_required

logger = logging.getLogger(__name__)
router = APIRouter(tags=["regions"])


# ---------------------------------------------------------------------------
# request bodies
# ---------------------------------------------------------------------------

class RegionCreate(BaseModel):
    page_index: int
    region_type: str
    geometry_kind: str
    geometry: Dict[str, Any]
    question_id: Optional[int] = None
    question_part: Optional[str] = None
    reading_order: Optional[int] = None


class RegionUpdate(BaseModel):
    """Every field optional: a correction touches only what changed."""

    region_type: Optional[str] = None
    geometry_kind: Optional[str] = None
    geometry: Optional[Dict[str, Any]] = None
    question_id: Optional[int] = None
    question_part: Optional[str] = None
    reading_order: Optional[int] = None
    status: Optional[str] = None
    #: Explicitly clear the question assignment, since `question_id=None` is
    #: indistinguishable from "not supplied" in a partial update.
    unassign_question: bool = False


class ReorderRequest(BaseModel):
    #: Region ids in their new reading order.
    region_ids: List[int]


class SegmentationRun(BaseModel):
    """Which page to segment. NOT which provider segments it.

    A `provider` field lived here and defaulted to the development double, so
    an ordinary production request against a real answer script persisted
    synthetic regions onto a real student's paper. Provider selection is
    deployment configuration (`resolve_segmentation_provider`), so the field is
    gone rather than merely given a safer default: a client that can name an
    adapter can eventually name the wrong one.
    """

    page_index: int
    page_count: int
    #: Discard existing model proposals for this page first. Human-authored and
    #: already-accepted regions are never touched.
    replace_existing: bool = True


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

async def _load_script(answer_script_id: int, db: AsyncSession) -> AnswerScript:
    found = await db.execute(
        select(AnswerScript).where(AnswerScript.id == answer_script_id)
    )
    script = found.scalars().first()
    if script is None:
        raise HTTPException(status_code=404, detail="Answer script not found")
    return script


async def _script_for_read(answer_script_id: int, user: User, db: AsyncSession) -> AnswerScript:
    """Managers may read any script in their exam; a student only their own."""
    script = await _load_script(answer_script_id, db)
    ctx = await assert_exam_participant(script.exam_id, user, db)
    if not ctx.owns(script.student_id):
        raise HTTPException(status_code=403, detail="Not your answer script")
    return script


async def _script_for_write(answer_script_id: int, user: User, db: AsyncSession) -> AnswerScript:
    """Correcting an annotation is a manager action.

    Students may see the regions on their own script but not re-annotate them:
    the annotation is what grading will run on.
    """
    script = await _load_script(answer_script_id, db)
    await assert_exam_manager(script.exam_id, user, db)
    return script


async def _region_for_write(region_id: int, user: User, db: AsyncSession):
    found = await db.execute(select(DocumentRegion).where(DocumentRegion.id == region_id))
    region = found.scalars().first()
    if region is None:
        raise HTTPException(status_code=404, detail="Region not found")
    # Authorisation is resolved from the REGION's own exam, so being a manager
    # of a different exam grants nothing here.
    await assert_exam_manager(region.exam_id, user, db)
    return region


def _serialise(region: DocumentRegion) -> Dict[str, Any]:
    return {
        "id": region.id,
        "answer_script_id": region.answer_script_id,
        "material_id": region.material_id,
        "page_index": region.page_index,
        "region_type": region.region_type,
        "geometry_kind": region.geometry_kind,
        "geometry": json.loads(region.geometry),
        "question_id": region.question_id,
        "question_part": region.question_part,
        "reading_order": region.reading_order,
        "status": region.status,
        "source": region.source,
        "provider": region.provider,
        "model": region.model_name,
        "prompt_version": region.prompt_version,
        "crop_path_present": bool(region.crop_path),
    }


async def _question_id_by_number(exam_id: int, db: AsyncSession) -> Dict[int, int]:
    rows = (await db.execute(
        select(Question.question_number, Question.id).where(Question.exam_id == exam_id)
    )).all()
    return {int(number): qid for number, qid in rows if number is not None}


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------

@router.get("/answer-scripts/{answer_script_id}/regions")
async def list_regions(
    answer_script_id: int,
    page_index: Optional[int] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    """Regions on one script, in page then reading order.

    Deterministic ordering, never insertion order: what a reviewer sees must
    not depend on which proposal happened to be written first.
    """
    script = await _script_for_read(answer_script_id, current_user, db)

    stmt = select(DocumentRegion).where(
        DocumentRegion.answer_script_id == script.id
    ).order_by(DocumentRegion.page_index, DocumentRegion.reading_order, DocumentRegion.id)
    if page_index is not None:
        stmt = stmt.where(DocumentRegion.page_index == page_index)

    regions = (await db.execute(stmt)).scalars().all()
    return {"answer_script_id": script.id, "regions": [_serialise(r) for r in regions]}


@router.post("/answer-scripts/{answer_script_id}/segmentation")
async def request_segmentation(
    answer_script_id: int,
    body: SegmentationRun,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    """Ask the CONFIGURED provider to PROPOSE regions for one page.

    Everything it returns is stored as `proposed` and nothing downstream may
    treat that as an annotation. Proposals that fail deterministic validation
    are dropped and counted, never silently repaired.

    The provider is resolved BEFORE the request is built and before anything is
    written, so a deployment with no segmentation adapter answers 503 and
    leaves the script exactly as it found it -- no deletions, no rows, no
    commit.
    """
    script = await _script_for_write(answer_script_id, current_user, db)

    try:
        provider = resolve_segmentation_provider()
    except ProviderNotConfiguredError as exc:
        # 503, not 400: the caller asked for something reasonable that this
        # deployment cannot currently perform. The code is provider-neutral --
        # naming the registry here would leak which adapters exist.
        logger.warning("segmentation requested but no provider is configured")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="segmentation_not_configured",
        ) from exc

    try:
        request = SegmentationRequest(
            page_image_path=script.file_path or "",
            page_index=body.page_index,
            page_count=body.page_count,
            known_question_numbers=(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    response = await provider.segment_page(request)
    outcome = validate_predictions(
        response, request,
        question_id_by_number=await _question_id_by_number(script.exam_id, db),
    )

    if body.replace_existing:
        # Only untouched MODEL proposals go. A human's work is never discarded
        # by re-running a model.
        existing = (await db.execute(select(DocumentRegion).where(
            DocumentRegion.answer_script_id == script.id,
            DocumentRegion.page_index == body.page_index,
            DocumentRegion.source == RegionSource.MODEL,
            DocumentRegion.status == RegionStatus.PROPOSED,
        ))).scalars().all()
        for stale in existing:
            await db.delete(stale)

    created = []
    for region in outcome.regions:
        row = DocumentRegion(
            exam_id=script.exam_id,
            answer_script_id=script.id,
            page_index=region.page_index,
            region_type=region.region_type,
            geometry_kind=region.geometry_kind,
            geometry=json.dumps(region.geometry),
            question_id=region.question_id,
            question_part=region.question_part,
            reading_order=region.reading_order,
            status=region.status,
            source=region.source,
            provider=response.provider,
            model_name=response.model,
            prompt_version=response.prompt_version,
            provider_metadata=json.dumps(region.metadata) if region.metadata else None,
        )
        db.add(row)
        created.append(row)
    await db.commit()
    for row in created:
        await db.refresh(row)

    logger.info(
        "segmentation proposals stored: answer_script=%s page=%s accepted=%s rejected=%s provider=%s",
        script.id, body.page_index, outcome.accepted_count, outcome.rejected_count,
        response.provider,
    )
    return {
        "answer_script_id": script.id,
        "page_index": body.page_index,
        "proposed": len(created),
        "rejected": outcome.rejected_count,
        "rejected_reasons": sorted({code for _, code, _ in outcome.rejected}),
        "regions": [_serialise(r) for r in created],
    }


@router.post("/answer-scripts/{answer_script_id}/regions", status_code=status.HTTP_201_CREATED)
async def create_region(
    answer_script_id: int,
    body: RegionCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    """A human draws a region. Born `accepted`: a person is the authority."""
    script = await _script_for_write(answer_script_id, current_user, db)

    if body.reading_order is None:
        highest = (await db.execute(select(DocumentRegion.reading_order).where(
            DocumentRegion.answer_script_id == script.id,
            DocumentRegion.page_index == body.page_index,
        ))).scalars().all()
        reading_order = (max(highest) + 1) if highest else 0
    else:
        reading_order = body.reading_order

    try:
        region = validate_region(
            page_index=body.page_index,
            region_type=body.region_type,
            geometry_kind=body.geometry_kind,
            geometry=body.geometry,
            reading_order=reading_order,
            status=RegionStatus.ACCEPTED,
            source=RegionSource.HUMAN,
            question_id=body.question_id,
            question_part=body.question_part,
        )
    except InvalidRegionError as exc:
        raise HTTPException(status_code=400, detail=f"{exc.code}: {exc.message}")

    row = DocumentRegion(
        exam_id=script.exam_id,
        answer_script_id=script.id,
        page_index=region.page_index,
        region_type=region.region_type,
        geometry_kind=region.geometry_kind,
        geometry=json.dumps(region.geometry),
        question_id=region.question_id,
        question_part=region.question_part,
        reading_order=region.reading_order,
        status=region.status,
        source=region.source,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _serialise(row)


@router.patch("/regions/{region_id}")
async def update_region(
    region_id: int,
    body: RegionUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    """Correct or accept one region.

    Editing a model PROPOSAL promotes it to `modified` automatically -- the
    distinction between "the model was right" and "a human fixed it" is the
    only record of how good the model actually was, and it must not depend on
    the client remembering to set a field.
    """
    region = await _region_for_write(region_id, current_user, db)

    merged = {
        "page_index": region.page_index,
        "region_type": body.region_type or region.region_type,
        "geometry_kind": body.geometry_kind or region.geometry_kind,
        "geometry": body.geometry if body.geometry is not None else json.loads(region.geometry),
        "reading_order": body.reading_order if body.reading_order is not None else region.reading_order,
        "question_id": region.question_id,
        "question_part": body.question_part if body.question_part is not None else region.question_part,
    }
    if body.unassign_question:
        merged["question_id"] = None
        merged["question_part"] = None
    elif body.question_id is not None:
        merged["question_id"] = body.question_id

    changed_content = any(
        value is not None for value in
        (body.region_type, body.geometry_kind, body.geometry, body.question_part)
    ) or body.question_id is not None or body.unassign_question

    if body.status is not None:
        new_status = body.status
    elif region.source == RegionSource.MODEL and region.status == RegionStatus.PROPOSED:
        new_status = RegionStatus.MODIFIED if changed_content else RegionStatus.ACCEPTED
    else:
        new_status = region.status

    try:
        validated = validate_region(
            status=new_status,
            source=region.source,
            **merged,
        )
    except InvalidRegionError as exc:
        raise HTTPException(status_code=400, detail=f"{exc.code}: {exc.message}")

    region.region_type = validated.region_type
    region.geometry_kind = validated.geometry_kind
    region.geometry = json.dumps(validated.geometry)
    region.question_id = validated.question_id
    region.question_part = validated.question_part
    region.reading_order = validated.reading_order
    region.status = validated.status
    await db.commit()
    await db.refresh(region)
    return _serialise(region)


@router.delete("/regions/{region_id}")
async def reject_region(
    region_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    """Reject a proposal, or delete a human region.

    A model proposal is marked `rejected` rather than deleted: which proposals
    a human threw away is the other half of the record of how well the model
    did. A human's own region has no such value and is removed outright.
    """
    region = await _region_for_write(region_id, current_user, db)

    if region.source == RegionSource.MODEL:
        region.status = RegionStatus.REJECTED
        await db.commit()
        await db.refresh(region)
        return {"id": region.id, "status": region.status, "deleted": False}

    await db.delete(region)
    await db.commit()
    return {"id": region_id, "status": None, "deleted": True}


@router.post("/answer-scripts/{answer_script_id}/regions/reorder")
async def reorder_regions(
    answer_script_id: int,
    body: ReorderRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_required),
):
    """Set reading order explicitly, 0..n-1 in the order given.

    Reading order is a fact the application owns once a human has stated it, so
    it is stored densely and never re-derived from ids or DOM position.
    """
    script = await _script_for_write(answer_script_id, current_user, db)

    rows = (await db.execute(select(DocumentRegion).where(
        DocumentRegion.answer_script_id == script.id,
        DocumentRegion.id.in_(body.region_ids),
    ))).scalars().all()
    by_id = {row.id: row for row in rows}

    missing = [rid for rid in body.region_ids if rid not in by_id]
    if missing:
        raise HTTPException(
            status_code=404,
            detail=f"regions not found on this answer script: {missing}",
        )

    for ordinal, region_id in enumerate(body.region_ids):
        by_id[region_id].reading_order = ordinal
    await db.commit()

    return {
        "answer_script_id": script.id,
        "reordered": len(body.region_ids),
        "order": body.region_ids,
    }
