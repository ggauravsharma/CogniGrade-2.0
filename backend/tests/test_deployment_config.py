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
import pytest_asyncio
# Module level so `from __future__ import annotations` can still resolve it:
# FastAPI reads a route's annotations out of its MODULE globals, and a name
# bound inside the fixture below would look to it like a query parameter.
from fastapi import Request

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


# ---------------------------------------------------------------------------
# Google sign-in
#
# Two independent faults shipped together and produced one unhelpful message.
#
#   1. GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET were unset, so an EMPTY client id
#      went to Google, which answered `401 invalid_client -- The OAuth client
#      was not found`. The app started, rendered the button, and said nothing.
#
#   2. The callback was `request.url_for("auth_via_google")`. nginx rewrites
#      `^/api/(.*)$ -> /$1` before proxying, so the backend never sees the
#      `/api` prefix the browser used and generated
#      `http://localhost/auth/google` -- which nginx routes to the FRONTEND
#      container and 404s. That fault survived credentials being added.
#
# No network, no credential, no quota.
# ---------------------------------------------------------------------------

OAUTH_ENV = ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REDIRECT_URI")
CALLBACK = "http://localhost/api/auth/google"

# The codes the routes put on the login URL. Values, not text: the browser must
# never be handed anything Authlib or Google produced.
OAUTH_SESSION_EXPIRED = "google_session_expired"
OAUTH_UNAVAILABLE = "google_unavailable"
OAUTH_FAILED = "google_failed"


def _set_oauth_env(monkeypatch, **overrides):
    """Configure Google sign-in with obvious non-secrets."""
    values = {
        "GOOGLE_CLIENT_ID": "test-client-id.apps.googleusercontent.com",
        "GOOGLE_CLIENT_SECRET": "test-client-secret",
        "GOOGLE_REDIRECT_URI": CALLBACK,
    }
    values.update(overrides)
    for name in OAUTH_ENV:
        value = values.get(name)
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


# --- the enabled/disabled rule -------------------------------------------

def test_google_sign_in_needs_all_three_variables(monkeypatch):
    from backend.config import google_oauth_enabled, missing_google_oauth_settings

    _set_oauth_env(monkeypatch)
    assert google_oauth_enabled() is True
    assert missing_google_oauth_settings() == []


@pytest.mark.parametrize("absent", OAUTH_ENV)
def test_any_missing_variable_disables_google_sign_in(monkeypatch, absent):
    """Partial configuration is the bug, so it must not count as enabled."""
    from backend.config import google_oauth_enabled, missing_google_oauth_settings

    _set_oauth_env(monkeypatch, **{absent: None})
    assert google_oauth_enabled() is False
    assert missing_google_oauth_settings() == [absent]


def test_a_blank_value_counts_as_missing(monkeypatch):
    """`GOOGLE_CLIENT_ID=` in a .env is what actually shipped."""
    from backend.config import google_oauth_enabled, missing_google_oauth_settings

    _set_oauth_env(monkeypatch, GOOGLE_CLIENT_ID="   ")
    assert google_oauth_enabled() is False
    assert "GOOGLE_CLIENT_ID" in missing_google_oauth_settings()


def test_nothing_configured_reports_every_name(monkeypatch):
    from backend.config import missing_google_oauth_settings

    for name in OAUTH_ENV:
        monkeypatch.delenv(name, raising=False)
    assert missing_google_oauth_settings() == list(OAUTH_ENV)


def test_missing_settings_reports_names_never_values(monkeypatch):
    from backend.config import missing_google_oauth_settings

    _set_oauth_env(monkeypatch, GOOGLE_CLIENT_SECRET=None)
    reported = missing_google_oauth_settings()
    assert reported == ["GOOGLE_CLIENT_SECRET"]
    joined = " ".join(reported)
    assert "test-client-id" not in joined and "test-client-secret" not in joined


# --- the routes -----------------------------------------------------------

