"""The Alembic foundation, and the C7 migration that justifies it.

WHAT THESE TESTS CAN AND CANNOT PROVE
-------------------------------------
They run the real revisions against a real database -- but a **SQLite** one,
because that is what this suite has always used and what runs anywhere. SQLite
reaches the migration through alembic's batch mode (copy the table, swap it in);
PostgreSQL, which is production, reaches it through `ALTER TABLE ... TYPE`.

So these tests establish:

    the revision graph is well formed and single-headed
    both revisions import and execute
    after `upgrade head`, the schema matches the models with no drift left over
    existing integer marks survive the upgrade unchanged
    fractional marks can be written afterwards and read back
    the downgrade refuses rather than truncating

They do NOT establish that PostgreSQL's `ALTER COLUMN ... TYPE NUMERIC(7,2)`
behaves as expected on a populated production table. That needs a PostgreSQL
run; see docs/COGNIGRADE_CONTEXT.md for what has and has not been verified.
"""

import pathlib
import sqlite3

import pytest
import sqlalchemy as sa

alembic = pytest.importorskip("alembic", reason="alembic is required to test the migrations")

from alembic import command  # noqa: E402
from alembic.autogenerate import compare_metadata  # noqa: E402
from alembic.runtime.migration import MigrationContext  # noqa: E402
from alembic.script import ScriptDirectory  # noqa: E402

from backend.database import Base  # noqa: E402
from backend.db_bootstrap import (  # noqa: E402
    STATE_ALEMBIC,
    STATE_FRESH,
    STATE_LEGACY,
    alembic_config,
    bootstrap_schema_sync,
    classify_schema_sync,
)
from backend.models.numeric import Marks  # noqa: E402


def _config(db_path):
    """An alembic Config pointed at a throwaway SQLite file."""
    return alembic_config(f"sqlite:///{db_path.as_posix()}")


REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "migration_test.db"


# ---------------------------------------------------------------------------
# the revision graph
# ---------------------------------------------------------------------------

def test_there_is_exactly_one_head():
    """Two heads would mean `upgrade head` is ambiguous and silently partial."""
    script = ScriptDirectory.from_config(alembic_config())
    assert len(script.get_heads()) == 1, script.get_heads()


def test_the_revision_chain_is_linear_and_in_order():
    """Each revision names the one before it, with no branch and no gap.

    The chain is walked from head backwards rather than hard-coded, so adding a
    revision does not require editing this test -- only breaking the chain does.
    """
    script = ScriptDirectory.from_config(alembic_config())

    chain = []
    revision = script.get_current_head()
    while revision is not None:
        chain.append(revision)
        down = script.get_revision(revision).down_revision
        assert not isinstance(down, tuple), f"{revision} is a merge point"
        revision = down
    chain.reverse()

    assert chain == sorted(chain), f"revision ids are not in order: {chain}"
    assert chain[0] == "0001"
    assert script.get_revision(chain[0]).down_revision is None
    assert len(chain) == len(set(chain))


def test_both_revisions_import_cleanly():
    script = ScriptDirectory.from_config(alembic_config())
    for revision in ("0001", "0002"):
        module = script.get_revision(revision).module
        assert callable(module.upgrade)
        assert callable(module.downgrade)


def test_the_baseline_still_describes_the_pre_alembic_schema():
    """`stamp 0001` on an existing database is only honest if this holds.

    Revision 0001 must keep declaring the INTEGER marks columns that
    `create_all` actually produced. If someone "helpfully" updates it to match
    the current models, every existing deployment that stamps it would record a
    schema it does not have, and 0002 would then be skipped or fail.
    """
    script = ScriptDirectory.from_config(alembic_config())
    source = open(script.get_revision("0001").module.__file__, encoding="utf-8").read()
    assert "sa.Column('marks_obtained', sa.Integer(), nullable=True)" in source
    assert "sa.Column('max_marks', sa.Integer(), nullable=False)" in source
    assert "Numeric" not in source


