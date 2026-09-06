"""CLI tests for `sav mod1 complete` — the completion overlays without the upload.

It shares `mod1_completion_path` with `sav enrollment create` and MCP's
`complete_mod1`, so these assert the CLI surface (options, reporting, exit
codes) rather than re-testing the overlay mechanics.
"""
import base64
import io

import pytest
from click.testing import CliRunner
from pypdf import PdfReader

from sav_cli import cli as cli_module
from sav_shared.fpb_mod1 import render_mod1

# Captured before the autouse fixture below stubs it out, so the two tests that
# exercise the pre-flight itself can put the real one back.
_REAL_PREFLIGHT = cli_module._require_google_credentials


@pytest.fixture(autouse=True)
def _no_credential_preflight(monkeypatch):
  """Skip the Document AI credential pre-flight by default.

  It performs a real ADC token refresh, so without this every scan-path test
  would pass or fail according to the developer's gcloud state rather than the
  code under test.
  """
  monkeypatch.setattr(cli_module, "_require_google_credentials", lambda: None)


def _stamp(tmp_path):
  from PIL import Image
  p = tmp_path / "stamp.png"
  Image.new("RGBA", (120, 40), (0, 0, 180, 255)).save(p, "PNG")
  return p


def _form(tmp_path, name="form.pdf", **values):
  from test_mod1_read import SAMPLE
  merged = {k: v for k, v in SAMPLE.items() if k not in ("data_assinatura", "license")}
  merged.update(values)
  p = tmp_path / name
  p.write_bytes(render_mod1(merged, season="2026/2027", validate=False))
  return p


def _fields(path):
  return PdfReader(io.BytesIO(path.read_bytes() if hasattr(path, "read_bytes")
                              else open(path, "rb").read())).get_fields()


def _run(monkeypatch, tmp_path, args):
  monkeypatch.setenv("CLUB_STAMP_PATH", str(_stamp(tmp_path)))
  monkeypatch.setattr(
    "sav_parsers.parse_fpb_mod1",
    lambda p: (_ for _ in ()).throw(AssertionError("a template form must not OCR")),
  )
  return CliRunner().invoke(cli_module.cli, args, catch_exceptions=False)


def test_completes_a_revalidacao_and_writes_the_pdf(monkeypatch, tmp_path):
  src = _form(tmp_path, tipo_inscricao=2)
  out = tmp_path / "out.pdf"

  result = _run(monkeypatch, tmp_path,
                ["mod1", "complete", str(src), "--out", str(out), "--license", "301772"])

  assert result.exit_code == 0, result.output
  assert "Saved completed Modelo 1" in result.output
  fields = _fields(out)
  assert str(fields["nr_licenca"].get("/V", "")) == "301772"
  # Stamping and dating are one action.
  assert all(str(fields[n].get("/V", "")) for n in ("ass_dia", "ass_mes", "ass_ano"))


def test_without_license_never_fills_the_licence(monkeypatch, tmp_path):
  """No --license means 1ª Inscrição, which has no licence at application time."""
  src = _form(tmp_path)
  out = tmp_path / "out.pdf"

  result = _run(monkeypatch, tmp_path,
                ["mod1", "complete", str(src), "--out", str(out)])

  assert result.exit_code == 0, result.output
  assert str(_fields(out)["nr_licenca"].get("/V", "")) == ""
  assert "Licença FPB" not in result.output


def test_an_already_complete_form_comes_back_untouched(monkeypatch, tmp_path):
  stamp = _stamp(tmp_path)
  from test_mod1_read import SAMPLE
  src = tmp_path / "done.pdf"
  src.write_bytes(render_mod1(SAMPLE, season="2026/2027",
                              club_stamp=stamp.read_bytes(), validate=False))
  out = tmp_path / "out.pdf"

  result = _run(monkeypatch, tmp_path,
                ["mod1", "complete", str(src), "--out", str(out)])

  assert result.exit_code == 0, result.output
  assert out.read_bytes() == src.read_bytes()
  assert "Applied club stamp" not in result.output


def test_requires_a_configured_club_stamp(monkeypatch, tmp_path):
  """The command exists to produce the club's attestation; without the asset it stops."""
  # The `cli` group callback runs load_dotenv(".env"), and this repo's own .env
  # sets CLUB_STAMP_PATH — so conftest's delenv is undone the moment the command
  # starts. Neutralise the load, or this asserts against the developer's machine.
  monkeypatch.setattr(cli_module, "load_dotenv", lambda *a, **kw: None)
  monkeypatch.delenv("CLUB_STAMP_PATH", raising=False)
  src = _form(tmp_path)

  result = CliRunner().invoke(
    cli_module.cli,
    ["mod1", "complete", str(src), "--out", str(tmp_path / "out.pdf")],
  )

  assert result.exit_code != 0
  assert "CLUB_STAMP_PATH" in result.output


def test_never_contacts_sav(monkeypatch, tmp_path):
  """Inspecting a local artifact must not reach the federation."""
  monkeypatch.setattr(
    cli_module, "_make_client",
    lambda: pytest.fail("mod1 complete must not talk to SAV"),
  )
  src = _form(tmp_path, tipo_inscricao=2)
  out = tmp_path / "out.pdf"

  result = _run(monkeypatch, tmp_path,
                ["mod1", "complete", str(src), "--out", str(out), "--license", "301772"])

  assert result.exit_code == 0, result.output


