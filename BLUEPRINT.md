# HiveMind

**An OS-level scheduler for concurrent LLM coding agents.**

When you spawn 10 agents, they shouldn't all stampede the API at once. HiveMind sits between the agents and the LLM provider, managing concurrency, rate limits, priority, and resource allocation — the way an OS kernel manages processes competing for CPU.

---

## The Problem (observed today, 2026-04-15)

11 parallel coding agents spawned simultaneously. All shared:
- One Anthropic API key (rate-limited)
- One network connection (through a Burp proxy)
- One filesystem (writing to the same `pocs/` directory)

Result: 3 agents died (27% failure rate). The failures were ECONNRESET and 502 — classic symptoms of connection exhaustion and rate limiting. The 8 surviving agents all completed successfully because they happened to get their requests through before the bottleneck hit.

**The waste:** Those 3 dead agents consumed ~45K tokens each before dying. Total wasted: ~135K tokens, ~15 minutes wall time, zero output.

**The irony:** If the 11 agents had been staggered by just 5 seconds each, all 11 would have succeeded. The problem isn't capacity — it's coordination.

---

## The Thesis

> Concurrent LLM agent workloads exhibit the same resource contention patterns as OS processes (CPU scheduling, memory pressure, I/O contention). Applying OS scheduling principles — admission control, fair queuing, backpressure, and priority scheduling — to LLM agent orchestration eliminates the failure modes caused by uncoordinated parallel execution.

---

## How It Works

### Without HiveMind (current state)
```
User: "Launch 11 agents"

Agent 1  ──→ API ──→ response ──→ API ──→ ...
Agent 2  ──→ API ──→ response ──→ API ──→ ...
Agent 3  ──→ API ──→ ECONNRESET ✗ (dead)
Agent 4  ──→ API ──→ response ──→ API ──→ ...
Agent 5  ──→ API ──→ response ──→ API ──→ ...
Agent 6  ──→ API ──→ 502 ✗ (dead)
Agent 7  ──→ API ──→ response ──→ API ──→ ...
  ...
Agent 11 ──→ API ──→ 429 ✗ (dead)

All 11 hit the API simultaneously. 3 die. Tokens wasted.
```

### With HiveMind
```
User: "Launch 11 agents"

HiveMind Scheduler:
  - API concurrency limit: 5 (measured, not guessed)
  - Admits agents 1-5 immediately
  - Queues agents 6-11
  - When agent 1 finishes a turn → agent 6 gets a slot
  - If agent 3 gets a 429 → back off 2s, retry, don't kill
  - Token budget: 500K total → each agent gets ~45K ceiling
  - Priority: agent writing poc_01 (simple) runs before poc_07 (complex)

Agent 1  ──→ [HiveMind] ──→ API ──→ response ──→ ...  ✓
Agent 2  ──→ [HiveMind] ──→ API ──→ response ──→ ...  ✓
Agent 3  ──→ [HiveMind] ──→ API ──→ 429 ──→ wait 2s ──→ retry ──→ ✓
Agent 4  ──→ [HiveMind] ──→ API ──→ response ──→ ...  ✓
Agent 5  ──→ [HiveMind] ──→ API ──→ response ──→ ...  ✓
Agent 6  ──→ [queued... agent 1 done] ──→ API ──→ ... ✓
  ...
Agent 11 ──→ [queued... agent 5 done] ──→ API ──→ ... ✓

All 11 complete. Zero waste. Maybe 20% slower wall time, but 100% success.
```

---

## The Five Scheduling Primitives

### 1. Admission Control
Don't let all agents hit the API at once. Measure the provider's actual concurrency limit (not the documented one — the real one) and enforce it.

```
Observed: Anthropic allows ~5 concurrent streaming connections per key.
HiveMind: Maintains an admission gate of size 5. Agent 6 waits until agent 1 releases.
```

This alone would have fixed today's failures.

### 2. Fair Queue with Priority
Not all agents are equal. A 30-line script should run before a 600-line script because it finishes faster and frees a slot sooner. Shortest-job-first reduces average completion time.

```
Priority factors:
  - Estimated complexity (tokens needed)
  - Dependencies (agent B needs agent A's output → A runs first)
  - User-specified priority
  - Deadline (CI pipeline has a timeout)
```

### 3. Rate Limit Awareness
HiveMind tracks the provider's rate limit headers (`x-ratelimit-remaining`, `retry-after`) and proactively throttles before hitting limits — not after.