def test_the_migration_and_the_models_agree_on_which_columns_are_marks():
    """Guards drift: a new Marks column added to a model but not to a migration."""
    script = ScriptDirectory.from_config(alembic_config())
    declared = {(t, c) for t, c, _ in script.get_revision("0002").module.MARK_COLUMNS}

    actual = {
        (table.name, column.name)
        for table in Base.metadata.tables.values()
        for column in table.columns
        if isinstance(column.type, Marks)
    }
    assert declared == actual


# ---------------------------------------------------------------------------
# running the migration
# ---------------------------------------------------------------------------

def test_upgrade_head_produces_a_schema_matching_the_models(db_path):
    """After the migrations there must be nothing left for autogenerate to say."""
    config = _config(db_path)
    command.upgrade(config, "head")

    engine = sa.create_engine(f"sqlite:///{db_path.as_posix()}")
    with engine.connect() as connection:
        context = MigrationContext.configure(
            connection, opts={"compare_type": True, "target_metadata": Base.metadata}
        )
        diff = compare_metadata(context, Base.metadata)
    engine.dispose()

    # alembic_version is created by alembic itself and is not in the models.
    diff = [d for d in diff if "alembic_version" not in repr(d)]
    assert diff == [], f"schema drifted from the models: {diff}"


def test_the_marks_columns_are_numeric_after_the_upgrade(db_path):
    command.upgrade(_config(db_path), "head")

    engine = sa.create_engine(f"sqlite:///{db_path.as_posix()}")
    inspector = sa.inspect(engine)
    for table, column, _nullable in (
        ("question_responses", "marks_obtained", True),
        ("exam_results", "marks_obtained", True),
        ("questions", "max_marks", False),
        ("submissions", "grade", True),
    ):
        found = {c["name"]: c for c in inspector.get_columns(table)}
        assert "NUMERIC" in str(found[column]["type"]).upper(), (table, column, found[column]["type"])
    engine.dispose()


def _seed_integer_marks(db_path):
    connection = sqlite3.connect(db_path)
    connection.execute("insert into users (id,email,hashed_password,full_name) values (1,'a@b.c','x','A')")
    connection.execute("insert into classrooms (id,name,subject,owner_id) values (1,'C','S',1)")
    connection.execute("insert into exams (id,title,classroom_id,author_id,points_possible) values (1,'E',1,1,100)")
    connection.execute("insert into questions (id,exam_id,question_number,text,max_marks) values (1,1,1,'Q',3)")
    connection.executemany(
        "insert into question_responses (id,question_id,student_id,marks_obtained) values (?,?,?,?)",
        [(1, 1, 1, 3), (2, 1, 1, 5), (3, 1, 1, 0), (4, 1, 1, None)],
    )
    connection.commit()
    connection.close()


def test_existing_integer_marks_survive_the_upgrade(db_path):
    """The adoption path: data written under 0001 must be intact at head.

    Also the shape of the real "existing deployment" procedure -- upgrade to
    the baseline, put data in it, then upgrade the rest of the way.
    """
    config = _config(db_path)
    command.upgrade(config, "0001")
    _seed_integer_marks(str(db_path))

    command.upgrade(config, "head")

    connection = sqlite3.connect(str(db_path))
    rows = dict(connection.execute("select id, marks_obtained from question_responses"))
    connection.close()
    assert rows[1] == 3      # 3 -> 3.00
    assert rows[2] == 5      # 5 -> 5.00
    assert rows[3] == 0      # a real zero stays a real zero
    assert rows[4] is None   # a missing mark stays missing (C6)


