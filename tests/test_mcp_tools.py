"""Tests for MCP tool functions."""

import pytest

from hivemind.scheduler.budget import BudgetManager
from hivemind.scheduler.queue import PriorityQueue
from hivemind.storage.models import TaskPriority
from hivemind.tools.batch import batch_submit
from hivemind.tools.budget import manage_budget
from hivemind.tools.priority import set_priority
from hivemind.tools.status import get_status
from hivemind.tools.submit import submit_task


@pytest.mark.asyncio
async def test_submit_task():
    q = PriorityQueue()
    result = await submit_task(
        queue=q,
        command="echo hello",
        priority="high",
        token_budget=10000,
    )
    assert "task_id" in result
    assert result["priority"] == "HIGH"
    assert result["state"] == "queued"

    # Verify it's in the queue
    task = await q.get_task(result["task_id"])
    assert task is not None
    assert task.priority == TaskPriority.HIGH


@pytest.mark.asyncio
async def test_batch_submit():
    q = PriorityQueue()
    result = await batch_submit(
        queue=q,
        tasks=[
            {"command": "echo 1", "priority": "low"},
            {"command": "echo 2", "priority": "high"},
            {"command": "echo 3"},
        ],
    )
    assert result["submitted"] == 3
    assert result["errors"] == 0
    assert len(result["tasks"]) == 3


@pytest.mark.asyncio
async def test_batch_submit_with_errors():
    q = PriorityQueue()
    result = await batch_submit(
        queue=q,
        tasks=[
            {"command": "echo 1"},
            {"priority": "high"},  # Missing command
            {"command": "echo 3"},
        ],
    )
    assert result["submitted"] == 2
    assert result["errors"] == 1


@pytest.mark.asyncio
async def test_get_status_queue():
    q = PriorityQueue()
    await submit_task(queue=q, command="task 1")
    await submit_task(queue=q, command="task 2")

    result = await get_status(queue=q)
    assert "queue" in result
    assert result["queue"]["total"] == 2


@pytest.mark.asyncio
async def test_get_status_specific_task():
    q = PriorityQueue()
    sub_result = await submit_task(queue=q, command="specific task")
    task_id = sub_result["task_id"]

    result = await get_status(queue=q, task_id=task_id)
    assert "task" in result
    assert result["task"]["id"] == task_id


@pytest.mark.asyncio
async def test_get_status_not_found():
    q = PriorityQueue()
    result = await get_status(queue=q, task_id="nonexistent")
    assert "error" in result


@pytest.mark.asyncio
async def test_set_priority():
    q = PriorityQueue()
    sub = await submit_task(queue=q, command="reprioritize")
    task_id = sub["task_id"]

    result = await set_priority(queue=q, task_id=task_id, priority="critical")
    assert result["priority"] == "CRITICAL"

    task = await q.get_task(task_id)
    assert task.priority == TaskPriority.CRITICAL


@pytest.mark.asyncio
async def test_set_priority_invalid():
    q = PriorityQueue()
    result = await set_priority(queue=q, task_id="x", priority="ultra")
    assert "error" in result


@pytest.mark.asyncio
async def test_manage_budget_status():
    bm = BudgetManager(total_budget=100000)
    result = await manage_budget(budget_manager=bm, action="status")
    assert result["total_budget"] == 100000


@pytest.mark.asyncio
async def test_manage_budget_set_agent():
    bm = BudgetManager()
    result = await manage_budget(
        budget_manager=bm,
        action="set_agent",
        agent_id="a1",
        budget=50000,
    )
    assert result["budget"] == 50000
    assert result["agent_id"] == "a1"


@pytest.mark.asyncio
async def test_manage_budget_set_total():
    bm = BudgetManager()
    result = await manage_budget(
        budget_manager=bm,
        action="set_total",
        total_budget=200000,
    )
    assert result["total_budget"] == 200000
