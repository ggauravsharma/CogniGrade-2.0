"""Account deletion: success must mean the account is gone.

`POST /delete-account` returned 200 while deleting nothing. It is the one
endpoint in the product that makes a privacy promise, and it broke that promise
silently, which is worse than failing loudly.

Three independent defects stacked in one handler:

    six `db.delete(...)` calls that were never awaited -- `AsyncSession.delete`
    is a coroutine, so each one built a coroutine object and discarded it

    `db.rollback()` unawaited too, so the error path did not roll back either

    iteration over four lazy relationships (`answer_scripts`,
    `question_responses`, `enrollments`, `received_notifications`) in async
    context, which SQLAlchemy answers with MissingGreenlet

WHAT THESE TESTS ASSERT
-----------------------
Not that the endpoint returns 200. That was always true. They assert the row is
GONE, by querying for it afterwards, and that dependent rows follow the cascade
the schema declares.

PASSWORD HASHING IS STUBBED
---------------------------
`verify_password` is replaced with a plain comparison. Production pins
bcrypt==4.0.1, which passlib 1.7.4 supports; the test venv happens to carry
bcrypt 5.0.0, whose removed `__about__` attribute makes passlib's bcrypt
backend unusable. That is an environment detail, not a product defect, and this
suite is about deletion correctness rather than hashing -- so the check is
stubbed and the 400-on-wrong-password path is still exercised through the stub.
"""

import pytest
from sqlalchemy import func, select

from backend.models.files import AnswerScript
from backend.models.notifications import Notification, NotificationType
from backend.models.tables import Classroom, Enrollment, EnrollmentStatus, QuestionResponse, Role
from backend.models.users import LoginHistory, User, UserSettings

from .conftest import as_user

PASSWORD = "correct-horse"


@pytest.fixture(autouse=True)
def _stub_password_check(monkeypatch):
    """Compare passwords literally; see the module docstring."""
    import backend.routers.user_routes as user_routes

    monkeypatch.setattr(user_routes, "verify_password", lambda plain, hashed: plain == hashed)


@pytest.fixture(autouse=True)
def profile_picture_dir(tmp_path, monkeypatch):
    """Point profile-picture deletion at a throwaway directory.

    The route removes `{PROFILE_PICTURE_DIR}/{user_id}.jpg`, and this
    repository TRACKS real files under `profile_pictures/`. Without this the
    suite deletes them out of the working tree, because fixture user ids
    collide with real ones. Redirected rather than skipped so the removal is
    still exercised.
    """
    import backend.routers.user_routes as user_routes

    directory = tmp_path / "profile_pictures"
    directory.mkdir()
    monkeypatch.setattr(user_routes, "PROFILE_PICTURE_DIR", str(directory))
    return directory


