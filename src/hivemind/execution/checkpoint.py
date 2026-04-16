"""Checkpoint store — saves agent state when budget is exhausted or agent is preempted.

Allows resumption by spawning a continuation agent with the saved context.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class Checkpoint:
    agent_id: str
    task_id: str
    state: dict
    stdout_so_far: str = ""
    tokens_used: int = 0
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "task_id": self.task_id,
            "state": self.state,
            "stdout_so_far": self.stdout_so_far,
            "tokens_used": self.tokens_used,
            "created_at": self.created_at,
        }


class CheckpointStore:
    """Manages checkpoint files for agent state persistence."""

    def __init__(self, checkpoint_dir: str = ".hivemind/checkpoints") -> None:
        self.checkpoint_dir = Path(checkpoint_dir)
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        self._checkpoints: dict[str, Checkpoint] = {}

    async def save(self, checkpoint: Checkpoint) -> str:
        """Save a checkpoint to disk. Returns the file path."""
        filename = f"{checkpoint.agent_id}_{checkpoint.task_id}_{int(checkpoint.created_at)}.json"
        filepath = self.checkpoint_dir / filename

        data = checkpoint.to_dict()
        filepath.write_text(json.dumps(data, indent=2))

        self._checkpoints[checkpoint.agent_id] = checkpoint
        logger.info(
            "Checkpoint: saved %s (task=%s, tokens=%d)",
            checkpoint.agent_id,
            checkpoint.task_id,
            checkpoint.tokens_used,
        )
        return str(filepath)

    async def load(self, agent_id: str) -> Checkpoint | None:
        """Load the most recent checkpoint for an agent."""
        # Check in-memory cache first
        if agent_id in self._checkpoints:
            return self._checkpoints[agent_id]

        # Scan checkpoint directory
        pattern = f"{agent_id}_*.json"
        files = sorted(self.checkpoint_dir.glob(pattern), reverse=True)
        if not files:
            return None

        try:
            data = json.loads(files[0].read_text())
            cp = Checkpoint(
                agent_id=data["agent_id"],
                task_id=data["task_id"],
                state=data["state"],
                stdout_so_far=data.get("stdout_so_far", ""),
                tokens_used=data.get("tokens_used", 0),
                created_at=data.get("created_at", 0),
            )
            self._checkpoints[agent_id] = cp
            return cp
        except (json.JSONDecodeError, KeyError) as exc:
            logger.error("Checkpoint: failed to load %s: %s", files[0], exc)
            return None

    async def load_by_path(self, path: str) -> Checkpoint | None:
        """Load a checkpoint from a specific file path."""
        try:
            data = json.loads(Path(path).read_text())
            return Checkpoint(
                agent_id=data["agent_id"],
                task_id=data["task_id"],
                state=data["state"],
                stdout_so_far=data.get("stdout_so_far", ""),
                tokens_used=data.get("tokens_used", 0),
                created_at=data.get("created_at", 0),
            )
        except (json.JSONDecodeError, KeyError, FileNotFoundError) as exc:
            logger.error("Checkpoint: failed to load %s: %s", path, exc)
            return None

    async def list_checkpoints(self, task_id: str | None = None) -> list[Checkpoint]:
        """List all checkpoints, optionally filtered by task."""
        results = []
        for filepath in sorted(self.checkpoint_dir.glob("*.json"), reverse=True):
            try:
                data = json.loads(filepath.read_text())
                if task_id and data.get("task_id") != task_id:
                    continue
                results.append(
                    Checkpoint(
                        agent_id=data["agent_id"],
                        task_id=data["task_id"],
                        state=data["state"],
                        stdout_so_far=data.get("stdout_so_far", ""),
                        tokens_used=data.get("tokens_used", 0),
                        created_at=data.get("created_at", 0),
                    )
                )
            except (json.JSONDecodeError, KeyError):
                continue
        return results

    async def delete(self, agent_id: str) -> bool:
        """Delete all checkpoints for an agent."""
        deleted = False
        for filepath in self.checkpoint_dir.glob(f"{agent_id}_*.json"):
            filepath.unlink()
            deleted = True
        self._checkpoints.pop(agent_id, None)
        return deleted

    async def cleanup(self, max_age: float = 86400.0) -> int:
        """Remove checkpoints older than max_age seconds."""
        now = time.time()
        removed = 0
        for filepath in self.checkpoint_dir.glob("*.json"):
            try:
                data = json.loads(filepath.read_text())
                created = data.get("created_at", 0)
                if now - created > max_age:
                    filepath.unlink()
                    removed += 1
            except (json.JSONDecodeError, KeyError):
                filepath.unlink()
                removed += 1
        return removed
