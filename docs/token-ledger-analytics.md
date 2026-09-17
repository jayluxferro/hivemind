# SPEC — Telemetry analytics platform (Phase 2)

## Context

Phase 1 (`token-ledger.md`) shipped a single-file dashboard with one aggregate JSON
endpoint (`/_telemetry/data?days=N`) and an always-on 10s refresh.  Phase 2 grows it
into an analytics platform: date ranges, drill-downs to per-request rows, multiple
views, server-side export, and bounded retention — while preserving Phase 1's hard
rules (D1 writer, D4 fail-open, D5 no prompt content, D6 self-managed schema).

Supersedes Phase 1 §5 ("Dashboard") for the data contract; `days` remains as a
compatibility alias.

## Design decisions (binding)

- **D7 — Time-bounded reads.** Every read takes an inclusive display range
  `[from_ts, to_ts]` of aware-UTC datetimes; the ledger converts to a half-open SQL
  window `ts >= %s AND ts < %s` (exclusive end = display `to` + 1 day).  Bucketing
  uses `AT TIME ZONE 'UTC'` — day/hour labels never shift with server timezone.
  Max range 366 days; inverted ranges swap; future `to` clamps to now.
- **D8 — Request-level trace, metadata only.** `/_telemetry/requests` returns the
  raw `token_usage` rows (id, ts, agent_hash, provider, model, token/cache counters,
  latency_ms, status).  D5 reaffirmed: prompt content is not in the schema and can
  never appear in any endpoint or export.  Filters (`agent_hash`, `model`,
  `provider`, `status`) are exact-match; sort/order resolve against whitelist dicts
  (user input never interpolates SQL).  Placeholder contract:
  `(from, to)`, filters in `_REQUEST_FILTERS` order, then `limit, offset`.
- **D9 — Server-side streaming export.** CSV and JSONL endpoints stream from a
  named server-side cursor in `fetchmany(1000)` batches.  A pre-flight count query
  runs BEFORE response headers go out (a down DB must fail into the standard
  `{"error": "telemetry unavailable"}` JSON, never a truncated 200 stream).
  CSV uses csv.writer plus an OWASP formula-injection guard (`'`-prefix for cells
  starting with `= + - @ \t \r`).  Cost columns are excluded everywhere (pricing is
  not maintained; the `usage_cost` view stays for opt-in consumers).
- **D10 — Retention pruning.** `TelemetryLedger(retention_days=90)`; a background
  pruner task deletes `token_usage` rows older than the retention on connect, then
  every 24h.  Fail-open (DEBUG logs), cancelled cleanly in `shutdown()` before the
  connection reset.  Config: `HiveMindConfig.telemetry_retention_days` via env
  `HIVEMIND_TELEMETRY_RETENTION_DAYS` (default 90, fail-loud on garbage — the
  `_default_max_rate_wait_s` pattern) and `--telemetry-retention-days` on the proxy
  parser.
- **D11 — Static-file frontend, zero build/zero CDN.** The 746-line `PAGE_HTML`
  string is replaced by a small shell + three real assets served by the proxy
  (`dashboard.css`, `charts.js`, `dashboard.js`) via `importlib.resources.files`
  (wheel-safe), read through a filename whitelist (user input never touches a
  filesystem path), cache-busted with a content-addressed sha256 version hash
  (`?v=…`, `cache-control: public, max-age=…, immutable`).  The shell, JS, and CSS
  contain zero external URLs — offline-only.  Auto-refresh is opt-in: a checkbox,
  default OFF, persisted in `localStorage["hivemindTelemetryAutoRefresh"]` via
  try/catch helpers (private-mode safe).

## API contract

All JSON endpoints: GET; `cache-control: no-store`; ledger error or unconfigured
ledger → **HTTP 200** `{"error": "telemetry unavailable"}` (Phase 1 mapping, D4).
Date params are ISO 8601 (`YYYY-MM-DD` or full datetime, `Z` suffix allowed);
naive inputs treated as UTC.