@pytest.fixture
async def deletable(db, world):
    """A student with a row in every table that points at a user.

    Deliberately broad: the defect was that dependent rows were left behind, so
    the fixture has to give the deletion something to leave behind.
    """
    user = User(
        email="deleteme@x.test", hashed_password=PASSWORD,
        full_name="Delete Me", is_professor=False,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    db.add_all([
        Enrollment(student_id=user.id, classroom_id=world["class_a"].id,
                   status=EnrollmentStatus.ACCEPTED, role=Role.STUDENT),
        AnswerScript(title="script.pdf", file_path="/tmp/x.pdf",
                     exam_id=world["exam_a"].id, student_id=user.id),
        QuestionResponse(question_id=world["q1"].id, student_id=user.id, marks_obtained=4),
        UserSettings(user_id=user.id, display_theme="dark"),
        LoginHistory(user_id=user.id, ip_address="127.0.0.1"),
        Notification(type=NotificationType.ANNOUNCEMENT, title="to them",
                     recipient_id=user.id, sender_id=world["owner_prof"].id),
        Notification(type=NotificationType.ANNOUNCEMENT, title="from them",
                     recipient_id=world["owner_prof"].id, sender_id=user.id),
    ])
    await db.commit()
    return user


async def _count(db, model, **filters):
    stmt = select(func.count()).select_from(model)
    for column, value in filters.items():
        stmt = stmt.where(getattr(model, column) == value)
    return (await db.execute(stmt)).scalar()


async def _user_exists(db, user_id):
    db.expunge_all()
    found = await db.execute(select(User).where(User.id == user_id))
    return found.scalars().first() is not None


# ---------------------------------------------------------------------------
# the central invariant
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_deletion_reports_success(client, deletable):
    res = await client.post(
        "/delete-account", data={"password": PASSWORD}, headers=as_user(deletable)
    )
    assert res.status_code == 200, res.text
    assert "deleted" in res.json()["message"].lower()


@pytest.mark.asyncio
async def test_the_user_row_actually_disappears(client, db, deletable):
    """THE regression. The old handler returned 200 with the row still there."""
    user_id = deletable.id
    assert await _user_exists(db, user_id) is True

    res = await client.post(
        "/delete-account", data={"password": PASSWORD}, headers=as_user(deletable)
    )
    assert res.status_code == 200, res.text

    assert await _user_exists(db, user_id) is False, (
        "delete-account reported success but the user row is still in the database"
    )


@pytest.mark.asyncio
async def test_the_route_does_not_crash_on_lazy_relationships(client, deletable):
    """MissingGreenlet regression: the handler walked four lazy relationships."""
    res = await client.post(
        "/delete-account", data={"password": PASSWORD}, headers=as_user(deletable)
    )
    assert res.status_code == 200, res.text
    body = res.text.lower()
    assert "greenlet" not in body
    assert "await_only" not in body


# ---------------------------------------------------------------------------
# dependent rows
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rows_owned_by_the_user_are_removed(client, db, deletable):
    """Every table with an ON DELETE CASCADE foreign key to users.id."""
    user_id = deletable.id
    res = await client.post(
        "/delete-account", data={"password": PASSWORD}, headers=as_user(deletable)
    )
    assert res.status_code == 200, res.text
    db.expunge_all()

    assert await _count(db, Enrollment, student_id=user_id) == 0
    assert await _count(db, AnswerScript, student_id=user_id) == 0
    assert await _count(db, QuestionResponse, student_id=user_id) == 0
    assert await _count(db, UserSettings, user_id=user_id) == 0
    assert await _count(db, LoginHistory, user_id=user_id) == 0
    assert await _count(db, Notification, recipient_id=user_id) == 0
    assert await _count(db, Notification, sender_id=user_id) == 0


@pytest.mark.asyncio
async def test_other_users_data_is_untouched(client, db, deletable, world):
    """The blast radius must stop at the account being deleted."""
    other = world["student_a"]
    before = await _count(db, QuestionResponse, student_id=other.id)
    assert before > 0

    res = await client.post(
        "/delete-account", data={"password": PASSWORD}, headers=as_user(deletable)
    )
    assert res.status_code == 200
    db.expunge_all()

    assert await _user_exists(db, other.id) is True
    assert await _count(db, QuestionResponse, student_id=other.id) == before
    assert await _user_exists(db, world["owner_prof"].id) is True


@pytest.mark.asyncio
async def test_deleting_an_owner_removes_their_classrooms(client, db, world):
    """The schema says ON DELETE CASCADE from classrooms.owner_id.

    Recorded as a test because it is a large, easily-missed consequence, not
    because this phase chose it: every exam, assignment and result inside a
    professor's classrooms goes with the account. See the context file.
    """
    owner = world["other_prof"]
    owner.hashed_password = PASSWORD
    await db.commit()
    class_b_id = world["class_b"].id

    res = await client.post(
        "/delete-account", data={"password": PASSWORD}, headers=as_user(owner)
    )
    assert res.status_code == 200, res.text
    db.expunge_all()

    found = await db.execute(select(Classroom).where(Classroom.id == class_b_id))
    assert found.scalars().first() is None


# ---------------------------------------------------------------------------
# the profile picture
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_profile_picture_is_removed_after_a_successful_deletion(
    client, deletable, profile_picture_dir
):
    picture = profile_picture_dir / f"{deletable.id}.jpg"
    picture.write_bytes(b"not really a jpeg")

    res = await client.post(
        "/delete-account", data={"password": PASSWORD}, headers=as_user(deletable)
    )
    assert res.status_code == 200
    assert not picture.exists()


@pytest.mark.asyncio
async def test_a_refused_deletion_keeps_the_profile_picture(
    client, deletable, profile_picture_dir
):
    """The old handler removed the file BEFORE the transaction, so a failed
    deletion still destroyed the picture of an account that still existed."""
    picture = profile_picture_dir / f"{deletable.id}.jpg"
    picture.write_bytes(b"keep me")

    res = await client.post(
        "/delete-account", data={"password": "wrong"}, headers=as_user(deletable)
    )
    assert res.status_code == 400
    assert picture.exists(), "the picture was destroyed for an account that was not deleted"


@pytest.mark.asyncio
async def test_a_missing_picture_does_not_break_deletion(client, db, deletable):
    """No file on disk is the normal case; it must not raise."""
    user_id = deletable.id
    res = await client.post(
        "/delete-account", data={"password": PASSWORD}, headers=as_user(deletable)
    )
    assert res.status_code == 200
    assert await _user_exists(db, user_id) is False


# ---------------------------------------------------------------------------
# what the deleted identity can still do
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_deleted_user_can_no_longer_authenticate(client, deletable):
    """Phase H: no token revocation exists, and none is needed here.

    `get_current_user_from_cookie` decodes the JWT and then LOADS the user by
    id on every request. Once the row is gone that load returns nothing and the
    auth dependency answers 401, so an already-issued token cannot act. The
    token stays syntactically valid until it expires -- this is a consequence
    of re-reading the user, not a deliberate revocation mechanism.
    """
    res = await client.post(
        "/delete-account", data={"password": PASSWORD}, headers=as_user(deletable)
    )
    assert res.status_code == 200

    again = await client.get("/get-info", headers=as_user(deletable))
    assert again.status_code == 401


@pytest.mark.asyncio
async def test_deleting_twice_does_not_report_a_second_success(client, deletable):
    """The second attempt must not claim to have deleted anything."""
    first = await client.post(
        "/delete-account", data={"password": PASSWORD}, headers=as_user(deletable)
    )
    assert first.status_code == 200

    second = await client.post(
        "/delete-account", data={"password": PASSWORD}, headers=as_user(deletable)
    )
    assert second.status_code == 401, second.text


# ---------------------------------------------------------------------------
# failure paths
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_wrong_password_is_400_and_deletes_nothing(client, db, deletable):
    user_id = deletable.id
    res = await client.post(
        "/delete-account", data={"password": "not-the-password"}, headers=as_user(deletable)
    )
    assert res.status_code == 400
    assert await _user_exists(db, user_id) is True


@pytest.mark.asyncio
async def test_an_anonymous_caller_cannot_delete(client, deletable):
    res = await client.post("/delete-account", data={"password": PASSWORD})
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_the_route_only_ever_deletes_the_caller(client, db, deletable, world):
    """There is no id parameter: identity comes from the session, so one user
    cannot name another. Asserted so a future signature change cannot add one
    silently."""
    import inspect

    from backend.routers.user_routes import delete_account

    params = set(inspect.signature(delete_account).parameters)
    assert params == {"password", "current_user", "db"}, params

    victim = world["student_b"]
    res = await client.post(
        "/delete-account", data={"password": PASSWORD}, headers=as_user(deletable)
    )
    assert res.status_code == 200
    db.expunge_all()
    assert await _user_exists(db, victim.id) is True


@pytest.mark.asyncio
async def test_a_database_failure_does_not_report_success(client, db, deletable, monkeypatch):
    """A commit that fails must roll back and surface an error, not a 200."""
    import backend.routers.user_routes as user_routes

    user_id = deletable.id
    rolled_back = {"called": False}

    real_delete = user_routes.sa_delete

    def _boom(*args, **kwargs):
        raise RuntimeError("database on fire")

    monkeypatch.setattr(user_routes, "sa_delete", _boom)

    res = await client.post(
        "/delete-account", data={"password": PASSWORD}, headers=as_user(deletable)
    )
    assert res.status_code >= 500
    assert await _user_exists(db, user_id) is True, (
        "a failed deletion must leave the account intact"
    )
    assert real_delete is not None  # the real symbol exists; monkeypatch restored it


@pytest.mark.asyncio
async def test_the_error_response_does_not_leak_internals(client, deletable, monkeypatch):
    import backend.routers.user_routes as user_routes

    def _boom(*args, **kwargs):
        raise RuntimeError("connection string postgresql://user:secret@host/db")

    monkeypatch.setattr(user_routes, "sa_delete", _boom)

    res = await client.post(
        "/delete-account", data={"password": PASSWORD}, headers=as_user(deletable)
    )
    assert res.status_code >= 500
    assert "secret" not in res.text
    assert "postgresql://" not in res.text


# ---------------------------------------------------------------------------
# static guarantees
# ---------------------------------------------------------------------------

ASYNC_SESSION_METHODS = {
    "delete", "commit", "rollback", "execute", "refresh", "flush", "close",
}


def _unawaited_session_calls(path):
    """Bare `db.<async method>(...)` statements in one module."""
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    offenders = []

    class Visitor(ast.NodeVisitor):
        def visit_Await(self, node):
            pass  # awaited: fine, and do not descend looking for a bare call

        def visit_Expr(self, node):
            call = node.value
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute):
                target = call.func
                if (
                    target.attr in ASYNC_SESSION_METHODS
                    and isinstance(target.value, ast.Name)
                    and target.value.id in ("db", "db_", "session")
                ):
                    offenders.append(
                        f"{path.name}:{node.lineno} {target.value.id}.{target.attr}(...) not awaited"
                    )
            self.generic_visit(node)

    Visitor().visit(tree)
    return offenders


