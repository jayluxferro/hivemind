"""HiveMind MCP Server — exposes scheduler tools via MCP protocol."""

from __future__ import annotations

import asyncio
import json
import logging

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from .execution.checkpoint import CheckpointStore
from .execution.pool import AgentPool
from .proxy.server import ProxyServer
from .scheduler.admission import AdmissionController
from .scheduler.backpressure import BackpressureController
from .scheduler.budget import BudgetManager
from .scheduler.queue import PriorityQueue
from .scheduler.rate_limiter import RateLimiter
from .tools.setup import setup_tool, SUPPORTED_TOOLS
from .storage.db import Database
from .storage.models import HiveMindConfig
from .tools.batch import batch_submit
from .tools.budget import manage_budget
from .tools.metrics import get_metrics
from .tools.priority import set_priority
from .tools.status import get_status
from .tools.submit import submit_task

logger = logging.getLogger(__name__)


class HiveMindServer:
    """MCP server that wraps all HiveMind functionality."""

    def __init__(self, config: HiveMindConfig | None = None) -> None:
        self.config = config or HiveMindConfig()
        self.config.apply_provider_defaults()

        # Core components
        self.db = Database(self.config.db_path)
        self.admission = AdmissionController(self.config.max_concurrency)
        self.rate_limiter = RateLimiter()
        self.backpressure = BackpressureController(
            max_concurrency=self.config.max_concurrency,
            additive_increase=self.config.aimd_additive_increase,
            multiplicative_decrease=self.config.aimd_multiplicative_decrease,
            latency_target_ms=self.config.latency_target_ms,
            min_concurrency=self.config.min_concurrency,
        )
        self.budget_manager = BudgetManager(
            total_budget=self.config.total_token_budget,
            default_agent_budget=self.config.default_agent_budget,
        )
        self.queue = PriorityQueue()
        self.pool = AgentPool(
            proxy_url=f"http://{self.config.proxy_host}:{self.config.proxy_port}",
        )
        self.checkpoint_store = CheckpointStore()

        # Proxy server
        self.proxy = ProxyServer(
            config=self.config,
            admission=self.admission,
            rate_limiter=self.rate_limiter,
            backpressure=self.backpressure,
            budget_manager=self.budget_manager,
            db=self.db,
        )

        # MCP server
        self.mcp = Server("hivemind")
        self._register_tools()

        # Background scheduler task
        self._scheduler_task: asyncio.Task | None = None

    def _register_tools(self) -> None:
        @self.mcp.list_tools()
        async def list_tools() -> list[Tool]:
            return [
                Tool(
                    name="hm.submit",
                    description="Submit an agent task to the HiveMind scheduler. The task will be queued and executed when a slot is available.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "command": {
                                "type": "string",
                                "description": "Shell command to execute as the agent",
                            },
                            "priority": {
                                "type": "string",
                                "enum": ["low", "normal", "high", "critical"],
                                "default": "normal",
                                "description": "Scheduling priority",
                            },
                            "token_budget": {
                                "type": "integer",
                                "description": "Max tokens this agent can consume",
                            },
                            "estimated_tokens": {
                                "type": "integer",
                                "description": "Estimated tokens needed (helps SJF scheduling)",
                            },
                            "dependencies": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Task IDs that must complete first",
                            },
                        },
                        "required": ["command"],
                    },
                ),
                Tool(
                    name="hm.batch",
                    description="Submit multiple agent tasks at once. Tasks are queued and scheduled based on priority and dependencies.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "tasks": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "command": {"type": "string"},
                                        "priority": {"type": "string"},
                                        "token_budget": {"type": "integer"},
                                        "estimated_tokens": {"type": "integer"},
                                        "dependencies": {
                                            "type": "array",
                                            "items": {"type": "string"},
                                        },
                                    },
                                    "required": ["command"],
                                },
                                "description": "Array of task specifications",
                            },
                        },
                        "required": ["tasks"],
                    },
                ),
                Tool(
                    name="hm.status",
                    description="Check the status of a specific task or the overall scheduler queue.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "task_id": {
                                "type": "string",
                                "description": "Specific task ID to check (omit for queue overview)",
                            },
                        },
                    },
                ),
                Tool(
                    name="hm.priority",
                    description="Change a task's scheduling priority.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "task_id": {
                                "type": "string",
                                "description": "Task ID to modify",
                            },
                            "priority": {
                                "type": "string",
                                "enum": ["low", "normal", "high", "critical"],
                                "description": "New priority level",
                            },
                        },
                        "required": ["task_id", "priority"],
                    },
                ),
                Tool(
                    name="hm.budget",
                    description="Manage token budgets — check status, set per-agent limits, or set global budget.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "action": {
                                "type": "string",
                                "enum": ["status", "set_agent", "set_total"],
                                "default": "status",
                                "description": "Budget action to perform",
                            },
                            "agent_id": {
                                "type": "string",
                                "description": "Agent ID (for set_agent or status)",
                            },
                            "budget": {
                                "type": "integer",
                                "description": "Token budget to set (for set_agent)",
                            },
                            "total_budget": {
                                "type": "integer",
                                "description": "Global token budget (for set_total)",
                            },
                        },
                    },
                ),
                Tool(
                    name="hm.metrics",
                    description="Get comprehensive scheduler performance metrics including admission control, rate limits, backpressure, latency, and token usage.",
                    inputSchema={
                        "type": "object",
                        "properties": {},
                    },
                ),
                Tool(
                    name="hm.config",
                    description="View or update HiveMind scheduler configuration.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "max_concurrency": {
                                "type": "integer",
                                "description": "Max concurrent API requests",
                            },
                            "upstream_url": {
                                "type": "string",
                                "description": "Upstream API URL",
                            },
                            "total_token_budget": {
                                "type": "integer",
                                "description": "Global token budget",
                            },
                            "default_agent_budget": {
                                "type": "integer",
                                "description": "Default per-agent token budget",
                            },
                            "latency_target_ms": {
                                "type": "number",
                                "description": "Target latency for backpressure (ms)",
                            },
                            "max_retries": {
                                "type": "integer",
                                "description": "Max retries per request",
                            },
                        },
                    },
                ),
                Tool(
                    name="hm.setup",
                    description="Generate configuration snippets to integrate HiveMind with AI coding tools (Claude Code, Cursor, Windsurf, Codex, Copilot, etc).",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "tool": {
                                "type": "string",
                                "enum": SUPPORTED_TOOLS + ["list"],
                                "default": "generic",
                                "description": "Target tool to generate config for, or 'list' to see all",
                            },
                            "proxy_port": {
                                "type": "integer",
                                "default": 8765,
                                "description": "HiveMind proxy port",
                            },
                            "upstream_url": {
                                "type": "string",
                                "description": "Upstream API URL",
                            },
                            "max_concurrency": {
                                "type": "integer",
                                "default": 5,
                                "description": "Max concurrent requests",
                            },
                        },
                    },
                ),
            ]

        @self.mcp.call_tool()
        async def call_tool(name: str, arguments: dict) -> list[TextContent]:
            try:
                result = await self._dispatch_tool(name, arguments)
                return [TextContent(type="text", text=json.dumps(result, indent=2))]
            except Exception as exc:
                logger.error("Tool %s error: %s", name, exc)
                return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]

    async def _dispatch_tool(self, name: str, arguments: dict) -> dict:
        if name == "hm.submit":
            return await submit_task(
                queue=self.queue,
                command=arguments["command"],
                priority=arguments.get("priority", "normal"),
                token_budget=arguments.get("token_budget"),
                estimated_tokens=arguments.get("estimated_tokens"),
                dependencies=arguments.get("dependencies"),
                metadata=arguments.get("metadata"),
            )

        elif name == "hm.batch":
            return await batch_submit(
                queue=self.queue,
                tasks=arguments["tasks"],
            )

        elif name == "hm.status":
            return await get_status(
                queue=self.queue,
                task_id=arguments.get("task_id"),
            )

        elif name == "hm.priority":
            return await set_priority(
                queue=self.queue,
                task_id=arguments["task_id"],
                priority=arguments["priority"],
            )

        elif name == "hm.budget":
            return await manage_budget(
                budget_manager=self.budget_manager,
                action=arguments.get("action", "status"),
                agent_id=arguments.get("agent_id"),
                budget=arguments.get("budget"),
                total_budget=arguments.get("total_budget"),
            )

        elif name == "hm.metrics":
            return await get_metrics(
                admission=self.admission,
                rate_limiter=self.rate_limiter,
                backpressure=self.backpressure,
                budget_manager=self.budget_manager,
                queue=self.queue,
                latency_tracker=self.proxy.latency_tracker,
                db=self.db,
            )

        elif name == "hm.config":
            return await self._handle_config(arguments)

        elif name == "hm.setup":
            return await setup_tool(
                tool=arguments.get("tool", "generic"),
                proxy_host=self.config.proxy_host,
                proxy_port=arguments.get("proxy_port", self.config.proxy_port),
                upstream_url=arguments.get("upstream_url", self.config.upstream_url),
                max_concurrency=arguments.get("max_concurrency", self.config.max_concurrency),
            )

        else:
            return {"error": f"Unknown tool: {name}"}

    async def _handle_config(self, arguments: dict) -> dict:
        """View or update configuration."""
        if not arguments:
            return {"config": self.config.to_dict()}

        updates = {}
        if "max_concurrency" in arguments:
            new_val = arguments["max_concurrency"]
            self.config.max_concurrency = new_val
            await self.admission.set_max_concurrency(new_val)
            updates["max_concurrency"] = new_val

        if "upstream_url" in arguments:
            self.config.upstream_url = arguments["upstream_url"]
            updates["upstream_url"] = arguments["upstream_url"]

        if "total_token_budget" in arguments:
            val = arguments["total_token_budget"]
            self.config.total_token_budget = val
            await self.budget_manager.set_total_budget(val)
            updates["total_token_budget"] = val

        if "default_agent_budget" in arguments:
            self.config.default_agent_budget = arguments["default_agent_budget"]
            updates["default_agent_budget"] = arguments["default_agent_budget"]

        if "latency_target_ms" in arguments:
            self.config.latency_target_ms = arguments["latency_target_ms"]
            self.backpressure._latency_target = arguments["latency_target_ms"]
            updates["latency_target_ms"] = arguments["latency_target_ms"]

        if "max_retries" in arguments:
            self.config.max_retries = arguments["max_retries"]
            self.proxy.retry_policy.max_retries = arguments["max_retries"]
            updates["max_retries"] = arguments["max_retries"]

        return {
            "updated": updates,
            "config": self.config.to_dict(),
        }

    async def _run_scheduler_loop(self) -> None:
        """Background loop that dequeues tasks and spawns agents."""
        while True:
            try:
                # Wait for a task from the queue
                task = await self.queue.get(timeout=5.0)

                # Register with budget manager
                if task.token_budget:
                    await self.budget_manager.register_agent(task.id, task.token_budget)

                # Spawn agent
                try:
                    agent = await self.pool.spawn(
                        command=task.command,
                        task_id=task.id,
                        agent_id=task.id,
                    )
                    task.agent_id = agent.id

                    # Wait for completion in background
                    asyncio.create_task(self._wait_task_completion(task, agent))

                except Exception as exc:
                    await self.queue.complete(task.id, error=str(exc))

            except asyncio.TimeoutError:
                # No tasks — update backpressure concurrency
                recommended = self.backpressure.recommended_concurrency
                if recommended != self.admission.max_concurrency:
                    await self.admission.set_max_concurrency(recommended)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Scheduler loop error: %s", exc)
                await asyncio.sleep(1)

    async def _wait_task_completion(self, task, agent) -> None:
        """Wait for an agent to finish and update task state."""
        from .storage.models import TaskState
        import time

        while True:
            await asyncio.sleep(2)
            current_agent = await self.pool.get_agent(agent.id)
            if current_agent is None or current_agent.state in ("completed", "failed", "killed"):
                break

        # Update task
        if current_agent and current_agent.state == "completed":
            task.completed_at = time.time()
            await self.queue.complete(task.id, result=current_agent.stdout_log[:10000])
        else:
            error = "unknown"
            if current_agent:
                error = current_agent.stderr_log[:2000] or f"exit code {current_agent.exit_code}"
            await self.queue.complete(task.id, error=error)

        # Store in DB
        try:
            task.state = (await self.queue.get_task(task.id)).state
            await self.db.update_task(task)
        except Exception as exc:
            logger.warning("Failed to persist task %s: %s", task.id, exc)

    async def start(self) -> None:
        """Initialize all components."""
        await self.db.connect()
        self._scheduler_task = asyncio.create_task(self._run_scheduler_loop())
        logger.info("HiveMind server started")

    async def stop(self) -> None:
        """Shut down all components."""
        if self._scheduler_task:
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                pass
        await self.pool.kill_all()
        await self.proxy.stop_background()
        await self.db.close()
        logger.info("HiveMind server stopped")

    async def run_mcp(self) -> None:
        """Run as an MCP stdio server."""
        await self.start()
        try:
            async with stdio_server() as (read_stream, write_stream):
                await self.mcp.run(read_stream, write_stream, self.mcp.create_initialization_options())
        finally:
            await self.stop()


async def run_server(config: HiveMindConfig | None = None) -> None:
    server = HiveMindServer(config)
    await server.run_mcp()


def main() -> None:
    """CLI entry point for the MCP server."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    asyncio.run(run_server())


if __name__ == "__main__":
    main()
