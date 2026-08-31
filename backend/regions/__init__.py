"""Structured regions of an answer sheet or marking scheme.

The domain contract for "a part of a page that means something". Provider-
neutral by construction: no SDK, no FastAPI, no SQLAlchemy (asserted by a
test), so a segmentation model, a specialist HTR system, a diagram detector or
a human drawing a box all produce the same shape.

WHY THIS EXISTS
---------------
The crop workflow persists only a cropped PNG. Everything structural about it
-- which page it came from, where on that page, whether it is the student's
work or the teacher's red pen, whether it was struck out, what order it should
be read in -- is either discarded or burned into the pixels and recovered later
by asking a model to read a number off an image. That makes the picture the
source of truth for facts the application already knew.

A `Region` keeps those facts as data.
"""

from backend.regions.schema import (
    ALLOWED_REGION_TYPES,
    LEGACY_BUCKET_TO_REGION_TYPE,
    GeometryKind,
    InvalidRegionError,
    Region,
    RegionSource,
    RegionStatus,
    RegionType,
    normalise_geometry,
    validate_region,
)

__all__ = [
    "ALLOWED_REGION_TYPES",
    "LEGACY_BUCKET_TO_REGION_TYPE",
    "GeometryKind",
    "InvalidRegionError",
    "Region",
    "RegionSource",
    "RegionStatus",
    "RegionType",
    "normalise_geometry",
    "validate_region",
]
