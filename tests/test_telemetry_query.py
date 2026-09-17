"""Pure-function tests for `hivemind.telemetry.query` (no I/O, no Postgres).

Every telemetry endpoint turns query-string values into ledger arguments here
and nowhere else, so this file is where the "lenient by design" promise is
pinned: a missing, malformed, inverted or oversized range has to degrade into
something *usable* rather than raise, because the caller is an HTTP handler
that has already promised a 200 (docs/token-ledger-analytics.md, D4/D7).

Two conventions in this file:

- Assertions about the wall clock are bounds around the call
  (``before <= ts <= after``) instead of a tolerance constant.  Both parsers
  read ``now`` exactly once, *inside* the call, so ``[before, after]`` is the
  tightest provable interval for the result: nothing here can flake on a slow
  machine, and a hard-coded expected date could not express the contract at
  all.
- Whitelists and column names are imported from ``ledger``, never re-typed.
  The parser's whole job is to produce keys the ledger accepts, so a test
  that spelled them out itself would stop noticing the moment they drift.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone

from hivemind.telemetry.ledger import (
    _DEFAULT_ORDER,
    _DEFAULT_SORT,
    _REQUEST_FILTERS,
    _REQUEST_ORDERS,
    _REQUEST_SORTS,
    TelemetryLedger,
)
from hivemind.telemetry.query import (
    DEFAULT_LIMIT,
    DEFAULT_RANGE_DAYS,
    MAX_DAYS,
    MAX_HOUR_BUCKET_DAYS,
    MAX_LIMIT,
    MAX_OFFSET,
    MAX_RANGE_DAYS,
    exclusive_end,
    parse_days,
    parse_range,
    parse_request_filters,
    parse_series_filters,
)


def _utc(*args: int) -> datetime:
    """An aware-UTC datetime, so expectations read as dates rather than tzinfo."""
    return datetime(*args, tzinfo=timezone.utc)


def _ranged(params: dict) -> tuple[datetime, datetime, datetime, datetime]:
    """parse_range plus bounds on the clock it read.

    Asserting aware-UTC here rather than in each test keeps the D7 invariant
    ("every read takes aware-UTC datetimes") in one place: a naive range would
    silently compare against ``timestamptz`` columns without ever raising.
    """
    before = datetime.now(timezone.utc)
    from_ts, to_ts = parse_range(params)
    after = datetime.now(timezone.utc)
    for ts in (from_ts, to_ts):
        assert ts.tzinfo is not None, "naive range returned"
        assert ts.utcoffset() == timedelta(0), ts
    return from_ts, to_ts, before, after


def _dayed(params: dict) -> tuple[tuple[datetime, datetime] | None, datetime, datetime]:
    """parse_days plus bounds on the clock it read."""
    before = datetime.now(timezone.utc)
    window = parse_days(params)
    after = datetime.now(timezone.utc)
    return window, before, after


# --- module constants (the budgets the rest of the file asserts against) ------


def test_constants_pin_the_documented_budgets():
    """These are the SPEC's numbers, quoted once so a widened clamp is a diff
    here and not a silent change in behavior."""
    assert MAX_RANGE_DAYS == 366
    assert DEFAULT_RANGE_DAYS == 14
    assert MAX_DAYS == 365
    assert MAX_HOUR_BUCKET_DAYS == 14
    assert MAX_LIMIT == 500
    assert MAX_OFFSET == 1_000_000
    assert DEFAULT_LIMIT == 100


# --- parse_range: the default window ------------------------------------------


def test_no_params_is_the_last_14_days_ending_now():
    from_ts, to_ts, before, after = _ranged({})
    assert before <= to_ts <= after
    assert to_ts - from_ts == timedelta(days=DEFAULT_RANGE_DAYS)


def test_days_param_is_not_a_range_param():
    """Only /data consults the legacy ``days`` alias (via parse_days); every
    other endpoint goes straight to parse_range, which does not read it — so
    ``?days=90`` on /requests is the 14-day default, not a 90-day window."""
    from_ts, to_ts, _, _ = _ranged({"days": "90"})
    assert to_ts - from_ts == timedelta(days=DEFAULT_RANGE_DAYS)


# --- parse_range: explicit dates ----------------------------------------------


def test_explicit_dates_are_kept_as_written():
    from_ts, to_ts, _, _ = _ranged({"from": "2026-01-01", "to": "2026-02-01"})
    assert from_ts == _utc(2026, 1, 1)
    assert to_ts == _utc(2026, 2, 1)


def test_datetimes_keep_their_time_of_day_and_naive_means_utc():
    """A naive datetime is read as UTC (SPEC): the proxy's own timezone must
    never shift a bucket boundary, so there is no local-time interpretation."""
    from_ts, to_ts, _, _ = _ranged({"from": "2026-01-01T06:30:00", "to": "2026-01-01T18:00:00Z"})
    assert from_ts == _utc(2026, 1, 1, 6, 30)
    assert to_ts == _utc(2026, 1, 1, 18, 0)


def test_trailing_z_is_accepted_in_both_cases():
    for raw in ("2026-01-01T06:30:00Z", "2026-01-01T06:30:00z"):
        from_ts, _, _, _ = _ranged({"from": raw, "to": "2026-01-02"})
        assert from_ts == _utc(2026, 1, 1, 6, 30), raw


def test_offset_aware_input_is_converted_to_utc():
    from_ts, _, _, _ = _ranged({"from": "2026-01-01T00:00:00+05:00", "to": "2026-01-02"})
    assert from_ts == _utc(2025, 12, 31, 19, 0)


# --- parse_range: the four repair rules (D7) ----------------------------------


def test_inverted_range_is_swapped():
    from_ts, to_ts, _, _ = _ranged({"from": "2026-02-01", "to": "2026-01-01"})
    assert (from_ts, to_ts) == (_utc(2026, 1, 1), _utc(2026, 2, 1))


def test_future_to_is_clamped_to_now_but_from_is_kept():
    """Clamping ``to`` must not drag the start with it: the caller asked for
    "since Jan 1", and that part is answerable."""
    from_ts, to_ts, before, after = _ranged({"from": "2026-01-01", "to": "2027-01-01"})
    assert before <= to_ts <= after
    assert from_ts == _utc(2026, 1, 1)


def test_inverted_range_with_a_future_from_collapses_at_now():
    """from=2027 (future), to=2026-01-01: the swap hands the future date to
    ``to``, and the clamp then walks it back to now."""
    from_ts, to_ts, before, after = _ranged({"from": "2027-01-01", "to": "2026-01-01"})
    assert from_ts == _utc(2026, 1, 1)
    assert before <= to_ts <= after


def test_two_future_ends_collapse_to_a_point_at_now():
    """Documented consequence of clamp-then-swap-then-clamp: with both ends in
    the future there is no past instant to show, so the window degenerates to
    a zero-width range at ``now`` (the ledger's ``to + 1 day`` still makes it
    a one-day SQL window)."""
    from_ts, to_ts, before, after = _ranged({"from": "2027-01-01", "to": "2027-06-01"})
    assert before <= from_ts <= after
    assert from_ts == to_ts


def test_range_wider_than_max_keeps_to_and_moves_from_forward():
    """The cap has to shrink the start, not the end: "the last 3 years" is
    still answerable as the last 366 days."""
    from_ts, to_ts, _, _ = _ranged({"from": "2024-01-01", "to": "2026-01-01"})
    assert to_ts == _utc(2026, 1, 1)
    assert to_ts - from_ts == timedelta(days=MAX_RANGE_DAYS)
    assert from_ts == to_ts - timedelta(days=MAX_RANGE_DAYS)


def test_range_exactly_at_max_is_left_alone():
    to_in = _utc(2026, 1, 1)
    from_in = to_in - timedelta(days=MAX_RANGE_DAYS)
    from_ts, to_ts, _, _ = _ranged({"from": from_in.isoformat(), "to": to_in.isoformat()})
    assert (from_ts, to_ts) == (from_in, to_in)


# --- parse_range: bad input ---------------------------------------------------


def test_unparseable_dates_fall_back_to_the_default_window():
    for bad in ("not-a-date", "2026-13-45", "2026-01-01T", "14d", ";;", "1.5"):
        from_ts, to_ts, before, after = _ranged({"from": bad, "to": bad})
        assert before <= to_ts <= after, bad
        assert to_ts - from_ts == timedelta(days=DEFAULT_RANGE_DAYS), bad


def test_blank_params_are_treated_as_absent():
    from_ts, to_ts, before, after = _ranged({"from": "   ", "to": "\t\n"})
    assert before <= to_ts <= after
    assert to_ts - from_ts == timedelta(days=DEFAULT_RANGE_DAYS)


def test_one_bad_end_does_not_discard_the_other():
    """Fallback is per param, and the good end stays the anchor — a typo in
    ``from`` must not throw away the day the user picked in ``to``."""
    from_ts, to_ts, _, _ = _ranged({"from": "garbage", "to": "2026-01-01"})
    assert to_ts == _utc(2026, 1, 1)
    assert from_ts == _utc(2026, 1, 1) - timedelta(days=DEFAULT_RANGE_DAYS)


# --- parse_days (the Phase 1 compatibility alias) -----------------------------


def test_parse_days_returns_none_when_days_is_absent():
    assert parse_days({}) is None
    assert parse_days({"from": "", "to": "   "}) is None
    assert parse_days({"bucket": "hour"}) is None


def test_parse_days_window_is_now_minus_n_days():
    window, before, after = _dayed({"days": "30"})
    assert window is not None
    from_ts, to_ts = window
    assert before <= to_ts <= after
    assert to_ts - from_ts == timedelta(days=30)
    assert from_ts.utcoffset() == timedelta(0)


def test_parse_days_clamps_into_1_to_365():
    for raw, expected in (("1", 1), ("0", 1), ("-5", 1), ("365", 365), ("366", 365), ("100000", 365)):
        window, _, _ = _dayed({"days": raw})
        assert window is not None, raw
        assert window[1] - window[0] == timedelta(days=expected), raw


def test_parse_days_ignores_unparseable_values():
    """None here means the handler falls through to parse_range, which is the
    Phase 1 behavior for a typo'd ``days``: default window, never an error."""
    for raw in ("", "   ", "abc", "3.5", "30d", "1e2"):
        window, _, _ = _dayed({"days": raw})
        assert window is None, raw


