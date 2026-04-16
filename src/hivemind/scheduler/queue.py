"""Priority queue with dependency DAG for task scheduling.

Tasks are ordered by:
1. Priority (CRITICAL > HIGH > NORMAL > LOW)
2. Estimated complexity (shortest-job-first — fewer tokens → higher priority)
3. Creation time (FIFO within same priority)

Dependencies are tracked as a DAG — a task won't be dequeued until
all its dependencies have completed.
"""

from __future__ import annotations

import asyncio
import heapq
import logging
from collections import defaultdict

from ..storage.models import Task, TaskState

logger = logging.getLogger(__name__)


class CycleError(Exception):
    """Raised when adding a dependency would create a cycle."""


class PriorityQueue:
    """Thread-safe priority queue with dependency tracking."""

    def __init__(self) -> None:
        self._heap: list[Task] = []
        self._tasks: dict[str, Task] = {}  # id → task (all tasks, any state)
        self._dependents: dict[str, set[str]] = defaultdict(set)  # dep_id → {tasks that depend on it}
        self._event = asyncio.Event()
        self._lock = asyncio.Lock()

    @property
    def pending_count(self) -> int:
        return len(self._heap)

    @property
    def total_count(self) -> int:
        return len(self._tasks)

    async def submit(self, task: Task) -> None:
        """Add a task to the queue."""
        async with self._lock:
            self._tasks[task.id] = task

            # Track dependency edges
            for dep_id in task.dependencies:
                self._dependents[dep_id].add(task.id)

            # Only enqueue if all dependencies are satisfied
            if self._deps_satisfied(task):
                task.state = TaskState.QUEUED
                heapq.heappush(self._heap, task)
                self._event.set()
                logger.info("Queue: task %s queued (priority=%s)", task.id, task.priority.name)
            else:
                task.state = TaskState.PENDING
                unmet = [d for d in task.dependencies if not self._is_completed(d)]
                logger.info(
                    "Queue: task %s pending — waiting on dependencies: %s",
                    task.id,
                    unmet,
                )

    async def get(self, timeout: float | None = None) -> Task:
        """Get the highest-priority task that's ready to run. Blocks if empty."""
        while True:
            async with self._lock:
                # Find the first task in the heap whose deps are satisfied
                while self._heap:
                    task = heapq.heappop(self._heap)
                    # Verify task is still queued (not cancelled/completed)
                    if task.id in self._tasks and task.state == TaskState.QUEUED:
                        if self._deps_satisfied(task):
                            task.state = TaskState.RUNNING
                            logger.info("Queue: dequeued task %s", task.id)
                            return task
                        else:
                            # Deps no longer satisfied (shouldn't happen, but be safe)
                            task.state = TaskState.PENDING

                self._event.clear()

            # Wait for new tasks
            if timeout is not None:
                try:
                    await asyncio.wait_for(self._event.wait(), timeout=timeout)
                except asyncio.TimeoutError:
                    raise asyncio.TimeoutError("No tasks available within timeout")
            else:
                await self._event.wait()

    async def complete(self, task_id: str, result: str | None = None, error: str | None = None) -> None:
        """Mark a task as completed and unblock dependents."""
        async with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return

            if error:
                task.state = TaskState.FAILED
                task.error = error
            else:
                task.state = TaskState.COMPLETED
                task.result = result

            # Check if any pending tasks are now unblocked
            for dependent_id in self._dependents.get(task_id, set()):
                dep_task = self._tasks.get(dependent_id)
                if dep_task and dep_task.state == TaskState.PENDING:
                    if self._deps_satisfied(dep_task):
                        dep_task.state = TaskState.QUEUED
                        heapq.heappush(self._heap, dep_task)
                        logger.info(
                            "Queue: task %s unblocked (dependency %s completed)",
                            dependent_id,
                            task_id,
                        )

            if self._heap:
                self._event.set()

    async def cancel(self, task_id: str) -> bool:
        """Cancel a pending/queued task. Returns True if cancelled."""
        async with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False
            if task.state in (TaskState.PENDING, TaskState.QUEUED):
                task.state = TaskState.FAILED
                task.error = "cancelled"
                return True
            return False

    async def update_priority(self, task_id: str, priority: int) -> bool:
        """Change a task's priority. Re-inserts into heap if queued."""
        from ..storage.models import TaskPriority

        async with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return False
            task.priority = TaskPriority(priority)
            if task.state == TaskState.QUEUED:
                # Re-heapify
                self._heap = [t for t in self._heap if t.id != task_id]
                heapq.heapify(self._heap)
                heapq.heappush(self._heap, task)
            return True

    def _deps_satisfied(self, task: Task) -> bool:
        """Check if all dependencies of a task are completed."""
        for dep_id in task.dependencies:
            if not self._is_completed(dep_id):
                return False
        return True

    def _is_completed(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if task is None:
            # Unknown dependency — treat as satisfied (external)
            return True
        return task.state == TaskState.COMPLETED

    def _has_cycle(self, task_id: str, dep_id: str) -> bool:
        """Check if adding dep_id as a dependency of task_id creates a cycle."""
        visited = set()
        stack = [dep_id]
        while stack:
            current = stack.pop()
            if current == task_id:
                return True
            if current in visited:
                continue
            visited.add(current)
            task = self._tasks.get(current)
            if task:
                stack.extend(task.dependencies)
        return False

    async def add_dependency(self, task_id: str, dep_id: str) -> None:
        """Add a dependency edge. Raises CycleError if it would create a cycle."""
        async with self._lock:
            if self._has_cycle(task_id, dep_id):
                raise CycleError(f"Adding dependency {task_id} → {dep_id} would create a cycle")
            task = self._tasks.get(task_id)
            if task and dep_id not in task.dependencies:
                task.dependencies.append(dep_id)
                self._dependents[dep_id].add(task_id)

    async def get_task(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)

    async def list_tasks(self, state: TaskState | None = None) -> list[Task]:
        if state:
            return [t for t in self._tasks.values() if t.state == state]
        return list(self._tasks.values())

    @property
    def stats(self) -> dict:
        by_state: dict[str, int] = {}
        for task in self._tasks.values():
            by_state[task.state.value] = by_state.get(task.state.value, 0) + 1
        return {
            "total": len(self._tasks),
            "queued": len(self._heap),
            "by_state": by_state,
        }
