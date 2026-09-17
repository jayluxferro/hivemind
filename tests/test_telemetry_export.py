"""Pure-function tests for `hivemind.telemetry.export` (row writers, no I/O).

These strings are produced *after* the HTTP 200 is already committed (the
export streams from a server-side cursor, SPEC D9), so a bug here cannot be
caught by a status-code check: it ships a corrupt file or hands a spreadsheet
a live formula.  Both properties are pinned directly, and both are pinned
against the module's own declarations rather than a re-typed copy:

- the column order against ``ledger._REQUEST_COLUMNS`` — the single source of
  truth for the SELECT list, the CSV header and the JSONL keys, so a test
  that spelled the columns out itself would stop noticing when they drift;
- the guard matrix against ``export._FORMULA_LEADERS`` — the set the module
  claims to neutralize, which is exactly the set an attacker gets to pick
  from (``model`` strings are observed from request bodies, never validated).

Rows are parsed back with ``csv.reader`` wherever possible instead of
string-compared, because "what the spreadsheet sees" is the contract; the
quoting rules belong to the ``csv`` module and are not this module's to pin.
"""

from __future__ import annotations

import csv
import io
import json
import re
from datetime import datetime, timezone

import pytest

from hivemind.telemetry.export import (
    _FORMULA_GUARD,
    _FORMULA_LEADERS,
    csv_header,
    csv_row,
    export_filename,
    jsonl_row,
)
from hivemind.telemetry.ledger import _REQUEST_COLUMNS


def _utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def _row(**overrides) -> dict:
    """A fully populated shaped row — the dict ``ledger._shape_row`` produces.

    ``cache_write`` stays None on purpose: the usage columns are optional
    (SPEC D2) and "not reported" has to survive the trip to a file distinctly
    from zero.
    """
    row = {
        "id": 1,
        "ts": "2026-01-01T00:00:00Z",
        "agent_hash": "abc123",
        "provider": "anthropic",
        "model": "claude-3-5-sonnet",
        "tokens_in": 10,
        "tokens_out": 20,
        "cache_read": 0,
        "cache_write": None,
        "latency_ms": 123.5,
        "status": 200,
    }
    row.update(overrides)
    return row


def _cells(line: str) -> list[str]:
    """One written line as a reader sees it (quoting undone, crlf dropped)."""
    return next(csv.reader(io.StringIO(line)))


# --- CSV header ---------------------------------------------------------------


def test_csv_header_is_the_request_column_order():
    expected = "id,ts,agent_hash,provider,model,tokens_in,tokens_out,cache_read,cache_write,latency_ms,status\r\n"
    assert csv_header() == expected


def test_csv_header_is_a_projection_of_the_ledger_column_tuple():
    """Both assertions matter: the literal above pins what today's file looks
    like, this one is what fails the day someone reorders ``_REQUEST_COLUMNS``
    — which is the point, because the SELECT list moves with it."""
    assert csv_header() == ",".join(_REQUEST_COLUMNS) + "\r\n"
    assert len(_cells(csv_header())) == len(_REQUEST_COLUMNS)


# --- CSV rows -----------------------------------------------------------------


def test_csv_row_renders_every_cell_as_text():
    cells = _cells(csv_row(_row()))
    assert cells == [
        "1",
        "2026-01-01T00:00:00Z",
        "abc123",
        "anthropic",
        "claude-3-5-sonnet",
        "10",
        "20",
        "0",
        "",
        "123.5",
        "200",
    ]


def test_csv_row_lines_end_with_crlf():
    """RFC 4180, and what Excel/LibreOffice expect from a downloaded file."""
    assert csv_header().endswith("\r\n")
    assert csv_row(_row()).endswith("\r\n")
    assert csv_row(_row()).count("\r\n") == 1


def test_none_and_missing_cells_render_empty():
    assert _cells(csv_row(_row(cache_write=None)))[_REQUEST_COLUMNS.index("cache_write")] == ""
    # A sparse row (an unshaped dict) must not produce a short line either.
    empty = _cells(csv_row({}))
    assert empty == [""] * len(_REQUEST_COLUMNS)


def test_csv_row_has_one_cell_per_column():
    """A short row would silently shift every column to its left in the
    reader's table — the failure mode is a wrong number, not an error."""
    assert len(_cells(csv_row(_row()))) == len(_REQUEST_COLUMNS)
    assert len(_cells(csv_row(_row(agent_hash="a,b", model='he said "hi"')))) == len(_REQUEST_COLUMNS)


