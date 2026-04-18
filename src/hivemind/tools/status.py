"""hm.status — Check task and queue status."""

from __future__ import annotations

from ..scheduler.queue import PriorityQueue


async def get_status(
    queue: PriorityQueue,
    task_id: str | None = None,
) -> dict:
    """Get status of a specific task or the entire queue.

    Args:
        task_id: If provided, get status of this specific task.
                 If None, get overall queue status.

    Returns:
        Task details or queue summary
    """
    if task_id:
        task = await queue.get_task(task_id)
        if not task:
            return {"error": f"Task {task_id} not found"}
        return {
            "task": task.to_dict(),
        }

    # Queue overview
    all_tasks = await queue.list_tasks()
    by_state: dict[str, list[dict]] = {}
    for task in all_tasks:
        state_name = task.state.value
        if state_name not in by_state:
            by_state[state_name] = []
        by_state[state_name].append({
            "id": task.id,
            "command": task.command[:80],
            "priority": task.priority.name,
            "tokens_used": task.tokens_used,
        })

    return {
        "queue": queue.stats,
        "tasks_by_state": {
            state: {
                "count": len(tasks),
                "tasks": tasks[:10],  # Limit to 10 per state
            }
            for state, tasks in by_state.items()
        },
    }
