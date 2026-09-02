from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from contextlib import asynccontextmanager
import os
import logging

# Base is no longer imported here: the schema decision moved to db_bootstrap.
from backend.database import engine, get_db
from backend.db_bootstrap import bootstrap_schema
from backend.routers import auth, classes, enrollments, notifications, announcements, exams, geminiAPI, studentBackend, peopleManagement, examStats, user_routes, studentEdit, routingTasks, regions
from backend.auth import files as protected_files
from backend.config import (
    GOOGLE_OAUTH_SETTINGS,
    missing_google_oauth_settings,
    settings,
)

from fastapi.staticfiles import StaticFiles

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# Query parameters that must never reach a log line, on any route.
#
# Google returns the browser to `/auth/google?state=...&code=...`, and uvicorn
# writes the request line to its access log verbatim -- so the one-use
# authorization code and the flow's CSRF state were sitting in
# `docker logs portal_backend` after every sign-in, successful or not. The
# access line is still written, and still says which route was reached with
# what status; only the values are dropped.
SENSITIVE_QUERY_KEYS = frozenset({
    "code", "state", "id_token", "access_token", "refresh_token", "token",
})


def _redact_query(value: str) -> str:
    if not value.startswith("/") or "?" not in value:
        return value
    path, _, query = value.partition("?")
    names = {pair.partition("=")[0] for pair in query.split("&")}
    return f"{path}?<redacted>" if names & SENSITIVE_QUERY_KEYS else value


class RedactSensitiveQuery(logging.Filter):
    """Strip credential-bearing query strings out of log records.

    Applied to the record's arguments rather than the formatted message, and by
    shape rather than by position, so it does not depend on how uvicorn happens
    to order the arguments of its access line in a given version.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple):
            redacted = tuple(
                _redact_query(a) if isinstance(a, str) else a for a in args
            )
            if redacted != args:
                record.args = redacted
        return True


def _install_query_redaction() -> None:
    """On uvicorn's access logger, and on whatever basicConfig put on root."""
    redactor = RedactSensitiveQuery()
    logging.getLogger("uvicorn.access").addFilter(redactor)
    for handler in logging.getLogger().handlers:
        handler.addFilter(redactor)


_install_query_redaction()

def _report_google_oauth_configuration() -> None:
    """Say at startup whether Google sign-in will actually work.

    A half-configured deployment used to start silently, render the Google
    button, and fail only once a user clicked it -- as `401 invalid_client`
    from Google, which names nothing. Deliberately not fatal: Google sign-in is
    optional and email/password login does not depend on it, so a deployment
    that sets none of these is normal and gets an INFO line. Setting SOME of
    them is a mistake and gets a WARNING naming exactly which are unset.

    Names only. The values are credentials.
    """
    missing = missing_google_oauth_settings()
    if not missing:
        logger.info("Google sign-in is configured.")
    elif len(missing) == len(GOOGLE_OAUTH_SETTINGS):
        logger.info(
            "Google sign-in is not configured and is disabled; email and "
            "password login is unaffected."
        )
    else:
        logger.warning(
            "Google sign-in is PARTIALLY configured and will be refused at the "
            "route rather than failing at Google. Unset: %s",
            ", ".join(missing),
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup. Alembic is the schema authority; create_all now runs only on a
    # genuinely empty database, and that database is stamped so the two agree.
    # See backend/db_bootstrap.py for the three cases and why each behaves the
    # way it does.
    await bootstrap_schema(engine)
    _report_google_oauth_configuration()
    yield
    # Shutdown
    await engine.dispose()  # closes all connections

app = FastAPI(title=settings.PROJECT_NAME, version=settings.PROJECT_VERSION, lifespan=lifespan)

# Serve static files with HTML support (so index.html is served as the default)
# app.mount("/static", StaticFiles(directory="frontend", html=True), name="static")         ### NOT IN DEPLOYMENT

# SECURITY: "/uploads" was previously mounted as StaticFiles, which served every
# answer script, marking scheme and cropped answer image to anyone who knew a
# URL, with no authentication. Uploaded files are now served exclusively through
# backend.auth.files, which authorizes the caller against the owning exam and
# resolves the path from the database rather than from the request.
# Do not re-add a StaticFiles mount over ./uploads.

# Mount profile pictures directory
os.makedirs("profile_pictures", exist_ok=True)
app.mount("/profile_pictures", StaticFiles(directory="profile_pictures"), name="profile_pictures")

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_middleware(SessionMiddleware, secret_key=settings.SECRET_KEY)

# Include routers
app.include_router(auth.router)
app.include_router(classes.router)
app.include_router(enrollments.router)
app.include_router(notifications.router)
app.include_router(announcements.router)
app.include_router(exams.router)
app.include_router(geminiAPI.router)
app.include_router(peopleManagement.router)
app.include_router(studentBackend.router)
app.include_router(examStats.router)  
app.include_router(studentEdit.router)  
app.include_router(user_routes.router)
app.include_router(routingTasks.router)
app.include_router(regions.router)
app.include_router(protected_files.router)

@app.get("/")
async def root(request: Request):
    return RedirectResponse(url="/login.htm")

@app.get("/health")
async def health_check():
    return {"status": "ok", "version": settings.PROJECT_VERSION}

@app.middleware("http")
async def add_process_time_header(request: Request, call_next):
    try:
        response = await call_next(request)
        return response
    except Exception as e:
        logger.error(f"Error in middleware: {str(e)}", exc_info=True)
        raise

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.main:app", reload=True)
