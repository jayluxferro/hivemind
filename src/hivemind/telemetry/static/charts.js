/* HiveCharts — the SVG chart toolkit behind the telemetry dashboard.
 *
 * Loaded before dashboard.js and exposing exactly one global, window.HiveCharts.
 * Everything else stays inside this closure on purpose: both files are classic
 * scripts sharing one global scope, so two top-level `function render`
 * declarations would silently overwrite each other (the second script wins at
 * evaluation time, and the first script's own calls would then land somewhere
 * else entirely).
 *
 * The visual rules implemented here are the design-system contract from the
 * module docstring of src/hivemind/telemetry/dashboard.py, pinned by
 * docs/token-ledger-analytics.md (D11):
 *
 *   - dark surface (#111); neutral ink for text/axes/legends, never the series hue
 *   - ColorBrewer Set2 in FIXED order, one hue per entity across every chart on
 *     the page; entities past the fifth fold into "Other" (muted gray)
 *   - thin marks (<= 24px), 4px radius cap, recessive hairline gridlines, one
 *     scale per chart (never a dual axis), no gradients, 2px surface gap between
 *     stacked segments and between paired bars
 *   - legend iff >= 2 series, none for a single series; direct labels are
 *     selective — endpoints and bar tips, never interior stacked segments
 *   - a per-mark tooltip whose hit target is larger than the painted mark
 *   - every dynamic string goes in through textContent (agent hashes and model
 *     names arrive from request bodies: they are untrusted)
 *   - nothing here fetches anything: no external URL, no CDN, no build step
 *
 * Palette honesty note. ColorBrewer Set2 is pinned by the SPEC, and a
 * dark-surface pass through the dataviz validator flags it rather than blessing
 * it: on #111 the lightness band is off, #66c2a5 and #8da0cb fall under the
 * chroma floor, and the worst adjacent pair (#8da0cb vs #e78ac3) separates by
 * only ~14 Delta-E in normal vision (1.5 in protan). Contrast against the
 * surface passes, and the SPEC wins, so Set2 stays. What makes it survivable is
 * everything the same SPEC already mandates: a legend whenever two series are
 * drawn, a tooltip on every mark, 2px gaps between touching fills, and the same
 * numbers in a plain table under every chart — identity is never carried by hue
 * alone. Hues are also never assigned by rank: dropping a series never repaints
 * the survivors (see the page-wide order registry below).
 */

"use strict";

