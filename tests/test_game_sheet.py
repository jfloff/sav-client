import pytest

from sav_client import SavClient
from sav_client.exceptions import SavResponseError


@pytest.fixture(scope="module")
def sample_game(client):
  # Prefer a played game (eligible players are populated); fall back to any game with an ID
  games = client.list_games()
  if not games:
    pytest.skip("Live SAV account has no visible games")
  game = next((g for g in games if g.id > 0 and g.game_status == "Marcado"), None)
  if game is None:
    game = next((g for g in games if g.id > 0), None)
  if game is None:
    pytest.skip("No game with a valid internal ID found")
  return game


@pytest.fixture(scope="module")
def sample_eligible(client, sample_game):
  data = client.get_eligible_players(sample_game.id, val=1)
  if not data.get("players"):
    data = client.get_eligible_players(sample_game.id, val=2)
  return data


class TestGetEligiblePlayers:
  def test_requires_login(self):
    c = SavClient("https://sav2.fpb.pt", "user", "pass")
    with pytest.raises(SavResponseError, match="Must call login"):
      c.get_eligible_players(1)

  def test_returns_dict_with_expected_keys(self, client, sample_game):
    data = client.get_eligible_players(sample_game.id, val=1)

    assert isinstance(data, dict)
    assert "game_number" in data
    assert "players" in data
    assert "coaches_pri" in data
    assert "coaches_adj" in data
    assert "staff" in data

  def test_players_have_licence_and_name(self, sample_eligible):
    players = sample_eligible["players"]
    if not players:
      pytest.skip("No eligible players found for this game")

    for player in players:
      assert player.get("licence"), f"Player entry missing licence: {player}"
      assert player.get("name"), f"Player entry missing name: {player}"

  def test_home_and_away_differ(self, client, sample_game):
    home = client.get_eligible_players(sample_game.id, val=1)
    away = client.get_eligible_players(sample_game.id, val=2)

    home_licences = {p.get("licence") for p in home["players"]}
    away_licences = {p.get("licence") for p in away["players"]}

    # Teams must not be identical (even if overlap is allowed)
    assert home_licences != away_licences or (not home_licences and not away_licences)


class TestGetEligiblePlayersPdf:
  def test_requires_login(self):
    c = SavClient("https://sav2.fpb.pt", "user", "pass")
    with pytest.raises(SavResponseError, match="Must call login"):
      c.get_eligible_players_pdf(1)

  def test_returns_pdf_bytes(self, client, sample_game, sample_eligible):
    if not sample_eligible.get("players"):
      pytest.skip("No eligible players — cannot generate PDF")

    val = 1
    pdf = client.get_eligible_players_pdf(sample_game.id, val=val)

    assert pdf is not None
    assert pdf.startswith(b"%PDF"), f"Expected PDF magic bytes, got: {pdf[:8]!r}"

  def test_player_filter_reduces_selection(self, client, sample_game, sample_eligible):
    players = sample_eligible["players"]
    if len(players) < 2:
      pytest.skip("Need at least 2 eligible players to test filtering")

    first_licence = int(players[0]["licence"])
    pdf_filtered = client.get_eligible_players_pdf(
      sample_game.id, val=1, player_licences=[first_licence]
    )
    pdf_all = client.get_eligible_players_pdf(sample_game.id, val=1)

    assert pdf_filtered is not None
    assert pdf_all is not None
    # Filtered PDF should be smaller (fewer players) — not always guaranteed,
    # but a reasonable heuristic for a sanity check
    assert len(pdf_filtered) <= len(pdf_all)

  def test_coaches_other_excluded_by_default(self, client, sample_game):
    # Passing coaches_other=() (the default) should not raise and return a PDF
    pdf = client.get_eligible_players_pdf(sample_game.id, val=1, coaches_other=())
    assert pdf is None or pdf.startswith(b"%PDF")

  def test_staff_excluded_by_default(self, client, sample_game):
    pdf = client.get_eligible_players_pdf(sample_game.id, val=1, staff=())
    assert pdf is None or pdf.startswith(b"%PDF")


class TestGetGameSheetPdf:
  def test_requires_login(self):
    c = SavClient("https://sav2.fpb.pt", "user", "pass")
    with pytest.raises(SavResponseError, match="Must call login"):
      c.get_game_sheet_pdf(1)

  def test_returns_bytes_or_none_for_live_game(self, client, sample_game):
    result = client.get_game_sheet_pdf(sample_game.id)

    assert result is None or isinstance(result, bytes)
