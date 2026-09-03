# HiveMind

[![CI](https://github.com/jayluxferro/hivemind/actions/workflows/ci.yml/badge.svg)](https://github.com/jayluxferro/hivemind/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

**OS-inspired scheduler for concurrent LLM coding agents.**

When you spawn 10 agents, they shouldn't all stampede the API at once. HiveMind sits between the agents and the LLM provider as a transparent HTTP proxy, managing concurrency, rate limits, priority, and resource allocation — the way an OS kernel manages processes competing for CPU.

## Quickstart

```bash
# Install
pip install hivemind-scheduler

# Start the proxy (auto-detects provider from URL)
hivemind proxy                                          # Anthropic (default)
hivemind proxy --upstream https://api.openai.com        # OpenAI
# Equivalent console entry point (same flags as `hivemind proxy`):
hivemind-proxy --upstream https://api.openai.com

# In another terminal, point your agents at it
ANTHROPIC_BASE_URL=http://127.0.0.1:8765 claude code   # Claude Code
OPENAI_BASE_URL=http://127.0.0.1:8765/v1 cursor        # Cursor / Copilot / Codex
```

That's it. Your agents now go through HiveMind. Zero code changes.

## The Problem

11 parallel agents, one API key. 3 died from ECONNRESET/502 — classic connection exhaustion. The surviving 8 worked fine. If they'd been staggered by 5 seconds, all 11 would have succeeded.

**The problem isn't capacity — it's coordination.**

## How It Works

```
Agent → http://localhost:8765/v1/... → HiveMind Proxy → Anthropic / OpenAI / Ollama / Azure
                                            ↑
                                Admission control (condition variable)
                                Rate limit tracking (provider-aware)
                                Per-agent rate buckets + fair-share governor
                                AIMD backpressure + circuit breaker
                                Token counting (budget enforcement)
                                Provider-specific retry (429/502/529)
                                SSE streaming pass-through
```

Agents don't know HiveMind exists. They make normal API calls. HiveMind sits in the middle.

## Results

Evaluated across 7 scenarios with 5–50 concurrent agents:

| Scenario | Without HiveMind | With HiveMind |
|----------|:----------------:|:-------------:|
| 10 agents, 50 req/min | 100% failure | **0% failure** |
| 11 agents, realistic errors | 73% failure | **0% failure** |
| 20 agents, stress test | 100% failure | **10% failure** |
| 50 agents, extreme | 100% failure | **0% failure** |

## Install

```bash
pip install hivemind-scheduler          # Core
pip install hivemind-scheduler[all]     # + tiktoken + redis
```

Or from source:

```bash
git clone https://github.com/jayluxferro/hivemind.git
cd hivemind
pip install -e ".[dev]"
```

## Usage

### Transparent Proxy (recommended)

```bash
# Start the proxy — auto-detects provider from URL
hivemind proxy --upstream https://api.anthropic.com
hivemind proxy --upstream https://api.openai.com
hivemind proxy --upstream http://localhost:11434  # Ollama

# Same behavior via the `hivemind-proxy` script (useful in minimal containers)
hivemind-proxy --upstream http://localhost:11434

# Local HTTPS / self-signed upstream (development only)
hivemind proxy --insecure --upstream https://127.0.0.1:8443

# Point agents at it
export ANTHROPIC_BASE_URL=http://127.0.0.1:8765
export OPENAI_BASE_URL=http://127.0.0.1:8765/v1
```

### MCP Server

```bash
hivemind serve
# Optional: same tuning flags as the proxy, plus TLS toggle
hivemind serve --upstream https://api.openai.com --insecure
```

### Per-Agent Rate Limiting

Multiple coding agents share one HiveMind without sharing one rate-limit window — a busy session no longer stalls the others. Every request is bucketed by identity, resolved in order:

1. **Explicit header/query** — `x-hivemind-agent-id` / `x-hivemind-session-id` headers or `?agent_id=` (budgets and DB logging use this explicit channel only).
2. **Session hint** — the Anthropic `metadata.user_id` field in the request body. Claude Code embeds a per-session UUID there, so same-key sessions separate automatically with zero configuration.
3. **Fingerprint** — SHA-256 of credential + user-agent (`fp-…`), or user-agent only (`ua-…`). Raw API keys are never stored or logged.
4. **`anonymous`** — one shared bucket when nothing identifies the caller.

```bash
hivemind proxy --rate-limit-scope global          # pre-1.0 behavior: one shared window
hivemind proxy --agent-limit batch-bot:rpm=20,tpm=40000   # cap one agent (repeatable)
```

**Fair-share governor.** Per-agent windows isolate agents from each other, but the provider key is still shared. When combined in-window traffic exceeds the provider limit, every bucket's effective limit shrinks proportionally (`limit / aggregate`): heavy agents absorb the squeeze, light agents keep flowing, and the factor returns to 1.0 as the windows drain. Provider 429s and `retry-after` always pause everyone globally — header state reflects the shared key.

**Residual limitation.** A tool that neither sets an explicit header nor sends `metadata.user_id` shares one bucket per credential+user-agent pair. For guaranteed session separation:

```bash
ANTHROPIC_CUSTOM_HEADERS="x-hivemind-session-id: my-session" claude
```

### IDE Integration

Generate config for your IDE/tool:

```bash
hivemind setup claude-code
hivemind setup cursor
hivemind setup windsurf
hivemind setup codex
hivemind setup copilot
hivemind setup all         # Show all configs
```

### CLI Reference

`hivemind proxy` and **`hivemind-proxy`** share the same flags (implemented in `hivemind.cli_args`). The parent `hivemind` command also accepts `--log-level` before the subcommand (for example `hivemind --log-level DEBUG proxy`). **`hivemind-proxy`** includes `--log-level` on its own parser.

#### `hivemind proxy` / `hivemind-proxy`

| Flag | Default | Description |
|------|---------|-------------|
| `--host` | `127.0.0.1` | Bind address |
| `--port` | `8765` | Bind port |
| `--upstream` | `https://api.anthropic.com` | Upstream API URL (provider auto-detected) |
| `--max-concurrency` | `5` | Max concurrent in-flight requests |
| `--min-concurrency` | `1` | Floor for AIMD backpressure |
| `--db` | `postgresql://hivemind@localhost:5432/hivemind` | Postgres connection string (`HIVEMIND_DB_URL` env overrides) |
| `--max-retries` | `3` | Max transparent retries on 429/502 |
| `--retry-base-delay` | `1.0` | Base retry delay (seconds) |
| `--retry-max-delay` | `30.0` | Max retry delay (seconds) |
| `--latency-target` | auto | Latency target in ms for AIMD (auto-detected from provider) |
| `--aimd-increase` | auto | AIMD additive increase (auto-detected from provider) |
| `--aimd-decrease` | auto | AIMD multiplicative decrease (auto-detected from provider) |
| `--total-budget` | unlimited | Global token budget |
| `--agent-budget` | unlimited | Default per-agent token budget |
| `--rate-limit-scope` | `per_agent` | `per_agent` buckets rate limits by session; `global` shares one window |
| `--agent-limit` | none | Per-agent override (repeatable): `AGENT:rpm=N,tpm=M` |
| `--insecure` | off | Disable upstream TLS certificate verification (dev only) |
| `--log-level` | `INFO` | **`hivemind-proxy` only** — logging verbosity |

#### `hivemind serve`

| Flag | Default | Description |
|------|---------|-------------|
| `--upstream` | `https://api.anthropic.com` | Upstream API URL |
| `--max-concurrency` | `5` | Max concurrent requests |
| `--db` | `postgresql://hivemind@localhost:5432/hivemind` | Postgres connection string (`HIVEMIND_DB_URL` env overrides) |
| `--total-budget` | unlimited | Global token budget |
| `--agent-budget` | unlimited | Default per-agent token budget |
| `--max-retries` | `3` | Max transparent retries |
| `--min-concurrency` | `1` | Floor for AIMD backpressure |
| `--rpm-limit` | auto | Override requests-per-minute limit (auto-detected from provider) |
| `--tpm-limit` | auto | Override tokens-per-minute limit (auto-detected from provider) |
| `--rate-limit-scope` | `per_agent` | `per_agent` buckets rate limits by session; `global` shares one window |
| `--agent-limit` | none | Per-agent override (repeatable): `AGENT:rpm=N,tpm=M` |
| `--insecure` | off | Disable upstream TLS certificate verification (dev only) |

### MCP Tools

| Tool | Description |
|------|-------------|
| `hm.submit` | Submit an agent task to the scheduler |
| `hm.batch` | Submit multiple tasks at once |
| `hm.status` | Check task/queue status |
| `hm.priority` | Adjust task priority (low/normal/high/critical) |
| `hm.budget` | Set/check token budgets (per-agent and global) |
| `hm.metrics` | Scheduler performance stats |
| `hm.config` | Tune scheduler, upstream URL, TLS, and concurrency at runtime (see below) |
| `hm.setup` | Generate IDE/tool integration configs |

#### `hm.config` (runtime updates)

Updates apply to the running MCP server (including the in-process proxy). Notable keys:

| Key | Effect |
|-----|--------|
| `upstream_url` | Re-detects provider, re-seeds rate limits / AIMD defaults from the profile, rebinds the live proxy target, and syncs admission + backpressure. |
| `max_concurrency` / `min_concurrency` | Clamped and pushed to admission + backpressure. |
| `latency_target_ms` | Backpressure latency target. |
| `http_tls_verify` | Recreates the upstream httpx client with verification on or off (boolean). |
| `max_retries` | Proxy retry policy. |
| `rate_limit_scope` | Switches rate-limit windowing between per-agent buckets and one shared global window at runtime. |
| `agent_limit_overrides` | Replaces the per-agent RPM/TPM override registry: `{agent_id: {"rpm": N, "tpm": M}}`. |
| Budget fields | Token budget manager + config mirror. |

## Database

HiveMind persists tasks, agent metrics, and the request log to **PostgreSQL**
(there is no SQLite fallback — the server fails loud at startup if Postgres
is down).  The schema is created and migrated automatically on every start
(`schema_migrations`), so no manual DDL is needed.

### One-time setup (local)

```bash
# As the postgres superuser (socket trust on local Debian/Homebrew setups):
sudo -u postgres psql -v ON_ERROR_STOP=1 -c "CREATE ROLE hivemind LOGIN"
sudo -u postgres psql -v ON_ERROR_STOP=1 -c "CREATE DATABASE hivemind OWNER hivemind"
sudo -u postgres psql -v ON_ERROR_STOP=1 -c "CREATE DATABASE hivemind_test OWNER hivemind"
```

Loopback is trust auth in the local default, so no password is needed:
`postgresql://hivemind@localhost:5432/hivemind` (test DB:
`postgresql://hivemind@localhost:5432/hivemind_test`).  Override the
connection with `HIVEMIND_DB_URL`, or the test connection with
`HIVEMIND_TEST_DB_URL`.

> The standalone `hivemind proxy` accepts `--db` but does not open it —
> request logging happens only in the embedded MCP-server proxy
> (`hivemind serve`).

### Migrating from SQLite

If you have data in a pre-migration `hivemind.db`, import it once:

```bash
uv run python tools/pg-migrate/import_sqlite.py --dry-run   # plan only, touches nothing
uv run python tools/pg-migrate/import_sqlite.py             # real import
```

`--fresh` (default) truncates the target tables first; `--no-fresh` skips
tables that already have rows.  The importer asserts sqlite/PG column
alignment before each table, verifies per-table counts afterwards (exit 1
on mismatch), and advances the identity sequences that explicit-id COPYs
bypass.

### Token ledger + cost dashboard (optional)

A second, opt-in Postgres database records one row per completed cloud-bound
request — token + cache counters, provider/model as observed, latency, final
status — and a dashboard answers "what did this actually cost, per day /
model / agent bucket?" from it.  It is deliberately separate from the
request-log DB: telemetry must never disturb the proxy, so everything about
it fails open and is disabled by default.

```bash
# Enable it (either form):
hivemind proxy --telemetry-dsn postgresql://hivemind@localhost:5432/hivemind
MESH_TELEMETRY_DSN=postgresql://hivemind@localhost:5432/hivemind hivemind proxy

# One-time pricing seed (schema is self-managed; this adds USD-per-1M-token rows):
psql "$MESH_TELEMETRY_DSN" -f tools/seed_pricing.sql   # PLACEHOLDER prices — verify first
```

Schema, index, and the costing view under `mesh_telemetry` are created
automatically by the ledger itself (no manual DDL).  The schema and seed SQL
live in `tools/seed_pricing.sql` and are idempotent.

Dashboard, served by the proxy (same port as the agents' upstream):

| Endpoint | Description |
|---|---|
| `/_telemetry` | Single self-contained HTML page (inline CSS/JS, no CDN) |
| `/_telemetry/data?days=N` | JSON aggregates (`N` clamps to 1–365, default 14) |

Fail-open contract: if Postgres is unreachable — or `--telemetry-dsn` is
unset — the proxy behaves exactly as before.  Writes are fire-and-forget
through a bounded queue drained by one background writer; rows are dropped
(at DEBUG log) rather than ever blocking or failing a request.  The data
endpoint answers `{"error": "telemetry unavailable"}` with HTTP 200 on any
DB failure.  No prompt content is stored — only the hashed rate-limit bucket
(agent_hash), provider/model names, and counters.

> Pricing is **hand-maintained** (never auto-fetched) and seeded with
> placeholder numbers — verify every row against provider price pages before
> trusting the cost column.  Models without a pricing row cost NULL, which
> the dashboard shows as uncosted rather than guessing.

## Architecture

### Five Scheduling Primitives

| # | Primitive | What it does | OS Analogy |
|---|-----------|-------------|------------|
| 1 | **Admission Control** | Concurrency gate — max N requests in-flight | Process scheduler |
| 2 | **Rate Limit Tracking** | Parse `x-ratelimit-*` headers, per-agent windows + fair-share governor, pause proactively | I/O scheduling |
| 3 | **AIMD Backpressure** | Latency-based concurrency: low → increase, high → cut | TCP congestion control |
| 4 | **Token Budgets** | Per-agent + global ceilings, warn at 85%, checkpoint at 100% | OOM killer |
| 5 | **Priority Queue + DAG** | Shortest-job-first, dependency tracking, reprioritization | Nice levels + cgroups |

### Provider Support

Auto-detected from upstream URL:

| Provider | Rate Limit Headers | Default Concurrency | Streaming |
|----------|:-:|:-:|:-:|
| Anthropic | Yes | 5 | Yes |
| OpenAI | Yes | 10 | Yes |
| Azure OpenAI | Yes | 10 | Yes |
| Google (Gemini) | - | 8 | Yes |
| Ollama (local) | - | 2 (GPU) | Yes |

### Optional Features

```bash
pip install hivemind-scheduler[tokenizer]     # tiktoken for accurate token counting
pip install hivemind-scheduler[distributed]   # Redis for multi-machine coordination
```

## Evaluation

Run benchmarks against a mock API (no real API credits needed):

```bash
python -m evaluation.run_benchmark --quick     # 5 agents, 30 seconds
python -m evaluation.run_benchmark --replay    # 11-agent original scenario
python -m evaluation.run_benchmark --ablation  # Test each primitive individually
python -m evaluation.run_benchmark             # Full suite (all scenarios)
```

## Testing

```bash
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run against a real local Postgres — create the `hivemind_test`
database during the one-time setup in the [Database](#database) section.
Override the test connection with `HIVEMIND_TEST_DB_URL` when it differs
from the default `postgresql://hivemind@localhost:5432/hivemind_test`.

See [CONTRIBUTING.md](CONTRIBUTING.md) for **pre-commit** (Ruff) and optional local **Gitleaks** usage.

Automated tests cover scheduler primitives (admission control, backpressure with circuit breaker, rate limiting with provider profiles), proxy, streaming, multi-provider integration (Anthropic + OpenAI), tokenizer, distributed backend, and MCP tools. Run `python -m pytest tests/ -q` for the current count.

## Security

See [SECURITY.md](SECURITY.md) for TLS defaults, secret handling, and **Gitleaks** in CI and publish workflows.

## License

MIT — see [LICENSE](LICENSE).