(function () {
  // An XML namespace is an *identifier*, not an address: createElementNS wants
  // the literal string, and nothing ever resolves it over a network.
  const SVG_NS = "http://www.w3.org/2000/svg";

  // ColorBrewer Set2, fixed order. A sixth entity is never a generated hue.
  const PALETTE = ["#66c2a5", "#fc8d62", "#8da0cb", "#e78ac3", "#a6d854"];
  const OTHER_COLOR = "#8f8f8f";
  // Status is a *category*, not an entity: one muted hue, never a Set2 slot.
  const STATUS_COLOR = "#7f8c99";
  // Latency pair. The two percentiles are the only two series in their chart,
  // so they may borrow two Set2 steps without colliding with the provider
  // identity used elsewhere — a hue still never means two things *within one
  // chart*, and the legend names both.
  const P50_COLOR = "#8da0cb";
  const P95_COLOR = "#fc8d62";

  /* ============ page-wide entity order ============
   * "Fixed order" means fixed page-wide, not fixed per chart: if the top-models
   * bars and the latency chart disagreed about which provider owns which hue,
   * the legend would teach the wrong thing, and a filter that dropped a series
   * would repaint the survivors. dashboard.js registers one order per dimension
   * before it builds any chart; the builders read it back through orderFor().
   * An explicit `order` argument always wins, so each builder still works
   * standalone.
   */
  const ORDERS = { agent: [], provider: [] };

  function setEntityOrder(dimension, names) {
    ORDERS[dimension] = (names || []).slice();
  }

  function orderFor(dimension) {
    return ORDERS[dimension] || [];
  }

  // The generic entity -> hue lookup (the name is Phase 1's; the same function
  // colors agent hashes and providers, whichever dimension a chart carries).
  function providerColor(name, order) {
    const list = order || orderFor("provider");
    const i = list.indexOf(name);
    return i >= 0 && i < PALETTE.length ? PALETTE[i] : OTHER_COLOR;
  }

  // The label an entity wears once sixth-place-and-later entities are folded.
  function foldedName(name, order) {
    const list = order || orderFor("provider");
    return list.indexOf(name) >= PALETTE.length ? "Other" : name;
  }

  // The series actually drawn in one chart, in page order, "Other" last.
  // Anything the order does not name is still drawn (first-appearance order):
  // a caller that never registered an order, or an entity the payload carries
  // but the order list missed, must not vanish from the chart silently.
  function chartSeries(rows, order) {
    const present = [];
    for (const name of order || []) {
      if (name === "Other") continue;
      if (rows.some((r) => r.series === name)) present.push(name);
    }
    for (const r of rows) {
      if (r.series === "Other" || present.includes(r.series)) continue;
      present.push(r.series);
    }
    if (rows.some((r) => r.series === "Other")) present.push("Other");
    return present;
  }

  /* ============ tiny DOM helpers ============ */
  // Dynamic data only ever arrives through textContent: `el("div", null, name)`
  // cannot execute anything, which is the whole point when `name` is a model
  // string that came in over the wire.
  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function svgEl(tag, attrs) {
    const node = document.createElementNS(SVG_NS, tag);
    const bag = attrs || {};
    for (const k in bag) {
      if (Object.prototype.hasOwnProperty.call(bag, k)) node.setAttribute(k, bag[k]);
    }
    return node;
  }

  function textEl(tag, text, attrs) {
    const node = svgEl(tag, attrs || {});
    node.textContent = text;
    return node;
  }

  /* ============ formatting ============ */
  function fmtInt(v) {
    if (v == null || !isFinite(v)) return "—";
    return Math.round(v).toLocaleString("en-US");
  }

  function fmtTokens(v) {
    if (v == null || !isFinite(v)) return "—";
    const n = Math.abs(v);
    if (n >= 1e9) return (v / 1e9).toFixed(1) + "B";
    if (n >= 1e6) return (v / 1e6).toFixed(1) + "M";
    if (n >= 1e3) return (v / 1e3).toFixed(1) + "K";
    return String(Math.round(v));
  }

  function fmtMs(v) {
    if (v == null || !isFinite(v)) return "—";
    if (v >= 1000) return (v / 1000).toFixed(1) + " s";
    return Math.round(v) + " ms";
  }

  // Rate formatter. Null-safe on purpose: "no cache reads and no input tokens"
  // is not 0% hit rate, it is an absent measurement, and the page says so with
  // an em dash rather than a confident zero.
  function fmtPct(v) {
    if (v == null || !isFinite(v)) return "—";
    return (v * 100).toFixed(1) + "%";
  }

  // Cache hit rate: tokens served from cache over total input (read + fresh in).
  const hitRate = (r) => {
    const total = (r.cache_read || 0) + (r.tokens_in || 0);
    return total > 0 ? (r.cache_read || 0) / total : null;
  };

  /* ============ bucket labels (UTC-safe by construction) ============
   * Server bucket timestamps arrive as text — "2026-09-14" for a day
   * (date_trunc(...)::date) and "2026-09-14T13:00:00Z" for an hour. They are
   * sliced as strings and never handed to `new Date(...)`: parsing would apply
   * the viewer's local timezone, and a bucket that starts at 00:00 UTC would
   * render as the previous day for anyone west of Greenwich. The SQL already
   * pins bucketing to UTC (D7); this is the other half of that promise.
   */
  function labelParts(iso) {
    const s = String(iso == null ? "" : iso);
    const head = s.slice(0, 10).split("-");
    return {
      y: Number(head[0]),
      m: Number(head[1]),
      d: Number(head[2]),
      hour: s.length > 12 ? s.slice(11, 13) : null,
    };
  }

  function dayLabel(iso) {
    const p = labelParts(iso);
    if (!isFinite(p.m) || !isFinite(p.d)) return String(iso == null ? "" : iso);
    return p.m + "/" + p.d;
  }

  function hourLabel(iso) {
    const p = labelParts(iso);
    if (!isFinite(p.m) || !isFinite(p.d) || p.hour == null) return dayLabel(iso);
    return p.m + "/" + p.d + " " + p.hour + ":00";
  }

  function bucketLabel(iso, bucket) {
    return bucket === "hour" ? hourLabel(iso) : dayLabel(iso);
  }

  // Tooltip-grade stamp: unambiguous without being a full ISO string.
  function stampLabel(iso, bucket) {
    const s = String(iso == null ? "" : iso);
    if (bucket === "hour" && s.length > 12) return s.slice(0, 10) + " " + s.slice(11, 13) + ":00 UTC";
    return s.slice(0, 10);
  }

  function niceMax(v) {
    if (!(v > 0)) return 1;
    const pow = Math.pow(10, Math.floor(Math.log10(v)));
    const frac = v / pow;
    const step = frac <= 1 ? 1 : frac <= 2 ? 2 : frac <= 5 ? 5 : 10;
    return step * pow;
  }

  function shortHash(h) {
    if (h == null) return "—";
    const s = String(h);
    return s.length > 12 ? s.slice(0, 12) + "…" : s;
  }

  /* ============ tooltip ============ */
  function tipEl() {
    return document.getElementById("tooltip");
  }

  function tipShow(title, rows) {
    const tip = tipEl();
    if (!tip) return;
    while (tip.firstChild) tip.removeChild(tip.firstChild);
    if (title) tip.appendChild(el("div", "tt-title", title));
    for (const r of rows || []) {
      const row = el("div", "tt-row");
      if (r.swatch) {
        const sw = el("span", "tt-swatch", "");
        sw.style.background = r.swatch;
        row.appendChild(sw);
      }
      row.appendChild(el("span", "tt-k", r.label));
      row.appendChild(el("span", "tt-v", r.value));
      tip.appendChild(row);
    }
    tip.style.display = "block";
  }

  function tipMove(ev) {
    const tip = tipEl();
    if (!tip) return;
    const pad = 14;
    const rect = tip.getBoundingClientRect();
    let x = ev.clientX + pad;
    let y = ev.clientY + pad;
    if (x + rect.width > window.innerWidth - 8) x = ev.clientX - rect.width - pad;
    if (y + rect.height > window.innerHeight - 8) y = ev.clientY - rect.height - pad;
    tip.style.left = Math.max(4, x) + "px";
    tip.style.top = Math.max(4, y) + "px";
  }

  function tipHide() {
    const tip = tipEl();
    if (tip) tip.style.display = "none";
  }

  function bindHover(node, title, rows) {
    node.addEventListener("pointermove", (ev) => {
      tipShow(title, rows);
      tipMove(ev);
    });
    node.addEventListener("pointerleave", tipHide);
  }

  // Make a mark navigable: pointer click plus keyboard (Enter/Space), because
  // the alternative is a drill-down only a mouse can reach.
  function bindActivate(node, fn) {
    if (typeof fn !== "function") return;
    node.style.cursor = "pointer";
    node.addEventListener("click", fn);
    node.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " " || ev.key === "Spacebar") {
        ev.preventDefault();
        fn(ev);
      }
    });
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

  /* ============ plain HTML tables (same data as the chart above) ============
   * Deliberately boring, and deliberately always present: it is the accessible
   * reading of every chart, and the only place a hue is never load-bearing.
   *
   * headers: string | {text, cls, title, onClick, ariaSort}
   * rows:    [cell, ...] | {cells: [...], cls, onClick, label}
   * cell:    string | {text, cls, title}
   * Returns the <table> so a caller can wire up anything else it needs.
   */
  function makeCell(spec) {
    if (spec !== null && typeof spec === "object" && !Array.isArray(spec)) {
      const td = el("td", spec.cls || null, spec.text);
      if (spec.title) td.title = spec.title;
      return td;
    }
    // A bare string/number is the shorthand; presentation that matters (the
    // monospace hash column) is asked for explicitly, never guessed from the
    // value's shape.
    return el("td", null, spec == null ? "" : String(spec));
  }

  function dataTable(holder, caption, headers, rows) {
    const table = el("table", "datatable");
    if (caption) table.appendChild(el("caption", null, caption));

    const thead = el("thead");
    const headRow = el("tr");
    for (const h of headers || []) {
      if (h !== null && typeof h === "object") {
        const th = el("th", h.cls || null, h.text);
        if (h.title) th.title = h.title;
        if (h.onClick) {
          // Focusable so the sort is reachable without a mouse. Not
          // role="button": the th has to stay a table header, which is what
          // aria-sort is for.
          th.setAttribute("tabindex", "0");
          bindActivate(th, h.onClick);
        }
        if (h.ariaSort) th.setAttribute("aria-sort", h.ariaSort);
        headRow.appendChild(th);
      } else {
        headRow.appendChild(el("th", null, h));
      }
    }
    thead.appendChild(headRow);
    table.appendChild(thead);

    const tbody = el("tbody");
    for (const row of rows || []) {
      const spec = row !== null && typeof row === "object" && !Array.isArray(row) && row.cells ? row : { cells: row };
      const tr = el("tr", spec.cls || null);
      for (const cell of spec.cells || []) tr.appendChild(makeCell(cell));
      if (spec.onClick) {
        // Clickable *and* focusable: a drill-down that only a mouse can reach
        // is a drill-down half the operators do not have.
        tr.classList.add("clickable");
        tr.setAttribute("tabindex", "0");
        if (spec.label) tr.setAttribute("aria-label", spec.label);
        bindActivate(tr, spec.onClick);
      }
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    holder.appendChild(table);
    return table;
  }

  /* =====================================================================
   * stackedDaily(holder, title, rows, bucket, opts?)
   *
   * One column per time bucket, one segment per agent. Stack order is page
   * order (fixed hues), and the fold is materialised in the data before
   * stacking so "Other" is a real segment with a real total.
   *
   * opts: {pick, valueFmt, timeKey, order, legendLabel, shortLabels,
   *        onSegmentClick(folded, stamp, entry)}
   * =================================================================== */
  function stackedDaily(holder, title, rows, bucket, opts) {
    const o = opts || {};
    const pick = o.pick || ((r) => (r.tokens_in || 0) + (r.tokens_out || 0));
    const valueFmt = o.valueFmt || fmtTokens;
    const timeKey = o.timeKey || "day";
    const order = o.order || orderFor("agent");
    const onSegmentClick = o.onSegmentClick || null;
    const legendLabel = o.legendLabel || ((name) => (name === "Other" ? "Other" : shortHash(name)));

    const stamps = [];
    const per = new Map(); // stamp -> Map(folded series -> accumulator)
    for (const r of rows || []) {
      const stamp = r[timeKey];
      if (stamp === undefined || stamp === null) continue;
      if (!stamps.includes(stamp)) stamps.push(stamp);
      let byStamp = per.get(stamp);
      if (!byStamp) {
        byStamp = new Map();
        per.set(stamp, byStamp);
      }
      const raw = r.agent_hash;
      const folded = foldedName(raw, order);
      let acc = byStamp.get(folded);
      if (!acc) {
        acc = { value: 0, tokensIn: 0, tokensOut: 0, requests: 0, errors: 0, raw: null };
        byStamp.set(folded, acc);
      }
      acc.value += pick(r) || 0;
      acc.tokensIn += r.tokens_in || 0;
      acc.tokensOut += r.tokens_out || 0;
      acc.requests += r.requests || 0;
      acc.errors += r.errors || 0;
      // Only an unfolded series may be drilled into: "Other" is an aggregate,
      // and navigating from it would silently pick one of its members.
      acc.raw = folded === "Other" ? null : raw;
    }
    stamps.sort();

    const box = el("div", "subchart");
    box.appendChild(el("h3", null, title));

    if (!stamps.length) {
      box.appendChild(el("p", "empty", "No telemetry in this window yet."));
      holder.appendChild(box);
      return;
    }

    // chartSeries wants `{series}` objects; the segments were already folded
    // into `per`, so one projected row per folded name is enough.
    const foldedRows = [];
    for (const name of order) {
      foldedRows.push({ series: foldedName(name, order) });
    }
    for (const r of rows || []) {
      foldedRows.push({ series: foldedName(r.agent_hash, order) });
    }
    const series = chartSeries(foldedRows, order);

    if (series.length >= 2) {
      box.appendChild(legendEl(series.map((s) => ({ name: legendLabel(s), color: providerColor(s, order) }))));
    }

    const W = 960;
    const H = 300;
    const mL = 84;
    const mR = 12;
    const mT = 12;
    const mB = 40;
    const plotW = W - mL - mR;
    const plotH = H - mT - mB;
    const slot = plotW / stamps.length;
    // SPEC: thin marks. Phase 1 allowed 34px columns; the cap is 24.
    const barW = Math.min(24, Math.max(4, slot * 0.62));

    let maxTotal = 0;
    for (const stamp of stamps) {
      let total = 0;
      const byStamp = per.get(stamp);
      for (const s of series) {
        const entry = byStamp.get(s);
        total += entry ? entry.value : 0;
      }
      if (total > maxTotal) maxTotal = total;
    }
    const max = niceMax(maxTotal * 1.08);
    const y = (v) => mT + plotH - (v / max) * plotH;

    const svg = svgEl("svg", { viewBox: "0 0 " + W + " " + H });
    svg.setAttribute("aria-label", title + " — stacked columns");
    for (let i = 1; i <= 4; i++) {
      const t = (max / 4) * i;
      svg.appendChild(svgEl("line", { x1: mL, x2: W - mR, y1: y(t), y2: y(t), class: "grid" }));
      svg.appendChild(textEl("text", valueFmt(t), { x: mL - 8, y: y(t) + 3.5, "text-anchor": "end", class: "tick-label" }));
    }
    const labelEvery = Math.max(1, Math.ceil(stamps.length / 10));
    stamps.forEach((stamp, i) => {
      if (i % labelEvery === 0) {
        svg.appendChild(
          textEl("text", bucketLabel(stamp, bucket), {
            x: mL + slot * i + slot / 2,
            y: H - 14,
            "text-anchor": "middle",
            class: "tick-label",
          }),
        );
      }
    });

    stamps.forEach((stamp, i) => {
      const x0 = mL + slot * i;
      const byStamp = per.get(stamp);
      let acc = 0;
      for (const s of series) {
        const entry = byStamp.get(s);
        const v = entry ? entry.value : 0;
        if (v <= 0) continue;
        const yTop = y(acc + v);
        const yBot = y(acc);
        const color = providerColor(s, order);
        const rect = svgEl("rect", {
          x: x0 + (slot - barW) / 2,
          y: yTop + 1,
          width: barW,
          height: Math.max(0, yBot - yTop - 2), // 2px surface gap between segments
          rx: Math.min(4, barW / 2),
        });
        rect.style.fill = color;
        svg.appendChild(rect);

        // Hit target spans the whole column slot and the segment height —
        // always larger than the painted mark. Drawn after the mark so it wins
        // the pointer events.
        const hit = svgEl("rect", {
          x: x0,
          y: yTop,
          width: Math.max(1, slot),
          height: Math.max(1, yBot - yTop),
          fill: "transparent",
        });
        const label = s === "Other" ? "Other (folded)" : s;
        bindHover(hit, stampLabel(stamp, bucket) + " · " + label, [
          { swatch: color, label: "agent", value: label },
          { label: "requests", value: fmtInt(entry ? entry.requests : 0) },
          {
            label: "tokens in/out",
            value: fmtInt(entry ? entry.tokensIn : 0) + " / " + fmtInt(entry ? entry.tokensOut : 0),
          },
          { label: "errors", value: fmtInt(entry ? entry.errors : 0) },
        ]);
        if (onSegmentClick && entry && entry.raw) {
          const raw = entry.raw;
          bindActivate(hit, () => onSegmentClick(raw, stamp, entry));
        }
        svg.appendChild(hit);
        acc += v;
      }
      // One direct label per chart: the endpoint column's total, never an
      // interior segment (SPEC: selective direct labels).
      if (i === stamps.length - 1 && acc > 0) {
        svg.appendChild(
          textEl("text", valueFmt(acc), {
            x: x0 + slot / 2,
            y: y(acc) - 6,
            "text-anchor": "middle",
            class: "bar-label",
          }),
        );
      }
    });

    box.appendChild(svg);
    holder.appendChild(box);
  }

  /* =====================================================================
   * bars(holder, title, rows, opts)
   *
   * Horizontal ranked bars. Used for two different jobs on purpose:
   *   - top models, where the color dimension is the provider (legend iff
   *     two or more providers are present)
   *   - status codes, where status is a *category*, so colorFn returns one
   *     muted hue and colorKey stays null (no legend — one series)
   *
   * opts: {labelKey, valueKey, colorFn, colorKey, legendName, subLabelKey,
   *        valueFmt, labelFmt, tipRows(row), onRowClick(row), rowLabelClass,
   *        labelW}
   * =================================================================== */
  function bars(holder, title, rows, opts) {
    const o = opts || {};
    const labelKey = o.labelKey || "label";
    const valueKey = o.valueKey || "value";
    const colorFn = o.colorFn || (() => PALETTE[0]);
    const colorKey = o.colorKey || null;
    const legendName = o.legendName || null;
    const valueFmt = o.valueFmt || fmtTokens;
    const labelFmt = o.labelFmt || ((v) => (v == null ? "—" : String(v)));
    const onRowClick = o.onRowClick || null;
    const rowLabelClass = o.rowLabelClass ? "row-label " + o.rowLabelClass : "row-label";
    const subLabelKey = o.subLabelKey !== undefined ? o.subLabelKey : colorKey && colorKey !== labelKey ? colorKey : null;
    const list = (rows || []).slice();

    const box = el("div", "subchart");
    box.appendChild(el("h3", null, title));

    if (!list.length) {
      box.appendChild(el("p", "empty", "No rows in this window."));
      holder.appendChild(box);
      return;
    }

    const W = 960;
    const labelW = o.labelW || 300;
    const mR = 150;
    const mT = 14;
    const rowH = 28;
    const H = mT + rowH * list.length + 26;
    const x0 = labelW + 4;
    const plotW = W - x0 - mR;
    const values = list.map((r) => Number(r[valueKey]) || 0);
    const max = niceMax(Math.max.apply(null, values));
    const x = (v) => x0 + (v / max) * plotW;

    if (colorKey) {
      const seen = [];
      for (const r of list) {
        const raw = r[colorKey];
        if (raw === undefined || raw === null) continue;
        const name = legendName ? legendName(raw) : raw;
        if (!seen.some((entry) => entry.name === name)) seen.push({ name: name, color: colorFn(raw, r) });
      }
      if (seen.length >= 2) box.appendChild(legendEl(seen));
    }

    const svg = svgEl("svg", { viewBox: "0 0 " + W + " " + H });
    svg.setAttribute("aria-label", title + " — horizontal bars");
    for (let i = 1; i <= 4; i++) {
      const t = (max / 4) * i;
      svg.appendChild(svgEl("line", { x1: x(t), x2: x(t), y1: mT - 4, y2: H - 20, class: "grid" }));
      svg.appendChild(textEl("text", valueFmt(t), { x: x(t), y: H - 6, "text-anchor": "middle", class: "tick-label" }));
    }

    list.forEach((r, i) => {
      const cy = mT + i * rowH + rowH / 2;
      const value = Number(r[valueKey]) || 0;
      const color = colorFn(colorKey ? r[colorKey] : r[labelKey], r);
      svg.appendChild(
        textEl("text", labelFmt(r[labelKey]), {
          x: labelW - 8,
          y: subLabelKey ? cy : cy + 3.5,
          "text-anchor": "end",
          class: rowLabelClass,
        }),
      );
      if (subLabelKey) {
        svg.appendChild(
          textEl("text", labelFmt(r[subLabelKey]), {
            x: labelW - 8,
            y: cy + 13,
            "text-anchor": "end",
            class: "tick-label",
          }),
        );
      }
      const bw = Math.max(2, x(value) - x0);
      const rect = svgEl("rect", { x: x0, y: cy - 7, width: bw, height: 14, rx: Math.min(4, 7) });
      rect.style.fill = color;
      svg.appendChild(rect);

      // Hit target: the whole row band right of the labels — taller and wider
      // than the 14px painted bar.
      const hit = svgEl("rect", { x: x0, y: cy - 12, width: Math.max(1, plotW + mR), height: 24, fill: "transparent" });
      bindHover(hit, labelFmt(r[labelKey]), o.tipRows ? o.tipRows(r, value) : [{ label: valueKey, value: valueFmt(value) }]);
      if (onRowClick) bindActivate(hit, () => onRowClick(r));
      svg.appendChild(hit);

      // Value at the tip: every ranked bar is an endpoint, so every bar is
      // directly labeled.
      svg.appendChild(textEl("text", valueFmt(value), { x: x(value) + 6, y: cy + 3.5, class: "bar-label" }));
    });

    box.appendChild(svg);
    holder.appendChild(box);
  }

  /* =====================================================================
   * latencyChart(holder, rows, opts)
   *
   * Grouped horizontal bars: p95 over p50 on ONE scale (never a dual axis),
   * 2px surface gap between the pair.
   *
   * opts: {nameKey, title, labelW, subLabelKey}
   * =================================================================== */
  function latencyChart(holder, rows, opts) {
    const o = opts || {};
    const nameKey = o.nameKey || "provider";
    const title = o.title || "p50 vs p95 response time";
    const list = (rows || []).slice();

    const box = el("div", "subchart");
    box.appendChild(el("h3", null, title));

    if (!list.length) {
      box.appendChild(el("p", "empty", "No latency rows in this window."));
      holder.appendChild(box);
      return;
    }

    // A model row still carries its provider — it is the useful second line.
    const subLabelKey = o.subLabelKey !== undefined ? o.subLabelKey : nameKey === "model" ? "provider" : null;
    const labelW = o.labelW || (nameKey === "model" ? 320 : 240);
    const W = 960;
    const mR = 150;
    const mT = 14;
    const rowH = 36;
    const H = mT + rowH * list.length + 26;
    const x0 = labelW + 4;
    const plotW = W - x0 - mR;
    const max = niceMax(
      Math.max.apply(
        null,
        list.map((r) => Math.max(Number(r.p50_ms) || 0, Number(r.p95_ms) || 0)),
      ),
    );
    const x = (v) => x0 + (v / max) * plotW;

    box.appendChild(
      legendEl([
        { name: "p95", color: P95_COLOR },
        { name: "p50", color: P50_COLOR },
      ]),
    );

    const svg = svgEl("svg", { viewBox: "0 0 " + W + " " + H });
    svg.setAttribute("aria-label", title + " — grouped bars by " + nameKey);
    for (let i = 1; i <= 4; i++) {
      const t = (max / 4) * i;
      svg.appendChild(svgEl("line", { x1: x(t), x2: x(t), y1: mT - 4, y2: H - 20, class: "grid" }));
      svg.appendChild(textEl("text", fmtMs(t), { x: x(t), y: H - 6, "text-anchor": "middle", class: "tick-label" }));
    }

    list.forEach((r, i) => {
      const top = mT + i * rowH;
      const name = r[nameKey];
      svg.appendChild(
        textEl("text", name == null ? "—" : String(name), {
          x: labelW - 8,
          y: subLabelKey ? top + 20 : top + 23,
          "text-anchor": "end",
          class: "row-label",
        }),
      );
      if (subLabelKey && r[subLabelKey] != null) {
        svg.appendChild(
          textEl("text", String(r[subLabelKey]), { x: labelW - 8, y: top + 33, "text-anchor": "end", class: "tick-label" }),
        );
      }

      // p50 and p95 share one baseline; 2px surface gap between the pair.
      const bar = (value, color, series, yOff) => {
        const v = Number(value) || 0;
        const bw = Math.max(2, x(v) - x0);
        const rect = svgEl("rect", { x: x0, y: top + yOff, width: bw, height: 13, rx: Math.min(4, 6.5) });
        rect.style.fill = color;
        svg.appendChild(rect);
        const hit = svgEl("rect", { x: x0, y: top + yOff - 4, width: Math.max(1, plotW + mR), height: 21, fill: "transparent" });
        bindHover(hit, (name == null ? "—" : String(name)) + " · " + series, [
          { label: "requests", value: fmtInt(r.requests) },
          { swatch: color, label: series, value: fmtMs(value) },
        ]);
        svg.appendChild(hit);
        return bw;
      };
      const p95Width = bar(r.p95_ms, P95_COLOR, "p95", 4);
      const p50Width = bar(r.p50_ms, P50_COLOR, "p50", 19);

      // p50 sits inside its bar, p95 at the tip when it fits, else inside.
      if (p50Width > 40) {
        svg.appendChild(textEl("text", fmtMs(r.p50_ms), { x: x0 + 6, y: top + 29, class: "bar-label-in" }));
      }
      if (p95Width > 40) {
        svg.appendChild(textEl("text", fmtMs(r.p95_ms), { x: x0 + p95Width + 6, y: top + 14, class: "bar-label" }));
      } else {
        svg.appendChild(textEl("text", fmtMs(r.p95_ms), { x: x0 + 6, y: top + 14, class: "bar-label-in" }));
      }
    });

    box.appendChild(svg);
    holder.appendChild(box);
  }

  /* =====================================================================
   * singleSeries(holder, title, rows, pick, opts)
   *
   * One measure over time — the drill-down activity chart. A single series
   * needs no legend (the title names it), and the mark changes with the
   * density: columns while they can still be read as distinct buckets, a thin
   * 2px line with a crosshair once there are more buckets than that.
   *
   * opts: {bucket, valueFmt, valueLabel, tipRows(row, value)}
   * =================================================================== */
  const MAX_BAR_BUCKETS = 48;

  function singleSeries(holder, title, rows, pick, opts) {
    const o = opts || {};
    const bucket = o.bucket || "day";
    const valueFmt = o.valueFmt || fmtTokens;
    const valueLabel = o.valueLabel || "value";
    const list = (rows || []).slice();

    const box = el("div", "subchart");
    box.appendChild(el("h3", null, title));

    if (!list.length) {
      box.appendChild(el("p", "empty", "No telemetry in this window yet."));
      holder.appendChild(box);
      return;
    }

    const W = 960;
    const H = 240;
    const mL = 84;
    const mR = 12;
    const mT = 14;
    const mB = 40;
    const plotW = W - mL - mR;
    const plotH = H - mT - mB;
    const values = list.map((r) => Number(pick(r)) || 0);
    const max = niceMax(Math.max.apply(null, values) * 1.08);
    const y = (v) => mT + plotH - (v / max) * plotH;

    const tipFor = (r, v) =>
      o.tipRows
        ? o.tipRows(r, v)
        : [
            { label: valueLabel, value: valueFmt(v) },
            { label: "requests", value: fmtInt(r.requests) },
            {
              label: "tokens in/out",
              value: fmtInt(r.tokens_in || 0) + " / " + fmtInt(r.tokens_out || 0),
            },
            { label: "errors", value: fmtInt(r.errors || 0) },
          ];

    const svg = svgEl("svg", { viewBox: "0 0 " + W + " " + H });
    svg.setAttribute("aria-label", title + " — activity over time");
    for (let i = 1; i <= 4; i++) {
      const t = (max / 4) * i;
      svg.appendChild(svgEl("line", { x1: mL, x2: W - mR, y1: y(t), y2: y(t), class: "grid" }));
      svg.appendChild(textEl("text", valueFmt(t), { x: mL - 8, y: y(t) + 3.5, "text-anchor": "end", class: "tick-label" }));
    }

    const slot = plotW / list.length;
    const labelEvery = Math.max(1, Math.ceil(list.length / 10));
    list.forEach((r, i) => {
      if (i % labelEvery === 0) {
        svg.appendChild(
          textEl("text", bucketLabel(r.bucket_start, bucket), {
            x: mL + slot * i + slot / 2,
            y: H - 14,
            "text-anchor": "middle",
            class: "tick-label",
          }),
        );
      }
    });

    let peak = 0;
    values.forEach((v, i) => {
      if (v > values[peak]) peak = i;
    });

    if (list.length <= MAX_BAR_BUCKETS) {
      const barW = Math.min(24, Math.max(4, slot * 0.62));
      list.forEach((r, i) => {
        const v = values[i];
        if (v <= 0) return;
        const x0 = mL + slot * i;
        const rect = svgEl("rect", {
          x: x0 + (slot - barW) / 2,
          y: y(v),
          width: barW,
          height: Math.max(0, mT + plotH - y(v)),
          rx: Math.min(4, barW / 2),
        });
        rect.style.fill = PALETTE[0];
        svg.appendChild(rect);
        // Full-height, full-slot target: a 4px column is not a hover target.
        const hit = svgEl("rect", { x: x0, y: mT, width: Math.max(1, slot), height: plotH, fill: "transparent" });
        bindHover(hit, stampLabel(r.bucket_start, bucket), tipFor(r, v));
        svg.appendChild(hit);
      });
      if (values[peak] > 0) {
        svg.appendChild(
          textEl("text", valueFmt(values[peak]), {
            x: mL + slot * peak + slot / 2,
            y: y(values[peak]) - 6,
            "text-anchor": "middle",
            class: "bar-label",
          }),
        );
      }
    } else {
      // Thin line, no markers (hundreds of them would be a texture, not data),
      // plus a crosshair so a bucket is still individually addressable.
      let d = "";
      values.forEach((v, i) => {
        const cx = mL + slot * i + slot / 2;
        d += (i === 0 ? "M" : "L") + cx.toFixed(1) + " " + y(v).toFixed(1) + " ";
      });
      const path = svgEl("path", { d: d.trim(), fill: "none" });
      path.setAttribute("class", "series-line");
      svg.appendChild(path);

      const cross = svgEl("line", { x1: 0, x2: 0, y1: mT, y2: mT + plotH, class: "crosshair" });
      cross.style.display = "none";
      svg.appendChild(cross);

      list.forEach((r, i) => {
        const hit = svgEl("rect", { x: mL + slot * i, y: mT, width: Math.max(1, slot), height: plotH, fill: "transparent" });
        hit.addEventListener("pointermove", (ev) => {
          const cx = mL + slot * i + slot / 2;
          cross.setAttribute("x1", cx);
          cross.setAttribute("x2", cx);
          cross.style.display = "block";
          tipShow(stampLabel(r.bucket_start, bucket), tipFor(r, values[i]));
          tipMove(ev);
        });
        hit.addEventListener("pointerleave", () => {
          cross.style.display = "none";
          tipHide();
        });
        svg.appendChild(hit);
      });
      if (values[peak] > 0) {
        svg.appendChild(
          textEl("text", valueFmt(values[peak]), {
            x: Math.min(W - mR - 4, mL + slot * peak + slot / 2),
            y: Math.max(mT + 10, y(values[peak]) - 8),
            "text-anchor": "middle",
            class: "bar-label",
          }),
        );
      }
    }

    box.appendChild(svg);
    holder.appendChild(box);
  }

  window.HiveCharts = {
    // tokens
    SVG_NS: SVG_NS,
    PALETTE: PALETTE,
    OTHER_COLOR: OTHER_COLOR,
    STATUS_COLOR: STATUS_COLOR,
    P50_COLOR: P50_COLOR,
    P95_COLOR: P95_COLOR,
    MAX_BAR_BUCKETS: MAX_BAR_BUCKETS,
    // page-wide entity order
    setEntityOrder: setEntityOrder,
    orderFor: orderFor,
    providerColor: providerColor,
    foldedName: foldedName,
    chartSeries: chartSeries,
    // DOM
    el: el,
    svgEl: svgEl,
    textEl: textEl,
    // formatting
    fmtInt: fmtInt,
    fmtTokens: fmtTokens,
    fmtMs: fmtMs,
    fmtPct: fmtPct,
    hitRate: hitRate,
    shortHash: shortHash,
    niceMax: niceMax,
    dayLabel: dayLabel,
    hourLabel: hourLabel,
    bucketLabel: bucketLabel,
    stampLabel: stampLabel,
    // interaction
    tipShow: tipShow,
    tipMove: tipMove,
    tipHide: tipHide,
    bindHover: bindHover,
    bindActivate: bindActivate,
    legendEl: legendEl,
    dataTable: dataTable,
    // builders
    stackedDaily: stackedDaily,
    bars: bars,
    latencyChart: latencyChart,
    singleSeries: singleSeries,
  };
})();
