from datetime import timedelta, datetime, timezone
import secrets
import logging
import os

from fastapi import APIRouter, Depends, HTTPException, Request, status, Response, BackgroundTasks
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from jose import JWTError, jwt
import smtplib
from email.message import EmailMessage

# Existing imports
from backend.config import (
    google_oauth_enabled,
    google_redirect_uri,
    missing_google_oauth_settings,
    settings,
)
from backend.database import get_db      # ← changed
from backend.models.users import User
from backend.utils.security import (
    get_password_hash,
    verify_password,
    create_access_token,
    get_current_user_from_cookie,
)
from backend.utils.validators import validate_email, validate_password

# NEW: Import Authlib’s OAuth tools
from authlib.integrations.base_client import MismatchingStateError
from authlib.integrations.starlette_client import OAuth, OAuthError

logger = logging.getLogger(__name__)
router = APIRouter(tags=["auth"])

# Set up the OAuth instance and register Google OAuth.
oauth = OAuth()
oauth.register(
    name="google",
    client_id=settings.GOOGLE_CLIENT_ID,
    client_secret=settings.GOOGLE_CLIENT_SECRET,
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile"},
)


@router.post("/login")
async def login(
    data: dict,
    response: Response,
    db: AsyncSession = Depends(get_db),      # ← changed
):
    email = (data.get("email") or "").lower().strip()
    password = data.get("password")
    remember = data.get("remember", False)

    if not email or not password:
        return JSONResponse(status_code=400, content={"success": False, "error": "Please fill in all fields"})

    # 1) build and run select
    stmt = select(User).where(User.email == email)
    result = await db.execute(stmt)
    user = result.scalars().first()

    if not user or not verify_password(password, user.hashed_password):
        logger.warning(f"Login attempt failed for email {email}")
        return JSONResponse(status_code=401, content={"success": False, "error": "Incorrect email or password"})

    user.last_login = datetime.now(timezone.utc)
    await db.commit()

    access_token_expires = timedelta(
        minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 7 if remember else settings.ACCESS_TOKEN_EXPIRE_MINUTES
    )
    access_token = create_access_token(
        data={"sub": str(user.id)},
        expires_delta=access_token_expires
    )

    resp = JSONResponse({"success": True, "message": "Login successful", "redirect": "/dashboard.htm"})
    resp.set_cookie(
        key="access_token",
        value=access_token,
        httponly=True,
        max_age=60*60*24*7 if remember else settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        path="/",
        secure=settings.COOKIE_SECURE,
        domain=settings.COOKIE_DOMAIN,
        samesite=settings.COOKIE_SAMESITE
    )
    logger.info(f"User {user.email} logged in successfully")
    return resp


