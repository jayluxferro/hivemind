"""Row writers for the server-side telemetry export (pure, no I/O).

Both formats are built from :data:`ledger._REQUEST_COLUMNS` — the same tuple
the requests SELECT list comes from — so the CSV header, the JSONL key order
and the API row shape can never drift apart.

The CSV side carries a formula-injection guard because the ledger's text
columns (``agent_hash``, ``provider``, ``model``) are attacker-influenced: a
model string arriving from a request body is *observed*, not validated.  Excel
and LibreOffice evaluate a cell beginning ``=``, ``+``, ``-``, ``@``, TAB or CR
as a formula, so those cells get an apostrophe prefix (OWASP CSV-injection
mitigation).  The prefix is data, not an escape — it is inside the quoted CSV
field and is what the spreadsheet shows as a leading apostrophe.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime
from typing import Any, Iterable, Mapping

from .ledger import _REQUEST_COLUMNS

#: Leading characters a spreadsheet would treat as a formula (OWASP).
_FORMULA_LEADERS = ("=", "+", "-", "@", "\t", "\r")
#: Neutralizer: the cell still reads as written, but is no longer a formula.
_FORMULA_GUARD = "'"

#: CSV line terminator — RFC 4180, and what Excel/LibreOffice expect.
_CRLF = "\r\n"


def _guard(value: Any) -> Any:
    """Formula-injection guard for TEXT cells only (None -> "").

    Numbers pass through untouched: a negative token count must stay a
    number for programmatic consumers (pandas reads a `'`-prefixed cell as
    a string and shifts the column dtype), and a numeric cell cannot carry
    a formula payload — the injection vector only exists in text.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        return value
    if value.startswith(_FORMULA_LEADERS):
        return _FORMULA_GUARD + value
    return value


def csv_header() -> str:
    """The CSV header line (``_REQUEST_COLUMNS`` order, crlf)."""
    return _csv_line(_REQUEST_COLUMNS)


def csv_row(row: Mapping[str, Any]) -> str:
    """One CSV row line (crlf) from a shaped requests row."""
    return _csv_line([_guard(row.get(column)) for column in _REQUEST_COLUMNS])


def jsonl_row(row: Mapping[str, Any]) -> str:
    """One compact JSON object per line, keys in ``_REQUEST_COLUMNS`` order."""
    payload = {column: row.get(column) for column in _REQUEST_COLUMNS}
    return json.dumps(payload, separators=(",", ":")) + "\n"


def export_filename(ext: str, from_ts: datetime, to_ts: datetime) -> str:
    """Download filename for a range: ``hivemind-telemetry-<from>--<to>.<ext>``."""
    return f"hivemind-telemetry-{from_ts:%Y-%m-%d}--{to_ts:%Y-%m-%d}.{ext}"


def _csv_line(cells: Iterable[Any]) -> str:
    """One csv.writer line.  A fresh StringIO per row keeps this stateless
    (the export generator may interleave batches from concurrent downloads)."""
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator=_CRLF).writerow(list(cells))
    return buffer.getvalue()