def test_parse_days_trims_whitespace():
    window, _, _ = _dayed({"days": " 7 "})
    assert window is not None
    assert window[1] - window[0] == timedelta(days=7)


def test_explicit_from_or_to_wins_over_days():
    for params in (
        {"days": "30", "from": "2026-01-01"},
        {"days": "30", "to": "2026-01-01"},
        {"days": "30", "from": "2026-01-01", "to": "2026-02-01"},
    ):
        assert params and parse_days(params) is None, params


def test_a_present_but_unparseable_date_still_suppresses_days():
    """parse_days keys on *presence*, not validity.  So ``?days=30&from=oops``
    is not a 30-day request — it falls through to parse_range, which repairs
    the bad date and answers with its own 14-day default."""
    assert parse_days({"days": "30", "from": "garbage"}) is None
    from_ts, to_ts, before, after = _ranged({"days": "30", "from": "garbage"})
    assert before <= to_ts <= after
    assert to_ts - from_ts == timedelta(days=DEFAULT_RANGE_DAYS)


def test_data_handler_precedence_days_first_then_range():
    """Mirrors `telemetry_data_handler`: `days` is consulted first, and only a
    None result (absent ``days``, or an explicit from/to) reaches parse_range."""

    def handler_window(params: dict) -> tuple[datetime, datetime]:
        window = parse_days(params)
        return parse_range(params) if window is None else window

    from_ts, to_ts = handler_window({"days": "90", "from": "2026-01-01", "to": "2026-01-15"})
    assert (from_ts, to_ts) == (_utc(2026, 1, 1), _utc(2026, 1, 15))

    from_ts, to_ts = handler_window({"days": "90"})
    assert to_ts - from_ts == timedelta(days=90)

    from_ts, to_ts = handler_window({})
    assert to_ts - from_ts == timedelta(days=DEFAULT_RANGE_DAYS)