@router.post("/signup")
async def signup(
    data: dict,
    response: Response,
    db: AsyncSession = Depends(get_db),      # ← changed
):
    full_name = data.get("full_name", "").strip()
    email = (data.get("email") or "").lower().strip()
    password = data.get("password")
    confirm_password = data.get("confirm_password")
    is_professor = data.get("is_professor", False)

    if not all([full_name, email, password, confirm_password]):
        return JSONResponse(status_code=400, content={"success": False, "error": "Please fill in all fields"})

    if not validate_email(email):
        return JSONResponse(status_code=400, content={"success": False, "error": "Invalid email format"})

    if password != confirm_password:
        return JSONResponse(status_code=400, content={"success": False, "error": "Passwords do not match"})

    valid, err = validate_password(password)
    if not valid:
        return JSONResponse(status_code=400, content={"success": False, "error": err})

    # check for existing user
    stmt = select(User).where(User.email == email)
    result = await db.execute(stmt)
    if result.scalars().first():
        return JSONResponse(status_code=400, content={"success": False, "error": "Email already registered"})

    # create
    hashed_password = get_password_hash(password)
    user = User(
        email=email,
        hashed_password=hashed_password,
        full_name=full_name,
        is_professor=is_professor
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    access_token = create_access_token(
        data={"sub": str(user.id)},
        expires_delta=timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    resp = JSONResponse({"success": True, "message": "Signup successful", "redirect": "/dashboard.htm"})
    resp.set_cookie(
        key="access_token",
        value=access_token,
        httponly=True,
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        path="/",
        secure=settings.COOKIE_SECURE,
        domain=settings.COOKIE_DOMAIN,
        samesite=settings.COOKIE_SAMESITE
    )
    logger.info(f"New user registered successfully: {user.email}")
    return resp


@router.get("/check-session")
async def check_session(request: Request, db: AsyncSession = Depends(get_db)):
    user = await get_current_user_from_cookie(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Session expired")
    return {"status": "ok"}


@router.get("/logout")
async def logout(request: Request):
    logger.info("Processing logout request")
    response = RedirectResponse(url="/login.htm", status_code=303)
    response.delete_cookie(key="access_token", path="/")
    response.set_cookie(
        key="access_token",
        value="",
        max_age=0,
        path="/",
        expires="Thu, 01 Jan 1970 00:00:00 GMT"
    )
    response.headers.update({
        "Cache-Control": "no-cache, no-store, must-revalidate, private",
        "Pragma": "no-cache",
        "Expires": "0"
    })
    return response


# ========= Google OAuth Endpoints =========

# The page the browser is on when either OAuth route is entered, and the only
# place either one sends it when the flow does not complete.
LOGIN_PAGE = "/login.htm"

# Both OAuth routes are TOP-LEVEL BROWSER NAVIGATIONS -- the Google button is a
# `window.location.href`, and Google itself navigates the browser to the
# callback. Whatever they return is rendered as a page, so a JSON body is a
# wall of raw JSON in front of the user; the failing callback showed
# `{"success":false,"error":"Google OAuth failed"}` with no way back.
#
# So a failure redirects to the login page carrying a CODE. A code, never text:
# nothing Authlib, Google or an exception said is copied into the URL, so a
# failure cannot put a state value, an authorization code, a token or an email
# address into the address bar, the browser history or a referrer header.
# login.htm holds the one sentence each code maps to.
OAUTH_ERROR_SESSION_EXPIRED = "google_session_expired"
OAUTH_ERROR_UNAVAILABLE = "google_unavailable"
OAUTH_ERROR_FAILED = "google_failed"


def _back_to_login(code: str) -> RedirectResponse:
    """Return the browser to the login page with a code login.htm can explain."""
    return RedirectResponse(url=f"{LOGIN_PAGE}?auth_error={code}", status_code=303)


def _clear_google_oauth_state(request: Request) -> None:
    """Drop this session's leftover Google OAuth state so a retry starts clean.

    Authlib parks the state it generated in the Starlette session under
    `_state_google_<state>`, and removes it itself only on the SUCCESS path. A
    failed callback leaves the entry behind, and on Authlib versions that do
    not prune on the next attempt they accumulate in the session cookie until
    it is too large to be sent -- at which point every further attempt fails
    the same way and the user can never get out of it.

    The keys are removed, never read and never logged: a state value is
    precisely the kind of thing that must not reach a log line.
    """
    try:
        session = request.session
    except (AssertionError, AttributeError):
        # No SessionMiddleware on this app; nothing was stored to clear.
        return
    for key in [k for k in session if k.startswith("_state_google_")]:
        session.pop(key, None)


def _google_oauth_unavailable():
    """What both OAuth routes do when Google sign-in is not configured.

    Done INSTEAD of redirecting to Google. Sending an empty client_id got
    `401 invalid_client -- The OAuth client was not found`, a Google-branded
    page that names nothing an operator can act on.

    The names of the unset variables go to the log, where an operator can act
    on them. The browser is simply put back on the login page, which offers the
    email and password sign-in that does work -- it never sees a status code,
    a configuration name, or a reason.
    """
    missing = missing_google_oauth_settings()
    logger.error(
        "Google sign-in is not configured; refusing the request. Unset: %s",
        ", ".join(missing),
    )
    return _back_to_login(OAUTH_ERROR_UNAVAILABLE)


@router.get("/login/google")
async def login_google(request: Request):
    if not google_oauth_enabled():
        return _google_oauth_unavailable()
    # The configured URL, never `request.url_for(...)`: nginx strips the `/api`
    # prefix before this app sees the request, so a derived callback points at
    # a path the browser cannot reach. See backend/config.py.
    return await oauth.google.authorize_redirect(request, google_redirect_uri())


@router.get("/auth/google", name="auth_via_google")
async def auth_via_google(request: Request, db: AsyncSession = Depends(get_db)):
    if not google_oauth_enabled():
        return _google_oauth_unavailable()
    try:
        token = await oauth.google.authorize_access_token(request)
        user_info = token["userinfo"]
        email = user_info["email"]
        full_name = user_info.get("name", "Google User")

        # lookup or create
        stmt = select(User).where(User.email == email)
        result = await db.execute(stmt)
        user = result.scalars().first()

        if not user:
            dummy_password = secrets.token_hex(16)
            user = User(
                email=email,
                full_name=full_name,
                hashed_password=get_password_hash(dummy_password)
            )
            db.add(user)
            await db.commit()
            await db.refresh(user)

        access_token = create_access_token(
            data={"sub": str(user.id)},
            expires_delta=timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
        )
        resp = RedirectResponse(url="/dashboard.htm", status_code=303)
        resp.set_cookie(
            key="access_token",
            value=access_token,
            httponly=True,
            max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
            path="/",
            secure=settings.COOKIE_SECURE,
            domain=settings.COOKIE_DOMAIN,
            samesite=settings.COOKIE_SAMESITE
        )
        return resp

    except OAuthError as e:
        # Authlib's checks are left exactly as Authlib performs them. Only the
        # PRESENTATION of a failure changes here: the callback is a browser
        # navigation, so it must answer with a page the user can act on rather
        # than the raw JSON it used to render.
        code = getattr(e, "error", "") or "oauth_error"
        if isinstance(e, MismatchingStateError) or code == MismatchingStateError.error:
            # The CSRF check did its job: the state Google handed back is not
            # one this browser session started. Ordinary causes are a stale
            # tab, a callback URL replayed from history, a flow begun before
            # the browser dropped the session cookie, or a second sign-in
            # started while the first was still open. Not an attack signal on
            # its own, and not something to weaken -- something to recover
            # from, by clearing what is left and offering a clean retry.
            logger.warning(
                "Google sign-in rejected: the OAuth state did not match this "
                "session (Authlib CSRF check). Returning the user to the login "
                "page to start again."
            )
            _clear_google_oauth_state(request)
            return _back_to_login(OAUTH_ERROR_SESSION_EXPIRED)

        # Everything else: the provider's short error code only. Deliberately
        # NOT `e.description` and NOT a traceback -- those can carry the
        # authorization code, the state, or the token request that failed.
        logger.error("Google sign-in failed: %s", code)
        _clear_google_oauth_state(request)
        return _back_to_login(OAUTH_ERROR_FAILED)


def send_reset_email(email: str, reset_link: str):
    msg = EmailMessage()
    msg.set_content(
        f"Hi,\n\nPlease click the following link to reset your password:\n{reset_link}\n\n"
        "If you did not request this, please ignore this email."
    )
    msg["Subject"] = "Password Reset Request"
    msg["From"] = settings.SMTP_USERNAME
    msg["To"] = email

    with smtplib.SMTP(settings.SMTP_SERVER, settings.SMTP_PORT) as server:
        server.starttls()
        server.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
        server.send_message(msg)
        server.quit()


@router.post("/forgot-password")
async def forgot_password(
    data: dict,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
):
    email = (data.get("email") or "").lower().strip()
    if not email:
        return JSONResponse(status_code=400, content={"success": False, "error": "Email is required"})

    stmt = select(User).where(User.email == email)
    result = await db.execute(stmt)
    user = result.scalars().first()
    if not user:
        return JSONResponse(status_code=400, content={"success": False, "error": "Email Not Registered"})

    reset_token = create_access_token(
        data={"sub": email, "action": "reset"},
        expires_delta=timedelta(minutes=15)
    )
    reset_link = f"{settings.FRONTEND_BASE_URL}/reset-password.htm?token={reset_token}"

    background_tasks.add_task(send_reset_email, email, reset_link)
    return JSONResponse(content={"success": True, "message": "Reset code sent to your email."})


@router.post("/reset-password")
async def reset_password(data: dict, db: AsyncSession = Depends(get_db)):
    token = data.get("token")
    new_password = data.get("new_password")
    confirm_password = data.get("confirm_password")

    if not token or not new_password or not confirm_password:
        return JSONResponse(status_code=400, content={"success": False, "error": "Missing required fields"})
    if new_password != confirm_password:
        return JSONResponse(status_code=400, content={"success": False, "error": "Passwords do not match"})

    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        if payload.get("action") != "reset":
            raise JWTError("Invalid action")
    except JWTError:
        return JSONResponse(status_code=400, content={"success": False, "error": "Invalid or expired token"})

    email = payload.get("sub", "").lower()
    stmt = select(User).where(User.email == email)
    result = await db.execute(stmt)
    user = result.scalars().first()
    if not user:
        return JSONResponse(status_code=400, content={"success": False, "error": "Email Not Registered"})

    user.hashed_password = get_password_hash(new_password)
    await db.commit()
    return JSONResponse(content={"success": True, "message": "Password reset successful"})
