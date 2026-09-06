"""Tests for shared Modelo 1 completion orchestration."""

from sav_parsers.types import ParsedField


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
