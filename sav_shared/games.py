"""Shared helpers for game filtering and ordering."""

from __future__ import annotations

from typing import Any

from .dates import to_date, to_iso


def filter_games(
  games: list[Any],
  *,
  competition: str = "",
  status: str = "",
  date_from: str = "",
  date_to: str = "",
) -> list[Any]:
  """Apply the common client-side game-sheet filters."""
  if competition:
    games = [g for g in games if competition.lower() in g.competition.lower()]
  if status:
    games = [g for g in games if g.game_status == status]
  if date_from:
    games = [g for g in games if to_iso(g.date) >= to_iso(date_from)]
  if date_to:
    games = [g for g in games if to_iso(g.date) <= to_iso(date_to)]
  return games


def game_sort_key(game: Any) -> tuple:
  """Return a (date, time) tuple for sorting games chronologically.

  Undated or unparseable games sort last rather than raising, so one odd row
  can't break a listing. The date is parsed with the shared helper: splitting
  on ``-`` and assuming ``DD-MM-YYYY`` used to yield three parts for an ISO
  date too, so the game sorted by ``(27, 8, 2026)`` — wrong order, no error.
  """
  parsed = to_date(getattr(game, "date", None))
  date_key = (parsed.year, parsed.month, parsed.day) if parsed else (9999, 99, 99)
  try:
    h, mi = game.time.split(":")
    time_key = (int(h), int(mi))
  except Exception:
    time_key = (99, 99)
  return date_key + time_key