def test_fractional_marks_can_be_written_after_the_upgrade(db_path):
    config = _config(db_path)
    command.upgrade(config, "0001")
    _seed_integer_marks(str(db_path))
    command.upgrade(config, "head")

    connection = sqlite3.connect(str(db_path))
    connection.executemany(
        "insert into question_responses (id,question_id,student_id,marks_obtained) values (?,?,?,?)",
        [(10, 1, 1, 0.5), (11, 1, 1, 1.5), (12, 1, 1, 2.25)],
    )
    connection.commit()
    rows = dict(connection.execute("select id, marks_obtained from question_responses where id >= 10"))
    total = connection.execute(
        "select sum(marks_obtained) from question_responses where id in (11,12,3)"
    ).fetchone()[0]
    connection.close()

    assert rows[10] == 0.5
    assert rows[11] == 1.5
    assert rows[12] == 2.25
    assert total == 3.75


# ---------------------------------------------------------------------------
# what PostgreSQL would actually receive
# ---------------------------------------------------------------------------

def _offline_sql(revision_range, url="postgresql://user:pw@host/db"):
    """The DDL alembic emits for a dialect, without connecting to it.

    Offline mode is the only way this suite can inspect the PostgreSQL
    statements. It verifies the SQL that WOULD be sent; it does not verify that
    a populated production table accepts it.
    """
    import io as _io

    from alembic.config import Config

    buffer = _io.StringIO()
    config = Config(
        str(alembic_config().config_file_name), stdout=buffer, output_buffer=buffer
    )
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, revision_range, sql=True)
    return buffer.getvalue()


def test_postgresql_receives_an_alter_to_numeric_for_every_marks_column():
    # Scoped to the one revision under test: later revisions legitimately
    # create tables, which the "no create table" assertion below would flag.
    sql = _offline_sql("0001:0002").lower()
    for table, column, _nullable in (
        ("assignments", "points_possible", True),
        ("exam_results", "marks_obtained", True),
        ("exams", "points_possible", True),
        ("question_responses", "marks_obtained", True),
        ("questions", "max_marks", False),
        ("submissions", "grade", True),
    ):
        expected = f"alter table {table} alter column {column} type numeric(7, 2)"
        assert expected in sql, f"missing: {expected}"

    # No table is dropped or recreated: the data stays where it is.
    assert "drop table" not in sql
    assert "create table" not in sql


# ---------------------------------------------------------------------------
# downgrade honesty
# ---------------------------------------------------------------------------

def test_downgrade_refuses_when_it_would_destroy_a_fractional_mark(db_path):
    config = _config(db_path)
    command.upgrade(config, "head")
    _seed_integer_marks(str(db_path))

    connection = sqlite3.connect(str(db_path))
    connection.execute(
        "insert into question_responses (id,question_id,student_id,marks_obtained) values (20,1,1,1.5)"
    )
    connection.commit()
    connection.close()

    with pytest.raises(RuntimeError) as exc:
        command.downgrade(config, "0001")
    assert "question_responses.marks_obtained" in str(exc.value)

    # ... and it really did refuse, rather than failing after doing half the work.
    engine = sa.create_engine(f"sqlite:///{db_path.as_posix()}")
    columns = {c["name"]: c for c in sa.inspect(engine).get_columns("question_responses")}
    engine.dispose()
    assert "NUMERIC" in str(columns["marks_obtained"]["type"]).upper()


def test_downgrade_is_allowed_when_every_mark_is_whole(db_path):
    config = _config(db_path)
    command.upgrade(config, "head")
    _seed_integer_marks(str(db_path))

    command.downgrade(config, "0001")

    engine = sa.create_engine(f"sqlite:///{db_path.as_posix()}")
    columns = {c["name"]: c for c in sa.inspect(engine).get_columns("question_responses")}
    engine.dispose()
    assert "INTEGER" in str(columns["marks_obtained"]["type"]).upper()

    connection = sqlite3.connect(str(db_path))
    rows = dict(connection.execute("select id, marks_obtained from question_responses"))
    connection.close()
    assert rows[1] == 3 and rows[3] == 0 and rows[4] is None


# ---------------------------------------------------------------------------
# startup: which authority owns the schema
# ---------------------------------------------------------------------------

