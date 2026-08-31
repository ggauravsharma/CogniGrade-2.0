"""Which provider and model run each task, and how hard they may try.

Configuration is per TASK, not global. Today every task points at the same
Gemini model, and that is fine -- what matters is that the model string lives
here instead of in a route, so pointing recognition at a specialist HTR model
later is an environment change rather than a code change.

    CG_AI__GRADING__MODEL=some-other-model
    CG_AI__ANSWER_RECOGNITION__MODEL=a-specialist-htr-model
    CG_AI__GRADING__TIMEOUT_SECONDS=90

Environment only. No database table: these are deployment knobs, and a DB
round-trip to discover which model to call would be a strange dependency for
the grading path to carry.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from typing import Dict, Optional

from backend.ai.contracts import AITask

#: The provider every task uses today. One provider, named in one place.
DEFAULT_PROVIDER = "gemini"

#: The model in production before this phase, kept verbatim. Changing it is a
#: benchmarking decision, not a refactor -- see the context file.
DEFAULT_MODEL = "gemini-2.0-flash"

#: Prefix for every per-task environment override.
ENV_PREFIX = "CG_AI"


@dataclass(frozen=True)
class TaskSettings:
    """Everything one task needs to invoke a provider."""

    task: str
    provider: str = DEFAULT_PROVIDER
    model: str = DEFAULT_MODEL
    #: Wall-clock budget for a single attempt.
    timeout_seconds: float = 120.0
    #: Additional attempts after the first. 2 means up to 3 calls.
    max_retries: int = 2
    #: First backoff step; doubles per attempt, with jitter.
    retry_base_delay: float = 1.0
    #: Ceiling on any single backoff, so a 429 storm cannot stall a request.
    retry_max_delay: float = 15.0
    #: Deterministic by default: grading a paper twice should not drift.
    temperature: float = 0.0
    #: Ask for JSON where the provider supports it.
    expects_json: bool = False

    def with_overrides(self, **kwargs) -> "TaskSettings":
        return replace(self, **kwargs)


#: Per-task defaults. Only where a task genuinely differs from the base.
_TASK_DEFAULTS: Dict[str, Dict[str, object]] = {
    # Grading is the one task whose output is parsed strictly, so it asks for
    # JSON and pins temperature to 0.
    AITask.GRADING: {"expects_json": True, "temperature": 0.0},
    # Document reads are long; a whole question paper takes longer than one
    # answer image.
    AITask.DOCUMENT_EXTRACTION: {"timeout_seconds": 180.0},
    AITask.LABEL_EXTRACTION: {"timeout_seconds": 180.0},
    AITask.ANSWER_RECOGNITION: {"timeout_seconds": 120.0},
    AITask.MARKING_SCHEME_RECOGNITION: {"timeout_seconds": 120.0},
}


def _env(task: str, field: str) -> Optional[str]:
    """`CG_AI__GRADING__MODEL` -- task-specific first, then a global fallback."""
    specific = os.getenv(f"{ENV_PREFIX}__{task.upper()}__{field.upper()}")
    if specific is not None:
        return specific
    return os.getenv(f"{ENV_PREFIX}__{field.upper()}")


def _coerce(value: str, template):
    if isinstance(template, bool):
        return value.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(template, int) and not isinstance(template, bool):
        return int(value)
    if isinstance(template, float):
        return float(value)
    return value


def get_task_settings(task: str) -> TaskSettings:
    """Resolve settings for one task: defaults, then per-task, then environment.

    Read on each call rather than cached at import, so a test (or an operator
    restarting a worker with new environment) sees the change without import
    order mattering.
    """
    if task not in AITask.ALL:
        raise ValueError(f"unknown AI task: {task!r}")

    settings = TaskSettings(task=task)
    for field, value in _TASK_DEFAULTS.get(task, {}).items():
        settings = settings.with_overrides(**{field: value})

    overrides = {}
    for field in (
        "provider", "model", "timeout_seconds", "max_retries",
        "retry_base_delay", "retry_max_delay", "temperature", "expects_json",
    ):
        raw = _env(task, field)
        if raw is None:
            continue
        try:
            overrides[field] = _coerce(raw, getattr(settings, field))
        except (TypeError, ValueError):
            # A malformed override must not take the grading path down; the
            # documented default is a safe answer.
            continue
    return settings.with_overrides(**overrides) if overrides else settings


def describe_configuration() -> Dict[str, Dict[str, object]]:
    """Every task's effective settings. For a startup log or an admin view."""
    return {
        task: {
            "provider": s.provider,
            "model": s.model,
            "timeout_seconds": s.timeout_seconds,
            "max_retries": s.max_retries,
            "expects_json": s.expects_json,
        }
        for task in AITask.ALL
        for s in (get_task_settings(task),)
    }
