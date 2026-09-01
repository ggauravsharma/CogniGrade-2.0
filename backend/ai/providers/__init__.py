"""Provider adapters. The only place in the repository allowed to import an SDK.

`get_provider(name)` is the seam a second provider plugs into: add a module,
register it here, point a task's `CG_AI__<TASK>__PROVIDER` at it. Nothing above
this package changes.
"""

import logging

from backend.ai.contracts import AITask
from backend.ai.config import NO_PROVIDER, get_task_settings
from backend.ai.errors import ProviderNotConfiguredError
from backend.ai.providers.base import TextTaskProvider

logger = logging.getLogger(__name__)

_REGISTRY = {}

#: Adapters that exist only to exercise the pipeline without a model. They are
#: never inference engines, so a deployment that resolves one is running a
#: stand-in against real student work. Named here so the resolver can say so
#: out loud instead of leaving it to whoever reads the region rows later.
NON_PRODUCTION_PROVIDERS = ("fake",)


def get_provider(name: str) -> TextTaskProvider:
    """Return the adapter for `name`, constructing it on first use.

    Imported lazily so that a deployment which never touches Gemini -- a test
    run, a future local-model-only install -- does not pay for its SDK import.
    """
    if name in _REGISTRY:
        return _REGISTRY[name]

    if name == "gemini":
        from backend.ai.providers.gemini import GeminiProvider

        _REGISTRY[name] = GeminiProvider()
        return _REGISTRY[name]

    raise ValueError(f"unknown AI provider: {name!r}")


_SEGMENTATION_REGISTRY = {}


def get_segmentation_provider(name: str):
    """Return the segmentation adapter for `name`.

    Separate from `get_provider` because segmentation has a different
    capability shape (regions in, not text) -- squeezing it into the text
    interface would have been the generic-`generate()` mistake in another form.

    There is deliberately NO default: no production segmentation provider
    exists yet, and quietly falling back to the fake would put test output in
    front of a teacher.

    Lookup by name only. A ROUTE must call `resolve_segmentation_provider`
    instead, so which adapter runs stays a deployment decision rather than
    something a request body can state.
    """
    if name in _SEGMENTATION_REGISTRY:
        return _SEGMENTATION_REGISTRY[name]

    if name == "fake":
        from backend.ai.providers.fake_segmentation import FakeSegmentationProvider

        _SEGMENTATION_REGISTRY[name] = FakeSegmentationProvider()
        return _SEGMENTATION_REGISTRY[name]

    raise ValueError(f"unknown segmentation provider: {name!r}")


def resolve_segmentation_provider():
    """The segmentation adapter THIS DEPLOYMENT is configured to use.

    Provider choice is application configuration, never a request field. A
    caller asks for the segmentation TASK; which model performs it is settled
    by `CG_AI__SEGMENTATION__PROVIDER` at deploy time, in the same one place
    every other task's provider lives.

    That is not a stylistic preference. When the request body chose the
    adapter, its default was the development double, so an ordinary production
    call to a real answer script wrote synthetic regions onto a real student's
    paper -- output that is indistinguishable, once stored, from a model's.

    Raises `ProviderNotConfiguredError` when nothing is configured or the
    configured name has no adapter. No fallback: there is no segmentation
    result that is better than an explicit refusal to produce one.
    """
    name = (get_task_settings(AITask.SEGMENTATION).provider or NO_PROVIDER).strip()
    if name == NO_PROVIDER:
        raise ProviderNotConfiguredError(
            "no segmentation provider is configured for this deployment",
            provider="unset",
        )
    try:
        provider = get_segmentation_provider(name)
    except ValueError as exc:
        raise ProviderNotConfiguredError(str(exc), provider=name) from exc

    if name in NON_PRODUCTION_PROVIDERS:
        # Reachable only by an operator naming it in configuration, which is a
        # deliberate act -- but a silent one, so it is logged every time.
        logger.warning(
            "segmentation is configured to use the NON-PRODUCTION provider %r; "
            "the regions it proposes are synthetic and must not be trusted",
            name,
        )
    return provider


def register_segmentation_provider(name: str, provider) -> None:
    _SEGMENTATION_REGISTRY[name] = provider


def register_provider(name: str, provider: TextTaskProvider) -> None:
    """Install a provider explicitly. Used by tests and by future adapters."""
    _REGISTRY[name] = provider


def reset_providers() -> None:
    """Drop cached adapters. Tests use this; nothing in production should."""
    _REGISTRY.clear()
    _SEGMENTATION_REGISTRY.clear()


__all__ = [
    "TextTaskProvider", "get_provider", "register_provider", "reset_providers",
    "get_segmentation_provider", "register_segmentation_provider",
    "resolve_segmentation_provider", "NON_PRODUCTION_PROVIDERS",
]