# --- parse_request_filters: text filters --------------------------------------


def test_empty_params_yield_defaults_and_no_filters():
    """Only paging/sort keys are unconditional.  The absence of the four
    filter keys *is* the API: they are splatted into fetch_requests, where a
    ``None`` default means "no WHERE clause for this column"."""
    assert parse_request_filters({}) == {
        "sort": _DEFAULT_SORT,
        "order": _DEFAULT_ORDER,
        "limit": DEFAULT_LIMIT,
        "offset": 0,
    }


def test_text_filters_are_stripped_and_case_preserved():
    """The values are exact-match against stored text, so only surrounding
    whitespace is removed — case is part of the match."""
    filters = parse_request_filters(
        {"agent_hash": "  abc123def ", "model": " Claude-3.5-Sonnet\n", "provider": "Anthropic "},
    )
    assert filters["agent_hash"] == "abc123def"
    assert filters["model"] == "Claude-3.5-Sonnet"
    assert filters["provider"] == "Anthropic"


def test_blank_text_filters_are_dropped():
    """An empty filter must mean "no filter", not "match the empty string" —
    the dashboard sends the param unconditionally on every page."""
    filters = parse_request_filters({"agent_hash": "   ", "model": "", "provider": "\t\n"})
    for name in ("agent_hash", "model", "provider"):
        assert name not in filters, name


