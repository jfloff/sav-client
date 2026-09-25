"""Shared player identity matching helpers.

These helpers keep placeholder NIF handling, fuzzy name comparison, and
same-person grouping consistent across consumers of ``sav-client``.
"""

from __future__ import annotations

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
