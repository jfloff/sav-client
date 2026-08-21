"""Offline MCP tests for NIF → player resolution across season rungs."""

from sav_client.models import Player
from sav_mcp import server as server_module


def _player(
  license: str,
  tier: str,
  *,
  season: str = "2025/2026",
  active: bool = True,
) -> Player:
  return Player(
    id=int(license), license=license, name="Atleta Teste",
    association="AB Test", club="Test Club", tier=tier,
    gender="Masculino", birth_date="2014-06-08",
    nationality="Portuguesa", status="FBP", season=season, active=active,
  )


class _StubClient:
  """Capture searches and resolve a single NIF to a licence."""

  def __init__(
    self,
    *,
    club_id: int = 200,
    license: int | None = 301772,
    responses: dict[int | None, list[Player]] | None = None,
  ):
    self.session = {"epoca_id": 100, "organizacao": club_id}
    self._license = license
    self._responses = responses or {}
    self.calls: list[dict] = []

  def _recent_season_ids(self):
    return [100, 99]

  def find_license_by_nif(self, nif, *, club_id=None):
    return self._license

  def search_players(self, **kwargs):
    self.calls.append(kwargs)
    return list(self._responses.get(kwargs.get("season"), []))


def test_active_default_checks_current_season_first(monkeypatch):
  current = _player("301772", "Sub 14")
  stub = _StubClient(responses={None: [current]})
  monkeypatch.setattr(server_module, "_get_client", lambda: stub)

  result = server_module.find_player_by_nif("123456789")

  assert result is not None
  assert result["license"] == "301772"
  assert result["tier"] == "Sub 14"
  assert [call["season"] for call in stub.calls] == [None]
  assert stub.calls[0]["status"] == "active"


def test_status_all_uses_ladder_instead_of_forcing_all_seasons(monkeypatch):
  pending = _player(
    "301772", "Sub 16", season="2024/2025", active=False,
  )
  stub = _StubClient(responses={99: [pending]})
  monkeypatch.setattr(server_module, "_get_client", lambda: stub)

  result = server_module.find_player_by_nif("123456789", status="all")

  assert result is not None
  assert result["tier"] == "Sub 16"
  assert [call["season"] for call in stub.calls] == [None, 99]
  assert [call["status"] for call in stub.calls] == ["all", "all"]


def test_lapsed_active_player_resolves_on_all_seasons_rung(monkeypatch):
  lapsed = _player("301772", "Sub 18", season="2022/2023")
  stub = _StubClient(responses={0: [lapsed]})
  monkeypatch.setattr(server_module, "_get_client", lambda: stub)

  result = server_module.find_player_by_nif("123456789", status="active")

  assert result is not None
  assert result["season"] == "2022/2023"
  assert result["tier"] == "Sub 18"
  assert [call["season"] for call in stub.calls] == [None, 99, 0]
  assert all(call["status"] == "active" for call in stub.calls)


def test_unresolved_nif_returns_none(monkeypatch):
  stub = _StubClient(license=None)
  monkeypatch.setattr(server_module, "_get_client", lambda: stub)

  assert server_module.find_player_by_nif("123456789", status="all") is None
  assert stub.calls == []


def test_invalid_nif_length_returns_none(monkeypatch):
  stub = _StubClient()
  monkeypatch.setattr(server_module, "_get_client", lambda: stub)

  assert server_module.find_player_by_nif("123") is None
  assert stub.calls == []
