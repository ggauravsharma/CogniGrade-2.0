"""Turning accepted regions into the evidence a grader is shown.

    DocumentRegion[]
      -> accepted/modified + this question only        select_gradeable_regions
      -> student-answer types only
      -> page_index, reading_order, id                 deterministic order
      -> render page, cut geometry                     backend/regions/cropping
      -> ImageSet                                      build_region_image_set
      -> GradingEvidence.student_images

Reference evidence is NOT touched. The marking scheme keeps coming from
`Question.ms_*_images`, because no structured-region contract exists for
marking schemes yet and inventing one here would be the C1 mistake in a new
costume: student regions must be able to reach the student slot and nothing
else.

WHAT DOES NOT ENTER GRADING
---------------------------
* `proposed` regions -- a model's guess must never move a mark on its own.
* `rejected` regions -- a human said no.
* `teacher_marking` -- the red pen is not the student's answer.
* `crossed_out` -- struck-through working is represented, not graded.
* `page_furniture` / `printed_text` -- not answer content.
* regions with no `question_id` -- real content, but assigning it here would
  be inventing a semantic link the annotation deliberately left open.

Provider-neutral: no SDK, no FastAPI, no SQLAlchemy (asserted by a test). Rows
are duck-typed, so ORM instances and plain stubs both work.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from backend.grading.evidence import ImageSet
from backend.regions.cropping import CropWorkspace, PageRenderer, RegionEvidenceError, crop_region
from backend.regions.schema import (
    STUDENT_ANSWER_TYPES,
    RegionStatus,
    RegionType,
)

logger = logging.getLogger(__name__)


class EvidenceSource:
    """Where a question's student evidence came from. Recorded for telemetry."""

    STRUCTURED_REGIONS = "structured_regions"
    LEGACY_CROPS = "legacy_crops"

    ALL = (STRUCTURED_REGIONS, LEGACY_CROPS)


#: How a student-answer region type lands in the three `ImageSet` buckets.
#:
#: `math` maps to `diagram`, and that is a deliberate, temporary compromise
#: rather than a claim that mathematics is a picture. `ImageSet` has exactly
#: three buckets, which exist to drive prompt wording ("the attached diagrams
#: and tables"), and `text` is reserved for content already converted to
#: `answer_text` upstream -- putting a math crop there would attach an image
#: the prompt describes as text and never mentions. Until an HMER stage exists
#: to turn handwritten mathematics into a transcription, the honest place for a
#: math crop is alongside diagrams, where the prompt does tell the grader to
#: look at the attached image.
REGION_TYPE_TO_BUCKET: Dict[str, str] = {
    RegionType.HANDWRITTEN_TEXT: "text",
    RegionType.MATH: "diagram",
    RegionType.DIAGRAM: "diagram",
    RegionType.TABLE: "table",
}


@dataclass
class RegionEvidenceResult:
    """The outcome of building student evidence from regions."""

    image_set: ImageSet
    source: str
    region_count: int = 0
    page_count: int = 0
    #: Pages actually rasterised. Lower than `page_count` means the cache worked.
    render_count: int = 0
    buckets: Dict[str, int] = field(default_factory=dict)

    @property
    def has_any(self) -> bool:
        return self.image_set.has_any


def select_gradeable_regions(regions: Sequence[Any], *, question_id: int) -> List[Any]:
    """The regions that may contribute evidence for one question.

    Filters, then orders by `(page_index, reading_order, id)`. The ordering is
    total and derived only from stored columns, so evidence is identical on
    every run regardless of insertion order, filesystem order or the order a
    provider happened to answer in.
    """
    eligible = [
        region for region in regions
        if getattr(region, "question_id", None) == question_id
        and getattr(region, "status", None) in RegionStatus.USABLE
        and getattr(region, "region_type", None) in STUDENT_ANSWER_TYPES
    ]
    return sorted(
        eligible,
        key=lambda r: (
            getattr(r, "page_index", 0) or 0,
            getattr(r, "reading_order", 0) or 0,
            getattr(r, "id", 0) or 0,
        ),
    )


def build_region_image_set(
    regions: Sequence[Any],
    *,
    source_path: str,
    workspace: CropWorkspace,
    renderer: Optional[PageRenderer] = None,
) -> RegionEvidenceResult:
    """Render each region to a temporary crop and bucket the paths.

    One `PageRenderer` for the whole call, so a page carrying four regions is
    rasterised once. Raises `RegionEvidenceError` if a page cannot be produced:
    that is a PREPARATION failure and the caller must record it as a missing
    mark, never as a zero.
    """
    if not regions:
        return RegionEvidenceResult(image_set=ImageSet(), source=EvidenceSource.STRUCTURED_REGIONS)

    owns_renderer = renderer is None
    renderer = renderer or PageRenderer(source_path)
    buckets: Dict[str, List[str]] = {"text": [], "table": [], "diagram": []}

    try:
        for region in regions:
            bucket = REGION_TYPE_TO_BUCKET.get(getattr(region, "region_type", None))
            if bucket is None:
                # Cannot happen after select_gradeable_regions, but a caller
                # passing its own list must not silently get a wrong bucket.
                continue
            geometry = getattr(region, "geometry", None)
            if isinstance(geometry, str):
                import json

                try:
                    geometry = json.loads(geometry)
                except ValueError:
                    raise RegionEvidenceError(
                        "geometry_unreadable", "a stored region geometry is not valid JSON"
                    )

            image = crop_region(
                renderer,
                page_index=getattr(region, "page_index", 0) or 0,
                geometry_kind=getattr(region, "geometry_kind", "rect"),
                geometry=geometry,
            )
            path = workspace.write(image, name=f"region-{getattr(region, 'id', len(workspace.paths))}")
            buckets[bucket].append(path)

        pages = {getattr(r, "page_index", 0) or 0 for r in regions}
        return RegionEvidenceResult(
            image_set=ImageSet(
                text=buckets["text"], table=buckets["table"], diagram=buckets["diagram"]
            ),
            source=EvidenceSource.STRUCTURED_REGIONS,
            region_count=len(regions),
            page_count=len(pages),
            render_count=renderer.render_count,
            buckets={k: len(v) for k, v in buckets.items()},
        )
    finally:
        if owns_renderer:
            renderer.close()


def log_evidence_source(
    *,
    question_id: Optional[int],
    student_id: Optional[int],
    result: RegionEvidenceResult,
) -> None:
    """One safe line saying where a question's evidence came from.

    Ids and counts only. No answer text, no marking scheme, no file paths -- a
    crop path names a student's temporary directory and belongs nowhere near a
    log line.
    """
    logger.info(
        "grading_evidence question_id=%s student_id=%s evidence_source=%s "
        "region_count=%s page_count=%s rendered_pages=%s buckets=%s",
        question_id if question_id is not None else "-",
        student_id if student_id is not None else "-",
        result.source, result.region_count, result.page_count,
        result.render_count, result.buckets or {},
    )
