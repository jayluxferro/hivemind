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

  /* The payload is stored on the node and read at pointer time, not captured in
   * the listener. That is what lets a refresh re-point a mark at the new row
   * (values change every tick while the node stays) without stacking a second
   * listener on it. */
  function setHover(node, title, rows) {
    node._tip = { title: title, rows: rows };
  }

  function bindHover(node, title, rows) {
    setHover(node, title, rows);
    if (node._hoverBound) return;
    node._hoverBound = true;
    node.addEventListener("pointermove", (ev) => {
      if (!node._tip) return;
      tipShow(node._tip.title, node._tip.rows);
      tipMove(ev);
    });
    node.addEventListener("pointerleave", tipHide);
  }

  function setActivate(node, fn) {
    node._activate = typeof fn === "function" ? fn : null;
  }

  // Attach once, re-point forever. Same reason as the tooltip payload: after a
  // morph the handler has to describe the *new* row, and re-binding would leave
  // the old listener running too.
  function bindRetargetable(node, type, slot, fn) {
    node[slot] = typeof fn === "function" ? fn : null;
    const flag = slot + "Bound";
    if (node[flag]) return;
    node[flag] = true;
    node.addEventListener(type, (ev) => {
      const handler = node[slot];
      if (typeof handler === "function") handler(ev);
    });
  }

  function bindMove(node, fn) {
    bindRetargetable(node, "pointermove", "_move", fn);
  }

  function bindLeave(node, fn) {
    bindRetargetable(node, "pointerleave", "_leave", fn);
  }

  // Make a mark navigable: pointer click plus keyboard (Enter/Space), because
  // the alternative is a drill-down only a mouse can reach.
  function bindActivate(node, fn) {
    setActivate(node, fn);
    if (!node._activate) return;
    node.style.cursor = "pointer";
    if (node._activateBound) return;
    node._activateBound = true;
    const run = (ev) => {
      // Re-read at event time: after a morph the target is the *new* row, and
      // a stale closure here would open last refresh's request.
      if (typeof node._activate === "function") node._activate(ev);
    };
    node.addEventListener("click", run);
    node.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " " || ev.key === "Spacebar") {
        ev.preventDefault();
        run(ev);
      }
    });
  }

  /* =====================================================================
   * in-place update machinery
   *
   * A refresh must not look like a reload. The rule this implements: a *new*
   * picture (different series set, different row count, different chart kind)
   * is allowed to rebuild — and fades in when it does. The *same* picture with
   * new numbers morphs: the marks keep their identity and only their geometry
   * changes, so the CSS transitions in dashboard.css have a previous value to
   * interpolate from.
   *
   * "Same" is decided by a structural key that names everything affecting how
   * many nodes exist. Everything else — values, labels, tooltip payloads,
   * drill-down targets — is allowed to differ while the key stays equal, and is
   * written into the existing nodes.
   *
   * The plan is built as plain data (an array of mark specs) *before* any DOM
   * exists, so the build path and the morph path share one geometry
   * computation. Two paths that each laid out their own marks would drift.
   * =================================================================== */

  function prefersReducedMotion() {
    try {
      return !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
    } catch (err) {
      return false; // no matchMedia: assume motion is welcome
    }
  }

  function clearChildren(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  // A tween on an element nobody can see is a rAF loop for no reason.
  function isVisible(node) {
    if (!node || node.hidden) return false;
    if (typeof node.getClientRects === "function") {
      try {
        return node.getClientRects().length > 0;
      } catch (err) {
        return true;
      }
    }
    return true;
  }

  /* Number tween for tiles. The from-value is the number currently on screen,
   * not the caller's idea of it: a refresh landing mid-tween must continue from
   * where the digits actually are, or the value jumps backwards first. So the
   * live value rides on the element (`node._num`, written every frame) and the
   * `fromNum` argument is only the fallback for the first tween. */
  const TWEENS = new WeakMap(); // node -> rAF id, so the next tween can cancel it

  function tweenCancel(node) {
    const id = TWEENS.get(node);
    if (id !== undefined && id !== null) {
      if (typeof window.cancelAnimationFrame === "function") window.cancelAnimationFrame(id);
      TWEENS.delete(node);
    }
  }

  function tweenText(node, fromNum, toNum, fmt, opts) {
    const o = opts || {};
    const format = fmt || ((v) => String(Math.round(v)));
    const to = toNum == null || !isFinite(toNum) ? null : Number(toNum);
    tweenCancel(node);
    // Nothing to interpolate towards (an em dash is not a number), no motion
    // wanted, or nobody looking: write the final text and stop.
    if (to === null || prefersReducedMotion() || !isVisible(node) || typeof window.requestAnimationFrame !== "function") {
      node.textContent = to === null ? format(null) : format(to);
      node._num = to;
      return;
    }
    const live = typeof node._num === "number" && isFinite(node._num) ? node._num : null;
    const raw = live !== null ? live : fromNum;
    const from = raw == null || !isFinite(raw) ? null : Number(raw);
    if (from === null || from === to) {
      node.textContent = format(to);
      node._num = to;
      return;
    }
    const dur = o.duration || 500;
    const t0 = typeof performance !== "undefined" && performance.now ? performance.now() : Date.now();
    const step = (now) => {
      const t = Math.min(1, (now - t0) / dur);
      const eased = 1 - Math.pow(1 - t, 3); // ease-out cubic
      const v = from + (to - from) * eased;
      node.textContent = format(v);
      node._num = t >= 1 ? to : v;
      if (t < 1 && TWEENS.has(node)) {
        TWEENS.set(node, window.requestAnimationFrame(step));
      } else {
        TWEENS.delete(node);
        node.textContent = format(to);
        node._num = to;
      }
    };
    TWEENS.set(node, window.requestAnimationFrame(step));
  }

  // A one-shot class for the enter animation, removed on a timer rather than on
  // animationend: the class must not survive an interrupted animation, and a
  // timer is the one signal that always fires.
  function fadeIn(node, cls) {
    if (!node || prefersReducedMotion()) return;
    const name = cls || "fade-in";
    node.classList.add(name);
    window.setTimeout(() => node.classList.remove(name), 600);
  }

  /* ---- marks -----------------------------------------------------------------
   * spec: {tag, attrs, cls, fill, text, tip:{title,rows}, activate}
   * `text` always lands through textContent. This is the only path by which a
   * model name or an agent hash reaches the DOM in this file. */
  function buildMark(spec) {
    const node = spec.tag === "text" ? textEl(spec.tag, spec.text, spec.attrs) : svgEl(spec.tag, spec.attrs);
    if (spec.cls !== undefined) node.setAttribute("class", spec.cls);
    if (spec.fill) node.style.fill = spec.fill;
    if (spec.text !== undefined && spec.tag !== "text") node.textContent = spec.text;
    if (spec.tip) bindHover(node, spec.tip.title, spec.tip.rows);
    if (spec.activate) bindActivate(node, spec.activate);
    if (spec.move !== undefined) bindMove(node, spec.move);
    if (spec.leave !== undefined) bindLeave(node, spec.leave);
    return node;
  }

  function applyMark(node, spec) {
    const attrs = spec.attrs || {};
    for (const k in attrs) {
      if (!Object.prototype.hasOwnProperty.call(attrs, k)) continue;
      // Setting an attribute to the value it already holds is a no-op for
      // transitions (the computed value does not change), so there is nothing
      // to diff here.
      if (node.getAttribute(k) !== String(attrs[k])) node.setAttribute(k, attrs[k]);
    }
    if (spec.cls !== undefined && node.getAttribute("class") !== spec.cls) node.setAttribute("class", spec.cls);
    if (spec.fill && node.style.fill !== spec.fill) node.style.fill = spec.fill;
    if (spec.text !== undefined && node.textContent !== spec.text) node.textContent = spec.text;
    if (spec.tip) setHover(node, spec.tip.title, spec.tip.rows);
    if (spec.activate !== undefined) setActivate(node, spec.activate);
    if (spec.move !== undefined) bindMove(node, spec.move);
    if (spec.leave !== undefined) bindLeave(node, spec.leave);
  }

  // The chart frame: a .subchart box with its heading, optional legend and the
  // svg. Rebuilt only when the structural key says the picture changed shape.
  function sceneFrame(holder, key, title, legendItems, ariaLabel, viewBox) {
    const prev = holder._scene;
    if (prev && prev.key === key) {
      if (prev.svg.getAttribute("aria-label") !== ariaLabel) prev.svg.setAttribute("aria-label", ariaLabel);
      return prev;
    }
    clearChildren(holder);
    const root = el("div", "subchart");
    root.appendChild(el("h3", null, title));
    if (legendItems && legendItems.length >= 2) root.appendChild(legendEl(legendItems));
    const svg = svgEl("svg", { viewBox: viewBox });
    svg.setAttribute("aria-label", ariaLabel);
    root.appendChild(svg);
    holder.appendChild(root);
    fadeIn(root);
    holder._scene = { key: key, root: root, svg: svg, nodes: [] };
    return holder._scene;
  }

  // Draw (or morph) the marks of a scene. `key` must change whenever
  // plan.length would; everything else may change freely.
  function renderScene(holder, key, title, legendItems, ariaLabel, viewBox, plan) {
    const scene = sceneFrame(holder, key, title, legendItems, ariaLabel, viewBox);
    if (scene.nodes.length === plan.length) {
      for (let i = 0; i < plan.length; i++) applyMark(scene.nodes[i], plan[i]);
      return scene;
    }
    // Shape changed under the same key (a builder bug, or a legend-less
    // single-series chart whose mark count moved): rebuild rather than
    // mis-align marks onto the wrong rows.
    clearChildren(scene.svg);
    scene.nodes = plan.map((spec) => {
      const node = buildMark(spec);
      scene.svg.appendChild(node);
      return node;
    });
    fadeIn(scene.root);
    return scene;
  }

  // The "nothing to draw here" state, itself a scene so a view that stays empty
  // across refreshes does not rebuild the paragraph every tick.
  function emptyScene(holder, title, text) {
    const key = "empty|" + title + "|" + text;
    const prev = holder._scene;
    if (prev && prev.key === key) return;
    clearChildren(holder);
    const root = el("div", "subchart");
    root.appendChild(el("h3", null, title));
    root.appendChild(el("p", "empty", text));
    holder.appendChild(root);
    fadeIn(root);
    holder._scene = { key: key, root: root, svg: null, nodes: [] };
  }

  // The legend is part of a chart's structure: if the named set changes, the
  // picture changed shape and the scene has to rebuild — which is also the only
  // way a legend can appear or disappear (sceneFrame draws one iff >= 2 series).
  function seenLegend(items) {
    return items && items.length >= 2 ? items.map((it) => it.name).join(">") : "";
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
  /* Cells are written by applyRow() rather than constructed by a makeCell()
   * helper: a first build and a morph have to agree on exactly what a cell is,
   * and the only way to guarantee that is to have one function do both. A bare
   * string/number is the shorthand; presentation that matters (the monospace
   * hash column) is asked for explicitly, never guessed from the value's shape.
   */
  function headerKey(caption, headers) {
    let key = String(caption == null ? "" : caption);
    for (const h of headers || []) {
      key +=
        "|" +
        (h !== null && typeof h === "object"
          ? [h.text, h.cls, h.title, h.ariaSort].join("~")
          : String(h));
    }
    return key;
  }

  // A changed cell gets a brief highlight. The flash marks *new information*,
  // so it is driven by the text differing — not by the fact that a render
  // happened. A refresh that changed nothing flashes nothing.
  function flash(node, cls) {
    const name = cls || "cell-changed";
    if (!node || prefersReducedMotion()) return;
    node.classList.remove(name);
    // Reading offsetWidth forces a style flush, without which removing and
    // re-adding the class in one frame does not restart the animation. Guarded
    // because a non-layout environment has no offsetWidth to read.
    if (typeof node.offsetWidth === "number") void node.offsetWidth;
    node.classList.add(name);
    window.setTimeout(() => node.classList.remove(name), 600);
  }

  function applyHeader(th, h) {
    const obj = h !== null && typeof h === "object";
    const text = obj ? h.text : h;
    const s = text == null ? "" : String(text);
    if (th.textContent !== s) th.textContent = s;
    th.setAttribute("class", obj ? h.cls || "" : "");
    th.title = obj && h.title ? h.title : "";
    const ariaSort = obj ? h.ariaSort || null : null;
    if (ariaSort) th.setAttribute("aria-sort", ariaSort);
    else th.removeAttribute("aria-sort");
    if (obj && h.onClick) {
      if (!th._activateBound) {
        // Focusable so the sort is reachable without a mouse. Not
        // role="button": the th has to stay a table header, which is what
        // aria-sort is for.
        th.setAttribute("tabindex", "0");
        bindActivate(th, h.onClick);
      } else {
        // Morph: the handler closes over the state it was built with, and the
        // range can have moved since. Re-point it at the current one.
        setActivate(th, h.onClick);
      }
    }
  }

  function applyRow(slot, spec, cols, fresh) {
    const obj = spec !== null && typeof spec === "object" && !Array.isArray(spec);
    const cells = obj ? spec.cells || [] : spec || [];
    const tr = slot.tr;
    const cls = obj && spec.cls ? spec.cls : "";
    if (tr.getAttribute("class") !== (cls || null)) tr.setAttribute("class", cls);
    const label = obj && spec.label ? spec.label : null;
    if (label) tr.setAttribute("aria-label", label);
    else tr.removeAttribute("aria-label");
    if (obj && spec.onClick) {
      if (!tr._activateBound) {
        // Clickable *and* focusable: a drill-down that only a mouse can reach
        // is a drill-down half the operators do not have.
        tr.classList.add("clickable");
        tr.setAttribute("tabindex", "0");
        bindActivate(tr, spec.onClick);
      } else {
        setActivate(tr, spec.onClick);
      }
    } else if (tr._activateBound) {
      setActivate(tr, null);
      tr.classList.remove("clickable");
      tr.removeAttribute("tabindex");
    }
    for (let c = 0; c < cols; c++) {
      const cell = cells[c];
      const td = slot.cells[c];
      const cobj = cell !== null && typeof cell === "object";
      const value = cobj ? cell.text : cell;
      const s = value == null ? "" : String(value);
      if (td.textContent !== s) {
        td.textContent = s;
        if (!fresh) flash(td);
      }
      td.setAttribute("class", cobj ? cell.cls || "" : "");
      const cellTitle = cobj && cell.title ? cell.title : "";
      if (td.title !== cellTitle) td.title = cellTitle;
    }
  }

  /* The table gets the same holder-keyed treatment as a chart: the grid keeps
   * its nodes whenever the caption and headers are unchanged, and only the
   * cells whose text actually moved are rewritten. Rows are reconciled by
   * count, so a table that gained a row does not rebuild — and does not flash
   * every cell either. */
  function dataTable(holder, caption, headers, rows) {
    const list = headers || [];
    const key = headerKey(caption, list);
    let entry = holder._table;
    if (!entry || entry.key !== key || entry.cols !== list.length) {
      clearChildren(holder);
      const table = el("table", "datatable");
      if (caption) table.appendChild(el("caption", null, caption));
      const headRow = el("tr");
      const cells = [];
      for (let c = 0; c < list.length; c++) {
        const th = el("th");
        headRow.appendChild(th);
        cells.push(th);
      }
      const thead = el("thead");
      thead.appendChild(headRow);
      table.appendChild(thead);
      const tbody = el("tbody");
      table.appendChild(tbody);
      holder.appendChild(table);
      fadeIn(table);
      entry = { key: key, cols: list.length, table: table, cells: cells, tbody: tbody, rows: [] };
      holder._table = entry;
    }
    for (let c = 0; c < list.length; c++) applyHeader(entry.cells[c], list[c]);

    const body = rows || [];
    while (entry.rows.length > body.length) {
      const gone = entry.rows.pop();
      entry.tbody.removeChild(gone.tr);
    }
    while (entry.rows.length < body.length) {
      const tr = el("tr");
      const cells = [];
      for (let c = 0; c < entry.cols; c++) {
        const td = el("td");
        tr.appendChild(td);
        cells.push(td);
      }
      entry.tbody.appendChild(tr);
      // `fresh` suppresses the per-cell flash for a row that did not exist a
      // moment ago: the whole row is new information, and sixteen flashes on
      // one arrival is noise, not signal.
      entry.rows.push({ tr: tr, cells: cells, fresh: true });
    }
    for (let i = 0; i < body.length; i++) {
      const slot = entry.rows[i];
      applyRow(slot, body[i], entry.cols, slot.fresh);
      slot.fresh = false;
    }
    return entry.table;
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

    if (!stamps.length) {
      emptyScene(holder, title, "No telemetry in this window yet.");
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

    const legendItems =
      series.length >= 2 ? series.map((s) => ({ name: legendLabel(s), color: providerColor(s, order) })) : null;

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
    const labelEvery = Math.max(1, Math.ceil(stamps.length / 10));

    // Structural key: everything that decides how many marks the scene has.
    // Values, labels, hues and drill-down targets may all change under it —
    // those are written into the existing marks.
    const key = ["stacked", title, bucket, series.join(">"), stamps.length].join("|");

    const plan = [];
    for (let i = 1; i <= 4; i++) {
      const t = (max / 4) * i;
      plan.push({ tag: "line", attrs: { x1: mL, x2: W - mR, y1: y(t), y2: y(t), class: "grid" } });
      plan.push({
        tag: "text",
        text: valueFmt(t),
        attrs: { x: mL - 8, y: y(t) + 3.5, "text-anchor": "end", class: "tick-label" },
      });
    }
    stamps.forEach((stamp, i) => {
      if (i % labelEvery === 0) {
        plan.push({
          tag: "text",
          text: bucketLabel(stamp, bucket),
          attrs: { x: mL + slot * i + slot / 2, y: H - 14, "text-anchor": "middle", class: "tick-label" },
        });
      }
    });

    stamps.forEach((stamp, i) => {
      const x0 = mL + slot * i;
      const byStamp = per.get(stamp);
      let acc = 0;
      for (const s of series) {
        const entry = byStamp.get(s);
        const v = entry ? entry.value : 0;
        const yTop = y(acc + v);
        const yBot = y(acc);
        const color = providerColor(s, order);
        const label = s === "Other" ? "Other (folded)" : s;
        // A zero-value segment is still emitted, at zero height. Skipping it
        // would make the mark count a function of the *values*, and a refresh
        // that pushed one agent to zero would then rebuild the chart instead of
        // letting its column shrink — exactly the flash this avoids. Height 0
        // paints nothing, and its hit target is 0-tall so it stays unhoverable.
        plan.push({
          tag: "rect",
          attrs: {
            x: x0 + (slot - barW) / 2,
            y: yTop + 1,
            width: barW,
            height: Math.max(0, yBot - yTop - 2), // 2px surface gap between segments
            rx: Math.min(4, barW / 2),
          },
          fill: color,
        });

        // Hit target spans the whole column slot and the segment height —
        // always larger than the painted mark. Drawn after the mark so it wins
        // the pointer events.
        plan.push({
          tag: "rect",
          attrs: {
            x: x0,
            y: yTop,
            width: Math.max(1, slot),
            height: v > 0 ? Math.max(1, yBot - yTop) : 0,
            fill: "transparent",
          },
          tip: {
            title: stampLabel(stamp, bucket) + " · " + label,
            rows: [
              { swatch: color, label: "agent", value: label },
              { label: "requests", value: fmtInt(entry ? entry.requests : 0) },
              {
                label: "tokens in/out",
                value: fmtInt(entry ? entry.tokensIn : 0) + " / " + fmtInt(entry ? entry.tokensOut : 0),
              },
              { label: "errors", value: fmtInt(entry ? entry.errors : 0) },
            ],
          },
          // Only an unfolded series may be drilled into: "Other" is an
          // aggregate, and navigating from it would silently pick one of its
          // members.
          activate: onSegmentClick && entry && entry.raw ? () => onSegmentClick(entry.raw, stamp, entry) : null,
        });
        acc += v;
      }
      // One direct label per chart: the endpoint column's total, never an
      // interior segment (SPEC: selective direct labels). Emitted always at
      // zero height so the count stays fixed; blank when there is no total.
      if (i === stamps.length - 1) {
        plan.push({
          tag: "text",
          text: acc > 0 ? valueFmt(acc) : "",
          attrs: { x: x0 + slot / 2, y: y(acc) - 6, "text-anchor": "middle", class: "bar-label" },
        });
      }
    });

    renderScene(holder, key, title, legendItems, title + " — stacked columns", "0 0 " + W + " " + H, plan);
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

    if (!list.length) {
      emptyScene(holder, title, "No rows in this window.");
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

    let legendItems = null;
    if (colorKey) {
      const seen = [];
      for (const r of list) {
        const raw = r[colorKey];
        if (raw === undefined || raw === null) continue;
        const name = legendName ? legendName(raw) : raw;
        if (!seen.some((entry) => entry.name === name)) seen.push({ name: name, color: colorFn(raw, r) });
      }
      legendItems = seen;
    }

    const key = ["bars", title, valueKey, colorKey || "", subLabelKey || "", list.length, seenLegend(legendItems)].join("|");

    const plan = [];
    for (let i = 1; i <= 4; i++) {
      const t = (max / 4) * i;
      plan.push({ tag: "line", attrs: { x1: x(t), x2: x(t), y1: mT - 4, y2: H - 20, class: "grid" } });
      plan.push({
        tag: "text",
        text: valueFmt(t),
        attrs: { x: x(t), y: H - 6, "text-anchor": "middle", class: "tick-label" },
      });
    }

    list.forEach((r, i) => {
      const cy = mT + i * rowH + rowH / 2;
      const value = Number(r[valueKey]) || 0;
      const color = colorFn(colorKey ? r[colorKey] : r[labelKey], r);
      plan.push({
        tag: "text",
        text: labelFmt(r[labelKey]),
        attrs: { x: labelW - 8, y: subLabelKey ? cy : cy + 3.5, "text-anchor": "end", class: rowLabelClass },
      });
      if (subLabelKey) {
        plan.push({
          tag: "text",
          text: labelFmt(r[subLabelKey]),
          attrs: { x: labelW - 8, y: cy + 13, "text-anchor": "end", class: "tick-label" },
        });
      }
      plan.push({
        tag: "rect",
        attrs: { x: x0, y: cy - 7, width: Math.max(2, x(value) - x0), height: 14, rx: Math.min(4, 7) },
        fill: color,
      });

      // Hit target: the whole row band right of the labels — taller and wider
      // than the 14px painted bar.
      plan.push({
        tag: "rect",
        attrs: { x: x0, y: cy - 12, width: Math.max(1, plotW + mR), height: 24, fill: "transparent" },
        tip: {
          title: labelFmt(r[labelKey]),
          rows: o.tipRows ? o.tipRows(r, value) : [{ label: valueKey, value: valueFmt(value) }],
        },
        activate: onRowClick ? () => onRowClick(r) : null,
      });

      // Value at the tip: every ranked bar is an endpoint, so every bar is
      // directly labeled.
      plan.push({
        tag: "text",
        text: valueFmt(value),
        attrs: { x: x(value) + 6, y: cy + 3.5, class: "bar-label" },
      });
    });

    renderScene(holder, key, title, legendItems, title + " — horizontal bars", "0 0 " + W + " " + H, plan);
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

    if (!list.length) {
      emptyScene(holder, title, "No latency rows in this window.");
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

    const legendItems = [
      { name: "p95", color: P95_COLOR },
      { name: "p50", color: P50_COLOR },
    ];
    const key = ["latency", title, nameKey, subLabelKey || "", list.length].join("|");

    const plan = [];
    for (let i = 1; i <= 4; i++) {
      const t = (max / 4) * i;
      plan.push({ tag: "line", attrs: { x1: x(t), x2: x(t), y1: mT - 4, y2: H - 20, class: "grid" } });
      plan.push({
        tag: "text",
        text: fmtMs(t),
        attrs: { x: x(t), y: H - 6, "text-anchor": "middle", class: "tick-label" },
      });
    }

    list.forEach((r, i) => {
      const top = mT + i * rowH;
      const name = r[nameKey];
      const nameText = name == null ? "—" : String(name);
      plan.push({
        tag: "text",
        text: nameText,
        attrs: { x: labelW - 8, y: subLabelKey ? top + 20 : top + 23, "text-anchor": "end", class: "row-label" },
      });
      if (subLabelKey && r[subLabelKey] != null) {
        plan.push({
          tag: "text",
          text: String(r[subLabelKey]),
          attrs: { x: labelW - 8, y: top + 33, "text-anchor": "end", class: "tick-label" },
        });
      }

      // p50 and p95 share one baseline; 2px surface gap between the pair.
      const bar = (value, color, series, yOff) => {
        const v = Number(value) || 0;
        const bw = Math.max(2, x(v) - x0);
        plan.push({
          tag: "rect",
          attrs: { x: x0, y: top + yOff, width: bw, height: 13, rx: Math.min(4, 6.5) },
          fill: color,
        });
        plan.push({
          tag: "rect",
          attrs: { x: x0, y: top + yOff - 4, width: Math.max(1, plotW + mR), height: 21, fill: "transparent" },
          tip: {
            title: nameText + " · " + series,
            rows: [
              { label: "requests", value: fmtInt(r.requests) },
              { swatch: color, label: series, value: fmtMs(value) },
            ],
          },
        });
        return bw;
      };
      const p95Width = bar(r.p95_ms, P95_COLOR, "p95", 4);
      const p50Width = bar(r.p50_ms, P50_COLOR, "p50", 19);

      // p50 sits inside its bar, p95 at the tip when it fits, else inside.
      // Both label marks are always emitted — the p50 one goes blank rather
      // than absent when the bar is too short to hold it, so the mark count
      // does not depend on the numbers and a refresh never has to rebuild.
      plan.push({
        tag: "text",
        text: p50Width > 40 ? fmtMs(r.p50_ms) : "",
        attrs: { x: x0 + 6, y: top + 29, class: "bar-label-in" },
      });
      plan.push({
        tag: "text",
        text: fmtMs(r.p95_ms),
        attrs: p95Width > 40
          ? { x: x0 + p95Width + 6, y: top + 14, class: "bar-label" }
          : { x: x0 + 6, y: top + 14, class: "bar-label-in" },
      });
    });

    renderScene(holder, key, title, legendItems, title + " — grouped bars by " + nameKey, "0 0 " + W + " " + H, plan);
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

    if (!list.length) {
      emptyScene(holder, title, "No telemetry in this window yet.");
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

    const slot = plotW / list.length;
    const labelEvery = Math.max(1, Math.ceil(list.length / 10));
    const barsMode = list.length <= MAX_BAR_BUCKETS;

    let peak = 0;
    values.forEach((v, i) => {
      if (v > values[peak]) peak = i;
    });

    const key = ["series", title, bucket, valueLabel, barsMode ? "bars" : "line", list.length].join("|");
    const plan = [];
    // Index of the shared crosshair mark in the plan, so the per-bucket hover
    // handlers can move it. Its position is fixed by construction, which keeps
    // it correct across a morph (the node at that index is the same node).
    let crossIdx = -1;
    for (let i = 1; i <= 4; i++) {
      const t = (max / 4) * i;
      plan.push({ tag: "line", attrs: { x1: mL, x2: W - mR, y1: y(t), y2: y(t), class: "grid" } });
      plan.push({
        tag: "text",
        text: valueFmt(t),
        attrs: { x: mL - 8, y: y(t) + 3.5, "text-anchor": "end", class: "tick-label" },
      });
    }
    list.forEach((r, i) => {
      if (i % labelEvery === 0) {
        plan.push({
          tag: "text",
          text: bucketLabel(r.bucket_start, bucket),
          attrs: { x: mL + slot * i + slot / 2, y: H - 14, "text-anchor": "middle", class: "tick-label" },
        });
      }
    });

    if (barsMode) {
      const barW = Math.min(24, Math.max(4, slot * 0.62));
      list.forEach((r, i) => {
        const v = values[i];
        const x0 = mL + slot * i;
        // Zero buckets keep their mark at zero height: the count stays a
        // function of the bucket count alone, so a refresh that zeroes one
        // bucket shrinks it instead of rebuilding the chart.
        plan.push({
          tag: "rect",
          attrs: {
            x: x0 + (slot - barW) / 2,
            y: y(v),
            width: barW,
            height: Math.max(0, mT + plotH - y(v)),
            rx: Math.min(4, barW / 2),
          },
          fill: PALETTE[0],
        });
        // Full-height, full-slot target: a 4px column is not a hover target.
        plan.push({
          tag: "rect",
          attrs: { x: x0, y: mT, width: Math.max(1, slot), height: plotH, fill: "transparent" },
          tip: { title: stampLabel(r.bucket_start, bucket), rows: tipFor(r, v) },
        });
      });
    } else {
      // Thin line, no markers (hundreds of them would be a texture, not data),
      // plus a crosshair so a bucket is still individually addressable.
      let d = "";
      values.forEach((v, i) => {
        const cx = mL + slot * i + slot / 2;
        d += (i === 0 ? "M" : "L") + cx.toFixed(1) + " " + y(v).toFixed(1) + " ";
      });
      plan.push({ tag: "path", attrs: { d: d.trim(), fill: "none", class: "series-line" } });
      crossIdx = plan.length;
      plan.push({
        tag: "line",
        attrs: { x1: 0, x2: 0, y1: mT, y2: mT + plotH, class: "crosshair", style: "display: none" },
      });
      list.forEach((r, i) => {
        plan.push({
          tag: "rect",
          attrs: { x: mL + slot * i, y: mT, width: Math.max(1, slot), height: plotH, fill: "transparent" },
          tip: { title: stampLabel(r.bucket_start, bucket), rows: tipFor(r, values[i]) },
          // The crosshair is shared by every bucket, so its position is
          // re-pointed per hover — and survives a morph, because the handler
          // reads the scene rather than closing over this render's row.
          move: (ev) => {
            const cross = holder._scene && holder._scene.nodes[crossIdx];
            if (cross) {
              const cx = mL + slot * i + slot / 2;
              cross.setAttribute("x1", cx);
              cross.setAttribute("x2", cx);
              cross.style.display = "block";
            }
            tipMove(ev);
          },
          leave: () => {
            const cross = holder._scene && holder._scene.nodes[crossIdx];
            if (cross) cross.style.display = "none";
          },
        });
      });
    }
    // One direct label per chart: the peak bucket. Emitted in both modes and
    // blanked when there is nothing to say, so the count never moves.
    plan.push({
      tag: "text",
      text: values[peak] > 0 ? valueFmt(values[peak]) : "",
      attrs: {
        x: Math.min(W - mR - 4, mL + slot * peak + slot / 2),
        y: Math.max(mT + 10, y(values[peak]) - (barsMode ? 6 : 8)),
        "text-anchor": "middle",
        class: "bar-label",
      },
    });

    renderScene(holder, key, title, null, title + " — activity over time", "0 0 " + W + " " + H, plan);
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
    // in-place update
    tweenText: tweenText,
    tweenCancel: tweenCancel,
    fadeIn: fadeIn,
    flash: flash,
    prefersReducedMotion: prefersReducedMotion,
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