def test_a_scan_uses_the_ocr_slots(monkeypatch, tmp_path):
  """A member-supplied scan has no AcroForm, so both slots come from OCR."""
  import img2pdf
  from PIL import Image
  from sav_parsers.types import BBox, ParsedField

  monkeypatch.setenv("CLUB_STAMP_PATH", str(_stamp(tmp_path)))
  buf = io.BytesIO()
  Image.new("RGB", (827, 1169), (255, 255, 255)).save(buf, "PNG")
  src = tmp_path / "scan.pdf"
  src.write_bytes(img2pdf.convert(buf.getvalue()))
  out = tmp_path / "out.pdf"

  carimbo_bbox = BBox(page=0, vertices=[(0.60, 0.80), (0.65, 0.80), (0.65, 0.81), (0.60, 0.81)])
  monkeypatch.setattr(
    "sav_parsers.parse_fpb_mod1",
    lambda p: {
      "fields": {
        "carimbo_clube_presente": ParsedField(value=False, confidence=0.9, bbox=carimbo_bbox),
      },
      "processing_id": "cli-scan",
    },
  )
  monkeypatch.setattr("sav_parsers.close_processing", lambda pid, **kw: None)

  result = CliRunner().invoke(
    cli_module.cli,
    ["mod1", "complete", str(src), "--out", str(out)],
    catch_exceptions=False,
  )

  assert result.exit_code == 0, result.output
  assert "Applied club stamp" in result.output
  assert out.read_bytes() != src.read_bytes()


# ── Document AI credential pre-flight ─────────────────────────────────────────
# An expired ADC token used to hang the command for minutes with no output:
# `process_document` is called with no timeout, so the client retried silently.


def _break_credentials(monkeypatch):
  """Restore the real pre-flight, with ADC failing the way an expired token does."""
  from google.auth.exceptions import RefreshError
  monkeypatch.setattr(cli_module, "_require_google_credentials", _REAL_PREFLIGHT)
  monkeypatch.setattr(
    "google.auth.default",
    lambda **kw: (_ for _ in ()).throw(RefreshError("Reauthentication is needed.")),
  )


def test_a_scan_fails_fast_when_credentials_are_expired(monkeypatch, tmp_path):
  import img2pdf
  from PIL import Image
  monkeypatch.setenv("CLUB_STAMP_PATH", str(_stamp(tmp_path)))
  _break_credentials(monkeypatch)
  monkeypatch.setattr(
    "sav_parsers.parse_fpb_mod1",
    lambda p: pytest.fail("must not reach Document AI without usable credentials"),
  )
  buf = io.BytesIO()
  Image.new("RGB", (827, 1169), (255, 255, 255)).save(buf, "PNG")
  src = tmp_path / "scan.pdf"
  src.write_bytes(img2pdf.convert(buf.getvalue()))

  result = CliRunner().invoke(
    cli_module.cli, ["mod1", "complete", str(src), "--out", str(tmp_path / "o.pdf")],
  )

  assert result.exit_code != 0
  assert "gcloud auth application-default login" in result.output


def test_a_template_form_does_not_need_credentials_at_all(monkeypatch, tmp_path):
  """The AcroForm path never calls Document AI, so it must stay fully offline."""
  _break_credentials(monkeypatch)
  src = _form(tmp_path, tipo_inscricao=2)
  out = tmp_path / "out.pdf"

  result = _run(monkeypatch, tmp_path,
                ["mod1", "complete", str(src), "--out", str(out), "--license", "301772"])

  assert result.exit_code == 0, result.output
  assert str(_fields(out)["nr_licenca"].get("/V", "")) == "301772"


def test_a_failed_ocr_reports_the_cause_and_writes_nothing(monkeypatch, tmp_path):
  """The helper degrades to an empty result list; unpacking it blindly crashed.

  Completing the form is the command's whole promise, so a file that looks
  completed but isn't is worse than no file — the input is untouched anyway.
  """
  import img2pdf
  from PIL import Image

  monkeypatch.setenv("CLUB_STAMP_PATH", str(_stamp(tmp_path)))
  monkeypatch.setattr(
    "sav_parsers.parse_fpb_mod1",
    lambda p: (_ for _ in ()).throw(
      AttributeError("Unknown field for NormalizedValue: Clear")
    ),
  )
  buf = io.BytesIO()
  Image.new("RGB", (827, 1169), (255, 255, 255)).save(buf, "PNG")
  src = tmp_path / "scan.pdf"
  src.write_bytes(img2pdf.convert(buf.getvalue()))
  out = tmp_path / "out.pdf"

  result = CliRunner().invoke(
    cli_module.cli, ["mod1", "complete", str(src), "--out", str(out)],
  )

  assert result.exit_code != 0
  assert "Could not read the form to complete it" in result.output
  assert "NormalizedValue" in result.output
  assert not out.exists(), "no half-completed artifact should be left behind"