```
API response headers:
  x-ratelimit-remaining-tokens: 12000
  x-ratelimit-remaining-requests: 3

HiveMind: "Only 3 requests left in this window. 
           Pause agents 4-11 for 8 seconds."
```

### 4. Backpressure Propagation
When the API slows down (latency increases), HiveMind reduces concurrency automatically. When it speeds up, it increases. Like TCP congestion control but for LLM API calls.

```
Normal: 5 concurrent agents, 200ms API latency
Degraded: API latency spikes to 2000ms
HiveMind: Reduce to 2 concurrent agents, queue the rest
Recovery: Latency drops → ramp back up to 5
```

### 5. Token Budget Management
Each agent gets a token ceiling. If an agent is approaching its budget, HiveMind can:
- Warn it to wrap up
- Checkpoint its state and kill it
- Split the remaining work into a new, smaller agent

```
Budget: 500K tokens across 11 agents → ~45K each
Agent 7 at 40K tokens, still going → warning
Agent 7 at 45K tokens → checkpoint + kill + spawn continuation agent
```

---

## What Makes This Novel

### The OS Scheduling Analogy (paper framing)

| OS Concept | HiveMind Equivalent |
|-----------|---------------------|
| Process | LLM agent (stateful, long-running, resource-consuming) |
| CPU time | API request slots (rate-limited) |
| Memory | Context window (fixed per model) |
| I/O | Network calls to API provider |
| Scheduler | HiveMind admission control + fair queue + circuit breaker |
| Virtual memory / swap | Checkpointing agent state to disk when context is full |
| OOM killer | Token budget enforcement |
| TCP congestion control | Backpressure from API latency |
| Circuit breaker | Trip on sustained errors, half-open probe, auto-reset |
| Fork bomb protection | Max concurrent agents per user/key |

Nobody has formalized this analogy for LLM agents. Existing frameworks (LangChain, CrewAI, AutoGen) spawn agents without resource management. They're running a multi-process OS without a scheduler.

### Comparison to Existing Work

| System | Admission Control | Rate Limit Aware | Backpressure | Token Budget | Priority Queue |
|--------|:-:|:-:|:-:|:-:|:-:|
| Claude Code (raw) | N | N | N | N | N |
| LangChain | N | Basic retry | N | N | N |
| CrewAI | N | N | N | N | Manual |
| AutoGen | N | N | N | N | N |
| Semantic Kernel | N | Basic | N | N | N |
| **HiveMind** | **Y** | **Y** | **Y** | **Y** | **Y** |

---

## Architecture

```
+-----------------------------------------------+
|              MCP Server                        |
|                                                |
|  Tools:                                        |
|    hm.submit     Submit agent task             |
|    hm.batch      Submit N tasks at once        |
|    hm.status     Check task/queue status       |
|    hm.priority   Adjust task priority          |
|    hm.budget     Set/check token budgets       |
|    hm.metrics    Scheduler performance stats   |
|    hm.config     Tune scheduler parameters     |
+-------------------+---------------------------+
                    |
     +--------------+------------------+
     |          Scheduler              |
     |                                 |
     |  Admission Controller           |
     |    - Concurrency gate (condition var)      |
     |    - Measured, not configured   |
     |                                 |
     |  Priority Queue                 |
     |    - Shortest-job-first         |
     |    - Dependency DAG             |
     |    - User priority override     |
     |                                 |
     |  Rate Limit Tracker             |
     |    - Parse x-ratelimit-* hdrs   |
     |    - Proactive throttle         |
     |    - Per-provider profiles      |
     |                                 |
     |  Backpressure Controller        |
     |    - Latency-based AIMD         |
     |    - Circuit breaker            |
     |    - Direct admission wiring    |
     |                                 |
     |  Token Budget Manager           |
     |    - Per-agent ceiling           |
     |    - Global pool                |
     |    - Checkpoint on exhaust      |
     +--------------+------------------+
                    |
     +--------------+------------------+
     |        Execution Layer          |
     |                                 |
     |  Agent Pool                     |
     |    - Subprocess management      |
     |    - Stdin/stdout capture       |
     |    - Health monitoring          |
     |                                 |
     |  API Proxy                      |
     |    - Intercepts LLM API calls   |
     |    - Injects rate limit logic   |
     |    - Counts tokens in/out       |
     |    - Records latency            |
     |                                 |
     |  Checkpoint Store               |
     |    - Agent state snapshots      |
     |    - Partial output capture     |
     |    - Resumption support         |
     +---------------------------------+
```

---

## The API Proxy — The Key Implementation Detail

HiveMind doesn't modify the agents. It runs a **local HTTP proxy** that the agents' API calls route through. The proxy:

