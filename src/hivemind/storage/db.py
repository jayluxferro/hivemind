"""SQLite storage backend for tasks, metrics, and checkpoints."""

from __future__ import annotations

import json
from pathlib import Path

import aiosqlite

from .models import AgentMetrics, Task, TaskPriority, TaskState

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    command TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    priority INTEGER NOT NULL DEFAULT 1,
    token_budget INTEGER,
    tokens_used INTEGER NOT NULL DEFAULT 0,
    dependencies TEXT NOT NULL DEFAULT '[]',
    estimated_tokens INTEGER,
    created_at REAL NOT NULL,
    started_at REAL,
    completed_at REAL,
    agent_id TEXT,
    error TEXT,
    result TEXT,
    checkpoint_path TEXT,
    metadata TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS agent_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    requests_made INTEGER NOT NULL DEFAULT 0,
    requests_failed INTEGER NOT NULL DEFAULT 0,
    retries INTEGER NOT NULL DEFAULT 0,
    avg_latency_ms REAL NOT NULL DEFAULT 0.0,
    recorded_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS request_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT,
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    status_code INTEGER,
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    latency_ms REAL NOT NULL DEFAULT 0.0,
    retried INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    recorded_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state);
CREATE INDEX IF NOT EXISTS idx_tasks_priority ON tasks(priority DESC, created_at ASC);
CREATE INDEX IF NOT EXISTS idx_metrics_agent ON agent_metrics(agent_id);
CREATE INDEX IF NOT EXISTS idx_request_log_agent ON request_log(agent_id);
CREATE INDEX IF NOT EXISTS idx_request_log_time ON request_log(recorded_at);
"""


class Database:
    def __init__(self, db_path: str = "hivemind.db") -> None:
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Database not connected. Call connect() first.")
        return self._db

    # --- Tasks ---

    async def insert_task(self, task: Task) -> None:
        await self.db.execute(
            """INSERT INTO tasks (id, command, state, priority, token_budget, tokens_used,
               dependencies, estimated_tokens, created_at, started_at, completed_at,
               agent_id, error, result, checkpoint_path, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
            """UPDATE tasks SET command=?, state=?, priority=?, token_budget=?, tokens_used=?,
               dependencies=?, estimated_tokens=?, started_at=?, completed_at=?,
               agent_id=?, error=?, result=?, checkpoint_path=?, metadata=?
               WHERE id=?""",
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
        cursor = await self.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,))
        row = await cursor.fetchone()
        if row is None:
            return None
        return self._row_to_task(row)

    async def list_tasks(self, state: TaskState | None = None) -> list[Task]:
        if state:
            cursor = await self.db.execute(
                "SELECT * FROM tasks WHERE state=? ORDER BY priority DESC, created_at ASC",
                (state.value,),
            )
        else:
            cursor = await self.db.execute(
                "SELECT * FROM tasks ORDER BY priority DESC, created_at ASC"
            )
        rows = await cursor.fetchall()
        return [self._row_to_task(row) for row in rows]

    async def count_tasks_by_state(self) -> dict[str, int]:
        cursor = await self.db.execute(
            "SELECT state, COUNT(*) as cnt FROM tasks GROUP BY state"
        )
        rows = await cursor.fetchall()
        return {row["state"]: row["cnt"] for row in rows}

    def _row_to_task(self, row: aiosqlite.Row) -> Task:
        return Task(
            id=row["id"],
            command=row["command"],
            state=TaskState(row["state"]),
            priority=TaskPriority(row["priority"]),
            token_budget=row["token_budget"],
            tokens_used=row["tokens_used"],
            dependencies=json.loads(row["dependencies"]),
            estimated_tokens=row["estimated_tokens"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            agent_id=row["agent_id"],
            error=row["error"],
            result=row["result"],
            checkpoint_path=row["checkpoint_path"],
            metadata=json.loads(row["metadata"]),
        )

    # --- Metrics ---

    async def insert_metrics(self, metrics: AgentMetrics) -> None:
        await self.db.execute(
            """INSERT INTO agent_metrics (agent_id, task_id, tokens_in, tokens_out,
               requests_made, requests_failed, retries, avg_latency_ms, recorded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
            "SELECT * FROM agent_metrics WHERE agent_id=? ORDER BY recorded_at DESC",
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
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
        await self.db.executescript(
            "DELETE FROM tasks; DELETE FROM agent_metrics; DELETE FROM request_log;"
        )
        await self.db.commit()
