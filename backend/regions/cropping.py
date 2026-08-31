"""Turn a region's geometry into an image, from the original page.

The inversion this module exists for: a crop is a DERIVED artefact, generated
on demand from the page plus the geometry, rather than the only surviving
record of an annotation. Nothing here writes to `uploads/`; every crop lands in
a temporary directory that the caller deletes in a `finally`.

PROVIDER-NEUTRAL
----------------
No SDK, no FastAPI, no SQLAlchemy (asserted by a test). It renders a page and
cuts a shape out of it -- equally useful to a grading call, a future HTR model,
or a human previewing what a region contains.

NOTHING IS DRAWN ONTO THE PIXELS
--------------------------------
No question number, no part label, no reading order, no region type. Those are
columns. The legacy workflow burned them into the image and asked a model to
read them back, which is the dependency this whole line of work removes.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from typing import Any, Dict, List, Optional, Sequence, Tuple

from backend.regions.schema import GeometryKind, geometry_bounds

logger = logging.getLogger(__name__)

#: Raster suffixes Pillow can open directly. Anything else is treated as a
#: document that needs rendering.
RASTER_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff")

#: Pixels per PDF point when rasterising. 2.0 gives roughly 144 dpi -- enough
#: for handwriting to survive a crop without producing images so large that
#: uploading them dominates grading latency.
PDF_RENDER_SCALE = 2.0


class RegionEvidenceError(Exception):
    """A region's image could not be produced.

    Carries a machine-readable `code`, matching the convention of
    `GradingResponseError`, `InvalidMarkError` and `InvalidRegionError`, so a
    caller can record WHY preparation failed without parsing message text.

    Critically this is a PREPARATION failure, not a grade: a caller must record
    it as a missing mark, never as a zero (audit C6).
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class PageRenderer:
    """Renders pages of one source document, caching what it has rendered.

    The cache is per instance and therefore per grading invocation: rendering
    page 3 once and cutting four regions out of it is the common case, and
    re-rasterising for each would be the obvious waste. There is deliberately
    no global or cross-request cache -- that would need invalidation nobody has
    designed, and would hold student page images in memory indefinitely.
    """

    def __init__(self, source_path: str):
        self.source_path = source_path
        self._pages: Dict[int, Any] = {}
        self._page_count: Optional[int] = None
        #: How many times a page was actually rasterised, for the cache test.
        self.render_count = 0

    # -- page access -------------------------------------------------------
    def _open_raster(self):
        from PIL import Image

        try:
            image = Image.open(self.source_path)
            image.load()
            return image.convert("RGB")
        except Exception as exc:  # noqa: BLE001 - Pillow raises assorted types
            raise RegionEvidenceError(
                "page_render_failed", f"could not open page image ({type(exc).__name__})"
            )

    def _open_pdf_page(self, page_index: int):
        try:
            import pypdfium2 as pdfium
        except ImportError:
            # Explicit, not a crash: a deployment without a PDF backend must
            # report a preparation failure the caller can record, and fall back
            # to whatever legacy evidence exists.
            raise RegionEvidenceError(
                "page_render_unavailable",
                "no PDF rendering backend is installed",
            )

        try:
            document = pdfium.PdfDocument(self.source_path)
        except Exception as exc:  # noqa: BLE001
            raise RegionEvidenceError(
                "page_render_failed", f"could not open the document ({type(exc).__name__})"
            )

        try:
            self._page_count = len(document)
            if page_index >= self._page_count:
                raise RegionEvidenceError(
                    "page_out_of_range",
                    f"page {page_index} of a {self._page_count}-page document",
                )
            page = document[page_index]
            bitmap = page.render(scale=PDF_RENDER_SCALE)
            return bitmap.to_pil().convert("RGB")
        except RegionEvidenceError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise RegionEvidenceError(
                "page_render_failed", f"could not render the page ({type(exc).__name__})"
            )
        finally:
            document.close()

    def page(self, page_index: int):
        """The rendered page as a PIL image. Cached per index."""
        if not self.source_path:
            raise RegionEvidenceError("source_missing", "no source document recorded")
        if page_index < 0:
            raise RegionEvidenceError("page_out_of_range", "page index must not be negative")
        if page_index in self._pages:
            return self._pages[page_index]
        if not os.path.exists(self.source_path):
            raise RegionEvidenceError("source_missing", "the source document is not on disk")

        suffix = os.path.splitext(self.source_path)[1].lower()
        if suffix in RASTER_SUFFIXES:
            if page_index != 0:
                # A single image is one page by definition; asking for page 4
                # of a PNG is a bug upstream, not something to silently clamp.
                raise RegionEvidenceError(
                    "page_out_of_range", "an image source has only page 0"
                )
            image = self._open_raster()
        else:
            image = self._open_pdf_page(page_index)

        self.render_count += 1
        self._pages[page_index] = image
        return image

    def close(self) -> None:
        for image in self._pages.values():
            try:
                image.close()
            except Exception:  # noqa: BLE001 - closing must never raise
                pass
        self._pages.clear()


