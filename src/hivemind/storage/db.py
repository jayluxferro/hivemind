"""Postgres storage backend for tasks, metrics, and request log.

One async psycopg3 connection per :class:`Database` instance (same
single-connection model as the old sqlite backend).  Autocommit is on
to keep the sqlite contract — every execute persists immediately, and
the ``await self.db.commit()`` calls left in the write paths are safe
no-ops under it.  The schema is versioned via the ``schema_migrations``
table: every open applies the unapplied migrations in order, all
idempotent, so re-opening an existing database is a no-op pass.

Failure mode: ``connect()`` fails loud when Postgres is down — there is
no sqlite fallback.  A scheduler that silently loses its task history
is worse than one that refuses to start.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from typing import Any

import psycopg
from psycopg.rows import dict_row

from .models import AgentMetrics, Task, TaskPriority, TaskState

_log = logging.getLogger(__name__)

SCHEMA_VERSION = 1


class Database:
    """Async Postgres store for tasks, agent metrics, and the request log."""

    def __init__(self, db_url: str = "postgresql://hivemind@localhost:5432/hivemind") -> None:
        self.db_url = db_url
        self._db: psycopg.AsyncConnection | None = None

    async def connect(self) -> None:
        self._db = await psycopg.AsyncConnection.connect(self.db_url, row_factory=dict_row)
        # The sqlite backend ran with isolation_level=None (every statement
        # committed immediately); psycopg defaults to implicit transactions,
        # so flip autocommit on to keep that contract.  Explicit
        # BEGIN/COMMIT statements still work under autocommit and group
        # statements into real transactions (used by _apply_migrations).
        await self._db.set_autocommit(True)
        await self._apply_migrations()

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> psycopg.AsyncConnection:
        if self._db is None:
            raise RuntimeError("Database not connected. Call connect() first.")
        return self._db

    # --- Migrations ---

    async def _apply_migrations(self) -> None:
        cur = self.db.cursor()
        await cur.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at BIGINT NOT NULL
            )
            """
        )
        await cur.execute("SELECT version FROM schema_migrations")
        applied = {row["version"] for row in await cur.fetchall()}
        for version, migration in sorted(_MIGRATIONS.items()):
            if version in applied:
                continue
            _log.info("applying hivemind schema migration v%d", version)
            await cur.execute("BEGIN")
            try:
                await migration(self.db)
                await cur.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (%s, %s)",
                    (version, int(time.time() * 1000)),
                )
                await cur.execute("COMMIT")
            except Exception:
                await cur.execute("ROLLBACK")
                raise
        await cur.close()

    async def applied_versions(self) -> list[int]:
        """Applied migration versions, ascending — for tests and the importer."""
        cursor = await self.db.execute("SELECT version FROM schema_migrations ORDER BY version ASC")
        rows = await cursor.fetchall()
        return [row["version"] for row in rows]

    # --- Tasks ---

    async def insert_task(self, task: Task) -> None:
        await self.db.execute(
            """INSERT INTO tasks (id, command, state, priority, token_budget, tokens_used,
               dependencies, estimated_tokens, created_at, started_at, completed_at,
               agent_id, error, result, checkpoint_path, metadata)
               VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)""",
            (
                task.id,
                task.command,
                task.state.value,
                task.priority.value,
                task.token_budget,
                task.tokens_used,
                json.dumps(task.dependencies),
                task.estimated_tokens,
                task.created_at,
                task.started_at,
                task.completed_at,
                task.agent_id,
                task.error,
                task.result,
                task.checkpoint_path,
                json.dumps(task.metadata),
            ),
        )
        await self.db.commit()

    async def update_task(self, task: Task) -> None:
        await self.db.execute(
            """UPDATE tasks SET command=%s, state=%s, priority=%s, token_budget=%s, tokens_used=%s,
               dependencies=%s::jsonb, estimated_tokens=%s, started_at=%s, completed_at=%s,
               agent_id=%s, error=%s, result=%s, checkpoint_path=%s, metadata=%s::jsonb
               WHERE id=%s""",
            (
                task.command,
                task.state.value,
                task.priority.value,
                task.token_budget,
                task.tokens_used,
                json.dumps(task.dependencies),
                task.estimated_tokens,
                task.started_at,
                task.completed_at,
                task.agent_id,
                task.error,
                task.result,
                task.checkpoint_path,
                json.dumps(task.metadata),
                task.id,
            ),
        )
        await self.db.commit()

    async def get_task(self, task_id: str) -> Task | None:
        cursor = await self.db.execute("SELECT * FROM tasks WHERE id=%s", (task_id,))
        row = await cursor.fetchone()
        if row is None:
            return None
        return self._row_to_task(row)

    async def list_tasks(self, state: TaskState | None = None) -> list[Task]:
        if state:
            cursor = await self.db.execute(
                "SELECT * FROM tasks WHERE state=%s ORDER BY priority DESC, created_at ASC",
                (state.value,),
            )
        else:
            cursor = await self.db.execute("SELECT * FROM tasks ORDER BY priority DESC, created_at ASC")
        rows = await cursor.fetchall()
        return [self._row_to_task(row) for row in rows]

    async def count_tasks_by_state(self) -> dict[str, int]:
        cursor = await self.db.execute("SELECT state, COUNT(*) as cnt FROM tasks GROUP BY state")
        rows = await cursor.fetchall()
        return {row["state"]: row["cnt"] for row in rows}

    def _row_to_task(self, row: dict[str, Any]) -> Task:
        return Task(
            id=row["id"],
            command=row["command"],
            state=TaskState(row["state"]),
            priority=TaskPriority(row["priority"]),
            token_budget=row["token_budget"],
            tokens_used=row["tokens_used"],
            dependencies=_maybe_json(row["dependencies"], []),
            estimated_tokens=row["estimated_tokens"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            agent_id=row["agent_id"],
            error=row["error"],
            result=row["result"],
            checkpoint_path=row["checkpoint_path"],
            metadata=_maybe_json(row["metadata"], {}),
        )

    # --- Metrics ---

    async def insert_metrics(self, metrics: AgentMetrics) -> None:
        await self.db.execute(
            """INSERT INTO agent_metrics (agent_id, task_id, tokens_in, tokens_out,
               requests_made, requests_failed, retries, avg_latency_ms, recorded_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                metrics.agent_id,
                metrics.task_id,
                metrics.tokens_in,
                metrics.tokens_out,
                metrics.requests_made,
                metrics.requests_failed,
                metrics.retries,
                metrics.avg_latency_ms,
                metrics.recorded_at,
            ),
        )
        await self.db.commit()

    async def get_agent_metrics(self, agent_id: str) -> list[AgentMetrics]:
        cursor = await self.db.execute(
            "SELECT * FROM agent_metrics WHERE agent_id=%s ORDER BY recorded_at DESC",
            (agent_id,),
        )
        rows = await cursor.fetchall()
        return [
            AgentMetrics(
                agent_id=row["agent_id"],
                task_id=row["task_id"],
                tokens_in=row["tokens_in"],
                tokens_out=row["tokens_out"],
                requests_made=row["requests_made"],
                requests_failed=row["requests_failed"],
                retries=row["retries"],
                avg_latency_ms=row["avg_latency_ms"],
                recorded_at=row["recorded_at"],
            )
            for row in rows
        ]

    # --- Request Log ---

    async def log_request(
        self,
        *,
        agent_id: str | None,
        method: str,
        path: str,
        status_code: int | None,
        tokens_in: int,
        tokens_out: int,
        latency_ms: float,
        retried: bool,
        error: str | None,
        recorded_at: float,
    ) -> None:
        await self.db.execute(
            """INSERT INTO request_log (agent_id, method, path, status_code, tokens_in,
               tokens_out, latency_ms, retried, error, recorded_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                agent_id,
                method,
                path,
                status_code,
                tokens_in,
                tokens_out,
                latency_ms,
                1 if retried else 0,
                error,
                recorded_at,
            ),
        )
        await self.db.commit()

    async def get_aggregate_stats(self) -> dict:
        cursor = await self.db.execute(
            """SELECT
                COUNT(*) as total_requests,
                SUM(tokens_in) as total_tokens_in,
                SUM(tokens_out) as total_tokens_out,
                AVG(latency_ms) as avg_latency_ms,
                SUM(retried) as total_retries,
                SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) as total_errors
               FROM request_log"""
        )
        row = await cursor.fetchone()
        return {
            "total_requests": row["total_requests"] or 0,
            "total_tokens_in": row["total_tokens_in"] or 0,
            "total_tokens_out": row["total_tokens_out"] or 0,
            "avg_latency_ms": round(row["avg_latency_ms"] or 0.0, 2),
            "total_retries": row["total_retries"] or 0,
            "total_errors": row["total_errors"] or 0,
        }

    async def reset(self) -> None:
        """Clear all data — for testing."""
        await self.db.execute("TRUNCATE tasks, agent_metrics, request_log")
        await self.db.commit()


def _maybe_json(value: Any, default: Any) -> Any:
    """Coerce a jsonb column value to a Python object, tolerating strings.

    psycopg loads jsonb into dict/list by default, but rows imported
    from sqlite (or written by older code) may still carry pre-serialized
    JSON strings — handle both.  NULL / empty → ``default``.
    """
    if value is None or value == "":
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


# ---------------------------------------------------------------------------
# Schema migrations
# ---------------------------------------------------------------------------
#
# Ported from the sqlite-era DDL: same column names and order, PG types
# where PG prefers them (TEXT JSON → JSONB for task dependencies/metadata,
# REAL → DOUBLE PRECISION for float timestamps — doubles round-trip
# ``time.time()`` floats bit-exact; AUTOINCREMENT → BIGINT GENERATED BY
# DEFAULT AS IDENTITY so the importer can copy exact ids).  All DDL is
# idempotent (IF NOT EXISTS) so re-opening an existing database is a
# no-op pass over already-applied versions.


async def _migration_v1(conn: psycopg.AsyncConnection) -> None:
    """Initial schema — tasks, agent_metrics, request_log.

    No FOREIGN KEYs — the sqlite schema had none, so behavior is
    unchanged.  Statements cannot take parameters (DDL), so literals
    are inlined.
    """
    cur = conn.cursor()

    await cur.execute(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id               TEXT PRIMARY KEY,
            command          TEXT NOT NULL,
            state            TEXT NOT NULL DEFAULT 'pending',
            priority         INTEGER NOT NULL DEFAULT 1,
            token_budget     INTEGER,
            tokens_used      INTEGER NOT NULL DEFAULT 0,
            dependencies     JSONB NOT NULL DEFAULT '[]',
            estimated_tokens INTEGER,
            created_at       DOUBLE PRECISION NOT NULL,
            started_at       DOUBLE PRECISION,
            completed_at     DOUBLE PRECISION,
            agent_id         TEXT,
            error            TEXT,
            result           TEXT,
            checkpoint_path  TEXT,
            metadata         JSONB NOT NULL DEFAULT '{}'
        )
        """
    )
    await cur.execute("CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state)")
    await cur.execute("CREATE INDEX IF NOT EXISTS idx_tasks_priority ON tasks(priority DESC, created_at ASC)")

    await cur.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_metrics (
            id               BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            agent_id         TEXT NOT NULL,
            task_id          TEXT NOT NULL,
            tokens_in        INTEGER NOT NULL DEFAULT 0,
            tokens_out       INTEGER NOT NULL DEFAULT 0,
            requests_made    INTEGER NOT NULL DEFAULT 0,
            requests_failed  INTEGER NOT NULL DEFAULT 0,
            retries          INTEGER NOT NULL DEFAULT 0,
            avg_latency_ms   DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            recorded_at      DOUBLE PRECISION NOT NULL
        )
        """
    )
    await cur.execute("CREATE INDEX IF NOT EXISTS idx_metrics_agent ON agent_metrics(agent_id)")

    await cur.execute(
        """
        CREATE TABLE IF NOT EXISTS request_log (
            id               BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            agent_id         TEXT,
            method           TEXT NOT NULL,
            path             TEXT NOT NULL,
            status_code      INTEGER,
            tokens_in        INTEGER NOT NULL DEFAULT 0,
            tokens_out       INTEGER NOT NULL DEFAULT 0,
            latency_ms       DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            retried          INTEGER NOT NULL DEFAULT 0,
            error            TEXT,
            recorded_at      DOUBLE PRECISION NOT NULL
        )
        """
    )
    await cur.execute("CREATE INDEX IF NOT EXISTS idx_request_log_agent ON request_log(agent_id)")
    await cur.execute("CREATE INDEX IF NOT EXISTS idx_request_log_time ON request_log(recorded_at)")

    await cur.close()


_MIGRATIONS: dict[int, Callable] = {1: _migration_v1}
