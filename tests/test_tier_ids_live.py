"""Live regression guard for PLAYER_REGISTRATION_TIERS.

The per-gender tier-id -> name map used to be scraped live from SAV2's op=3
dropdown; commit 499fc6e replaced it with a hardcoded static table (see
sav_shared/lookups.py) on the assumption it's stable across seasons, and
deleted the scraping code without leaving a live test behind. This re-fetches
the op=3 dropdown directly to catch SAV2 silently renumbering escalões.
"""
import re

import pytest

from sav_shared.lookups import PLAYER_REGISTRATION_TIERS

_REGISTRATIONS_PATH = "php/incricoesdb.php"
_TIERS_OP = "3"


def _fetch_live_tiers(client, gender_id: int) -> dict[int, str]:
  resp = client._http.get(
    client._url(_REGISTRATIONS_PATH),
    params={"op": _TIERS_OP, "genero": gender_id},
    timeout=client._timeout,
  )
  resp.raise_for_status()
  options = re.findall(r"<option value='(\d+)'\s*>([^<]+)</option>", resp.text)
  return {int(i): name.strip() for i, name in options if int(i) != 0}


class TestTierIdsMatchLive:
  @pytest.mark.parametrize("gender_id", [1, 2])
  def test_tier_ids_match_static_table(self, client, gender_id):
    live = _fetch_live_tiers(client, gender_id)
    assert live == PLAYER_REGISTRATION_TIERS[gender_id]
