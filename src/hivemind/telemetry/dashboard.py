"""Self-contained cost-dashboard page served at GET /_telemetry.

Single HTML file: inline CSS + vanilla JS, no CDN, no build step, works
offline.  All charts are plain SVG built in JS.  Visual rules (dataviz
method, pinned down for this SPEC):

- dark surface (#111); neutral ink for all text/axes/legends, never the
  series color
- one validated categorical palette, ColorBrewer Set2
  (#66c2a5 #fc8d62 #8da0cb #e78ac3 #a6d854 — a known CVD-safe set; nothing
  here runs externally), assigned in FIXED order per entity across every
  chart on the page; entities beyond the fifth fold into "Other" (muted
  gray)
- thin marks (bars <= 24px or proportional), 4px cap on rounding,
  recessive hairline gridlines, no dual axes, no gradients, no 3D
- legend always present for >= 2 series; none for a single series;
  direct labels are selective (endpoints and bar tips, never interior
  stacked segments)
- per-mark hover tooltip with a hit target larger than the painted mark
- a plain HTML table with the same data below every chart
- dynamic text is inserted with textContent only (labels are untrusted)
- data failures render "telemetry unavailable", never a JS error
"""

PAGE_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hivemind — token ledger</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; }
  body {
    background: #111;
    color: #e8e8e8;
    font: 14px/1.45 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  }
  header {
    display: flex; flex-wrap: wrap; gap: 8px 20px; align-items: baseline;
    justify-content: space-between;
    padding: 18px 24px;
    border-bottom: 1px solid #2a2a2a;
  }
  header h1 { margin: 0; font-size: 18px; font-weight: 650; letter-spacing: .2px; }
  .controls { display: flex; align-items: center; gap: 8px; color: #b8b8b8; }
  .controls select {
    background: #1c1c1c; color: #e8e8e8;
    border: 1px solid #3a3a3a; border-radius: 6px;
    padding: 4px 8px; font-size: 13px;
  }
  main { max-width: 1060px; margin: 0 auto; padding: 22px 24px 80px; }
  main.loading { opacity: .45; transition: opacity .15s ease; }
  #updated { color: #8f8f8f; font-size: 12px; }

  .tiles {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    gap: 10px; margin: 20px 0 26px;
  }
  .tile {
    background: #181818; border: 1px solid #2a2a2a; border-radius: 8px;
    padding: 11px 14px;
  }
  .tile .label { color: #9d9d9d; font-size: 12px; }
  .tile .value { font-size: 21px; font-weight: 650; margin-top: 3px; }

  .card {
    background: #161616; border: 1px solid #2a2a2a; border-radius: 10px;
    padding: 18px 20px; margin-bottom: 22px;
  }
  .card h2 { margin: 0 0 3px; font-size: 16px; font-weight: 650; }
  .card .sub { margin: 0 0 14px; color: #9d9d9d; font-size: 12.5px; }
  .subchart { margin: 8px 0 20px; }
  .subchart h3 { margin: 0 0 2px; font-size: 13.5px; font-weight: 600; }

  .legend {
    display: flex; flex-wrap: wrap; gap: 5px 16px;
    margin: 2px 0 8px; color: #cfcfcf; font-size: 12.5px;
  }
  .legend .sw {
    width: 10px; height: 10px; border-radius: 2px;
    display: inline-block; margin-right: 5px; vertical-align: -1px;
  }

  svg { display: block; width: 100%; height: auto; overflow: visible; }
  svg .grid { stroke: #333; stroke-width: 1; }
  svg .tick-label { fill: #9d9d9d; font-size: 11px; }
  svg .bar-label { fill: #dcdcdc; font-size: 11px; }
  svg .row-label { fill: #cfcfcf; font-size: 11.5px; }
  svg .bar-label-in { fill: #171717; font-size: 11px; font-weight: 600; }

  table.datatable {
    width: 100%; border-collapse: collapse; margin-top: 12px;
    font-size: 12.5px; font-variant-numeric: tabular-nums;
  }
  .datatable caption {
    text-align: left; color: #8f8f8f; font-size: 11.5px;
    padding-bottom: 6px;
  }
  .datatable th {
    color: #9d9d9d; font-weight: 550; text-align: right;
    border-bottom: 1px solid #3a3a3a; padding: 4px 10px; white-space: nowrap;
  }
  .datatable td {
    padding: 4px 10px; text-align: right;
    border-bottom: 1px solid #232323; white-space: nowrap;
  }
  .datatable th:first-child, .datatable td:first-child { text-align: left; }
  .datatable tr:last-child td { border-bottom: none; }
  td.agent-hash { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 11.5px; }
  td .cost-bar {
    display: inline-block; height: 8px; border-radius: 2px;
    vertical-align: -1px; margin-left: 8px;
  }

  #tooltip {
    position: fixed; pointer-events: none; z-index: 30;
    background: #242424; border: 1px solid #4a4a4a; border-radius: 6px;
    box-shadow: 0 4px 16px rgba(0,0,0,.5);
    padding: 8px 11px; font-size: 12.5px; display: none; max-width: 360px;
  }
  #tooltip .tt-title { color: #f2f2f2; font-weight: 650; margin-bottom: 3px; }
  #tooltip .tt-row { display: flex; align-items: center; gap: 7px; padding: 1px 0; }
  #tooltip .tt-k { color: #c9c9c9; }
  #tooltip .tt-v { font-weight: 600; color: #f2f2f2; margin-left: auto; padding-left: 14px; }
  #tooltip .tt-swatch { width: 9px; height: 9px; border-radius: 2px; flex: none; }

  .empty { color: #9d9d9d; padding: 10px 2px; font-size: 13px; }
  .error {
    background: #1d1613; border: 1px solid #4a2f24; border-radius: 8px;
    padding: 22px; margin: 22px 0; color: #f0e2da;
  }
  .error h2 { margin: 0 0 6px; font-size: 16px; }
  .error p { margin: 0; color: #c9a99a; }
  footer {
    max-width: 1060px; margin: 0 auto; padding: 0 24px 44px;
    color: #8f8f8f; font-size: 12px; line-height: 1.7;
  }
  footer code { color: #b8b8b8; }
</style>
</head>
<body>
<header>
  <h1>Hivemind token ledger</h1>
  <div class="controls">
    <label for="days">Window</label>
    <select id="days">
      <option value="7">7 days</option>
      <option value="14" selected>14 days</option>
      <option value="30">30 days</option>
      <option value="90">90 days</option>
    </select>
    <span id="updated"></span>
  </div>
</header>

<main id="main"><p class="empty">Loading telemetry&hellip;</p></main>

<footer>
  Costs come from <code>mesh_telemetry.model_pricing</code> — seed or edit it with
  <code>tools/seed_pricing.sql</code>; models without a pricing row carry no cost (never guessed).
  Ledger writes are fail-open: if Postgres is unreachable telemetry is dropped and the proxy keeps
  serving. No prompt content is ever stored — only hashed agent buckets and numbers.
</footer>

<div id="tooltip" role="tooltip"></div>

<script>
"use strict";
/* ============ palette & tokens ============ */
// ColorBrewer Set2, fixed order. Entities beyond the fifth fold into "Other".
const PALETTE = ["#66c2a5", "#fc8d62", "#8da0cb", "#e78ac3", "#a6d854"];
const OTHER_COLOR = "#8f8f8f";
const SVG_NS = "http://www.w3.org/2000/svg";

const state = { days: 14 };

/* ============ tiny DOM helpers (data only ever via textContent) ============ */
function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}
function svgEl(tag, attrs) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const k in attrs) node.setAttribute(k, attrs[k]);
  return node;
}
function textEl(tag, text, attrs) {
  const node = svgEl(tag, attrs || {});
  node.textContent = text;
  return node;
}

/* ============ formatting ============ */
function fmtInt(v) { return Math.round(v).toLocaleString("en-US"); }
function fmtMoney(v) {
  if (v == null || !isFinite(v)) return "—";
  if (v === 0) return "$0";
  if (v < 0.01) return "$" + v.toFixed(4);
  if (v < 1) return "$" + v.toFixed(3);
  return "$" + v.toLocaleString("en-US", { maximumFractionDigits: 2 });
}
function fmtTokens(v) {
  if (v == null || !isFinite(v)) return "—";
  if (v >= 1e9) return (v / 1e9).toFixed(1) + "B";
  if (v >= 1e6) return (v / 1e6).toFixed(1) + "M";
  if (v >= 1e3) return (v / 1e3).toFixed(1) + "K";
  return String(Math.round(v));
}
function fmtMs(v) {
  if (v == null || !isFinite(v)) return "—";
  if (v >= 1000) return (v / 1000).toFixed(1) + " s";
  return Math.round(v) + " ms";
}
function fmtPct(v) { return (v * 100).toFixed(1) + "%"; }
function dayLabel(iso) {
  // UTC-safe "M/D" label — parse the ISO date as text so timezones never shift the day.
  const parts = String(iso).split("-");
  return Number(parts[1]) + "/" + Number(parts[2]);
}
function niceMax(v) {
  if (!(v > 0)) return 1;
  const pow = Math.pow(10, Math.floor(Math.log10(v)));
  const frac = v / pow;
  const step = frac <= 1 ? 1 : frac <= 2 ? 2 : frac <= 5 ? 5 : 10;
  return step * pow;
}

/* ============ fixed categorical color per entity (never cycled) ============
 * One page-wide alpha order is computed from every provider that appears in
 * the payload; the hue index is stable across all charts, and providers
 * ranked 6th+ fold into "Other" everywhere. */
function providerColor(provider, order) {
  const i = order.indexOf(provider);
  return i >= 0 && i < PALETTE.length ? PALETTE[i] : OTHER_COLOR;
}
function foldedName(provider, order) {
  return order.indexOf(provider) >= PALETTE.length ? "Other" : provider;
}
// The series actually drawn in one chart, in page order ("Other" last).
function chartSeries(rows, order) {
  const present = [];
  for (const name of order) {
    for (const r of rows) if (r.series === name) { present.push(name); break; }
  }
  if (!present.includes("Other")) {
    for (const r of rows) if (r.series === "Other") { present.push("Other"); break; }
  }
  return present;
}

/* ============ tooltip ============ */
const tip = document.getElementById("tooltip");
function tipShow(title, rows) {
  while (tip.firstChild) tip.removeChild(tip.firstChild);
  if (title) tip.appendChild(el("div", "tt-title", title));
  for (const r of rows) {
    const row = el("div", "tt-row");
    if (r.swatch) row.appendChild(el("span", "tt-swatch", "")).style.background = r.swatch;
    row.appendChild(el("span", "tt-k", r.label));
    row.appendChild(el("span", "tt-v", r.value));
    tip.appendChild(row);
  }
  tip.style.display = "block";
}
function tipMove(ev) {
  const pad = 14;
  const rect = tip.getBoundingClientRect();
  let x = ev.clientX + pad;
  let y = ev.clientY + pad;
  if (x + rect.width > window.innerWidth - 8) x = ev.clientX - rect.width - pad;
  if (y + rect.height > window.innerHeight - 8) y = ev.clientY - rect.height - pad;
  tip.style.left = x + "px";
  tip.style.top = y + "px";
}
function tipHide() { tip.style.display = "none"; }
function bindHover(node, title, rows) {
  node.addEventListener("pointermove", (ev) => { tipShow(title, rows); tipMove(ev); });
  node.addEventListener("pointerleave", tipHide);
}

/* ============ legend (present iff >= 2 series) ============ */
function legendEl(items) {
  const legend = el("div", "legend");
  for (const it of items) {
    const span = el("span");
    const sw = el("span", "sw", "");
    sw.style.background = it.color;
    span.appendChild(sw);
    span.appendChild(document.createTextNode(it.name));
    legend.appendChild(span);
  }
  return legend;
}

/* =====================================================================
 * Stacked daily columns — one column per day, segments per provider.
 * =================================================================== */
function stackedDaily(holder, title, rows, pick, fmt, tipFor, order) {
  const days = [];
  const per = new Map(); // day -> Map(series -> {value, cost, tokensIn, tokensOut, requests})
  for (const r of rows) {
    if (!days.includes(r.day)) days.push(r.day);
    let m = per.get(r.day);
    if (!m) { m = new Map(); per.set(r.day, m); }
    let acc = m.get(r.series);
    if (!acc) { acc = { value: 0, cost: 0, tokensIn: 0, tokensOut: 0, requests: 0 }; m.set(r.series, acc); }
    acc.value += pick(r);
    acc.cost += r.cost_usd;
    acc.tokensIn += r.tokens_in;
    acc.tokensOut += r.tokens_out;
    acc.requests += r.requests;
  }
  days.sort();
  const series = chartSeries(rows, order); // stack order == page order

  const W = 960, H = 300;
  const mL = 84, mR = 12, mT = 12, mB = 40;
  const plotW = W - mL - mR, plotH = H - mT - mB;
  const slot = plotW / days.length;
  const barW = Math.min(34, Math.max(5, slot * 0.62));
  let maxTotal = 0;
  for (const d of days) {
    let t = 0;
    for (const s of series) t += (per.get(d).get(s) || { value: 0 }).value;
    if (t > maxTotal) maxTotal = t;
  }
  const max = niceMax(maxTotal * 1.08);
  const y = (v) => mT + plotH - (v / max) * plotH;

  const box = el("div", "subchart");
  box.appendChild(el("h3", null, title));

  if (series.length >= 2) {
    box.appendChild(legendEl(series.map((s) => ({ name: s, color: providerColor(s, order) }))));
  }

  const svg = svgEl("svg", { viewBox: "0 0 " + W + " " + H });
  svg.setAttribute("aria-label", title + " — stacked by provider");
  for (let i = 1; i <= 4; i++) {
    const t = (max / 4) * i;
    svg.appendChild(svgEl("line", { x1: mL, x2: W - mR, y1: y(t), y2: y(t), class: "grid" }));
    svg.appendChild(textEl("text", fmt(t), { x: mL - 8, y: y(t) + 3.5, "text-anchor": "end", class: "tick-label" }));
  }
  const labelEvery = Math.max(1, Math.ceil(days.length / 10));
  days.forEach((d, i) => {
    if (i % labelEvery === 0) {
      svg.appendChild(textEl("text", dayLabel(d), {
        x: mL + slot * i + slot / 2, y: H - 14, "text-anchor": "middle", class: "tick-label",
      }));
    }
  });

  days.forEach((d, i) => {
    const x0 = mL + slot * i;
    const byDay = per.get(d);
    let acc = 0;
    for (const s of series) {
      const entry = byDay.get(s);
      const v = entry ? entry.value : 0;
      if (v <= 0) continue;
      const yTop = y(acc + v), yBot = y(acc);
      const rect = svgEl("rect", {
        x: x0 + (slot - barW) / 2, y: yTop + 1, width: barW,
        height: Math.max(0, yBot - yTop - 2), rx: Math.min(4, barW / 2),
      });
      rect.style.fill = providerColor(s, order);
      svg.appendChild(rect);
      // Hit target spans the whole column slot and the segment height — larger
      // than the painted mark. Drawn after the mark so it wins pointer events.
      const hit = svgEl("rect", {
        x: x0, y: yTop, width: slot, height: Math.max(1, yBot - yTop), fill: "transparent",
      });
      bindHover(hit, dayLabel(d) + " · " + s, tipFor(entry, d, s));
      svg.appendChild(hit);
      acc += v;
    }
    // Direct label: the total of the last column only (sparing — never per segment).
    if (i === days.length - 1 && acc > 0) {
      svg.appendChild(textEl("text", fmt(acc), {
        x: x0 + slot / 2, y: y(acc) - 6, "text-anchor": "middle", class: "bar-label",
      }));
    }
  });
  box.appendChild(svg);
  holder.appendChild(box);
}

/* =====================================================================
 * Top models — horizontal bars, colored by provider (same fixed map).
 * =================================================================== */
function topModelBars(holder, title, models, order) {
  const box = el("div", "subchart");
  box.appendChild(el("h3", null, title));
  const rows = models.slice();
  const W = 960;
  const labelW = 310, mR = 150, mT = 14, rowH = 28;
  const H = mT + rowH * rows.length + 26;
  const x0 = labelW + 4;
  const plotW = W - x0 - mR;
  const max = niceMax(Math.max.apply(null, rows.map((m) => m.cost_usd)));
  const x = (v) => x0 + (v / max) * plotW;

  const present = [];
  for (const m of rows) if (!present.includes(m.provider)) present.push(m.provider);
  if (present.length >= 2) {
    box.appendChild(legendEl(present.map((p) => ({ name: p, color: providerColor(p, order) }))));
  }

  const svg = svgEl("svg", { viewBox: "0 0 " + W + " " + H });
  svg.setAttribute("aria-label", title + " — horizontal bars");
  for (let i = 1; i <= 4; i++) {
    const t = (max / 4) * i;
    svg.appendChild(svgEl("line", { x1: x(t), x2: x(t), y1: mT - 4, y2: H - 20, class: "grid" }));
    svg.appendChild(textEl("text", fmtMoney(t), { x: x(t), y: H - 6, "text-anchor": "middle", class: "tick-label" }));
  }
  rows.forEach((m, i) => {
    const cy = mT + i * rowH + rowH / 2;
    svg.appendChild(textEl("text", m.model, { x: labelW - 8, y: cy, "text-anchor": "end", class: "row-label" }));
    svg.appendChild(textEl("text", m.provider, { x: labelW - 8, y: cy + 13, "text-anchor": "end", class: "tick-label" }));
    const color = providerColor(m.provider, order);
    const bw = Math.max(2, x(m.cost_usd) - x0);
    const rect = svgEl("rect", { x: x0, y: cy - 7, width: bw, height: 14, rx: Math.min(4, 7) });
    rect.style.fill = color;
    svg.appendChild(rect);
    // Hit target: the whole row band right of the labels.
    const hit = svgEl("rect", { x: x0, y: cy - 12, width: plotW + mR, height: 24, fill: "transparent" });
    bindHover(hit, m.model, [
      { swatch: color, label: "provider", value: m.provider },
      { label: "requests", value: fmtInt(m.requests) },
      { label: "tokens in/out", value: fmtInt(m.tokens_in) + " / " + fmtInt(m.tokens_out) },
      { label: "cost", value: fmtMoney(m.cost_usd) },
    ]);
    svg.appendChild(hit);
    // Value at the tip (ranked bars — every bar is an endpoint).
    svg.appendChild(textEl("text", fmtMoney(m.cost_usd), { x: x(m.cost_usd) + 6, y: cy + 3.5, class: "bar-label" }));
  });
  box.appendChild(svg);
  holder.appendChild(box);
}

/* =====================================================================
 * Latency — grouped horizontal bars, two series (p50/p95), one scale.
 * =================================================================== */
function latencyChart(holder, rows) {
  const box = el("div", "subchart");
  box.appendChild(el("h3", null, "p50 vs p95 response time"));
  const W = 960;
  const labelW = 240, mR = 150, mT = 14, rowH = 36;
  const H = mT + rowH * rows.length + 26;
  const x0 = labelW + 4;
  const plotW = W - x0 - mR;
  const max = niceMax(Math.max.apply(null, rows.map((r) => Math.max(r.p50_ms || 0, r.p95_ms || 0))));
  const x = (v) => x0 + (v / max) * plotW;
  const P50 = "#8da0cb", P95 = "#fc8d62";

  box.appendChild(legendEl([{ name: "p95", color: P95 }, { name: "p50", color: P50 }]));
  const svg = svgEl("svg", { viewBox: "0 0 " + W + " " + H });
  svg.setAttribute("aria-label", "Latency p50/p95 by provider — grouped bars");
  for (let i = 1; i <= 4; i++) {
    const t = (max / 4) * i;
    svg.appendChild(svgEl("line", { x1: x(t), x2: x(t), y1: mT - 4, y2: H - 20, class: "grid" }));
    svg.appendChild(textEl("text", fmtMs(t), { x: x(t), y: H - 6, "text-anchor": "middle", class: "tick-label" }));
  }
  rows.forEach((r, i) => {
    const top = mT + i * rowH;
    svg.appendChild(textEl("text", r.provider, { x: labelW - 8, y: top + 20, "text-anchor": "end", class: "row-label" }));
    // p50 and p95 share one baseline; 2px surface gap between the pair.
    const bar = (v, color, yOff, h, series) => {
      const bw = Math.max(2, x(v) - x0);
      const rect = svgEl("rect", { x: x0, y: top + yOff, width: bw, height: h, rx: Math.min(4, h / 2) });
      rect.style.fill = color;
      svg.appendChild(rect);
      const hit = svgEl("rect", { x: x0, y: top + yOff - 4, width: plotW + mR, height: h + 8, fill: "transparent" });
      bindHover(hit, r.provider + " · " + series, [
        { label: "requests", value: fmtInt(r.requests) },
        { swatch: color, label: series, value: fmtMs(v) },
      ]);
      svg.appendChild(hit);
      return { bw };
    };
    const p95 = bar(r.p95_ms || 0, P95, 4, 13, "p95");
    const p50 = bar(r.p50_ms || 0, P50, 19, 13, "p50");
    // p50 label sits inside its bar; p95 at the tip when it fits, else inside.
    if (p50.bw > 40) {
      svg.appendChild(textEl("text", fmtMs(r.p50_ms), { x: x0 + 6, y: top + 29, class: "bar-label-in" }));
    }
    if (p95.bw > 40) {
      svg.appendChild(textEl("text", fmtMs(r.p95_ms), { x: x0 + p95.bw + 6, y: top + 14, class: "bar-label" }));
    } else {
      svg.appendChild(textEl("text", fmtMs(r.p95_ms), { x: x0 + 6, y: top + 14, class: "bar-label-in" }));
    }
  });
  box.appendChild(svg);
  holder.appendChild(box);
}

/* =====================================================================
 * Plain HTML tables — same data as each chart.
 * =================================================================== */
function dataTable(holder, caption, headers, rows) {
  const table = el("table", "datatable");
  table.appendChild(el("caption", null, caption));
  const thead = el("thead");
  const hr = el("tr");
  for (const h of headers) hr.appendChild(el("th", null, h));
  thead.appendChild(hr);
  table.appendChild(thead);
  const tbody = el("tbody");
  for (const row of rows) {
    const tr = el("tr");
    for (const cell of row) {
      const td = el("td", cell.cls || null, cell.text);
      if (cell.title) td.title = cell.title;
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  holder.appendChild(table);
}

/* =====================================================================
 * Section renderers
 * =================================================================== */
function renderTiles(main, t) {
  const tiles = el("div", "tiles");
  const defs = [
    { label: "Total cost", value: fmtMoney(t.cost_usd) },
    { label: "Requests", value: fmtInt(t.requests) },
    { label: "Tokens in", value: fmtInt(t.tokens_in) },
    { label: "Tokens out", value: fmtInt(t.tokens_out) },
    { label: "Error rate", value: fmtPct(t.error_rate) },
    { label: "Local share", value: (t.local_share_pct || 0).toFixed(1) + "%" },
  ];
  for (const d of defs) {
    const tile = el("div", "tile");
    tile.appendChild(el("div", "label", d.label));
    tile.appendChild(el("div", "value", d.value));
    tiles.appendChild(tile);
  }
  main.appendChild(tiles);
}

function renderDaily(main, data, order) {
  const card = el("section", "card");
  card.appendChild(el("h2", null, "Daily usage"));
  card.appendChild(el("p", "sub",
    "Cost and tokens per day, stacked by provider in one fixed color order. The “Local share” " +
    "tile counts requests this proxy forwarded to a local Ollama upstream."));

  if (!data.daily.length) {
    card.appendChild(el("p", "empty", "No telemetry in this window yet."));
    main.appendChild(card);
    return;
  }
  const rows = data.daily.map((r) => ({
    day: r.day, provider: r.provider, series: foldedName(r.provider, order),
    cost_usd: r.cost_usd, tokens_in: r.tokens_in, tokens_out: r.tokens_out, requests: r.requests,
  }));

  const costBox = el("div", "subchart");
  stackedDaily(costBox, "Cost per day", rows, (r) => r.cost_usd, fmtMoney,
    (e, d) => [
      { label: "day", value: d },
      { label: "requests", value: fmtInt(e.requests) },
      { label: "cost", value: fmtMoney(e.cost) },
      { label: "tokens in/out", value: fmtInt(e.tokensIn) + " / " + fmtInt(e.tokensOut) },
    ], order);
  card.appendChild(costBox);
  const tableRows = data.daily.slice()
    .sort((a, b) => (b.day < a.day ? -1 : b.day > a.day ? 1 : 0));
  dataTable(card, "Same data as the chart above (cost).", ["Day", "Provider", "Requests", "Tokens in", "Tokens out", "Cost"],
    tableRows.map((r) => [
      { text: r.day }, { text: r.provider }, { text: fmtInt(r.requests) },
      { text: fmtInt(r.tokens_in) }, { text: fmtInt(r.tokens_out) }, { text: fmtMoney(r.cost_usd) },
    ]));

  const tokenBox = el("div", "subchart");
  stackedDaily(tokenBox, "Tokens per day", rows, (r) => r.tokens_in + r.tokens_out, fmtTokens,
    (e, d) => [
      { label: "day", value: d },
      { label: "requests", value: fmtInt(e.requests) },
      { label: "tokens in", value: fmtInt(e.tokensIn) },
      { label: "tokens out", value: fmtInt(e.tokensOut) },
    ], order);
  card.appendChild(tokenBox);
  dataTable(card, "Same data as the chart above (tokens).", ["Day", "Provider", "Requests", "Tokens in", "Tokens out", "Cost"],
    tableRows.map((r) => [
      { text: r.day }, { text: r.provider }, { text: fmtInt(r.requests) },
      { text: fmtInt(r.tokens_in) }, { text: fmtInt(r.tokens_out) }, { text: fmtMoney(r.cost_usd) },
    ]));
  main.appendChild(card);
}

function renderModels(main, data, order) {
  const card = el("section", "card");
  card.appendChild(el("h2", null, "Top models by cost"));
  card.appendChild(el("p", "sub",
    "Only models with a pricing row in mesh_telemetry.model_pricing are costed; pricing is never " +
    "auto-fetched — edit tools/seed_pricing.sql."));
  if (data.top_models.length) {
    topModelBars(card, "Cost by model", data.top_models, order);
    dataTable(card, "Same data as the chart above.", ["Model", "Provider", "Requests", "Tokens in", "Tokens out", "Cost"],
      data.top_models.map((m) => [
        { text: m.model }, { text: m.provider }, { text: fmtInt(m.requests) },
        { text: fmtInt(m.tokens_in) }, { text: fmtInt(m.tokens_out) }, { text: fmtMoney(m.cost_usd) },
      ]));
  } else {
    card.appendChild(el("p", "empty",
      "No priced models in this window. Models need rows in mesh_telemetry.model_pricing — see tools/seed_pricing.sql."));
  }
  main.appendChild(card);
}

function renderLatency(main, data) {
  const card = el("section", "card");
  card.appendChild(el("h2", null, "Latency by provider"));
  card.appendChild(el("p", "sub", "Response latency p50/p95 — two series, one scale."));
  if (data.latency.length) {
    const box = el("div", "subchart");
    latencyChart(box, data.latency);
    card.appendChild(box);
    dataTable(card, "Same data as the chart above.", ["Provider", "Requests", "p50", "p95"],
      data.latency.map((l) => [
        { text: l.provider }, { text: fmtInt(l.requests) },
        { text: fmtMs(l.p50_ms) }, { text: fmtMs(l.p95_ms) },
      ]));
  } else {
    card.appendChild(el("p", "empty", "No latency rows in this window."));
  }
  main.appendChild(card);
}

function renderAgents(main, data) {
  const card = el("section", "card");
  card.appendChild(el("h2", null, "Per-agent totals"));
  card.appendChild(el("p", "sub", "Agent hashes are rate-limit bucket labels only — identities are never stored."));
  if (!data.agents.length) {
    card.appendChild(el("p", "empty", "No requests in this window."));
    main.appendChild(card);
    return;
  }
  const maxCost = Math.max.apply(null, data.agents.map((a) => a.cost_usd));
  const table = el("table", "datatable");
  table.appendChild(el("caption", null, "One row per agent hash; the final column is the proportional cost share."));
  const thead = el("thead");
  const hr = el("tr");
  for (const h of ["Agent (hash)", "Requests", "Tokens in", "Tokens out", "Cost", "Error rate", "Cost share"]) {
    hr.appendChild(el("th", null, h));
  }
  thead.appendChild(hr);
  table.appendChild(thead);
  const tbody = el("tbody");
  data.agents.forEach((a) => {
    const tr = el("tr");
    const cells = [
      { text: a.agent_hash, cls: "agent-hash", title: "rate-limit bucket hash (agent identity)" },
      { text: fmtInt(a.requests) },
      { text: fmtInt(a.tokens_in) },
      { text: fmtInt(a.tokens_out) },
      { text: fmtMoney(a.cost_usd) },
      { text: fmtPct(a.error_rate), title: a.errors + " of " + a.requests + " requests returned status >= 400" },
    ];
    for (const cell of cells) {
      const td = el("td", cell.cls || null, cell.text);
      if (cell.title) td.title = cell.title;
      tr.appendChild(td);
    }
    const shareTd = el("td");
    if (maxCost > 0) {
      const share = Math.max(2, (a.cost_usd / maxCost) * 100);
      const bar = el("span", "cost-bar", "");
      bar.style.width = share.toFixed(1) + "px";
      bar.style.background = "#8da0cb";
      shareTd.appendChild(bar);
    } else {
      shareTd.textContent = "—";
    }
    tr.appendChild(shareTd);
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  card.appendChild(table);
  main.appendChild(card);
}

function renderError(detail) {
  const main = document.getElementById("main");
  while (main.firstChild) main.removeChild(main.firstChild);
  const box = el("div", "error");
  box.appendChild(el("h2", null, "Telemetry unavailable"));
  box.appendChild(el("p", null,
    (detail || "The ledger is unreachable or disabled (no --telemetry-dsn).") +
    " The proxy keeps serving — only telemetry is affected (fail-open)."));
  main.appendChild(box);
}

function render(payload) {
  const main = document.getElementById("main");
  while (main.firstChild) main.removeChild(main.firstChild);
  if (!payload || payload.error) {
    renderError(payload && payload.error !== "telemetry unavailable" ? payload.error : null);
    return;
  }
  // Page-wide provider order — hue identity stays stable across every chart.
  const all = [];
  for (const list of [payload.daily, payload.top_models, payload.latency]) {
    for (const r of list) {
      if (r.provider && !all.includes(r.provider)) all.push(r.provider);
    }
  }
  all.sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
  const order = all;

  renderTiles(main, payload.totals);
  renderDaily(main, payload, order);
  renderModels(main, payload, order);
  renderLatency(main, payload);
  renderAgents(main, payload);
  const stamp = String(payload.generated_at || "").replace("T", " ").replace("+00:00", " UTC");
  if (stamp) document.getElementById("updated").textContent = "updated " + stamp;
}

async function load() {
  const main = document.getElementById("main");
  main.classList.add("loading"); // keep the previous frame visible at reduced opacity
  let payload;
  try {
    const resp = await fetch("/_telemetry/data?days=" + state.days, { headers: { accept: "application/json" } });
    payload = await resp.json();
  } catch (err) {
    payload = { error: "Dashboard request failed: " + String(err) };
  }
  main.classList.remove("loading");
  render(payload);
}

document.getElementById("days").addEventListener("change", (ev) => {
  state.days = parseInt(ev.target.value, 10) || 14;
  load();
});

load();
</script>
</body>
</html>
"""
