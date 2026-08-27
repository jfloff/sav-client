"""Shared text and date normalization helpers.

The date rules themselves live in :mod:`sav_shared.dates`; this module keeps
``iso_date`` as an alias so existing imports keep working.
"""

from __future__ import annotations

import re
import unicodedata

from .dates import to_iso


def normalise_text(value: str) -> str:
  """Lowercase, strip accents, collapse punctuation for fuzzy matching."""
  ascii_val = "".join(
    ch for ch in unicodedata.normalize("NFKD", value)
    if not unicodedata.combining(ch)
  )
  return " ".join(re.sub(r"[^a-z0-9]+", " ", ascii_val.lower()).split())


def iso_date(value: str) -> str:
  """Normalise a date to YYYY-MM-DD for lexicographic comparison.

  Thin alias for :func:`sav_shared.dates.to_iso`, kept for the existing
  callers. It used to swap the components unconditionally, which mangled a
  date that was already ISO (``2026-08-27`` came back as ``27-08-2026``) —
  silently, since the result is still a plausible date string.
  """
  return to_iso(value)
