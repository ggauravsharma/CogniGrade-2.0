"""HTTP adapter for a mark arriving from a client.

`backend/grading/marks.py` decides what a mark is; it is deliberately free of
FastAPI so that a future non-HTTP grading path (a batch job, a human-grading
worker, an ensemble reconciler) can reuse it unchanged. This module is the thin
layer that turns its rejection into the right status code.

It matters here because the professor-facing UI posts marks as **strings**: the
manual-edit control sends `marksInput.value`, and the re-grade prompt sends
whatever was typed. Before this, that string went straight onto the ORM
attribute, which meant a typo reached the database driver and surfaced as a 500
long after the request looked fine.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import HTTPException, status

from backend.grading.marks import InvalidMarkError, to_number


def parse_mark_input(value: Any, *, field: str = "grade") -> Optional[float]:
    """Normalise a client-supplied mark, or raise 400.

    `None` and `""` mean "no mark" and pass through as `None` -- clearing a
    mark is a legitimate action, and the caller decides whether it is allowed
    here. `0` is a mark and is preserved as one.
    """
    try:
        return to_number(value)
    except InvalidMarkError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field}: {exc.message}",
        )
