"""Offline coverage for staged, persistent NIF roster indexing."""

from collections import Counter
from types import SimpleNamespace

import pytest

from sav_client import SavClient
from sav_client.exceptions import SavError


@pytest.fixture
def client(monkeypatch, tmp_path):
  monkeypatch.setattr("sav_client.cache._CACHE_DIR", tmp_path)
  result = SavClient("https://example.invalid", "user", "pass")
  result.session = {"organizacao": 200, "epoca_id": 20}
  monkeypatch.setattr(result, "_recent_season_ids", lambda: [20, 19])
  return result


def _players(*licenses):
  return [SimpleNamespace(license=str(license)) for license in licenses]


def _stub_rosters(monkeypatch, client, rosters, nifs):
  search_calls = []
  profile_calls = Counter()

  def search_players(*, club, season):
    assert club == 200
    search_calls.append(season)
    return _players(*rosters.get(season, ()))

  def load_player_profile(license, *, club_id):
    assert club_id == 200
    profile_calls[license] += 1
    return {"nif": nifs.get(license, "")}

  monkeypatch.setattr(client, "search_players", search_players)
  monkeypatch.setattr(client, "load_player_profile", load_player_profile)
  return search_calls, profile_calls


def test_recent_roster_hit_never_scans_all_seasons(monkeypatch, client):
  search_calls, profile_calls = _stub_rosters(
    monkeypatch,
    client,
    {20: (101,), 19: (102,)},
    {101: "111111111", 102: "222222222"},
  )

  assert client.find_license_by_nif("222222222") == 102
  assert search_calls == [20, 19]
  assert 0 not in search_calls
  assert profile_calls == Counter({101: 1, 102: 1})


def test_historical_hit_escalates_without_refetching_recent_profiles(
  monkeypatch, client,
):
  search_calls, profile_calls = _stub_rosters(
    monkeypatch,
    client,
    {20: (101,), 19: (101, 102), 0: (101, 102, 103)},
    {
      101: "111111111",
      102: "222222222",
      103: "333333333",
    },
  )

  assert client.find_license_by_nif("333333333") == 103
  assert search_calls == [20, 19, 0]
  assert profile_calls == Counter({101: 1, 102: 1, 103: 1})


def test_fresh_full_marker_makes_miss_without_http(monkeypatch, client):
  client._cache.record_nif_index(200, "full", 75)
  search = pytest.fail
  profile = pytest.fail
  monkeypatch.setattr(client, "search_players", search)
  monkeypatch.setattr(client, "load_player_profile", profile)

  assert client.find_license_by_nif("999999999") is None


def test_refresh_bypasses_positive_cache_and_markers(monkeypatch, client):
  client._cache.record_player_nifs([(101, "111111111")])
  client._cache.record_nif_index(200, "full", 1)
  search_calls, profile_calls = _stub_rosters(
    monkeypatch,
    client,
    {0: (101,)},
    {101: "222222222"},
  )

  assert client.find_license_by_nif("111111111", refresh=True) is None
  assert client._cache.get_license_by_nif("222222222") == 101
  assert search_calls == [0]
  assert profile_calls == Counter({101: 1})


def test_roster_failure_records_no_marker_and_retries(monkeypatch, client):
  search_calls = []

  def fail_search(*, club, season):
    search_calls.append(season)
    raise SavError("temporary failure")

  monkeypatch.setattr(client, "search_players", fail_search)
  monkeypatch.setattr(
    client, "load_player_profile",
    lambda *args, **kwargs: pytest.fail("profiles require a roster"),
  )

  assert client.find_license_by_nif("999999999") is None
  assert client._cache.get_nif_index(200, "recent", ttl=300) is None
  assert client._cache.get_nif_index(200, "full", ttl=300) is None
  assert client.find_license_by_nif("999999999") is None
  assert search_calls == [20, 0, 20, 0]


def test_full_build_also_refreshes_recent_marker(monkeypatch, client):
  search_calls, profile_calls = _stub_rosters(
    monkeypatch,
    client,
    {0: (101, 102)},
    {101: "111111111", 102: "222222222"},
  )

  result = client.build_nif_index(200, scope="full")

  assert result["players_indexed"] == 2
  assert result["from_cache"] is False
  assert client._cache.get_nif_index(200, "full", ttl=300) is not None
  assert client._cache.get_nif_index(200, "recent", ttl=300) is not None
  assert search_calls == [0]
  assert profile_calls == Counter({101: 1, 102: 1})


def test_successful_primeira_enrolment_clears_both_markers(monkeypatch, client):
  batch = SimpleNamespace(
    id=50, is_open=True, type_id=1, state="Em construção", club_id=200,
  )
  monkeypatch.setattr(
    client, "list_player_registration_batches", lambda: [batch],
  )
  monkeypatch.setattr(
    client, "_add_player_to_primeira_batch", lambda *args, **kwargs: 700,
  )
  client._cache.record_nif_index(200, "recent", 10)
  client._cache.record_nif_index(200, "full", 20)

  result = client.add_player_to_registration_batch(
    50,
    name="New Player",
    birth_date="2010-01-01",
    gender_id=1,
    nif="123456789",
    id_type=1,
    id_number="12345678",
    id_expiry="2030-01-01",
    email="player@example.com",
    morada="Rua Um",
    cod_postal="1000-001",
    distrito_id=1,
    concelho_id=2,
  )

  assert result == 700
  assert client._cache.get_nif_index(200, "recent", ttl=300) is None
  assert client._cache.get_nif_index(200, "full", ttl=300) is None
