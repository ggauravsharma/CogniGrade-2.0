"""The AI service/provider boundary.

The point of this layer is that swapping Gemini for something else should be an
adapter plus configuration, not a rewrite. These tests assert the properties
that claim depends on:

    nothing above `providers/` imports a vendor SDK
    the model comes from configuration, per task
    retries happen for transport failures and never for application ones
    provider files are uploaded once and always deleted
    an SDK exception becomes a provider-neutral category
    the domain grading contract is unchanged

No live provider call: the whole suite runs against a recording provider or a
fake SDK module.
"""

import ast
import asyncio
import pathlib
import random

import pytest

from backend.ai import services as ai_services
from backend.ai.config import DEFAULT_MODEL, get_task_settings
from backend.ai.contracts import AITask, FilePart, ProviderRequest, TextPart
from backend.ai.errors import (
    ALL_CATEGORIES,
    ProviderAuthenticationError,
    ProviderError,
    ProviderInvalidRequestError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTemporaryError,
    ProviderTimeoutError,
)
from backend.ai.prompts import (
    GRADING_PROMPT_VERSION,
    build_grading_prompt,
    build_label_extraction_prompt,
)
from backend.ai.retry import compute_delay, run_with_retries
from backend.grading.result import GradingResponseError

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
AI_ROOT = REPO_ROOT / "backend" / "ai"

VENDOR_TOKENS = ("google.generativeai", "google.genai", "genai", "vertexai", "openai", "anthropic")


