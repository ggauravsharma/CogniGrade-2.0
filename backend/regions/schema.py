"""What a region is, and what makes one valid.

Pure domain: no SDK, no FastAPI, no SQLAlchemy, no `backend.models` (asserted by
a token-level test, as in `backend/grading/`). A model proposing regions, a
teacher drawing one by hand, and a future specialist detector all produce this
same structure.

THE COORDINATE CONVENTION
-------------------------
Geometry is **normalised to the page: every x and y is a float in [0, 1]**,
with the origin at the page's top-left.

That is not a stylistic choice. `crop-edit.js` renders each PDF page through
`page.getViewport({scale: displayScale})` and reads mouse positions from
`getBoundingClientRect()`, so the pixel numbers a browser produces depend on
the zoom level, the device pixel ratio and the window size. Persisting those
would mean a box drawn on a laptop lands somewhere else on a projector. A
fraction of the page is the only figure that survives re-rendering, and it
survives re-rasterising the PDF at a different DPI too -- which matters because
crops are meant to be regenerable from the original page (see the module
docstring in `backend/regions/__init__.py`).

WHAT IS DELIBERATELY NOT HERE
-----------------------------
No confidence threshold logic. Prior experiments found provider self-reported
confidence and "did I miss anything" flags unreliable, so confidence may travel
as opaque metadata but must never gate acceptance. Nothing in this module reads
it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple


class InvalidRegionError(ValueError):
    """A proposed region cannot be stored as it stands.

    Carries a machine-readable `code` so a caller can count rejection reasons
    without matching on message text -- the same convention as
    `GradingResponseError` and `InvalidMarkError`.
    """

    def __init__(self, code: str, message: str, *, region_id: Optional[str] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.region_id = region_id

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"InvalidRegionError(code={self.code!r}, message={self.message!r})"


class RegionType:
    """The controlled vocabulary for what a region contains.

    Deliberately short. Nine values, each earning its place from something the
    system does today or a failure mode the segmentation experiments actually
    observed -- not a speculative taxonomy.

    * The three legacy crop buckets map onto `HANDWRITTEN_TEXT`, `TABLE` and
      `DIAGRAM`, so existing data has somewhere to go.
    * `MATH` is separate from text because handwritten mathematics needs an
      HMER model rather than an HTR one; that routing decision is the whole
      point of typing a region.
    * `CROSSED_OUT` and `TEACHER_MARKING` exist so that struck-through work and
      the teacher's red pen can be REPRESENTED rather than silently dropped
      into or excluded from a student's answer. Both were observed in the
      experiments and both are answer-correctness-relevant.
    * `PRINTED_TEXT` and `PAGE_FURNITURE` (headings, roll-number boxes, page
      numbers) let a detector say "this is not answer content" explicitly
      instead of omitting it, which is the silent-omission failure mode.
    * `OTHER` keeps an unknown label storable instead of forcing a wrong one.
    """

    HANDWRITTEN_TEXT = "handwritten_text"
    PRINTED_TEXT = "printed_text"
    MATH = "math"
    DIAGRAM = "diagram"
    TABLE = "table"
    CROSSED_OUT = "crossed_out"
    TEACHER_MARKING = "teacher_marking"
    PAGE_FURNITURE = "page_furniture"
    OTHER = "other"


ALLOWED_REGION_TYPES: Tuple[str, ...] = (
    RegionType.HANDWRITTEN_TEXT,
    RegionType.PRINTED_TEXT,
    RegionType.MATH,
    RegionType.DIAGRAM,
    RegionType.TABLE,
    RegionType.CROSSED_OUT,
    RegionType.TEACHER_MARKING,
    RegionType.PAGE_FURNITURE,
    RegionType.OTHER,
)

#: Region types that are the STUDENT's own answer content. Crossed-out work and
#: the teacher's marking are deliberately excluded: representing them is the
#: point, feeding them to a grader as the student's answer is not.
STUDENT_ANSWER_TYPES: Tuple[str, ...] = (
    RegionType.HANDWRITTEN_TEXT,
    RegionType.MATH,
    RegionType.DIAGRAM,
    RegionType.TABLE,
)

#: How the existing three crop buckets translate, so legacy crops can be
#: represented in the new vocabulary without guessing.
LEGACY_BUCKET_TO_REGION_TYPE: Dict[str, str] = {
    "text": RegionType.HANDWRITTEN_TEXT,
    "table": RegionType.TABLE,
    "diagram": RegionType.DIAGRAM,
}


class RegionStatus:
    """The smallest lifecycle that distinguishes a guess from an annotation.

    A model's output is a PROPOSAL. It becomes an annotation only when a person
    accepts it, edits it, or it is created by hand in the first place. That is
    the entire workflow: nothing downstream may treat `PROPOSED` as truth.
    """

    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    #: Accepted, but the human changed the geometry, type or assignment first.
    #: Kept distinct from ACCEPTED so a later benchmark can ask how often the
    #: model was right unaided.
    MODIFIED = "modified"
    REJECTED = "rejected"

    ALL: Tuple[str, ...] = (PROPOSED, ACCEPTED, MODIFIED, REJECTED)
    #: Statuses that represent a real annotation a downstream stage may use.
    USABLE: Tuple[str, ...] = (ACCEPTED, MODIFIED)


class RegionSource:
    """Who produced the region. Structural meaning only -- never a vendor name.

    Which provider and model is metadata (`provider`, `model`), because the
    domain must not care, and a DB enum naming a vendor would be exactly the
    coupling this architecture exists to avoid.
    """

    MODEL = "model"
    HUMAN = "human"

    ALL: Tuple[str, ...] = (MODEL, HUMAN)


class GeometryKind:
    RECT = "rect"
    POLYGON = "polygon"

    ALL: Tuple[str, ...] = (RECT, POLYGON)


# ---------------------------------------------------------------------------
# geometry
# ---------------------------------------------------------------------------

#: Floating-point slack when checking the [0, 1] bounds. A viewer computing
#: 1.0000000000000002 from a drag to the page edge is not an error.
BOUNDS_EPSILON = 1e-6
#: Smallest side of a rectangle, as a fraction of the page. Below this a region
#: is a stray click, not an annotation.
MIN_SIDE = 1e-4


def _coerce_unit(value: Any, *, field_name: str, region_id: Optional[str]) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidRegionError(
            "geometry_not_numeric", f"{field_name} must be a number", region_id=region_id
        )
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise InvalidRegionError(
            "geometry_not_finite", f"{field_name} must be finite", region_id=region_id
        )
    if number < -BOUNDS_EPSILON or number > 1 + BOUNDS_EPSILON:
        raise InvalidRegionError(
            "geometry_out_of_bounds",
            f"{field_name}={number} is outside the normalised page (0..1)",
            region_id=region_id,
        )
    # Clamp the epsilon slack away so what is stored is genuinely in range.
    return min(1.0, max(0.0, number))


def normalise_geometry(
    kind: str, geometry: Any, *, region_id: Optional[str] = None
) -> Dict[str, Any]:
    """Validate and canonicalise one region's geometry.

    Returns the stored form: `{"x", "y", "w", "h"}` for a rectangle, or
    `{"points": [[x, y], ...]}` for a polygon. Raises `InvalidRegionError` for
    anything that could not be drawn on a page.

    A rectangle given with negative width or height (the user dragged up and to
    the left) is normalised rather than rejected -- that is a drawing gesture,
    not bad data.
    """
    if kind not in GeometryKind.ALL:
        raise InvalidRegionError(
            "geometry_kind_unknown", f"unknown geometry kind {kind!r}", region_id=region_id
        )
    if not isinstance(geometry, dict):
        raise InvalidRegionError(
            "geometry_malformed", "geometry must be an object", region_id=region_id
        )

    if kind == GeometryKind.RECT:
        for key in ("x", "y", "w", "h"):
            if key not in geometry:
                raise InvalidRegionError(
                    "geometry_malformed", f"rectangle is missing {key!r}", region_id=region_id
                )
        x = _coerce_unit(geometry["x"], field_name="x", region_id=region_id)
        y = _coerce_unit(geometry["y"], field_name="y", region_id=region_id)
        w = float(geometry["w"]) if isinstance(geometry["w"], (int, float)) and not isinstance(geometry["w"], bool) else None
        h = float(geometry["h"]) if isinstance(geometry["h"], (int, float)) and not isinstance(geometry["h"], bool) else None
        if w is None or h is None:
            raise InvalidRegionError(
                "geometry_not_numeric", "width and height must be numbers", region_id=region_id
            )
        # A drag up/left produces negative extents; fold them into the origin.
        if w < 0:
            x, w = x + w, -w
        if h < 0:
            y, h = y + h, -h
        x = _coerce_unit(x, field_name="x", region_id=region_id)
        y = _coerce_unit(y, field_name="y", region_id=region_id)
        if w < MIN_SIDE or h < MIN_SIDE:
            raise InvalidRegionError(
                "geometry_degenerate",
                "rectangle has no area; it is a stray click rather than a region",
                region_id=region_id,
            )
        if x + w > 1 + BOUNDS_EPSILON or y + h > 1 + BOUNDS_EPSILON:
            raise InvalidRegionError(
                "geometry_out_of_bounds",
                "rectangle extends past the page edge",
                region_id=region_id,
            )
        return {
            "x": round(x, 6), "y": round(y, 6),
            "w": round(min(w, 1.0 - x), 6), "h": round(min(h, 1.0 - y), 6),
        }

    points = geometry.get("points")
    if not isinstance(points, (list, tuple)):
        raise InvalidRegionError(
            "geometry_malformed", "polygon needs a list of points", region_id=region_id
        )
    if len(points) < 3:
        raise InvalidRegionError(
            "geometry_degenerate",
            "a polygon needs at least three points to enclose anything",
            region_id=region_id,
        )
    cleaned: List[List[float]] = []
    for index, point in enumerate(points):
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise InvalidRegionError(
                "geometry_malformed",
                f"polygon point {index} is not an [x, y] pair",
                region_id=region_id,
            )
        px = _coerce_unit(point[0], field_name=f"points[{index}].x", region_id=region_id)
        py = _coerce_unit(point[1], field_name=f"points[{index}].y", region_id=region_id)
        cleaned.append([round(px, 6), round(py, 6)])
    return {"points": cleaned}


def geometry_bounds(kind: str, geometry: Dict[str, Any]) -> Dict[str, float]:
    """The axis-aligned box enclosing a geometry.

    Deterministic and derived, never asked of a model: a crop generator or a
    viewer needs a box even when the region is a polygon.
    """
    if kind == GeometryKind.RECT:
        return {
            "x": geometry["x"], "y": geometry["y"],
            "w": geometry["w"], "h": geometry["h"],
        }
    xs = [p[0] for p in geometry["points"]]
    ys = [p[1] for p in geometry["points"]]
    # Rounded to the same 6 decimal places the stored geometry uses. Without
    # this, 0.6 - 0.2 comes back as 0.39999999999999997 and a crop generated
    # from these bounds would differ from one generated from the same polygon
    # a moment later.
    return {
        "x": round(min(xs), 6), "y": round(min(ys), 6),
        "w": round(max(xs) - min(xs), 6), "h": round(max(ys) - min(ys), 6),
    }


# ---------------------------------------------------------------------------
# the region itself
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Region:
    """One validated region, independent of storage and of any provider."""

    page_index: int
    region_type: str
    geometry_kind: str
    geometry: Dict[str, Any]
    reading_order: int
    status: str = RegionStatus.PROPOSED
    source: str = RegionSource.MODEL
    #: Optional semantic assignment. A region may legitimately belong to no
    #: question yet -- see `validate_region`.
    question_id: Optional[int] = None
    question_part: Optional[str] = None
    #: Opaque, non-sensitive provider metadata. Never read by domain logic.
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_student_answer_content(self) -> bool:
        """True only for the student's own work.

        Crossed-out and teacher-marking regions are represented but are NOT
        answer evidence, so a grading pipeline that filters on this cannot
        accidentally mark a teacher's tick as the student's working.
        """
        return self.region_type in STUDENT_ANSWER_TYPES

    @property
    def bounds(self) -> Dict[str, float]:
        return geometry_bounds(self.geometry_kind, self.geometry)


def validate_region(
    *,
    page_index: Any,
    region_type: Any,
    geometry_kind: Any,
    geometry: Any,
    reading_order: Any,
    status: Any = RegionStatus.PROPOSED,
    source: Any = RegionSource.MODEL,
    question_id: Any = None,
    question_part: Any = None,
    metadata: Optional[Dict[str, Any]] = None,
    page_count: Optional[int] = None,
    region_id: Optional[str] = None,
) -> Region:
    """Turn a candidate region into a validated `Region`, or raise.

    Everything a provider could get wrong is checked here, deterministically,
    before anything is persisted: the page must exist, the type must be in the
    vocabulary, the geometry must be drawable and inside the page, and the
    reading order must be an integer.

    What is NOT done here, deliberately: inventing a question assignment. A
    region with no confident assignment stays unassigned. Guessing would put a
    student's working under the wrong question, which is worse than leaving a
    human to place it.
    """
    if isinstance(page_index, bool) or not isinstance(page_index, int):
        raise InvalidRegionError("page_invalid", "page_index must be an integer", region_id=region_id)
    if page_index < 0:
        raise InvalidRegionError("page_invalid", "page_index must not be negative", region_id=region_id)
    if page_count is not None and page_index >= page_count:
        raise InvalidRegionError(
            "page_out_of_range",
            f"page_index {page_index} is beyond the document's {page_count} page(s)",
            region_id=region_id,
        )

    if region_type not in ALLOWED_REGION_TYPES:
        raise InvalidRegionError(
            "region_type_unknown", f"unknown region type {region_type!r}", region_id=region_id
        )
    if status not in RegionStatus.ALL:
        raise InvalidRegionError("status_unknown", f"unknown status {status!r}", region_id=region_id)
    if source not in RegionSource.ALL:
        raise InvalidRegionError("source_unknown", f"unknown source {source!r}", region_id=region_id)

    if isinstance(reading_order, bool) or not isinstance(reading_order, int):
        raise InvalidRegionError(
            "reading_order_invalid", "reading_order must be an integer", region_id=region_id
        )
    if reading_order < 0:
        raise InvalidRegionError(
            "reading_order_invalid", "reading_order must not be negative", region_id=region_id
        )

    if question_id is not None:
        if isinstance(question_id, bool) or not isinstance(question_id, int):
            raise InvalidRegionError(
                "question_invalid", "question_id must be an integer or absent", region_id=region_id
            )
    if question_part is not None:
        if not isinstance(question_part, str) or not question_part.strip():
            raise InvalidRegionError(
                "question_part_invalid",
                "question_part must be a non-empty string or absent",
                region_id=region_id,
            )
        question_part = question_part.strip()[:32]

    normalised = normalise_geometry(geometry_kind, geometry, region_id=region_id)

    return Region(
        page_index=page_index,
        region_type=region_type,
        geometry_kind=geometry_kind,
        geometry=normalised,
        reading_order=reading_order,
        status=status,
        source=source,
        question_id=question_id,
        question_part=question_part,
        metadata=dict(metadata or {}),
    )


def assign_reading_order(regions: Sequence[Region]) -> List[Region]:
    """Renumber a sequence 0..n-1 in its current order.

    Deterministic and derived from application state, not asked of a model.
    Used after an explicit reorder so the stored ordinals stay dense.
    """
    from dataclasses import replace

    return [replace(region, reading_order=index) for index, region in enumerate(regions)]
