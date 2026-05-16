"""Unit tests for the batch_number ↔ batch_id cache and SDK resolver."""

import pytest

from sav_client.cache import Cache
from sav_client.models import PlayerRegistrationBatch


@pytest.fixture
def tmp_cache(monkeypatch, tmp_path):
  monkeypatch.setattr("sav_client.cache._CACHE_DIR", tmp_path)
  return Cache()


def _batch(id: int, number: str) -> PlayerRegistrationBatch:
  return PlayerRegistrationBatch(
    id=id, number=number,
    type_id=2, type="Revalidação",
    association_id=0, association="",
    club_id=0, club="",
    tier_id=5, tier="Sub 14",
    gender_id=1, gender="Masculino",
    state_id=1, state="Em construção",
    state_date="2026-01-01",
    item_count=0,
    season_id=2026, season="2025/2026",
  )


def test_cache_records_and_retrieves_batch_mapping(tmp_cache):
  tmp_cache.record_batches([("2025/00123", 42), ("2025/00124", 43)])

  assert tmp_cache.get_batch_id("2025/00123") == 42
  assert tmp_cache.get_batch_id("2025/00124") == 43
  assert tmp_cache.get_batch_number(42) == "2025/00123"


def test_cache_returns_none_for_unknown_batch(tmp_cache):
  assert tmp_cache.get_batch_id("nope") is None
  assert tmp_cache.get_batch_number(999) is None


def test_cache_replaces_existing_pair(tmp_cache):
  tmp_cache.record_batches([("X", 1)])
  tmp_cache.record_batches([("X", 99)])
  assert tmp_cache.get_batch_id("X") == 99


def test_cache_record_batches_noop_on_empty(tmp_cache):
  tmp_cache.record_batches([])
  assert tmp_cache.get_batch_id("anything") is None


def test_cache_invalidate_wipes_batch_mapping(tmp_cache):
  tmp_cache.record_batches([("X", 1)])
  tmp_cache.invalidate()
  assert tmp_cache.get_batch_id("X") is None


def test_resolve_batch_id_hits_cache(monkeypatch, tmp_cache):
  from sav_client.sav_client import SavClient

  client = SavClient.__new__(SavClient)
  client._cache = tmp_cache
  tmp_cache.record_batches([("2025/00500", 500)])

  def _fail():
    raise AssertionError("list_player_registration_batches should not run on cache hit")

  monkeypatch.setattr(client, "list_player_registration_batches", _fail, raising=False)

  assert client.resolve_batch_id("2025/00500") == 500


def test_resolve_batch_id_refreshes_on_miss(monkeypatch, tmp_cache):
  from sav_client.sav_client import SavClient

  client = SavClient.__new__(SavClient)
  client._cache = tmp_cache
  calls = {"n": 0}

  def _fetch(*args, **kwargs):
    calls["n"] += 1
    # The real list_*() populates the cache; mimic that here.
    tmp_cache.record_batches([("2025/00600", 600)])
    return [_batch(600, "2025/00600")]

  monkeypatch.setattr(client, "list_player_registration_batches", _fetch, raising=False)

  assert client.resolve_batch_id("2025/00600") == 600
  assert calls["n"] == 1


def test_resolve_batch_id_raises_on_unknown(monkeypatch, tmp_cache):
  from sav_client.sav_client import SavClient

  client = SavClient.__new__(SavClient)
  client._cache = tmp_cache

  monkeypatch.setattr(client, "list_player_registration_batches", lambda: [], raising=False)

  with pytest.raises(ValueError, match="not found"):
    client.resolve_batch_id("missing")


def test_resolve_batch_id_rejects_empty(tmp_cache):
  from sav_client.sav_client import SavClient

  client = SavClient.__new__(SavClient)
  client._cache = tmp_cache

  with pytest.raises(ValueError, match="must not be empty"):
    client.resolve_batch_id("")
