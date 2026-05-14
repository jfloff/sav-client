"""Shared helpers for game filtering and ordering."""

from __future__ import annotations

from typing import Any

from .text import iso_date


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
    games = [g for g in games if iso_date(g.date) >= iso_date(date_from)]
  if date_to:
    games = [g for g in games if iso_date(g.date) <= iso_date(date_to)]
  return games


def game_sort_key(game: Any) -> tuple:
  """Return a (date, time) tuple for sorting games chronologically."""
  try:
    d, m, y = game.date.split("-")
    date_key = (int(y), int(m), int(d))
  except Exception:
    date_key = (9999, 99, 99)
  try:
    h, mi = game.time.split(":")
    time_key = (int(h), int(mi))
  except Exception:
    time_key = (99, 99)
  return date_key + time_key
