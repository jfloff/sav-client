from click.testing import CliRunner

from sav_cli import cli as cli_module
from sav_client.models import Player


def test_players_status_option_is_forwarded_to_client(monkeypatch):
  captured = {}

  class StubClient:
    def search_players(self, **kwargs):
      captured.update(kwargs)
      return [
        Player(
          id=1,
          license="100",
          name="A",
          association="AB X",
          club="Club X",
          tier="Mini 12",
          gender="Masculino",
          birth_date="2015-01-01",
          nationality="Portuguesa",
          status="FBP",
          season="2025/2026",
          active=True,
        )
      ]

  monkeypatch.setattr(cli_module, "_make_client", lambda: StubClient())

  runner = CliRunner()
  result = runner.invoke(
    cli_module.cli,
    ["--output", "json", "players", "--status", "active", "--club", "123"],
  )

  assert result.exit_code == 0
  assert captured["status"] == "active"
  assert '"status": "FBP"' in result.output


def test_players_without_association_option_does_not_forward_association_filter(monkeypatch):
  captured = {}

  class StubClient:
    def search_players(self, **kwargs):
      captured.update(kwargs)
      return [
        Player(
          id=1,
          license="100",
          name="A",
          association="AB X",
          club="Club X",
          tier="Mini 12",
          gender="Masculino",
          birth_date="2015-01-01",
          nationality="Portuguesa",
          status="FBP",
          season="2025/2026",
          active=True,
        )
      ]

  monkeypatch.setattr(cli_module, "_make_client", lambda: StubClient())

  runner = CliRunner()
  result = runner.invoke(
    cli_module.cli,
    ["--output", "json", "players", "--club", "123"],
  )

  assert result.exit_code == 0
  assert captured["association"] is None


def test_players_association_zero_is_rejected(monkeypatch):
  class StubClient:
    pass

  monkeypatch.setattr(cli_module, "_make_client", lambda: StubClient())

  runner = CliRunner()
  result = runner.invoke(
    cli_module.cli,
    ["--output", "json", "players", "--association", "0"],
  )

  assert result.exit_code != 0
  assert "--association no longer accepts 0" in result.output


def test_players_requires_explicit_scope(monkeypatch):
  def fail_make_client():
    raise AssertionError("_make_client should not run for scope validation errors")

  monkeypatch.setattr(cli_module, "_make_client", fail_make_client)

  runner = CliRunner()
  result = runner.invoke(
    cli_module.cli,
    ["--output", "json", "players", "--status", "active"],
  )

  assert result.exit_code != 0
  assert "One of --club, --association, or --all-clubs is required." in result.output


def test_player_requires_explicit_scope(monkeypatch):
  def fail_make_client():
    raise AssertionError("_make_client should not run for scope validation errors")

  monkeypatch.setattr(cli_module, "_make_client", fail_make_client)

  runner = CliRunner()
  result = runner.invoke(
    cli_module.cli,
    ["--output", "json", "player", "301772"],
  )

  assert result.exit_code != 0
  assert "One of --club, --association, or --all-clubs is required." in result.output


def test_player_forwards_all_clubs_scope(monkeypatch):
  captured = []

  class StubClient:
    def search_players(self, **kwargs):
      captured.append(kwargs)
      return [
        Player(
          id=1,
          license="301772",
          name="A",
          association="AB X",
          club="Club X",
          tier="Mini 12",
          gender="Masculino",
          birth_date="2015-01-01",
          nationality="Portuguesa",
          status="FBP",
          season="2025/2026",
          active=True,
        )
      ]

  monkeypatch.setattr(cli_module, "_make_client", lambda: StubClient())

  runner = CliRunner()
  result = runner.invoke(
    cli_module.cli,
    ["--output", "json", "player", "301772", "--all-clubs"],
  )

  assert result.exit_code == 0
  assert captured[0]["club"] == 0
  assert captured[0]["association"] is None
