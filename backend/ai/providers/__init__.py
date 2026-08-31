"""Provider adapters. The only place in the repository allowed to import an SDK.

`get_provider(name)` is the seam a second provider plugs into: add a module,
register it here, point a task's `CG_AI__<TASK>__PROVIDER` at it. Nothing above
this package changes.
"""

from backend.ai.providers.base import TextTaskProvider

_REGISTRY = {}


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


def register_provider(name: str, provider: TextTaskProvider) -> None:
    """Install a provider explicitly. Used by tests and by future adapters."""
    _REGISTRY[name] = provider


def reset_providers() -> None:
    """Drop cached adapters. Tests use this; nothing in production should."""
    _REGISTRY.clear()


__all__ = ["TextTaskProvider", "get_provider", "register_provider", "reset_providers"]
