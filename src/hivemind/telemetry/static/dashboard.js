/* Telemetry dashboard — hash-routed views over the /_telemetry/* endpoints.
 *
 * State lives in the hash (#/<view>?from&to&agent&model&provider&status&sort&
 * order&page) so every drill-down is a copyable link and the back button walks
 * the analysis trail the operator actually took. Nothing is kept in a JS
 * variable that the URL does not also carry: a render is a pure function of
 * (hash, server). The one exception is the request detail panel, which is
 * ephemeral by design and closes on navigation.
 *
 * Rules this file keeps (docs/token-ledger-analytics.md):
 *   - every dynamic string goes in through textContent. Agent hashes and model
 *     names come from request bodies, so they are untrusted input that happens
 *     to look like labels; there is no innerHTML anywhere in this file.
 *   - a failed fetch (or the ledger's own {"error": "telemetry unavailable"})
 *     renders the "Telemetry unavailable" card inside the current view. It
 *     never becomes a JS error, and the views above it stay interactive.
 *   - auto-refresh is opt-in, default OFF, persisted in localStorage under
 *     hivemindTelemetryAutoRefresh behind try/catch helpers — a page that
 *     cannot store the preference must still work (private mode, quotas).
 *   - in-flight fetches never overlap: one `inflight` latch guards both the
 *     refresh timer and navigation.
 *   - no external assets, no network beyond this origin, no build step. The
 *     only URLs in this file are same-origin paths.
 *
 * Cost is deliberately absent from every view: pricing is not maintained, and
 * an unmaintained price rendered confidently is worse than no price at all.
 */

"use strict";

