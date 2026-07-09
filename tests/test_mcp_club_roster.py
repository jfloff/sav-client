"""MCP tests for club_roster — the single-shot current-season roster tool.

The tool's job: return one club's active, current-season roster in a single
call, with each row attributed to its source club (club_id/club_name) and
carrying birth_date/birth_year, so a projection relays rather than assembles.
"""
import pytest

from sav_client.models import Player, Season
from sav_mcp import server as server_module


def _player(license: str, name: str, birth_date: str, *, tier: str = "Sub 14",
            gender: str = "Masculino", tier_id: int = 5, gender_id: int = 1,
            club_id: int = 200) -> Player:
  # tier_id/gender_id are resolved during parsing; a real search row carries
  # them, so the stub sets them to relay-realistic values (Sub 14 / Masculino).
  return Player(
    id=int(license), license=license, name=name,
    association="AB Test", club="Test Club", club_id=club_id,
    tier=tier, gender=gender, tier_id=tier_id, gender_id=gender_id,
    birth_date=birth_date, nationality="Portuguesa", status="FBP",
    season="2025/2026", active=True,
  )


class _StubClient:
  def __init__(self, *, epoca_id: int = 100, season_year: int = 2025,
               club_id: int = 200, players: list[Player] | None = None,
               club_names: tuple[str, str] = ("Test Club Full", "TCF")):
    self.session = {"epoca_id": epoca_id, "organizacao": club_id}
    self._epoca_id = epoca_id
    self._season_year = season_year
    self._players = players or []
    self._club_names = club_names
    self.calls: list[dict] = []

  def get_current_season(self) -> Season:
    return Season(
      id=self._epoca_id,
      label=f"{self._season_year}/{self._season_year + 1}",
      start_year=self._season_year,
      is_active=True,
    )

  def search_players(self, **kwargs):
    self.calls.append(kwargs)
    return list(self._players)

  def _fetch_club_names(self, club_id: int) -> tuple[str, str]:
    return self._club_names


def _use(monkeypatch, stub: _StubClient) -> None:
  monkeypatch.setattr(server_module, "_get_client", lambda: stub)


class TestClubRoster:
  def test_defaults_to_session_club_and_current_season(self, monkeypatch):
    stub = _StubClient(players=[_player("301772", "Atleta 2014", "2014-06-08")])
    _use(monkeypatch, stub)

    result = server_module.club_roster()

    # Scoped to the session club, current epoch, active-only.
    [call] = stub.calls
    assert call["club"] == 200
    assert call["status"] == "active"
    assert call["season"] == 100

    assert result["club_id"] == 200
    assert result["club_name"] == "Test Club Full"
    assert result["season"] == "2025/2026"
    assert result["season_id"] == 100
    assert result["count"] == 1

  def test_row_carries_club_and_birth_year(self, monkeypatch):
    stub = _StubClient(players=[_player("301772", "Atleta 2014", "2014-06-08")])
    _use(monkeypatch, stub)

    [row] = server_module.club_roster()["players"]

    assert row["name"] == "Atleta 2014"
    assert row["birth_date"] == "2014-06-08"
    assert row["birth_year"] == 2014
    assert row["tier"] == "Sub 14"
    assert row["tier_id"] == 5
    assert row["gender_id"] == 1
    assert row["club_id"] == 200
    assert row["club_name"] == "Test Club Full"

  def test_explicit_club_id_is_scoped_and_reported(self, monkeypatch):
    stub = _StubClient(players=[_player("1", "X", "2013-02-02", club_id=555)])
    _use(monkeypatch, stub)

    result = server_module.club_roster(club_id=555)

    assert stub.calls[0]["club"] == 555
    assert result["club_id"] == 555
    assert result["players"][0]["club_id"] == 555

  def test_tier_and_gender_are_forwarded(self, monkeypatch):
    stub = _StubClient()
    _use(monkeypatch, stub)

    server_module.club_roster(tier="Sub 14", gender=1)

    call = stub.calls[0]
    assert call["tier"] == "Sub 14"
    assert call["gender"] == 1

  def test_past_season_query_has_no_current_label(self, monkeypatch):
    stub = _StubClient(players=[_player("1", "X", "2013-02-02")])
    _use(monkeypatch, stub)

    result = server_module.club_roster(season=99)

    assert stub.calls[0]["season"] == 99
    assert result["season_id"] == 99
    assert result["season"] == ""  # label only when the current epoch was queried

  def test_rejects_club_id_zero(self, monkeypatch):
    stub = _StubClient()
    _use(monkeypatch, stub)

    with pytest.raises(ValueError, match="single-club"):
      server_module.club_roster(club_id=0)

  def test_no_resolvable_club_raises(self, monkeypatch):
    stub = _StubClient(club_id=0)  # session has no organizacao
    _use(monkeypatch, stub)

    with pytest.raises(ValueError, match="single-club"):
      server_module.club_roster()

  def test_birth_year_none_when_birth_date_blank(self, monkeypatch):
    stub = _StubClient(players=[_player("1", "X", "")])
    _use(monkeypatch, stub)

    assert server_module.club_roster()["players"][0]["birth_year"] is None
