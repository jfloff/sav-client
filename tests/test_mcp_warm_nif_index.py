"""Offline MCP tests for warm_nif_index."""

import pytest

from sav_mcp import server as server_module


class _StubClient:
  def __init__(self):
    self.session = {"organizacao": 200}
    self.calls: list[tuple[int, str, bool]] = []

  def build_nif_index(self, club_id, *, scope="recent", force=False):
    self.calls.append((club_id, scope, force))
    return {
      "club_id": club_id,
      "scope": scope,
      "players_indexed": 17,
      "built_at": 1_700_000_000.0,
      "from_cache": False,
    }


def test_warm_nif_index_delegates_and_serializes_built_at(monkeypatch):
  stub = _StubClient()
  monkeypatch.setattr(server_module, "_get_client", lambda: stub)

  result = server_module.warm_nif_index(scope="full", force=True)

  assert stub.calls == [(200, "full", True)]
  assert result == {
    "club_id": 200,
    "scope": "full",
    "players_indexed": 17,
    "built_at": "2023-11-14T22:13:20+00:00",
    "from_cache": False,
  }


def test_warm_nif_index_raises_without_a_club(monkeypatch):
  stub = _StubClient()
  stub.session = {}
  monkeypatch.setattr(server_module, "_get_client", lambda: stub)

  with pytest.raises(ValueError, match="requires a club_id"):
    server_module.warm_nif_index()

  assert stub.calls == []


def test_warm_nif_index_raises_on_explicit_club_zero(monkeypatch):
  stub = _StubClient()
  monkeypatch.setattr(server_module, "_get_client", lambda: stub)

  with pytest.raises(ValueError, match="requires a club_id"):
    server_module.warm_nif_index(club_id=0)

  assert stub.calls == []
