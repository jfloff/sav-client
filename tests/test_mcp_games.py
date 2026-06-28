"""MCP tests for list_games — club-perspective rows, date/status filtering, order.

list_games returns each game from the session club's perspective (home /
our_score / opp_score / opponent are relative to the club). SAV2 ignores the
inicio/fim window server-side and leaks out-of-range games, so list_games must
guarantee the bounds with a client-side pass (filter_games) and return rows
sorted chronologically. These tests stub the client to return a deliberately
out-of-window, out-of-order set.
"""
from sav_client.models import Game
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
  def test_played_requires_both_scores(self, monkeypatch):
    games = [
      _game("1", "20-06-2026", home_score="80", away_score="70"),
      _game("2", "21-06-2026"),  # no scores yet
    ]
    _stub(monkeypatch, games)

    played = server_module.list_games(status="played")
    scheduled = server_module.list_games(status="scheduled")

    assert [g["source_id"] for g in played] == ["1"]
    assert [g["source_id"] for g in scheduled] == ["2"]

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
            home_score="80", away_score="70"),
    ])

    (row,) = server_module.list_games()

    assert row["home"] is True
    assert row["opponent"] == "Foes"
    assert row["our_score"] == 80
    assert row["opp_score"] == 70
    assert row["status"] == "played"
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

    assert row["status"] == "scheduled"
    assert row["our_score"] is None
    assert row["opp_score"] is None
    assert row["starts_at"] == ""


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
