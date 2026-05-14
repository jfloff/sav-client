"""Shared text and date normalization helpers."""

from __future__ import annotations

import re
import unicodedata


def normalise_text(value: str) -> str:
  """Lowercase, strip accents, collapse punctuation for fuzzy matching."""
  ascii_val = "".join(
    ch for ch in unicodedata.normalize("NFKD", value)
    if not unicodedata.combining(ch)
  )
  return " ".join(re.sub(r"[^a-z0-9]+", " ", ascii_val.lower()).split())


def iso_date(date_ddmmyyyy: str) -> str:
  """Convert DD-MM-YYYY to YYYY-MM-DD for lexicographic date comparison."""
  try:
    d, m, y = date_ddmmyyyy.split("-")
    return f"{y}-{m}-{d}"
  except Exception:
    return date_ddmmyyyy
