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


# ─── resolve_batch_id_by_license ─────────────────────────────────────────────

def test_cache_license_to_batch_records_and_forgets(tmp_cache):
  tmp_cache.record_license_batch(301772, 42)
  assert tmp_cache.get_batch_id_by_license(301772) == 42

  tmp_cache.forget_license_batch(301772)
  assert tmp_cache.get_batch_id_by_license(301772) is None


def test_cache_forget_licenses_in_batch_wipes_all_matching(tmp_cache):
  tmp_cache.record_license_batch(301772, 42)
  tmp_cache.record_license_batch(301773, 42)
  tmp_cache.record_license_batch(301774, 99)

  tmp_cache.forget_licenses_in_batch(42)

  assert tmp_cache.get_batch_id_by_license(301772) is None
  assert tmp_cache.get_batch_id_by_license(301773) is None
  assert tmp_cache.get_batch_id_by_license(301774) == 99


def test_cache_invalidate_wipes_license_to_batch(tmp_cache):
  tmp_cache.record_license_batch(301772, 42)
  tmp_cache.invalidate()
  assert tmp_cache.get_batch_id_by_license(301772) is None


def test_resolve_batch_id_by_license_cache_hit_validates(monkeypatch, tmp_cache):
  from sav_client.sav_client import SavClient

  client = SavClient.__new__(SavClient)
  client._cache = tmp_cache
  tmp_cache.record_license_batch(301772, 42)

  probe_calls: list[tuple[int, int]] = []

  def _probe(batch_id, license):
    probe_calls.append((batch_id, license))
    return {"id": 77, "nome": "Player A"}

  monkeypatch.setattr(client, "load_existing_registration_record", _probe, raising=False)
  # The resolver always fetches batch state once to verify the cached id
  # still points at an open batch — that call is allowed; per-batch items
  # scanning is not.
  monkeypatch.setattr(
    client, "list_player_registration_batches",
    lambda: [_batch(42, "2025/42")],
    raising=False,
  )

  def _no_scan(batch_id):
    raise AssertionError("per-batch item scan should not run on valid cache hit")

  monkeypatch.setattr(client, "list_player_registration_batch_items", _no_scan, raising=False)

  assert client.resolve_batch_id_by_license(301772) == 42
  assert probe_calls == [(42, 301772)]


def test_resolve_batch_id_by_license_cache_stale_falls_through(monkeypatch, tmp_cache):
  from sav_client.exceptions import SavRecordNotFoundError
  from sav_client.sav_client import SavClient

  client = SavClient.__new__(SavClient)
  client._cache = tmp_cache
  tmp_cache.record_license_batch(301772, 42)

  def _stale(batch_id, license):
    raise SavRecordNotFoundError("player not in this batch")

  monkeypatch.setattr(client, "load_existing_registration_record", _stale, raising=False)
  # 42 is still open in the listing, so the resolver will probe it; the
  # probe says the player isn't there, so we scan and find them in 99.
  monkeypatch.setattr(
    client, "list_player_registration_batches",
    lambda: [_batch(42, "2025/42"), _batch(99, "2025/99")],
    raising=False,
  )
  monkeypatch.setattr(
    client, "list_player_registration_batch_items",
    lambda batch_id: [{"license": 301772, "name": "Player A"}] if batch_id == 99 else [],
    raising=False,
  )

  assert client.resolve_batch_id_by_license(301772) == 99
  # The stale entry should have been forgotten and replaced.
  assert tmp_cache.get_batch_id_by_license(301772) == 99


def test_resolve_batch_id_by_license_cache_closed_forgets_and_scans(monkeypatch, tmp_cache):
  """A cached batch that has transitioned to closed must be forgotten."""
  from sav_client.sav_client import SavClient

  client = SavClient.__new__(SavClient)
  client._cache = tmp_cache
  tmp_cache.record_license_batch(301772, 42)

  # Batch 42 is no longer open (e.g., admin submitted it server-side).
  closed = _batch(42, "2025/42")
  object.__setattr__(closed, "state_id", 3)  # "Em Validação"
  open_b = _batch(99, "2025/99")

  monkeypatch.setattr(
    client, "list_player_registration_batches",
    lambda: [closed, open_b],
    raising=False,
  )

  def _must_not_probe(batch_id, license):
    raise AssertionError("must not probe a cached batch that is no longer open")

  monkeypatch.setattr(client, "load_existing_registration_record", _must_not_probe, raising=False)
  monkeypatch.setattr(
    client, "list_player_registration_batch_items",
    lambda batch_id: [{"license": 301772, "name": "Player A"}] if batch_id == 99 else [],
    raising=False,
  )

  assert client.resolve_batch_id_by_license(301772) == 99
  assert tmp_cache.get_batch_id_by_license(301772) == 99


