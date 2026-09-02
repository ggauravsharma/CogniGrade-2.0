"""Turning an uploaded document into what a reader actually sees.

Document-reading tasks used to hand the provider the uploaded FILE. For a PDF
that means the provider gets the PDF, and a PDF carries more than its pages
show: text painted invisibly, text covered by something drawn over it, and
objects an editor "removed" without deleting are all still in the file, and all
still come back from any native text reader.

That is not hypothetical. A five-question paper (31, 32, 36, 37, 39) produced by
cutting questions out of a full paper rendered exactly those five -- the band on
page 1 where question 33 had been contains ZERO non-white pixels -- while its
text layer still held questions 33 and 38 in full, with MediaBox, CropBox and
TrimBox all identical, so nothing was outside any crop. CogniGrade extracted
seven questions. The two nobody could see were about to be graded.

So a document task is given the PAGES, not the file. Rendering collapses
everything a page does not show -- invisible text, white-on-white, covered
objects, content outside the crop -- into the single representation the user
approved, which is the one an AI-first system has to reason about. It is the
same operation the region evidence path already performs, with the same library
and the same scale (`backend/regions/cropping.py`).

Deliberately NOT a native text-extraction step. Reading the PDF's own text and
filtering it by visibility would be a second document-understanding engine, in
the layer that exists precisely so there is only one.

PROVIDER-NEUTRAL BY CONSTRUCTION. This hands back local file paths, which is
what `FilePart` has always carried. No task, route, schema, prompt or stored
format changes, and no provider learns anything new.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import tempfile
from typing import AsyncIterator, List, Sequence

logger = logging.getLogger(__name__)

#: Matches `backend/regions/cropping.py`. Deliberately the same number: a page
#: legible enough to cut evidence out of is legible enough to read questions
#: off, and two rendering scales that drift apart is a bug waiting to be filed.
PAGE_RENDER_SCALE = 2.0

#: Already one visible page each. Nothing to render, nothing to hide behind.
RASTER_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff")

#: A page count above this is a sign the wrong file was uploaded, and rendering
#: it would blow the provider's inline ceiling long before it finished. Refused
#: with a named error rather than sent as a truncated document.
MAX_RENDERED_PAGES = 60


class DocumentNormalisationError(RuntimeError):
    """A document could not be reduced to its visible pages.

    Raised rather than falling back to sending the original file. A fallback
    would silently restore exactly the failure this module exists to prevent,
    and it would do it on the paper least likely to be checked by hand.
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _is_raster(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in RASTER_SUFFIXES


def render_visible_pages(document_path: str, out_dir: str) -> List[str]:
    """Rasterise every page of `document_path` into `out_dir`. Blocking.

    Returns one image path per page, in page order. A raster source is already
    a single visible page and is returned unchanged, so nothing is copied and
    nothing is re-encoded.
    """
    if not document_path:
        raise DocumentNormalisationError("source_missing", "no document path was given")
    if not os.path.exists(document_path):
        raise DocumentNormalisationError(
            "source_missing", "the document is not on disk"
        )
    if _is_raster(document_path):
        return [document_path]

    try:
        import pypdfium2 as pdfium
    except ImportError as exc:
        # Named, not a crash. A deployment without a PDF backend must be told
        # what is missing -- and must NOT quietly go back to sending the file.
        raise DocumentNormalisationError(
            "page_render_unavailable", "no PDF rendering backend is installed"
        ) from exc

    try:
        document = pdfium.PdfDocument(document_path)
    except Exception as exc:  # noqa: BLE001 - pdfium raises assorted types
        raise DocumentNormalisationError(
            "document_unreadable", f"could not open the document ({type(exc).__name__})"
        ) from exc

    try:
        page_count = len(document)
        if page_count == 0:
            raise DocumentNormalisationError("document_empty", "the document has no pages")
        if page_count > MAX_RENDERED_PAGES:
            raise DocumentNormalisationError(
                "document_too_long",
                f"{page_count} pages exceeds the {MAX_RENDERED_PAGES}-page limit",
            )

        paths: List[str] = []
        for index in range(page_count):
            target = os.path.join(out_dir, f"page-{index + 1:04d}.png")
            try:
                image = document[index].render(scale=PAGE_RENDER_SCALE).to_pil()
                image.convert("RGB").save(target, format="PNG", optimize=True)
            except Exception as exc:  # noqa: BLE001
                raise DocumentNormalisationError(
                    "page_render_failed",
                    f"could not render page {index + 1} ({type(exc).__name__})",
                ) from exc
            paths.append(target)
        return paths
    finally:
        with contextlib.suppress(Exception):
            document.close()


@contextlib.asynccontextmanager
async def visible_pages(document_path: str) -> AsyncIterator[Sequence[str]]:
    """The document's visible pages, as local image paths, for one call.

    Rendering runs off the event loop. The temporary directory is removed on
    the way out whether the call succeeded or not, so a failed provider call
    cannot leave page images of an exam paper on disk. A raster source is
    yielded as-is and no directory is created for it.
    """
    if _is_raster(document_path):
        yield [document_path]
        return

    workspace = tempfile.mkdtemp(prefix="cognigrade-pages-")
    try:
        paths = await asyncio.to_thread(render_visible_pages, document_path, workspace)
        # Path count only. A page image of an exam paper is not log material.
        logger.info("document normalised to %d visible page image(s)", len(paths))
        yield paths
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
