"""Tests for shared Modelo 1 completion orchestration."""

import io

import img2pdf
from PIL import Image
from sav_parsers.types import BBox, ParsedField

# Values-derived fields describe a Revalidação whose licence number the caller
# knows. They are built through mod1_values_to_fields in every test below so the
# fixture keeps the real entity shape — notably that it carries `licenca_fpb`
# (the text) and never `licenca_fpb_presente` (the slot's presence).
_REVALIDACAO_VALUES = {"tipo_inscricao": 2, "license": "301772"}

_LICENSE_BBOX = BBox(
  page=0,
  vertices=[(0.30, 0.20), (0.45, 0.20), (0.45, 0.23), (0.30, 0.23)],
)
_CARIMBO_BBOX = BBox(
  page=0,
  vertices=[(0.60, 0.80), (0.65, 0.80), (0.65, 0.81), (0.60, 0.81)],
)
_INSCRICAO_BBOX = BBox(
  page=0,
  vertices=[(0.10, 0.20), (0.13, 0.20), (0.13, 0.22), (0.10, 0.22)],
)


def _scan_pdf(tmp_path):
  """Write a blank one-page PDF and return its path.

  Deliberately *not* render_mod1: an AcroForm sends licenca_overlay down its
  template branch, and these tests exercise the OCR/bbox branch a member's scan
  takes.
  """
  image = io.BytesIO()
  Image.new("RGB", (827, 1169), (255, 255, 255)).save(image, "PNG")
  pdf_path = tmp_path / "scan.pdf"
  pdf_path.write_bytes(img2pdf.convert(image.getvalue()))
  return pdf_path


def _scan_ocr_fields(*, licenca_present=False, inscricao_present=None):
  """Stub parse_fpb_mod1 fields for a scan, with the bboxes overlays need."""
  fields = {
    "carimbo_clube_presente": ParsedField(
      value=True, confidence=1.0, bbox=_CARIMBO_BBOX,
    ),
    "licenca_fpb_presente": ParsedField(
      value=licenca_present, confidence=1.0, bbox=_LICENSE_BBOX,
    ),
  }
  if inscricao_present is not None:
    fields["tipo_inscricao_revalidacao"] = ParsedField(
      value=inscricao_present, confidence=1.0, bbox=_INSCRICAO_BBOX,
    )
  return fields


def _stub_parser(monkeypatch, fields, *, calls=None, closed=None):
  """Point sav_parsers at `fields`, recording the calls and session closes."""
  def fake_parse(path):
    if calls is not None:
      calls.append(path)
    return {"fields": fields, "processing_id": "scan-session"}

  monkeypatch.setattr("sav_parsers.parse_fpb_mod1", fake_parse)
  monkeypatch.setattr(
    "sav_parsers.close_processing",
    lambda processing_id, **_kwargs: (
      closed.append(processing_id) if closed is not None else None
    ),
  )


def test_completion_without_ocr_reuses_parsed_fields_and_caller_session(
  monkeypatch, tmp_path,
):
  from sav_shared.fpb_mod1 import render_mod1
  from sav_shared.mod1_completion import mod1_completion_path

  pdf_path = tmp_path / "form.pdf"
  pdf_path.write_bytes(render_mod1({}, season="2026/2027", validate=False))
  closed: list[str] = []
  monkeypatch.setattr(
    "sav_parsers.parse_fpb_mod1",
    lambda _path: (_ for _ in ()).throw(AssertionError("completion must not OCR")),
  )
  monkeypatch.setattr(
    "sav_parsers.close_processing",
    lambda processing_id, **_kwargs: closed.append(processing_id),
  )
  parsed = {
    "tipo_inscricao_primeira": ParsedField(value=True, confidence=1.0),
    "carimbo_clube_presente": ParsedField(value=True, confidence=1.0),
  }

  with mod1_completion_path(
    str(pdf_path),
    parsed=parsed,
    processing_id="caller-owned",
    reg_type=1,
    allow_ocr_fallback=False,
  ) as (upload_path, _status, results):
    assert upload_path == str(pdf_path)
    assert len(results) == 3

  assert closed == []