def _pixel_box(bounds: Dict[str, float], width: int, height: int) -> Tuple[int, int, int, int]:
    """Normalised bounds -> integer pixel box, clamped inside the page.

    This is the one place normalised coordinates become pixels, which is why
    the conversion is independent of browser size, device pixel ratio and
    viewer zoom: those never enter the stored value in the first place.
    """
    left = int(round(bounds["x"] * width))
    top = int(round(bounds["y"] * height))
    right = int(round((bounds["x"] + bounds["w"]) * width))
    bottom = int(round((bounds["y"] + bounds["h"]) * height))

    left = max(0, min(left, width - 1))
    top = max(0, min(top, height - 1))
    right = max(left + 1, min(right, width))
    bottom = max(top + 1, min(bottom, height))
    return left, top, right, bottom


def crop_region(renderer: PageRenderer, *, page_index: int, geometry_kind: str, geometry: Dict[str, Any]):
    """Cut one region out of its page and return a PIL image.

    POLYGON BEHAVIOUR, stated explicitly: the crop is the polygon's bounding
    box, with everything OUTSIDE the polygon painted white. That keeps the
    result a plain rectangular image every downstream consumer can handle,
    while removing neighbouring content that a bounding box alone would drag
    in -- which matters most for the case polygons exist for, a diagram drawn
    at an angle beside unrelated working. White rather than transparent because
    the graders and OCR/HTR systems this feeds expect an opaque page-like
    background, and an alpha channel would be flattened to black by some of
    them.
    """
    page = renderer.page(page_index)
    width, height = page.size
    bounds = geometry_bounds(geometry_kind, geometry)
    box = _pixel_box(bounds, width, height)
    cropped = page.crop(box)

    if geometry_kind != GeometryKind.POLYGON:
        return cropped

    from PIL import Image, ImageDraw

    left, top, right, bottom = box
    mask = Image.new("L", (right - left, bottom - top), 0)
    points = [
        (
            round(px * width) - left,
            round(py * height) - top,
        )
        for px, py in geometry["points"]
    ]
    ImageDraw.Draw(mask).polygon(points, fill=255)

    backdrop = Image.new("RGB", cropped.size, (255, 255, 255))
    backdrop.paste(cropped, (0, 0), mask)
    return backdrop


class CropWorkspace:
    """Temporary crop files for ONE grading invocation.

    A context manager because the guarantee that matters is deletion: crops are
    written to a private temporary directory and the whole directory is removed
    on exit, on success and on failure alike. Nothing is ever written into
    `uploads/`, so a failed grading run cannot leave orphans behind.
    """

    def __init__(self, prefix: str = "cg-region-"):
        self._prefix = prefix
        self._directory: Optional[str] = None
        self.paths: List[str] = []

    def __enter__(self) -> "CropWorkspace":
        self._directory = tempfile.mkdtemp(prefix=self._prefix)
        return self

    def __exit__(self, *exc_info) -> bool:
        self.cleanup()
        return False

    @property
    def directory(self) -> str:
        if self._directory is None:
            raise RuntimeError("CropWorkspace used outside its context manager")
        return self._directory

    def write(self, image, *, name: str) -> str:
        """Save one crop and remember it for cleanup."""
        path = os.path.join(self.directory, f"{name}.png")
        try:
            image.save(path, format="PNG")
        except Exception as exc:  # noqa: BLE001
            raise RegionEvidenceError(
                "crop_write_failed", f"could not write a region crop ({type(exc).__name__})"
            )
        self.paths.append(path)
        return path

    def cleanup(self) -> None:
        if self._directory and os.path.isdir(self._directory):
            shutil.rmtree(self._directory, ignore_errors=True)
        self._directory = None
        self.paths = []
