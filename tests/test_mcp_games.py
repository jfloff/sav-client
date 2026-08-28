"""MCP tests for list_games — club-perspective rows, date/status filtering, order.

list_games returns each game from the session club's perspective (home /
our_score / opp_score / opponent are relative to the club). SAV2 ignores the
inicio/fim window server-side and leaks out-of-range games, so list_games must
guarantee the bounds with a client-side pass (filter_games) and return rows
sorted chronologically. These tests stub the client to return a deliberately
out-of-window, out-of-order set.
"""
import pytest

from sav_client.models import Game
from sav_shared.serializers import club_game_to_dict
from sav_mcp import server as server_module

CLUB = "Rio Maior Basket"


def _game(
  number: str,
  date: str,
  *,
  home: str = CLUB,
  away: str = "Opponent",
  home_score: str = "",
  away_score: str = "",
  status: str = "Marcado",
  time: str = "10:00",
) -> Game:
  return Game(
    id=int(number), number=number, competition="Liga", phase="1ª Fase",
    round="1", date=date, time=time, home=home, away=away,
    home_score=home_score, away_score=away_score, venue="Pavilhão",
    game_status=status, result_status="Sem Resultado",
    tier="Sub 14", gender="Masculino", level="Sub 14 M",
  )


class _StubClient:
  """Returns a fixed game list regardless of the date window passed in —
  mirroring SAV2's empirically broken server-side filter."""

  def __init__(self, games: list[Game]):
    self.session = {"organizacao": 2430, "epoca_id": 64}
    self._games = games
    self.calls: list[dict] = []

  def list_games(self, **kwargs):
    self.calls.append(kwargs)
    return list(self._games)

  def _fetch_club_names(self, club_id):
    return (CLUB, "RMB")


# Out of window (before 12-06-2026), out of chronological order on purpose.
_GAMES = [
  _game("3", "28-09-2025"),
  _game("1", "20-06-2026"),
  _game("4", "01-02-2026"),
  _game("2", "12-06-2026"),  # exactly on the boundary — inclusive
]


def _stub(monkeypatch, games):
  stub = _StubClient(games)
  monkeypatch.setattr(server_module, "_get_client", lambda: stub)
  return stub


class TestDateFiltering:
  def test_date_from_drops_earlier_games(self, monkeypatch):
    _stub(monkeypatch, _GAMES)

    result = server_module.list_games(date_from="12-06-2026")

    # Only on/after 2026-06-12 survive; boundary date is inclusive.
    assert [g["source_id"] for g in result] == ["2", "1"]

  def test_date_to_bounds_upper_end(self, monkeypatch):
    _stub(monkeypatch, _GAMES)

    result = server_module.list_games(date_to="12-06-2026")

    # On/before 2026-06-12, sorted earliest first.
    assert [g["source_id"] for g in result] == ["3", "4", "2"]

  def test_window_bounds_both_ends(self, monkeypatch):
    _stub(monkeypatch, _GAMES)

    result = server_module.list_games(date_from="01-01-2026", date_to="12-06-2026")

    assert [g["source_id"] for g in result] == ["4", "2"]


class TestStatusFiltering:
  def test_status_filters_sav_fixture_state(self, monkeypatch):
    games = [
      _game("1", "20-06-2026", status="Realizado",
            home_score="80", away_score="70"),
      _game("2", "21-06-2026", status="Marcado"),
      _game("3", "22-06-2026", status="Adiado"),
    ]
    _stub(monkeypatch, games)

    played = server_module.list_games(status="played")
    scheduled = server_module.list_games(status="scheduled")
    postponed = server_module.list_games(status="postponed")

    assert [g["source_id"] for g in played] == ["1"]
    assert [g["source_id"] for g in scheduled] == ["2"]
    assert [g["source_id"] for g in postponed] == ["3"]

  def test_all_keeps_every_game(self, monkeypatch):
    games = [
      _game("1", "20-06-2026", home_score="80", away_score="70"),
      _game("2", "21-06-2026"),
    ]
    _stub(monkeypatch, games)

    result = server_module.list_games(status="all")

    assert {g["source_id"] for g in result} == {"1", "2"}

  def test_rejects_unknown_status(self, monkeypatch):
    _stub(monkeypatch, _GAMES)

    import pytest
    with pytest.raises(ValueError, match="status must be"):
      server_module.list_games(status="Marcado")