(function () {
  const C = window.HiveCharts;
  const el = C ? C.el : null;
  const dataTable = C ? C.dataTable : null;

  /* ============ constants ============ */
  const VIEWS = ["overview", "agents", "models", "requests"];
  // Sort keys the ledger whitelists for /_telemetry/requests. Anything else is
  // dropped server-side, so the page does not offer what the API cannot do.
  const SORT_KEYS = ["ts", "tokens", "latency", "status", "agent", "model"];
  // First click on a text column reads better ascending; a timestamp reads
  // better descending ("what just happened").
  const TEXT_SORTS = ["agent", "model"];
  const PAGE_SIZE = 100;
  const LS_KEY = "hivemindTelemetryAutoRefresh";
  // Opt-in only. Long enough that an idle tab is not chatty, short enough that
  // an operator watching a running fleet sees movement without clicking.
  const REFRESH_MS = 15000;
  const DAY_MS = 86400000;

  if (!C) {
    // charts.js did not load (or was blocked). Say so in the same words, with
    // the barest DOM calls available — the page must not fail silently.
    const main = document.getElementById("main");
    if (main) {
      const box = document.createElement("div");
      box.className = "error";
      const h = document.createElement("h2");
      h.textContent = "Telemetry unavailable";
      const p = document.createElement("p");
      p.textContent = "The dashboard's chart library did not load. The raw data is still at /_telemetry/data (fail-open).";
      box.appendChild(h);
      box.appendChild(p);
      while (main.firstChild) main.removeChild(main.firstChild);
      main.appendChild(box);
    }
    return;
  }

  /* ============ persisted preference (private-mode safe) ============ */
  function readLS(key) {
    try {
      return window.localStorage.getItem(key);
    } catch (err) {
      return null; // private mode / storage disabled: fall back to the default
    }
  }

  function writeLS(key, value) {
    try {
      window.localStorage.setItem(key, value);
    } catch (err) {
      /* the preference simply does not persist; the page keeps working */
    }
  }

  let autoRefresh = readLS(LS_KEY) === "1";
  let inflight = false;
  let stamp = "";
  let state = null;

  /* ============ UTC day helpers ============
   * The display range is always named in UTC days, exactly like the SQL
   * bucketing (SPEC D7): a range the server interprets one way and the axis
   * labels another is how a dashboard starts lying quietly. That is also why
   * the range row says "UTC" out loud.
   */
  function nowIsoDay() {
    return new Date().toISOString().slice(0, 10);
  }

  function validDay(s) {
    return typeof s === "string" && /^\d{4}-\d{2}-\d{2}$/.test(s);
  }

  function dayToUTC(iso) {
    const p = String(iso).split("-");
    return Date.UTC(Number(p[0]), Number(p[1]) - 1, Number(p[2]));
  }

  function addDays(iso, delta) {
    return new Date(dayToUTC(iso) + delta * DAY_MS).toISOString().slice(0, 10);
  }

  function dayDiff(from, to) {
    return Math.round((dayToUTC(to) - dayToUTC(from)) / DAY_MS);
  }

  // The bucket the server will actually use for a range: hour reads as a
  // readable column count up to two weeks, and the server degrades hour->day
  // past MAX_HOUR_BUCKET_DAYS anyway. Matching the server here means the first
  // request already asks for the right thing.
  function autoBucket(st) {
    return dayDiff(st.from, st.to) <= 14 ? "hour" : "day";
  }

  function formatStamp(iso) {
    const s = String(iso || "");
    return s.length >= 16 ? s.slice(0, 16).replace("T", " ") + " UTC" : s;
  }

  // The header already says UTC, so the table drops the suffix and keeps the
  // seconds (two requests a minute apart in the same minute are the ones you
  // open the table to tell apart). Slice, never Date(): the value is the
  // server's UTC string and re-parsing it locally can shift it a day.
  function tableStamp(iso) {
    const s = String(iso == null ? "" : iso);
    if (s.length >= 19) return s.slice(0, 10) + " " + s.slice(11, 19);
    return s.length >= 16 ? s.slice(0, 16).replace("T", " ") : s;
  }

  /* ============ hash state ============
   * parseHash and buildHash are the only two places that know the URL shape;
   * every navigation goes through navigate() -> buildHash, so the back button
   * and a copied link are the same mechanism.
   */
  function hashValue(params, key) {
    const v = params.get(key);
    return v === null || v === "" ? null : v;
  }

  function parseHash(hash) {
    const raw = String(hash === undefined ? window.location.hash : hash).replace(/^#/, "");
    const cut = raw.indexOf("?");
    const name = (cut >= 0 ? raw.slice(0, cut) : raw).replace(/^\/+|\/+$/g, "");
    const params = new URLSearchParams(cut >= 0 ? raw.slice(cut + 1) : "");

    let to = validDay(params.get("to")) ? params.get("to") : nowIsoDay();
    let from = validDay(params.get("from")) ? params.get("from") : addDays(to, -13);
    if (dayDiff(from, to) < 0) {
      const swap = from; // D7: inverted ranges swap rather than span negative
      from = to;
      to = swap;
    }
    if (dayDiff(from, to) > 366) from = addDays(to, -366); // same 366-day cap as D7

    const status = parseInt(params.get("status"), 10);
    const sort = SORT_KEYS.indexOf(params.get("sort")) >= 0 ? params.get("sort") : "ts";
    return {
      view: VIEWS.indexOf(name) >= 0 ? name : "overview",
      from: from,
      to: to,
      agent: hashValue(params, "agent"),
      model: hashValue(params, "model"),
      provider: hashValue(params, "provider"),
      // 100..599 is the only range the ledger will accept as an HTTP status;
      // anything else is dropped rather than sent to be dropped again.
      status: Number.isInteger(status) && status >= 100 && status <= 599 ? status : null,
      sort: sort,
      order: params.get("order") === "asc" ? "asc" : "desc",
      page: Math.max(1, parseInt(params.get("page"), 10) || 1),
    };
  }

  function buildHash(st) {
    const parts = ["from=" + st.from, "to=" + st.to];
    const add = (key, value) => {
      if (value !== null && value !== undefined && value !== "") {
        parts.push(key + "=" + encodeURIComponent(String(value)));
      }
    };
    add("agent", st.agent);
    add("model", st.model);
    add("provider", st.provider);
    if (st.status !== null && st.status !== undefined) parts.push("status=" + st.status);
    if (st.sort !== "ts") parts.push("sort=" + st.sort);
    if (st.order !== "desc") parts.push("order=" + st.order);
    if (st.page > 1) parts.push("page=" + st.page);
    return "#/" + st.view + "?" + parts.join("&");
  }

  // A patch only has to be *hash-expressible*: it is written to the URL and
  // then read back through parseHash, which is what types it (a status code
  // arrives from a <select> as "500" and comes back as the integer 500). Every
  // caller therefore holds state that came from parseHash, and there is exactly
  // one place that decides what a valid state is.
  function navigate(next) {
    const hash = buildHash(next);
    if (hash === window.location.hash) {
      render(); // same URL fires no hashchange, and the click still meant "go"
      return;
    }
    window.location.hash = hash;
  }

  function apply(patch) {
    navigate(Object.assign({}, state, patch));
  }

  // Which of the current filters survive a trip to another view. Filters that
  // a view cannot act on are dropped rather than carried invisibly: a hidden
  // filter that still narrows the data is the worst kind of state.
  function stateForView(st, view) {
    const next = { view: view, from: st.from, to: st.to, sort: "ts", order: "desc", page: 1 };
    if (view === "agents") next.agent = st.agent;
    if (view === "models") next.model = st.model;
    if (view === "requests") {
      next.agent = st.agent;
      next.model = st.model;
      next.provider = st.provider;
      next.status = st.status;
    }
    return next;
  }

  /* ============ API ============ */
  function rangeParams(st) {
    const p = new URLSearchParams();
    p.set("from", st.from);
    p.set("to", st.to);
    return p;
  }

  function filterParams(st) {
    const p = rangeParams(st);
    if (st.agent) p.set("agent_hash", st.agent);
    if (st.model) p.set("model", st.model);
    if (st.provider) p.set("provider", st.provider);
    if (st.status !== null && st.status !== undefined) p.set("status", String(st.status));
    return p;
  }

  async function apiGet(path, params) {
    const query = params && params.toString ? params.toString() : "";
    // Same-origin, relative, GET — the only kind of request this page makes.
    const resp = await fetch(path + (query ? "?" + query : ""), { headers: { accept: "application/json" } });
    if (!resp.ok) throw new Error("the ledger answered HTTP " + resp.status);
    const payload = await resp.json();
    // D4: a down or unconfigured ledger is a 200 with an error marker, not a
    // 5xx. Both shapes land here as "unavailable".
    if (payload && payload.error) throw new Error(payload.error);
    return payload;
  }

  /* --- small per-range caches -------------------------------------------
   * Only two things are cached, and both are "slow-moving lists", never data
   * that a refresh should repaint: the facet lists (the SPEC asks for exactly
   * this) and the status codes behind the requests filter (see the deviation
   * note in docs/token-ledger-analytics.md). Keyed by range so a range change
   * refetches, and capped so a long click-through cannot grow without bound.
   */
  const CACHE_LIMIT = 8;
  const facetCache = new Map();
  const statusCache = new Map();

  function cachePut(cache, key, value) {
    cache.set(key, value);
    while (cache.size > CACHE_LIMIT) cache.delete(cache.keys().next().value);
  }

  function rangeKey(st) {
    return st.from + "|" + st.to;
  }

  async function loadFacets(st) {
    const key = rangeKey(st);
    if (facetCache.has(key)) return facetCache.get(key);
    const payload = await apiGet("/_telemetry/facets", rangeParams(st));
    cachePut(facetCache, key, payload);
    return payload;
  }

  // The status dropdown needs the codes that actually occurred. There is no
  // status-facet endpoint, so the list comes from the overview's status_codes
  // (a real GROUP BY over the window, busiest first, capped at 12) — one cached
  // call per range, never on the polling path.
  async function loadStatusCodes(st) {
    const key = rangeKey(st);
    if (statusCache.has(key)) return statusCache.get(key);
    const payload = await apiGet("/_telemetry/data", rangeParams(st));
    const codes = [];
    for (const row of payload.status_codes || []) {
      const code = parseInt(row.status, 10);
      if (Number.isInteger(code) && codes.indexOf(code) < 0) codes.push(code);
    }
    codes.sort((a, b) => a - b);
    cachePut(statusCache, key, codes);
    return codes;
  }

  /* ============ header chrome (built once, synced per render) ============
   * The controls are built a single time: rebuilding them on every render
   * would drop focus out of the date input mid-edit and reset the dropdowns
   * under the pointer. Only values and classes are synced.
   */
  const PRESETS = [
    ["24h", 1],
    ["7d", 7],
    ["14d", 14],
    ["30d", 30],
    ["90d", 90],
  ];

  let controls = null;

  function buildChrome() {
    const header = document.querySelector("header");
    const host = document.getElementById("controls");
    if (!header || !host || host.dataset.built === "1") return;

    // View tabs are real links: middle-click and "copy link address" work
    // without a line of JS, and hrefs are refreshed per render.
    const nav = el("nav", "tabs");
    nav.setAttribute("aria-label", "Views");
    for (const view of VIEWS) {
      const a = el("a", "tab", view.charAt(0).toUpperCase() + view.slice(1));
      a.href = "#/" + view;
      a.dataset.view = view;
      nav.appendChild(a);
    }
    header.insertBefore(nav, host);

    const range = el("span", "range");
    const fromLabel = el("label", null, "From");
    fromLabel.setAttribute("for", "range-from");
    const toLabel = el("label", null, "To");
    toLabel.setAttribute("for", "range-to");
    const fromInput = document.createElement("input");
    fromInput.type = "date";
    fromInput.id = "range-from";
    const toInput = document.createElement("input");
    toInput.type = "date";
    toInput.id = "range-to";
    range.appendChild(fromLabel);
    range.appendChild(fromInput);
    range.appendChild(toLabel);
    range.appendChild(toInput);

    const chips = el("span", "chips");
    for (const [label, days] of PRESETS) {
      const chip = el("button", "chip", label);
      chip.type = "button";
      chip.dataset.days = String(days);
      chip.addEventListener("click", () => {
        const to = nowIsoDay();
        apply({ from: addDays(to, -(days - 1)), to: to, page: 1 });
      });
      chips.appendChild(chip);
    }
    range.appendChild(chips);

    const onChange = () => {
      const next = { from: fromInput.value, to: toInput.value };
      if (!validDay(next.from) || !validDay(next.to)) return; // mid-edit / cleared
      let from = next.from;
      let to = next.to;
      if (dayDiff(from, to) < 0) {
        const swap = from;
        from = to;
        to = swap;
      }
      apply({ from: from, to: to, page: 1 });
    };
    fromInput.addEventListener("change", onChange);
    toInput.addEventListener("change", onChange);

    const toggle = el("label", "toggle");
    const box = document.createElement("input");
    box.type = "checkbox";
    box.id = "autorefresh";
    box.checked = autoRefresh;
    box.title = "Reload the current view every " + REFRESH_MS / 1000 + "s while this tab is visible";
    box.addEventListener("change", () => {
      autoRefresh = box.checked;
      writeLS(LS_KEY, autoRefresh ? "1" : "0");
    });
    toggle.appendChild(box);
    toggle.appendChild(document.createTextNode("Auto-refresh"));

    const csv = el("a", null, "CSV");
    csv.id = "export-csv";
    csv.title = "Download the current range and filters as CSV";
    const jsonl = el("a", null, "JSONL");
    jsonl.id = "export-jsonl";
    jsonl.title = "Download the current range and filters as JSONL";

    const updated = el("span", null, "");
    updated.id = "updated";

    host.appendChild(range);
    host.appendChild(toggle);
    host.appendChild(csv);
    host.appendChild(jsonl);
    host.appendChild(updated);
    host.dataset.built = "1";
    controls = {
      host: host,
      nav: nav,
      from: fromInput,
      to: toInput,
      chips: chips,
      box: box,
      csv: csv,
      jsonl: jsonl,
      updated: updated,
    };
  }

  function activePreset(st) {
    const today = nowIsoDay();
    if (st.to !== today) return null;
    for (const [, days] of PRESETS) {
      if (st.from === addDays(today, -(days - 1))) return days;
    }
    return null;
  }

  function syncChrome(st) {
    if (!controls) return;
    // Never stomp on a field the operator is typing into.
    if (document.activeElement !== controls.from) controls.from.value = st.from;
    if (document.activeElement !== controls.to) controls.to.value = st.to;
    const active = activePreset(st);
    for (const chip of controls.chips.children) {
      chip.classList.toggle("on", Number(chip.dataset.days) === active);
    }
    if (controls.box.checked !== autoRefresh) controls.box.checked = autoRefresh;
    const query = filterParams(st).toString();
    controls.csv.href = "/_telemetry/export.csv?" + query;
    controls.jsonl.href = "/_telemetry/export.jsonl?" + query;
    controls.updated.textContent = stamp ? "updated " + stamp : "";
    for (const tab of controls.nav.children) {
      const view = tab.dataset.view;
      tab.href = buildHash(stateForView(st, view));
      tab.classList.toggle("on", view === st.view);
      if (view === st.view) tab.setAttribute("aria-current", "page");
      else tab.removeAttribute("aria-current");
    }
  }

  /* ============ small view helpers ============ */
  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function viewCard(title, sub) {
    const card = el("section", "card");
    card.appendChild(el("h2", null, title));
    if (sub) card.appendChild(el("p", "sub", sub));
    return card;
  }

  function tilesInto(holder, defs) {
    const tiles = el("div", "tiles");
    for (const d of defs) {
      const tile = el("div", "tile");
      tile.appendChild(el("div", "label", d.label));
      tile.appendChild(el("div", "value", d.value));
      if (d.title) tile.title = d.title;
      tiles.appendChild(tile);
    }
    holder.appendChild(tiles);
    return tiles;
  }

  function totalsTiles(holder, t) {
    tilesInto(holder, [
      { label: "Requests", value: C.fmtInt(t.requests) },
      { label: "Tokens in", value: C.fmtInt(t.tokens_in) },
      { label: "Tokens out", value: C.fmtInt(t.tokens_out) },
      { label: "Cache reads", value: C.fmtTokens(t.cache_read) },
      {
        label: "Error rate",
        value: C.fmtPct(t.error_rate),
        title: C.fmtInt(t.errors) + " of " + C.fmtInt(t.requests) + " requests returned status >= 400",
      },
    ]);
  }

  function renderUnavailable(main, detail) {
    const box = el("div", "error");
    box.appendChild(el("h2", null, "Telemetry unavailable"));
    box.appendChild(
      el(
        "p",
        null,
        (detail ? detail + " " : "") +
          "The ledger is unreachable or disabled (no --telemetry-dsn). " +
          "The proxy keeps serving — only telemetry is affected (fail-open).",
      ),
    );
    main.appendChild(box);
  }

  function empty(text) {
    return el("p", "empty", text);
  }

  // Every drill-down link on the page is a real anchor so it can be copied,
  // opened in a new tab, or followed with the keyboard.
  function drillLink(label, target) {
    const a = el("a", null, label);
    a.href = buildHash(target);
    return a;
  }

  /* ============ dropdown helpers ============ */
  function selectBox(id, options, current, onChange) {
    const sel = document.createElement("select");
    sel.id = id;
    for (const opt of options) {
      const node = document.createElement("option");
      node.value = opt.value;
      node.textContent = opt.label; // untrusted values: textContent, always
      if (opt.title) node.title = opt.title;
      if (opt.value === current) node.selected = true;
      sel.appendChild(node);
    }
    sel.addEventListener("change", () => onChange(sel.value === "" ? null : sel.value));
    return sel;
  }

  /* ============ detail panel (metadata only) ============ */
  let panel = null;

  function panelClose() {
    if (panel) panel.hidden = true;
    C.tipHide();
  }

  function ensurePanel() {
    if (panel) return panel;
    panel = el("aside", "panel");
    panel.hidden = true;
    panel.setAttribute("aria-label", "Request detail");
    document.body.appendChild(panel);
    document.addEventListener("keydown", (ev) => {
      if (ev.key === "Escape") panelClose();
    });
    return panel;
  }

  function openPanel(row) {
    const box = ensurePanel();
    clear(box);

    const head = el("div", "panel-head");
    head.appendChild(el("h3", null, "Request #" + C.fmtInt(row.id)));
    const close = el("button", "close", "×");
    close.type = "button";
    close.setAttribute("aria-label", "Close detail");
    close.addEventListener("click", panelClose);
    head.appendChild(close);
    box.appendChild(head);

    // One row per stored column, in the ledger's own order (_REQUEST_COLUMNS).
    // This list is the whole schema on purpose: "metadata only" is a claim the
    // operator can check, and there is no prompt column to leak.
    const fields = [
      ["id", C.fmtInt(row.id), false],
      ["ts", row.ts == null ? "—" : String(row.ts), true],
      ["agent_hash", row.agent_hash == null ? "—" : String(row.agent_hash), true],
      ["provider", row.provider == null ? "—" : String(row.provider), false],
      ["model", row.model == null ? "—" : String(row.model), true],
      ["tokens_in", C.fmtInt(row.tokens_in), false],
      ["tokens_out", C.fmtInt(row.tokens_out), false],
      ["cache_read", C.fmtInt(row.cache_read), false],
      ["cache_write", C.fmtInt(row.cache_write), false],
      ["latency_ms", C.fmtMs(row.latency_ms), false],
      ["status", row.status == null ? "—" : String(row.status), false],
    ];
    const list = el("dl", "kv");
    for (const [key, value, mono] of fields) {
      list.appendChild(el("dt", null, key));
      list.appendChild(el("dd", mono ? "mono" : null, value));
    }
    box.appendChild(list);

    // What the numbers are, and what they are not.
    const input = (row.cache_read || 0) + (row.tokens_in || 0);
    const note = el("p", "note");
    note.appendChild(
      document.createTextNode(
        "Cache hit " + (input > 0 ? C.fmtPct((row.cache_read || 0) / input) : "—") + " (reads / reads + input). ",
      ),
    );
    note.appendChild(
      document.createTextNode(
        "metadata only — no prompt content is ever stored. Rows are the token ledger's own " +
          "columns; the schema has nowhere to put a message body.",
      ),
    );
    box.appendChild(note);
    box.hidden = false;
  }

  /* =====================================================================
   * #/overview
   * =================================================================== */
  // Fixed hue identity, computed from the payload before any chart is built.
  // Agent hues follow the stack's own ranking (the SQL keeps the top five by
  // window tokens and folds the rest into "Other"), so the five hues land on
  // the five drawn series. Consequence worth knowing: when the ranking itself
  // changes, hues move with it — which is why the legend names every segment
  // and the tooltip prints the raw hash. Identity is never carried by hue
  // alone anywhere on this page.
  function agentOrder(daily) {
    const totals = new Map();
    for (const r of daily) {
      if (!r.agent_hash || r.agent_hash === "Other") continue;
      totals.set(r.agent_hash, (totals.get(r.agent_hash) || 0) + (r.tokens_in || 0) + (r.tokens_out || 0));
    }
    return Array.from(totals.keys()).sort(
      (a, b) => totals.get(b) - totals.get(a) || (a < b ? -1 : a > b ? 1 : 0),
    );
  }

  function providerOrder(payload) {
    const names = [];
    for (const list of [payload.top_models, payload.latency, payload.latency_models]) {
      for (const r of list || []) {
        if (r.provider && names.indexOf(r.provider) < 0) names.push(r.provider);
      }
    }
    return names.sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
  }

  function renderOverview(payload, st, main) {
    const totals = payload.totals || {};
    const daily = payload.daily_agents || [];
    const models = payload.top_models || [];
    const statuses = payload.status_codes || [];
    const latency = payload.latency || [];
    const latencyModels = payload.latency_models || [];
    const agents = payload.agents || [];

    C.setEntityOrder("agent", agentOrder(daily));
    C.setEntityOrder("provider", providerOrder(payload));

    totalsTiles(main, totals);

    if (!totals.requests && !daily.length && !models.length) {
      const card = viewCard("No telemetry yet");
      card.appendChild(empty("No telemetry in this window yet. Widen the range above, or check that the proxy is writing to a ledger (--telemetry-dsn)."));
      main.appendChild(card);
      return;
    }

    // --- status codes: a category, so one muted hue and no legend ---------
    if (statuses.length) {
      const card = viewCard("Status codes", "Every response status seen in this window. Status is a category, not an entity — it never takes a series hue.");
      const total = statuses.reduce((acc, r) => acc + (r.requests || 0), 0);
      C.bars(card, "Responses by status", statuses, {
        labelKey: "status",
        valueKey: "requests",
        labelW: 120,
        rowLabelClass: "status-label",
        valueFmt: C.fmtInt,
        colorFn: () => C.STATUS_COLOR,
        tipRows: (r) => [
          { label: "requests", value: C.fmtInt(r.requests) },
          { label: "share", value: total > 0 ? C.fmtPct((r.requests || 0) / total) : "—" },
        ],
        onRowClick: (r) => navigate(Object.assign({}, st, { view: "requests", status: r.status, page: 1 })),
      });
      dataTable(
        card,
        "Same data as the chart above. Click a bar to open that status in the requests view.",
        ["Status", "Requests", "Share"],
        statuses.map((r) => [
          { text: String(r.status), cls: "agent-hash" },
          { text: C.fmtInt(r.requests) },
          { text: total > 0 ? C.fmtPct((r.requests || 0) / total) : "—" },
        ]),
      );
      main.appendChild(card);
    }

    // --- daily per-agent stack -------------------------------------------
    const dailyCard = viewCard(
      "Daily usage per agent",
      "Tokens per day, stacked by agent (top 5 by window tokens; the rest fold into “Other”). " +
        "The provider dimension is not charted — mid-pipeline upstreams all detect as one profile. " +
        "Click a segment to open that agent's requests.",
    );
    if (daily.length) {
      C.stackedDaily(dailyCard, "Tokens per day", daily, "day", {
        onSegmentClick: (rawHash) => navigate(Object.assign({}, st, { view: "requests", agent: rawHash, page: 1 })),
      });
      const rows = daily.slice().sort((a, b) => (b.day < a.day ? -1 : b.day > a.day ? 1 : 0));
      dataTable(
        dailyCard,
        "Same data as the chart above.",
        ["Day", "Agent (hash)", "Requests", "Tokens in", "Tokens out", "Errors"],
        rows.map((r) => [
          r.day,
          { text: r.agent_hash, cls: "agent-hash", title: "rate-limit bucket hash (agent identity)" },
          C.fmtInt(r.requests),
          C.fmtInt(r.tokens_in),
          C.fmtInt(r.tokens_out),
          C.fmtInt(r.errors),
        ]),
      );
    } else {
      dailyCard.appendChild(empty("No telemetry in this window yet."));
    }
    main.appendChild(dailyCard);

    // --- top models -------------------------------------------------------
    const modelCard = viewCard(
      "Top models by tokens",
      "Ranked by tokens consumed, not cost — cache reads are the actionable signal for multi-agent " +
        "runs (a low hit rate means repeated full-context sends). Click a row to open that model.",
    );
    if (models.length) {
      const rows = models.map((m) =>
        Object.assign({}, m, { tokens: (m.tokens_in || 0) + (m.tokens_out || 0) }),
      );
      C.bars(modelCard, "Tokens by model", rows, {
        labelKey: "model",
        valueKey: "tokens",
        colorKey: "provider",
        colorFn: (name) => C.providerColor(name),
        legendName: (name) => C.foldedName(name),
        tipRows: (m) => [
          { swatch: C.providerColor(m.provider), label: "provider", value: m.provider == null ? "—" : String(m.provider) },
          { label: "requests", value: C.fmtInt(m.requests) },
          { label: "tokens in/out", value: C.fmtInt(m.tokens_in) + " / " + C.fmtInt(m.tokens_out) },
          { label: "cache reads", value: C.fmtInt(m.cache_read) },
          { label: "cache hit", value: C.fmtPct(C.hitRate(m)) },
        ],
        onRowClick: (m) => navigate(Object.assign({}, st, { view: "models", model: m.model, page: 1 })),
      });
      dataTable(
        modelCard,
        "Same data as the chart above.",
        ["Model", "Provider", "Requests", "Tokens in", "Tokens out", "Cache reads", "Cache hit"],
        rows.map((m) => [
          { text: m.model, cls: "agent-hash" },
          m.provider,
          C.fmtInt(m.requests),
          C.fmtInt(m.tokens_in),
          C.fmtInt(m.tokens_out),
          C.fmtInt(m.cache_read),
          C.fmtPct(C.hitRate(m)),
        ]),
      );
    } else {
      modelCard.appendChild(empty("No requests in this window."));
    }
    main.appendChild(modelCard);

    // --- latency ----------------------------------------------------------
    const latCard = viewCard("Latency by provider", "Response latency p50/p95 — two series, one scale.");
    if (latency.length) {
      C.latencyChart(latCard, latency, { nameKey: "provider" });
      dataTable(
        latCard,
        "Same data as the chart above.",
        ["Provider", "Requests", "p50", "p95"],
        latency.map((l) => [l.provider, C.fmtInt(l.requests), C.fmtMs(l.p50_ms), C.fmtMs(l.p95_ms)]),
      );
    } else {
      latCard.appendChild(empty("No latency rows in this window."));
    }
    main.appendChild(latCard);

    const latModelCard = viewCard(
      "Latency by model",
      "The same two percentiles per model, slowest p95 first (top 15 by p95).",
    );
    if (latencyModels.length) {
      C.latencyChart(latModelCard, latencyModels, { nameKey: "model" });
      dataTable(
        latModelCard,
        "Same data as the chart above.",
        ["Model", "Provider", "Requests", "p50", "p95"],
        latencyModels.map((l) => [
          { text: l.model, cls: "agent-hash" },
          l.provider,
          C.fmtInt(l.requests),
          C.fmtMs(l.p50_ms),
          C.fmtMs(l.p95_ms),
        ]),
      );
    } else {
      latModelCard.appendChild(empty("No latency rows in this window."));
    }
    main.appendChild(latModelCard);

    // --- per-agent totals -------------------------------------------------
    const agentCard = viewCard(
      "Per-agent totals",
      "Agent hashes are rate-limit bucket labels only — identities are never stored. " +
        "Cache hit = tokens served from cache over total input.",
    );
    if (agents.length) {
      const rows = agents.map((a) => ({
        cells: [
          { text: a.agent_hash, cls: "agent-hash", title: "rate-limit bucket hash (agent identity)" },
          C.fmtInt(a.requests),
          C.fmtInt(a.tokens_in),
          C.fmtInt(a.tokens_out),
          C.fmtInt(a.cache_read),
          C.fmtPct(C.hitRate(a)),
          {
            text: C.fmtPct(a.error_rate),
            title: C.fmtInt(a.errors) + " of " + C.fmtInt(a.requests) + " requests returned status >= 400",
          },
        ],
        onClick: () => navigate(Object.assign({}, st, { view: "requests", agent: a.agent_hash, page: 1 })),
        label: "Open requests for agent " + a.agent_hash,
      }));
      dataTable(
        agentCard,
        "One row per agent hash. Select a row to open its requests.",
        ["Agent (hash)", "Requests", "Tokens in", "Tokens out", "Cache reads", "Cache hit", "Error rate"],
        rows,
      );
    } else {
      agentCard.appendChild(empty("No requests in this window."));
    }
    main.appendChild(agentCard);
  }

  /* =====================================================================
   * #/agents
   * =================================================================== */
  // The series rows ARE the per-agent window totals once summed — no second
  // aggregate query is needed to fill the summary tiles.
  function sumSeries(rows) {
    const out = { requests: 0, tokens_in: 0, tokens_out: 0, cache_read: 0, errors: 0 };
    for (const r of rows) {
      out.requests += r.requests || 0;
      out.tokens_in += r.tokens_in || 0;
      out.tokens_out += r.tokens_out || 0;
      out.cache_read += r.cache_read || 0;
      out.errors += r.errors || 0;
    }
    out.error_rate = out.requests > 0 ? out.errors / out.requests : 0;
    return out;
  }

  function seriesParams(base, filters, bucket) {
    const p = rangeParams(base);
    p.set("bucket", bucket);
    for (const key of Object.keys(filters)) {
      if (filters[key]) p.set(key, filters[key]);
    }
    return p;
  }

  // Which bucket the response actually used. The server degrades hour->day for
  // long ranges, and the response says so — labelling a day-bucketed response
  // with hour labels would print "00:00" on every tick and quietly lie about
  // the resolution.
  function effectiveBucket(payload, requested) {
    return payload.bucket === "hour" || payload.bucket === "day" ? payload.bucket : requested;
  }

  function seriesSection(card, title, rows, pick, bucket, valueFmt, valueLabel) {
    C.singleSeries(card, title, rows, pick, { bucket: bucket, valueFmt: valueFmt, valueLabel: valueLabel });
    dataTable(
      card,
      "Same data as the chart above.",
      ["Bucket (UTC)", "Requests", "Tokens in", "Tokens out", "Cache reads", "Errors"],
      rows.map((r) => [
        { text: r.bucket_start },
        C.fmtInt(r.requests),
        C.fmtInt(r.tokens_in),
        C.fmtInt(r.tokens_out),
        C.fmtInt(r.cache_read),
        C.fmtInt(r.errors),
      ]),
    );
  }

  async function renderAgents(st, main) {
    const facets = await loadFacets(st);
    const card = viewCard("Agent activity");
    const picker = el("div", "picker");
    const label = el("label", null, "Agent");
    label.setAttribute("for", "agent-picker");
    picker.appendChild(label);
    const options = [{ value: "", label: "— pick an agent —" }];
    for (const a of facets.agents || []) {
      options.push({
        value: a.agent_hash,
        label: C.shortHash(a.agent_hash) + " — " + C.fmtInt(a.requests) + " requests",
        title: a.agent_hash,
      });
    }
    picker.appendChild(
      selectBox("agent-picker", options, st.agent || "", (value) => apply({ agent: value, page: 1 })),
    );
    if (st.agent) {
      picker.appendChild(drillLink("View requests →", Object.assign({}, st, { view: "requests", page: 1 })));
    }
    card.appendChild(picker);
    main.appendChild(card);

    if (!st.agent) {
      card.appendChild(empty("Pick an agent to see its activity over time. The list is busiest-first for the selected range."));
      return;
    }

    const sub = el("p", "sub");
    sub.appendChild(document.createTextNode("Agent "));
    const hashEl = el("span", "agent-hash", st.agent);
    sub.appendChild(hashEl);
    sub.appendChild(
      document.createTextNode(
        " — a rate-limit bucket label, never an identity. Granularity follows the range: hourly up to two weeks, daily beyond it.",
      ),
    );
    card.appendChild(sub);

    const requested = autoBucket(st);
    const payload = await apiGet("/_telemetry/series", seriesParams(st, { agent_hash: st.agent }, requested));
    const rows = payload.rows || [];
    const bucket = effectiveBucket(payload, requested);

    if (!rows.length) {
      card.appendChild(empty("No telemetry in this window yet."));
      return;
    }

    const totals = sumSeries(rows);
    totalsTiles(card, totals);
    seriesSection(card, "Requests per " + bucket, rows, (r) => r.requests, bucket, C.fmtInt, "requests");
    seriesSection(
      card,
      "Tokens per " + bucket,
      rows,
      (r) => (r.tokens_in || 0) + (r.tokens_out || 0),
      bucket,
      C.fmtTokens,
      "tokens",
    );
  }

  /* =====================================================================
   * #/models
   * =================================================================== */
  async function renderModels(st, main) {
    const payload = await apiGet("/_telemetry/data", rangeParams(st));
    const models = payload.top_models || [];
    const latencyModels = payload.latency_models || [];

    C.setEntityOrder("provider", providerOrder(payload));

    const card = viewCard("Models", "Tokens, cache reuse and latency per model. Click a model to see its activity over time.");
    // The picker is always present (not just once something is selected) —
    // otherwise the only way into the per-model series is clicking a bar.
    const picker = el("div", "picker");
    const label = el("label", null, "Model");
    label.setAttribute("for", "model-picker");
    picker.appendChild(label);
    const options = [{ value: "", label: "— pick a model —" }];
    for (const m of models) options.push({ value: m.model, label: m.model, title: m.model });
    if (st.model && !models.some((m) => m.model === st.model)) {
      // A link into a model the current window does not rank must still render
      // — and must still be escapable.
      options.push({ value: st.model, label: st.model, title: st.model });
    }
    picker.appendChild(selectBox("model-picker", options, st.model || "", (value) => apply({ model: value, page: 1 })));
    if (st.model) {
      picker.appendChild(drillLink("View requests →", Object.assign({}, st, { view: "requests", page: 1 })));
    }
    card.appendChild(picker);
    main.appendChild(card);

    // --- tokens by model (click = select, click again = clear) ------------
    if (models.length) {
      const rows = models.map((m) => Object.assign({}, m, { tokens: (m.tokens_in || 0) + (m.tokens_out || 0) }));
      C.bars(card, "Tokens by model", rows, {
        labelKey: "model",
        valueKey: "tokens",
        colorKey: "provider",
        colorFn: (name) => C.providerColor(name),
        legendName: (name) => C.foldedName(name),
        tipRows: (m) => [
          { swatch: C.providerColor(m.provider), label: "provider", value: m.provider == null ? "—" : String(m.provider) },
          { label: "requests", value: C.fmtInt(m.requests) },
          { label: "tokens in/out", value: C.fmtInt(m.tokens_in) + " / " + C.fmtInt(m.tokens_out) },
          { label: "cache reads", value: C.fmtInt(m.cache_read) },
        ],
        onRowClick: (m) => apply({ model: st.model === m.model ? null : m.model, page: 1 }),
      });
      dataTable(
        card,
        "Same data as the chart above.",
        ["Model", "Provider", "Requests", "Tokens in", "Tokens out", "Cache reads"],
        rows.map((m) => [
          { text: m.model, cls: "agent-hash" },
          m.provider,
          C.fmtInt(m.requests),
          C.fmtInt(m.tokens_in),
          C.fmtInt(m.tokens_out),
          C.fmtInt(m.cache_read),
        ]),
      );
    } else {
      card.appendChild(empty("No requests in this window."));
    }

    // --- cache-hit distribution ------------------------------------------
    const hitCard = viewCard(
      "Cache hit rate by model",
      "Cache reads over cache reads plus fresh input tokens. A model with no input tokens " +
        "has no hit rate — it shows as “—” rather than a confident 0%.",
    );
    // A null rate is an absent measurement, so it stays out of the chart (a
    // zero-height bar would read as "cached nothing") and shows up in the table.
    const hitRows = models
      .map((m) => ({ model: m.model, provider: m.provider, requests: m.requests, rate: C.hitRate(m) }))
      .filter((m) => m.rate !== null);
    if (hitRows.length) {
      C.bars(hitCard, "Cache hit rate", hitRows, {
        labelKey: "model",
        valueKey: "rate",
        colorKey: "provider",
        colorFn: (name) => C.providerColor(name),
        legendName: (name) => C.foldedName(name),
        valueFmt: C.fmtPct,
        tipRows: (m) => [
          { label: "cache hit", value: C.fmtPct(m.rate) },
          { label: "requests", value: C.fmtInt(m.requests) },
        ],
      });
    } else {
      hitCard.appendChild(empty("No cache reads recorded in this window."));
    }
    dataTable(
      hitCard,
      "Same data as the chart above, including the models with no hit rate to compute.",
      ["Model", "Provider", "Requests", "Cache hit"],
      models.map((m) => [{ text: m.model, cls: "agent-hash" }, m.provider, C.fmtInt(m.requests), C.fmtPct(C.hitRate(m))]),
    );
    main.appendChild(hitCard);

    // --- latency by model -------------------------------------------------
    const latCard = viewCard("Latency by model", "p50/p95 per model, slowest p95 first (top 15 by p95).");
    if (latencyModels.length) {
      C.latencyChart(latCard, latencyModels, { nameKey: "model" });
      dataTable(
        latCard,
        "Same data as the chart above.",
        ["Model", "Provider", "Requests", "p50", "p95"],
        latencyModels.map((l) => [
          { text: l.model, cls: "agent-hash" },
          l.provider,
          C.fmtInt(l.requests),
          C.fmtMs(l.p50_ms),
          C.fmtMs(l.p95_ms),
        ]),
      );
    } else {
      latCard.appendChild(empty("No latency rows in this window."));
    }
    main.appendChild(latCard);

    // --- the selected model's activity ------------------------------------
    if (!st.model) return;
    const requested = autoBucket(st);
    const series = await apiGet("/_telemetry/series", seriesParams(st, { model: st.model }, requested));
    const rows = series.rows || [];
    const bucket = effectiveBucket(series, requested);
    const selCard = viewCard(
      "Activity — " + st.model,
      "Hourly up to a two-week range, daily beyond it. The range controls above apply.",
    );
    if (rows.length) {
      seriesSection(selCard, "Requests per " + bucket, rows, (r) => r.requests, bucket, C.fmtInt, "requests");
      seriesSection(
        selCard,
        "Tokens per " + bucket,
        rows,
        (r) => (r.tokens_in || 0) + (r.tokens_out || 0),
        bucket,
        C.fmtTokens,
        "tokens",
      );
    } else {
      selCard.appendChild(empty("No telemetry in this window yet."));
    }
    main.appendChild(selCard);
  }

  /* =====================================================================
   * #/requests
   * =================================================================== */
  function dedupeModels(facets) {
    const seen = [];
    for (const m of facets.models || []) {
      if (!m.model || seen.some((x) => x.model === m.model)) continue;
      seen.push({ model: m.model, provider: m.provider, requests: m.requests });
    }
    return seen;
  }

  function statusOptions(st, codes) {
    const options = [{ value: "", label: "All statuses" }];
    const listed = codes.slice();
    // The selected status is always offered, even when the window no longer
    // contains it: a link from last week still has to render and still has to
    // be escapable.
    if (st.status !== null && listed.indexOf(st.status) < 0) listed.push(st.status);
    listed.sort((a, b) => a - b);
    for (const code of listed) {
      options.push({ value: String(code), label: String(code) });
    }
    return options;
  }

  function requestParams(st) {
    const p = filterParams(st);
    p.set("sort", st.sort);
    p.set("order", st.order);
    p.set("limit", String(PAGE_SIZE));
    p.set("offset", String((st.page - 1) * PAGE_SIZE));
    return p;
  }

  // Header click cycles: a new column takes a sensible first direction, the
  // current column flips, and the page resets (page 3 of the old ordering is
  // meaningless under the new one).
  function sortHandler(st, key) {
    return () => {
      const first = TEXT_SORTS.indexOf(key) >= 0 ? "asc" : "desc";
      const order = st.sort === key ? (st.order === "desc" ? "asc" : "desc") : first;
      apply({ sort: key, order: order, page: 1 });
    };
  }

  function sortableHeader(st, key, text, title) {
    const sorted = st.sort === key;
    // The arrow rides inside the header text: one th, one click target, and
    // aria-sort carries the same fact to a screen reader.
    const arrow = sorted ? (st.order === "asc" ? " ↑" : " ↓") : "";
    return {
      text: text + arrow,
      cls: "sortable" + (sorted ? " sorted" : ""),
      title: title,
      onClick: sortHandler(st, key),
      ariaSort: sorted ? (st.order === "asc" ? "ascending" : "descending") : "none",
    };
  }

  function requestTable(card, st, payload) {
    const rows = payload.rows || [];
    // Columns follow _REQUEST_COLUMNS. Only the six whitelisted sort keys get a
    // click handler: offering a sort the API would silently ignore is a lie the
    // table tells about itself.
    const headers = [
      sortableHeader(st, "ts", "Time (UTC)", "Sort by timestamp"),
      sortableHeader(st, "agent", "Agent (hash)", "Sort by agent hash"),
      { text: "Provider", title: "Not a server-side sort key" },
      sortableHeader(st, "model", "Model", "Sort by model name"),
      sortableHeader(st, "tokens", "Tokens in", "Sorts by tokens in + tokens out"),
      sortableHeader(st, "tokens", "Tokens out", "Sorts by tokens in + tokens out"),
      { text: "Cache read", title: "Not a server-side sort key" },
      sortableHeader(st, "latency", "Latency", "Sort by latency"),
      sortableHeader(st, "status", "Status", "Sort by status code"),
    ];

    dataTable(
      card,
      "One row per request. Every column is a ledger column — there is no message content in the schema. " +
        "Select a row to open its detail panel.",
      headers,
      rows.map((r) => ({
        cells: [
          {
            text: r.ts == null ? "—" : tableStamp(r.ts),
            cls: "cell-ts",
            title: r.ts == null ? "" : String(r.ts),
          },
          { text: r.agent_hash == null ? "—" : String(r.agent_hash), cls: "agent-hash" },
          r.provider == null ? "—" : String(r.provider),
          { text: r.model == null ? "—" : String(r.model), cls: "agent-hash" },
          C.fmtInt(r.tokens_in),
          C.fmtInt(r.tokens_out),
          C.fmtInt(r.cache_read),
          C.fmtMs(r.latency_ms),
          {
            text: r.status == null ? "—" : String(r.status),
            cls: r.status != null && r.status >= 400 ? "status-bad" : "status-ok",
          },
        ],
        onClick: () => openPanel(r),
        label: "Request " + r.id + " detail",
      })),
    );
  }

  function pager(card, st, payload) {
    const total = payload.total || 0;
    const offset = (st.page - 1) * PAGE_SIZE;
    const shown = (payload.rows || []).length;
    const bar = el("div", "pager");

    const prev = el("button", "btn", "← Prev");
    prev.type = "button";
    prev.disabled = st.page <= 1;
    prev.addEventListener("click", () => apply({ page: st.page - 1 }));
    const next = el("button", "btn", "Next →");
    next.type = "button";
    next.disabled = shown === 0 || offset + shown >= total;
    next.addEventListener("click", () => apply({ page: st.page + 1 }));
    bar.appendChild(prev);
    bar.appendChild(next);

    const of = el("span", "of");
    of.textContent =
      total > 0
        ? C.fmtInt(offset + 1) + "–" + C.fmtInt(offset + shown) + " of " + C.fmtInt(total)
        : "0 of 0";
    bar.appendChild(of);

    if (st.page > 1) {
      bar.appendChild(drillLink("First page", Object.assign({}, st, { page: 1 })));
    }
    card.appendChild(bar);
  }

  async function renderRequests(st, main) {
    const [facets, codes, payload] = await Promise.all([
      loadFacets(st),
      loadStatusCodes(st),
      apiGet("/_telemetry/requests", requestParams(st)),
    ]);

    const card = viewCard(
      "Requests",
      "Raw ledger rows for the selected range. Filters and sort are exact-match and whitelisted server-side.",
    );

    const filters = el("div", "filters");
    const dropdown = (labelText, id, options, current, key) => {
      const wrap = el("span");
      const label = el("label", null, labelText);
      label.setAttribute("for", id);
      wrap.appendChild(label);
      wrap.appendChild(selectBox(id, options, current || "", (value) => apply({ [key]: value, page: 1 })));
      return wrap;
    };

    const agentOptions = [{ value: "", label: "All agents" }];
    for (const a of facets.agents || []) {
      agentOptions.push({
        value: a.agent_hash,
        label: C.shortHash(a.agent_hash) + " (" + C.fmtInt(a.requests) + ")",
        title: a.agent_hash,
      });
    }
    filters.appendChild(dropdown("Agent", "f-agent", agentOptions, st.agent, "agent"));

    const modelOptions = [{ value: "", label: "All models" }];
    for (const m of dedupeModels(facets)) modelOptions.push({ value: m.model, label: m.model, title: m.model });
    filters.appendChild(dropdown("Model", "f-model", modelOptions, st.model, "model"));

    const providerOptions = [{ value: "", label: "All providers" }];
    for (const p of facets.providers || []) {
      if (p.provider === null || p.provider === undefined) continue;
      providerOptions.push({ value: p.provider, label: p.provider + " (" + C.fmtInt(p.requests) + ")" });
    }
    filters.appendChild(dropdown("Provider", "f-provider", providerOptions, st.provider, "provider"));

    const statusSel = statusOptions(st, codes);
    filters.appendChild(dropdown("Status", "f-status", statusSel, st.status === null ? "" : String(st.status), "status"));

    if (st.agent || st.model || st.provider || st.status !== null) {
      const clearAll = el("button", "btn", "Clear filters");
      clearAll.type = "button";
      clearAll.addEventListener("click", () =>
        apply({ agent: null, model: null, provider: null, status: null, page: 1 }),
      );
      filters.appendChild(clearAll);
    }
    // The same range + filters the header exports, repeated where the filters
    // are actually being edited. (Export ignores sort/limit/offset: it streams
    // the whole filtered range, not the current page.)
    const exportQuery = filterParams(st).toString();
    for (const [ext, label] of [["csv", "Export CSV"], ["jsonl", "Export JSONL"]]) {
      const link = el("a", "btn", label);
      link.href = "/_telemetry/export." + ext + "?" + exportQuery;
      filters.appendChild(link);
    }
    card.appendChild(filters);
    main.appendChild(card);

    if (!(payload.rows || []).length) {
      card.appendChild(
        empty(
          payload.total
            ? "No rows on this page. Use Prev to step back, or clear the filters."
            : "No requests match these filters in this window.",
        ),
      );
      pager(card, st, payload);
      return;
    }

    requestTable(card, st, payload);
    pager(card, st, payload);
    card.appendChild(
      el(
        "p",
        "note",
        "metadata only — no prompt content is ever stored. There is no message column in the ledger schema, so " +
          "there is no code path that could render one.",
      ),
    );
  }

  /* ============ router ============ */
  // Returns the server's own generated_at when the view fetched it (that is the
  // truth about the data's age), else null — the caller falls back to the local
  // clock, so "updated" never freezes on a view that does not read /data.
  async function dispatch(st, main) {
    if (st.view === "agents") {
      await renderAgents(st, main);
      return null;
    }
    if (st.view === "models") {
      await renderModels(st, main);
      return null;
    }
    if (st.view === "requests") {
      await renderRequests(st, main);
      return null;
    }
    const payload = await apiGet("/_telemetry/data", rangeParams(st));
    renderOverview(payload, st, main);
    return payload.generated_at ? formatStamp(payload.generated_at) : null;
  }

  // A navigation that arrives while a fetch is open is *remembered*, not
  // dropped: the whole point of the hash-as-state design is that clicking a tab
  // always goes there. Dropping it would leave the URL on the new view and the
  // screen on the old one.
  let pendingRender = false;

  async function render() {
    const next = parseHash();
    state = next;
    const main = document.getElementById("main");
    if (!main) return;
    if (inflight) {
      pendingRender = true;
      return;
    }
    inflight = true;
    syncChrome(next);
    main.classList.add("loading"); // keep the previous frame visible, dimmed
    clear(main);
    try {
      const serverStamp = await dispatch(next, main);
      stamp = serverStamp || formatStamp(new Date().toISOString());
    } catch (err) {
      clear(main);
      const message = err && err.message ? String(err.message) : "";
      renderUnavailable(main, message && message !== "telemetry unavailable" ? message : null);
    } finally {
      main.classList.remove("loading");
      inflight = false;
      syncChrome(state);
      if (pendingRender) {
        pendingRender = false; // cleared first: render() may set it again
        render();
      }
    }
  }

  /* ============ refresh (opt-in) ============ */
  function startRefresh() {
    setInterval(() => {
      if (!autoRefresh || document.hidden || inflight) return;
      render();
    }, REFRESH_MS);
    document.addEventListener("visibilitychange", () => {
      // Coming back to the tab refreshes immediately when the operator asked
      // for auto-refresh — the timer may have skipped every tick meanwhile.
      if (!document.hidden && autoRefresh && !inflight) render();
    });
  }

  function start() {
    if (!document.getElementById("main")) return;
    buildChrome();
    if (!window.location.hash) {
      // Normalise the first load so the URL is immediately copyable, and use
      // replaceState so the initial normalisation is not a history entry the
      // back button has to walk through.
      window.history.replaceState(null, "", buildHash(parseHash()));
    }
    window.addEventListener("hashchange", () => render());
    render();
    startRefresh();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
