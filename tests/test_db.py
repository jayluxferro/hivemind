"""Tests for the SQLite database layer."""

import time
import pytest

from hivemind.storage.db import Database
from hivemind.storage.models import AgentMetrics, Task, TaskPriority, TaskState


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.connect()
    yield database
    await database.close()


@pytest.mark.asyncio
async def test_insert_and_get_task(db):
    task = Task(
        id="test-1",
        command="echo hello",
        priority=TaskPriority.HIGH,
        token_budget=5000,
    )
    await db.insert_task(task)

    loaded = await db.get_task("test-1")
    assert loaded is not None
    assert loaded.id == "test-1"
    assert loaded.command == "echo hello"
    assert loaded.priority == TaskPriority.HIGH
    assert loaded.token_budget == 5000
    assert loaded.state == TaskState.PENDING


@pytest.mark.asyncio
async def test_update_task(db):
    task = Task(id="test-2", command="echo update")
    await db.insert_task(task)

    task.state = TaskState.RUNNING
    task.started_at = time.time()
    task.tokens_used = 1500
    await db.update_task(task)

    loaded = await db.get_task("test-2")
    assert loaded.state == TaskState.RUNNING
    assert loaded.tokens_used == 1500
    assert loaded.started_at is not None


@pytest.mark.asyncio
async def test_list_tasks(db):
    for i in range(5):
        task = Task(id=f"list-{i}", command=f"task {i}")
        await db.insert_task(task)

    all_tasks = await db.list_tasks()
    assert len(all_tasks) == 5

    pending = await db.list_tasks(state=TaskState.PENDING)
    assert len(pending) == 5


@pytest.mark.asyncio
async def test_count_tasks_by_state(db):
    t1 = Task(id="s1", command="a", state=TaskState.PENDING)
    t2 = Task(id="s2", command="b", state=TaskState.RUNNING)
    t3 = Task(id="s3", command="c", state=TaskState.RUNNING)
    await db.insert_task(t1)
    await db.insert_task(t2)
    await db.insert_task(t3)

    counts = await db.count_tasks_by_state()
    assert counts["pending"] == 1
    assert counts["running"] == 2


@pytest.mark.asyncio
async def test_task_with_dependencies(db):
    task = Task(
        id="dep-1",
        command="echo deps",
        dependencies=["other-1", "other-2"],
    )
    await db.insert_task(task)

    loaded = await db.get_task("dep-1")
    assert loaded.dependencies == ["other-1", "other-2"]


@pytest.mark.asyncio
async def test_task_with_metadata(db):
    task = Task(
        id="meta-1",
        command="echo meta",
        metadata={"source": "test", "tags": ["a", "b"]},
    )
    await db.insert_task(task)

    loaded = await db.get_task("meta-1")
    assert loaded.metadata["source"] == "test"
    assert loaded.metadata["tags"] == ["a", "b"]


@pytest.mark.asyncio
async def test_insert_metrics(db):
    metrics = AgentMetrics(
        agent_id="agent-1",
        task_id="task-1",
        tokens_in=5000,
        tokens_out=2000,
        requests_made=10,
        avg_latency_ms=150.0,
    )
    await db.insert_metrics(metrics)

    loaded = await db.get_agent_metrics("agent-1")
    assert len(loaded) == 1
    assert loaded[0].tokens_in == 5000
    assert loaded[0].tokens_out == 2000


@pytest.mark.asyncio
async def test_log_request(db):
    await db.log_request(
        agent_id="agent-1",
        method="POST",
        path="/v1/messages",
        status_code=200,
        tokens_in=1000,
        tokens_out=500,
        latency_ms=250.0,
        retried=False,
        error=None,
        recorded_at=time.time(),
    )

    stats = await db.get_aggregate_stats()
    assert stats["total_requests"] == 1
    assert stats["total_tokens_in"] == 1000
    assert stats["total_tokens_out"] == 500


@pytest.mark.asyncio
async def test_aggregate_stats(db):
    for i in range(5):
        await db.log_request(
            agent_id="agent-1",
            method="POST",
            path="/v1/messages",
            status_code=200 if i < 4 else 429,
            tokens_in=1000,
            tokens_out=500,
            latency_ms=100.0 + i * 50,
            retried=i == 4,
            error="rate limited" if i == 4 else None,
            recorded_at=time.time(),
        )

    stats = await db.get_aggregate_stats()
    assert stats["total_requests"] == 5
    assert stats["total_tokens_in"] == 5000
    assert stats["total_retries"] == 1
    assert stats["total_errors"] == 1


@pytest.mark.asyncio
async def test_reset(db):
    await db.insert_task(Task(id="reset-1", command="x"))
    await db.reset()
    tasks = await db.list_tasks()
    assert len(tasks) == 0
