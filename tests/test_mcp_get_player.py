"""Offline MCP tests for get_player's season-resolution ladder."""

from sav_client.models import Player
from sav_mcp import server as server_module


def _player(season: str, *, tier: str = "Sub 14") -> Player:
  return Player(
    id=301772, license="301772", name="Atleta Teste",
    association="AB Test", club="Test Club", tier=tier,
    gender="Masculino", birth_date="2012-06-08",
    nationality="Portuguesa", status="FBP", season=season, active=True,
  )


class _StubClient:
  def __init__(self, responses=None, *, recent_seasons=None, recent_error=None):
    self.session = {"epoca_id": 100, "organizacao": 200}
    self.responses = responses or {}
    self.recent_seasons = recent_seasons or [100, 99]
    self.recent_error = recent_error
    self.calls: list[dict] = []
    self.recent_calls = 0

  def _recent_season_ids(self):
    self.recent_calls += 1
    if self.recent_error:
      raise self.recent_error
    return self.recent_seasons

  def search_players(self, **kwargs):
    self.calls.append(kwargs)
    return list(self.responses.get(kwargs["season"], []))


def test_current_player_stops_after_one_query(monkeypatch):
  stub = _StubClient({None: [_player("2025/2026")]})
  monkeypatch.setattr(server_module, "_get_client", lambda: stub)

  result = server_module.get_player(301772)

  assert result is not None
  assert result["season"] == "2025/2026"
  assert [call["season"] for call in stub.calls] == [None]
  assert stub.calls[0]["license"] == "301772"
  assert stub.recent_calls == 0


def test_previous_season_is_second_rung(monkeypatch):
  stub = _StubClient({99: [_player("2024/2025")]})
  monkeypatch.setattr(server_module, "_get_client", lambda: stub)

  result = server_module.get_player(301772)

  assert result is not None
  assert result["season"] == "2024/2025"
  assert [call["season"] for call in stub.calls] == [None, 99]


def test_all_seasons_is_third_rung_and_status_is_unchanged(monkeypatch):
  stub = _StubClient({0: [_player("2023/2024")]})
  monkeypatch.setattr(server_module, "_get_client", lambda: stub)

  result = server_module.get_player(301772, status="inactive")

  assert result is not None
  assert result["season"] == "2023/2024"
  assert [call["season"] for call in stub.calls] == [None, 99, 0]
  assert [call["status"] for call in stub.calls] == [
    "inactive", "inactive", "inactive",
  ]


def test_explicit_season_bypasses_ladder(monkeypatch):
  stub = _StubClient({42: [_player("2020/2021")]})
  monkeypatch.setattr(server_module, "_get_client", lambda: stub)

  result = server_module.get_player(301772, season=42, status="all")

  assert result is not None
  assert [call["season"] for call in stub.calls] == [42]
  assert stub.calls[0]["status"] == "all"
  assert stub.recent_calls == 0


def test_latest_season_row_is_selected_not_first_row(monkeypatch):
  stub = _StubClient({
    None: [
      _player("2023/2024", tier="Sub 12"),
      _player("2025/2026", tier="Sub 16"),
    ],
  })
  monkeypatch.setattr(server_module, "_get_client", lambda: stub)

  result = server_module.get_player(301772)

  assert result is not None
  assert result["season"] == "2025/2026"
  assert result["tier"] == "Sub 16"


def test_previous_season_lookup_failure_still_runs_all_seasons(monkeypatch):
  stub = _StubClient(
    {0: [_player("2022/2023")]}, recent_error=RuntimeError("offline"),
  )
  monkeypatch.setattr(server_module, "_get_client", lambda: stub)

  result = server_module.get_player(301772)

  assert result is not None
  assert [call["season"] for call in stub.calls] == [None, 0]


def test_get_player_profile_passes_integer_license_to_client(monkeypatch):
  calls = []

  class StubClient:
    def load_player_profile(self, license, *, club_id=None):
      calls.append((license, club_id))
      return {"nome": "Atleta Teste"}

  monkeypatch.setattr(server_module, "_get_client", lambda: StubClient())

  result = server_module.get_player_profile(301772, club_id=200)

  assert result == {"nome": "Atleta Teste"}
  assert calls == [(301772, 200)]


def test_get_player_schema_declares_integer_license():
  tool = server_module.server._tool_manager.get_tool("get_player")

  assert tool is not None
  assert tool.parameters["properties"]["license"]["type"] == "integer"
