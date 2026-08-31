"""The Gemini adapter. The only module in CogniGrade that imports the SDK.

Everything vendor-shaped lives behind this file: client construction, API keys,
file upload and deletion, generation config, request timeouts, and the mapping
from SDK exceptions to `backend.ai.errors`. Routes, services and the domain see
`ProviderRequest` in and `ProviderResponse` out, and nothing else.

API KEYS -- WHAT THE OLD CODE ACTUALLY DID
------------------------------------------
Module scope in `geminiAPI.py` ran::

    for key in api_keys:
        genai.configure(api_key=key)
        models.append(genai.GenerativeModel(model_name))

`genai.configure()` sets a MODULE-GLOBAL default client; `GenerativeModel` does
not capture a key, it resolves one at call time from that global. So every
model in the list used whichever key was configured LAST, and `get_model()`
rotating through them every 15 calls rotated nothing. It looked like quota
spreading and was a no-op.

Rather than pretend, this adapter uses ONE key explicitly and says so, loudly,
when more are configured. Real rotation needs per-call client binding, which
the legacy SDK does not offer cleanly -- see the context file.

CONCURRENCY
-----------
No module-global mutable call counter, no shared per-request state. The client
is built once and is safe to use from several tasks at a time, so the later
bounded-concurrency phase has nothing to unpick here.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, List, Optional, Sequence, Tuple

import google.generativeai as genai

from backend.ai.config import TaskSettings
from backend.ai.contracts import FilePart, ProviderRequest, ProviderResponse, TextPart
from backend.ai.errors import (
    ProviderAuthenticationError,
    ProviderError,
    ProviderInvalidRequestError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTemporaryError,
    ProviderTimeoutError,
)

logger = logging.getLogger(__name__)

PROVIDER_NAME = "gemini"


def _read_api_keys() -> List[str]:
    """`GEMINI_API_KEY_1`, `_2`, ... in order, stopping at the first gap."""
    keys: List[str] = []
    index = 1
    while True:
        key = os.getenv(f"GEMINI_API_KEY_{index}")
        if not key:
            break
        keys.append(key)
        index += 1
    return keys


def _classify(exc: BaseException) -> ProviderError:
    """Map an SDK/transport exception onto the provider-neutral taxonomy.

    Matched on type name and message text rather than by importing
    `google.api_core.exceptions`, because the exact exception classes move
    between SDK versions and an ImportError here would take grading down. The
    categories are what callers act on; the precise class is only a hint.
    """
    name = type(exc).__name__
    text = f"{name} {exc}".lower()

    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)) or "deadline" in text or "timeout" in text:
        return ProviderTimeoutError(f"provider call timed out ({name})", provider=PROVIDER_NAME, cause=exc)
    if "resourceexhausted" in text or "rate limit" in text or "quota" in text or " 429" in text:
        return ProviderRateLimitError(f"provider rate limited ({name})", provider=PROVIDER_NAME, cause=exc)
    if any(token in text for token in ("unauthenticated", "permissiondenied", "api key", "401", "403")):
        return ProviderAuthenticationError(f"provider rejected credentials ({name})", provider=PROVIDER_NAME, cause=exc)
    if any(token in text for token in ("unavailable", "internal", "serviceunavailable", "503", "500", "aborted")):
        return ProviderTemporaryError(f"provider temporarily unavailable ({name})", provider=PROVIDER_NAME, cause=exc)
    if any(token in text for token in ("invalidargument", "failedprecondition", "badrequest", "400", "blocked", "safety")):
        return ProviderInvalidRequestError(f"provider rejected the request ({name})", provider=PROVIDER_NAME, cause=exc)

    # Unknown failures are treated as temporary: a bounded retry is cheap, and
    # the alternative is failing a whole exam on one unrecognised transport
    # hiccup. The retry budget stops it from becoming a hang.
    return ProviderTemporaryError(f"provider call failed ({name})", provider=PROVIDER_NAME, cause=exc)


class GeminiProvider:
    """Implements `TextTaskProvider` against `google.generativeai`."""

    name = PROVIDER_NAME

    def __init__(self, *, api_keys: Optional[Sequence[str]] = None):
        keys = list(api_keys) if api_keys is not None else _read_api_keys()
        self._configured = False
        self._models = {}

        if not keys:
            # Not fatal at construction: a deployment that never calls Gemini
            # (a test run, a future local-model install) must still import.
            # The failure surfaces as an authentication error at call time.
            logger.warning("no GEMINI_API_KEY_n configured; Gemini calls will fail")
            self._api_key = None
            return

        if len(keys) > 1:
            logger.warning(
                "%s GEMINI_API_KEY_n values are configured but only the first is "
                "used. The previous rotation was a no-op: genai.configure() sets a "
                "module-global key, so every model object used whichever key was "
                "configured last. Real rotation needs per-call client binding.",
                len(keys),
            )
        self._api_key = keys[0]

    # -- client -----------------------------------------------------------
    def _model_for(self, model_name: str):
        """One `GenerativeModel` per model name, built once.

        Cached because construction is not free and because a per-call model
        would make the global `configure` race with itself under concurrency.
        """
        if self._api_key is None:
            raise ProviderAuthenticationError(
                "no Gemini API key configured", provider=PROVIDER_NAME
            )
        if not self._configured:
            genai.configure(api_key=self._api_key)
            self._configured = True
        if model_name not in self._models:
            self._models[model_name] = genai.GenerativeModel(model_name)
        return self._models[model_name]

    # -- files ------------------------------------------------------------
    async def _upload(self, path: str):
        """Upload one local file, returning the provider handle."""
        try:
            return await asyncio.to_thread(
                genai.upload_file, path=path, display_name=os.path.basename(path)
            )
        except Exception as exc:  # noqa: BLE001 - SDK raises assorted types
            raise _classify(exc)

    async def _build_contents(self, request: ProviderRequest) -> Tuple[List[Any], List[Any], List[str]]:
        """Turn ordered prompt parts into SDK contents, uploading files in place.

        Returns `(contents, handles, warnings)`. ORDER IS PRESERVED: a file part
        becomes an uploaded handle at exactly the position it occupied, because
        diagram grading depends on each image sitting immediately after the text
        that introduces it.

        Each DISTINCT path is uploaded once per call -- a question that names
        the same reference image twice costs one upload, not two. Per-invocation
        only; a cross-request cache would need invalidation nobody has designed.

        Audit C1 note: dedup is by resolved PATH, so a reference image and a
        student image collapse only when they are literally the same file. It
        cannot make one side stand in for the other.
        """
        contents: List[Any] = []
        handles: List[Any] = []
        warnings: List[str] = []
        seen = {}

        for part in request.parts:
            if isinstance(part, TextPart):
                contents.append(part.text)
                continue
            path = part.path
            if not path:
                continue
            key = os.path.abspath(path)
            if key in seen:
                contents.append(seen[key])
                continue
            if not os.path.exists(path):
                warnings.append("missing_file")
                logger.warning("skipping a missing file for provider upload")
                continue
            handle = await self._upload(path)
            seen[key] = handle
            handles.append(handle)
            contents.append(handle)

        return contents, handles, warnings

    async def _delete_uploads(self, handles: Sequence[Any]) -> None:
        """Remove the provider-side copies. Best effort, never fatal.

        Historically `upload_file` was called from five places and `delete_file`
        from none, so every graded diagram question left files behind forever.
        Cleanup failure is logged and swallowed: losing a remote temp file must
        never destroy a grading result that was already produced.

        Only provider handles are touched. The LOCAL file is not deleted here --
        it is the student's answer script and belongs to the application.
        """
        deleter = getattr(genai, "delete_file", None)
        if deleter is None:  # pragma: no cover - very old SDK
            logger.warning("SDK exposes no delete_file; provider files will accumulate")
            return
        for handle in handles:
            name = getattr(handle, "name", None)
            if not name:
                continue
            try:
                await asyncio.to_thread(deleter, name)
            except Exception as exc:  # noqa: BLE001
                logger.warning("could not delete provider file: %s", type(exc).__name__)

    # -- generation --------------------------------------------------------
    def _generation_config(self, settings: TaskSettings, request: ProviderRequest) -> dict:
        """Provider-specific knobs, assembled here and nowhere else.

        `response_mime_type` is passed as a plain string: the pinned
        google-generativeai 0.8.4 accepts that, whereas `response_schema` wants
        a version-specific SDK type. The strict decoder in
        `backend.grading.result` is what actually guarantees a valid result, so
        a provider that ignores the hint still yields either a valid grade or an
        explicit failure -- never a silent None.
        """
        config = {"temperature": settings.temperature}
        if request.expects_json or settings.expects_json:
            config["response_mime_type"] = "application/json"
        return config

    def _call_model(self, model, contents, config, timeout_seconds):
        """The blocking SDK call, with a real request deadline where supported."""
        try:
            return model.generate_content(
                contents,
                generation_config=config,
                request_options={"timeout": timeout_seconds},
            )
        except TypeError:
            # Older/newer SDKs may not accept request_options. Losing the
            # deadline is worse than nothing, so the caller still wraps this in
            # asyncio.wait_for -- see run_text_task.
            logger.warning("SDK rejected request_options; falling back without a request deadline")
            return model.generate_content(contents, generation_config=config)

    @staticmethod
    def _response_text(response) -> str:
        """Read the body defensively.

        `response.text` RAISES on a blocked or empty candidate rather than
        returning None, which used to surface as an opaque 500.
        """
        try:
            text = response.text
        except Exception as exc:  # noqa: BLE001
            raise ProviderResponseError(
                f"provider returned no usable text ({type(exc).__name__})",
                provider=PROVIDER_NAME,
                cause=exc,
            )
        return text or ""

    async def run_text_task(
        self,
        request: ProviderRequest,
        settings: TaskSettings,
        *,
        timeout_seconds: Optional[float] = None,
    ) -> ProviderResponse:
        """One attempt: upload, generate, read, clean up. Retries live elsewhere."""
        budget = timeout_seconds if timeout_seconds is not None else settings.timeout_seconds
        started = time.monotonic()
        handles: List[Any] = []
        try:
            contents, handles, warnings = await self._build_contents(request)
            model = self._model_for(settings.model)
            config = self._generation_config(settings, request)
            if not contents:
                raise ProviderInvalidRequestError(
                    "request carried no prompt content", provider=PROVIDER_NAME
                )

            try:
                response = await asyncio.wait_for(
                    asyncio.to_thread(self._call_model, model, contents, config, budget),
                    timeout=budget + 5,
                )
            except asyncio.TimeoutError as exc:
                raise ProviderTimeoutError(
                    f"provider call exceeded {budget}s", provider=PROVIDER_NAME, cause=exc
                )
            except ProviderError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise _classify(exc)

            text = self._response_text(response)
            return ProviderResponse(
                text=text,
                provider=PROVIDER_NAME,
                model=settings.model,
                task=request.task,
                prompt_version=request.prompt_version,
                duration_ms=int((time.monotonic() - started) * 1000),
                uploaded_file_count=len(handles),
                warnings=tuple(warnings),
            )
        finally:
            # Runs on success, on provider failure, and on timeout. This is the
            # `finally` whose absence leaked every uploaded file.
            if handles:
                await self._delete_uploads(handles)
