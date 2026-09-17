"""Telemetry dashboard shell + static assets served at ``/_telemetry``.

Phase 2 split the old single-file page: this module serves a small HTML shell
and the three real assets next to it (``static/dashboard.css``, ``charts.js``,
``dashboard.js``) from the installed package, so the page is offline-only —
no CDN, no build step, no framework.  The shell carries zero inline CSS/JS and
zero absolute URLs; the assets are looked up through :data:`STATIC_ASSETS`
(a filename whitelist: user input never reaches a filesystem path) and are
cache-busted with ``?v=<sha256[:8]>`` computed from their contents, so a
changed asset is fetched without anyone remembering to bump a version.

Visual rules (dataviz method, pinned down for this SPEC) — these live in
``static/charts.js`` now, and are listed here because they are the contract
the assets are written against:

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
- the primary dimension is AGENT usage over time — provider is only shown
  where it still carries signal (model/latency cards).  Cost is NOT
  displayed anywhere: pricing is not maintained, so showing dollars would be
  guessing under a confident number.

Auto-refresh is opt-in in Phase 2: a checkbox, default OFF, persisted in
``localStorage["hivemindTelemetryAutoRefresh"]`` behind try/catch helpers
(private-mode safe).  The page loads once and stays on screen until the
operator asks for a refresh — the 10s always-on poll is gone.
"""

from __future__ import annotations

import hashlib
from importlib import resources

#: Package the assets ship in (resolved via importlib.resources so a wheel
#: works the same as a checkout).
_PACKAGE = "hivemind.telemetry"
_STATIC_DIR = "static"

#: Filename whitelist -> content type.  Doubles as the directory listing: a
#: name that is not a key here is a 404, whatever the filesystem holds.
STATIC_ASSETS: dict[str, str] = {
    "dashboard.css": "text/css; charset=utf-8",
    "charts.js": "text/javascript; charset=utf-8",
    "dashboard.js": "text/javascript; charset=utf-8",
}

# Read-once cache: assets are immutable for the life of the process, and a
# download of the page should never touch the filesystem twice for the same
# file.
_STATIC_CACHE: dict[str, tuple[bytes, str]] = {}


def read_static(filename: str) -> tuple[bytes, str] | None:
    """``(bytes, content_type)`` for a whitelisted asset, else None.

    ``importlib.resources.files`` is what makes this wheel-safe; the whitelist
    is what makes it safe at all (a traversal or a stray name can never name a
    file, because only three names are keys).
    """
    content_type = STATIC_ASSETS.get(filename)
    if content_type is None:
        return None
    cached = _STATIC_CACHE.get(filename)
    if cached is not None:
        return cached
    try:
        data = resources.files(_PACKAGE).joinpath(_STATIC_DIR, filename).read_bytes()
    except (FileNotFoundError, OSError, ModuleNotFoundError):
        # A broken install must render the page's error state, not a 500.
        return None
    entry = (data, content_type)
    _STATIC_CACHE[filename] = entry
    return entry


def _compute_static_version() -> str:
    """Content hash of every asset, in whitelist order, truncated to 8 chars.

    Content-addressed on purpose: the shell is served with a long-lived
    ``immutable`` cache header, so the version has to change exactly when a
    byte of CSS or JS changes — and only then.
    """
    digest = hashlib.sha256()
    for name in STATIC_ASSETS:
        entry = read_static(name)
        digest.update(entry[0] if entry is not None else b"")
    return digest.hexdigest()[:8]


#: Cache-buster appended to every asset URL in the shell (``?v=…``).
STATIC_VERSION = _compute_static_version()


def render_page() -> str:
    """The dashboard shell: document head, header, empty controls, main, tooltip.

    Deliberately empty of behavior — ``static/dashboard.js`` owns the views,
    the hash state and the header controls (which is why the controls div
    ships empty), and it is the only thing that ever writes into ``#main``.
    """
    version = STATIC_VERSION
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hivemind — telemetry</title>
<link rel="stylesheet" href="/_telemetry/static/dashboard.css?v={version}">
</head>
<body>
<header>
  <h1>Hivemind token ledger</h1>
  <div class="controls" id="controls"></div>
</header>

<main id="main"><p class="empty">Loading telemetry&hellip;</p></main>

<noscript>
  <p class="empty">The telemetry dashboard needs JavaScript. The raw data is at
  <code>/_telemetry/data</code> (JSON) and <code>/_telemetry/export.jsonl</code>.</p>
</noscript>

<div id="tooltip" role="tooltip"></div>

<script defer src="/_telemetry/static/charts.js?v={version}"></script>
<script defer src="/_telemetry/static/dashboard.js?v={version}"></script>
</body>
</html>
"""
