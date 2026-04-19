import pytest

from sav_client import SavClient
from sav_client.exceptions import SavResponseError


class TestSearchPlayers:
  def test_requires_login(self):
    c = SavClient("https://sav2.fpb.pt", "user", "pass")
    with pytest.raises(SavResponseError, match="Must call login"):
      c.search_players()

  def test_returns_players_from_live_search(self, client):
    results = client.search_players()

    assert isinstance(results, list)
    assert results

    first = results[0]
    assert first.id > 0
    assert first.license
    assert first.name
    assert first.club

  def test_can_find_player_by_license(self, client, sample_player):
    results = client.search_players(license=sample_player.license)

    assert results
    assert any(player.id == sample_player.id for player in results)

  def test_can_search_all_seasons_for_sample_player(self, client, sample_player):
    results = client.search_players(license=sample_player.license, season=0)

    assert results
    assert any(player.license == sample_player.license for player in results)

  def test_can_search_players_across_multiple_clubs(self, client):
    clubs = client.list_clubs()
    if len(clubs) < 2:
      pytest.skip("Need at least 2 clubs to test multi-club search")

    ids = [clubs[0].id, clubs[1].id]
    results = client.search_players(club=ids)

    assert isinstance(results, list)

  def test_can_search_all_clubs_in_association(self, client, sample_player):
    associations = client.list_associations()
    association = next(
      (assoc for assoc in associations if assoc.name.strip() == sample_player.association.strip()),
      None,
    )
    if association is None:
      pytest.skip(f"Could not map player association {sample_player.association!r} to a live association id")

    results = client.search_players(
      license=sample_player.license,
      club=0,
      association=association.id,
    )

    assert results
    assert any(player.license == sample_player.license for player in results)
