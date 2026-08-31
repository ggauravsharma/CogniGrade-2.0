"""Bounded concurrency for AI work, with per-item failure isolation.

`asyncio.gather` is the wrong primitive here twice over:

    it is UNBOUNDED -- forty questions become forty simultaneous provider calls,
    which is a rate-limit storm and, on a metered API, a bill;

    its default failure mode CANCELS the siblings -- one question failing would
    abandon the rest of the paper, which is exactly the behaviour Correctness
    v3 exists to prevent.

`run_bounded` fixes both: at most `limit` coroutines are in flight, and every
item gets its own outcome regardless of what the others did.

DETERMINISTIC ORDER
-------------------
Results come back in INPUT order, never completion order. Concurrency is a
performance detail; it must not reach the domain, the API or the logs. A
professor reading "failed: Q2, Q5" should see the same list on every run.

DELIBERATELY SMALL
------------------
A local semaphore, no Redis, no distributed coordination, no adaptive
controller, no circuit breaker. One process grading one student's paper is the
unit of work, and a semaphore is the right size for it. The semaphore is
created PER CALL, never at module scope, so it binds to whichever event loop is
running -- which matters because Celery drives this through
`loop.run_until_complete`, not a long-lived server loop.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Generic, List, Optional, Sequence, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")
R = TypeVar("R")


@dataclass
class Outcome(Generic[R]):
    """What happened to one item. Exactly one of `value` / `error` is set."""

    index: int
    item: Any
    value: Optional[R] = None
    error: Optional[BaseException] = None

    @property
    def ok(self) -> bool:
        return self.error is None


async def run_bounded(
    items: Sequence[T],
    worker: Callable[[T], Awaitable[R]],
    *,
    limit: int,
    label: str = "task",
) -> List[Outcome]:
    """Run `worker(item)` over `items`, at most `limit` at a time.

    Returns one `Outcome` per item, IN INPUT ORDER. An item that raises is
    recorded, not propagated: the caller decides what a failure means, and the
    other items are unaffected.

    `limit <= 1` runs strictly sequentially -- the same shape as the loop this
    replaces, which is what makes `max_concurrency = 1` an exact restoration of
    the previous behaviour rather than an approximation of it.

    Cancellation of the caller propagates: `asyncio.gather` cancels the pending
    children, and the `finally` in each provider call still cleans up its
    uploads. No task is left running behind a cancelled request.
    """
    if not items:
        return []

    bound = max(1, int(limit))

    if bound == 1:
        outcomes: List[Outcome] = []
        for index, item in enumerate(items):
            try:
                outcomes.append(Outcome(index=index, item=item, value=await worker(item)))
            except asyncio.CancelledError:
                raise
            except BaseException as exc:  # noqa: BLE001 - isolation is the point
                outcomes.append(Outcome(index=index, item=item, error=exc))
        return outcomes

    # Created here, not at module scope: a Semaphore binds to the running loop,
    # and Celery gives this a different loop from a web request.
    semaphore = asyncio.Semaphore(bound)

    async def _guarded(index: int, item: T) -> Outcome:
        async with semaphore:
            try:
                return Outcome(index=index, item=item, value=await worker(item))
            except asyncio.CancelledError:
                raise
            except BaseException as exc:  # noqa: BLE001
                return Outcome(index=index, item=item, error=exc)

    logger.debug("running %s items of %s with concurrency %s", len(items), label, bound)
    # return_exceptions=True is belt and braces: _guarded already catches, but a
    # failure to even schedule must not cancel the siblings either.
    gathered = await asyncio.gather(
        *(_guarded(i, item) for i, item in enumerate(items)),
        return_exceptions=True,
    )

    results: List[Outcome] = []
    for index, entry in enumerate(gathered):
        if isinstance(entry, Outcome):
            results.append(entry)
        elif isinstance(entry, BaseException):
            results.append(Outcome(index=index, item=items[index], error=entry))
        else:  # pragma: no cover - gather returns one or the other
            results.append(Outcome(index=index, item=items[index], value=entry))

    results.sort(key=lambda outcome: outcome.index)
    return results
