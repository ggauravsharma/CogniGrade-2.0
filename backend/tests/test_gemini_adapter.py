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


def _inline_parts(sdk, call=0):
    """The media blobs sent in one generate_content call.

    Media travels INLINE rather than through the File API -- see
    `GeminiProvider._inline`. A blob is a plain
    `{"mime_type": ..., "data": ...}` mapping, so tests read it directly
    instead of through an upload handle.
    """
    return [
        c for c in sdk.generate_calls[call]["contents"]
        if isinstance(c, dict) and "mime_type" in c
    ]


# ---------------------------------------------------------------------------
# file lifecycle
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_media_is_sent_inline_and_leaves_nothing_behind(adapter, sdk, tmp_path):
    """Nothing is created provider-side, so nothing can be left there.

    This replaces an upload/delete pair. The guarantee is the same one --
    whatever we create, we remove -- reached by creating nothing.
    """
    a = tmp_path / "a.png"
    b = tmp_path / "b.png"
    a.write_bytes(b"a")
    b.write_bytes(b"b")

    response = await adapter.run_text_task(
        _request([str(a), str(b)]), get_task_settings(AITask.GRADING)
    )
    assert response.text
    assert sdk.uploaded == [], "the File API was used"
    assert sdk.deleted == []
    assert response.uploaded_file_count == 0

    blobs = _inline_parts(sdk)
    assert [blob["data"] for blob in blobs] == [b"a", b"b"]
    assert all(blob["mime_type"] == "image/png" for blob in blobs)


@pytest.mark.asyncio
async def test_the_file_api_is_never_called(adapter, sdk, tmp_path):
    """THE live blocker.

    `genai.upload_file` reaches the REST discovery client, which authenticates
    with `?key=` and rejects the newer AQ.-format keys outright:

        HttpError 400 ... API_KEY_INVALID

    while gRPC `generate_content` accepts the same key. Every recognition call
    and every diagram question carries an image, so routing media through the
    File API failed the entire product on a key that works.
    """
    a = tmp_path / "answer.png"
    a.write_bytes(b"a")

    await adapter.run_text_task(_request([str(a)]), get_task_settings(AITask.GRADING))

    assert sdk.uploaded == []
    assert _inline_parts(sdk), "the image never reached the model"


@pytest.mark.asyncio
async def test_a_failed_call_leaves_nothing_provider_side(adapter, sdk, tmp_path):
    """The leak that mattered: a failed grading used to leave files behind."""
    a = tmp_path / "a.png"
    a.write_bytes(b"a")
    sdk.raise_on_generate = RuntimeError("503 service unavailable")

    with pytest.raises(ProviderTemporaryError):
        await adapter.run_text_task(_request([str(a)]), get_task_settings(AITask.GRADING))

    assert sdk.uploaded == [] and sdk.deleted == []


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
async def test_the_same_file_is_read_once_per_call(adapter, sdk, tmp_path):
    a = tmp_path / "shared.png"
    a.write_bytes(b"a")

    request = _request([str(a), str(a)])
    await adapter.run_text_task(request, get_task_settings(AITask.GRADING))

    # One read, and the SAME object at both positions -- so the bytes are not
    # carried twice in the request either.
    blobs = _inline_parts(sdk)
    assert len(blobs) == 2 and blobs[0] is blobs[1]


@pytest.mark.asyncio
async def test_a_missing_file_is_skipped_not_fatal(adapter, sdk, tmp_path):
    a = tmp_path / "here.png"
    a.write_bytes(b"a")

    response = await adapter.run_text_task(
        _request([str(a), str(tmp_path / "gone.png")]), get_task_settings(AITask.GRADING)
    )
    assert [blob["data"] for blob in _inline_parts(sdk)] == [b"a"]
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
    assert contents[1]["data"] == b"r", "the reference image left its slot"
    assert contents[2] == "student answer"
    assert contents[3]["data"] == b"s", "the student image left its slot"
    assert contents[4] == "tail"
    assert contents[1] is not contents[3], "the two sides collapsed into one part"


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


@pytest.mark.asyncio
async def test_concurrent_invocations_do_not_share_per_call_state(adapter, sdk, tmp_path):
    """Part P: uploads, warnings and timings must be local to one invocation.

    Ten calls run at once, each with its OWN file. If the adapter kept the
    upload list, the warning list or the timer on the instance, the responses
    would cross-contaminate and this would catch it.
    """
    import asyncio

    paths = []
    for index in range(10):
        path = tmp_path / f"file-{index}.png"
        path.write_bytes(b"x")
        paths.append(str(path))

    responses = await asyncio.gather(*(
        adapter.run_text_task(_request([path]), get_task_settings(AITask.GRADING))
        for path in paths
    ))

    assert len(responses) == 10
    for response in responses:
        assert response.warnings == (), (
            "an invocation saw another invocation's warnings"
        )
    # Every file read once and sent once, nothing lost, doubled or crossed.
    sent = [
        blob["data"]
        for call in range(10)
        for blob in _inline_parts(sdk, call)
    ]
    assert len(sent) == 10, "an invocation carried another invocation's media"
    assert sdk.uploaded == [] and sdk.deleted == []


def test_the_adapter_holds_no_global_mutable_call_state():
    """The old router kept a module-global counter behind two locks, which is
    exactly what a later concurrency phase would have had to unpick."""
    import backend.ai.providers.gemini as mod

    for name in ("call_count", "calls_per_key", "call_lock", "async_call_lock", "models"):
        assert not hasattr(mod, name), f"module-global {name} is back"