1. Counts concurrent connections → enforces admission control
2. Reads rate limit headers → proactively queues
3. Measures latency → applies backpressure
4. Counts tokens (from request/response bodies) → enforces budgets
5. Retries on 429/502/ECONNRESET → transparently to the agent

The agent doesn't know HiveMind exists. It just makes normal API calls. HiveMind sits in the middle like a reverse proxy.

```
Agent → http://localhost:8765/v1/... → HiveMind Proxy → Anthropic / OpenAI / Ollama / Azure
                                            ↑
                                Admission control
                                Rate limit tracking (provider-aware headers)
                                Backpressure (AIMD + circuit breaker)
                                Token counting (both API formats)
                                Provider-specific retry (429/502/529)
```

The provider is auto-detected from the upstream URL. Each provider has its own profile with rate limit header names, retry codes, concurrency defaults, and latency targets.

This is the cleanest implementation path because:
- Zero changes to agent code
- Works with any LLM provider (Anthropic, OpenAI, Azure, Ollama, Google)
- Works with any agent framework (Claude Code, Cursor, Copilot, Codex, LangChain, raw SDK)
- Easy to measure — all traffic flows through one point

---

## Research (arXiv)

### Title
"HiveMind: OS-Inspired Scheduling for Concurrent LLM Agent Workloads"

### Claims
1. Uncoordinated parallel LLM agents exhibit resource contention patterns analogous to unscheduled OS processes
2. Admission control alone reduces agent failure rates by 80%+ (from today: 27% failure → <5%)
3. Latency-based backpressure (AIMD) maintains throughput within 90% of maximum while preventing connection exhaustion
4. Token budget management with checkpointing reduces wasted compute by 60%+

### Evaluation
- **Micro-benchmark:** Spawn 5/10/20/50 concurrent agents, measure failure rate with and without HiveMind
- **Provider comparison:** Test against Anthropic, OpenAI, and local Ollama (different rate limit behaviors)
- **Real workload:** Replay today's 11-agent IDLEx PoC generation, measure tokens wasted
- **Stress test:** 50 agents, deliberate rate limit exhaustion, measure recovery time
- **Ablation:** Each primitive individually (admission only, backpressure only, etc.) to measure contribution

### Key Metrics
- Failure rate (agents that die vs complete)
- Token waste (tokens consumed by failed agents)
- Throughput (tasks completed per minute)
- Latency (wall time from submit to complete)
- Recovery time (time from API error to successful retry)

---

## Relationship to Phoenix

Phoenix = **task-level** resilience (checkpoint, retry, decompose).
HiveMind = **system-level** resource management (scheduling, rate limits, concurrency).

They're complementary. HiveMind prevents the failures. Phoenix recovers from the ones that slip through.

Stack: Agent → Phoenix (task resilience) → HiveMind (resource scheduling) → API

---

## File Structure
```
hivemind/
  pyproject.toml
  BLUEPRINT.md
  src/hivemind/
    __init__.py
    __main__.py
    server.py                  # MCP server
    tools/
      submit.py                # hm.submit
      batch.py                 # hm.batch
      status.py                # hm.status
      priority.py              # hm.priority
      budget.py                # hm.budget
      metrics.py               # hm.metrics
    scheduler/
      admission.py             # Concurrency gate (condition var)
      queue.py                 # Priority queue with dependency DAG
      rate_limiter.py          # Rate limit header parsing + proactive throttle
      backpressure.py          # AIMD latency-based concurrency control
      budget.py                # Token budget allocation + enforcement
    proxy/
      server.py                # Local HTTP proxy (the core)
      interceptor.py           # Request/response interception (provider-aware)
      token_counter.py         # Count tokens from API payloads (Anthropic + OpenAI)
      streaming.py             # SSE streaming pass-through + token extraction
      retry.py                 # Provider-specific retry (429/502/529)
      latency_tracker.py       # Rolling latency measurement
    execution/
      pool.py                  # Agent subprocess pool
      health.py                # Health monitoring
      checkpoint.py            # State snapshot on budget exhaust
    storage/
      db.py                    # SQLite: tasks, metrics, checkpoints
      models.py
  tests/
  docs/
    RESEARCH.md
```

## Build Plan
1. API proxy (the core — intercept, count, retry)
2. Admission controller (condition variable gate)
3. Rate limit tracker (parse headers)
4. Backpressure (AIMD)
5. Token budget manager
6. Priority queue
7. MCP server wrapper
8. Evaluation: replay 11-agent workload
