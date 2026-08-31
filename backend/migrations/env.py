"""Alembic environment for CogniGrade.

Wiring, in one place:

* **Metadata** comes from `backend.database.Base`, after importing every model
  module, so `--autogenerate` compares against the real application schema
  rather than whatever happened to be imported first.
* **The URL** comes from `backend.config.settings.DATABASE_URL` -- the same
  value the application uses -- or from `ALEMBIC_DATABASE_URL` when a migration
  should target a different database (a disposable copy, a test container).
  Nothing is copied into `alembic.ini`, so no password is ever committed.
* **Async** is handled properly: production runs `postgresql+asyncpg`, which
  cannot be driven synchronously, so an async engine is created and the
  migration body runs inside `connection.run_sync`. A synchronous URL (a plain
  `sqlite:///` used by a test) is detected and run directly.
"""

from __future__ import annotations

import asyncio
import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# The repository root, so `import backend...` works however alembic was invoked.
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.config import settings  # noqa: E402
from backend.database import Base  # noqa: E402

# Importing the model packages is what populates Base.metadata. Without this,
# autogenerate would cheerfully propose dropping every table.
import backend.models  # noqa: E402,F401
import backend.models.files  # noqa: E402,F401
import backend.models.notifications  # noqa: E402,F401
import backend.models.tables  # noqa: E402,F401
import backend.models.users  # noqa: E402,F401

config = context.config

if config.config_file_name is not None:
    # disable_existing_loggers defaults to True, which would silence every
    # logger the application had already created -- including the ones the
    # startup bootstrap uses to warn about a pre-Alembic database.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def get_url() -> str:
    """The database this migration run should act on.

    Precedence: an explicit `-x url=...`, then a `sqlalchemy.url` set
    programmatically on the Config (how the test suite and
    `db_bootstrap.alembic_config` point a run at a disposable database), then
    `ALEMBIC_DATABASE_URL`, then the application's own configured URL. The last
    is the default so that `alembic upgrade head` on a deployed host needs no
    extra environment -- and `alembic.ini` stays free of credentials, since it
    sets no url of its own.
    """
    x_args = context.get_x_argument(as_dictionary=True)
    return (
        x_args.get("url")
        or config.get_main_option("sqlalchemy.url", None)
        or os.getenv("ALEMBIC_DATABASE_URL")
        or settings.DATABASE_URL
    )


def _is_async_url(url: str) -> bool:
    driver = url.split("://", 1)[0]
    return any(marker in driver for marker in ("asyncpg", "aiosqlite", "aiomysql", "asyncmy"))


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        # SQLite cannot ALTER a column in place; batch mode rewrites the table
        # instead. On PostgreSQL this is a no-op wrapper around a plain ALTER.
        render_as_batch=connection.dialect.name == "sqlite",
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting -- `alembic upgrade head --sql`.

    Useful for handing a DBA the exact statements before they touch a
    production database.
    """
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        {"sqlalchemy.url": get_url()},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    url = get_url()
    if _is_async_url(url):
        asyncio.run(run_async_migrations())
        return

    # A synchronous URL still has to work: `sqlite:///file.db` is the cheapest
    # way to exercise a migration end to end.
    from sqlalchemy import create_engine

    connectable = create_engine(url, poolclass=pool.NullPool)
    with connectable.connect() as connection:
        do_run_migrations(connection)
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
