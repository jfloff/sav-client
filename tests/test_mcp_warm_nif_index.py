"""Offline MCP tests for warm_nif_index."""

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
