"""A deterministic segmentation provider for tests and local development.

Exists so the region contract, the validation gate, the persistence layer and
the API can all be exercised without a live model and WITHOUT CONSUMING ANY
API QUOTA. It is not an inference engine and must never be a production
default: `get_segmentation_provider` returns it only when explicitly named.

WHY IT RETURNS AWKWARD OUTPUT ON PURPOSE
----------------------------------------
A fake that only emits tidy rectangles would prove nothing. Real segmentation
experiments produced overlapping regions, polygons, crossed-out work, teacher
markings, content it could not attribute to any question, and occasional
malformed geometry. `MALFORMED_PAGE` reproduces the last of those so the
validation gate is tested against the failure mode it exists for, rather than
against a hypothetical one.

Output depends only on `page_index`, so a test asserting on ordering or content
gets the same answer every run.
"""

from __future__ import annotations

import time
from typing import Dict, List, Sequence

from backend.ai.segmentation import (
    RegionPrediction,
    SegmentationRequest,
    SegmentationResponse,
)
from backend.regions.schema import GeometryKind, RegionType

PROVIDER_NAME = "fake"
FAKE_MODEL = "fake-segmenter-v1"
FAKE_PROMPT_VERSION = "segmentation/fake-v1"


def _rect(x, y, w, h):
    return {"x": x, "y": y, "w": w, "h": h}


#: Page 0: an ordinary answer page -- two questions, a diagram, a table, some
#: crossed-out working, a teacher's tick, and a page header that is not answer
#: content. Includes two regions that overlap slightly, because real ones do.
PAGE_0: List[RegionPrediction] = [
    RegionPrediction(
        region_type=RegionType.PAGE_FURNITURE, geometry_kind=GeometryKind.RECT,
        geometry=_rect(0.05, 0.02, 0.90, 0.05),
    ),
    RegionPrediction(
        region_type=RegionType.HANDWRITTEN_TEXT, geometry_kind=GeometryKind.RECT,
        geometry=_rect(0.08, 0.10, 0.84, 0.18), question_number=1, confidence=0.91,
    ),
    RegionPrediction(
        region_type=RegionType.MATH, geometry_kind=GeometryKind.RECT,
        # Deliberately overlaps the text region above by a sliver.
        geometry=_rect(0.10, 0.27, 0.50, 0.10), question_number=1, question_part="a",
    ),
    RegionPrediction(
        region_type=RegionType.CROSSED_OUT, geometry_kind=GeometryKind.RECT,
        geometry=_rect(0.10, 0.38, 0.60, 0.06), question_number=1,
    ),
    RegionPrediction(
        region_type=RegionType.DIAGRAM, geometry_kind=GeometryKind.POLYGON,
        geometry={"points": [[0.15, 0.48], [0.55, 0.46], [0.60, 0.70], [0.18, 0.72]]},
        question_number=2,
    ),
    RegionPrediction(
        region_type=RegionType.TABLE, geometry_kind=GeometryKind.RECT,
        geometry=_rect(0.10, 0.74, 0.70, 0.12), question_number=2, question_part="b",
    ),
    RegionPrediction(
        region_type=RegionType.TEACHER_MARKING, geometry_kind=GeometryKind.RECT,
        geometry=_rect(0.86, 0.12, 0.08, 0.05),
    ),
    RegionPrediction(
        # Real content the model could not attribute to a question. Kept,
        # unassigned -- never guessed at.
        region_type=RegionType.HANDWRITTEN_TEXT, geometry_kind=GeometryKind.RECT,
        geometry=_rect(0.08, 0.88, 0.80, 0.08),
    ),
]

#: Page 1: a question continuing from the previous page, to exercise one
#: question owning regions on more than one page.
PAGE_1: List[RegionPrediction] = [
    RegionPrediction(
        region_type=RegionType.HANDWRITTEN_TEXT, geometry_kind=GeometryKind.RECT,
        geometry=_rect(0.08, 0.06, 0.84, 0.22), question_number=2,
    ),
    RegionPrediction(
        region_type=RegionType.MATH, geometry_kind=GeometryKind.POLYGON,
        geometry={"points": [[0.12, 0.32], [0.62, 0.30], [0.64, 0.46], [0.14, 0.48]]},
        question_number=2, question_part="c",
    ),
]

#: Page 2: everything the validation gate must throw away, mixed with two good
#: regions so a test can prove the good ones still survive.
MALFORMED_PAGE: List[RegionPrediction] = [
    RegionPrediction(
        region_type=RegionType.HANDWRITTEN_TEXT, geometry_kind=GeometryKind.RECT,
        geometry=_rect(0.10, 0.10, 0.50, 0.10), question_number=1,
    ),
    RegionPrediction(  # off the page
        region_type=RegionType.HANDWRITTEN_TEXT, geometry_kind=GeometryKind.RECT,
        geometry=_rect(0.80, 0.80, 0.90, 0.90),
    ),
    RegionPrediction(  # not in the vocabulary
        region_type="handwriting_maybe", geometry_kind=GeometryKind.RECT,
        geometry=_rect(0.10, 0.30, 0.20, 0.10),
    ),
    RegionPrediction(  # a polygon that encloses nothing
        region_type=RegionType.DIAGRAM, geometry_kind=GeometryKind.POLYGON,
        geometry={"points": [[0.1, 0.1], [0.2, 0.2]]},
    ),
    RegionPrediction(  # zero-area rectangle: a stray click
        region_type=RegionType.TABLE, geometry_kind=GeometryKind.RECT,
        geometry=_rect(0.4, 0.4, 0.0, 0.0),
    ),
    RegionPrediction(  # a question this exam does not have
        region_type=RegionType.HANDWRITTEN_TEXT, geometry_kind=GeometryKind.RECT,
        geometry=_rect(0.10, 0.55, 0.40, 0.08), question_number=9999,
    ),
    RegionPrediction(  # geometry is not an object at all
        region_type=RegionType.MATH, geometry_kind=GeometryKind.RECT,
        geometry="somewhere near the middle",
    ),
    RegionPrediction(
        region_type=RegionType.DIAGRAM, geometry_kind=GeometryKind.RECT,
        geometry=_rect(0.10, 0.68, 0.55, 0.20), question_number=2,
    ),
]

_PAGES: Dict[int, List[RegionPrediction]] = {0: PAGE_0, 1: PAGE_1, 2: MALFORMED_PAGE}


class FakeSegmentationProvider:
    """Deterministic segmentation output. No network, no SDK, no quota."""

    name = PROVIDER_NAME

    def __init__(self, *, pages: Dict[int, Sequence[RegionPrediction]] = None):
        self._pages = dict(pages) if pages is not None else dict(_PAGES)
        self.requests: List[SegmentationRequest] = []

    async def segment_page(self, request: SegmentationRequest) -> SegmentationResponse:
        started = time.monotonic()
        self.requests.append(request)
        predictions = list(self._pages.get(request.page_index, []))
        return SegmentationResponse(
            predictions=predictions,
            provider=self.name,
            model=FAKE_MODEL,
            prompt_version=FAKE_PROMPT_VERSION,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
