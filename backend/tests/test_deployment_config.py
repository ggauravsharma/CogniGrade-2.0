"""Configuration that has to be right before a single live call is worth making.

Live validation burned a day's quota discovering two things that were
configuration, not code: the deployed container read a credential variable
nobody sets, and every SQL statement was being echoed with its bound
parameters. Both are cheap to assert and expensive to rediscover.

No network, no key, no quota.
"""

from __future__ import annotations

import pathlib

import pytest

from backend.ai.errors import ProviderAuthenticationError

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# the credential
# ---------------------------------------------------------------------------

def _keys(monkeypatch, **env):
    """Read the adapter's key resolution under a clean environment."""
    from backend.ai.providers.gemini import _read_api_keys

    for name in ("GEMINI_API_KEY", "GEMINI_API_KEY_1", "GEMINI_API_KEY_2"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return _read_api_keys()


def test_the_canonical_variable_is_read(monkeypatch):
    """GEMINI_API_KEY is what a deployment sets, and it was not read at all."""
    assert _keys(monkeypatch, GEMINI_API_KEY="canonical") == ["canonical"]


def test_the_legacy_numbered_variable_still_works(monkeypatch):
    """Existing deployments must not break on the rename."""
    assert _keys(monkeypatch, GEMINI_API_KEY_1="legacy") == ["legacy"]


def test_the_canonical_variable_wins_over_the_legacy_one(monkeypatch):
    keys = _keys(monkeypatch, GEMINI_API_KEY="canonical", GEMINI_API_KEY_1="legacy")
    assert keys == ["canonical"], "the legacy fallback overrode the canonical name"


def test_surrounding_whitespace_is_stripped(monkeypatch):
    """A trailing space in a .env line is invisible and reads as a bad key."""
    assert _keys(monkeypatch, GEMINI_API_KEY="  spaced  ") == ["spaced"]
    assert _keys(monkeypatch, GEMINI_API_KEY="   ") == [], "blank must not count"


def test_no_credential_resolves_to_nothing(monkeypatch):
    assert _keys(monkeypatch) == []


# ---------------------------------------------------------------------------
# failing fast, and safely
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_an_unconfigured_provider_fails_before_uploading_anything(tmp_path):
    """Building contents uploads the student's files. Check the key first."""
    from backend.ai.config import get_task_settings
    from backend.ai.contracts import AITask, FilePart, ProviderRequest, TextPart
    from backend.ai.providers.gemini import GeminiProvider

    evidence = tmp_path / "answer.png"
    evidence.write_bytes(b"\x89PNG synthetic")

    provider = GeminiProvider(api_keys=[])
    uploads = []
    provider._upload = lambda path: uploads.append(path)  # noqa: SLF001

    request = ProviderRequest(
        task=AITask.GRADING,
        parts=(TextPart("grade this"), FilePart(str(evidence))),
        expects_json=True,
    )

    with pytest.raises(ProviderAuthenticationError) as exc:
        await provider.run_text_task(request, get_task_settings(AITask.GRADING))

    assert uploads == [], "a student's file was uploaded before the key was checked"
    assert exc.value.category == "authentication"
    assert exc.value.retryable is False, "retrying cannot install a credential"


def test_the_unconfigured_error_names_the_variable_and_no_key_material():
    from backend.ai.providers.gemini import API_KEY_ENV, GeminiProvider

    message = str(GeminiProvider(api_keys=[])._not_configured())  # noqa: SLF001
    assert API_KEY_ENV in message
    assert "AQ." not in message and "AIza" not in message


def test_a_missing_credential_is_not_a_zero():
    """It maps to a category the grading path records as a missing mark."""
    from backend.ai.errors import ALL_CATEGORIES
    from backend.ai.providers.gemini import GeminiProvider

    error = GeminiProvider(api_keys=[])._not_configured()  # noqa: SLF001
    assert error.category in ALL_CATEGORIES


# ---------------------------------------------------------------------------
# SQL echo
# ---------------------------------------------------------------------------

def test_sql_echo_is_off_by_default(monkeypatch):
    """echo=True logs bound parameters: answers, reasons, marking schemes."""
    import importlib

    import backend.config as config

    monkeypatch.delenv("DATABASE_ECHO", raising=False)
    reloaded = importlib.reload(config)
    try:
        assert reloaded.settings.DATABASE_ECHO is False
    finally:
        importlib.reload(config)


@pytest.mark.parametrize("value,expected", [
    ("true", True), ("True", True), ("1", True), ("yes", True), ("on", True),
    ("false", False), ("0", False), ("", False), ("nonsense", False),
])
def test_sql_echo_opt_in_is_explicit(monkeypatch, value, expected):
    import importlib

    import backend.config as config

    monkeypatch.setenv("DATABASE_ECHO", value)
    reloaded = importlib.reload(config)
    try:
        assert reloaded.settings.DATABASE_ECHO is expected
    finally:
        monkeypatch.delenv("DATABASE_ECHO", raising=False)
        importlib.reload(config)


def test_the_engine_takes_echo_from_configuration():
    """Not a hard-coded True, which is what shipped."""
    source = (REPO_ROOT / "backend" / "database.py").read_text(
        encoding="utf-8", errors="replace"
    )
    assert "echo=settings.DATABASE_ECHO" in source
    assert "echo=True" not in source


# ---------------------------------------------------------------------------
# the Celery entry point
# ---------------------------------------------------------------------------

def test_the_task_does_not_call_get_event_loop():
    """`get_event_loop()` raises RuntimeError on Python 3.12+.

    The Celery task is the only way grading starts in production, so this
    would not run at all on any interpreter newer than the container's 3.11.

    Parsed rather than grepped, so the prose explaining the fix does not count
    as the fault it describes.
    """
    import ast

    tree = ast.parse((REPO_ROOT / "backend" / "tasks.py").read_text(encoding="utf-8"))

    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                called.add(func.id)
            elif isinstance(func, ast.Attribute):
                called.add(func.attr)

    assert "get_event_loop" not in called
    assert "run" in called, "the task no longer starts a loop at all"


@pytest.mark.asyncio
async def test_the_task_job_disposes_the_engine(monkeypatch):
    """`asyncio.run` closes its loop, so pooled connections must not outlive it."""
    import backend.tasks as tasks

    calls = []

    async def _graded(exam_id, student_id):
        calls.append(("graded", exam_id, student_id))

    class _Engine:
        async def dispose(self):
            calls.append(("disposed",))

    monkeypatch.setattr(tasks, "_process_and_grade", _graded)
    monkeypatch.setattr(tasks, "engine", _Engine())

    await tasks._run_exam_job(7, 9)

    assert calls == [("graded", 7, 9), ("disposed",)]


@pytest.mark.asyncio
async def test_the_engine_is_disposed_even_when_grading_raises(monkeypatch):
    import backend.tasks as tasks

    disposed = []

    async def _boom(exam_id, student_id):
        raise RuntimeError("synthetic")

    class _Engine:
        async def dispose(self):
            disposed.append(True)

    monkeypatch.setattr(tasks, "_process_and_grade", _boom)
    monkeypatch.setattr(tasks, "engine", _Engine())

    with pytest.raises(RuntimeError):
        await tasks._run_exam_job(1, 2)
    assert disposed == [True], "a failed task leaked its connections"


# ---------------------------------------------------------------------------
# backend / worker parity
# ---------------------------------------------------------------------------

def test_backend_and_worker_share_one_env_file():
    """A variable the API sees and the worker does not is a silent split brain."""
    compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    backend_block = compose.split("backend:", 1)[1].split("celery_worker:", 1)[0]
    worker_block = compose.split("celery_worker:", 1)[1].split("frontend:", 1)[0]
    assert "env_file: ./.env" in backend_block
    assert "env_file: ./.env" in worker_block


def test_the_template_documents_the_canonical_credential():
    """The template is what a deployer copies; it named the variable nobody reads."""
    from backend.ai.providers.gemini import API_KEY_ENV

    template = (REPO_ROOT / ".env.template").read_text(encoding="utf-8")
    assert f"{API_KEY_ENV}=" in template
    assert "DATABASE_ECHO" in template
    # The legacy name may be mentioned, but never as the active setting.
    for line in template.splitlines():
        stripped = line.strip()
        if stripped.startswith("GEMINI_API_KEY_1="):
            pytest.fail("the template still sets the legacy variable")


def test_the_template_carries_no_secret_values():
    """It is committed; it must stay a list of names."""
    template = (REPO_ROOT / ".env.template").read_text(encoding="utf-8")
    for line in template.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        if name.strip() in ("GEMINI_API_KEY", "SECRET_KEY", "GOOGLE_CLIENT_SECRET",
                            "SMTP_PASSWORD"):
            assert value.strip() == "", f"{name} carries a value in the template"
