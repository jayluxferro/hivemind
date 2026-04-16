"""hm.submit — Submit a single agent task to the scheduler."""

from __future__ import annotations

from ..scheduler.queue import PriorityQueue
from ..storage.models import Task, TaskPriority


async def submit_task(
    queue: PriorityQueue,
    command: str,
    priority: str = "normal",
    token_budget: int | None = None,
    estimated_tokens: int | None = None,
    dependencies: list[str] | None = None,
    metadata: dict | None = None,
) -> dict:
    """Submit a task to the HiveMind scheduler.

    Args:
        command: Shell command to execute as the agent
        priority: low, normal, high, or critical
        token_budget: Max tokens this agent can consume
        estimated_tokens: Estimated tokens needed (for SJF scheduling)
        dependencies: List of task IDs that must complete first
        metadata: Arbitrary key-value metadata

    Returns:
        Task info dict with the assigned task ID
    """
    priority_map = {
        "low": TaskPriority.LOW,
        "normal": TaskPriority.NORMAL,
        "high": TaskPriority.HIGH,
        "critical": TaskPriority.CRITICAL,
    }
    task_priority = priority_map.get(priority.lower(), TaskPriority.NORMAL)

    task = Task(
        command=command,
        priority=task_priority,
        token_budget=token_budget,
        estimated_tokens=estimated_tokens,
        dependencies=dependencies or [],
        metadata=metadata or {},
    )

    await queue.submit(task)

    return {
        "task_id": task.id,
        "state": task.state.value,
        "priority": task.priority.name,
        "message": f"Task {task.id} submitted to scheduler",
    }
