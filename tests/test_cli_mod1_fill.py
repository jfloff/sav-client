import json

from click.testing import CliRunner

from sav_cli import cli as cli_module
from sav_client.models import Season


def test_mod1_fill_uses_current_sav_season(monkeypatch, tmp_path):
  captured = {}

  class StubClient:
    def get_current_season(self):
      return Season(id=65, label="2026/2027", start_year=2026, is_active=True)

  def fake_render(values, **kwargs):
    captured.update(values=values, kwargs=kwargs)
    return b"%PDF-test"

  values_path = tmp_path / "values.json"
  values_path.write_text(json.dumps({"nome": "Player"}), encoding="utf-8")
  out_path = tmp_path / "modelo1.pdf"
  monkeypatch.setattr(cli_module, "_make_client", lambda: StubClient())
  monkeypatch.setattr(cli_module, "render_mod1", fake_render)

  result = CliRunner().invoke(cli_module.cli, [
    "mod1", "fill", "--values", str(values_path), "--out", str(out_path),
  ])

  assert result.exit_code == 0
  assert captured["values"] == {"nome": "Player"}
  assert captured["kwargs"]["season"] == "2026/2027"
  assert out_path.read_bytes() == b"%PDF-test"


def test_mod1_fill_has_no_season_option(tmp_path):
  values_path = tmp_path / "values.json"
  values_path.write_text("{}", encoding="utf-8")

  result = CliRunner().invoke(cli_module.cli, [
    "mod1", "fill", "--values", str(values_path), "--out", str(tmp_path / "out.pdf"),
    "--season", "2025/2026",
  ])

  assert result.exit_code != 0
  assert "No such option '--season'" in result.output
