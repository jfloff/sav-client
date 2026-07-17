"""Short-TTL cross-invocation cache for game listings.

Two layers are covered: Cache.get_games (round-trip + TTL) and the opt-in
list_games(use_cache=...) wiring — including that it stays off by default so
live callers always hit the server."""

import pytest

from sav_client.cache import Cache
from sav_client.models import Game


@pytest.fixture
def tmp_cache(monkeypatch, tmp_path):
  monkeypatch.setattr("sav_client.cache._CACHE_DIR", tmp_path)
  return Cache()


def _game(id: int) -> Game:
  return Game(
    id=id, number=f"S14M-{id:03d}", competition="Liga", phase="1ª Fase",
    round="1", date="12-04-2026", time="10:00", home="A", away="B",
    home_score="", away_score="", venue="Pavilhão", game_status="Marcado",
    result_status="Sem Resultado", tier="Sub 14", gender="Masculino",
    level="Sub 14 M",
  )


# ── Cache.get_games ───────────────────────────────────────────────────────────

def test_get_games_caches_and_reconstructs(tmp_cache):
  calls = {"n": 0}

  def fetch():
    calls["n"] += 1
    return [_game(1), _game(2)]

  first = tmp_cache.get_games(fetch, "sig-A", ttl=300)
  second = tmp_cache.get_games(fetch, "sig-A", ttl=300)

  assert calls["n"] == 1               # second served from cache
  assert second == [_game(1), _game(2)]  # frozen dataclass → field equality
  # A different query signature is a separate entry → fetches again.
  tmp_cache.get_games(fetch, "sig-B", ttl=300)
  assert calls["n"] == 2


def test_get_games_refetches_after_ttl(tmp_cache):
  calls = {"n": 0}

  def fetch():
    calls["n"] += 1
    return [_game(1)]

  tmp_cache.get_games(fetch, "sig", ttl=0)   # ttl 0 → row is always stale
  tmp_cache.get_games(fetch, "sig", ttl=0)
  assert calls["n"] == 2


def test_invalidate_wipes_games(tmp_cache):
  tmp_cache.get_games(lambda: [_game(1)], "sig", ttl=300)
  tmp_cache.invalidate()

  calls = {"n": 0}

  def fetch():
    calls["n"] += 1
    return [_game(1)]

  tmp_cache.get_games(fetch, "sig", ttl=300)
  assert calls["n"] == 1  # invalidate cleared the row → fetched again


# ── list_games(use_cache=...) ─────────────────────────────────────────────────

def _client(monkeypatch, tmp_cache):
  from sav_client.sav_client import SavClient

  client = SavClient.__new__(SavClient)
  client._cache = tmp_cache
  client.session = {"user": "u", "organizacao": 1, "perfil": 1, "epoca_id": 100}
  calls = {"n": 0}

  def fake_post_form(path, payload, params=None):
    calls["n"] += 1
    return '{"msg": "<table><tbody></tbody></table>"}'

  monkeypatch.setattr(client, "_post_form", fake_post_form)
  return client, calls


def test_use_cache_collapses_identical_listings(monkeypatch, tmp_cache):
  client, calls = _client(monkeypatch, tmp_cache)

  client.list_games(use_cache=True)
  client.list_games(use_cache=True)          # same signature → cache hit
  assert calls["n"] == 1

  client.list_games(game_number="X-1", use_cache=True)  # new signature → miss
  assert calls["n"] == 2


def test_use_cache_off_by_default_always_fetches(monkeypatch, tmp_cache):
  client, calls = _client(monkeypatch, tmp_cache)

  client.list_games()
  client.list_games()
  assert calls["n"] == 2  # no caching unless explicitly opted in
