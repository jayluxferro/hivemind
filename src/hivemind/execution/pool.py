"""Agent subprocess pool — manages agent lifecycle.

Spawns agent processes with ANTHROPIC_BASE_URL pointed at the HiveMind proxy,
monitors their health, and captures their output.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
import uuid
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class AgentProcess:
    """A managed agent subprocess."""

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    task_id: str | None = None
    command: str = ""
    pid: int | None = None
    state: str = "pending"  # pending, running, completed, failed, killed
    exit_code: int | None = None
    started_at: float | None = None
    completed_at: float | None = None
    stdout_log: str = ""
    stderr_log: str = ""
    _process: asyncio.subprocess.Process | None = field(default=None, repr=False)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "command": self.command,
            "pid": self.pid,
            "state": self.state,
            "exit_code": self.exit_code,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }


class AgentPool:
    """Manages a pool of agent subprocesses."""

    def __init__(self, proxy_url: str = "http://127.0.0.1:8765", max_agents: int = 20) -> None:
        self.proxy_url = proxy_url
        self.max_agents = max_agents
        self._agents: dict[str, AgentProcess] = {}
        self._lock = asyncio.Lock()

    @property
    def active_count(self) -> int:
        return sum(1 for a in self._agents.values() if a.state == "running")

    @property
    def total_count(self) -> int:
        return len(self._agents)

    async def spawn(
        self,
        command: str,
        task_id: str | None = None,
        agent_id: str | None = None,
        env_extra: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> AgentProcess:
        """Spawn an agent subprocess with proxy environment configured."""
        async with self._lock:
            if self.active_count >= self.max_agents:
                raise RuntimeError(f"Agent pool full ({self.max_agents} max)")

        agent = AgentProcess(
            id=agent_id or uuid.uuid4().hex[:12],
            task_id=task_id,
            command=command,
        )

        # Build environment — point API calls through HiveMind proxy
        env = os.environ.copy()
        env["ANTHROPIC_BASE_URL"] = self.proxy_url
        env["OPENAI_BASE_URL"] = f"{self.proxy_url}/v1"
        env["HIVEMIND_AGENT_ID"] = agent.id
        if env_extra:
            env.update(env_extra)

        try:
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=cwd,
            )
            agent._process = process
            agent.pid = process.pid
            agent.state = "running"
            agent.started_at = time.time()

            async with self._lock:
                self._agents[agent.id] = agent

            logger.info(
                "Pool: spawned agent %s (pid=%d, task=%s): %s",
                agent.id,
                agent.pid,
                task_id,
                command[:100],
            )

            # Start background task to wait for completion
            asyncio.create_task(self._wait_for_agent(agent))

            return agent

        except Exception as exc:
            agent.state = "failed"
            agent.stderr_log = str(exc)
            async with self._lock:
                self._agents[agent.id] = agent
            logger.error("Pool: failed to spawn agent %s: %s", agent.id, exc)
            raise

    async def _wait_for_agent(self, agent: AgentProcess) -> None:
        """Background task that waits for an agent process to complete."""
        if agent._process is None:
            return

        try:
            stdout, stderr = await agent._process.communicate()
            agent.exit_code = agent._process.returncode
            agent.stdout_log = stdout.decode("utf-8", errors="replace") if stdout else ""
            agent.stderr_log = stderr.decode("utf-8", errors="replace") if stderr else ""
            agent.completed_at = time.time()

            if agent.exit_code == 0:
                agent.state = "completed"
                logger.info("Pool: agent %s completed (exit=0)", agent.id)
            else:
                agent.state = "failed"
                logger.warning(
                    "Pool: agent %s failed (exit=%d): %s",
                    agent.id,
                    agent.exit_code,
                    agent.stderr_log[:200],
                )
        except asyncio.CancelledError:
            agent.state = "killed"
            agent.completed_at = time.time()

    async def kill(self, agent_id: str) -> bool:
        """Kill an agent process."""
        agent = self._agents.get(agent_id)
        if not agent or not agent._process or agent.state != "running":
            return False

        try:
            agent._process.send_signal(signal.SIGTERM)
            # Give it 5 seconds to die gracefully
            try:
                await asyncio.wait_for(agent._process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                agent._process.kill()
            agent.state = "killed"
            agent.completed_at = time.time()
            logger.info("Pool: killed agent %s", agent_id)
            return True
        except ProcessLookupError:
            agent.state = "completed"
            return True

    async def kill_all(self) -> int:
        """Kill all running agents. Returns count killed."""
        killed = 0
        for agent_id in list(self._agents):
            if await self.kill(agent_id):
                killed += 1
        return killed

    async def get_agent(self, agent_id: str) -> AgentProcess | None:
        return self._agents.get(agent_id)

    async def list_agents(self, state: str | None = None) -> list[AgentProcess]:
        if state:
            return [a for a in self._agents.values() if a.state == state]
        return list(self._agents.values())

    async def cleanup_finished(self, max_age: float = 3600.0) -> int:
        """Remove finished agent records older than max_age seconds."""
        now = time.time()
        to_remove = []
        for agent_id, agent in self._agents.items():
            if agent.state in ("completed", "failed", "killed"):
                if agent.completed_at and (now - agent.completed_at) > max_age:
                    to_remove.append(agent_id)
        for agent_id in to_remove:
            del self._agents[agent_id]
        return len(to_remove)

    @property
    def stats(self) -> dict:
        by_state: dict[str, int] = {}
        for agent in self._agents.values():
            by_state[agent.state] = by_state.get(agent.state, 0) + 1
        return {
            "active": self.active_count,
            "total": self.total_count,
            "max_agents": self.max_agents,
            "by_state": by_state,
        }
