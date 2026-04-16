# HiveMind

OS-inspired scheduler for concurrent LLM coding agents. A transparent HTTP proxy with admission control, rate limit awareness, AIMD backpressure, token budgets, and priority scheduling.

## The Problem

11 parallel agents, one API key. 3 died from ECONNRESET/502 — classic connection exhaustion. The surviving 8 worked fine. If they'd been staggered by 5 seconds, all 11 would have succeeded. The problem isn't capacity — it's coordination.

## How It Works

```
Agent → http://localhost:8765/v1/messages → HiveMind Proxy → https://api.anthropic.com/v1/messages
                                                ↑
                                    Admission control (semaphore)
                                    Rate limit tracking (header parsing)
                                    AIMD backpressure (latency-based)
                                    Token counting (budget enforcement)
                                    Transparent retry (429/502/ECONNRESET)
```

Agents don't know HiveMind exists. They make normal API calls. HiveMind sits in the middle.

## Install

```bash
pip install -e ".[dev]"
```

## Usage

### Standalone Proxy

```bash
# Start the proxy (agents point ANTHROPIC_BASE_URL at this)
hivemind proxy --port 8765 --upstream https://api.anthropic.com --max-concurrency 5

# Then run agents with:
ANTHROPIC_BASE_URL=http://127.0.0.1:8765 claude-code ...
```

### MCP Server

```bash
# Run as MCP stdio server
hivemind serve --max-concurrency 5

# Or just:
hivemind
```

### MCP Tools

| Tool | Description |
|------|-------------|
| `hm.submit` | Submit an agent task to the scheduler |
| `hm.batch` | Submit multiple tasks at once |
| `hm.status` | Check task/queue status |
| `hm.priority` | Adjust task priority (low/normal/high/critical) |
| `hm.budget` | Set/check token budgets (per-agent and global) |
| `hm.metrics` | Scheduler performance stats |
| `hm.config` | Tune scheduler parameters at runtime |

## Architecture

### Five Scheduling Primitives

1. **Admission Control** — Concurrency semaphore. Don't let more than N requests hit the API at once.
2. **Rate Limit Tracking** — Parse `x-ratelimit-*` headers and proactively pause before hitting limits.
3. **AIMD Backpressure** — Like TCP congestion control. Low latency → increase concurrency. High latency → cut it.
4. **Token Budget Management** — Per-agent ceilings and global pool. Warning at 85%, checkpoint on exhaust.
5. **Priority Queue with DAG** — Shortest-job-first, dependency tracking, dynamic reprioritization.

## Testing

```bash
python3 -m pytest tests/ -v -p no:pytest_ethereum -p no:xonsh
```

100 tests covering all scheduler primitives, proxy interceptor, database layer, token counter, and MCP tools.