@pytest_asyncio.fixture
async def auth_client():
    """An ASGI client over the real auth router, with the session Authlib needs.

    Two probe routes let a test read and seed the Starlette session without
    unpicking the signed cookie by hand. They live on this throwaway app only;
    the application under test never gains them.
    """
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from starlette.middleware.sessions import SessionMiddleware

    from backend.routers import auth as auth_router

    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-session-key")
    app.include_router(auth_router.router)

    @app.get("/__probe__/session-keys")
    async def _session_keys(request: Request):
        return sorted(request.session.keys())

    @app.get("/__probe__/seed-state")
    async def _seed_state(request: Request, state: str):
        # The shape Authlib parks in the session when a flow starts.
        request.session[f"_state_google_{state}"] = {"data": {}, "exp": 0}
        return {"seeded": state}

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


def _assert_lands_on_login(res, code=None):
    """Both OAuth routes are browser navigations, so a failure must be a PAGE.

    Never a JSON body: the user reads whatever these routes return.
    """
    assert res.status_code == 303, f"expected a redirect, got {res.status_code}"
    target = res.headers.get("location", "")
    assert target.startswith("/login.htm"), f"must return to the login page, got {target}"
    assert "accounts.google.com" not in target
    if code is not None:
        assert f"auth_error={code}" in target, target


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/login/google", "/auth/google"])
async def test_unconfigured_oauth_returns_to_login_not_to_google(monkeypatch, auth_client, path):
    """Back to a page the user can use, instead of Google's `401 invalid_client`."""
    for name in OAUTH_ENV:
        monkeypatch.delenv(name, raising=False)

    res = await auth_client.get(path)
    _assert_lands_on_login(res, OAUTH_UNAVAILABLE)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/login/google", "/auth/google"])
async def test_unconfigured_oauth_shows_no_raw_json(monkeypatch, auth_client, path):
    """The raw `{"success":false,...}` page the button used to render is gone."""
    for name in OAUTH_ENV:
        monkeypatch.delenv(name, raising=False)

    res = await auth_client.get(path)
    assert "application/json" not in res.headers.get("content-type", "")
    assert '"success"' not in res.text and '"error"' not in res.text


@pytest.mark.asyncio
async def test_the_refusal_names_no_configuration_to_the_browser(monkeypatch, auth_client):
    for name in OAUTH_ENV:
        monkeypatch.delenv(name, raising=False)

    res = await auth_client.get("/login/google")
    exposed = res.text + " " + res.headers.get("location", "")
    for leak in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REDIRECT_URI",
                 "client_id", "client_secret", "Traceback"):
        assert leak not in exposed, f"the refusal leaks {leak} to the browser"


@pytest.mark.asyncio
@pytest.mark.parametrize("absent", OAUTH_ENV)
async def test_partial_configuration_is_also_refused(monkeypatch, auth_client, absent):
    """No `importlib.reload` here on purpose.

    Reloading `backend.config` re-runs `load_dotenv()`, which puts a developer's
    real `.env` back over the variable this test just removed -- the test then
    silently checked the opposite of what it claims. The helpers read
    `os.environ` at CALL time precisely so no reload is needed.
    """
    _set_oauth_env(monkeypatch, **{absent: None})

    res = await auth_client.get("/login/google")
    _assert_lands_on_login(res, OAUTH_UNAVAILABLE)


# --- the callback URL -----------------------------------------------------

@pytest.mark.asyncio
async def test_the_callback_url_is_the_configured_one(monkeypatch, auth_client):
    """The whole point: NOT derived from a path nginx already rewrote."""
    import importlib
    from urllib.parse import parse_qs, urlparse

    import backend.config as config  # noqa: F401  (imported for symmetry)

    _set_oauth_env(monkeypatch)
    try:
        res = await auth_client.get("/login/google")
        assert res.status_code in (302, 307), res.status_code
        target = res.headers["location"]
        assert target.startswith("https://accounts.google.com/"), target

        sent = parse_qs(urlparse(target).query)["redirect_uri"][0]
        assert sent == CALLBACK, f"callback must be the configured value, got {sent}"
        assert sent != "http://testserver/auth/google", (
            "the callback was derived from the request again"
        )
        assert "/api/" in sent, "the externally reachable callback carries the /api prefix"
    finally:
        pass