def test_commas_quotes_and_newlines_are_quoted_by_csv_writer():
    row = _row(agent_hash="a,b", provider="line1\nline2", model='he said "hi"')
    line = csv_row(row)
    cells = _cells(line)
    assert cells[_REQUEST_COLUMNS.index("agent_hash")] == "a,b"
    assert cells[_REQUEST_COLUMNS.index("provider")] == "line1\nline2"
    assert cells[_REQUEST_COLUMNS.index("model")] == 'he said "hi"'
    # The escaping is the csv module's (RFC 4180 doubling), not hand-rolled.
    assert '"a,b"' in line
    assert '"line1\nline2"' in line
    assert '"he said ""hi"""' in line


def test_csv_export_round_trips_through_dict_reader():
    """The end-to-end promise: header + rows concatenated is a file that a
    spreadsheet, pandas or csv.DictReader reads back with the API's own field
    names."""
    text = csv_header() + csv_row(_row(agent_hash="=SUM(A1)")) + csv_row(_row(id=2, ts="2026-01-02T00:00:00Z"))
    parsed = list(csv.DictReader(io.StringIO(text)))
    assert len(parsed) == 2
    assert list(parsed[0]) == list(_REQUEST_COLUMNS)
    assert parsed[0]["agent_hash"] == "'=SUM(A1)"
    assert parsed[0]["cache_write"] == ""
    assert parsed[0]["latency_ms"] == "123.5"
    assert parsed[1]["id"] == "2"


# --- CSV formula-injection guard (OWASP) --------------------------------------


def test_formula_guard_prefixes_every_declared_leader():
    """The matrix is keyed by the module's own ``_FORMULA_LEADERS``: a leader
    added there without a payload here fails the first assertion, and a leader
    dropped there fails it too.  A cell starting with any of these is a
    formula in Excel/LibreOffice, and the values in these columns come from
    request bodies (``model`` is observed, not validated)."""
    payloads = {
        "=": "=SUM(A1)",
        "+": "+x",
        "-": "-y",
        "@": "@cmd",
        "\t": "\tx",
        "\r": "\ry",
    }
    assert tuple(payloads) == _FORMULA_LEADERS
    for leader, payload in payloads.items():
        cells = _cells(csv_row(_row(agent_hash=payload, model=payload, provider=payload)))
        for column in ("agent_hash", "model", "provider"):
            assert cells[_REQUEST_COLUMNS.index(column)] == _FORMULA_GUARD + payload, (leader, column)


def test_formula_guard_leaves_ordinary_cells_alone():
    for value in ("normal text", "123", "claude-3-5-sonnet", "  =SUM(A1)", "a=b", "x+y", ""):
        assert _cells(csv_row(_row(agent_hash=value)))[_REQUEST_COLUMNS.index("agent_hash")] == value, value


def test_formula_guard_is_a_prefix_test_not_a_search():
    """Only a *leading* leader is a formula, and only the ones a spreadsheet
    evaluates: a leader mid-cell does nothing, and a bare LF is not in the
    leader set (Excel does not start a formula on one), so a cell like
    ``"\\nx"`` is quoted by csv.writer and left otherwise intact."""
    for value in ("a=b", "trailing =SUM(A1)", "\nx", "x+y", '"=SUM(A1)"'):
        assert _cells(csv_row(_row(model=value)))[_REQUEST_COLUMNS.index("model")] == value, value


def test_formula_guard_skips_numeric_cells():
    """Numbers are not text: a negative count stays a number so programmatic
    consumers keep the column dtype (a `'`-prefix would make pandas read it
    as a string).  A numeric cell cannot carry a formula payload, so there
    is nothing to guard."""
    row = _row(tokens_in=-5, latency_ms=-1.5)
    cells = _cells(csv_row(row))
    assert cells[_REQUEST_COLUMNS.index("tokens_in")] == "-5"
    assert cells[_REQUEST_COLUMNS.index("latency_ms")] == "-1.5"