| Endpoint | Params | Response |
|---|---|---|
| `/_telemetry` | — | HTML shell (no-store) |
| `/_telemetry/data` | `from`, `to`; legacy `days` (clamp [1,365], default 14; explicit from/to wins) | Phase 1 payload shape (`totals`, `daily_agents`, `top_models`, `agents`, `latency`, `generated_at`) + `from`, `to`, `status_codes: [{status, requests}]` (≤12), `latency_models: [{provider, model, requests, p50_ms, p95_ms}]` (≤15) |
| `/_telemetry/requests` | `from`, `to`, `agent_hash`, `model`, `provider`, `status`, `sort` (ts\|tokens\|latency\|status\|agent\|model, default ts), `order` (asc\|desc, default desc), `limit` (clamp [1,500], default 100), `offset` (clamp [0,1e6]) | `{total, limit, offset, from, to, rows: [{id, ts, agent_hash, provider, model, tokens_in, tokens_out, cache_read, cache_write, latency_ms, status}]}` — `ts` ISO `Z`; nullable numeric fields stay null |
| `/_telemetry/facets` | `from`, `to` | `{agents: [{agent_hash, requests}] ≤200, models: [{provider, model, requests}] ≤100, providers: [{provider, requests}]}` ordered by requests DESC |
| `/_telemetry/series` | `from`, `to`, `bucket` (hour\|day, default hour), optional `agent_hash`/`model`/`provider` | `{bucket, rows: [{bucket_start: ISO, requests, tokens_in, tokens_out, cache_read, errors}]}` ascending; hour forced to day when range > 14d |
| `/_telemetry/export.csv` | `from`, `to` + the four filters | streamed CSV: header row, crlf, formula guard; `Content-Disposition: attachment; filename="hivemind-telemetry-<from>--<to>.csv"` |
| `/_telemetry/export.jsonl` | same | streamed JSONL: one compact object per line, keys in `_REQUEST_COLUMNS` order |
| `/_telemetry/static/{filename}` | whitelist `{dashboard.css, charts.js, dashboard.js}` | bytes + correct content-type; unknown → 404; `cache-control: public, max-age=31536000, immutable` |

Export pre-flight: `count_requests(...)` before streaming; mid-stream failure
(DC during export) ends the stream cleanly with a DEBUG log (truncation is
documented behavior; the count-based HTTP status is already decided).

## Final SQL (forms)

- `_RANGE = "ts >= %s AND ts < %s"` replaces `_WINDOW` in the five overview queries
  (each now takes `(from_ts, to_ts)`).
- `_SQL_DAILY_AGENTS`: `WITH filtered AS (SELECT * … WHERE ts >= %s AND ts < %s),
  ranked AS (top-5 agents by window tokens FROM filtered)` → predicate appears once
  (removes the double-param case); `date_trunc('day', f.ts AT TIME ZONE 'UTC')::date`;
  `CASE WHEN r.agent_hash IS NULL THEN 'Other' ELSE u.agent_hash END`.
- `_SQL_LATENCY_MODELS`: percentile_cont p50/p95 grouped by `provider, model`,
  `ORDER BY p95_ms DESC NULLS LAST, requests DESC LIMIT 15`.
- `_SQL_STATUS`: `SELECT status, count(*) AS requests … GROUP BY status
  ORDER BY requests DESC LIMIT 12`.
- Facets: three queries, distinct+count ordered by requests DESC, LIMIT 200/100/—.
- `_SQL_SERIES_HOUR`/`_SQL_SERIES_DAY`: identical SELECT with
  `date_trunc('hour'|'day', ts AT TIME ZONE 'UTC') AS bucket_start`, shared filter
  builder appends `AND <col> = %s` per active filter in `_REQUEST_FILTERS` order.
- Requests: `SELECT {columns} … WHERE ts >= %s AND ts < %s [+filters]
  ORDER BY <whitelisted> <ASC|DESC>, id <ASC|DESC> LIMIT %s OFFSET %s` +
  `SELECT count(*) AS total …` with the same WHERE.  `_REQUEST_COLUMNS` is the
  single source of truth for the SELECT list, CSV header, and JSONL keys.
- `_SQL_PRUNE`: `DELETE FROM mesh_telemetry.token_usage WHERE ts < now() - make_interval(days => %s)`.

Whitelists live in `ledger.py`: `_REQUEST_FILTERS` (tuple of
`(param_name, "col = %s")` — fixed append order), `_REQUEST_SORTS`,
`_REQUEST_ORDERS`; `query.py` imports them for validation (single source of truth).

## Frontend spec