def test_the_route_no_longer_derives_the_callback_from_the_request():
    """url_for cannot see the prefix nginx stripped, so it must not be used here."""
    source = (REPO_ROOT / "backend" / "routers" / "auth.py").read_text(
        encoding="utf-8", errors="replace"
    )
    assert 'request.url_for("auth_via_google")' not in source
    assert "google_redirect_uri()" in source


# --- a failed callback is a page, not a stack trace ------------------------
#
# Live failure, second Chrome profile: Google returned to the correct callback
# and Authlib raised `MismatchingStateError: CSRF Warning! State not equal in
# request and response.` The route caught it and rendered
# `{"success":false,"error":"Google OAuth failed"}` -- raw JSON, in a top-level
# browser navigation, with no way back to the login page.
#
# The check itself is correct and stays exactly as Authlib performs it. These
# tests hold BOTH halves: it must still reject, and the rejection must be
# something a user can act on that carries no detail out of the server.
# ---------------------------------------------------------------------------

STALE_STATE = "state-from-some-other-session"
AUTH_CODE = "4-0AfakeAuthorizationCode"


@pytest.mark.asyncio
async def test_a_stale_state_returns_the_user_to_login(monkeypatch, auth_client):
    _set_oauth_env(monkeypatch)

    res = await auth_client.get("/auth/google", params={"state": STALE_STATE, "code": AUTH_CODE})
    _assert_lands_on_login(res, OAUTH_SESSION_EXPIRED)


@pytest.mark.asyncio
async def test_the_state_failure_exposes_nothing_to_the_browser(monkeypatch, auth_client):
    """Not the exception, not the state, not the authorization code."""
    _set_oauth_env(monkeypatch)

    res = await auth_client.get("/auth/google", params={"state": STALE_STATE, "code": AUTH_CODE})
    exposed = res.text + " " + res.headers.get("location", "")
    for leak in ("MismatchingStateError", "mismatching_state", "CSRF", "Traceback",
                 "authlib", "Authlib", '"success"', "Google OAuth failed",
                 STALE_STATE, AUTH_CODE):
        assert leak not in exposed, f"the state failure leaks {leak} to the browser"


@pytest.mark.asyncio
async def test_a_callback_with_no_state_at_all_is_handled(monkeypatch, auth_client):
    """Google always sends one; a replayed or hand-typed callback URL does not."""
    _set_oauth_env(monkeypatch)

    res = await auth_client.get("/auth/google", params={"code": AUTH_CODE})
    _assert_lands_on_login(res)
    assert AUTH_CODE not in (res.text + res.headers.get("location", ""))


@pytest.mark.asyncio
async def test_a_provider_error_is_never_reflected_back(monkeypatch, auth_client):
    """Google's own `?error=` text is attacker-reachable; it must not be echoed."""
    _set_oauth_env(monkeypatch)

    res = await auth_client.get("/auth/google", params={
        "error": "access_denied",
        "error_description": "denied <script>alert(1)</script>",
        "state": STALE_STATE,
    })
    _assert_lands_on_login(res, OAUTH_FAILED)
    exposed = res.text + " " + res.headers.get("location", "")
    assert "access_denied" not in exposed
    assert "<script>" not in exposed


@pytest.mark.asyncio
async def test_a_failed_callback_leaves_no_state_behind(monkeypatch, auth_client):
    """So the next attempt starts clean.

    Authlib removes its `_state_google_<state>` session entry only on the
    SUCCESS path. Left behind, the entries accumulate in the session cookie
    until it is too large to send -- and then every later attempt fails the
    same way, which is a dead end the user cannot get out of.
    """
    _set_oauth_env(monkeypatch)

    await auth_client.get("/__probe__/seed-state", params={"state": "leftover-one"})
    await auth_client.get("/__probe__/seed-state", params={"state": "leftover-two"})
    seeded = (await auth_client.get("/__probe__/session-keys")).json()
    assert [k for k in seeded if k.startswith("_state_google_")], seeded

    await auth_client.get("/auth/google", params={"state": STALE_STATE, "code": AUTH_CODE})

    after = (await auth_client.get("/__probe__/session-keys")).json()
    assert not [k for k in after if k.startswith("_state_google_")], after


