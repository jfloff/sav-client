"""Shared helpers for club-name matching."""

from __future__ import annotations

from typing import Any

from .text import normalise_text


_CLUB_FIELDS = ("name", "full_name", "code")
_FUZZY_MIN_SCORE = 82
_FUZZY_TIE_BAND = 3


def _field_aliases(value: str) -> tuple[str, set[str], set[str]]:
  """Return normalised field text plus token/acronym aliases for fuzzy matching."""
  normalised = normalise_text(value)
  if not normalised:
    return "", set(), set()

  tokens = tuple(normalised.split())
  aliases = {normalised, "".join(tokens)}
  if len(tokens) >= 2:
    aliases.add("".join(token[0] for token in tokens))
    # Tail acronyms stopping at 2 remaining tokens so we never generate a 1-letter alias.
    for start in range(1, len(tokens) - 1):
      aliases.add("".join(token[0] for token in tokens[start:]))
  return normalised, set(tokens), {a for a in aliases if len(a) >= 2}


def _club_matches_query(club: Any, query: str) -> bool:
  """Match a club query against name/full-name/code with accent-tolerant aliases."""
  normalised_query = normalise_text(query)
  if not normalised_query:
    return True

  query_tokens = normalised_query.split()
  for raw in (getattr(club, f, "") for f in _CLUB_FIELDS):
    field_text, field_tokens, aliases = _field_aliases(raw)
    if not field_text:
      continue
    if normalised_query in field_text or normalised_query in aliases:
      return True
    if all(token in field_tokens or token in aliases for token in query_tokens):
      return True
  return False


def _club_match_candidates(club: Any) -> set[str]:
  """Build normalised candidate strings for fuzzy club matching."""
  candidates: set[str] = set()
  for raw in (getattr(club, f, "") for f in _CLUB_FIELDS):
    field_text, field_tokens, aliases = _field_aliases(raw)
    if field_text:
      candidates.add(field_text)
    candidates.update(field_tokens)
    candidates.update(aliases)
  return {c for c in candidates if c}


def _rapidfuzz_best_score(query: str, candidates: set[str]) -> float:
  """Return the best fuzzy score for a query/candidate set."""
  from rapidfuzz import fuzz

  normalised_query = normalise_text(query)
  if not normalised_query:
    return 0.0

  best = 0.0
  for candidate in candidates:
    best = max(
      best,
      fuzz.ratio(normalised_query, candidate),
      fuzz.partial_ratio(normalised_query, candidate),
      fuzz.token_sort_ratio(normalised_query, candidate),
      fuzz.token_set_ratio(normalised_query, candidate),
    )
  return float(best)


def find_club_matches(clubs: list[Any], query: str) -> list[Any]:
  """Find direct matches first, then fuzzy matches ranked by score."""
  direct_matches = [c for c in clubs if _club_matches_query(c, query)]
  if direct_matches:
    return direct_matches

  scored: list[tuple[float, Any]] = []
  for club in clubs:
    score = _rapidfuzz_best_score(query, _club_match_candidates(club))
    if score >= _FUZZY_MIN_SCORE:
      scored.append((score, club))

  if not scored:
    return []

  scored.sort(key=lambda item: (-item[0], getattr(item[1], "name", "")))
  best_score = scored[0][0]
  return [club for score, club in scored if score >= best_score - _FUZZY_TIE_BAND]
