import os
import secrets
from dotenv import load_dotenv

load_dotenv()

class Settings:
    PROJECT_NAME = "Institute Classroom Portal"
    PROJECT_VERSION = "1.0.0"
    
    # Environment
    ENV = os.getenv("ENV", "development")
    DEBUG = ENV == "development"
    
    # Security settings
    SECRET_KEY = os.getenv("SECRET_KEY", secrets.token_hex(32))
    ALGORITHM = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", 60 * 24))  # 24 hours
    
    # Cookie settings
    COOKIE_SECURE = ENV != "development"  # Use secure cookies in production
    COOKIE_DOMAIN = os.getenv("COOKIE_DOMAIN", None)
    COOKIE_SAMESITE = "lax"
    
    # Database settings
    DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://myuser:mypassword@localhost:5432/classroom")
    DATABASE_POOL_SIZE = int(os.getenv("DATABASE_POOL_SIZE", 20))
    DATABASE_MAX_OVERFLOW = int(os.getenv("DATABASE_MAX_OVERFLOW", 10))
    DATABASE_POOL_TIMEOUT = int(os.getenv("DATABASE_POOL_TIMEOUT", 30))
    # SQLAlchemy statement echo. OFF unless a developer asks for it: echo logs
    # every statement WITH ITS BOUND PARAMETERS, which for this schema means
    # recognised answer text, grading reasons and marking-scheme content in
    # plain sight. It was unconditionally on.
    DATABASE_ECHO = os.getenv("DATABASE_ECHO", "false").strip().lower() in (
        "1", "true", "yes", "on",
    )
    
    # Email settings (for future use)
    SMTP_USERNAME = os.getenv("SMTP_USERNAME", "")
    SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
    SMTP_SERVER = os.getenv("SMTP_SERVER", "")
    SMTP_PORT = os.getenv("SMTP_PORT", "")

    # Google Sign In
    GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
    GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
    # The callback Google sends the browser back to, stated explicitly rather
    # than derived from the incoming request. See GOOGLE_OAUTH_SETTINGS below.
    GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI")


    FRONTEND_BASE_URL = "http://127.0.0.1:8000"

settings = Settings()


# ---------------------------------------------------------------------------
# Google sign-in configuration
#
# Three variables, and Google sign-in is enabled only when all three are set.
# Partial configuration is the failure this exists to stop: with the client id
# absent the app still started, still offered the button, and still redirected
# to Google -- which answered `401 invalid_client`, a message that says nothing
# about which variable is missing.
#
# GOOGLE_REDIRECT_URI is not derivable from the request. The bundled nginx
# rewrites `^/api/(.*)$ -> /$1` before proxying, so FastAPI never sees the
# `/api` prefix the browser used and `request.url_for()` generates a path that
# is not externally reachable. Google also requires the redirect URI to match a
# registered value EXACTLY, so it is deployment configuration by nature, not
# something to infer -- and stating it survives TLS termination, where a
# derived URL would claim http://.
# ---------------------------------------------------------------------------

GOOGLE_OAUTH_SETTINGS = ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REDIRECT_URI")


def missing_google_oauth_settings(env=None):
    """Names (never values) of the Google sign-in variables that are not set.

    Reads the environment at call time rather than at import, so a caller --
    or a test -- sees the current environment.
    """
    source = os.environ if env is None else env
    return [name for name in GOOGLE_OAUTH_SETTINGS if not (source.get(name) or "").strip()]


def google_oauth_enabled(env=None) -> bool:
    """True only when every Google sign-in variable is set."""
    return not missing_google_oauth_settings(env)


def google_redirect_uri(env=None):
    """The configured callback, read from the same place as the enabled check.

    Deliberately not `settings.GOOGLE_REDIRECT_URI`: that is captured at import,
    so a route reading it while `google_oauth_enabled()` read the live
    environment could disagree with itself. One source, one answer.
    """
    source = os.environ if env is None else env
    return (source.get("GOOGLE_REDIRECT_URI") or "").strip() or None