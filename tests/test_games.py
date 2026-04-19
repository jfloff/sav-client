import pytest

from sav_client import SavClient
from sav_client.exceptions import SavResponseError


class TestListGames:
  def test_requires_login(self):
    c = SavClient("https://sav2.fpb.pt", "user", "pass")
    with pytest.raises(SavResponseError, match="Must call login"):
      c.list_games()

  def test_lists_live_games(self, client):
    results = client.list_games()

    assert isinstance(results, list)
    if results:
      first = results[0]
      assert first.number
      assert first.home
      assert first.away
      assert isinstance(first.date, str)
      assert isinstance(first.time, str)

  def test_game_has_internal_id(self, client):
    results = client.list_games()
    if not results:
      pytest.skip("Live SAV account has no visible games")

    first = results[0]
    assert first.id > 0

  def test_filters_games_by_number_when_available(self, client):
    results = client.list_games()
    if not results:
      pytest.skip("Live SAV account has no visible games to use as a sample")

    sample = results[0]
    filtered = client.list_games(game_number=sample.number)

    assert filtered
    assert any(game.number == sample.number for game in filtered)
