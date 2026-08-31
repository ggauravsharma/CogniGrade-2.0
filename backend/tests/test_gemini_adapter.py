"""The Gemini adapter, against a fake SDK module.

This is the one place a vendor SDK is exercised at all, and it is faked -- no
network, no key, no quota. What is being tested is the adapter's own behaviour:
uploads happen once per distinct file, uploads are ALWAYS deleted, local files
are never touched, and whatever the SDK raises comes out as a provider-neutral
category.

THE BUG THIS EXISTS FOR
-----------------------
`genai.upload_file` was called from five places in the router and
`genai.delete_file` from none, so every graded diagram question left provider
files behind permanently. Cleanup now lives in a `finally`, and these tests are
what keep it there.
"""

import asyncio
import sys
import types

import pytest

from backend.ai.config import get_task_settings
from backend.ai.contracts import AITask, FilePart, ProviderRequest, TextPart
from backend.ai.errors import (
    ProviderAuthenticationError,
    ProviderInvalidRequestError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTemporaryError,
    ProviderTimeoutError,
)


class FakeHandle:
    def __init__(self, path, index):
        self.path = path
        self.name = f"files/fake-{index}"


class FakeSDK:
    """Stands in for `google.generativeai`, recording what it was asked to do."""

    def __init__(self, *, text='{"score": 1, "reason": "ok"}', raise_on_generate=None):
        self.uploaded = []
        self.deleted = []
        self.configured_keys = []
        self.generate_calls = []
        self.text = text
        self.raise_on_generate = raise_on_generate
        self.delete_should_fail = False

    # -- SDK surface ------------------------------------------------------
    def configure(self, api_key=None, **kw):
        self.configured_keys.append(api_key)

    def upload_file(self, path=None, display_name=None, **kw):
        handle = FakeHandle(path, len(self.uploaded))
        self.uploaded.append(path)
        return handle

    def delete_file(self, name, **kw):
        if self.delete_should_fail:
            raise RuntimeError("delete exploded")
        self.deleted.append(name)

    def GenerativeModel(self, model_name):  # noqa: N802 - mirrors the SDK name
        sdk = self

        class _Model:
            def generate_content(self, contents, generation_config=None, request_options=None):
                sdk.generate_calls.append(
                    {
                        "contents": contents,
                        "generation_config": generation_config,
                        "request_options": request_options,
                    }
                )
                if sdk.raise_on_generate is not None:
                    raise sdk.raise_on_generate
                return types.SimpleNamespace(text=sdk.text)

        return _Model()


@pytest.fixture
def sdk(monkeypatch):
    """Install a fake `google.generativeai` and hand back a fresh adapter."""
    fake = FakeSDK()
    module = types.ModuleType("google.generativeai")
    module.configure = fake.configure
    module.upload_file = fake.upload_file
    module.delete_file = fake.delete_file
    module.GenerativeModel = fake.GenerativeModel

    import backend.ai.providers.gemini as adapter_module

    monkeypatch.setattr(adapter_module, "genai", module)
    fake.module = module
    return fake


@pytest.fixture
def adapter(sdk):
    from backend.ai.providers.gemini import GeminiProvider

    return GeminiProvider(api_keys=["test-key-not-real"])


def _request(paths=(), *, task=AITask.GRADING, text="prompt"):
    parts = tuple(FilePart(p) for p in paths) + (TextPart(text),)
    return ProviderRequest(task=task, parts=parts, prompt_version="test/v1")


# ---------------------------------------------------------------------------
# file lifecycle
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_uploads_are_deleted_after_a_successful_call(adapter, sdk, tmp_path):
    a = tmp_path / "a.png"
    b = tmp_path / "b.png"
    a.write_bytes(b"a")
    b.write_bytes(b"b")

    response = await adapter.run_text_task(
        _request([str(a), str(b)]), get_task_settings(AITask.GRADING)
    )
    assert response.text
    assert sdk.uploaded == [str(a), str(b)]
    assert len(sdk.deleted) == 2, "every uploaded file must be removed"


@pytest.mark.asyncio
async def test_uploads_are_deleted_when_the_model_call_fails(adapter, sdk, tmp_path):
    """The leak that mattered: a failed grading still leaves files behind."""
    a = tmp_path / "a.png"
    a.write_bytes(b"a")
    sdk.raise_on_generate = RuntimeError("503 service unavailable")

    with pytest.raises(ProviderTemporaryError):
        await adapter.run_text_task(_request([str(a)]), get_task_settings(AITask.GRADING))

    assert sdk.uploaded == [str(a)]
    assert len(sdk.deleted) == 1, "a failed call must still clean up its uploads"


