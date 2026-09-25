"""Looking a player up by licence, the one way.

``lookup_player`` / ``get_player`` in sav-mcp and the CLI resolve a licence to
its newest row through the same season ladder: current season, then the
previous one, then every season. A current-season-only search misses anyone not
yet enrolled this season — every Revalidação athlete, by definition — which is
the bug class this module exists to keep out.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from .text import normalise_text

if TYPE_CHECKING:
  from sav_client.models import Player

logger = logging.getLogger(__name__)


def most_recent(players: list[Player]) -> Player | None:
  """Return the latest-season row, or None when no rows were found.

  A defensive invariant guard, not a fix for an observed bug. It was added on
  the assumption that a licence search with ``jc_epoca=0`` returns one row per
  season, making ``results[0]`` arbitrary. Live verification (2026-08-20, club
  2430) refuted that: a licence-scoped all-seasons search returned a single
  row, and the club-wide all-seasons search returned 684 rows for 684 distinct
  licences — no duplicates. SAV2 does not *document* single-row, so this stays
  as a cheap guard; just don't mistake it for load-bearing.

  ``Player.season`` is a "YYYY/YYYY" string, so lexicographic max is
  chronological. With one row this is identical to ``results[0]``.
  """
  if not players:
    return None
  return max(players, key=lambda player: player.season or "")


def resolve_license_row(
  client: Any,
  *,
  license: int,
  club_id: int = 0,
  status: str = "all",
  season: int | None = None,
  with_details: bool = False,
) -> Player | None:
  """Resolve one licence to its newest row: current → previous → all seasons.

  The one licence lookup — ``lookup_player`` / ``get_player`` and the CLI all
  go through it. ``club_id=0`` is one federation-wide request per rung (SAV
  matches a licence at any club natively). ``season`` pins a single rung.
  """
  def search(rung: int | None) -> list[Player]:
    return client.search_players(
      license=str(license), club=club_id, status=status, season=rung,
      with_details=with_details,
    )

  if season is not None:
    return most_recent(search(season))

  # Keep the common current-enrollment case to one query. A player last
  # enrolled in the previous season costs two; the all-seasons rung is
  # deliberately the last resort.
  current = search(None)
  if current:
    return most_recent(current)

  previous_season: int | None = None
  try:
    recent_seasons = client._recent_season_ids()
    if len(recent_seasons) > 1:
      previous_season = recent_seasons[1]
  except Exception:
    # Deliberately broad, unlike the SavError handlers elsewhere in this
    # file: this rung is only an optimisation and the all-seasons query
    # below returns the correct answer without it. Failing a caller's
    # lookup because an optional shortcut broke would be the worse trade.
    logger.debug("Could not resolve the previous SAV season", exc_info=True)

  if previous_season is not None:
    previous = search(previous_season)
    if previous:
      return most_recent(previous)

  return most_recent(search(0))


def gender_id_of(player: Any) -> int:
  """The player's gender id (1 Masculino, 2 Feminino) from a SAV row.

  ``Player.gender_id`` is resolved from the gender label; a bare row is
  resolved from its label the same way. Anything else raises rather than
  guessing — a wrong gender resolves a subida against the wrong tier table.
  """
  gender_id = int(getattr(player, "gender_id", 0) or 0)
  if gender_id in (1, 2):
    return gender_id
  gender_id = {"masculino": 1, "feminino": 2}.get(
    normalise_text(getattr(player, "gender", "") or ""), 0,
  )
  if gender_id not in (1, 2):
    raise ValueError(
      f"Unrecognised gender {getattr(player, 'gender', None)!r} for licence "
      f"{getattr(player, 'license', None)}"
    )
  return gender_id
