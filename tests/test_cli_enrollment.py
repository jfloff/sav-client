from click.testing import CliRunner
from sav_parsers.types import DocType

from sav_cli import cli as cli_module


def _write_pdf(tmp_path):
  pdf_path = tmp_path / "sample.pdf"
  pdf_path.write_bytes(b"%PDF-1.4\n")
  return pdf_path


def test_enrollment_update_rejects_legacy_tipo_alias(monkeypatch, tmp_path):
  pdf_path = _write_pdf(tmp_path)

  def fail_make_client():
    raise AssertionError("_make_client should not run for invalid --tipo values")

  monkeypatch.setattr(cli_module, "_make_client", fail_make_client)

  runner = CliRunner()
  result = runner.invoke(
    cli_module.cli,
    ["enrollment", "update", "12", "301772", str(pdf_path), "--file-only", "--tipo", "modelo1"],
  )

  assert result.exit_code != 0
  assert "Unknown doc_type 'modelo1'" in result.output


def test_enrollment_update_rejects_raw_tipo_integer(monkeypatch, tmp_path):
  pdf_path = _write_pdf(tmp_path)

  def fail_make_client():
    raise AssertionError("_make_client should not run for invalid --tipo values")

  monkeypatch.setattr(cli_module, "_make_client", fail_make_client)

  runner = CliRunner()
  result = runner.invoke(
    cli_module.cli,
    ["enrollment", "update", "12", "301772", str(pdf_path), "--file-only", "--tipo", "1"],
  )

  assert result.exit_code != 0
  assert "Unknown doc_type '1'" in result.output


def test_enrollment_update_maps_parser_tipo_names_for_file_replace(monkeypatch, tmp_path):
  pdf_path = _write_pdf(tmp_path)
  captured: list[int] = []

  class StubClient:
    def replace_player_registration_document(self, batch_id, license, pdf, *, tipo_doc):
      captured.append(tipo_doc)

  monkeypatch.setattr(cli_module, "_make_client", lambda: StubClient())

  runner = CliRunner()
  result = runner.invoke(
    cli_module.cli,
    [
      "enrollment", "update", "12", "301772", str(pdf_path), "--file-only",
      "--tipo", "exame_medico",
    ],
  )

  assert result.exit_code == 0
  assert captured == [2]
  assert "Replaced exame_medico" in result.output


def test_enrollment_update_classifies_exam_for_file_replace(monkeypatch, tmp_path):
  pdf_path = _write_pdf(tmp_path)
  captured: list[int] = []

  class StubClient:
    def replace_player_registration_document(self, batch_id, license, pdf, *, tipo_doc):
      captured.append(tipo_doc)

  monkeypatch.setattr(cli_module, "_make_client", lambda: StubClient())
  monkeypatch.setattr("sav_parsers.classify", lambda pdf: DocType.EM)

  runner = CliRunner()
  result = runner.invoke(
    cli_module.cli,
    ["enrollment", "update", "12", "301772", str(pdf_path), "--file-only"],
  )

  assert result.exit_code == 0
  assert captured == [2]
  assert "Replaced exame_medico" in result.output


def test_enrollment_update_rejects_unmapped_classified_doc_type(monkeypatch, tmp_path):
  pdf_path = _write_pdf(tmp_path)

  def fail_make_client():
    raise AssertionError("_make_client should not run for unmapped classified doc types")

  monkeypatch.setattr(cli_module, "_make_client", fail_make_client)
  monkeypatch.setattr("sav_parsers.classify", lambda pdf: DocType.OUTROS)

  runner = CliRunner()
  result = runner.invoke(
    cli_module.cli,
    ["enrollment", "update", "12", "301772", str(pdf_path), "--file-only"],
  )

  assert result.exit_code != 0
  assert "has no SAV2 tipo_doc mapping yet" in result.output


def test_enrollment_update_reconcile_accepts_only_fpb_modelo_1(monkeypatch, tmp_path):
  pdf_path = _write_pdf(tmp_path)

  def fail_make_client():
    raise AssertionError("_make_client should not run for unsupported reconcile doc types")

  monkeypatch.setattr(cli_module, "_make_client", fail_make_client)

  runner = CliRunner()
  result = runner.invoke(
    cli_module.cli,
    ["enrollment", "update", "12", "301772", str(pdf_path), "--tipo", "exame_medico"],
  )

  assert result.exit_code != 0
  assert "only fpb_modelo_1 forms are reconciled" in result.output