def test_resolve_batch_id_by_license_propagates_probe_connection_error(monkeypatch, tmp_cache):
  """A transport error during the cache-probe must not be masked as a stale hit."""
  from sav_client.exceptions import SavConnectionError
  from sav_client.sav_client import SavClient

  client = SavClient.__new__(SavClient)
  client._cache = tmp_cache
  tmp_cache.record_license_batch(301772, 42)

  monkeypatch.setattr(
    client, "list_player_registration_batches",
    lambda: [_batch(42, "2025/42")],
    raising=False,
  )

  def _network_blip(batch_id, license):
    raise SavConnectionError("connection reset")

  monkeypatch.setattr(client, "load_existing_registration_record", _network_blip, raising=False)
  monkeypatch.setattr(
    client, "list_player_registration_batch_items",
    lambda batch_id: (_ for _ in ()).throw(AssertionError("must not fall through on transport error")),
    raising=False,
  )

  with pytest.raises(SavConnectionError, match="connection reset"):
    client.resolve_batch_id_by_license(301772)
  # The cache entry must be preserved — the failure was transport, not staleness.
  assert tmp_cache.get_batch_id_by_license(301772) == 42


def test_resolve_batch_id_by_license_propagates_probe_parse_error(monkeypatch, tmp_cache):
  """A response-shape error during probe must not be masked as a stale hit.

  Only ``SavRecordNotFoundError`` (a well-formed "not in this batch" response)
  should be treated as stale-cache. A generic ``SavResponseError`` — e.g. the
  server returned unparseable JSON — is a real failure and must propagate.
  """
  from sav_client.exceptions import SavResponseError
  from sav_client.sav_client import SavClient

  client = SavClient.__new__(SavClient)
  client._cache = tmp_cache
  tmp_cache.record_license_batch(301772, 42)

  monkeypatch.setattr(
    client, "list_player_registration_batches",
    lambda: [_batch(42, "2025/42")],
    raising=False,
  )

  def _broken_response(batch_id, license):
    raise SavResponseError("Could not parse existing record: '<html>500</html>'")

  monkeypatch.setattr(client, "load_existing_registration_record", _broken_response, raising=False)
  monkeypatch.setattr(
    client, "list_player_registration_batch_items",
    lambda batch_id: (_ for _ in ()).throw(AssertionError("must not fall through on parse error")),
    raising=False,
  )

  with pytest.raises(SavResponseError, match="Could not parse"):
    client.resolve_batch_id_by_license(301772)
  # The cache entry must be preserved — the failure was a server bug, not staleness.
  assert tmp_cache.get_batch_id_by_license(301772) == 42


def test_resolve_batch_id_by_license_propagates_scan_connection_error(monkeypatch, tmp_cache):
  """A transport error while scanning open batches must propagate."""
  from sav_client.exceptions import SavConnectionError
  from sav_client.sav_client import SavClient

  client = SavClient.__new__(SavClient)
  client._cache = tmp_cache

  monkeypatch.setattr(
    client, "list_player_registration_batches",
    lambda: [_batch(12, "2025/12"), _batch(13, "2025/13")],
    raising=False,
  )

  def _fail_first(batch_id):
    if batch_id == 12:
      raise SavConnectionError("connection reset")
    return [{"license": 301772, "name": "Player A"}]

  monkeypatch.setattr(client, "list_player_registration_batch_items", _fail_first, raising=False)

  with pytest.raises(SavConnectionError, match="connection reset"):
    client.resolve_batch_id_by_license(301772)


def test_resolve_batch_id_by_license_scans_open_batches(monkeypatch, tmp_cache):
  from sav_client.sav_client import SavClient

  client = SavClient.__new__(SavClient)
  client._cache = tmp_cache

  closed = _batch(11, "2025/11")
  object.__setattr__(closed, "state_id", 2)  # not "Em construção"
  open_a = _batch(12, "2025/12")
  open_b = _batch(13, "2025/13")

  scan_calls: list[int] = []

  def _items(batch_id):
    scan_calls.append(batch_id)
    if batch_id == 13:
      return [{"license": 301772, "name": "Player A"}]
    return [{"license": 999, "name": "Other"}]

  monkeypatch.setattr(
    client, "list_player_registration_batches",
    lambda: [closed, open_a, open_b],
    raising=False,
  )
  monkeypatch.setattr(client, "list_player_registration_batch_items", _items, raising=False)

  assert client.resolve_batch_id_by_license(301772) == 13
  # Closed batch must be skipped, then the two open ones scanned in order.
  assert scan_calls == [12, 13]
  # Successful scan caches the result.
  assert tmp_cache.get_batch_id_by_license(301772) == 13


def test_resolve_batch_id_by_license_raises_when_not_enrolled(monkeypatch, tmp_cache):
  from sav_client.exceptions import LicenseNotEnrolledError
  from sav_client.sav_client import SavClient

  client = SavClient.__new__(SavClient)
  client._cache = tmp_cache

  open_a = _batch(12, "2025/12")
  open_b = _batch(13, "2025/13")

  monkeypatch.setattr(
    client, "list_player_registration_batches",
    lambda: [open_a, open_b],
    raising=False,
  )
  monkeypatch.setattr(
    client, "list_player_registration_batch_items",
    lambda batch_id: [],
    raising=False,
  )

  with pytest.raises(LicenseNotEnrolledError) as excinfo:
    client.resolve_batch_id_by_license(301772)

  err = excinfo.value
  assert err.license == 301772
  numbers = [b["number"] for b in err.open_batches]
  assert numbers == ["2025/12", "2025/13"]
