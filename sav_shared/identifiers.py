"""Canonical forms for the identifiers SAV hands us in more than one shape.

SAV renders a licence as table text and takes it back as a number; it accepts a
NIF with or without separators and validates it in some places and not others.
`sav_client`, `sav_cli` and `sav_mcp` present one shape for each, following the
same split as :mod:`sav_shared.dates`:

* **Reading** — `to_license` / `normalise_nif` are tolerant and never raise, so
  a malformed value SAV sent us degrades to "unknown" instead of breaking a
  listing.
* **Writing** — `require_license` / `require_nif` raise, so a caller learns at
  the boundary rather than having a bad identifier filed with the federation.
"""

from __future__ import annotations

import re
from typing import Any

# Digits only, optionally grouped by the separators SAV and humans use. Anchored
# on purpose: scraping digits out of arbitrary text invents identifiers —
# "Sub 14" would have become licence 14, and "abc123456789" a valid-looking NIF.
_DIGITS_ONLY = re.compile(r"^[\d\s.\-/]*$")
_SEPARATORS = re.compile(r"[\s.\-/]")

#: A Portuguese NIF is exactly nine digits.
NIF_DIGITS = 9

#: Sentinel for "no licence" — SAV never issues 0, and it keeps `Player.license`
#: an int for every row instead of splitting the type across the surface.
NO_LICENSE = 0


def to_license(value: Any) -> int:
  """Return `value` as a licence number, or ``NO_LICENSE`` when it isn't one.

  The read path: licences arrive as table text, sometimes blank (a detail-only
  row) or decorated. Never raises — a row we can't read a licence from is worth
  returning with ``license=0`` rather than failing the whole listing.
  """
  if value is None:
    return NO_LICENSE
  if isinstance(value, bool):
    return NO_LICENSE
  if isinstance(value, int):
    return value if value > 0 else NO_LICENSE
  raw = str(value).strip()
  if not raw or not _DIGITS_ONLY.match(raw):
    return NO_LICENSE
  digits = _SEPARATORS.sub("", raw)
  if not digits:
    return NO_LICENSE
  return int(digits) if int(digits) > 0 else NO_LICENSE


def require_license(value: Any, *, field: str = "license") -> int:
  """Return `value` as a positive licence number, raising if it isn't one.

  The write path. Rejects 0, blanks and non-numeric text, so a caller that
  passes an empty string does not silently address licence 0.
  """
  licence = to_license(value)
  if licence <= 0:
    raise ValueError(f"{field} must be a positive licence number; got {value!r}.")
  return licence


def normalise_nif(value: Any) -> str | None:
  """Return a nine-digit NIF string, or ``None`` when `value` isn't one.

  Separators and whitespace are stripped, so ``"289 463 491"`` and
  ``"289463491"`` are the same NIF. The read path — never raises.

  The check digit is deliberately not verified: SAV is the system of record and
  rejecting a NIF it accepts would block a real enrollment over a rule we have
  not confirmed it applies.
  """
  if value is None or isinstance(value, bool):
    return None
  raw = str(value).strip()
  if not _DIGITS_ONLY.match(raw):
    return None
  digits = _SEPARATORS.sub("", raw)
  return digits if len(digits) == NIF_DIGITS else None


def require_nif(value: Any, *, field: str = "nif") -> str:
  """Return `value` as a nine-digit NIF, raising otherwise.

  The write path, so ``nif="abc"`` fails here rather than reaching SAV's
  create-player call — which used to take it verbatim.
  """
  nif = normalise_nif(value)
  if nif is None:
    raise ValueError(
      f"{field} must be {NIF_DIGITS} digits; got {value!r}."
    )
  return nif
