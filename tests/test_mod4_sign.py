"""Tests for the Modelo 4 signature/stamp overlay (fpb_mod4) and its wiring into
the standalone-Subida upload path (`enrollment create --detentor-signature`).

Offline and deterministic: OCR (parse_fpb_mod4) and the SAV client are stubbed,
and PDFs are built in-test from a white image, so we assert on the overlay
plumbing (one Form XObject added per stamp) and the per-slot OverlayResult
rather than rendered pixels.
"""
import io

import img2pdf
import pikepdf
from click.testing import CliRunner
from PIL import Image

from sav_cli import cli as cli_module
from sav_parsers.types import BBox, DocType, ParsedField
from sav_shared.fpb_mod4 import (
  club_signature_overlay,
  detentor_signature_overlay,
  read_club_signature,
  read_detentor_signature,
)

# Wide, thin printed-label anchors (the shape parse_fpb_mod4 folds onto each
# `_presente` field).
_CLUB_BBOX = BBox(page=0, vertices=[(0.59, 0.30), (0.83, 0.30), (0.83, 0.32), (0.59, 0.32)])
_DET_BBOX = BBox(page=0, vertices=[(0.10, 0.62), (0.45, 0.62), (0.45, 0.64), (0.10, 0.64)])


def _png_bytes():
  buf = io.BytesIO()
  Image.new("RGBA", (120, 40), (0, 0, 180, 255)).save(buf, "PNG")
  return buf.getvalue()


def _blank_pdf():
  """A valid one-page PDF with no overlays yet."""
  buf = io.BytesIO()
  Image.new("RGB", (827, 1169), (255, 255, 255)).save(buf, "PNG")
  return img2pdf.convert(buf.getvalue())


def _xobject_count(pdf_bytes):
  with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
    return len(pdf.pages[0].get("/Resources", {}).get("/XObject", {}))


def _parsed(*, det=False, club=False, det_bbox=_DET_BBOX, club_bbox=_CLUB_BBOX):
  """A parse_fpb_mod4-shaped fields dict. det/club are the `_presente` values
  (False = empty, True = already signed, None = unresolved)."""
  return {
    "assinatura_detentor_presente": ParsedField(value=det, confidence=0.9, bbox=det_bbox),
    "club_signature_presente": ParsedField(value=club, confidence=0.9, bbox=club_bbox),
  }


class TestReadHelpers:
  def test_reads_present_and_bbox(self):
    parsed = _parsed(det=False, club=True)
    assert read_detentor_signature(parsed) == (False, _DET_BBOX)
    assert read_club_signature(parsed) == (True, _CLUB_BBOX)

  def test_missing_field_is_none(self):
    assert read_detentor_signature({}) == (None, None)
    assert read_club_signature({}) == (None, None)

  def test_unresolved_value_is_none_present(self):
    present, bbox = read_detentor_signature(_parsed(det=None))
    assert present is None
    assert bbox is _DET_BBOX  # bbox still surfaces even when presence is unknown


class TestOverlayFactories:
  """Each factory returns an overlaid_pdf-compatible callable
  (bytes -> (bytes, OverlayResult))."""

  def test_stamps_empty_slot(self):
    base = _blank_pdf()
    signed, r = detentor_signature_overlay(
      present=False, bbox=_DET_BBOX, image=_png_bytes())(base)
    assert r.applied is True and r.effective is True
    assert _xobject_count(signed) == _xobject_count(base) + 1

  def test_skips_already_signed_slot(self):
    base = _blank_pdf()
    signed, r = club_signature_overlay(
      present=True, bbox=_CLUB_BBOX, image=_png_bytes())(base)
    assert r.applied is None and r.effective is True
    assert signed == base

  def test_skips_when_no_image(self):
    base = _blank_pdf()
    signed, r = detentor_signature_overlay(
      present=False, bbox=_DET_BBOX, image=None)(base)
    assert r.applied is None and r.effective is False
    assert signed == base

  def test_skips_unresolved_slot(self):
    base = _blank_pdf()
    signed, r = detentor_signature_overlay(
      present=None, bbox=_DET_BBOX, image=_png_bytes())(base)
    assert r.applied is None and r.effective is None
    assert signed == base

  def test_missing_bbox_reports_failure_without_mutating(self):
    base = _blank_pdf()
    signed, r = detentor_signature_overlay(
      present=False, bbox=None, image=_png_bytes())(base)
    assert r.applied is False and r.effective is False
    assert "assinatura_detentor" in r.error
    assert signed == base