`window.HiveCharts` (charts.js): ported viz toolkit (Set2 fixed-order palette,
Other folding, tooltips with oversized hit targets, legend iff ≥2 series,
tables-below-charts, textContent-only) + builders: `stackedDaily` (day/hour labels),
`bars`, `latencyChart` (provider/model rows), `singleSeries` (drill-down activity;
bars ≤48 buckets, thin line otherwise).

`dashboard.js`: hash-routed views; state lives in the hash
(`#/<view>?from&to&agent&model&provider&status&sort&order&page`); hashchange →
parse → render; drill-downs navigate (back button walks the analysis trail).
- Header (all views): native from/to date inputs, preset chips 24h/7d/14d/30d/90d,
  auto-refresh checkbox default OFF persisted in localStorage (key
  `hivemindTelemetryAutoRefresh`), updated-stamp, CSV/JSONL export links built from
  current state.
- `#/overview`: tiles (requests, tokens in/out, cache reads, error rate), status
  bars (muted single hue — status is a category, never Set2), daily per-agent stack
  (segment click → `#/requests?agent=…`), top-models bars (row → `#/models?model=…`),
  latency by provider + by model, agents table.
- `#/agents`: picker from `/facets` (cached per range), summary tiles,
  `/series` requests/tokens-over-time (hour when range ≤14d else day),
  "view requests" link.
- `#/models`: token bars, client-side cache-hit distribution (single hit-rate
  formula in JS), latency-by-model, per-model series, "view requests" link.
- `#/requests`: filter dropdowns from `/facets`, sortable table (header click
  cycles sort/order), pagination (prev/next + "N of M"), row click → detail panel
  (every field via textContent), footer: "metadata only — no prompt content is
  ever stored" (D5).
- Errors: the Phase 1 "Telemetry unavailable" card renders inside the current view;
  `.loading` dimming; in-flight fetches never overlap.

## Test contract

- Ledger read tests re-key fake-conn fixtures to the new SQL constants; overview =
  7 queries each with `(from, to)`; requests assert the placeholder-order contract;
  series asserts the bucket whitelist; export asserts named-cursor batches + close;
  prune asserts `(retention_days,)` + swallow-on-failure + lifecycle with
  `_PRUNE_INTERVAL_S` monkeypatched small; NullLedger gains every new stub.
- Route tests: static whitelist/404/content-types/no-external-assets across
  shell+JS+CSS; `/data` default range ≈ now-14d→now, explicit from/to passthrough,
  `days` back-compat, clamp/swap; `/requests` clamps; `/series` hour→day clamp;
  export pre-flight failure → 200 JSON; CSV/JSONL content (injection guard matrix).
- `test_telemetry_query.py`: pure parsing matrices (naive/Z/invalid/inverted/cap).
- Real-PG (MESH_TEST_DSN-gated): round-trip via `fetch_requests` + `export_rows`;
  prune round-trip (backdate a row, prune, assert gone; fresh row survives).
- Existing 295 tests stay green; ruff check + format clean.

## Cutover

Additive: all Phase 1 endpoints keep working (`days` alias on `/data`); the page
URL is unchanged.  The Phase 1 self-contained-page test is replaced by shell +
static-asset marker tests.  One line added at the top of `docs/token-ledger.md` §5:
"Superseded by docs/token-ledger-analytics.md (Phase 2)".

## Implementation notes (deviations log)

Recorded by the Phase 2 backend pass (ledger/query/export/dashboard/CLI/routes/tests).
Each entry is a place where the code makes a choice this SPEC does not pin down, or
where the SPEC's literal instruction did not survive contact with Postgres.