class TestPerspective:
  def test_home_game(self, monkeypatch):
    _stub(monkeypatch, [
      _game("1", "20-06-2026", home=CLUB, away="Foes",
            home_score="80", away_score="70", status="Realizado"),
    ])

    (row,) = server_module.list_games()

    assert row["home"] is True
    assert row["opponent"] == "Foes"
    assert row["our_score"] == 80
    assert row["opp_score"] == 70
    assert row["status"] == "played"
    assert row["status_raw"] == "Realizado"
    assert row["has_result"] is True
    assert row["starts_at"] == "2026-06-20T10:00"
    assert row["escalao"] == "Sub 14 M"
    assert row["gender"] == "Masculino"
    assert row["venue"] == "Pavilhão"

  def test_away_game_matches_suffixed_team_name(self, monkeypatch):
    # SAV2 appends team suffixes; the club is still ours on the away side.
    _stub(monkeypatch, [
      _game("2", "21-06-2026", home="Foes", away=f"{CLUB} - B",
            home_score="60", away_score="65"),
    ])

    (row,) = server_module.list_games()

    assert row["home"] is False
    assert row["opponent"] == "Foes"
    assert row["our_score"] == 65
    assert row["opp_score"] == 60

  def test_unplayed_game_has_null_scores(self, monkeypatch):
    _stub(monkeypatch, [
      _game("3", "", home=CLUB, away="Foes", time="", status="Não Marcado"),
    ])

    (row,) = server_module.list_games()

    assert row["status"] == "not_scheduled"
    assert row["status_raw"] == "Não Marcado"
    assert row["has_result"] is False
    assert row["our_score"] is None
    assert row["opp_score"] is None
    assert row["starts_at"] == ""


class TestClubGameSerializer:
  def test_home_match_keeps_scores(self):
    game = _game("10", "20-06-2026", home=CLUB, away="Foes",
                 home_score="80", away_score="70")

    row = club_game_to_dict(game, club_name=CLUB)

    assert row["home"] is True
    assert row["our_score"] == 80
    assert row["opp_score"] == 70

  def test_away_match_swaps_scores(self):
    game = _game("11", "20-06-2026", home="Foes", away=CLUB,
                 home_score="60", away_score="65")

    row = club_game_to_dict(game, club_name=CLUB)

    assert row["home"] is False
    assert row["our_score"] == 65
    assert row["opp_score"] == 60

  def test_neither_match_raises_with_both_teams(self):
    game = _game("12", "20-06-2026", home="Home Lions", away="Away Tigers")

    with pytest.raises(ValueError, match="Home Lions.*Away Tigers"):
      club_game_to_dict(game, club_name="Unknown Club")

  @pytest.mark.parametrize("club_name", ["", "   "])
  def test_blank_club_name_raises(self, club_name):
    game = _game("13", "20-06-2026", home="Home Lions", away="Away Tigers")

    with pytest.raises(ValueError, match="Home Lions.*Away Tigers"):
      club_game_to_dict(game, club_name=club_name)

  def test_both_matches_raise_as_ambiguous(self):
    game = _game("14", "20-06-2026", home="Club United", away="Club United B")

    with pytest.raises(ValueError, match="matches both sides"):
      club_game_to_dict(game, club_name="Club United")

  def test_mcp_keeps_unmatchable_fixture_as_error_row(self, monkeypatch):
    good = _game("15", "20-06-2026", home=CLUB, away="Foes",
                 home_score="80", away_score="70")
    bad = _game("16", "21-06-2026", home="Home Lions", away="Away Tigers")
    _stub(monkeypatch, [good, bad])

    result = server_module.list_games()

    assert result[0]["source_id"] == "15"
    assert result[0]["home"] is True
    assert result[1]["source_id"] == "16"
    assert set(result[1]) == {"source_id", "error"}
    assert "Home Lions" in result[1]["error"]
    assert "Away Tigers" in result[1]["error"]

  def test_error_row_survives_status_filter(self, monkeypatch):
    good = _game("17", "20-06-2026", home=CLUB, away="Foes",
                 status="Realizado", home_score="80", away_score="70")
    bad = _game("18", "21-06-2026", home="Home Lions", away="Away Tigers")
    _stub(monkeypatch, [good, bad])

    result = server_module.list_games(status="cancelled")

    assert len(result) == 1
    assert result[0]["source_id"] == "18"
    assert set(result[0]) == {"source_id", "error"}


