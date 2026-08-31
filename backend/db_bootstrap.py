"""What the application is allowed to do to the schema at startup.

Until now `lifespan` called `Base.metadata.create_all(checkfirst=True)`
unconditionally. That was the only schema mechanism CogniGrade had, and it is
exactly why audit C7 could not be fixed: `create_all` creates missing tables and
nothing else -- it will never alter a column, so the Integer marks columns were
unreachable.

Alembic is now the schema authority. `create_all` is not deleted outright,
because a developer starting a brand-new database should not have their first
run fail on a missing migration step, and because an existing deployment must
not silently lose the only mechanism it has ever had. Instead the two are made
to agree, by deciding which case the database is in before touching it:

    ALEMBIC   `alembic_version` exists.
              Alembic owns this database. Do nothing at all -- running
              create_all here is what would create the dual-authority mess,
              because it would add tables Alembic does not know about.

    FRESH     No tables at all.
              Create the schema from the models, then STAMP head. The stamp is
              what keeps the two mechanisms consistent: the database now
              matches head and says so, and a future `alembic upgrade head`
              will apply only genuinely new revisions instead of trying to
              recreate everything.

    LEGACY    Tables exist but `alembic_version` does not.
              A pre-Alembic database. Its marks columns are still Integer, so
              it is silently truncating partial credit RIGHT NOW. The
              application cannot fix that itself, so it warns loudly with the
              exact adoption commands and leaves the schema alone.

The stamp path degrades safely: if alembic is not importable the database is
still created and the application still starts, with a warning. Startup must
never hard-require migration tooling that a given environment may not have.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from sqlalchemy import inspect

from backend.database import Base

logger = logging.getLogger(__name__)

#: Ships next to this module and inside the backend image.
ALEMBIC_INI = Path(__file__).resolve().parent / "alembic.ini"

ALEMBIC_VERSION_TABLE = "alembic_version"

#: Return values of `bootstrap_schema`, so a caller (or a test) can assert on
#: which branch ran without parsing log output.
STATE_ALEMBIC = "alembic"
STATE_FRESH = "fresh"
STATE_LEGACY = "legacy"

ADOPTION_INSTRUCTIONS = (
    "This database predates Alembic and its marks columns are still INTEGER, "
    "so fractional marks are being truncated. Back it up, then adopt the "
    "migration history: "
    "`alembic -c backend/alembic.ini stamp 0001` followed by "
    "`alembic -c backend/alembic.ini upgrade head`."
)


def alembic_config(url: Optional[str] = None):
    """An alembic `Config` pointing at this project's migrations.

    `url` overrides the database only for the duration of the call; the ini
    file itself holds no credentials.
    """
    from alembic.config import Config

    config = Config(str(ALEMBIC_INI))
    if url is not None:
        config.set_main_option("sqlalchemy.url", url)
    return config


def classify_schema_sync(connection) -> str:
    """Which of the three cases this database is in. Pure inspection."""
    tables = set(inspect(connection).get_table_names())
    if ALEMBIC_VERSION_TABLE in tables:
        return STATE_ALEMBIC
    if not tables:
        return STATE_FRESH
    return STATE_LEGACY


def _stamp_head_sync(connection) -> None:
    """Record head in `alembic_version` on a schema just built from the models.

    Done through `MigrationContext` rather than `alembic.command.stamp` so it
    runs on the connection already open, inside the same transaction as the
    create_all that justifies it.
    """
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    context = MigrationContext.configure(connection)
    script = ScriptDirectory.from_config(alembic_config())
    context.stamp(script, "head")


def bootstrap_schema_sync(connection) -> str:
    """The startup decision, on a synchronous connection. Returns the state."""
    state = classify_schema_sync(connection)

    if state == STATE_ALEMBIC:
        logger.info(
            "Database is under Alembic control; skipping create_all. "
            "Apply schema changes with `alembic -c backend/alembic.ini upgrade head`."
        )
        return state

    if state == STATE_FRESH:
        Base.metadata.create_all(bind=connection, checkfirst=True)
        try:
            _stamp_head_sync(connection)
            logger.info("Created a fresh schema from the models and stamped it at Alembic head.")
        except Exception:
            logger.warning(
                "Created a fresh schema from the models but could not stamp the "
                "Alembic revision. Run `alembic -c backend/alembic.ini stamp head` "
                "before applying any future migration.",
                exc_info=True,
            )
        return state

    logger.warning("Pre-Alembic database detected. %s", ADOPTION_INSTRUCTIONS)
    # Left untouched deliberately: create_all here would add any table a newer
    # release introduced while leaving the columns this release needs to ALTER
    # still wrong, which is a harder state to reason about than a clean warning.
    return state


async def bootstrap_schema(engine) -> str:
    """Async entry point used by the FastAPI lifespan."""
    async with engine.begin() as conn:
        return await conn.run_sync(bootstrap_schema_sync)