def test_formula_guard_composes_with_csv_quoting():
    line = csv_row(_row(agent_hash="=SUM(A1),B2"))
    assert _cells(line)[_REQUEST_COLUMNS.index("agent_hash")] == _FORMULA_GUARD + "=SUM(A1),B2"
    assert '"' + _FORMULA_GUARD + "=SUM(A1),B2" + '"' in line


# --- JSONL --------------------------------------------------------------------


def test_jsonl_row_is_one_compact_object_per_line():
    line = jsonl_row(_row())
    assert line.endswith("\n")
    assert line.count("\n") == 1
    assert '"id":1' in line and '", "' not in line  # separators=(",", ":"), no padding
    assert json.loads(line)["agent_hash"] == "abc123"


def test_jsonl_keys_follow_the_request_column_order():
    """``json.dumps`` preserves insertion order, so this is the same contract
    the CSV header pins — the API's row shape, spelled once in ledger.py."""
    assert list(json.loads(jsonl_row(_row()))) == list(_REQUEST_COLUMNS)


def test_jsonl_keeps_types_and_nulls():
    payload = json.loads(jsonl_row(_row(id=7, cache_write=None, latency_ms=None, status=500)))
    assert payload["id"] == 7
    assert payload["status"] == 500
    assert payload["tokens_in"] == 10
    assert payload["cache_write"] is None
    assert payload["latency_ms"] is None


def test_jsonl_missing_keys_become_null_not_absent():
    """Every line carries all columns: a consumer can index the object
    without a presence check (the file is not a sparse record dump)."""
    payload = json.loads(jsonl_row({}))
    assert list(payload) == list(_REQUEST_COLUMNS)
    assert all(value is None for value in payload.values())


def test_jsonl_does_not_carry_the_csv_formula_guard():
    """The asymmetry is deliberate: JSONL is read by programs, and an
    apostrophe there would be a corrupted value (``"agent_hash": "'=x"``) for
    no benefit — the consumer is not a spreadsheet."""
    payload = json.loads(jsonl_row(_row(agent_hash="=SUM(A1)", model="+1")))
    assert payload["agent_hash"] == "=SUM(A1)"
    assert payload["model"] == "+1"


def test_jsonl_requires_pre_shaped_rows():
    """``ts`` must already be an ISO string (``ledger._shape_row`` does that).
    A raw datetime is not JSON-serializable, and this raises mid-stream —
    after the 200 is committed, i.e. as a truncated download — which is why
    the ledger shapes before it hands rows to the exporter."""
    with pytest.raises(TypeError):
        jsonl_row(_row(ts=datetime(2026, 1, 1, tzinfo=timezone.utc)))


# --- filenames ----------------------------------------------------------------


def test_export_filename_names_both_ends_of_the_range():
    assert export_filename("csv", _utc(2026, 1, 1), _utc(2026, 2, 1)) == "hivemind-telemetry-2026-01-01--2026-02-01.csv"
    assert (
        export_filename("jsonl", _utc(2026, 1, 1), _utc(2026, 2, 1))
        == "hivemind-telemetry-2026-01-01--2026-02-01.jsonl"
    )


def test_export_filename_covers_a_single_day_range():
    """from == to is the normal case for "today", and it must not collapse
    into one date or emit a triple dash."""
    name = export_filename("csv", _utc(2026, 3, 9), _utc(2026, 3, 9))
    assert name == "hivemind-telemetry-2026-03-09--2026-03-09.csv"
    assert name.count("--") == 1


def test_export_filename_uses_whole_days_only():
    assert export_filename("csv", _utc(2026, 1, 1, 23, 59, 59), _utc(2026, 1, 2, 0, 0, 1)) == (
        "hivemind-telemetry-2026-01-01--2026-01-02.csv"
    )


def test_export_filename_is_header_safe():
    """It is interpolated into ``Content-Disposition: attachment;
    filename="..."``, so it may only contain characters that need no quoting:
    the dates as ``YYYY-MM-DD``, the static prefix and a single separating
    ``--`` — hence [0-9a-z-] with no exception for the extension dot."""
    stem = export_filename("csv", _utc(2026, 1, 1), _utc(2026, 2, 1)).rsplit(".", 1)[0]
    assert re.fullmatch(r"[0-9a-z-]+", stem)
    assert stem.startswith("hivemind-telemetry-")
    assert re.fullmatch(r"hivemind-telemetry-\d{4}-\d{2}-\d{2}--\d{4}-\d{2}-\d{2}", stem)
