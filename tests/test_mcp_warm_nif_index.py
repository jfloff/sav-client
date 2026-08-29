"""Offline MCP tests for warm_nif_index."""

import pytest

from sav_mcp import server as server_module


class _StubClient:
  def __init__(self, result=None):
    self.session = {"organizacao": 200}
    self.calls: list[bool] = []
    self.result = result or {
      "players_enumerated": 17,
      "players_indexed": 17,
      "unresolved": [],
      "complete": True,
      "built_at": 1_700_000_000.0,
      "from_cache": False,
    }

  def build_nif_index(self, *, force=False):
    self.calls.append(force)
    return {
      "club_id": int(self.session.get("organizacao") or 0),
      **self.result,
    }


def test_warm_nif_index_delegates_and_serializes_built_at(monkeypatch):
  stub = _StubClient()
  monkeypatch.setattr(server_module, "_get_client", lambda: stub)

  result = server_module.warm_nif_index(force=True)

  assert stub.calls == [True]
  assert result == {
    "club_id": 200,
    "players_enumerated": 17,
    "players_indexed": 17,
    "unresolved": [],
    "complete": True,
    "built_at": "2023-11-14T22:13:20+00:00",
    "from_cache": False,
  }


def test_warm_nif_index_raises_without_a_club(monkeypatch):
  stub = _StubClient()
  stub.session = {}
  monkeypatch.setattr(server_module, "_get_client", lambda: stub)

  with pytest.raises(ValueError, match="needs a session club"):
    server_module.warm_nif_index()

  assert stub.calls == []


def test_warm_nif_index_reports_incomplete_scan(monkeypatch):
  stub = _StubClient({
    "players_enumerated": 17,
    "players_indexed": 16,
    "unresolved": [42],
    "complete": False,
    "built_at": 1_700_000_000.0,
    "from_cache": False,
  })
  monkeypatch.setattr(server_module, "_get_client", lambda: stub)

  result = server_module.warm_nif_index()

  assert result["error"] == "incomplete_scan"
  assert result["unresolved"] == [42]
  assert result["built_at"] == "2023-11-14T22:13:20+00:00"
  assert stub.calls == [False]


def test_warm_nif_index_reports_roster_unavailable(monkeypatch):
  stub = _StubClient({
    "players_enumerated": 0,
    "players_indexed": 0,
    "unresolved": [],
    "complete": False,
    "built_at": 0.0,
    "from_cache": False,
  })
  monkeypatch.setattr(server_module, "_get_client", lambda: stub)

  result = server_module.warm_nif_index()

  assert result == {
    "club_id": 200,
    "players_enumerated": 0,
    "players_indexed": 0,
    "unresolved": [],
    "complete": False,
    "built_at": None,
    "from_cache": False,
    "error": "roster_unavailable",
  }
  assert stub.calls == [False]


def test_warm_nif_index_takes_no_club_id(monkeypatch):
  """The index only ever covers the session's own club, so there is no
  club to pass: SAV2 only exposes a player's NIF to their own club."""
  stub = _StubClient()
  monkeypatch.setattr(server_module, "_get_client", lambda: stub)

  with pytest.raises(TypeError):
    server_module.warm_nif_index(club_id=0)

  assert stub.calls == []
