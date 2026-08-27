"""Date parsing and normalisation shared by the client, CLI and MCP.

SAV2 is not internally consistent about dates. Its registration wizard and
profile endpoints round-trip ISO (``YYYY-MM-DD``), while its listing endpoints
render the European ``DD-MM-YYYY``. Hiding that from callers is part of what
`sav_client`, `sav_cli` and `sav_mcp` are for, so there is one rule per
direction:

* **Reading** — values arriving from SAV or from an OCR pass go through
  :func:`to_iso`, which accepts either order and normalises to ISO. Tolerance
  belongs here: we don't control what SAV sends.
* **Writing** — values a caller supplies that will reach a SAV commit body go
  through :func:`require_iso`, which accepts ISO only and raises otherwise.
  Tolerance is actively harmful here, because it turns a clear boundary error
  into a silent failure three calls later inside the federation.

Every date the public surface *emits* is ISO, whichever endpoint it came from.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

_SEPARATORS = re.compile(r"[-/.]")


def split_date_parts(value: Any) -> tuple[str, str, str] | None:
  """``(year, month, day)`` from an ISO or European date string, else ``None``.

  Tolerant of ``-``, ``/`` and ``.`` separators. Order is decided by which end
  carries the four-digit year, so ``2026-08-27`` and ``27-08-2026`` are both
  unambiguous. Anything else — a two-digit year, the wrong field count —
  returns ``None`` rather than guessing, since a guess here writes a wrong date
  to a federation record.
  """
  parts = [p for p in _SEPARATORS.split(str(value).strip()) if p]
  if len(parts) != 3:
    return None
  first, _, last = parts
  if len(first) == 4:
    year, month, day = parts
  elif len(last) == 4:
    day, month, year = parts
  else:
    return None
  return year, month, day


def to_date(value: Any) -> date | None:
  """Parse an ISO or European date value into a ``date``, or ``None`` if it can't be.

  Blank and unparseable values return ``None`` so callers can distinguish
  "no date" from "a date I refuse to guess at" without catching exceptions.
  """
  if value is None:
    return None
  if isinstance(value, date):
    return value
  if not str(value).strip():
    return None
  parts = split_date_parts(value)
  if parts is None:
    return None
  year, month, day = parts
  try:
    return date(int(year), int(month), int(day))
  except ValueError:
    return None


def to_iso(value: Any) -> str:
  """Normalise an ISO or European date to ISO, passing through what it can't parse.

  The read path. An unparseable value is returned unchanged rather than blanked,
  so a display string SAV sent us is never destroyed by normalisation — the
  caller sees whatever SAV actually said.
  """
  parsed = to_date(value)
  if parsed is not None:
    return parsed.isoformat()
  return "" if value is None else str(value)


def require_iso(value: Any, *, field: str) -> str:
  """Return ``value`` as an ISO date string, raising unless it already is one.

  The write path. ``DD-MM-YYYY`` is rejected rather than converted: a caller
  with its formats mixed up should learn at the boundary, not have a
  wrong-but-plausible date filed with the federation.

  Also rejects the compact ``YYYYMMDD`` and unpadded ``2026-5-1`` spellings that
  :meth:`datetime.date.fromisoformat` accepts from Python 3.11 on, so the
  contract really is one shape.

  Args:
      value: The caller-supplied date.
      field: Field name to name in the error, e.g. ``"exam_date"``.

  Raises:
      ValueError: When `value` is blank, unparseable, or not hyphenated ISO.
  """
  if value is None:
    raise ValueError(f"{field} must be YYYY-MM-DD; got None.")
  raw = value.isoformat() if isinstance(value, date) else str(value).strip()
  if not raw:
    raise ValueError(f"{field} must be YYYY-MM-DD; got {value!r}.")
  try:
    parsed = date.fromisoformat(raw)
  except ValueError as exc:
    raise ValueError(f"{field} must be YYYY-MM-DD; got {value!r}.") from exc
  if parsed.isoformat() != raw:
    raise ValueError(f"{field} must be YYYY-MM-DD; got {value!r}.")
  return raw
