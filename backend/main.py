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
from backend.routers import auth, classes, enrollments, notifications, announcements, exams, geminiAPI, studentBackend, peopleManagement, examStats, user_routes, studentEdit, routingTasks
from backend.auth import files as protected_files
from backend.config import settings

from fastapi.staticfiles import StaticFiles

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup. Alembic is the schema authority; create_all now runs only on a
    # genuinely empty database, and that database is stamped so the two agree.
    # See backend/db_bootstrap.py for the three cases and why each behaves the
    # way it does.
    await bootstrap_schema(engine)
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