@pytest.mark.asyncio
async def test_a_fresh_retry_starts_a_new_flow_after_a_failure(monkeypatch, auth_client):
    """The recovery has to actually recover. No network, no live credential."""
    from starlette.responses import RedirectResponse

    from backend.routers import auth as auth_module

    _set_oauth_env(monkeypatch)
    failed = await auth_client.get("/auth/google", params={"state": STALE_STATE, "code": AUTH_CODE})
    _assert_lands_on_login(failed, OAUTH_SESSION_EXPIRED)

    started = {}

    async def fake_authorize_redirect(request, redirect_uri=None, **kwargs):
        started["redirect_uri"] = redirect_uri
        request.session["_state_google_fresh"] = {"data": {}, "exp": 0}
        return RedirectResponse("https://accounts.google.com/o/oauth2/v2/auth", status_code=302)

    monkeypatch.setattr(auth_module.oauth.google, "authorize_redirect", fake_authorize_redirect)

    res = await auth_client.get("/login/google")
    assert res.status_code == 302
    assert res.headers["location"].startswith("https://accounts.google.com/")
    assert started["redirect_uri"] == CALLBACK, "the retry must use the configured callback"

    keys = (await auth_client.get("/__probe__/session-keys")).json()
    assert "_state_google_fresh" in keys, "the retry stored a new state"


@pytest.mark.asyncio
async def test_email_password_login_still_works_after_an_oauth_failure(monkeypatch, auth_client):
    """The OAuth session cleanup must not take the rest of the session with it."""
    _set_oauth_env(monkeypatch)
    await auth_client.get("/auth/google", params={"state": STALE_STATE, "code": AUTH_CODE})

    res = await auth_client.post("/login", json={"email": "", "password": ""})
    assert res.status_code == 400
    assert "fill in all fields" in res.text.lower()


# --- what the login page does with the code -------------------------------

def _login_page() -> str:
    return (REPO_ROOT / "frontend" / "login.htm").read_text(encoding="utf-8", errors="replace")


def test_the_routes_and_the_page_agree_on_the_codes():
    from backend.routers import auth as auth_module

    assert auth_module.OAUTH_ERROR_SESSION_EXPIRED == OAUTH_SESSION_EXPIRED
    assert auth_module.OAUTH_ERROR_UNAVAILABLE == OAUTH_UNAVAILABLE
    assert auth_module.OAUTH_ERROR_FAILED == OAUTH_FAILED
    assert auth_module.LOGIN_PAGE == "/login.htm"
    assert (REPO_ROOT / "frontend" / "login.htm").exists(), "the redirect target must exist"


def test_the_login_page_explains_every_code_the_routes_send():
    page = _login_page()
    for code in (OAUTH_SESSION_EXPIRED, OAUTH_UNAVAILABLE, OAUTH_FAILED):
        assert code in page, f"login.htm has no message for {code}"
    assert "Google sign-in session expired. Please try again." in page


def test_the_login_page_renders_the_message_as_text_not_markup():
    """`?auth_error=` is attacker-controlled, so only fixed sentences are shown."""
    page = _login_page()
    start = page.index("showGoogleSignInError")
    handler = page[start:page.index("visibilitychange", start)]
    assert "auth_error" in handler
    assert "textContent" in handler
    assert "innerHTML" not in handler, "the code must never be written as markup"


# --- route compatibility behind the /api prefix ---------------------------

def test_nginx_strips_the_api_prefix_before_proxying():
    """The fact the callback configuration exists to accommodate."""
    conf = (REPO_ROOT / "nginx" / "nginx.conf").read_text(encoding="utf-8", errors="replace")
    assert "rewrite ^/api/(.*)$ /$1 break;" in conf
    assert "proxy_pass $backend_upstream;" in conf
    assert "set $backend_upstream http://backend:8000;" in conf