class TestCreateSubidaDetentorSignature:
  """`enrollment create <mod4> --detentor-signature` overlays the holder
  signature onto the mod4 before uploading it on the standalone-Subida path."""

  def _run(self, monkeypatch, tmp_path, parsed, extra_argv=()):
    subida_batch = type("BatchStub", (), {
      "id": 12, "number": "2025/12", "club_id": 99,
      "tier": "Sub 14", "gender": "Masculino", "type_id": 4,
    })()
    subida_path = tmp_path / "subida.pdf"
    subida_path.write_bytes(_blank_pdf())
    sig = tmp_path / "sig.png"
    sig.write_bytes(_png_bytes())

    captured: dict = {"uploads": []}

    class StubClient:
      session = {"organizacao": 42}

      def load_player_profile(self, license, club_id=None):
        return {"nome": "Player A"}

      def list_player_registration_tiers(self, gender_id):
        return {5: "Sub 14"}

      def search_players(self, *, license="", name="", club=None, **kw):
        if license == "301772":
          return [type("P", (), {
            "id": 1, "license": "301772", "name": "Player A",
            "gender": "Masculino", "birth_date": "2010-01-01",
          })()]
        return []

      def add_player_to_registration_batch(self, batch_id, license, **kwargs):
        return 301772

      def replace_player_registration_document(self, batch_id, license, pdf, *, tipo_doc):
        captured["uploads"].append((str(pdf), tipo_doc))

    monkeypatch.setattr(cli_module, "_make_client", lambda: StubClient())
    monkeypatch.setattr(
      cli_module, "_resolve_enroll_batch",
      lambda client, reg_type, tier_id, gender_id: (12, subida_batch),
    )
    monkeypatch.setattr("sav_parsers.classify", lambda pdf: DocType.FPB_MODELO_4)
    monkeypatch.setattr(
      "sav_parsers.parse_fpb_mod4",
      lambda pdf: {"fields": parsed, "processing_id": "proc-mod4"},
    )
    monkeypatch.setattr("sav_parsers.close_processing", lambda pid, corrections=None: None)
    monkeypatch.setattr("sav_parsers.train_classifier", lambda pdf, dt: None)
    # overlaid_pdf writes the stamped copy here; point it at the test's tmp dir.
    monkeypatch.setattr("sav_parsers.processing_dir", lambda pid: str(tmp_path))

    argv = ["enrollment", "create", str(subida_path),
            "--detentor-signature", str(sig), *extra_argv]
    result = CliRunner().invoke(cli_module.cli, argv, input="y\n")
    return result, captured

  def test_overlays_holder_signature_on_upload(self, monkeypatch, tmp_path):
    parsed = _parsed(det=False, club=None)  # empty holder slot, club unresolved
    parsed.update({
      "licenca_nr": ParsedField(value="301772", confidence=0.95),
      "nome_jogador": ParsedField(value="Player A", confidence=0.92),
      "escalao_subida": ParsedField(value="Sub 14", confidence=0.95),
    })
    result, captured = self._run(monkeypatch, tmp_path, parsed)

    assert result.exit_code == 0, result.output
    assert "Applied detentor signature" in result.output
    # The uploaded PDF is the stamped copy — one more XObject than the original.
    assert len(captured["uploads"]) == 1
    uploaded_path, tipo_doc = captured["uploads"][0]
    assert tipo_doc == 6
    with open(uploaded_path, "rb") as f:
      assert _xobject_count(f.read()) == _xobject_count(_blank_pdf()) + 1

  def test_already_signed_slot_uploads_original(self, monkeypatch, tmp_path):
    parsed = _parsed(det=True, club=None)  # holder already signed
    parsed.update({
      "licenca_nr": ParsedField(value="301772", confidence=0.95),
      "nome_jogador": ParsedField(value="Player A", confidence=0.92),
      "escalao_subida": ParsedField(value="Sub 14", confidence=0.95),
    })
    result, captured = self._run(monkeypatch, tmp_path, parsed)

    assert result.exit_code == 0, result.output
    assert "Applied detentor signature" not in result.output
    uploaded_path, _ = captured["uploads"][0]
    with open(uploaded_path, "rb") as f:
      assert _xobject_count(f.read()) == _xobject_count(_blank_pdf())

  def test_manual_mode_warns_signature_ignored(self, monkeypatch, tmp_path):
    # --batch + --license (manual mode) skips OCR, so the signature can't land.
    sig = tmp_path / "sig.png"
    sig.write_bytes(_png_bytes())
    mod4 = tmp_path / "subida.pdf"
    mod4.write_bytes(_blank_pdf())

    class StubClient:
      def resolve_registration_batch(self, *a, **k):
        raise AssertionError("should warn and proceed to manual mode")

    monkeypatch.setattr(cli_module, "_make_client", lambda: StubClient())
    # Manual mode routes to _run_manual_mode_enrollment; stub it out so the test
    # only checks the warning fired.
    monkeypatch.setattr(cli_module, "_run_manual_mode_enrollment", lambda *a, **k: None)

    result = CliRunner().invoke(cli_module.cli, [
      "enrollment", "create", "--mod4", str(mod4),
      "--batch", "2025/12", "--license", "301772",
      "--detentor-signature", str(sig),
    ])

    assert result.exit_code == 0, result.output
    assert "--detentor-signature is ignored in manual mode" in result.output