@pytest.mark.asyncio
async def test_cleanup_failure_does_not_destroy_the_result(adapter, sdk, tmp_path):
    a = tmp_path / "a.png"
    a.write_bytes(b"a")
    sdk.delete_should_fail = True

    response = await adapter.run_text_task(
        _request([str(a)]), get_task_settings(AITask.GRADING)
    )
    assert response.text, "a failed cleanup must not lose a grade that was produced"
    assert sdk.deleted == []


@pytest.mark.asyncio
async def test_local_files_are_never_deleted(adapter, sdk, tmp_path):
    """The student's answer script belongs to the application, not the provider."""
    a = tmp_path / "answer.png"
    a.write_bytes(b"a")

    await adapter.run_text_task(_request([str(a)]), get_task_settings(AITask.GRADING))
    assert a.exists(), "the local file must survive provider cleanup"


@pytest.mark.asyncio
async def test_the_same_file_is_uploaded_once_per_call(adapter, sdk, tmp_path):
    a = tmp_path / "shared.png"
    a.write_bytes(b"a")

    request = _request([str(a), str(a)])
    response = await adapter.run_text_task(request, get_task_settings(AITask.GRADING))

    assert sdk.uploaded == [str(a)], "a repeated path must not be uploaded twice"
    assert response.uploaded_file_count == 1
    # ... and it still appears at BOTH positions in the prompt.
    contents = sdk.generate_calls[0]["contents"]
    handles = [c for c in contents if isinstance(c, FakeHandle)]
    assert len(handles) == 2 and handles[0] is handles[1]


@pytest.mark.asyncio
async def test_a_missing_file_is_skipped_not_fatal(adapter, sdk, tmp_path):
    a = tmp_path / "here.png"
    a.write_bytes(b"a")

    response = await adapter.run_text_task(
        _request([str(a), str(tmp_path / "gone.png")]), get_task_settings(AITask.GRADING)
    )
    assert sdk.uploaded == [str(a)]
    assert "missing_file" in response.warnings


# ---------------------------------------------------------------------------
# ordering: audit C1 depends on it
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_part_order_is_preserved_exactly(adapter, sdk, tmp_path):
    ref = tmp_path / "ref.png"
    stu = tmp_path / "stu.png"
    ref.write_bytes(b"r")
    stu.write_bytes(b"s")

    request = ProviderRequest(
        task=AITask.GRADING,
        parts=(
            TextPart("scheme text"),
            FilePart(str(ref)),
            TextPart("student answer"),
            FilePart(str(stu)),
            TextPart("tail"),
        ),
        prompt_version="test/v1",
    )
    await adapter.run_text_task(request, get_task_settings(AITask.GRADING))

    contents = sdk.generate_calls[0]["contents"]
    assert contents[0] == "scheme text"
    assert contents[1].path == str(ref)
    assert contents[2] == "student answer"
    assert contents[3].path == str(stu)
    assert contents[4] == "tail"


# ---------------------------------------------------------------------------
# generation config and timeout
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_grading_requests_json_and_zero_temperature(adapter, sdk):
    await adapter.run_text_task(_request(), get_task_settings(AITask.GRADING))
    config = sdk.generate_calls[0]["generation_config"]
    assert config["response_mime_type"] == "application/json"
    assert config["temperature"] == 0.0


@pytest.mark.asyncio
async def test_recognition_does_not_request_json(adapter, sdk):
    await adapter.run_text_task(
        _request(task=AITask.ANSWER_RECOGNITION),
        get_task_settings(AITask.ANSWER_RECOGNITION),
    )
    assert "response_mime_type" not in sdk.generate_calls[0]["generation_config"]


@pytest.mark.asyncio
async def test_a_request_deadline_is_passed_to_the_sdk(adapter, sdk):
    settings = get_task_settings(AITask.GRADING).with_overrides(timeout_seconds=42.0)
    await adapter.run_text_task(_request(), settings)
    assert sdk.generate_calls[0]["request_options"] == {"timeout": 42.0}


@pytest.mark.asyncio
async def test_an_sdk_without_request_options_still_works(adapter, sdk, monkeypatch):
    """Losing the deadline must degrade, not crash."""
    original = sdk.GenerativeModel

    def _old_style(model_name):
        class _Model:
            def generate_content(self, contents, generation_config=None):
                sdk.generate_calls.append({"contents": contents, "generation_config": generation_config,
                                           "request_options": None})
                return types.SimpleNamespace(text=sdk.text)

        return _Model()

    monkeypatch.setattr(sdk.module, "GenerativeModel", _old_style)
    from backend.ai.providers.gemini import GeminiProvider

    fresh = GeminiProvider(api_keys=["test-key-not-real"])
    response = await fresh.run_text_task(_request(), get_task_settings(AITask.GRADING))
    assert response.text
    assert original is not None