def test_text_filter_values_are_not_escaped_here():
    """A quote-heavy value survives the parser verbatim: the safety is the
    ``%s`` placeholder in ``ledger._REQUEST_FILTERS``, so escaping it here
    would corrupt exact-match lookups without buying anything."""
    payload = "'; DROP TABLE token_usage; --"
    assert parse_request_filters({"agent_hash": payload})["agent_hash"] == payload


# --- parse_request_filters: status --------------------------------------------


def test_status_accepts_http_status_codes_only():
    for raw, expected in (
        (100, 100),
        (200, 200),
        (404, 404),
        (599, 599),
        (99, None),
        (0, None),
        (-1, None),
        (600, None),
    ):
        filters = parse_request_filters({"status": str(raw)})
        if expected is None:
            assert "status" not in filters, raw
        else:
            assert filters["status"] == expected, raw


def test_status_rejects_non_integers():
    """No float, no exponent, no blank — the value has to be a whole number or
    the filter is dropped entirely (a typo'd status must not widen the read)."""
    for raw in ("abc", "200.0", "2e2", " ", "2 0 0", "0x200"):
        filters = parse_request_filters({"status": raw})
        assert "status" not in filters, raw
    # ...but surrounding whitespace is not a typo, it is trimming.
    assert parse_request_filters({"status": " 200 "})["status"] == 200


# --- parse_request_filters: sort / order --------------------------------------


def test_sort_falls_back_to_the_default_on_garbage():
    """User input is only ever a dict *key* (SPEC D8): an unknown sort is
    dropped, never escaped-and-interpolated."""
    for raw in ("cost", "id", "", "   ", "ts; DROP TABLE token_usage", "1", "id, ts"):
        assert parse_request_filters({"sort": raw})["sort"] == _DEFAULT_SORT, raw


def test_order_falls_back_to_the_default_on_garbage():
    for raw in ("", "sideways", "ASC; --", "0", "desc nulls last"):
        assert parse_request_filters({"order": raw})["order"] == _DEFAULT_ORDER, raw


def test_sort_and_order_accept_every_whitelisted_key_case_insensitively():
    for key in _REQUEST_SORTS:
        assert parse_request_filters({"sort": key})["sort"] == key
        assert parse_request_filters({"sort": key.upper()})["sort"] == key
        assert parse_request_filters({"sort": key.title()})["sort"] == key
    for key in _REQUEST_ORDERS:
        assert parse_request_filters({"order": key})["order"] == key
        assert parse_request_filters({"order": key.upper()})["order"] == key


# --- parse_request_filters: paging --------------------------------------------


def test_limit_clamps_into_1_to_500_and_defaults_to_100():
    for raw, expected in (
        (None, DEFAULT_LIMIT),
        ("", DEFAULT_LIMIT),
        ("abc", DEFAULT_LIMIT),
        ("3.5", DEFAULT_LIMIT),
        ("0", 1),
        ("-10", 1),
        ("1", 1),
        ("250", 250),
        (" 250 ", 250),
        ("500", MAX_LIMIT),
        ("501", MAX_LIMIT),
        ("1000000", MAX_LIMIT),
    ):
        params = {} if raw is None else {"limit": raw}
        assert parse_request_filters(params)["limit"] == expected, raw


