# backend/database.py
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base
from .config import settings


@event.listens_for(Engine, "connect")
def _enforce_sqlite_foreign_keys(dbapi_connection, connection_record):
    """Make SQLite obey the ON DELETE CASCADE the models already declare.

    Every foreign key pointing at `users.id` -- and most others in this schema
    -- is declared `ON DELETE CASCADE`. PostgreSQL, which production runs,
    enforces that. SQLite ignores foreign key constraints entirely unless
    `PRAGMA foreign_keys` is switched on per connection, so on a developer or
    test database those cascades were dead letters: deleting a parent left its
    children orphaned and nothing said so.

    Registered against the `Engine` class rather than one engine instance, so
    the application engine, the Alembic engine and the test engine all behave
    the same way. Non-SQLite drivers are left untouched.
    """
    import sqlite3

    # Two shapes reach this hook: a plain `sqlite3.Connection` from the
    # pysqlite driver, and SQLAlchemy's `AsyncAdapt_aiosqlite_connection`,
    # which is NOT a sqlite3.Connection but does expose the same cursor API.
    # Identify by driver module so both are covered and nothing else is.
    is_sqlite = isinstance(dbapi_connection, sqlite3.Connection) or (
        "sqlite" in type(dbapi_connection).__module__.lower()
    )
    if not is_sqlite:
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()

# note the asyncpgù in the URL
engine = create_async_engine(settings.DATABASE_URL, echo=True)

# use AsyncSession for async ORM
AsyncSessionLocal = sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)
Base = declarative_base()

# async dependency
async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
