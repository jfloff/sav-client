"""Tests for reading a filled Modelo 1 AcroForm and skipping OCR.

Offline and deterministic: render a form with render_mod1 (which fills the
bundled fillable template), then read it back with read_mod1_acroform /
mod1_acroform_to_fields and assert the recovered fields match — the inbound
mirror of test_mod1_fill. No Document AI is involved.
"""
import io

import pikepdf
from pypdf import PdfWriter

from sav_shared.enrollment import derive_enrollment_params
from sav_shared.fpb_mod1 import (
  _MOD1_READ_CHECK,
  CLUB_STAMP_RECT,
  MOD1_FILL_MAPPING,
  is_filled_mod1_template,
  mod1_acroform_to_fields,
  mod1_values_to_fields,
  read_mod1_acroform,
  render_mod1,
)
from sav_shared.files import overlay_image_on_pdf

# A minor, 1ª Inscrição — so the guardian block and both checkbox/consent paths
# are exercised. Mirrors the SAMPLE in test_mod1_fill.
SAMPLE = {
  "tipo_inscricao": 1,             # 1ª Inscrição
  "clube": "Sport Algés e Dafundo",
  "associacao": "AB Lisboa",
  "genero": "Feminino",
  "escalao": "Sub 14",
  "nome": "Rita Constança Silva",
  "nacionalidade": "Portuguesa",
  "pais_nascimento": "Portugal",
  "nif": "123456789",
  "nasc": "2010-03-07",
  "tipo": 1,                       # Cartão Cidadão
  "numi": "12345678",
  "dataval": "2030-12-31",
  "email": "rita@example.pt",
  "tele": "912345678",
  "morada": "Rua das Flores, 12",
  "localidade_txt": "Benfica",
  "codpostal": "1500-123",
  "distrito": "Lisboa",
  "concelho": "Lisboa",
  "guardian_name": "Maria Silva",
  "guardian_relation": 2,          # mãe
  "guardian_id_type": 1,           # Cartão Cidadão
  "guardian_id_number": "11223344",
  "guardian_id_expiry": "2031-05-20",
  "guardian_phone": "913333333",
  "guardian_email": "maria@example.pt",
  "consent_data": True,
  "consent_communications": False,
  "consent_marketing": True,
  "data_assinatura": "2026-07-08",
}


def _filled_pdf(overrides=None):
  values = {**SAMPLE, **(overrides or {})}
  return render_mod1(values)


def _png_bytes():
  from PIL import Image
  buf = io.BytesIO()
  Image.new("RGBA", (60, 24), (0, 0, 180, 255)).save(buf, "PNG")
  return buf.getvalue()


def _fields(overrides=None):
  raw = read_mod1_acroform(_filled_pdf(overrides))
  assert raw is not None
  return mod1_acroform_to_fields(raw)


def _v(fields, entity):
  f = fields.get(entity)
  return None if f is None else f.value


class FakeClient:
  """Minimal stand-in for derive_enrollment_params."""
  def list_player_registration_tiers(self, gender_id):
    return {7: "Sub 14", 8: "Sub 16"}

  def find_license_by_nif(self, nif, *, refresh=False):
    return None


def test_detects_filled_template():
  raw = read_mod1_acroform(_filled_pdf())
  assert raw is not None
  assert is_filled_mod1_template(raw) is True


def test_values_path_matches_filled_template_fields():
  raw = read_mod1_acroform(render_mod1(SAMPLE))
  assert raw is not None
  assert mod1_values_to_fields(SAMPLE) == mod1_acroform_to_fields(raw)


def test_signed_render_is_detected_as_filled_template():
  png = _png_bytes()
  signed = render_mod1(
    SAMPLE,
    player_signature=png,
    guardian_signature=png,
    club_stamp=png,
  )
  raw = read_mod1_acroform(signed)
  assert raw is not None
  assert is_filled_mod1_template(raw) is True


def test_signed_render_fields_match_unsigned_render():
  png = _png_bytes()
  signed = render_mod1(
    SAMPLE,
    player_signature=png,
    guardian_signature=png,
    club_stamp=png,
  )
  raw = read_mod1_acroform(signed)
  assert raw is not None
  assert mod1_acroform_to_fields(raw) == _fields()


def test_signed_render_derived_params_match_unsigned_render():
  png = _png_bytes()
  signed = render_mod1(
    SAMPLE,
    player_signature=png,
    guardian_signature=png,
    club_stamp=png,
  )
  raw = read_mod1_acroform(signed)
  assert raw is not None
  signed_params = derive_enrollment_params(mod1_acroform_to_fields(raw), FakeClient())
  unsigned_params = derive_enrollment_params(_fields(), FakeClient())
  assert signed_params == unsigned_params


def test_externally_overlaid_render_is_detected_as_filled_template():
  overlaid = overlay_image_on_pdf(
    render_mod1(SAMPLE),
    _png_bytes(),
    rect=CLUB_STAMP_RECT,
  )
  raw = read_mod1_acroform(overlaid)
  assert raw is not None
  assert is_filled_mod1_template(raw) is True


def test_externally_overlaid_render_fields_match_unsigned_render():
  overlaid = overlay_image_on_pdf(
    render_mod1(SAMPLE),
    _png_bytes(),
    rect=CLUB_STAMP_RECT,
  )
  raw = read_mod1_acroform(overlaid)
  assert raw is not None
  assert mod1_acroform_to_fields(raw) == _fields()