def test_offset_clamps_into_0_to_1_000_000_and_defaults_to_0():
    for raw, expected in (
        (None, 0),
        ("", 0),
        ("nope", 0),
        ("-1", 0),
        ("0", 0),
        ("250", 250),
        ("1000000", MAX_OFFSET),
        ("99999999", MAX_OFFSET),
    ):
        params = {} if raw is None else {"offset": raw}
        assert parse_request_filters(params)["offset"] == expected, raw


# --- parse_request_filters: the ledger link -----------------------------------


def test_filters_are_exactly_the_kwargs_fetch_requests_accepts():
    """This is the parser's whole contract: its dict is splatted into
    ``fetch_requests(from_ts, to_ts, **filters)``, so one key too many is a
    TypeError at request time (a 500 on a URL the user typed), not a
    validation error.  Equality with the whitelist means the two cannot drift
    in either direction."""
    accepted = {
        name
        for name, param in inspect.signature(TelemetryLedger.fetch_requests).parameters.items()
        if param.kind is inspect.Parameter.KEYWORD_ONLY
    }
    assert set(parse_request_filters({})) <= accepted
    full = parse_request_filters({"agent_hash": "a", "model": "m", "provider": "p", "status": "200"})
    assert set(full) == accepted


def test_filter_names_cover_the_ledger_filter_tuple():
    """``_REQUEST_FILTERS`` fixes the SQL placeholder order; the parser has to
    produce a value for every column in it (and the parser's extra keys must
    all be paging/sort, which the ledger takes last)."""
    declared = {name for name, _ in _REQUEST_FILTERS}
    full = parse_request_filters({"agent_hash": "a", "model": "m", "provider": "p", "status": "200"})
    assert declared <= set(full)
    assert declared == {"agent_hash", "model", "provider", "status"}


# --- parse_series_filters (the subset the series endpoint understands) ---------


def test_series_filters_drop_paging_sort_and_status():
    """Series rows are pre-aggregated, so sort/limit/offset are meaningless
    there, and the series endpoint offers no status control.  Reusing the
    requests parser would have silently accepted all four."""
    params = {
        "agent_hash": "abc",
        "model": "m",
        "provider": "p",
        "status": "200",
        "sort": "tokens",
        "order": "asc",
        "limit": "5",
        "offset": "10",
    }
    assert parse_series_filters(params) == {"agent_hash": "abc", "model": "m", "provider": "p"}
    assert parse_series_filters({}) == {}


# --- exclusive_end (display range -> half-open SQL window) ---------------------


def test_exclusive_end_is_the_first_instant_after_the_last_displayed_day():
    end = exclusive_end(_utc(2026, 1, 1))
    assert end == _utc(2026, 1, 2)
    assert end.tzinfo is not None and end.utcoffset() == timedelta(0)


def test_exclusive_end_preserves_the_time_of_day():
    assert exclusive_end(_utc(2026, 1, 1, 12, 30, 45)) == _utc(2026, 1, 2, 12, 30, 45)


def test_exclusive_end_across_month_year_and_leap_boundaries():
    assert exclusive_end(_utc(2025, 12, 31)) == _utc(2026, 1, 1)
    assert exclusive_end(_utc(2026, 11, 30, 23, 59, 59)) == _utc(2026, 12, 1, 23, 59, 59)
    # 2024 is a leap year: "all of Feb 28" ends before Feb 29, not on Mar 1.
    assert exclusive_end(_utc(2024, 2, 28)) == _utc(2024, 2, 29)
    assert exclusive_end(_utc(2023, 2, 28)) == _utc(2023, 3, 1)


def test_exclusive_end_closes_the_parsed_default_window():
    """The two functions only make sense as a pair: ``to`` is the last
    *displayed* day (inclusive), so the SQL window runs one day further —
    "a bare to=2026-09-01 covers all of Sept 1" for every endpoint."""
    from_ts, to_ts, _, _ = _ranged({})
    assert exclusive_end(to_ts) - from_ts == timedelta(days=DEFAULT_RANGE_DAYS + 1)