class TestCanonicalStatuses:
  def test_scoreless_cancelled_and_postponed_are_not_scheduled(self, monkeypatch):
    games = [
      _game("19", "20-06-2026", status="Anulado"),
      _game("20", "21-06-2026", status="Adiado"),
    ]
    _stub(monkeypatch, games)

    club_rows = server_module.list_games()
    sheet_rows = server_module.list_game_sheets()
    club_statuses = {row["source_id"]: row["status"] for row in club_rows}
    sheet_statuses = {str(row["id"]): row["status"] for row in sheet_rows}

    assert club_statuses == {"19": "cancelled", "20": "postponed"}
    assert sheet_statuses == club_statuses
    assert "scheduled" not in club_statuses.values()
    assert "scheduled" not in sheet_statuses.values()

  def test_both_tools_agree_that_a_scored_fixture_is_played(self, monkeypatch):
    game = _game("21", "20-06-2026", status="Realizado",
                 home_score="80", away_score="70")
    _stub(monkeypatch, [game])

    (club_row,) = server_module.list_games()
    (sheet_row,) = server_module.list_game_sheets()

    assert club_row["status"] == sheet_row["status"] == "played"
    assert club_row["has_result"] is sheet_row["has_result"] is True

  def test_unmappable_sav_status_is_explicit_and_filterable(self, monkeypatch):
    game = _game("22", "20-06-2026", status="Em Revisão")
    _stub(monkeypatch, [game])

    (club_row,) = server_module.list_games(status="unknown")
    (sheet_row,) = server_module.list_game_sheets(status="unknown")

    assert club_row["status"] == sheet_row["status"] == "unknown"
    assert club_row["status_raw"] == sheet_row["status_raw"] == "Em Revisão"
    assert club_row["has_result"] is sheet_row["has_result"] is False


class TestOrdering:
  def test_results_sorted_earliest_first(self, monkeypatch):
    _stub(monkeypatch, _GAMES)

    result = server_module.list_games()  # no filters

    assert [g["source_id"] for g in result] == ["3", "4", "2", "1"]

  def test_window_still_sent_to_sav(self, monkeypatch):
    """We still forward inicio/fim so SAV narrows the payload when it works."""
    stub = _stub(monkeypatch, _GAMES)

    server_module.list_games(date_from="12-06-2026", date_to="30-06-2026")

    assert stub.calls[0]["date_from"] == "12-06-2026"
    assert stub.calls[0]["date_to"] == "30-06-2026"


def test_canonical_status_is_case_and_whitespace_tolerant():
  """SAV renders these labels from HTML; its casing is not our contract."""
  from sav_shared.lookups import canonical_game_status
  for raw in ("Marcado", "  marcado ", "MARCADO"):
    assert canonical_game_status(raw) == "scheduled"
  assert canonical_game_status("nao marcado") == "unknown"  # accent still matters
  assert canonical_game_status("Não Marcado") == "not_scheduled"


def test_unmapped_status_never_masquerades_as_a_real_state():
  from sav_shared.lookups import canonical_game_status, GAME_STATUS_VALUES
  for raw in ("Novo Estado", "", None):
    assert canonical_game_status(raw) not in GAME_STATUS_VALUES
