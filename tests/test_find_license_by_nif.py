"""Offline coverage for staged, persistent NIF roster indexing."""

from collections import Counter
from threading import Event
from time import sleep
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
  client._cache.record_nif_index(200, 75)
  search = pytest.fail
  profile = pytest.fail
  monkeypatch.setattr(client, "search_players", search)
  monkeypatch.setattr(client, "load_player_profile", profile)

  assert client.find_license_by_nif("999999999") is None


def test_refresh_bypasses_positive_cache_and_markers(monkeypatch, client):
  client._cache.record_player_nifs([(101, "111111111")])
  client._cache.record_nif_index(200, 1)
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
  assert client._cache.get_nif_index(200, ttl=300) is None
  assert client.find_license_by_nif("999999999") is None
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


def test_early_exit_stops_large_roster_scan(monkeypatch, client):
  licenses = list(range(1000, 1040))
  target_started = Event()

  def hold_non_target(license):
    if license == 1001:
      target_started.set()
    else:
      target_started.wait()
      sleep(0.01)

  search_calls, profile_calls = _stub_rosters(
    monkeypatch,
    client,
    {20: tuple(licenses)},
    {license: str(license) for license in licenses},
    before_profile=hold_non_target,
  )

  assert client.find_license_by_nif("1001") == 1001
  assert search_calls == [20]
  assert len(profile_calls) < 40
  untouched = set(licenses) - set(profile_calls)
  assert untouched
  assert all(profile_calls.get(license, 0) == 0 for license in untouched)


def test_early_exit_persists_resolved_profiles_for_next_lookup(
  monkeypatch, client,
):
  """A cancelled scan still banks what it resolved.

  A single-player roster would pass this trivially, so use one big enough that
  the first lookup genuinely exits early with work left undone.
  """
  licenses = list(range(1000, 1040))
  target_started = Event()

  def hold_non_target(license):
    if license == 1001:
      target_started.set()
    else:
      target_started.wait()
      sleep(0.01)

  _, profile_calls = _stub_rosters(
    monkeypatch,
    client,
    {20: tuple(licenses), 19: (), 0: tuple(licenses)},
    {license: str(license) for license in licenses},
    before_profile=hold_non_target,
  )

  assert client.find_license_by_nif("1001") == 1001
  first_round = sum(profile_calls.values())
  assert first_round < len(licenses)

  assert client.find_license_by_nif("1039") == 1039
  second_round = sum(profile_calls.values()) - first_round

  # The second lookup reuses the first one's persisted rows instead of
  # re-scanning the roster. It is not free: profiles still in flight when the
  # first hit landed were paid for but discarded, so they are fetched again.
  assert second_round < len(licenses)
  assert profile_calls[1001] == 1


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

  assert client.find_license_by_nif("999999999") is None
  assert client._cache.get_nif_index(200, ttl=300) is None
  first_attempts = profile_calls[102]

  assert client.find_license_by_nif("999999999") is None
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

  assert client.find_license_by_nif("999999999") is None
  assert client._cache.get_nif_index(200, ttl=300) is not None

  result = client.build_nif_index()

  assert result["complete"] is True
  assert result["unresolved"] == []
  assert result["no_nif"] == []  # already recorded, so not rescanned
  assert profile_calls[102] == 1

  # The empty row marks 102 scanned; a blank query must not match it.
  assert client._cache.known_nif_licenses([102]) == {102}
  assert client._cache.get_license_by_nif("") is None


def test_exhaustive_miss_writes_marker_and_next_miss_uses_no_http(
  monkeypatch, client,
):
  _stub_rosters(
    monkeypatch,
    client,
    {0: (101, 102)},
    {101: "111111111", 102: "222222222"},
  )

  assert client.find_license_by_nif("999999999") is None
  assert client._cache.get_nif_index(200, ttl=300) is not None

  monkeypatch.setattr(
    client, "search_players", lambda **kwargs: pytest.fail("unexpected HTTP"),
  )
  monkeypatch.setattr(
    client,
    "load_player_profile",
    lambda *args, **kwargs: pytest.fail("unexpected HTTP"),
  )

  assert client.find_license_by_nif("888888888") is None
