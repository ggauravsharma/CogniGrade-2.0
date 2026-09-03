"""Where uploaded files live, and the one rule about leaving that place.

`UPLOAD_ROOT` was previously defined only inside `backend/auth/files.py`, the
router that SERVES files. Deleting files needs the same boundary, and a second
definition of a security root is exactly the kind of duplication that drifts,
so it moved here and the serving router imports it. Nothing else changed about
serving.

`resolve_within_root` answers one question -- "is this stored path really inside
the upload root?" -- and answers it by RESOLVING first, so `../` segments and
symlinks are collapsed before the comparison rather than after. It returns
`None` instead of raising: a caller deleting files wants to skip a suspicious
path and carry on, not turn a 200 into a 500. The serving path keeps its own
HTTP-shaped wrapper.

No model, no session, no FastAPI import. This is filesystem policy and nothing
else.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

#: Everything the application stores lives under here. Resolved once at import
#: so that a later `chdir` cannot move the goalposts.
UPLOAD_ROOT = Path("./uploads").resolve()


def resolve_within_root(stored_path: Optional[str], *, root: Path = UPLOAD_ROOT) -> Optional[Path]:
    """The real path for `stored_path` if it is inside `root`, else `None`.

    Applied to values coming from the DATABASE, never straight from a request.
    A path that will not resolve, sits on another drive, or escapes the root is
    refused. Existence is NOT checked here -- callers differ on whether a
    missing file is a problem.
    """
    if not stored_path or not str(stored_path).strip():
        return None

    try:
        candidate = Path(str(stored_path)).resolve()
    except (OSError, ValueError):
        logger.warning("unresolvable stored file path rejected")
        return None

    try:
        common = os.path.commonpath([str(candidate), str(root)])
    except ValueError:
        # Different drives on Windows, or otherwise incomparable.
        logger.warning("stored file path outside upload root rejected")
        return None

    if common != str(root):
        logger.warning("stored file path escaped upload root; refusing to touch it")
        return None

    return candidate


def delete_files_within_root(
    stored_paths: Iterable[str], *, root: Path = UPLOAD_ROOT
) -> "tuple[int, int]":
    """Delete the paths that are genuinely inside `root`. Returns `(deleted, skipped)`.

    Never raises. Cleanup runs AFTER the database transaction has committed, so
    a file that cannot be removed must not turn a completed deletion into an
    error -- the rows are already gone and re-raising would only report a
    failure that did not happen. Failures are counted and logged without the
    path, because an uploaded filename can name a student.

    Directories are never removed: this deletes files it was given, and does not
    walk or prune the tree.
    """
    deleted = skipped = 0
    for stored in stored_paths:
        target = resolve_within_root(stored, root=root)
        if target is None:
            skipped += 1
            continue
        try:
            if target.is_file():
                target.unlink()
                deleted += 1
            else:
                skipped += 1
        except OSError:
            # Locked, already gone, permissions. Counted, never fatal.
            skipped += 1
            logger.warning("could not remove a stored file during cleanup")
    return deleted, skipped
