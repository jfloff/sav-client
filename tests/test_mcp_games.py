"""MCP tests for list_games — the date/status filtering and ordering.

SAV2 ignores the inicio/fim window server-side and leaks out-of-range games,
so list_games must guarantee the bounds with a client-side pass (filter_games)
and return results sorted chronologically. These tests stub the client to
return a deliberately out-of-window, out-of-order set.
"""
from sav_client.models import Game
from sav_mcp import server as server_module


def _game(number: str, date: str, status: str = "Marcado") -> Game:
  return Game(
    id=int(number), number=number, competition="Liga", phase="1ª Fase",
    round="1", date=date, time="10:00", home="A", away="B",
    home_score="", away_score="", venue="Pavilhão",
    game_status=status, result_status="Sem Resultado",
    tier="Sub 14", gender="Masculino", level="Sub 14 M",
  )


class _StubClient:
  """Returns a fixed game list regardless of the date window passed in —
  mirroring SAV2's empirically broken server-side filter."""

  def __init__(self, games: list[Game]):
    self._games = games
    self.calls: list[dict] = []

  def list_games(self, **kwargs):
    self.calls.append(kwargs)
    return list(self._games)


# Out of window (before 12-06-2026), out of chronological order on purpose.
_GAMES = [
  _game("3", "28-09-2025"),
  _game("1", "20-06-2026"),
  _game("4", "01-02-2026"),
  _game("2", "12-06-2026"),  # exactly on the boundary — inclusive
]


class TestDateFiltering:
  def test_date_from_drops_earlier_games(self, monkeypatch):
    stub = _StubClient(_GAMES)
    monkeypatch.setattr(server_module, "_get_client", lambda: stub)

    result = server_module.list_games(date_from="12-06-2026", status="Marcado")

    # Only on/after 2026-06-12 survive; boundary date is inclusive.
    assert [g["number"] for g in result] == ["2", "1"]

  def test_date_to_bounds_upper_end(self, monkeypatch):
    stub = _StubClient(_GAMES)
    monkeypatch.setattr(server_module, "_get_client", lambda: stub)

    result = server_module.list_games(date_to="12-06-2026")

    # On/before 2026-06-12, sorted earliest first.
    assert [g["number"] for g in result] == ["3", "4", "2"]

  def test_window_bounds_both_ends(self, monkeypatch):
    stub = _StubClient(_GAMES)
    monkeypatch.setattr(server_module, "_get_client", lambda: stub)

    result = server_module.list_games(
      date_from="01-01-2026", date_to="12-06-2026",
    )

    assert [g["number"] for g in result] == ["4", "2"]


class TestStatusFiltering:
  def test_status_filter_unchanged(self, monkeypatch):
    games = [
      _game("1", "20-06-2026", status="Marcado"),
      _game("2", "21-06-2026", status="Não Marcado"),
    ]
    stub = _StubClient(games)
    monkeypatch.setattr(server_module, "_get_client", lambda: stub)

    result = server_module.list_games(status="Marcado")

    assert [g["number"] for g in result] == ["1"]


class TestOrdering:
  def test_results_sorted_earliest_first(self, monkeypatch):
    stub = _StubClient(_GAMES)
    monkeypatch.setattr(server_module, "_get_client", lambda: stub)

    result = server_module.list_games()  # no filters

    assert [g["number"] for g in result] == ["3", "4", "2", "1"]

  def test_window_still_sent_to_sav(self, monkeypatch):
    """We still forward inicio/fim so SAV narrows the payload when it works."""
    stub = _StubClient(_GAMES)
    monkeypatch.setattr(server_module, "_get_client", lambda: stub)

    server_module.list_games(date_from="12-06-2026", date_to="30-06-2026")

    assert stub.calls[0]["date_from"] == "12-06-2026"
    assert stub.calls[0]["date_to"] == "30-06-2026"