# ---------------------------------------------------------------------------
# nginx must survive the backend being recreated
#
# Live failure: after `docker compose -p dep25-g06-cognigrade up -d --build`,
# every /api/ call answered `502 Bad Gateway` until someone ran
# `docker restart portal_nginx` by hand.
#
# Cause, reproduced deterministically: nginx resolves a host name written
# LITERALLY in `proxy_pass` exactly once, when the configuration is loaded, and
# reuses that address for the life of the worker. A recreated container can come
# back on a different address on the compose network, and nginx went on dialling
# the old one. The reproduction held the backend's address with a throwaway
# container so the recreated backend had to move; nginx then logged
#
#   connect() failed (111: Connection refused) while connecting to upstream,
#   upstream: "http://172.19.0.5:8000/check-session"
#
# while the backend answered 401 normally on its new address from inside the
# nginx container itself.
#
# The fix is a request-time lookup: name each upstream through a variable and
# point nginx at Docker's embedded DNS. What can be asserted from the repository
# is asserted below; the end-to-end behaviour needs a Docker daemon, so the
# deterministic manual procedure is in the docstring of the last test here.
# ---------------------------------------------------------------------------

def _nginx_conf() -> str:
    return (REPO_ROOT / "nginx" / "nginx.conf").read_text(encoding="utf-8", errors="replace")


def test_nginx_has_a_resolver_so_upstreams_can_be_looked_up_per_request():
    conf = _nginx_conf()
    assert "resolver 127.0.0.11" in conf, (
        "no resolver: a variable in proxy_pass cannot be resolved at request time"
    )
    assert "valid=" in conf, "an unbounded cache is the bug again, just slower"


@pytest.mark.parametrize("literal", [
    "proxy_pass http://backend:8000;",
    "proxy_pass http://frontend:80;",
])
def test_no_upstream_is_named_literally_in_proxy_pass(literal):
    """A literal host name here is resolved once and cached until nginx restarts."""
    assert literal not in _nginx_conf(), f"{literal} pins the address at config load"


@pytest.mark.parametrize("variable", ["$backend_upstream", "$frontend_upstream"])
def test_every_upstream_goes_through_a_variable(variable):
    conf = _nginx_conf()
    assert f"proxy_pass {variable};" in conf
    assert f"set {variable} http://" in conf


def _compose_service_block(name: str) -> str:
    """The indented lines belonging to one service in docker-compose.yml."""
    compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8", errors="replace")
    lines = compose.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"  {name}:"))
    block = []
    for line in lines[start + 1:]:
        if line.strip() and not line.startswith("    "):
            break
        block.append(line)
    return "\n".join(block)


@pytest.mark.parametrize("service", ["backend", "frontend", "postgres"])
def test_the_fix_does_not_publish_an_internal_service_to_the_host(service):
    """Fixing the proxy must not turn into punching a hole around it."""
    block = _compose_service_block(service)
    assert "ports:" not in block, f"{service} must stay reachable only on the compose network"
    assert "internal" in block, f"{service} must stay on the internal network"


def test_only_nginx_faces_the_host_for_web_traffic():
    """And the manual check, for the part a Docker-less test run cannot do.

    Deterministic reproduction and verification, from the repository root::

        NET=dep25-g06-cognigrade_internal
        docker compose -p dep25-g06-cognigrade up -d --build
        curl -o /dev/null -w '%{http_code}\\n' http://localhost/api/check-session   # 401

        # force the backend onto a different address without restarting nginx
        docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' portal_backend
        docker stop portal_backend
        docker run -d --rm --name ip_decoy --network $NET alpine sleep 300  # takes the old address
        docker start portal_backend                                        # lands on a new one
        sleep 15
        curl -o /dev/null -w '%{http_code}\\n' http://localhost/api/check-session

    Before the fix that last call is 502 and stays 502 until
    ``docker restart portal_nginx``. After it, it is 401 within the resolver's
    ``valid=`` window, with no manual restart. Clean up with
    ``docker rm -f ip_decoy``.
    """
    block = _compose_service_block("nginx")
    assert "ports:" in block and '"80:80"' in block


