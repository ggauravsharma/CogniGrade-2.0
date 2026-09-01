"""Choose between structured-region evidence and legacy crops, per question.

The one place the precedence rule lives, and it is decided PER EVIDENCE
CATEGORY rather than per question:

    for each of math, table, diagram:
        accepted/modified regions produced a crop for this category
            -> use those crops, cut from the ORIGINAL PAGE + geometry
        otherwise
            -> the legacy `ans_*_images` paths for that category, as before

    handwritten_text: never attached either way -- `answer_text` carries it.

Category-by-category because the first version replaced the whole student
`ImageSet`, so accepting a single table region silently deleted a valid legacy
diagram from the prompt and the question was graded on less evidence than the
teacher had provided. Replacement was right about one thing -- a structured
diagram and a stale legacy diagram must never both be sent -- and that survives
here, scoped to the category it actually applies to.

Additive. An exam with no regions produces byte-for-byte the evidence it
produced before this module existed, which is what lets the old crop editor and
every existing exam carry on untouched.

WHY FALLBACK IS NOT A CATCH-ALL
-------------------------------
Falling back covers "there is nothing structured to use". It deliberately does
NOT cover "there was something structured and producing it failed": grading a
student against stale legacy crops while their teacher believes the new
annotations are in force would be a quiet wrong answer. A rendering or geometry
failure is therefore raised as `RegionEvidenceError`, and the caller records it
as a preparation failure with no mark (audit C6).

"Something structured to use" means a region that would ATTACH AN IMAGE --
`attachable_regions`, not merely `select_gradeable_regions`. A question whose
only accepted region is handwritten text never had structured images to
produce, so a missing source document there is not a failed preparation and the
legacy crops are not stale substitutes for anything.

This module needs a database session, so unlike `backend/regions/` it is not
pure -- but it holds no provider concept, and the evidence it returns is the
same provider-neutral `GradingEvidence` as before.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any, List, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.grading.evidence import (
    EVIDENCE_CATEGORIES,
    GradingEvidence,
    ImageSet,
    build_grading_evidence,
)
from backend.models.files import AnswerScript
from backend.models.tables import DocumentRegion
from backend.regions.cropping import CropWorkspace, PageRenderer, RegionEvidenceError
from backend.regions.evidence import (
    EvidenceSource,
    RegionEvidenceResult,
    attachable_regions,
    build_region_image_set,
    log_evidence_source,
    merge_student_evidence,
    select_gradeable_regions,
)

logger = logging.getLogger(__name__)


async def load_answer_script(
    exam_id: int, student_id: int, db: AsyncSession
) -> Optional[AnswerScript]:
    """The student's own script for this exam, and nothing else.

    Both ids are constrained, so a region can never be rendered from another
    student's or another exam's document even if a row pointed at one. The path
    comes from this row -- never from a client -- which is what keeps arbitrary
    paths out of the renderer.
    """
    found = await db.execute(
        select(AnswerScript)
        .where(AnswerScript.exam_id == exam_id, AnswerScript.student_id == student_id)
        .order_by(AnswerScript.id)
    )
    return found.scalars().first()


async def load_regions_for_question(
    *, exam_id: int, student_id: int, question_id: int, db: AsyncSession
) -> List[DocumentRegion]:
    """Regions on THIS student's script for THIS question in THIS exam.

    ONE query, joined to `answer_scripts` so the student constraint is enforced
    in SQL. All three of exam, student and question are conditions rather than
    post-filters, which makes cross-student and cross-exam leakage impossible by
    construction instead of by remembering to check. It is also one round trip
    per question rather than two, which matters because every graded question
    pays it whether or not any regions exist.
    """
    found = await db.execute(
        select(DocumentRegion)
        .join(AnswerScript, AnswerScript.id == DocumentRegion.answer_script_id)
        .where(
            DocumentRegion.exam_id == exam_id,
            DocumentRegion.question_id == question_id,
            AnswerScript.student_id == student_id,
            AnswerScript.exam_id == exam_id,
        )
    )
    return list(found.scalars().all())


def _legacy_result(
    evidence: GradingEvidence, *, region_count: int = 0, not_attached: int = 0
) -> RegionEvidenceResult:
    return RegionEvidenceResult(
        image_set=evidence.student_images,
        source=EvidenceSource.LEGACY_CROPS,
        region_count=region_count,
        not_attached_count=not_attached,
    )


def _composed_source(*, covered: "Sequence[str]", legacy: ImageSet) -> str:
    """Name what the merged evidence is actually made of.

    Three honest answers, not two: a question can legitimately end up with a
    structured table and a legacy diagram, and calling that "structured" would
    overstate how much of the prompt reflects the current annotation.
    """
    covered_set = set(covered)
    if not covered_set:
        return EvidenceSource.LEGACY_CROPS
    kept_legacy = any(
        legacy.paths_for(category)
        for category in EVIDENCE_CATEGORIES
        if category not in covered_set
    )
    return EvidenceSource.MIXED if kept_legacy else EvidenceSource.STRUCTURED_REGIONS


async def build_evidence(
    *,
    question,
    question_response,
    exam_id: int,
    student_id: int,
    db: AsyncSession,
    workspace: Optional[CropWorkspace] = None,
    ideal_answer: Optional[str] = None,
    marking_scheme: Optional[str] = None,
) -> "tuple[GradingEvidence, RegionEvidenceResult]":
    """Build one question's evidence, preferring accepted structured regions.

    Returns `(evidence, result)`, where `result.source` says which path was
    taken. The reference side is untouched in both cases -- it always comes
    from the marking scheme (audit C1), and a student region can no more reach
    it here than it could before.

    `workspace` owns the temporary crops. When regions are used the caller MUST
    keep it alive until the provider call has finished, and its `cleanup()`
    removes every generated file.
    """
    # The legacy evidence is built first and unconditionally: it is the
    # reference side in both branches, and the fallback in one of them.
    evidence = build_grading_evidence(
        question=question,
        question_response=question_response,
        ideal_answer=ideal_answer,
        marking_scheme=marking_scheme,
    )

    question_id = getattr(question, "id", None)
    if question_id is None or workspace is None:
        return evidence, _legacy_result(evidence)

    regions = await load_regions_for_question(
        exam_id=exam_id, student_id=student_id, question_id=question_id, db=db
    )
    usable = select_gradeable_regions(regions, question_id=question_id)
    attachable = attachable_regions(usable)
    if not attachable:
        # Nothing accepted; only proposals, rejections or non-answer content;
        # or only handwritten text, which attaches no image. Conservative by
        # design: fall back rather than treat a model's unreviewed guess as
        # evidence, and never discard legacy crops on the strength of regions
        # that were never going to contribute one.
        result = _legacy_result(
            evidence, region_count=len(usable), not_attached=len(usable)
        )
        log_evidence_source(question_id=question_id, student_id=student_id, result=result)
        return evidence, result

    script = await load_answer_script(exam_id, student_id, db)
    if script is None or not script.file_path:
        raise RegionEvidenceError(
            "source_missing",
            "accepted regions exist but the answer script is not available",
        )

    # `usable`, not `attachable`: the builder skips the non-attached types
    # itself and counts them, so the mapping stays in one place.
    result = build_region_image_set(
        usable, source_path=script.file_path, workspace=workspace
    )

    # Per-category precedence. Structured crops win the categories they cover;
    # every other category keeps whatever legacy evidence it had, so accepting
    # a table region cannot delete the student's diagram.
    merged = merge_student_evidence(
        legacy=evidence.student_images,
        structured=result.image_set,
        covered=result.covered_categories,
    )
    result = RegionEvidenceResult(
        image_set=merged,
        source=_composed_source(
            covered=result.covered_categories, legacy=evidence.student_images
        ),
        region_count=result.region_count,
        page_count=result.page_count,
        render_count=result.render_count,
        buckets=result.buckets,
        covered_categories=result.covered_categories,
        not_attached_count=result.not_attached_count,
    )
    log_evidence_source(question_id=question_id, student_id=student_id, result=result)

    return replace(evidence, student_images=merged), result
