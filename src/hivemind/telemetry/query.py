"""Pure query-string parsing for the telemetry analytics endpoints (no I/O).

The HTTP handlers stay dumb: they hand ``request.query_params`` to these
functions and pass the results straight to the ledger.  Everything here is
deterministic given the params and the wall clock — no connection, no logging,
nothing that can raise on a bad URL.

Two rules shape the API below:

- **The display range is inclusive.**  ``parse_range`` returns aware-UTC
  ``(from_ts, to_ts)`` where ``to_ts`` is the last day *shown*; the ledger
  converts that to the half-open SQL window via :func:`exclusive_end`.  Keeping
  the conversion out of the handlers is what makes "a bare ``to=2026-09-01``
  covers all of Sept 1" true for every endpoint at once.
- **User input never reaches SQL.**  Sort columns, sort directions and filter
  columns come from the whitelists in ``ledger.py`` (single source of truth);
  a query-string value is only ever used as a dict *key*.  A value that is not
  in the whitelist is dropped, never escaped-and-interpolated.

Lenient by design: a missing, malformed or nonsensical param falls back to the
default window instead of erroring — a dashboard that 500s on a typo'd URL is
worse than one that shows the default range.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from .ledger import (
    _DEFAULT_ORDER,
    _DEFAULT_SORT,
    _REQUEST_FILTERS,
    _REQUEST_ORDERS,
    _REQUEST_SORTS,
)

#: Widest window a read may ask for (SPEC D7).  A year plus a day so that
#: "the last 366 days" is expressible without a special case.
MAX_RANGE_DAYS = 366
#: Window used when a request carries no usable date params.
DEFAULT_RANGE_DAYS = 14
#: Legacy ``days=`` alias clamp (Phase 1 kept [1, 365]).
MAX_DAYS = 365
#: Above this range the ``hour`` bucket is too granular to read — the series
#: endpoint degrades to daily buckets (SPEC API contract).
MAX_HOUR_BUCKET_DAYS = 14
#: Page-size clamps for the requests endpoint (SPEC D8).
MAX_LIMIT = 500
#: Offset clamp: deep pagination is a scan, and nobody pages past this.
MAX_OFFSET = 1_000_000
#: Default page size for the requests endpoint.
DEFAULT_LIMIT = 100

#: Exact-match text filters (``status`` is numeric and handled separately).
_TEXT_FILTERS = ("agent_hash", "model", "provider")

#: Inclusive status-code bounds; anything outside is not an HTTP status.
MIN_STATUS = 100
MAX_STATUS = 599


def _text(params: Mapping[str, Any], name: str) -> str:
    """One param as stripped text (``""`` when absent or None)."""
    value = params.get(name) if hasattr(params, "get") else None
    return "" if value is None else str(value).strip()


def _int(raw: str) -> int | None:
    """Parse a whole number, or None.  Floats ("3.5") are deliberately invalid."""
    if not raw:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def _parse_ts(raw: str) -> datetime | None:
    """ISO 8601 -> aware UTC datetime, or None.

    Accepts ``YYYY-MM-DD`` and full datetimes, with or without a trailing
    ``Z``; naive input is read as UTC (SPEC: "naive inputs treated as UTC") so
    the server's own timezone can never shift a bucket boundary.
    """
    if not raw:
        return None
    text = raw[:-1] + "+00:00" if raw[-1] in ("Z", "z") else raw
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def exclusive_end(to_ts: datetime) -> datetime:
    """The half-open SQL bound for an inclusive display ``to``.

    Display dates are whole days, so the SQL window ends one day after the
    last displayed day: ``[from, to]`` inclusive becomes ``ts >= from AND
    ts < to + 1 day``.
    """
    return to_ts + timedelta(days=1)


def parse_range(params: Mapping[str, Any]) -> tuple[datetime, datetime]:
    """Display range ``(from_ts, to_ts)`` from ``from``/``to`` params.

    Missing or unparseable dates fall back to ``to = now`` and
    ``from = to - DEFAULT_RANGE_DAYS``; a future ``to`` clamps to now; an
    inverted pair swaps; a wider-than-:data:`MAX_RANGE_DAYS` span keeps ``to``
    and moves ``from`` forward (the caller asked for "up to X", which still
    holds after the cap).
    """
    now = datetime.now(timezone.utc)
    raw_from = _parse_ts(_text(params, "from"))
    raw_to = _parse_ts(_text(params, "to"))

    to_ts = min(raw_to if raw_to is not None else now, now)
    from_ts = raw_from if raw_from is not None else to_ts - timedelta(days=DEFAULT_RANGE_DAYS)
    if from_ts > to_ts:
        from_ts, to_ts = to_ts, from_ts
        to_ts = min(to_ts, now)  # an inverted range starting in the future collapses
    if to_ts - from_ts > timedelta(days=MAX_RANGE_DAYS):
        from_ts = to_ts - timedelta(days=MAX_RANGE_DAYS)
    return from_ts, to_ts


def parse_days(params: Mapping[str, Any]) -> tuple[datetime, datetime] | None:
    """The legacy ``days=N`` window as ``(from_ts, to_ts)``, or None.

    None means "this request is not a ``days`` request": either it carries an
    explicit ``from``/``to`` (which always wins — SPEC API contract) or it has
    no ``days`` param at all.  ``days`` itself clamps to [1, :data:`MAX_DAYS`]
    and is ignored when unparseable (the caller then falls through to
    :func:`parse_range`'s default window, exactly like Phase 1).
    """
    if _text(params, "from") or _text(params, "to"):
        return None
    days = _int(_text(params, "days"))
    if days is None:
        return None
    now = datetime.now(timezone.utc)
    return now - timedelta(days=_clamp(days, 1, MAX_DAYS)), now


def parse_request_filters(params: Mapping[str, Any]) -> dict[str, Any]:
    """Validated filter/paging/sort kwargs for the requests + export endpoints.

    Returns only what passed validation: text filters are present when
    non-empty, ``status`` when it is a real HTTP status, ``sort``/``order`` when
    they are in the ledger whitelists (else the defaults), and ``limit``/
    ``offset`` clamped into range.  The result is splatted straight into
    ``fetch_requests(**filters)``, so every key here is a keyword the ledger
    accepts.
    """
    filters: dict[str, Any] = {}
    for name in _TEXT_FILTERS:
        value = _text(params, name)
        if value:
            filters[name] = value
    status = _int(_text(params, "status"))
    if status is not None and MIN_STATUS <= status <= MAX_STATUS:
        filters["status"] = status
    sort = _text(params, "sort").lower()
    filters["sort"] = sort if sort in _REQUEST_SORTS else _DEFAULT_SORT
    order = _text(params, "order").lower()
    filters["order"] = order if order in _REQUEST_ORDERS else _DEFAULT_ORDER
    limit = _int(_text(params, "limit"))
    filters["limit"] = DEFAULT_LIMIT if limit is None else _clamp(limit, 1, MAX_LIMIT)
    offset = _int(_text(params, "offset"))
    filters["offset"] = 0 if offset is None else _clamp(offset, 0, MAX_OFFSET)
    return filters


def parse_series_filters(params: Mapping[str, Any]) -> dict[str, Any]:
    """The subset of filters the series endpoint understands (no paging/sort).

    Series rows are pre-aggregated, so ``sort``/``limit``/``offset`` are
    meaningless there; reusing the requests parser would silently accept them.
    """
    allowed = {name for name, _ in _REQUEST_FILTERS if name != "status"}
    return {k: v for k, v in parse_request_filters(params).items() if k in allowed}
