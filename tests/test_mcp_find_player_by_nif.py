"""MCP tests for find_player_by_nif — NIF → player resolution.

Focus: the `status` param. The default ("active") stays scoped to the
current season; "all" broadens to every season so a not-yet-renewed /
pending player still resolves (and keeps their tier) instead of None.
"""
from sav_client.models import Player
from sav_mcp import server as server_module


def _player(license: str, tier: str, active: bool = True) -> Player:
  return Player(
    id=int(license), license=license, name="Atleta Teste",
    association="AB Test", club="Test Club",
    tier=tier, gender="Masculino",
    birth_date="2014-06-08", nationality="Portuguesa", status="FBP",
    season="2025/2026", active=active,
  )


class _StubClient:
  """Captures search_players calls and resolves a single NIF → licence."""

  def __init__(self, *, club_id: int = 200, license: int | None = 301772,
               responses: dict[str, list[Player]] | None = None):
    self.session = {"epoca_id": 100, "organizacao": club_id}
    self._license = license
    self._responses = responses or {}
    self.calls: list[dict] = []

  def find_license_by_nif(self, nif, *, club_id=None):
    return self._license

  def search_players(self, **kwargs):
    self.calls.append(kwargs)
    return list(self._responses.get(kwargs.get("status"), []))


def test_active_default_scopes_to_current_season(monkeypatch):
  p = _player("301772", "Sub 14")
  stub = _StubClient(responses={"active": [p]})
  monkeypatch.setattr(server_module, "_get_client", lambda: stub)

  result = server_module.find_player_by_nif("123456789")

  assert result is not None
  assert result["license"] == "301772"
  assert result["tier"] == "Sub 14"
  call = stub.calls[0]
  assert call["status"] == "active"
  assert call["season"] is None  # current epoch


def test_all_searches_every_season_and_keeps_tier(monkeypatch):
  # Not-yet-renewed player: absent from the active current-season roster,
  # present (with their tier) when scanning all seasons.
  pending = _player("301772", "Sub 16", active=False)
  stub = _StubClient(responses={"active": [], "all": [pending]})
  monkeypatch.setattr(server_module, "_get_client", lambda: stub)

  assert server_module.find_player_by_nif("123456789") is None

  result = server_module.find_player_by_nif("123456789", status="all")
  assert result is not None
  assert result["tier"] == "Sub 16"
  assert stub.calls[-1]["status"] == "all"
  assert stub.calls[-1]["season"] == 0  # all seasons


def test_unresolved_nif_returns_none(monkeypatch):
  stub = _StubClient(license=None)
  monkeypatch.setattr(server_module, "_get_client", lambda: stub)

  assert server_module.find_player_by_nif("123456789", status="all") is None
  assert stub.calls == []  # never reaches search_players


def test_invalid_nif_length_returns_none(monkeypatch):
  stub = _StubClient()
  monkeypatch.setattr(server_module, "_get_client", lambda: stub)

  assert server_module.find_player_by_nif("123") is None
