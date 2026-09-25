"""Offline coverage for staged, persistent NIF roster indexing."""

from collections import Counter
from types import SimpleNamespace

import pytest

from sav_client import SavClient
from sav_client.exceptions import SavError
from sav_client.models import NifLicenses


@pytest.fixture
def client(monkeypatch, tmp_path):
  monkeypatch.setattr("sav_client.cache._CACHE_DIR", tmp_path)
  result = SavClient("https://example.invalid", "user", "pass")
  result.session = {"organizacao": 200, "epoca_id": 20}
  monkeypatch.setattr(result, "_recent_season_ids", lambda: [20, 19])
  return result


def _players(*licenses):
  return [SimpleNamespace(license=str(license)) for license in licenses]


def _stub_rosters(
  monkeypatch, client, rosters, nifs, *, failures=None, before_profile=None,
):
  search_calls = []
  profile_calls = Counter()

  def search_players(*, club, season):
    assert club == 200
    search_calls.append(season)
    return _players(*rosters.get(season, ()))

  def load_player_profile(license, *, club_id):
    assert club_id == 200
    profile_calls[license] += 1
    if before_profile is not None:
      before_profile(license)
    if failures and license in failures:
      raise failures[license]
    if nifs.get(license):
      return {"nif": nifs[license]}
    return {}

  monkeypatch.setattr(client, "search_players", search_players)
  monkeypatch.setattr(client, "load_player_profile", load_player_profile)
  return search_calls, profile_calls


def test_recent_nif_is_returned_after_all_seasons_coverage(monkeypatch, client):
  search_calls, profile_calls = _stub_rosters(
    monkeypatch,
    client,
    {20: (101,), 19: (102,)},
    {101: "111111111", 102: "222222222"},
  )

  assert client.find_licenses_by_nif("222222222") == NifLicenses([102], True)
  assert search_calls == [20, 19, 0]
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

  assert client.find_licenses_by_nif("333333333") == NifLicenses([103], True)
  assert search_calls == [20, 19, 0]
  assert profile_calls == Counter({101: 1, 102: 1, 103: 1})


def test_fresh_full_marker_makes_miss_without_http(monkeypatch, client):
  client._cache.record_nif_index(200, 75)
  search = pytest.fail
  profile = pytest.fail
  monkeypatch.setattr(client, "search_players", search)
  monkeypatch.setattr(client, "load_player_profile", profile)

  assert client.find_licenses_by_nif("999999999") == NifLicenses([], True)


def test_forced_index_rebuild_bypasses_cached_profiles_and_marker(
  monkeypatch, client,
):
  client._cache.record_player_nifs([(101, "111111111")])
  client._cache.record_nif_index(200, 1)
  search_calls, profile_calls = _stub_rosters(
    monkeypatch,
    client,
    {0: (101,)},
    {101: "222222222"},
  )

  result = client.build_nif_index(force=True)
  assert result["complete"] is True
  assert client._cache.get_licenses_by_nif("111111111") == []
  assert client._cache.get_licenses_by_nif("222222222") == [101]
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

  assert client.find_licenses_by_nif("999999999") == NifLicenses([], False)
  assert client._cache.get_nif_index(200, ttl=300) is None
  assert client.find_licenses_by_nif("999999999") == NifLicenses([], False)
  assert search_calls == [20, 19, 0, 20, 19, 0]


def test_full_build_writes_single_club_marker(monkeypatch, client):
  search_calls, profile_calls = _stub_rosters(
    monkeypatch,
    client,
    {0: (101, 102)},
    {101: "111111111", 102: "222222222"},
  )

  result = client.build_nif_index()

  assert result["players_enumerated"] == 2
  assert result["players_indexed"] == 2
  assert result["unresolved"] == []
  assert result["complete"] is True
  assert result["from_cache"] is False
  assert client._cache.get_nif_index(200, ttl=300) is not None
  assert search_calls == [0]
  assert profile_calls == Counter({101: 1, 102: 1})


def test_build_reports_shared_nif_counts_for_enumerated_roster_and_cache(
  monkeypatch, client,
):
  search_calls, _ = _stub_rosters(
    monkeypatch,
    client,
    {0: (101, 102, 103, 104)},
    {
      101: "111111111", 102: "111111111",
      103: "999999990", 104: "999999990",
    },
  )
  client._cache.record_player_nifs([
    (900, "222222222"), (901, "222222222"),
  ])

  built = client.build_nif_index()
  cached = client.build_nif_index()

  assert built["shared_nif_groups"] == 2
  assert built["licenses_on_shared_nifs"] == 4
  assert built["from_cache"] is False
  assert cached["from_cache"] is True
  assert cached["shared_nif_groups"] == 3
  assert cached["licenses_on_shared_nifs"] == 6
  assert search_calls == [0]


def test_successful_primeira_enrolment_clears_club_marker(monkeypatch, client):
  batch = SimpleNamespace(
    id=50, is_open=True, type_id=1, state="Em construção", club_id=200,
  )
  monkeypatch.setattr(
    client, "list_player_registration_batches", lambda: [batch],
  )
  monkeypatch.setattr(
    client, "_add_player_to_primeira_batch", lambda *args, **kwargs: 700,
  )
  client._cache.record_nif_index(200, 20)

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
  assert client._cache.get_nif_index(200, ttl=300) is None


