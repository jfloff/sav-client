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


def test_clubs_query_matches_acronym_style_name(monkeypatch):
  class StubClient:
    def list_clubs(self, **kwargs):
      return [
        Club(id=1, name="Santarém Basket Clube", full_name="Santarém Basket Clube", code="SBC")
      ]

  monkeypatch.setattr(cli_module, "_make_client", lambda: StubClient())

  runner = CliRunner()
  result = runner.invoke(
    cli_module.cli,
    ["--output", "json", "clubs", "--all-associations", "Santarém BC"],
  )

  assert result.exit_code == 0
  assert '"Santarém Basket Clube"' in result.output


def test_clubs_query_uses_fuzzy_fallback(monkeypatch):
  class StubClient:
    def list_clubs(self, **kwargs):
      return [
        Club(id=1, name="Santarém Basket Clube", full_name="Santarém Basket Clube", code="SBC"),
        Club(id=2, name="Outro Clube", full_name="Outro Clube", code="OC"),
      ]

  def fake_score(query, candidates):
    return 90.0 if "santarem basket clube" in candidates else 10.0

  monkeypatch.setattr(cli_module, "_make_client", lambda: StubClient())
  monkeypatch.setattr(cli_module, "_rapidfuzz_best_score", fake_score)

  runner = CliRunner()
  result = runner.invoke(
    cli_module.cli,
    ["--output", "json", "clubs", "--all-associations", "Santaram Bsket Clbe"],
  )

  assert result.exit_code == 0
  assert '"Santarém Basket Clube"' in result.output
