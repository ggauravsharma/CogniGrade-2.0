"""Test fixtures for the authorization layer.

Deliberately minimal: an in-memory SQLite database, the real SQLAlchemy models,
the real FastAPI app routers, and a login shim. No Postgres, no RabbitMQ, no
Gemini, no network.

WHY SQLITE
----------
The production database is Postgres, but every model used by the authorization
layer is plain SQL (integers, text, enums, foreign keys) and the policies
contain no Postgres-specific SQL. SQLite therefore exercises the real query
paths while keeping the suite runnable anywhere. Anything that later depends on
Postgres semantics must be marked as an integration test instead.

AUTHENTICATION SHIM
-------------------
`get_current_user_required` reads a JWT from a cookie. Rather than mint real
tokens, the app dependency is overridden with one that reads a plain header,
`X-Test-User`. This keeps the tests focused on AUTHORIZATION -- the thing being
built -- instead of re-testing token signing. Anonymous requests simply omit
the header, and the override raises the same 401 the real dependency does.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException, Request, status
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.future import select

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# The app imports settings at module load; make sure a key exists so importing
# backend.config does not fall back to a random one mid-suite.
os.environ.setdefault("SECRET_KEY", "test-secret-not-a-real-key")
os.environ.setdefault("GEMINI_API_KEY_1", "test-key-not-real")

# backend.database builds its engine at IMPORT time from settings.DATABASE_URL.
# Point it at SQLite before that import so the suite needs neither a running
# Postgres nor the asyncpg driver. Production configuration is untouched.
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"


def _stub_generativeai_if_absent() -> bool:
    """Install a no-op `google.generativeai` when the real SDK is not present.

    `examStats.py` imports `geminiAPI` at module scope, which imports the Gemini
    SDK and builds model objects at import time. This suite tests AUTHORIZATION;
    it must not require a heavyweight AI SDK (or a real API key) to run, and it
    never exercises a code path that calls the model.

    The stub is installed ONLY if the real package is missing, so a developer
    with the SDK installed tests against the real import. No test asserts
    anything about model behaviour.
    """
    try:
        import google.generativeai  # noqa: F401
        return False
    except Exception:
        pass

    import types as _types

    genai = _types.ModuleType("google.generativeai")

    class _StubModel:
        def __init__(self, *a, **kw):
            pass

        def generate_content(self, *a, **kw):
            raise AssertionError(
                "The authorization test suite must never reach the Gemini model."
            )

    def _configure(*a, **kw):
        return None

    def _upload_file(*a, **kw):
        raise AssertionError(
            "The authorization test suite must never upload to Gemini."
        )

    genai.configure = _configure
    genai.GenerativeModel = _StubModel
    genai.upload_file = _upload_file

    try:
        import google as _google
    except Exception:
        _google = _types.ModuleType("google")
        _google.__path__ = []
        sys.modules["google"] = _google

    sys.modules["google.generativeai"] = genai
    setattr(_google, "generativeai", genai)
    return True


GEMINI_SDK_STUBBED = _stub_generativeai_if_absent()

from backend.database import Base, get_db  # noqa: E402
from backend.models.files import AnswerScript, FileTypeEnum, Material  # noqa: E402
from backend.models.tables import (  # noqa: E402
    Classroom,
    Enrollment,
    EnrollmentStatus,
    Exam,
    Question,
    QuestionResponse,
    Role,
)
from backend.models.users import User  # noqa: E402
from backend.utils.security import get_current_user_required  # noqa: E402

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine(TEST_DB_URL, future=True)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(engine):
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture
async def db(session_factory) -> AsyncSession:
    async with session_factory() as s:
        yield s


@pytest_asyncio.fixture
async def world(db: AsyncSession, tmp_path: Path):
    """A realistic two-classroom world.

    owner_prof  owns classroom A and authored exam A
    other_prof  owns an unrelated classroom B  (the "unrelated professor")
    ta_user     accepted TA enrolment in classroom A
    student_a   accepted student enrolment in classroom A, has answers
    student_b   accepted student enrolment in classroom A, has answers
    outsider    a student enrolled in nothing
    """
    owner_prof = User(email="owner@x.test", hashed_password="x", full_name="Owner Prof", is_professor=True)
    other_prof = User(email="other@x.test", hashed_password="x", full_name="Other Prof", is_professor=True)
    ta_user = User(email="ta@x.test", hashed_password="x", full_name="TA", is_professor=False)
    student_a = User(email="a@x.test", hashed_password="x", full_name="Student A", is_professor=False)
    student_b = User(email="b@x.test", hashed_password="x", full_name="Student B", is_professor=False)
    outsider = User(email="out@x.test", hashed_password="x", full_name="Outsider", is_professor=False)
    db.add_all([owner_prof, other_prof, ta_user, student_a, student_b, outsider])
    await db.commit()
    for u in (owner_prof, other_prof, ta_user, student_a, student_b, outsider):
        await db.refresh(u)

    class_a = Classroom(name="Class A", subject="CS", owner_id=owner_prof.id, class_code="AAAAAA")
    class_b = Classroom(name="Class B", subject="CS", owner_id=other_prof.id, class_code="BBBBBB")
    db.add_all([class_a, class_b])
    await db.commit()
    await db.refresh(class_a)
    await db.refresh(class_b)

    db.add_all([
        Enrollment(student_id=student_a.id, classroom_id=class_a.id,
                   status=EnrollmentStatus.ACCEPTED, role=Role.STUDENT),
        Enrollment(student_id=student_b.id, classroom_id=class_a.id,
                   status=EnrollmentStatus.ACCEPTED, role=Role.STUDENT),
        Enrollment(student_id=ta_user.id, classroom_id=class_a.id,
                   status=EnrollmentStatus.ACCEPTED, role=Role.TA),
    ])
    await db.commit()

    exam_a = Exam(title="Exam A", classroom_id=class_a.id, author_id=owner_prof.id, exam_stage=3)
    exam_b = Exam(title="Exam B", classroom_id=class_b.id, author_id=other_prof.id, exam_stage=1)
    db.add_all([exam_a, exam_b])
    await db.commit()
    await db.refresh(exam_a)
    await db.refresh(exam_b)

    q1 = Question(exam_id=exam_a.id, question_number=1, text="Q1", max_marks=10)
    q_other = Question(exam_id=exam_b.id, question_number=1, text="Q1 of other exam", max_marks=10)
    db.add_all([q1, q_other])
    await db.commit()
    await db.refresh(q1)
    await db.refresh(q_other)

    # Real files on disk, inside the upload root the server will serve from.
    upload_root = Path("./uploads").resolve()
    (upload_root / "text_images" / "ans").mkdir(parents=True, exist_ok=True)
    script_a = upload_root / "test_script_a.pdf"
    script_a.write_bytes(b"%PDF-1.4 student A script")
    script_b = upload_root / "test_script_b.pdf"
    script_b.write_bytes(b"%PDF-1.4 student B script")
    ms_file = upload_root / "test_marking_scheme.pdf"
    ms_file.write_bytes(b"%PDF-1.4 marking scheme")
    qp_file = upload_root / "test_question_paper.pdf"
    qp_file.write_bytes(b"%PDF-1.4 question paper")
    crop_a = upload_root / "text_images" / "ans" / "test_crop_a.png"
    crop_a.write_bytes(b"\x89PNG crop a")
    outside_file = Path(tmp_path) / "outside_root.pdf"
    outside_file.write_bytes(b"%PDF-1.4 must never be served")

    db.add_all([
        AnswerScript(title="a.pdf", file_path=str(script_a), exam_id=exam_a.id, student_id=student_a.id),
        AnswerScript(title="b.pdf", file_path=str(script_b), exam_id=exam_a.id, student_id=student_b.id),
        Material(title="ms.pdf", file_path=str(ms_file), related_exam_id=exam_a.id,
                 author_id=owner_prof.id, file_type=FileTypeEnum.marking_scheme),
        Material(title="qp.pdf", file_path=str(qp_file), related_exam_id=exam_a.id,
                 author_id=owner_prof.id, file_type=FileTypeEnum.question_paper),
    ])

    resp_a = QuestionResponse(question_id=q1.id, student_id=student_a.id, marks_obtained=5,
                              ans_text_images=f'["{str(crop_a)!s}"]'.replace("\\", "\\\\"))
    resp_b = QuestionResponse(question_id=q1.id, student_id=student_b.id, marks_obtained=7)
    db.add_all([resp_a, resp_b])
    await db.commit()
    await db.refresh(resp_a)
    await db.refresh(resp_b)

    # A question whose marking-scheme image points OUTSIDE the upload root.
    q1.ms_diagram_images = f'["{str(outside_file)!s}"]'.replace("\\", "\\\\")
    await db.commit()

    return {
        "owner_prof": owner_prof, "other_prof": other_prof, "ta": ta_user,
        "student_a": student_a, "student_b": student_b, "outsider": outsider,
        "class_a": class_a, "class_b": class_b,
        "exam_a": exam_a, "exam_b": exam_b,
        "q1": q1, "q_other": q_other,
        "resp_a": resp_a, "resp_b": resp_b,
        "files": {"script_a": script_a, "script_b": script_b, "ms": ms_file,
                  "qp": qp_file, "crop_a": crop_a, "outside": outside_file},
    }


@pytest_asyncio.fixture
async def client(session_factory, world):
    """An ASGI client over the real routers, with auth shimmed to a header."""
    from backend.auth import files as protected_files
    from backend.routers import (
        announcements,
        classes,
        enrollments,
        examStats,
        exams,
        peopleManagement,
        studentBackend,
        studentEdit,
    )

    app = FastAPI()
    # Include order mirrors backend/main.py, because several paths are declared
    # in more than one router and the FIRST registration wins.
    app.include_router(classes.router)
    app.include_router(enrollments.router)
    app.include_router(announcements.router)
    app.include_router(exams.router)
    app.include_router(peopleManagement.router)
    app.include_router(examStats.router)
    app.include_router(studentBackend.router)
    app.include_router(studentEdit.router)
    app.include_router(protected_files.router)

    async def _override_db():
        async with session_factory() as s:
            yield s

    async def _override_user(request: Request):
        raw = request.headers.get("X-Test-User")
        if not raw:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                detail="Not authenticated")
        async with session_factory() as s:
            result = await s.execute(select(User).where(User.id == int(raw)))
            user = result.scalars().first()
        if not user:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                detail="Not authenticated")
        return user

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_current_user_required] = _override_user

    # raise_app_exceptions=False so that a crash INSIDE a handler comes back as
    # a 500 response instead of propagating into the test. Two endpoints in this
    # repository crash at runtime for reasons unrelated to authorization
    # (see the report: a lazy relationship loaded in async context, and a
    # synchronous db.query on an AsyncSession). Those must not mask the
    # authorization assertions, which are about 401/403 only.
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def as_user(user) -> dict:
    """Headers that authenticate as `user` under the test shim."""
    return {"X-Test-User": str(user.id)}


@pytest_asyncio.fixture
async def enrollment_ids(db: AsyncSession, world):
    """Enrolment ids, including one in the OTHER classroom and one pending.

    Cross-classroom cases need an enrolment that genuinely lives in class B so
    that a class A manager can be shown to be refused.
    """
    result = await db.execute(
        select(Enrollment).where(Enrollment.classroom_id == world["class_a"].id)
    )
    rows = result.scalars().all()
    by_student = {e.student_id: e.id for e in rows}

    in_b = Enrollment(
        student_id=world["student_a"].id,
        classroom_id=world["class_b"].id,
        status=EnrollmentStatus.ACCEPTED,
        role=Role.STUDENT,
    )
    pending = Enrollment(
        student_id=world["outsider"].id,
        classroom_id=world["class_a"].id,
        status=EnrollmentStatus.PENDING,
        role=Role.STUDENT,
    )
    db.add_all([in_b, pending])
    await db.commit()
    await db.refresh(in_b)
    await db.refresh(pending)

    return {
        "student_a": by_student[world["student_a"].id],
        "student_b": by_student[world["student_b"].id],
        "ta": by_student[world["ta"].id],
        "in_class_b": in_b.id,
        "pending_outsider": pending.id,
    }


@pytest_asyncio.fixture
async def announcement_ids(db: AsyncSession, world):
    """One announcement in each classroom, for cross-class mismatch tests."""
    from backend.models.tables import Announcement

    a = Announcement(title="A", content="in class A",
                     classroom_id=world["class_a"].id, author_id=world["owner_prof"].id)
    b = Announcement(title="B", content="in class B",
                     classroom_id=world["class_b"].id, author_id=world["other_prof"].id)
    db.add_all([a, b])
    await db.commit()
    await db.refresh(a)
    await db.refresh(b)
    return {"in_class_a": a.id, "in_class_b": b.id}