def test_completion_falls_back_to_scan_licence_slot_for_values_fields(
  monkeypatch, tmp_path,
):
  """OCR must supply a scan's licence slot or Revalidação silently stays unknown.

  Values-derived fields answer the inscription question but carry no
  licenca_fpb_presente. Preferring them wholesale — as the old single
  `tipo_fields` source did — filed the form with the licence box blank and
  reported has_license: None, which reads as "not inspected", not "not filled".
  """
  from sav_shared.fpb_mod1 import mod1_values_to_fields
  from sav_shared.mod1_completion import mod1_completion_path

  pdf_path = _scan_pdf(tmp_path)
  parse_calls: list[str] = []
  closed: list[str] = []
  _stub_parser(
    monkeypatch, _scan_ocr_fields(), calls=parse_calls, closed=closed,
  )
  parsed = mod1_values_to_fields(_REVALIDACAO_VALUES, validate=False)

  with mod1_completion_path(
    str(pdf_path), parsed=parsed, reg_type=2, license=301772,
  ) as (_upload_path, status, results):
    assert status["has_license"] is True
    assert status["license_warning"] is None
    assert results[1].applied is True

  assert parse_calls == [str(pdf_path)]
  assert closed == ["scan-session"]


def test_completion_falls_back_to_scan_inscricao_slot_for_values_fields(
  monkeypatch, tmp_path,
):
  """The inscription mark needs the same per-question fallback as the licence.

  tipo_inscricao_* exists in values-derived fields only when the caller ticked
  that box, so a caller that omits it leaves the slot unknown and the mark was
  skipped even though OCR had located a blank checkbox.
  """
  from sav_shared.fpb_mod1 import mod1_values_to_fields
  from sav_shared.mod1_completion import mod1_completion_path

  pdf_path = _scan_pdf(tmp_path)
  closed: list[str] = []
  _stub_parser(
    monkeypatch,
    _scan_ocr_fields(inscricao_present=False),
    closed=closed,
  )
  # No tipo_inscricao: the caller knows the licence but not what the form marks.
  parsed = mod1_values_to_fields({"license": "301772"}, validate=False)

  with mod1_completion_path(
    str(pdf_path), parsed=parsed, reg_type=2, license=301772,
  ) as (_upload_path, status, results):
    assert status["has_inscricao_mark"] is True
    assert status["inscricao_warning"] is None
    assert results[0].applied is True

  assert closed == ["scan-session"]


def test_completion_without_ocr_fallback_does_not_fill_unknown_scan_licence(
  monkeypatch, tmp_path,
):
  """Disabling OCR must leave an absent licence slot unknown instead of starting OCR.

  In this mode the caller declared its own fields authoritative and forbade a
  second Document AI round-trip; an unanswered slot has nowhere else to go.
  """
  from sav_shared.fpb_mod1 import mod1_values_to_fields
  from sav_shared.mod1_completion import mod1_completion_path

  pdf_path = _scan_pdf(tmp_path)
  monkeypatch.setattr(
    "sav_parsers.parse_fpb_mod1",
    lambda _path: (_ for _ in ()).throw(
      AssertionError("completion must not OCR when fallback is disabled"),
    ),
  )
  closed: list[str] = []
  monkeypatch.setattr(
    "sav_parsers.close_processing",
    lambda processing_id, **_kwargs: closed.append(processing_id),
  )
  parsed = mod1_values_to_fields(_REVALIDACAO_VALUES, validate=False)

  with mod1_completion_path(
    str(pdf_path),
    parsed=parsed,
    reg_type=2,
    license=301772,
    allow_ocr_fallback=False,
  ) as (_upload_path, status, _results):
    assert status["has_license"] is None

  assert closed == []


def test_completion_does_not_overwrite_existing_scan_licence(
  monkeypatch, tmp_path,
):
  """An OCR-present licence must remain untouched rather than being written again.

  The fallback supplies a location and a presence answer, never permission to
  overwrite a number the form already carries.
  """
  from sav_shared.fpb_mod1 import mod1_values_to_fields
  from sav_shared.mod1_completion import mod1_completion_path

  pdf_path = _scan_pdf(tmp_path)
  closed: list[str] = []
  _stub_parser(
    monkeypatch, _scan_ocr_fields(licenca_present=True), closed=closed,
  )
  parsed = mod1_values_to_fields(_REVALIDACAO_VALUES, validate=False)

  with mod1_completion_path(
    str(pdf_path), parsed=parsed, reg_type=2, license=301772,
  ) as (_upload_path, status, results):
    assert status["has_license"] is True
    assert results[1].applied is not True

  assert closed == ["scan-session"]
