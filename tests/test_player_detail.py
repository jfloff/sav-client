import pytest

from sav_client import SavClient
from sav_client.exceptions import SavResponseError


class TestGetPlayerDetail:
  def test_requires_login(self):
    c = SavClient("https://sav2.fpb.pt", "user", "pass")
    with pytest.raises(SavResponseError, match="Must call login"):
      c.get_player_detail(9, photo=True)

  def test_photo_false_returns_minimal_player(self, client, sample_player):
    result = client.get_player_detail(sample_player.id, photo=False)

    assert result.id == sample_player.id
    assert result.photo_url == ""
    assert result.name == ""

  def test_photo_true_fetches_live_detail(self, client, sample_player):
    result = client.get_player_detail(sample_player.id, photo=True)

    assert result.id == sample_player.id
    assert isinstance(result.photo_url, str)
