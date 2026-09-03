"""One structured line per provider invocation.

Enough to answer "why was that slow / why did that fail / which model and
prompt produced this", and nothing more. Application logs, not a database
table: a metrics store is a later decision and an unnecessary dependency for
the grading path to carry today.

WHAT IS DELIBERATELY ABSENT
---------------------------
No API key. No student answer text. No marking scheme. No file contents. No
raw provider response. Those are the things that make a log file a data-breach
surface, and a grading system's logs would otherwise be full of them. File
paths are reduced to a count, because a path can name a student.

`finish_reason` and `output_tokens` are here for one reason: a response cut off
at an output limit still returns its partial text, so it reaches the strict
grading decoder as invalid JSON and is recorded as `malformed_json` -- the same
code a model that simply wrote nonsense would get. The provider knew which it
was. A normalised reason and a token COUNT tell them apart without a single
character of the response ever being logged.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def log_invocation(
    *,
    task: str,
    provider: str,
    model: str,
    prompt_version: str,
    duration_ms: int,
    attempts: int,
    success: bool,
    error_category: Optional[str] = None,
    file_count: int = 0,
    finish_reason: Optional[str] = None,
    output_tokens: Optional[int] = None,
    exam_id: Optional[int] = None,
    student_id: Optional[int] = None,
    question_id: Optional[int] = None,
) -> None:
    """Record one completed provider call, successful or not."""
    payload = (
        "ai_invocation task=%s provider=%s model=%s prompt_version=%s "
        "duration_ms=%s attempts=%s success=%s error_category=%s files=%s "
        "finish_reason=%s output_tokens=%s "
        "exam_id=%s student_id=%s question_id=%s"
    )
    args = (
        task, provider, model, prompt_version, duration_ms, attempts,
        success, error_category or "-", file_count,
        finish_reason or "-",
        output_tokens if output_tokens is not None else "-",
        exam_id if exam_id is not None else "-",
        student_id if student_id is not None else "-",
        question_id if question_id is not None else "-",
    )
    if success:
        logger.info(payload, *args)
    else:
        logger.error(payload, *args)
