import pytest

from sav_client import SavClient
from sav_client.exceptions import SavResponseError


class TestGetPlayerDetail:
  def test_requires_login(self):
    c = SavClient("https://sav2.fpb.pt", "user", "pass")
    with pytest.raises(SavResponseError, match="Must call login"):
      c.get_player_detail(9, with_details=True)

  def test_with_details_false_returns_minimal_player(self, client, sample_player):
    result = client.get_player_detail(sample_player.id, with_details=False)

    assert result.id == sample_player.id
    assert result.photo_url == ""
    assert result.mobile_phone == ""
    assert result.name == ""

  def test_with_details_true_fetches_live_detail(self, client, sample_player):
    result = client.get_player_detail(sample_player.id, with_details=True)

    assert result.id == sample_player.id
    assert isinstance(result.photo_url, str)
    assert isinstance(result.mobile_phone, str)