class TestUpdateMod4DetentorSignature:
  """`enrollment update <mod4> --detentor-signature` overlays the holder
  signature onto the replaced fpb_modelo_4 (single upload/replace path)."""

  def _run(self, monkeypatch, tmp_path, parsed):
    mod4 = tmp_path / "subida.pdf"
    mod4.write_bytes(_blank_pdf())
    sig = tmp_path / "sig.png"
    sig.write_bytes(_png_bytes())

    captured: dict = {"uploads": []}

    class StubClient:
      _cache = None

      def resolve_batch_id_by_license(self, license):
        return 12

      def replace_player_registration_document(self, batch_id, license, pdf, *, tipo_doc):
        captured["uploads"].append((str(pdf), tipo_doc))

    monkeypatch.setattr(cli_module, "_make_client", lambda: StubClient())
    monkeypatch.setattr(
      "sav_parsers.parse_fpb_mod4",
      lambda pdf: {"fields": parsed, "processing_id": "proc-mod4"},
    )
    monkeypatch.setattr("sav_parsers.close_processing", lambda pid, corrections=None: None)
    monkeypatch.setattr("sav_parsers.processing_dir", lambda pid: str(tmp_path))

    result = CliRunner().invoke(cli_module.cli, [
      "enrollment", "update", "--license", "301772", str(mod4),
      "--tipo", "fpb_modelo_4", "--detentor-signature", str(sig),
    ])
    return result, captured

  def test_overlays_holder_signature_on_replace(self, monkeypatch, tmp_path):
    result, captured = self._run(
      monkeypatch, tmp_path, _parsed(det=False, club=None))

    assert result.exit_code == 0, result.output
    assert "Applied detentor signature" in result.output
    assert len(captured["uploads"]) == 1
    uploaded_path, tipo_doc = captured["uploads"][0]
    assert tipo_doc == 6
    with open(uploaded_path, "rb") as f:
      assert _xobject_count(f.read()) == _xobject_count(_blank_pdf()) + 1

  def test_already_signed_uploads_original(self, monkeypatch, tmp_path):
    result, captured = self._run(
      monkeypatch, tmp_path, _parsed(det=True, club=None))

    assert result.exit_code == 0, result.output
    assert "Applied detentor signature" not in result.output
    uploaded_path, _ = captured["uploads"][0]
    with open(uploaded_path, "rb") as f:
      assert _xobject_count(f.read()) == _xobject_count(_blank_pdf())
