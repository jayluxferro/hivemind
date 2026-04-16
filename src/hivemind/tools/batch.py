"""hm.batch — Submit multiple tasks at once."""

from __future__ import annotations

from ..scheduler.queue import PriorityQueue
from ..storage.models import Task, TaskPriority


async def batch_submit(
    queue: PriorityQueue,
    tasks: list[dict],
) -> dict:
    """Submit a batch of tasks to the scheduler.

    Args:
        tasks: List of task dicts, each with:
            - command (required): Shell command
            - priority: low/normal/high/critical
            - token_budget: Max tokens
            - estimated_tokens: Estimated tokens needed
            - dependencies: List of task IDs
            - metadata: Key-value metadata

    Returns:
        Summary with all submitted task IDs
    """
    priority_map = {
        "low": TaskPriority.LOW,
        "normal": TaskPriority.NORMAL,
        "high": TaskPriority.HIGH,
        "critical": TaskPriority.CRITICAL,
    }

    submitted = []
    errors = []

    for i, task_spec in enumerate(tasks):
        try:
            command = task_spec.get("command", "")
            if not command:
                errors.append({"index": i, "error": "missing command"})
                continue

            priority_str = task_spec.get("priority", "normal")
            task = Task(
                command=command,
                priority=priority_map.get(priority_str.lower(), TaskPriority.NORMAL),
                token_budget=task_spec.get("token_budget"),
                estimated_tokens=task_spec.get("estimated_tokens"),
                dependencies=task_spec.get("dependencies", []),
                metadata=task_spec.get("metadata", {}),
            )
            await queue.submit(task)
            submitted.append({
                "task_id": task.id,
                "command": command[:80],
                "priority": task.priority.name,
                "state": task.state.value,
            })
        except Exception as exc:
            errors.append({"index": i, "error": str(exc)})

    return {
        "submitted": len(submitted),
        "errors": len(errors),
        "tasks": submitted,
        "error_details": errors if errors else None,
        "message": f"Submitted {len(submitted)}/{len(tasks)} tasks",
    }
