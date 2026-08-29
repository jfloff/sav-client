"""Persistent cache coverage for staged club NIF indexes."""

import pytest

from sav_client.cache import Cache


@pytest.fixture
def cache(monkeypatch, tmp_path):
  monkeypatch.setattr("sav_client.cache._CACHE_DIR", tmp_path)
  return Cache()


def test_nif_index_round_trip_and_ttl(cache):
  cache.record_nif_index(10, 42)

  built_at, player_count = cache.get_nif_index(10, ttl=300)

  assert built_at > 0
  assert player_count == 42
  assert cache.get_nif_index(10, ttl=0) is None


def test_nif_index_is_keyed_by_club(cache):
  cache.record_nif_index(10, 5)

  assert cache.get_nif_index(10, ttl=300) is not None
  assert cache.get_nif_index(11, ttl=300) is None


def test_clear_nif_index_for_club_or_all(cache):
  cache.record_nif_index(10, 10)
  cache.record_nif_index(11, 11)

  cache.clear_nif_index(10)
  assert cache.get_nif_index(10, ttl=300) is None
  assert cache.get_nif_index(11, ttl=300) is not None

  cache.clear_nif_index()
  assert cache.get_nif_index(11, ttl=300) is None


def test_known_nif_licenses_chunks_large_inputs(cache):
  cache.record_player_nifs([
    (1, "100000001"),
    (999, "100000999"),
    (1000, "100001000"),
    (1205, "100001205"),
  ])

  assert cache.known_nif_licenses([]) == set()
  assert cache.known_nif_licenses(list(range(1, 1206))) == {
    1, 999, 1000, 1205,
  }


def test_invalidate_wipes_nif_index(cache):
  cache.record_nif_index(10, 50)

  cache.invalidate()

  assert cache.get_nif_index(10, ttl=300) is None
