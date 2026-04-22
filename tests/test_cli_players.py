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
    ["--output", "json", "players", "--status", "active"],
  )

  assert result.exit_code == 0
  assert captured["status"] == "active"
  assert '"status": "FBP"' in result.output