1. **Export cursor is created `withhold=True`.** psycopg3 refuses `DECLARE CURSOR`
   outside a transaction block, and every ledger connection is autocommit (Phase 1
   style, kept). Without `WITH HOLD` the first `fetchmany` raises
   `NoActiveSqlTransaction` — verified against Postgres 16. Cost: a held cursor
   materializes its result set on the server, which is the intended trade (D9 keeps
   the rows out of the proxy's memory, and the export is not a hot path).
2. **`_build_request_where()` converts the range itself.** The SPEC pins only the
   placeholder order `(from, to), filters…, limit, offset`; the helper applies
   `to + 1 day` and returns the full params tuple, so *every* endpoint (including
   `/requests`, `/series`, `/facets`, export) honors "a bare `to=2026-09-01` covers
   all of Sept 1". Handlers stay dumb; the conversion exists once.
3. **`ORDER BY … NULLS LAST` in both directions**, with `id` as the tiebreaker.
   Postgres sorts NULLs first on `DESC`, so without this "slowest first" would open
   with rows that never reported a latency. The tiebreaker keeps paging stable when
   timestamps collide.
4. **One shared `_read_conn()` asynccontextmanager** for all seven readers
   (factory → autocommit → `_ensure_schema` → yield → `_safe_close` in `finally`).
   The SPEC lists the reads but not their connection lifecycle; sharing it is what
   guarantees a raising reader cannot leak a connection (the D4 fail-open path).
5. **`NullLedger` keeps the export types.** `count_requests` → `0` and `export_rows`
   → an empty async generator, because both are consumed by arithmetic/iteration
   rather than rendered. Consequence: on an unconfigured deployment
   `/_telemetry/export.csv` answers **200 with a header-only file**, not the
   "telemetry unavailable" JSON — the pre-flight count is the only place that can
   render that marker, and it legitimately succeeds with zero rows.
6. **`prune()` propagates `CancelledError`** (swallowing every other exception at
   DEBUG, as D10 requires). Cancellation is how `shutdown()` joins the pruner inside
   its 1s budget; a swallowed cancel would leave the task outliving the connection it
   is deleting from.
7. **The prune loop deletes inside the loop.** It prunes immediately on connect (a
   restart must not leave an over-retention table sitting for 24h) and then once per
   `_PRUNE_INTERVAL_S`. The first draft had the initial `prune()` *above* the `while`,
   which means retention would have run exactly once per process start — caught by
   the lifecycle test (monkeypatched interval) rather than by review.
8. **`_range_params()` imports `exclusive_end` inside the function body.** `query.py`
   imports the ledger's whitelists at module scope (single source of truth), so a
   module-level import in the other direction is circular.
9. **`query.parse_series_filters()` is an extra helper beyond the SPEC's function
   list.** Reusing `parse_request_filters` would have made `/series` silently accept
   `sort`/`limit`/`offset` (meaningless on pre-aggregated rows) and forced
   `fetch_series` to accept-and-ignore them. The series endpoint takes the
   agent/model/provider filters only.
10. **`fetch_requests` floors `limit`/`offset`.** The SPEC puts the *upper* clamps in
    the parser (single validation point); a negative `LIMIT` is a SQL error rather
    than a clamp, so the ledger refuses to pass one through.
11. **Overview keeps `cost_usd` in the payload** (`totals`, `top_models`, `agents`)
    while the dashboard displays no cost anywhere. The SPEC demotes cost to
    "optional, not displayed" for the *frontend*; dropping the field from the JSON
    would have been a data-contract change beyond that.
12. **Route order is load-bearing:** every
    `/_telemetry/*` route is registered above the `/{path:path}` catch-all, which
    would otherwise forward the dashboard upstream. The static handler answers 404
    JSON for a non-whitelisted name (a dict-key miss, not a path check).

Appended by the Phase 2 frontend pass (`static/dashboard.css`, `static/charts.js`,
`static/dashboard.js`, the shell's asset markers in `test_telemetry_dashboard.py`).
Numbering continues; entries 1–12 are the backend's and are unchanged.

13. **ColorBrewer Set2 fails a dark-surface palette validation, and is kept.** The
    dataviz method's validator was run against `#111` rather than eyeballed, and it
    flags the pinned order: the lightness band is off, `#66c2a5` and `#8da0cb` fall
    under the chroma floor, and the worst adjacent pair (`#8da0cb` vs `#e78ac3`)
    separates by ~14 ΔE in normal vision and ~1.5 ΔE in protanopia — under both the
    ΔE 8 CVD target and the ΔE 15 normal-vision floor. Surface contrast passes. D11
    pins Set2, so it stays; the mitigations the same SPEC mandates (legend whenever
    two series are drawn, per-mark tooltips, 2px gaps between touching fills, a
    plain table under every chart, hue never assigned by rank) are what carry
    readability. This is a deliberate accepted FAIL, recorded in full at the top of
    `charts.js` — not an untested check.
14. **Auto-refresh runs every 15 s (`REFRESH_MS`).** The SPEC requires the control
    and its visibility gating but never names a period. 15 s is fast enough to watch
    a live run and slow enough that a 15-minute hour-bucket is not re-fetched into
    three identical responses; it is not configurable from the UI.
15. **The `24h` preset is the current UTC calendar day, not a rolling 24 hours.**
    The range controls are day-granularity (`<input type="date">` plus the API's
    `from`/`to` day semantics), so a rolling window has nowhere to live in the
    pinned hash. The label is the operator's mental model; the value is one UTC day.
16. **Ranges are named in UTC, not the viewer's local day.** `from`/`to` are UTC
    days to match the server's `AT TIME ZONE 'UTC'` bucketing (D7) and the axis
    labels. An operator at UTC−8 asking for "today" therefore gets a window that
    started yesterday afternoon locally. The alternative — naming the local day —
    makes the API clip the newest hours of data they can already see, which is the
    worse lie; instead every timestamp column says UTC in its header.
17. **The status filter is populated from `/facets`' `status_codes`, not a written
    list.** The API contract has no status-facet endpoint, but the codes have to
    come from somewhere real — a hand-authored 200/429/500 list would filter to an
    empty table the first time a deployment emits 503 and read as a bug. The
    dropdown presents the actual `GROUP BY` result under 2xx/4xx/5xx optgroups; the
    hash value and the SQL predicate stay an exact integer (D8).
18. **Facets and status codes are cached per range (LRU 8); the requests table never
    is.** Auto-refresh has to repaint the live views, so caching the table would make
    the control a no-op. Facets move on a much slower clock than request rows, so
    re-fetching them on every 15 s tick is the waste, not the cache.
19. **Bars are capped at 24px wide.** Phase 1's ported chart allowed 34px — the
    maximum the *column count* implied, which violates the ≤ 24px mark ceiling the
    SPEC's own visual rules state. The cap now binds before the available width
    does, and the chart scales its width instead.
20. **Agent hues follow the response's own top-5-by-tokens ranking**
    (`_SQL_DAILY_AGENTS`'s `ORDER BY`/`LIMIT`, mirrored client-side by `agentOrder()`
    over `daily_agents`), not a stable identity order. A re-rank therefore moves a
    hue. The frontend cannot pin an order without an endpoint that returns the whole
    ranking, so identity rides on the legend label and the per-segment tooltip, and
    the first successful stack publishes its order so no *other* chart on the page
    disagrees with it during that render.
21. **The request detail panel is ephemeral UI state, not a hash key.** The pinned
    hash shape (`#/<view>?from&to&agent&model&provider&status&sort&order&page`) has
    no slot that can address one row, and adding one would break the contract for
    every existing link. Cost: the panel does not survive a reload or a copied URL.
    It is a peek, not a destination — the tables below the charts are the drill-down.
22. **Sort headers cycle select-then-flip, with a type-aware default direction.** The
    API's whitelist has a single `tokens` key for two token columns, so clicking
    either sends `sort=tokens` and the arrow marks which column is live. The first
    click on a header takes a direction from the column's type (text ascending,
    numbers descending) rather than always descending.
23. **p50/p95 borrow two Set2 steps** (`#8da0cb` / `#fc8d62`) instead of a diverging
    or sequential pair. Two percentiles of one measure are two named series, not a
    magnitude ramp, so the categorical rule applies and they are never drawn on a
    second axis. Known cost, called out here rather than hidden: those two hues mean
    something else on the agent and provider charts. A hue never means two things
    within one chart, and each chart's legend names its own series.
24. **The client mirrors D7's range clamp and inverted-pair swap.** `parseHash()`
    swaps a reversed `from`/`to` and clamps to 366 days *before* any request is
    built, so the URL in the address bar is the range actually queried. The server
    still does both — it cannot trust a client — which means the two implementations
    have to agree on the bound; they use the same 366 days, and the test asserts that
    a range past 14 days asks for `bucket=day`.
25. **Axis labels trust the response's echoed `bucket`, not the requested one.** The
    server degrades `hour` → `day` past 14 days and echoes what it used. Labeling
    from the request would print "03:00" under a full day of data — a chart that
    lies in a way no one catches by looking at it.
26. **`bindActivate` adds Enter/Space activation to chart marks, table rows and
    sortable headers.** The mark specs require oversized hit targets and per-mark
    tooltips; a hover-only layer is unreachable without a mouse, so every target that
    paints a tooltip or performs a drill-down is focusable. Sortable `th`s keep the
    `th` role and use `tabindex` + `aria-sort` rather than becoming buttons.
