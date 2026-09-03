# SPEC — Token ledger & cost dashboard

## 1. Context

The manifold mesh has every signal needed to understand token spend — hivemind
already counts tokens per cloud-bound request, knows the provider and model,
the agent identity (hashed), latency, and status — but nothing accumulates
them anywhere queryable.  Every optimization decision this session (local
routing share, compression tuning, per-agent budgets, model tiers) ended in
"we need numbers".  This SPEC builds the minimal feedback loop: a generic
ledger in Postgres, a data-driven cost table, and one dashboard page.  No
prompt content is ever stored (privacy); no third-party observability product.

Scope is deliberately Phase 1: visibility only.  Automated knob-turning is a
later decision that this data enables.

## 2. Design decisions (binding)

- **D1 — One writer, one choke point.** Hivemind is the only layer that sees
  every cloud-bound request with usage already parsed (both streaming and
  non-streaming paths).  It is the sole ledger writer.
- **D2 — Generic by observation, not code.** `provider` and `model` are
  stored as observed values; usage columns are all optional (providers vary).
  A new model/provider requires zero code change.
- **D3 — Cost is data.** A `model_pricing` table (per provider+model, prices
  per 1M tokens for input / cache-read / cache-write / output).  Cost is
  computed on READ via a view so updating the table re-prices history.
  Unmapped models carry NULL cost (never guessed); local models cost 0.
- **D4 — Fail-open.** If Postgres is down or the DSN unset, the proxy keeps
  serving; telemetry is dropped.  Telemetry must never block or error a request.
- **D5 — No prompt content.** Only hashed identities and numeric usage.
- **D6 — Storage.** The user's existing Postgres instance, schema
  `mesh_telemetry`, self-managed by the emitter (CREATE TABLE IF NOT EXISTS
  on connect; CREATE SCHEMA IF NOT EXISTS).  No lattice code touched.

## 3. Schema (Postgres)

```sql
CREATE SCHEMA IF NOT EXISTS mesh_telemetry;

CREATE TABLE IF NOT EXISTS mesh_telemetry.token_usage (
    id           BIGSERIAL PRIMARY KEY,
    ts           TIMESTAMPTZ NOT NULL DEFAULT now(),
    agent_hash   TEXT NOT NULL,          -- hivemind rate-limit bucket (already hashed)
    provider     TEXT NOT NULL,          -- observed (detect_provider)
    model        TEXT NOT NULL,          -- observed from the request
    tokens_in    BIGINT,                 -- all optional: providers vary
    tokens_out   BIGINT,
    cache_read   BIGINT,
    cache_write  BIGINT,
    reasoning    BIGINT,
    latency_ms   DOUBLE PRECISION,
    status       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS token_usage_ts_idx ON mesh_telemetry.token_usage (ts);

CREATE TABLE IF NOT EXISTS mesh_telemetry.model_pricing (
    provider          TEXT NOT NULL,
    model             TEXT NOT NULL,
    price_in          DOUBLE PRECISION,  -- USD per 1M tokens
    price_cache_read  DOUBLE PRECISION,
    price_cache_write DOUBLE PRECISION,
    price_out         DOUBLE PRECISION,
    PRIMARY KEY (provider, model)
);

CREATE OR REPLACE VIEW mesh_telemetry.usage_cost AS
SELECT u.*,
       CASE WHEN p.provider IS NULL THEN NULL
            ELSE round(
              (coalesce(u.tokens_in,0) - coalesce(u.cache_write,0) - coalesce(u.cache_read,0))
                * coalesce(p.price_in,0)
              + coalesce(u.cache_read,0)  * coalesce(p.price_cache_read,0)
              + coalesce(u.cache_write,0) * coalesce(p.price_cache_write,0)
              + coalesce(u.tokens_out,0)  * coalesce(p.price_out,0)
            ) / 1e6, 6)
       END AS cost_usd
FROM mesh_telemetry.token_usage u
LEFT JOIN mesh_telemetry.model_pricing p
  ON p.provider = u.provider AND p.model = u.model;
```

Seed the pricing table with the models in current use (deepseek-chat,
deepseek-reasoner, claude-sonnet-4-*, kimi models, local models at 0) — a
documented SQL file (`tools/seed_pricing.sql`) the operator runs/edits by hand.

## 4. Hivemind emitter

- New module `src/hivemind/telemetry/ledger.py`:
  - `TelemetryLedger` — lazy psycopg3 connection (`psycopg` dependency added
    to pyproject), `connect(dsn)` + `CREATE SCHEMA/TABLE IF NOT EXISTS`
    on first use, one `INSERT` per row (autocommit), reconnect with backoff
    on failure, `close()`.
  - All calls wrapped so ANY exception is logged at DEBUG and swallowed
    (D4).  The proxy path never awaits the ledger in a way that can raise.
  - Enabled only when `--telemetry-dsn` (or `MESH_TELEMETRY_DSN`) is set;
    otherwise a null-object no-op.
- `cli_args.py` / server wiring: `--telemetry-dsn` plumbed into the
  interceptor (or a module-level singleton the interceptor calls).
- Interceptor hooks (both paths — streaming AND `handle_request`), called
  AFTER the response completes with: agent bucket, provider name,
  model (from request body; absent → `"unknown"`), tokens_in/out from the
  existing counters, cache_read/cache_write from the cache telemetry or
  response headers where available, latency, final status.