def test_raising_profile_fetch_blocks_marker_and_is_retried(
  monkeypatch, client,
):
  _, profile_calls = _stub_rosters(
    monkeypatch,
    client,
    {0: (101, 102, 103)},
    {101: "111111111", 103: "333333333"},
    failures={102: SavError("temporary profile failure")},
  )

  assert client.find_licenses_by_nif("999999999") == NifLicenses([], False)
  assert client._cache.get_nif_index(200, ttl=300) is None
  first_attempts = profile_calls[102]

  assert client.find_licenses_by_nif("999999999") == NifLicenses([], False)
  assert profile_calls[102] > first_attempts
  assert client._cache.get_nif_index(200, ttl=300) is None


def test_no_nif_on_file_is_covered_not_unresolved(monkeypatch, client):
  """A licence with no NIF is a scanned fact, so it cannot block coverage.

  Measured live, ~21% of a real club's licences have no NIF on file. Treating
  those as failures makes the marker unwritable and every miss a full rescan
  forever, so they count as covered: they can never match a NIF query.
  """
  _, profile_calls = _stub_rosters(
    monkeypatch,
    client,
    {0: (101, 102)},
    {101: "111111111"},
  )

  assert client.find_licenses_by_nif("999999999") == NifLicenses([], True)
  assert client._cache.get_nif_index(200, ttl=300) is not None

  result = client.build_nif_index()

  assert result["complete"] is True
  assert result["unresolved"] == []
  assert result["no_nif"] == []  # already recorded, so not rescanned
  assert profile_calls[102] == 1

  # The empty row marks 102 scanned; a blank query must not match it.
  assert client._cache.known_nif_licenses([102]) == {102}
  assert client._cache.get_licenses_by_nif("") == []


def test_exhaustive_miss_writes_marker_and_next_miss_uses_no_http(
  monkeypatch, client,
):
  _stub_rosters(
    monkeypatch,
    client,
    {0: (101, 102)},
    {101: "111111111", 102: "222222222"},
  )

  assert client.find_licenses_by_nif("999999999") == NifLicenses([], True)
  assert client._cache.get_nif_index(200, ttl=300) is not None

  monkeypatch.setattr(
    client, "search_players", lambda **kwargs: pytest.fail("unexpected HTTP"),
  )
  monkeypatch.setattr(
    client,
    "load_player_profile",
    lambda *args, **kwargs: pytest.fail("unexpected HTTP"),
  )

  assert client.find_licenses_by_nif("888888888") == NifLicenses([], True)


def test_cache_returns_every_license_for_a_nif_in_ascending_order(client):
  client._cache.record_player_nifs([
    (102, "123456789"),
    (101, "123456789"),
    (103, ""),
  ])

  assert client._cache.get_licenses_by_nif("123456789") == [101, 102]
  assert client._cache.get_licenses_by_nif("") == []


def test_find_licenses_by_nif_scans_all_matches_without_early_exit(
  monkeypatch, client,
):
  search_calls, profile_calls = _stub_rosters(
    monkeypatch,
    client,
    {20: (102, 101), 19: (103,), 0: (101, 102, 103)},
    {
      101: "123456789",
      102: "123456789",
      103: "987654321",
    },
  )

  result = client.find_licenses_by_nif("123456789")

  assert result == NifLicenses(licenses=[101, 102], complete=True)
  assert search_calls == [20, 19, 0]
  assert profile_calls == Counter({101: 1, 102: 1, 103: 1})
  assert client._cache.get_nif_index(200, ttl=300) is not None


def test_find_licenses_by_nif_fresh_marker_uses_cached_matches_without_http(
  monkeypatch, client,
):
  client._cache.record_player_nifs([
    (202, "123456789"),
    (201, "123456789"),
  ])
  client._cache.record_nif_index(200, 2)
  monkeypatch.setattr(
    client, "search_players", lambda **kwargs: pytest.fail("unexpected HTTP"),
  )
  monkeypatch.setattr(
    client,
    "load_player_profile",
    lambda *args, **kwargs: pytest.fail("unexpected HTTP"),
  )

  result = client.find_licenses_by_nif("123456789")

  assert result == NifLicenses(licenses=[201, 202], complete=True)


def test_find_licenses_by_nif_unresolved_profile_leaves_coverage_unknown(
  monkeypatch, client,
):
  _stub_rosters(
    monkeypatch,
    client,
    {0: (101, 102)},
    {101: "123456789"},
    failures={102: SavError("temporary profile failure")},
  )

  result = client.find_licenses_by_nif("123456789")

  assert result == NifLicenses(licenses=[101], complete=False)
  assert client._cache.get_nif_index(200, ttl=300) is None


def test_find_licenses_by_nif_blank_and_missing_session_club(monkeypatch, client):
  client.session = {"organizacao": 0}

  assert client.find_licenses_by_nif(None) == NifLicenses([], complete=True)
  with pytest.raises(ValueError, match="session club is required"):
    client.find_licenses_by_nif("123456789")