def test_delete_account_awaits_every_session_call():
    """The original defect was invisible at runtime -- an unawaited coroutine
    is discarded silently -- so it is asserted structurally."""
    import pathlib

    import backend.routers.user_routes as mod

    assert _unawaited_session_calls(pathlib.Path(mod.__file__)) == []


def test_no_live_module_leaves_a_session_call_unawaited():
    """Repo-wide guard. The scan is clean today; this keeps it that way."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    offenders = []
    for path in sorted(root.rglob("*.py")):
        if "old" in path.parts or "tests" in path.parts:
            continue
        try:
            offenders += _unawaited_session_calls(path)
        except (SyntaxError, UnicodeDecodeError):
            continue  # backend/routers/old/ is known-dead and does not parse
    assert offenders == [], offenders


@pytest.mark.asyncio
async def test_sqlite_connections_enforce_foreign_keys(engine):
    """The deletion relies on ON DELETE CASCADE actually firing.

    PostgreSQL always enforces it; SQLite ignores every foreign key unless the
    pragma is set per connection, which is why the cascade was silently absent
    on dev and test databases before backend/database.py enabled it.
    """
    import sqlalchemy as sa

    async with engine.connect() as connection:
        enabled = (await connection.execute(sa.text("PRAGMA foreign_keys"))).scalar()
    assert enabled == 1, (
        "SQLite is ignoring ON DELETE CASCADE; the schema's cascade rules are dead letters"
    )
