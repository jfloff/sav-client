import pytest

from sav_client import SavClient
from sav_client.models import Player
from sav_client.exceptions import SavResponseError


class TestSearchPlayers:
  def test_requires_login(self):
    c = SavClient("https://sav2.fpb.pt", "user", "pass")
    with pytest.raises(SavResponseError, match="Must call login"):
      c.search_players()

  def test_requires_explicit_club_scope(self):
    c = SavClient("https://sav2.fpb.pt", "user", "pass")
    c.session = {"epoca_id": 123, "organizacao": 456}
    with pytest.raises(ValueError, match="club is required"):
      c.search_players()

  def test_returns_players_from_live_search(self, client):
    club_id = int(client.session.get("organizacao") or 0)
    results = client.search_players(club=club_id)

    assert isinstance(results, list)
    assert results

    first = results[0]
    assert first.id > 0
    assert first.license
    assert first.name
    assert first.club

  def test_can_find_player_by_license(self, client, sample_player):
    results = client.search_players(license=sample_player.license, club=0)

    assert results
    assert any(player.id == sample_player.id for player in results)

  def test_can_search_all_seasons_for_sample_player(self, client, sample_player):
    results = client.search_players(license=sample_player.license, season=0, club=0)

    assert results
    assert any(player.license == sample_player.license for player in results)

  def test_can_search_players_across_multiple_clubs(self, client):
    clubs = []
    for association in client.list_associations():
      clubs = client.list_clubs(association=association.id)
      if len(clubs) >= 2:
        break
    if len(clubs) < 2:
      pytest.skip("Need at least 2 clubs in one association to test multi-club search")

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

  def test_can_filter_players_by_active_status_case_insensitively(self, monkeypatch):
    client = SavClient("https://sav2.fpb.pt", "user", "pass")
    client.session = {"epoca_id": 123, "organizacao": 456}

    def fake_search_single(**kwargs):
      return [
        Player(
          id=1,
          license="100",
          name="A",
          association="AB X",
          club="Club X",
          tier="Mini 12",
          gender="Masculino",
          birth_date="2015-01-01",
          nationality="Portuguesa",
          status="FBP",
          season="2025/2026",
          active=True,
        ),
        Player(
          id=2,
          license="101",
          name="B",
          association="AB X",
          club="Club X",
          tier="Mini 12",
          gender="Masculino",
          birth_date="2015-01-02",
          nationality="Portuguesa",
          status="Local",
          season="2025/2026",
          active=False,
        ),
      ]

    monkeypatch.setattr(client, "_search_players_single", fake_search_single)

    results = client.search_players(status="ACTIVE", club=789)

    assert [player.id for player in results] == [1]
    assert results[0].active is True

  def test_status_filter_applies_before_final_limit_for_parallel_searches(self, monkeypatch):
    client = SavClient("https://sav2.fpb.pt", "user", "pass")
    client.session = {"epoca_id": 123, "organizacao": 456}
    captured = {}

    def fake_search_club_list(club_ids, *, limit=None, **filters):
      captured["club_ids"] = club_ids
      captured["limit"] = limit
      captured["filters"] = filters
      return [
        Player(
          id=1,
          license="100",
          name="A",
          association="AB X",
          club="Club X",
          tier="Mini 12",
          gender="Masculino",
          birth_date="2015-01-01",
          nationality="Portuguesa",
          status="Local",
          season="2025/2026",
          active=False,
        ),
        Player(
          id=2,
          license="101",
          name="B",
          association="AB X",
          club="Club Y",
          tier="Mini 12",
          gender="Feminino",
          birth_date="2015-01-02",
          nationality="Portuguesa",
          status="FBP",
          season="2025/2026",
          active=True,
        ),
        Player(
          id=3,
          license="102",
          name="C",
          association="AB X",
          club="Club Z",
          tier="Mini 12",
          gender="Feminino",
          birth_date="2015-01-03",
          nationality="Portuguesa",
          status="FBP",
          season="2025/2026",
          active=True,
        ),
      ]

    monkeypatch.setattr(client, "_search_club_list", fake_search_club_list)

    results = client.search_players(club=[10, 11], status="active", limit=1)

    assert captured["club_ids"] == [10, 11]
    assert captured["limit"] is None
    assert [player.id for player in results] == [2]

  def test_omitted_association_is_forwarded_as_none(self, monkeypatch):
    client = SavClient("https://sav2.fpb.pt", "user", "pass")
    client.session = {"epoca_id": 123, "organizacao": 456}
    captured = {}

    def fake_search_single(**kwargs):
      captured.update(kwargs)
      return []

    monkeypatch.setattr(client, "_search_players_single", fake_search_single)

    client.search_players(club=789)

    assert captured["association"] is None

  def test_association_zero_is_rejected(self):
    client = SavClient("https://sav2.fpb.pt", "user", "pass")
    client.session = {"epoca_id": 123, "organizacao": 456}

    with pytest.raises(ValueError, match="association=0 is no longer supported"):
      client.search_players(club=0, association=0)

  def test_omitted_association_is_sent_as_empty_string_in_request_payload(self, monkeypatch):
    client = SavClient("https://sav2.fpb.pt", "user", "pass")
    client.session = {"epoca_id": 123, "organizacao": 456, "perfil": 1, "user": "tester"}
    captured = {}

    monkeypatch.setattr(client, "_post_form", lambda path, payload, params=None: captured.update({
      "path": path,
      "payload": payload,
      "params": params,
    }) or "<table><tbody></tbody></table>")

    client._search_players_single(club=789, association=None)

    assert captured["payload"]["jc_associacao"] == ""
