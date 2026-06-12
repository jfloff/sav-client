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


class TestDateWindowPayload:
  """SAV2 only honors the date window when both inicio AND fim are present and
  ISO-formatted. The client must translate DD-MM-YYYY → YYYY-MM-DD and backfill
  the missing bound with an open sentinel; these tests capture the raw payload.
  """

  def _capture(self, monkeypatch, **kwargs):
    client = SavClient("https://sav2.fpb.pt", "user", "pass")
    client.session = {"user": "u", "organizacao": 1, "perfil": 1, "epoca_id": 100}
    sent = {}

    def fake_post_form(path, payload, params=None):
      sent.update(payload)
      # Minimal valid games response: a table with an empty body.
      return '{"msg": "<table><tbody></tbody></table>"}'

    monkeypatch.setattr(client, "_post_form", fake_post_form)
    client.list_games(**kwargs)
    return sent

  def test_both_bounds_translated_to_iso(self, monkeypatch):
    sent = self._capture(monkeypatch, date_from="12-06-2026", date_to="30-06-2026")
    assert sent["inicio"] == "2026-06-12"
    assert sent["fim"] == "2026-06-30"

  def test_date_from_only_backfills_open_upper_bound(self, monkeypatch):
    sent = self._capture(monkeypatch, date_from="12-06-2026")
    assert sent["inicio"] == "2026-06-12"
    assert sent["fim"] == "2999-12-31"

  def test_date_to_only_backfills_open_lower_bound(self, monkeypatch):
    sent = self._capture(monkeypatch, date_to="30-06-2026")
    assert sent["inicio"] == "1900-01-01"
    assert sent["fim"] == "2026-06-30"

  def test_no_dates_sends_empty_window(self, monkeypatch):
    sent = self._capture(monkeypatch)
    assert sent["inicio"] == ""
    assert sent["fim"] == ""