def test_an_empty_database_is_created_and_stamped(db_path):
    engine = sa.create_engine(f"sqlite:///{db_path.as_posix()}")
    with engine.begin() as connection:
        assert classify_schema_sync(connection) == STATE_FRESH
        assert bootstrap_schema_sync(connection) == STATE_FRESH

    with engine.connect() as connection:
        version = connection.execute(sa.text("select version_num from alembic_version")).scalar()
        assert version == ScriptDirectory.from_config(alembic_config()).get_current_head()
        # ... and the schema is really there, not merely stamped.
        assert "question_responses" in sa.inspect(connection).get_table_names()
    engine.dispose()


def test_bootstrap_registers_the_models_by_itself():
    """Importing db_bootstrap must be enough to populate Base.metadata.

    Declaring a model registers its table as a side effect of import, and
    nothing else does. This suite cannot see the failure in-process -- conftest
    has already imported the models -- so it asks a clean interpreter, which is
    the only way to reproduce it.

    Found by the PostgreSQL runtime verification: with an empty
    `Base.metadata`, `create_all` created nothing and the fresh-database branch
    still stamped head, leaving a database with no schema that Alembic believed
    was fully migrated.
    """
    import subprocess
    import sys

    script = (
        "import os, sys; sys.path.insert(0, os.getcwd());"
        "os.environ.setdefault('SECRET_KEY', 'x');"
        "os.environ['DATABASE_URL'] = 'sqlite+aiosqlite:///:memory:';"
        "import backend.db_bootstrap;"
        "from backend.database import Base;"
        "print(len(Base.metadata.tables))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, cwd=str(REPO_ROOT),
    )
    assert completed.returncode == 0, completed.stderr
    registered = int(completed.stdout.strip().splitlines()[-1])
    assert registered >= 10, f"only {registered} tables registered by importing db_bootstrap"


def test_bootstrap_refuses_to_stamp_when_no_models_are_registered(db_path, caplog):
    """A stamp claims "this database is at head". Never claim it over nothing."""
    from unittest.mock import patch

    import backend.db_bootstrap as bootstrap

    engine = sa.create_engine(f"sqlite:///{db_path.as_posix()}")
    with patch.object(bootstrap.Base, "metadata", sa.MetaData()):
        with engine.begin() as connection:
            with caplog.at_level("ERROR"):
                assert bootstrap.bootstrap_schema_sync(connection) == STATE_FRESH

    with engine.connect() as connection:
        tables = sa.inspect(connection).get_table_names()
    engine.dispose()

    assert tables == [], f"nothing should have been created, found {tables}"
    assert "alembic_version" not in tables, "an empty database must not be stamped"
    assert "Refusing to initialise an empty database" in caplog.text


def test_a_stamped_database_is_left_to_alembic(db_path):
    config = _config(db_path)
    command.upgrade(config, "head")

    engine = sa.create_engine(f"sqlite:///{db_path.as_posix()}")
    with engine.begin() as connection:
        assert classify_schema_sync(connection) == STATE_ALEMBIC
        assert bootstrap_schema_sync(connection) == STATE_ALEMBIC
    engine.dispose()


def test_a_pre_alembic_database_is_reported_not_silently_patched(db_path, caplog):
    """The legacy case must be loud: it is truncating marks right now."""
    engine = sa.create_engine(f"sqlite:///{db_path.as_posix()}")
    with engine.begin() as connection:
        connection.execute(sa.text("create table users (id integer primary key)"))

    with engine.begin() as connection:
        assert classify_schema_sync(connection) == STATE_LEGACY
        with caplog.at_level("WARNING"):
            assert bootstrap_schema_sync(connection) == STATE_LEGACY

    # No table was invented behind the operator's back.
    with engine.connect() as connection:
        assert sa.inspect(connection).get_table_names() == ["users"]
    engine.dispose()

    message = caplog.text
    assert "stamp 0001" in message and "upgrade head" in message