# ---------------------------------------------------------------------------
# error mapping
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "message,expected",
    [
        ("429 ResourceExhausted: quota", ProviderRateLimitError),
        ("503 ServiceUnavailable", ProviderTemporaryError),
        ("Unauthenticated: API key invalid", ProviderAuthenticationError),
        ("InvalidArgument: bad request", ProviderInvalidRequestError),
        ("deadline exceeded", ProviderTimeoutError),
    ],
)
@pytest.mark.asyncio
async def test_sdk_exceptions_map_to_neutral_categories(adapter, sdk, message, expected):
    sdk.raise_on_generate = RuntimeError(message)
    with pytest.raises(expected):
        await adapter.run_text_task(_request(), get_task_settings(AITask.GRADING))


@pytest.mark.asyncio
async def test_an_unrecognised_failure_is_treated_as_temporary(adapter, sdk):
    """Better a bounded retry than failing a whole exam on one odd error."""
    sdk.raise_on_generate = RuntimeError("something nobody has seen before")
    with pytest.raises(ProviderTemporaryError):
        await adapter.run_text_task(_request(), get_task_settings(AITask.GRADING))


@pytest.mark.asyncio
async def test_a_blocked_response_is_an_explicit_provider_error(adapter, sdk, monkeypatch):
    """`response.text` RAISES on a blocked candidate; that was an opaque 500."""

    class Exploding:
        @property
        def text(self):
            raise RuntimeError("no candidates")

    def _model(model_name):
        class _M:
            def generate_content(self, *a, **kw):
                return Exploding()

        return _M()

    monkeypatch.setattr(sdk.module, "GenerativeModel", _model)
    from backend.ai.providers.gemini import GeminiProvider

    fresh = GeminiProvider(api_keys=["test-key-not-real"])
    with pytest.raises(ProviderResponseError):
        await fresh.run_text_task(_request(), get_task_settings(AITask.GRADING))


@pytest.mark.asyncio
async def test_no_api_key_is_an_authentication_error_not_a_crash(sdk):
    from backend.ai.providers.gemini import GeminiProvider

    provider = GeminiProvider(api_keys=[])
    with pytest.raises(ProviderAuthenticationError):
        await provider.run_text_task(_request(), get_task_settings(AITask.GRADING))


# ---------------------------------------------------------------------------
# api keys
# ---------------------------------------------------------------------------

def test_only_one_key_is_used_and_extras_are_reported(sdk, caplog):
    """The old rotation was a no-op; saying so is better than pretending."""
    from backend.ai.providers.gemini import GeminiProvider

    # Distinctive values, so a match cannot be an English word in the message.
    keys = ["AIzaSyFAKEKEY0001", "AIzaSyFAKEKEY0002", "AIzaSyFAKEKEY0003"]
    with caplog.at_level("WARNING", logger="backend.ai.providers.gemini"):
        provider = GeminiProvider(api_keys=keys)
    assert "only the first is" in caplog.text
    for key in keys:
        assert key not in caplog.text, "a key must never be logged"
    assert provider._api_key == keys[0]


@pytest.mark.asyncio
async def test_the_client_is_configured_once_not_per_call(adapter, sdk):
    settings = get_task_settings(AITask.GRADING)
    await adapter.run_text_task(_request(), settings)
    await adapter.run_text_task(_request(), settings)
    assert len(sdk.configured_keys) == 1, "configure() must not run per call"
    assert len(sdk.generate_calls) == 2


@pytest.mark.asyncio
async def test_different_models_get_different_clients(adapter, sdk):
    base = get_task_settings(AITask.GRADING)
    await adapter.run_text_task(_request(), base)
    await adapter.run_text_task(_request(), base.with_overrides(model="another-model"))
    assert len(sdk.generate_calls) == 2


def test_the_adapter_holds_no_global_mutable_call_state():
    """The old router kept a module-global counter behind two locks, which is
    exactly what a later concurrency phase would have had to unpick."""
    import backend.ai.providers.gemini as mod

    for name in ("call_count", "calls_per_key", "call_lock", "async_call_lock", "models"):
        assert not hasattr(mod, name), f"module-global {name} is back"