- Fire-and-forget: schedule the INSERT (e.g. `asyncio.create_task`) so a
  slow PG can never add request-path latency; the ledger serializes its own
  writes through a small queue if needed (PG at current volumes is trivial —
  one task + internal queue is enough).

## 5. Dashboard (single page, served by hivemind)

- `GET /_telemetry` — HTML page (single file, inline CSS + vanilla JS, no
  build step, no framework).
- `GET /_telemetry/data?days=14` — JSON with:
  - daily cost + tokens (stacked by provider), local-share note
  - top models by cost/tokens
  - per-agent cost/tokens/error rate (agent_hash labels only)
  - per-provider latency p50/p95
- Charts: simple, readable, no external CDN dependencies (offline-friendly).
- If the ledger is unreachable, the page renders "telemetry unavailable"
  without erroring.

## 6. Test contract

- Ledger unit tests with a fake/no-op connection (no real PG in CI): rows
  formatted correctly; exceptions swallowed; null-object when DSN unset.
- Interceptor hook tests: both paths emit exactly one row with the expected
  fields (mock the ledger singleton).
- Endpoint tests: `/_telemetry` renders HTML, `/_telemetry/data` returns the
  JSON shape with a mocked ledger reader.
- Existing 257 tests stay green.

## 7. Validation (manual)

1. Set `--telemetry-dsn` to the local PG, restart hivemind, send a few
   requests through the chain → rows appear in `mesh_telemetry.token_usage`.
2. Seed pricing → `usage_cost.cost_usd` populated for known models, NULL for
   unknown, 0 for local.
3. Open `http://127.0.0.1:8765/_telemetry` → dashboard renders.
4. Kill PG → proxy keeps serving, no errors beyond DEBUG logs.

## 8. Cutover

Additive: new module + optional flag.  No existing behavior changes when the
DSN is unset.  Manifold config gains an optional `--telemetry-dsn` in the
hivemind command (operator's choice).

### Implementation notes (deviations reality forced — 2026-09-03)

1. **§3 view DDL does not parse as written.**  `round( ... ) / 1e6, 6)` puts
   the `, 6)` precision argument OUTSIDE `round()`.  Both the ledger's
   `_SCHEMA_DDL` and `tools/seed_pricing.sql` implement the intent —
   `round((...) / 1e6, 6)` — i.e. dollars = per-1M-token products divided by
   1e6, rounded to 6 decimals.  If the SPEC's exact DDL was ever applied to a
   database, re-run the corrected view from either file.
2. **`psycopg[binary]>=3.2` was already in `pyproject.toml`** when this work
   landed (used by the request-log DB).  No dependency change was needed.
3. **Recorded `provider` is the *detected profile display name*, not the
   API brand.**  `detect_provider` maps api.deepseek.com / api.kimi.com /
   api.z.ai / api.myapi.world onto the ANTHROPIC profile, so DeepSeek/Kimi
   traffic records provider `'Anthropic'` — the model name is what
   disambiguates the real service.  Pricing rows in `seed_pricing.sql` are
   keyed on the RECORDED name for this reason.  A request that bypasses a
   profile (provider unset) records `'unknown'`.
4. **The streaming record hook lives in an OUTERMOST `finally`, not after
   the last yield.**  The proxy server pulls the first yield of an early
   error (401 flow) and then `aclose()`s the generator: `GeneratorExit`
   arrives right after that first yield and skips any trailing statements.
   `finally` runs on normal completion, mid-stream failure, early aclose and
   cancellation alike — and a generator is single-pass, so the hook fires
   exactly once per request regardless of exit path.
5. **Cache counters come from the response usage blocks, not headers.**  No
   provider in the chain reports cache usage in headers.  Non-streaming rows
   parse the buffered body; streaming rows merge per-key MAXIMA across SSE
   frames (Anthropic usage blocks are cumulative snapshots that may split
   across byte chunks).  Absent → NULL.
6. **Reconnect is lazy, not backoff-scheduled.**  A failed write drops the
   connection and logs at DEBUG; the NEXT write reconnects.  The SPEC's
   "reconnect with backoff" would have put sleeps on a fire-and-forget path —
   the queue is bounded (10 000 rows) and one background task drains it, so
   a dead PG drops rows instead of growing memory or delaying anything.
7. **Fast-fail errors are recorded too.**  Circuit-open / admission-timeout
   503s and rate-limit-queue-full 429s emit a row with the real status, so
   the dashboard's error rate counts every rejected request, not just
   upstream failures.
8. **`reasoning` is always NULL** — no provider in the chain reports
   reasoning tokens separately (column kept per §3 for future use).
9. **Dashboard read failures** surface as HTTP 200 with
   `{"error": "telemetry unavailable"}` (renders the page's error state);
   `days` clamps to 1–365, default 14.

## 9. Success metrics

- One dashboard answers: cost per day/provider/model/agent, local vs cloud
  share, error rate — without grepping logs.
- Zero request-path impact: ledger writes are fire-and-forget and fail-open.

## 10. Known limitations

- No prompt content by design (D5) — the dashboard cannot show *what* a
  request asked, only its numbers.
- Locally-routed traffic is not in the ledger (splitter stats already count
  it; a local-share metric on the dashboard comes from splitter stats, not
  the ledger).
- Pricing table is maintained by hand (deliberate — no auto-fetch drift).