def test_the_callback_route_is_declared_without_the_api_prefix():
    """So that /api/auth/google, once rewritten to /auth/google, lands on it."""
    from backend.routers import auth as auth_module

    paths = {r.path for r in auth_module.router.routes}
    assert "/auth/google" in paths
    assert "/login/google" in paths
    assert not any(p.startswith("/api/") for p in paths), (
        "the app must not carry the prefix itself; nginx removes it"
    )


def test_the_documented_callback_matches_the_declared_route():
    """`/api` + the declared path is what the template tells operators to register."""
    template = (REPO_ROOT / ".env.template").read_text(encoding="utf-8", errors="replace")
    assert "http://localhost/api/auth/google" in template


# --- email/password login is untouched ------------------------------------

@pytest.mark.asyncio
async def test_email_password_login_does_not_depend_on_oauth(monkeypatch, auth_client):
    """Disabling Google sign-in must not disable the login that always worked."""
    for name in OAUTH_ENV:
        monkeypatch.delenv(name, raising=False)

    res = await auth_client.post("/login", json={"email": "", "password": ""})
    # Reaches the handler and fails on its own validation, not on OAuth config.
    assert res.status_code == 400
    assert "fill in all fields" in res.text.lower()


# --- the template ---------------------------------------------------------

def test_the_template_documents_every_oauth_variable():
    template = (REPO_ROOT / ".env.template").read_text(encoding="utf-8", errors="replace")
    for name in OAUTH_ENV:
        assert f"{name}=" in template, f"the template does not document {name}"


def test_the_template_carries_no_oauth_values():
    """It is committed; these three are credentials or deployment identity."""
    template = (REPO_ROOT / ".env.template").read_text(encoding="utf-8", errors="replace")
    for line in template.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        if name.strip() in OAUTH_ENV:
            assert value.strip() == "", f"{name} carries a value in the template"


# ---------------------------------------------------------------------------
# the access log
#
# Verified live: after a callback, `docker logs portal_backend` contained
#
#   INFO: "GET /auth/google?state=bogus-state-value&code=4-0AfakeCode
#          HTTP/1.1" 303 See Other
#
# written by uvicorn, not by application code -- so a one-use authorization
# code and the flow's CSRF state were logged on every sign-in, successful or
# not, whatever the route itself chose to log.
# ---------------------------------------------------------------------------

def _access_record(path):
    """A record shaped the way uvicorn's access logger builds one."""
    import logging

    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("172.19.0.5:33872", "GET", path, "1.1", 303),
        exc_info=None,
    )


@pytest.mark.parametrize("secret", ["code", "state", "access_token", "id_token"])
def test_a_credential_bearing_query_is_redacted_from_logs(secret):
    from backend.main import RedactSensitiveQuery

    record = _access_record(f"/auth/google?{secret}=SUPER-SECRET-VALUE&hl=en")
    RedactSensitiveQuery().filter(record)

    rendered = record.getMessage()
    assert "SUPER-SECRET-VALUE" not in rendered, f"{secret} still reaches the log"
    assert "/auth/google" in rendered, "the route must still be identifiable"
    assert "303" in rendered, "the status must still be there"


def test_an_ordinary_query_is_left_alone():
    """Redaction that swallowed every query string would not be worth having."""
    from backend.main import RedactSensitiveQuery

    record = _access_record("/exams/12/scripts?page=2")
    RedactSensitiveQuery().filter(record)
    assert "page=2" in record.getMessage()


def test_a_record_with_no_arguments_survives_the_filter():
    import logging

    from backend.main import RedactSensitiveQuery

    record = logging.LogRecord(
        name="uvicorn.error", level=logging.INFO, pathname=__file__, lineno=1,
        msg="Application startup complete.", args=(), exc_info=None,
    )
    assert RedactSensitiveQuery().filter(record) is True
    assert record.getMessage() == "Application startup complete."


def test_the_redaction_is_installed_on_the_access_logger():
    """Attaching it to the module is not the same as it being in the path."""
    import logging

    import backend.main  # noqa: F401  (installs the filter on import)

    filters = logging.getLogger("uvicorn.access").filters
    assert any(f.__class__.__name__ == "RedactSensitiveQuery" for f in filters)