def test_externally_overlaid_render_derived_params_match_unsigned_render():
  overlaid = overlay_image_on_pdf(
    render_mod1(SAMPLE),
    _png_bytes(),
    rect=CLUB_STAMP_RECT,
  )
  raw = read_mod1_acroform(overlaid)
  assert raw is not None
  overlaid_params = derive_enrollment_params(mod1_acroform_to_fields(raw), FakeClient())
  unsigned_params = derive_enrollment_params(_fields(), FakeClient())
  assert overlaid_params == unsigned_params


def test_non_form_pdf_is_not_a_template():
  writer = PdfWriter()
  writer.add_blank_page(width=595, height=842)
  buf = io.BytesIO()
  writer.write(buf)
  assert read_mod1_acroform(buf.getvalue()) is None


def test_foreign_acroform_is_not_a_template():
  # A PDF with an AcroForm but none of our Modelo 1 field names.
  writer = PdfWriter()
  writer.add_blank_page(width=595, height=842)
  buf = io.BytesIO()
  writer.write(buf)
  with pikepdf.open(io.BytesIO(buf.getvalue())) as pdf:
    pdf.Root.AcroForm = pdf.make_indirect(pikepdf.Dictionary(Fields=pikepdf.Array()))
    field = pdf.make_indirect(pikepdf.Dictionary(T="unrelated_field", V="x"))
    pdf.Root.AcroForm.Fields.append(field)
    out = io.BytesIO()
    pdf.save(out)
  raw = read_mod1_acroform(out.getvalue())
  assert raw is not None
  assert is_filled_mod1_template(raw) is False


def test_garbage_bytes_read_as_none():
  assert read_mod1_acroform(b"not a pdf at all") is None


def test_text_fields_round_trip():
  f = _fields()
  assert _v(f, "nome_completo") == "Rita Constança Silva"
  assert _v(f, "nif") == "123456789"
  assert _v(f, "num_doc_identificacao") == "12345678"
  assert _v(f, "email_jogador") == "rita@example.pt"
  assert _v(f, "telemovel") == "912345678"
  assert _v(f, "morada") == "Rua das Flores, 12"
  assert _v(f, "localidade") == "Benfica"
  assert _v(f, "distrito") == "Lisboa"
  assert _v(f, "concelho") == "Lisboa"
  assert _v(f, "clube") == "Sport Algés e Dafundo"
  assert _v(f, "associacao") == "AB Lisboa"
  assert _v(f, "nacionalidade") == "Portuguesa"
  assert _v(f, "pais_nascimento") == "Portugal"


def test_guardian_fields_round_trip():
  f = _fields()
  assert _v(f, "nome_encarregado") == "Maria Silva"
  assert _v(f, "num_doc_encarregado") == "11223344"
  assert _v(f, "telefone_encarregado") == "913333333"
  assert _v(f, "email_encarregado") == "maria@example.pt"
  assert _v(f, "validade_doc_encarregado") == "2031-05-20"
  assert _v(f, "parentesco_encarregado_mae") is True
  assert _v(f, "tipo_doc_encarregado_cc") is True


def test_dates_are_iso():
  f = _fields()
  assert _v(f, "data_nascimento") == "2010-03-07"
  assert _v(f, "validade_doc") == "2030-12-31"


def test_postal_is_formatted():
  assert _v(_fields(), "codigo_postal") == "1500-123"


def test_checkbox_groups():
  f = _fields()
  assert _v(f, "genero_feminino") is True
  assert _v(f, "genero_masculino") is None
  assert _v(f, "escalao_sub14") is True
  assert _v(f, "tipo_inscricao_primeira") is True
  assert _v(f, "tipo_inscricao_revalidacao") is None
  assert _v(f, "tipo_doc_cc") is True


def test_consents_are_booleans():
  f = _fields()
  assert _v(f, "consentimento_dados") is True
  assert _v(f, "consentimento_comunicacoes") is False
  assert _v(f, "consentimento_marketing") is True


def test_unfilled_field_reads_as_none():
  # licenca_fpb is empty on a 1ª Inscrição, so the entity is absent, not
  # (None, 0.0) — downstream reads it None-safely either way.
  f = _fields()
  assert _v(f, "licenca_fpb") is None
  assert "licenca_fpb" not in f


def test_populated_fields_are_full_confidence():
  f = _fields()
  assert f["nome_completo"].confidence == 1.0
  assert f["genero_feminino"].confidence == 1.0


def test_derive_enrollment_params_from_form():
  f = _fields()
  reg_type, tier_id, gender_id = derive_enrollment_params(f, FakeClient())
  assert reg_type == 1        # 1ª Inscrição checked
  assert gender_id == 2       # Feminino
  assert tier_id == 7         # matched "Sub 14"


def test_revalidacao_with_license():
  f = _fields({"tipo_inscricao": 2, "license": "301772"})
  assert _v(f, "tipo_inscricao_revalidacao") is True
  assert _v(f, "tipo_inscricao_primeira") is None
  assert _v(f, "licenca_fpb") == "301772"
  reg_type, _, _ = derive_enrollment_params(f, FakeClient())
  assert reg_type == 2


def test_read_check_boxes_are_resolvable_on_the_form():
  # Guard against the read-side checkbox map drifting from MOD1_FILL_MAPPING:
  # every box we read must be a box the fill side can tick.
  fill_boxes = set()
  for spec in MOD1_FILL_MAPPING.values():
    for attr in ("by_int", "by_name"):
      table = getattr(spec, attr, None)
      if table:
        fill_boxes.update(table.values())
    for attr in ("yes", "no"):
      box = getattr(spec, attr, None)
      if box:
        fill_boxes.add(box)
  assert set(_MOD1_READ_CHECK) <= fill_boxes