def _imported_modules(path: pathlib.Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
    return names


# ---------------------------------------------------------------------------
# the boundary itself
# ---------------------------------------------------------------------------

def test_only_the_provider_package_imports_a_vendor_sdk():
    """THE architectural invariant. Everything else is downstream of it."""
    offenders = []
    for path in sorted(AI_ROOT.rglob("*.py")):
        if "providers" in path.relative_to(AI_ROOT).parts:
            continue
        for name in _imported_modules(path):
            for token in VENDOR_TOKENS:
                if token in name.lower():
                    offenders.append(f"{path.relative_to(REPO_ROOT)}: {name}")
    assert offenders == [], offenders


def test_the_router_no_longer_imports_the_sdk():
    """`geminiAPI.py` built model objects and uploaded files directly."""
    path = REPO_ROOT / "backend" / "routers" / "geminiAPI.py"
    for name in _imported_modules(path):
        for token in ("google.generativeai", "google.genai"):
            assert token not in name.lower(), f"router still imports {name}"


def test_no_route_module_imports_a_vendor_sdk():
    routers = REPO_ROOT / "backend" / "routers"
    offenders = []
    for path in sorted(routers.glob("*.py")):
        for name in _imported_modules(path):
            for token in ("google.generativeai", "google.genai"):
                if token in name.lower():
                    offenders.append(f"{path.name}: {name}")
    assert offenders == [], offenders


def test_contracts_and_prompts_are_provider_neutral():
    banned = ("fastapi", "sqlalchemy", "backend.models") + VENDOR_TOKENS
    for path in [AI_ROOT / "contracts.py", AI_ROOT / "errors.py"] + list((AI_ROOT / "prompts").glob("*.py")):
        for name in _imported_modules(path):
            for token in banned:
                assert token not in name.lower(), f"{path.name} imports {name}"


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------

def test_the_default_model_is_not_a_retired_one():
    """Live validation found the previous default had been withdrawn.

    `gemini-2.0-flash` returned 404 on every call -- "no longer available" --
    so grading was impossible until the default was repaired. A retired model
    fails in a way that looks like a CogniGrade bug rather than an expired
    dependency, which is exactly why it is worth pinning down.
    """
    from backend.ai.config import RETIRED_MODELS

    assert DEFAULT_MODEL not in RETIRED_MODELS, (
        f"{DEFAULT_MODEL} has been withdrawn by the provider"
    )
    for task in AITask.ALL:
        assert get_task_settings(task).model not in RETIRED_MODELS


def test_every_task_resolves_settings():
    for task in AITask.ALL:
        settings = get_task_settings(task)
        assert settings.task == task
        assert settings.model == DEFAULT_MODEL
        assert settings.provider == "gemini"


def test_the_model_is_not_hardcoded_in_the_router():
    """A model string in a route is what made task-specific models impossible."""
    source = (REPO_ROOT / "backend" / "routers" / "geminiAPI.py").read_text(encoding="utf-8")
    assert 'model_name = "gemini' not in source
    assert 'GenerativeModel(' not in source


def test_a_task_can_be_pointed_at_a_different_model(monkeypatch):
    monkeypatch.setenv("CG_AI__ANSWER_RECOGNITION__MODEL", "some-specialist-htr-model")
    assert get_task_settings(AITask.ANSWER_RECOGNITION).model == "some-specialist-htr-model"
    # ... and only that task moves.
    assert get_task_settings(AITask.GRADING).model == DEFAULT_MODEL


def test_a_global_override_applies_to_every_task(monkeypatch):
    monkeypatch.setenv("CG_AI__MODEL", "one-model-everywhere")
    for task in AITask.ALL:
        assert get_task_settings(task).model == "one-model-everywhere"


def test_numeric_and_boolean_overrides_are_coerced(monkeypatch):
    monkeypatch.setenv("CG_AI__GRADING__MAX_RETRIES", "5")
    monkeypatch.setenv("CG_AI__GRADING__TIMEOUT_SECONDS", "12.5")
    settings = get_task_settings(AITask.GRADING)
    assert settings.max_retries == 5
    assert settings.timeout_seconds == 12.5


def test_a_malformed_override_falls_back_to_the_default(monkeypatch):
    """A typo in an environment variable must not take grading down."""
    monkeypatch.setenv("CG_AI__GRADING__MAX_RETRIES", "not-a-number")
    assert get_task_settings(AITask.GRADING).max_retries == 2


def test_grading_asks_for_json_and_is_deterministic():
    settings = get_task_settings(AITask.GRADING)
    assert settings.expects_json is True
    assert settings.temperature == 0.0


def test_an_unknown_task_is_rejected():
    with pytest.raises(ValueError):
        get_task_settings("not_a_task")
    with pytest.raises(ValueError):
        ProviderRequest(task="not_a_task", parts=(TextPart("x"),))


# ---------------------------------------------------------------------------
# retry
# ---------------------------------------------------------------------------

class _Sleeper:
    def __init__(self):
        self.delays = []

    async def __call__(self, delay):
        self.delays.append(delay)


@pytest.mark.asyncio
async def test_a_transient_failure_is_retried_then_succeeds():
    settings = get_task_settings(AITask.GRADING)
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise ProviderTemporaryError("503")
        return "ok"

    sleeper = _Sleeper()
    result, attempts = await run_with_retries(flaky, settings=settings, sleep=sleeper)
    assert result == "ok"
    assert attempts == 2
    assert len(sleeper.delays) == 1


@pytest.mark.asyncio
async def test_rate_limiting_is_retried():
    settings = get_task_settings(AITask.GRADING)
    calls = {"n": 0}

    async def limited():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ProviderRateLimitError("429")
        return "ok"

    _, attempts = await run_with_retries(limited, settings=settings, sleep=_Sleeper())
    assert attempts == 3


@pytest.mark.asyncio
async def test_retries_are_exhausted_and_the_last_error_is_raised():
    settings = get_task_settings(AITask.GRADING).with_overrides(max_retries=2)
    calls = {"n": 0}

    async def always_fails():
        calls["n"] += 1
        raise ProviderTemporaryError(f"attempt {calls['n']}")

    sleeper = _Sleeper()
    with pytest.raises(ProviderTemporaryError) as exc:
        await run_with_retries(always_fails, settings=settings, sleep=sleeper)
    assert calls["n"] == 3, "max_retries=2 means three attempts in total"
    assert "attempt 3" in str(exc.value), "the LAST failure must be the one reported"
    assert len(sleeper.delays) == 2


@pytest.mark.parametrize(
    "error",
    [
        ProviderAuthenticationError("bad key"),
        ProviderInvalidRequestError("bad argument"),
        ProviderResponseError("nothing usable"),
    ],
)
@pytest.mark.asyncio
async def test_permanent_failures_are_not_retried(error):
    """Retrying a wrong key or a malformed request just wastes the budget."""
    settings = get_task_settings(AITask.GRADING)
    calls = {"n": 0}

    async def fails():
        calls["n"] += 1
        raise error

    sleeper = _Sleeper()
    with pytest.raises(type(error)):
        await run_with_retries(fails, settings=settings, sleep=sleeper)
    assert calls["n"] == 1
    assert sleeper.delays == []


@pytest.mark.asyncio
async def test_retries_can_be_disabled_by_configuration():
    settings = get_task_settings(AITask.GRADING).with_overrides(max_retries=0)
    calls = {"n": 0}

    async def fails():
        calls["n"] += 1
        raise ProviderTemporaryError("nope")

    with pytest.raises(ProviderTemporaryError):
        await run_with_retries(fails, settings=settings, sleep=_Sleeper())
    assert calls["n"] == 1


def test_backoff_grows_and_is_capped_and_jittered():
    settings = get_task_settings(AITask.GRADING).with_overrides(
        retry_base_delay=1.0, retry_max_delay=8.0
    )
    rng = random.Random(0)
    # Full jitter: every delay lies within [0, capped].
    for attempt, cap in ((1, 1.0), (2, 2.0), (3, 4.0), (4, 8.0), (9, 8.0)):
        for _ in range(20):
            delay = compute_delay(attempt, settings, rng=rng)
            assert 0.0 <= delay <= cap


# ---------------------------------------------------------------------------
# error taxonomy
# ---------------------------------------------------------------------------

def test_categories_are_stable_and_provider_independent():
    assert len(set(ALL_CATEGORIES)) == len(ALL_CATEGORIES)
    for category in ALL_CATEGORIES:
        for token in ("gemini", "google", "openai", "vertex"):
            assert token not in category


@pytest.mark.parametrize(
    "error,retryable",
    [
        (ProviderRateLimitError, True),
        (ProviderTemporaryError, True),
        (ProviderTimeoutError, True),
        (ProviderAuthenticationError, False),
        (ProviderInvalidRequestError, False),
        (ProviderResponseError, False),
    ],
)
def test_retryability_is_declared_on_the_class(error, retryable):
    assert error("x").retryable is retryable
    assert isinstance(error("x"), ProviderError)


# ---------------------------------------------------------------------------
# prompts and versioning
# ---------------------------------------------------------------------------

def test_the_grading_prompt_carries_a_version():
    prompt, version = build_grading_prompt(
        question_text="Q", student_answer="A", max_marks=5, marking_scheme="MS",
    )
    assert version == GRADING_PROMPT_VERSION
    assert version.startswith("grading/")
    assert "MS" in prompt and "A" in prompt


def test_the_grading_prompt_adapts_to_available_reference_material():
    both, _ = build_grading_prompt(
        question_text="Q", student_answer="A", max_marks=5,
        marking_scheme="MS", ideal_answer="IDEAL",
    )
    scheme_only, _ = build_grading_prompt(
        question_text="Q", student_answer="A", max_marks=5, marking_scheme="MS",
    )
    ideal_only, _ = build_grading_prompt(
        question_text="Q", student_answer="A", max_marks=5, ideal_answer="IDEAL",
    )
    assert "IDEAL" in both and "MS" in both
    assert "IDEAL" not in scheme_only
    assert "MS" not in ideal_only


def test_every_prompt_builder_returns_a_version():
    for builder in (build_label_extraction_prompt,):
        prompt, version = builder()
        assert prompt.strip()
        assert "/" in version, "versions look like 'task/vN'"


# ---------------------------------------------------------------------------
# services: the domain contract is unchanged
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_grade_answer_returns_the_domain_result(fake_provider):
    fake_provider(body='{"score": 2.5, "reason": "half credit"}')
    result, raw = await ai_services.grade_answer(
        question_text="Q", student_answer="A", max_marks=5, marking_scheme="MS",
    )
    assert result.score == 2.5
    assert result.reason == "half credit"
    assert raw


@pytest.mark.asyncio
async def test_an_invalid_response_is_a_grading_failure_not_a_zero(fake_provider):
    """C6: a validation failure must never look like a mark of zero."""
    fake_provider(body="I cannot grade this.")
    with pytest.raises(GradingResponseError):
        await ai_services.grade_answer(
            question_text="Q", student_answer="A", max_marks=5, marking_scheme="MS",
        )


@pytest.mark.asyncio
async def test_a_score_above_max_is_still_rejected(fake_provider):
    fake_provider(body='{"score": 500, "reason": "way too high"}')
    with pytest.raises(GradingResponseError):
        await ai_services.grade_answer(
            question_text="Q", student_answer="A", max_marks=5, marking_scheme="MS",
        )


@pytest.mark.asyncio
async def test_fractional_marks_survive_the_service_layer(fake_provider):
    """C7: partial credit must not be rounded on the way through."""
    for score in (0.5, 1.5, 2.25):
        fake_provider(body=f'{{"score": {score}, "reason": "x"}}')
        result, _ = await ai_services.grade_answer(
            question_text="Q", student_answer="A", max_marks=5, marking_scheme="MS",
        )
        assert result.score == score


@pytest.mark.asyncio
async def test_a_provider_failure_reaches_the_caller_as_a_provider_error(fake_provider):
    fake_provider(error=ProviderAuthenticationError("bad key"))
    with pytest.raises(ProviderError) as exc:
        await ai_services.grade_answer(
            question_text="Q", student_answer="A", max_marks=5, marking_scheme="MS",
        )
    assert exc.value.category == "authentication"


@pytest.mark.asyncio
async def test_recognition_sends_the_images_and_the_task(fake_provider):
    provider = fake_provider(body="Question Number 1\nAnswer: hello")
    text = await ai_services.recognise_answer_images(["/tmp/a.png", "/tmp/b.png"])
    assert text.startswith("Question Number 1")
    request = provider.last_request
    assert request.task == AITask.ANSWER_RECOGNITION
    assert request.file_paths == ("/tmp/a.png", "/tmp/b.png")
    assert request.expects_json is False


@pytest.mark.asyncio
async def test_the_service_layer_retries_before_giving_up(fake_provider):
    provider = fake_provider(
        body='{"score": 1, "reason": "ok"}',
        errors=[ProviderTemporaryError("503"), None],
    )
    result, _ = await ai_services.grade_answer(
        question_text="Q", student_answer="A", max_marks=5, marking_scheme="MS",
    )
    assert result.score == 1
    assert len(provider.requests) == 2, "the first attempt failed and was retried"


# ---------------------------------------------------------------------------
# telemetry
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_successful_call_is_logged_with_its_metadata(fake_provider, caplog):
    fake_provider(body='{"score": 1, "reason": "ok"}')
    with caplog.at_level("INFO", logger="backend.ai.telemetry"):
        await ai_services.grade_answer(
            question_text="Q", student_answer="A", max_marks=5, marking_scheme="MS",
            exam_id=7, student_id=8, question_id=9,
        )
    line = caplog.text
    assert "ai_invocation" in line
    assert "task=grading" in line
    assert f"model={DEFAULT_MODEL}" in line
    assert "success=True" in line
    assert "attempts=1" in line
    assert "prompt_version=grading/" in line


@pytest.mark.asyncio
async def test_a_failed_call_is_logged_with_its_category(fake_provider, caplog):
    fake_provider(error=ProviderRateLimitError("429"))
    with caplog.at_level("ERROR", logger="backend.ai.telemetry"):
        with pytest.raises(ProviderError):
            await ai_services.grade_answer(
                question_text="Q", student_answer="A", max_marks=5, marking_scheme="MS",
            )
    assert "success=False" in caplog.text
    assert "error_category=rate_limit" in caplog.text


@pytest.mark.asyncio
async def test_telemetry_never_carries_answer_content(fake_provider, caplog):
    """Logs must not become a transcript of every student's work."""
    secret = "THE-STUDENTS-PRIVATE-ANSWER"
    fake_provider(body='{"score": 1, "reason": "ok"}')
    with caplog.at_level("DEBUG", logger="backend.ai.telemetry"):
        await ai_services.grade_answer(
            question_text="Q", student_answer=secret, max_marks=5,
            marking_scheme="MARKING-SCHEME-TEXT",
        )
    assert secret not in caplog.text
    assert "MARKING-SCHEME-TEXT" not in caplog.text
