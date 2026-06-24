"""Unit tests for SavClient.classify_enrollment_status — the bulk status pass.

The single-licence resolution branches are covered by test_batch_resolver.py;
here we cover the bulk collapse: one batch listing, one item scan per open
batch, one roster query, then in-memory classification.
"""

import pytest

from sav_client.cache import Cache
from sav_client.models import PlayerRegistrationBatch
from sav_client.sav_client import SavClient


@pytest.fixture
def tmp_cache(monkeypatch, tmp_path):
  monkeypatch.setattr("sav_client.cache._CACHE_DIR", tmp_path)
  return Cache()


def _batch(id, number, *, state_id=1):
  return PlayerRegistrationBatch(
    id=id, number=number,
    type_id=2, type="Revalidação",
    association_id=0, association="",
    club_id=0, club="",
    tier_id=5, tier="Sub 14",
    gender_id=1, gender="Masculino",
    state_id=state_id,
    state="Em construção" if state_id == 1 else "Em Validação",
    state_date="2026-01-01",
    item_count=0,
    season_id=2026, season="2025/2026",
  )


class _P:
  """Minimal Player stand-in carrying the fields classify reads."""

  def __init__(self, license, name="Player"):
    self.license = str(license)
    self.name = name


def _client(monkeypatch, tmp_cache, *, batches, items, roster):
  client = SavClient.__new__(SavClient)
  client._cache = tmp_cache
  client.session = {"organizacao": 7}
  monkeypatch.setattr(
    client, "list_player_registration_batches",
    lambda season=None: batches, raising=False,
  )
  monkeypatch.setattr(
    client, "list_player_registration_batch_items",
    lambda batch_id: items.get(batch_id, []), raising=False,
  )
  monkeypatch.setattr(client, "search_players", lambda **kw: roster, raising=False)
  return client


def test_classifies_pending_enrolled_and_not_enrolled(monkeypatch, tmp_cache):
  client = _client(
    monkeypatch, tmp_cache,
    batches=[_batch(12, "2025/12")],
    items={12: [{"license": 301772, "name": "Pending Player"}]},
    roster=[_P(301773, "Roster Player")],
  )

  out = client.classify_enrollment_status([301772, 301773, 999])

  assert out[301772]["status"] == "pending"
  assert out[301772]["batch"]["number"] == "2025/12"
  assert out[301772]["name"] == "Pending Player"
  assert out[301773] == {"status": "enrolled", "name": "Roster Player"}
  assert out[999]["status"] == "not_enrolled"
  assert out[999]["open_batches"] == [
    {"number": "2025/12", "tier": "Sub 14", "gender": "Masculino"},
  ]


def test_pending_wins_over_enrolled(monkeypatch, tmp_cache):
  # Same licence sits in an open batch *and* the active roster.
  client = _client(
    monkeypatch, tmp_cache,
    batches=[_batch(12, "2025/12")],
    items={12: [{"license": 301772, "name": "Both"}]},
    roster=[_P(301772, "Both")],
  )

  out = client.classify_enrollment_status([301772])

  assert out[301772]["status"] == "pending"


def test_closed_batches_are_not_scanned(monkeypatch, tmp_cache):
  scanned: list[int] = []

  client = SavClient.__new__(SavClient)
  client._cache = tmp_cache
  client.session = {"organizacao": 7}
  monkeypatch.setattr(
    client, "list_player_registration_batches",
    lambda season=None: [_batch(11, "2025/11", state_id=3), _batch(12, "2025/12")],
    raising=False,
  )

  def _items(batch_id):
    scanned.append(batch_id)
    return [{"license": 301772, "name": "P"}] if batch_id == 12 else []

  monkeypatch.setattr(client, "list_player_registration_batch_items", _items, raising=False)
  monkeypatch.setattr(client, "search_players", lambda **kw: [], raising=False)

  out = client.classify_enrollment_status([301772])

  assert scanned == [12]  # the closed batch (state_id=3) is skipped
  assert out[301772]["status"] == "pending"


def test_records_found_license_batch_in_cache(monkeypatch, tmp_cache):
  client = _client(
    monkeypatch, tmp_cache,
    batches=[_batch(12, "2025/12")],
    items={12: [{"license": 301772, "name": "P"}]},
    roster=[],
  )

  client.classify_enrollment_status([301772])

  assert tmp_cache.get_batch_id_by_license(301772) == 12


def test_roster_query_scoped_to_session_club_and_active(monkeypatch, tmp_cache):
  captured: dict = {}

  client = SavClient.__new__(SavClient)
  client._cache = tmp_cache
  client.session = {"organizacao": 7}
  monkeypatch.setattr(
    client, "list_player_registration_batches", lambda season=None: [], raising=False,
  )
  monkeypatch.setattr(
    client, "list_player_registration_batch_items", lambda batch_id: [], raising=False,
  )

  def _search(**kw):
    captured.update(kw)
    return [_P(301773)]

  monkeypatch.setattr(client, "search_players", _search, raising=False)

  client.classify_enrollment_status([301773])

  assert captured["club"] == 7
  assert captured["status"] == "active"


def test_no_roster_query_without_club(monkeypatch, tmp_cache):
  client = SavClient.__new__(SavClient)
  client._cache = tmp_cache
  client.session = {"organizacao": 0}
  monkeypatch.setattr(
    client, "list_player_registration_batches", lambda season=None: [], raising=False,
  )
  monkeypatch.setattr(
    client, "list_player_registration_batch_items", lambda batch_id: [], raising=False,
  )

  def _must_not_search(**kw):
    raise AssertionError("roster query must not run without a session club")

  monkeypatch.setattr(client, "search_players", _must_not_search, raising=False)

  out = client.classify_enrollment_status([301773])

  assert out[301773]["status"] == "not_enrolled"


def test_empty_input_returns_empty(monkeypatch, tmp_cache):
  client = _client(monkeypatch, tmp_cache, batches=[], items={}, roster=[])
  assert client.classify_enrollment_status([]) == {}
