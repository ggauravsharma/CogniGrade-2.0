"""The contract every provider adapter satisfies.

One method, because every CogniGrade AI task today is "here is a prompt and
some local files, give me text back". That is an observation about the current
system, not a claim about the future: when a segmentation provider arrives it
returns regions rather than text and gets its own capability here, alongside
this one rather than crammed into it.

What makes this NOT a generic `generate(prompt)`:

* the request carries a TASK, so configuration, model choice, prompt version
  and telemetry all have something to key on;
* the caller passes LOCAL paths, never provider file handles, so no upload can
  escape the adapter uncleaned;
* every failure is a `ProviderError`, never an SDK exception.
"""

from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

from backend.ai.config import TaskSettings
from backend.ai.contracts import ProviderRequest, ProviderResponse


@runtime_checkable
class TextTaskProvider(Protocol):
    """A provider that turns a prompt plus optional files into text."""

    #: Stable identifier, matching the value tasks configure as `provider`.
    name: str

    async def run_text_task(
        self,
        request: ProviderRequest,
        settings: TaskSettings,
        *,
        timeout_seconds: Optional[float] = None,
    ) -> ProviderResponse:
        """Execute one attempt. Raise `ProviderError` on any failure.

        One ATTEMPT: retrying is `backend.ai.retry`'s job, so an adapter never
        has to reimplement backoff and a future adapter cannot get it wrong.
        """
        ...
