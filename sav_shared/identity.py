"""Shared player identity matching helpers.

These helpers keep placeholder NIF handling, fuzzy name comparison, and
same-person grouping consistent across consumers of ``sav-client``.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from rapidfuzz import fuzz

from .identifiers import normalise_nif
from .text import normalise_text

if TYPE_CHECKING:
  from sav_client.models import Player


PLACEHOLDER_NIFS = frozenset({"999999990"})


def is_placeholder_nif(nif: str | None) -> bool:
  """Return whether ``nif`` normalises to a known placeholder NIF."""
  return normalise_nif(nif) in PLACEHOLDER_NIFS


def names_match(a: str, b: str, *, threshold: float = 90.0) -> bool:
  """Compare names without accents or case, guarding short shared tokens.

  Token-sort similarity tolerates reordered names. Fuzzy matches require at
  least two shared tokens, so a one-token query such as ``"Hu"`` cannot match
  every longer name ending in that token. An exact normalised match always
  succeeds. Blank inputs never match.
  """
  left = normalise_text(a or "")
  right = normalise_text(b or "")
  if not left or not right:
    return False
  if left == right:
    return True

  left_tokens = set(left.split())
  right_tokens = set(right.split())
  shared_tokens = left_tokens & right_tokens
  if len(shared_tokens) < 2:
    return False

  score = fuzz.token_sort_ratio(left, right)
  score = max(score, fuzz.token_set_ratio(left, right))
  return float(score) >= threshold


def group_same_person(players: list[Player]) -> list[list[Player]]:
  """Group rows by normalised name and exact birth date, preserving order."""
  groups: dict[tuple[str, str], list[Player]] = {}
  for player in players:
    key = (normalise_text(player.name or ""), player.birth_date)
    groups.setdefault(key, []).append(player)
  return list(groups.values())


# SAV's `tipo_identificacao` for a Cartão de Cidadão.
CARTAO_CIDADAO = 1

# A Cartão de Cidadão number, separators removed: the 8-digit civil number,
# optionally followed by the card's own check digit and version (two letters
# and a digit). SAV spaces the tail inconsistently — verified live:
# "15932997 3ZW6", "305439731 ZW4", "30927726 4 ZX1".
_CC_NUMBER = re.compile(r"(\d{8})(\d[A-Z]{2}\d)?")


def cc_civil_number(value: str | None) -> str | None:
  """The 8-digit civil number of a Cartão de Cidadão number, or None.

  The civil number identifies the person; the check digit and version that
  may follow belong to the physical card and change on renewal. Separators
  (spaces, dots, dashes) are ignored. Anything that is not an 8-digit civil
  number, alone or with a card tail, returns None — never a guess.
  """
  match = _CC_NUMBER.fullmatch(_compact_id(value))
  return match.group(1) if match else None


def id_numbers_match(given: str | None, on_file: str | None, *, doc_type: int | None) -> bool:
  """Whether a supplied doc number is the one SAV holds.

  * ``doc_type`` 1 (Cartão de Cidadão): the 8-digit civil numbers are equal.
    SAV's form stores only the civil number today (``validanumid`` accepts
    exactly 8 characters), but older records hold the full card number.
  * Any other type (passport, residence permit, …): every character counts,
    but case and separators (spaces, dots, dashes, slashes) do not — the Sheet
    holds ``860ww7029`` style values, and case or spacing carries no meaning in
    a document number. Nothing is ever dropped, so a different document is
    still a different number.
  * ``doc_type`` None (unknown): the same whole-number comparison, the
    conservative answer. A CC civil number against a full card number then
    reads as *not* matching, so the caller sees it reported rather than
    silently accepted.
  """
  left = _compact_id(given)
  right = _compact_id(on_file)
  if not left or not right:
    return False
  if doc_type == CARTAO_CIDADAO:
    left_cc, right_cc = cc_civil_number(left), cc_civil_number(right)
    if left_cc and right_cc:
      return left_cc == right_cc
  return left == right


def _compact_id(value: str | None) -> str:
  """A doc number with separators removed and letters upper-cased."""
  return re.sub(r"[\s.\-/]+", "", str(value or "")).upper()
