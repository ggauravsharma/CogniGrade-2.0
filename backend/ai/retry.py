"""Bounded retry for transient provider failures.

Retries only what a retry can fix. `ProviderError.retryable` decides -- set on
the error class by the adapter that raised it, never guessed here and never
guessed at a call site, so "is a 429 worth retrying" is answered in exactly one
place.

Explicitly NOT retried: authentication (a wrong key stays wrong), invalid
request (the same wrong request would be sent again), and an unusable response
(re-rolling a model that answered badly is a product decision, not a transport
one -- and the grading path already reports that as a validated failure).

Backoff is exponential with full jitter and a per-step ceiling: a rate-limit
storm must not turn one request into a minute of sleeping, and synchronised
retries across concurrent gradings must not arrive in a thundering herd.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Awaitable, Callable, Optional, TypeVar

from backend.ai.config import TaskSettings
from backend.ai.errors import ProviderError

logger = logging.getLogger(__name__)

T = TypeVar("T")


def compute_delay(attempt: int, settings: TaskSettings, *, rng: Optional[random.Random] = None) -> float:
    """Full-jitter exponential backoff for `attempt` (1 = after the first try)."""
    raw = settings.retry_base_delay * (2 ** max(0, attempt - 1))
    capped = min(raw, settings.retry_max_delay)
    source = rng or random
    return source.uniform(0.0, capped)


async def run_with_retries(
    operation: Callable[[], Awaitable[T]],
    *,
    settings: TaskSettings,
    on_attempt: Optional[Callable[[int], None]] = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    rng: Optional[random.Random] = None,
) -> "tuple[T, int]":
    """Call `operation` until it succeeds or the budget runs out.

    Returns `(result, attempts_used)`. Re-raises the LAST provider error when
    every attempt fails, so the caller reports the failure that actually ended
    the sequence rather than the first one.

    `sleep` and `rng` are injectable so the tests can assert the retry
    behaviour without spending real seconds.
    """
    attempts = 0
    last_error: Optional[ProviderError] = None
    total = max(0, settings.max_retries) + 1

    while attempts < total:
        attempts += 1
        if on_attempt is not None:
            on_attempt(attempts)
        try:
            return await operation(), attempts
        except ProviderError as exc:
            last_error = exc
            if not exc.retryable:
                logger.warning(
                    "provider error not retryable: task=%s category=%s attempt=%s",
                    settings.task, exc.category, attempts,
                )
                raise
            if attempts >= total:
                break
            delay = compute_delay(attempts, settings, rng=rng)
            logger.warning(
                "provider error, retrying: task=%s category=%s attempt=%s/%s delay=%.2fs",
                settings.task, exc.category, attempts, total, delay,
            )
            await sleep(delay)

    assert last_error is not None  # only reachable after a retryable failure
    logger.error(
        "provider retries exhausted: task=%s category=%s attempts=%s",
        settings.task, last_error.category, attempts,
    )
    raise last_error
