"""hm.priority — Adjust task priority."""

from __future__ import annotations

from ..scheduler.queue import PriorityQueue
from ..storage.models import TaskPriority


async def set_priority(
    queue: PriorityQueue,
    task_id: str,
    priority: str,
) -> dict:
    """Change a task's scheduling priority.

    Args:
        task_id: The task to modify
        priority: New priority — low, normal, high, or critical

    Returns:
        Updated task info
    """
    priority_map = {
        "low": TaskPriority.LOW,
        "normal": TaskPriority.NORMAL,
        "high": TaskPriority.HIGH,
        "critical": TaskPriority.CRITICAL,
    }

    new_priority = priority_map.get(priority.lower())
    if new_priority is None:
        return {"error": f"Invalid priority '{priority}'. Use: low, normal, high, critical"}

    success = await queue.update_priority(task_id, new_priority.value)
    if not success:
        return {"error": f"Task {task_id} not found"}

    task = await queue.get_task(task_id)
    return {
        "task_id": task_id,
        "priority": new_priority.name,
        "state": task.state.value if task else "unknown",
        "message": f"Task {task_id} priority set to {new_priority.name}",
    }
