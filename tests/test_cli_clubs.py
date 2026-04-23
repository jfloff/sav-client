from click.testing import CliRunner

from sav_cli import cli as cli_module
from sav_client.models import Club


def test_clubs_requires_explicit_scope(monkeypatch):
  def fail_make_client():
    raise AssertionError("_make_client should not run for scope validation errors")

  monkeypatch.setattr(cli_module, "_make_client", fail_make_client)

  runner = CliRunner()
  result = runner.invoke(
    cli_module.cli,
    ["--output", "json", "clubs"],
  )

  assert result.exit_code != 0
  assert "One of --association or --all-associations is required." in result.output


def test_clubs_forwards_all_associations(monkeypatch):
  captured = {}

  class StubClient:
    def list_clubs(self, **kwargs):
      captured.update(kwargs)
      return [Club(id=1, name="Club X", full_name="Club X", code="CX")]

  monkeypatch.setattr(cli_module, "_make_client", lambda: StubClient())

  runner = CliRunner()
  result = runner.invoke(
    cli_module.cli,
    ["--output", "json", "clubs", "--all-associations"],
  )

  assert result.exit_code == 0
  assert captured == {"association": None, "all_associations": True}
