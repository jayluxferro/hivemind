"""Tests for the priority queue with dependency DAG."""

import asyncio
import pytest

from hivemind.scheduler.queue import CycleError, PriorityQueue
from hivemind.storage.models import Task, TaskPriority, TaskState


@pytest.mark.asyncio
async def test_submit_and_get():
    q = PriorityQueue()
    task = Task(command="echo hello", priority=TaskPriority.NORMAL)
    await q.submit(task)

    assert q.pending_count == 1
    assert task.state == TaskState.QUEUED

    dequeued = await q.get(timeout=1.0)
    assert dequeued.id == task.id
    assert dequeued.state == TaskState.RUNNING


@pytest.mark.asyncio
async def test_priority_ordering():
    q = PriorityQueue()

    low = Task(command="low", priority=TaskPriority.LOW)
    high = Task(command="high", priority=TaskPriority.HIGH)
    normal = Task(command="normal", priority=TaskPriority.NORMAL)

    await q.submit(low)
    await q.submit(high)
    await q.submit(normal)

    # Should come out highest priority first
    first = await q.get(timeout=1.0)
    assert first.priority == TaskPriority.HIGH

    second = await q.get(timeout=1.0)
    assert second.priority == TaskPriority.NORMAL

    third = await q.get(timeout=1.0)
    assert third.priority == TaskPriority.LOW


@pytest.mark.asyncio
async def test_dependencies():
    q = PriorityQueue()

    task_a = Task(id="a", command="task a")
    task_b = Task(id="b", command="task b", dependencies=["a"])

    await q.submit(task_a)
    await q.submit(task_b)

    # Only task_a should be available (b depends on a)
    assert task_b.state == TaskState.PENDING

    got = await q.get(timeout=1.0)
    assert got.id == "a"

    # b is still pending
    with pytest.raises(asyncio.TimeoutError):
        await q.get(timeout=0.1)

    # Complete a — b should become available
    await q.complete("a", result="done")

    got = await q.get(timeout=1.0)
    assert got.id == "b"


@pytest.mark.asyncio
async def test_complete_task():
    q = PriorityQueue()
    task = Task(command="echo test")
    await q.submit(task)

    dequeued = await q.get(timeout=1.0)
    await q.complete(dequeued.id, result="success")

    t = await q.get_task(dequeued.id)
    assert t.state == TaskState.COMPLETED
    assert t.result == "success"


@pytest.mark.asyncio
async def test_fail_task():
    q = PriorityQueue()
    task = Task(command="bad command")
    await q.submit(task)

    dequeued = await q.get(timeout=1.0)
    await q.complete(dequeued.id, error="command not found")

    t = await q.get_task(dequeued.id)
    assert t.state == TaskState.FAILED
    assert t.error == "command not found"


@pytest.mark.asyncio
async def test_cancel_task():
    q = PriorityQueue()
    task = Task(command="cancel me")
    await q.submit(task)

    cancelled = await q.cancel(task.id)
    assert cancelled is True

    t = await q.get_task(task.id)
    assert t.state == TaskState.FAILED
    assert t.error == "cancelled"


@pytest.mark.asyncio
async def test_update_priority():
    q = PriorityQueue()
    task = Task(command="reprioritize me", priority=TaskPriority.LOW)
    await q.submit(task)

    success = await q.update_priority(task.id, TaskPriority.CRITICAL.value)
    assert success is True

    t = await q.get_task(task.id)
    assert t.priority == TaskPriority.CRITICAL


@pytest.mark.asyncio
async def test_cycle_detection():
    q = PriorityQueue()

    task_a = Task(id="a", command="a")
    task_b = Task(id="b", command="b", dependencies=["a"])
    await q.submit(task_a)
    await q.submit(task_b)

    with pytest.raises(CycleError):
        await q.add_dependency("a", "b")


@pytest.mark.asyncio
async def test_list_tasks():
    q = PriorityQueue()
    for i in range(5):
        await q.submit(Task(command=f"task {i}"))

    all_tasks = await q.list_tasks()
    assert len(all_tasks) == 5

    queued = await q.list_tasks(TaskState.QUEUED)
    assert len(queued) == 5


@pytest.mark.asyncio
async def test_stats():
    q = PriorityQueue()
    await q.submit(Task(command="t1"))
    await q.submit(Task(command="t2"))

    stats = q.stats
    assert stats["total"] == 2
    assert stats["queued"] == 2
    assert stats["by_state"]["queued"] == 2


@pytest.mark.asyncio
async def test_get_timeout():
    q = PriorityQueue()
    with pytest.raises(asyncio.TimeoutError):
        await q.get(timeout=0.1)
