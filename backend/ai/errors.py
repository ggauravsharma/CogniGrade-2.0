"""Provider failures, named in terms the application can act on.

An SDK exception must never reach a route or the domain: `google.api_core`
types would make every caller Gemini-shaped, and swapping providers would then
mean rewriting every `except`. The adapter maps whatever its SDK raises onto
the small taxonomy here, and everything above reasons about the categories.

The categories exist to answer exactly two questions:

    is it worth trying again?          -> `retryable`
    whose fault is it?                 -> the class

Nothing more. No error codes per provider, no hierarchy for its own sake.
"""

from __future__ import annotations

from typing import Optional


class ProviderError(Exception):
    """Base for every provider failure the application is allowed to see."""

    #: Whether a bounded retry could plausibly succeed. Set per subclass, not
    #: guessed at the call site.
    retryable = False
    #: Stable machine-readable label used in telemetry and in the grading
    #: failure code. Provider-independent on purpose.
    category = "provider_error"

    def __init__(self, message: str, *, provider: str = "unknown", cause: Optional[BaseException] = None):
        super().__init__(message)
        self.message = message
        self.provider = provider
        self.cause = cause

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"{type(self).__name__}(provider={self.provider!r}, message={self.message!r})"


class ProviderRateLimitError(ProviderError):
    """Quota or rate limit (HTTP 429). Worth retrying, after a wait."""

    retryable = True
    category = "rate_limit"


class ProviderTemporaryError(ProviderError):
    """Service unavailable / internal provider fault (HTTP 5xx). Retryable."""

    retryable = True
    category = "temporary"


class ProviderTimeoutError(ProviderError):
    """The call exceeded its deadline. Retryable: the next one may be quicker."""

    retryable = True
    category = "timeout"


class ProviderAuthenticationError(ProviderError):
    """Bad or missing credentials. Retrying cannot fix a wrong key."""

    retryable = False
    category = "authentication"


class ProviderInvalidRequestError(ProviderError):
    """The request itself is wrong -- bad argument, unsupported input, blocked
    content. Retrying sends the same wrong request again."""

    retryable = False
    category = "invalid_request"


class ProviderResponseError(ProviderError):
    """A response arrived but carried nothing usable.

    NOT retryable here: the grading path already distinguishes "no valid result"
    from "call failed" through `GradingResponseError`, and re-rolling a model
    that answered badly is a product decision, not a transport one.
    """

    retryable = False
    category = "empty_response"


#: Every category, for tests and for anything that wants to enumerate them.
ALL_CATEGORIES = (
    ProviderError.category,
    ProviderRateLimitError.category,
    ProviderTemporaryError.category,
    ProviderTimeoutError.category,
    ProviderAuthenticationError.category,
    ProviderInvalidRequestError.category,
    ProviderResponseError.category,
)
