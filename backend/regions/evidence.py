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
* `handwritten_text` AS AN IMAGE -- the region counts, but the crop is not
  attached, because the recognised `answer_text` already carries it. See
  `NON_ATTACHED_STUDENT_TYPES`.

PRECEDENCE IS PER CATEGORY, NOT PER QUESTION
--------------------------------------------
Structured evidence used to replace the student `ImageSet` wholesale, so one
accepted table region deleted a perfectly valid legacy diagram from the prompt
-- evidence loss with nothing in the logs to show for it. `merge_student_evidence`
decides category by category instead: `math`, `table` and `diagram` each go to
the structured crops IF structured crops for that category were produced, and
otherwise stay with the legacy paths. That keeps the anti-duplication property
that motivated replacement (a structured diagram and a stale legacy diagram are
never both sent) without letting one category speak for the others.

Provider-neutral: no SDK, no FastAPI, no SQLAlchemy (asserted by a test). Rows
are duck-typed, so ORM instances and plain stubs both work.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from backend.grading.evidence import EVIDENCE_CATEGORIES, ImageSet
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
    #: Structured evidence covered some categories; valid legacy crops supplied
    #: the rest. See `merge_student_evidence`.
    MIXED = "mixed"

    ALL = (STRUCTURED_REGIONS, LEGACY_CROPS, MIXED)


#: Which `ImageSet` category a student-answer region type is ATTACHED under.
#:
#: `math` has its own category. It previously mapped to `diagram`, which meant
#: the prompt told the grader that an attached handwritten derivation was a
#: diagram -- a false statement about the evidence, and one that invites the
#: wrong reading strategy (checking a figure's labels instead of following a
#: method). Nothing about that compromise was necessary: the category costs one
#: field on a frozen dataclass and no migration.
REGION_TYPE_TO_BUCKET: Dict[str, str] = {
    RegionType.MATH: "math",
    RegionType.DIAGRAM: "diagram",
    RegionType.TABLE: "table",
}

#: Student-answer region types that are REPRESENTED but deliberately not
#: attached as images.
#:
#: Handwritten text is already in the prompt: the answer-recognition stage
#: turns it into `answer_text`, and the legacy path has always passed
#: `ImageSet.text = []` on the student side for exactly that reason. Attaching
#: the crop as well would send the same sentences twice -- once transcribed,
#: once as pixels -- inflating cost and latency and inviting the grader either
#: to double-count the content or to disagree with itself about what the
#: student wrote.
#:
#: This is a policy, not an oversight. If a later phase wants raw handwriting
#: as a distinct multimodal signal (to check a transcription, say), it belongs
#: in the `text` category WITH the recognised text suppressed, not alongside
#: it. Nothing here is lost meanwhile: the region is still selected, still
#: counted, and still says which question the writing belongs to.
NON_ATTACHED_STUDENT_TYPES: Tuple[str, ...] = (RegionType.HANDWRITTEN_TEXT,)


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
    #: Categories for which structured evidence was actually PRODUCED. The
    #: precedence rule keys off this rather than off "an accepted region
    #: exists", so an accepted handwritten-text region -- which attaches
    #: nothing -- cannot displace a valid legacy diagram.
    covered_categories: Tuple[str, ...] = ()
    #: Usable regions that are real answer content but attach no image.
    not_attached_count: int = 0

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


def attachable_regions(regions: Sequence[Any]) -> List[Any]:
    """The subset of usable regions that will actually produce an image.

    The fail-closed rule in `backend/grading/region_evidence.py` keys off this,
    not off `select_gradeable_regions`. A question whose only accepted region
    is handwritten text has nothing to render, so a missing source document is
    not a preparation FAILURE there -- there was never any structured evidence
    to fail at producing. Where something IS attachable and rendering it fails,
    the failure still stands.
    """
    return [
        region for region in regions
        if getattr(region, "region_type", None) in REGION_TYPE_TO_BUCKET
    ]


def merge_student_evidence(
    *, legacy: ImageSet, structured: ImageSet, covered: Sequence[str]
) -> ImageSet:
    """Compose the student `ImageSet` one category at a time.

        category in `covered`   -> the structured crops for it, alone
        otherwise               -> whatever legacy evidence that category had

    So a structured table and a legacy diagram both survive, while a structured
    diagram and a legacy diagram never both go: within a category the
    structured evidence is authoritative, because it reflects the annotation as
    it stands now and the legacy crop may predate it.

    Never a union across a category, and never an append: both would show the
    grader the same answer twice.
    """
    covered_set = set(covered)
    return ImageSet(**{
        category: (
            list(structured.paths_for(category))
            if category in covered_set
            else list(legacy.paths_for(category))
        )
        for category in EVIDENCE_CATEGORIES
    })


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
    buckets: Dict[str, List[str]] = {category: [] for category in EVIDENCE_CATEGORIES}
    not_attached = 0

    try:
        for region in regions:
            region_type = getattr(region, "region_type", None)
            if region_type in NON_ATTACHED_STUDENT_TYPES:
                # Represented, and already in the prompt as recognised text.
                # Not rendered at all: producing a crop only to drop it would
                # cost a rasterisation per region for nothing.
                not_attached += 1
                continue
            bucket = REGION_TYPE_TO_BUCKET.get(region_type)
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

        pages = {
            getattr(r, "page_index", 0) or 0
            for r in regions
            if getattr(r, "region_type", None) not in NON_ATTACHED_STUDENT_TYPES
        }
        image_set = ImageSet(
            **{category: buckets[category] for category in EVIDENCE_CATEGORIES}
        )
        return RegionEvidenceResult(
            image_set=image_set,
            source=EvidenceSource.STRUCTURED_REGIONS,
            region_count=len(regions),
            page_count=len(pages),
            render_count=renderer.render_count,
            buckets={k: len(v) for k, v in buckets.items()},
            covered_categories=image_set.present_categories,
            not_attached_count=not_attached,
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
        "region_count=%s page_count=%s rendered_pages=%s buckets=%s "
        "structured_categories=%s not_attached=%s",
        question_id if question_id is not None else "-",
        student_id if student_id is not None else "-",
        result.source, result.region_count, result.page_count,
        result.render_count, result.buckets or {},
        list(result.covered_categories), result.not_attached_count,
    )
