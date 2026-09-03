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
import mimetypes
import os
import time
from typing import Any, List, Optional, Sequence, Tuple

import google.generativeai as genai

from backend.ai.config import TaskSettings
from backend.ai.contracts import (
    FilePart,
    FinishReason,
    ProviderRequest,
    ProviderResponse,
    TextPart,
)
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

#: Ceiling on the total inline media in one request. The documented limit for
#: inline data is 20MB for the whole request; this leaves headroom for the
#: prompt text and protobuf overhead. An answer-sheet crop is tens of
#: kilobytes, so a whole question is nowhere near it -- but a caller that
#: attached a 30MB scan should get an explicit, provider-neutral refusal
#: rather than a truncated request or an opaque SDK error.
MAX_INLINE_REQUEST_BYTES = 15 * 1024 * 1024


#: The one variable a deployment is expected to set.
API_KEY_ENV = "GEMINI_API_KEY"
#: Accepted for compatibility with the older numbered convention. Read only
#: when the canonical variable is unset -- NOT rotation, which this adapter
#: cannot do (see the constructor).
LEGACY_API_KEY_ENV = "GEMINI_API_KEY_1"


def _read_api_keys() -> List[str]:
    """The configured Gemini key: `GEMINI_API_KEY`, else the legacy numbered form.

    WHY BOTH. Live validation found the deployed container authenticating with
    `GEMINI_API_KEY_1` and failing, while the working key in the repository
    `.env` was named `GEMINI_API_KEY` -- which this function did not read at
    all. A deployment could therefore hold a valid key and configure nothing,
    and the first sign of it was a 401 partway through grading a paper. One
    canonical name fixes that; the numbered form stays readable so existing
    deployments keep working.

    Values are stripped: a trailing space in a `.env` line is invisible in an
    editor and produces an authentication failure that looks like a bad key.
    """
    canonical = (os.getenv(API_KEY_ENV) or "").strip()
    if canonical:
        return [canonical]

    keys: List[str] = []
    index = 1
    while True:
        key = (os.getenv(f"GEMINI_API_KEY_{index}") or "").strip()
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
            # The failure surfaces as an authentication error at call time --
            # before any upload, see `run_text_task`.
            logger.warning(
                "no Gemini credential configured (set %s); Gemini calls will fail",
                API_KEY_ENV,
            )
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

    @staticmethod
    def _not_configured() -> ProviderAuthenticationError:
        """The failure for "no credential", named so it cannot be misread.

        Provider-neutral category (`authentication`), so the grading path
        records a missing mark with a safe code rather than a zero, and no key
        material can reach the message: it names the VARIABLE, never a value.
        """
        return ProviderAuthenticationError(
            f"no Gemini credential is configured; set {API_KEY_ENV}",
            provider=PROVIDER_NAME,
        )

    # -- client -----------------------------------------------------------
    def _model_for(self, model_name: str):
        """One `GenerativeModel` per model name, built once.

        Cached because construction is not free and because a per-call model
        would make the global `configure` race with itself under concurrency.
        """
        if self._api_key is None:
            raise self._not_configured()
        if not self._configured:
            genai.configure(api_key=self._api_key)
            self._configured = True
        if model_name not in self._models:
            self._models[model_name] = genai.GenerativeModel(model_name)
        return self._models[model_name]

    # -- files ------------------------------------------------------------
    async def _inline(self, path: str) -> dict:
        """Read one local file as an inline part.

        WHY NOT `genai.upload_file`. The File API in google-generativeai 0.8.4
        is reached through the REST discovery client, which authenticates by
        appending `?key=` to the URL. That path rejects the newer `AQ.`-format
        keys outright:

            HttpError 400 ... "API key not valid. Please pass a valid API key."
            reason: API_KEY_INVALID

        while the gRPC `generate_content` path accepts the same key and answers
        normally. Live validation reproduced exactly that split: text-only
        grading worked and every call carrying an image failed as
        `authentication`, which is every recognition call and every diagram
        question -- the whole point of the product.

        Inline bytes go over the working path, and they also remove the
        upload/delete lifecycle that used to leak provider-side files. The cost
        is the request-size ceiling above, which page crops do not approach.
        """
        def _read():
            with open(path, "rb") as handle:
                return handle.read()

        try:
            data = await asyncio.to_thread(_read)
        except OSError as exc:
            raise ProviderInvalidRequestError(
                f"could not read a local file for the request ({type(exc).__name__})",
                provider=PROVIDER_NAME, cause=exc,
            )
        mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
        return {"mime_type": mime, "data": data}

    async def _build_contents(self, request: ProviderRequest) -> Tuple[List[Any], List[Any], List[str]]:
        """Turn ordered prompt parts into SDK contents, inlining files in place.

        Returns `(contents, handles, warnings)`. ORDER IS PRESERVED: a file part
        becomes its bytes at exactly the position it occupied, because diagram
        grading depends on each image sitting immediately after the text that
        introduces it.

        Each DISTINCT path is read once per call -- a question that names the
        same reference image twice costs one read and is sent once.
        Per-invocation only; a cross-request cache would need invalidation
        nobody has designed.

        `handles` is now always empty: nothing is created provider-side, so
        there is nothing to clean up. It is still returned so the caller's
        `finally` stays in place for any future path that does upload.

        Audit C1 note: dedup is by resolved PATH, so a reference image and a
        student image collapse only when they are literally the same file. It
        cannot make one side stand in for the other.
        """
        contents: List[Any] = []
        handles: List[Any] = []
        warnings: List[str] = []
        seen = {}
        total_bytes = 0

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
                logger.warning("skipping a missing file for the provider request")
                continue
            blob = await self._inline(path)
            total_bytes += len(blob["data"])
            if total_bytes > MAX_INLINE_REQUEST_BYTES:
                raise ProviderInvalidRequestError(
                    "request media exceeds the inline size limit "
                    f"({total_bytes} > {MAX_INLINE_REQUEST_BYTES} bytes)",
                    provider=PROVIDER_NAME,
                )
            seen[key] = blob
            contents.append(blob)

        return contents, handles, warnings

    async def _delete_uploads(self, handles: Sequence[Any]) -> None:
        """Remove any provider-side copies. Best effort, never fatal.

        Now a no-op in practice: media is sent inline, so `_build_contents`
        produces no handles and nothing is left provider-side to leak. Kept
        because the guarantee -- whatever we create, we delete -- is the one
        that was missing when `upload_file` was called from five places and
        `delete_file` from none, and it should already be here if a large-media
        path ever brings uploads back.

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

        `response_mime_type` is a real provider-level JSON mode, not a wording
        choice in the prompt: `expects_json` on the task or the request turns it
        on here.

        `response_schema` is NOT set, and the reason recorded here previously --
        that it "wants a version-specific SDK type" -- is wrong for the pinned
        google-generativeai 0.8.4, whose GenerationConfig declares
        `protos.Schema | Mapping[str, Any] | type | None`; a plain dict is
        accepted (verified locally against the installed SDK, no API call).
        Constraining the shape as well as the mime type is therefore available
        and is the obvious next step -- it is left for a change that can be
        validated against the provider, because it alters what the model is
        allowed to emit on every graded question.

        Either way the strict decoder in `backend.grading.result` is what
        guarantees a valid result, so a provider that ignores the hint still
        yields a valid grade or an explicit failure -- never a silent None.
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

        Note what it does NOT do: when generation stopped at an output limit
        but produced some text, `parts` is non-empty and this returns the
        PARTIAL body quite happily. That is why `_finish_reason` exists -- the
        truncation is invisible here and would otherwise reach the grading
        decoder as ordinary invalid JSON.
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

    @staticmethod
    def _finish_reason(response) -> str:
        """Translate this provider's stop reason into the application's own.

        The one place a vendor's enum is spoken. Nothing above the adapter sees
        `MAX_TOKENS` or `RECITATION`; it sees `FinishReason.TRUNCATED` or
        `FinishReason.BLOCKED`, which are the distinctions that change what
        CogniGrade should do.

        Never raises. A diagnostic that can take down a successful grading call
        is worse than no diagnostic, so an unreadable reason is `UNKNOWN`.
        """
        try:
            candidates = getattr(response, "candidates", None) or []
            if not candidates:
                return FinishReason.UNKNOWN
            raw = getattr(candidates[0], "finish_reason", None)
            if raw is None:
                return FinishReason.UNKNOWN
            # `.name` on the SDK enum, else whatever str() gives, upper-cased.
            name = str(getattr(raw, "name", raw)).rsplit(".", 1)[-1].upper()
        except Exception:  # noqa: BLE001
            return FinishReason.UNKNOWN

        if name == "STOP":
            return FinishReason.COMPLETE
        if name == "MAX_TOKENS":
            return FinishReason.TRUNCATED
        if name in ("SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII"):
            return FinishReason.BLOCKED
        if name in ("FINISH_REASON_UNSPECIFIED", "0"):
            return FinishReason.UNKNOWN
        return FinishReason.OTHER

    @staticmethod
    def _token_counts(response):
        """`(input, output)` token counts when the provider reports them.

        Counts only. A number cannot carry a student's answer, which is why
        this is safe to log where the response body never will be.
        """
        try:
            usage = getattr(response, "usage_metadata", None)
            if usage is None:
                return None, None
            prompt = getattr(usage, "prompt_token_count", None)
            output = getattr(usage, "candidates_token_count", None)
            return (
                int(prompt) if prompt is not None else None,
                int(output) if output is not None else None,
            )
        except Exception:  # noqa: BLE001
            return None, None

    async def run_text_task(
        self,
        request: ProviderRequest,
        settings: TaskSettings,
        *,
        timeout_seconds: Optional[float] = None,
    ) -> ProviderResponse:
        """One attempt: upload, generate, read, clean up. Retries live elsewhere.

        The credential is checked FIRST. `_build_contents` uploads every file
        in the request to the provider, so an unconfigured deployment used to
        push a student's whole answer set over the wire before discovering it
        could not authenticate.
        """
        if self._api_key is None:
            raise self._not_configured()
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
            finish_reason = self._finish_reason(response)
            input_tokens, output_tokens = self._token_counts(response)
            if finish_reason == FinishReason.TRUNCATED:
                # Loud, and carried on the response rather than swallowed: this
                # is the difference between "the model wrote something invalid"
                # and "we cut the model off mid-sentence", and the caller's
                # decoder cannot tell them apart from the text alone.
                warnings = list(warnings) + ["truncated_response"]
                logger.warning(
                    "provider response was truncated at an output limit: "
                    "task=%s model=%s output_tokens=%s",
                    request.task, settings.model, output_tokens,
                )
            return ProviderResponse(
                text=text,
                provider=PROVIDER_NAME,
                model=settings.model,
                task=request.task,
                prompt_version=request.prompt_version,
                duration_ms=int((time.monotonic() - started) * 1000),
                uploaded_file_count=len(handles),
                warnings=tuple(warnings),
                finish_reason=finish_reason,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
        finally:
            # Runs on success, on provider failure, and on timeout. This is the
            # `finally` whose absence leaked every uploaded file.
            if handles:
                await self._delete_uploads(handles)
