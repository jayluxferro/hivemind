"""Shared test fixtures (Postgres backend).

The suite runs against a dedicated ``hivemind_test`` database on the
local Postgres instance (override via ``HIVEMIND_TEST_DB_URL`` for CI /
other machines).  Each test gets a clean database: the ``db`` fixture
drops the known hivemind tables *before* opening (migration v1
recreates them on connect) and again after closing.

The schema itself is NEVER dropped — ``DROP SCHEMA public CASCADE``
would destroy every object in the database, not just hivemind's.
Teardown drops exactly the tables the migration can create, nothing else.
"""

from __future__ import annotations

import os

import psycopg
import pytest

from hivemind.storage.db import Database

TEST_DB_URL = os.environ.get(
    "HIVEMIND_TEST_DB_URL",
    "postgresql://hivemind@localhost:5432/hivemind_test",
)

# Every table migration v1 can create.  Drops are scoped to exactly this
# set — keep in sync with _migration_v1 in hivemind.storage.db.
HIVEMIND_TABLES = (
    "tasks",
    "agent_metrics",
    "request_log",
    "schema_migrations",
)


def drop_hivemind_tables(dsn: str = TEST_DB_URL) -> None:
    """Drop the known hivemind tables via a dedicated connection.

    Uses a fresh short-lived *synchronous* psycopg connection because the
    fixture's own async connection may be closed by the time teardown
    runs, and a blocking driver call must never share the event loop's
    connection.  Autocommit keeps each DROP outside any transaction (no
    locking surprises).
    """
    conn = psycopg.connect(dsn)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            for table in HIVEMIND_TABLES:
                cur.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE')
    finally:
        conn.close()


@pytest.fixture
async def db():
    """A fresh hivemind database in ``hivemind_test``.

    Drop → open (migration v1 recreates the schema) → yield → close → drop.
    The post-test drop guarantees the next test starts from an empty
    database even if this one failed mid-test.
    """
    drop_hivemind_tables()
    database = Database(TEST_DB_URL)
    try:
        await database.connect()
        yield database
    finally:
        await database.close()
        drop_hivemind_tables()


@pytest.fixture
def anyio_backend():
    return "asyncio"
