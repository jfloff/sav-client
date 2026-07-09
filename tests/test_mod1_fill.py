"""Tests for render_mod1 — filling the Modelo 1 AcroForm from field values.

Offline and deterministic: the bundled blank template is a fillable PDF form,
so we assert on the resulting field values (`/V`) rather than rendered pixels.
"""
import io

import pikepdf
import pytest
from pypdf import PdfReader

from sav_shared.fpb_mod1 import MOD1_FILL_MAPPING, render_mod1

SAMPLE = {
  "tipo_inscricao": 1,             # 1ª Inscrição
  "license": "301772",
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
  "telef": "218000000",
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


def _fields(pdf_bytes):
  return PdfReader(io.BytesIO(pdf_bytes)).get_fields()


def _v(fields, name):
  val = fields[name].get("/V")
  return "" if val is None else str(val)


def _widget_as(pdf_bytes, name):
  """The on-page widget /AS for checkbox `name` (follows /Kids), or None.

  Interactive viewers (Preview/Acrobat) render a checkbox off its widget /AS,
  not the field /V — so this is what actually shows as ticked."""
  with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
    for a in pdf.pages[0].get("/Annots", []):
      t = str(a.T) if "/T" in a else (str(a.Parent.T) if "/Parent" in a and "/T" in a.Parent else None)
      if t == name:
        v = a.get("/AS")
        return None if v is None else str(v)
  return None


def _has_appearance(pdf_bytes, name):
  """True when field `name` carries a normal appearance stream (/AP /N) —
  the thing that makes its value actually render/print (not just NeedAppearances)."""
  with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
    for f in pdf.Root.AcroForm.Fields:
      if "/T" in f and str(f.T) == name:
        ap = f.get("/AP")
        return ap is not None and "/N" in ap
  return False


@pytest.fixture(scope="module")
def pdf_bytes():
  return render_mod1(SAMPLE)


@pytest.fixture(scope="module")
def fields(pdf_bytes):
  return _fields(pdf_bytes)


class TestRenderMod1:
  def test_is_valid_single_page_pdf(self, pdf_bytes):
    assert pdf_bytes[:5] == b"%PDF-"
    assert len(PdfReader(io.BytesIO(pdf_bytes)).pages) == 1

  def test_text_fields(self, fields):
    assert _v(fields, "Nr Contribuinte") == "123456789"
    assert _v(fields, "nr_identificacao") == "12345678"
    assert _v(fields, "Email") == "rita@example.pt"
    assert _v(fields, "Telemóvel") == "912345678"
    assert _v(fields, "Telefone") == "218000000"
    assert _v(fields, "Morada") == "Rua das Flores, 12"
    assert _v(fields, "Localidade") == "Benfica"
    assert _v(fields, "Distrito") == "Lisboa"
    assert _v(fields, "Concelho") == "Lisboa"
    assert _v(fields, "nome_paternal") == "Maria Silva"
    assert _v(fields, "paternal_telefone") == "913333333"
    assert _v(fields, "email_paternal") == "maria@example.pt"

  def test_dates_are_split(self, fields):
    assert (_v(fields, "dn_dia"), _v(fields, "dn_mes"), _v(fields, "dn_ano")) == ("07", "03", "2010")
    assert (_v(fields, "val_dia"), _v(fields, "val_mes"), _v(fields, "val_ano")) == ("31", "12", "2030")

  def test_postal_is_split(self, fields):
    assert _v(fields, "codpostal") == "1500"
    assert _v(fields, "cp3") == "123"

  def test_id_type_checkbox(self, fields):
    assert _v(fields, "Cartão Cidadão") == "/On"
    assert _v(fields, "Passaporte") != "/On"
    assert _v(fields, "Outro") != "/On"

  def test_guardian_relation_checkbox(self, fields):
    assert _v(fields, "mae") == "/On"
    assert _v(fields, "pai") != "/On"
    assert _v(fields, "Tutor") != "/On"

  def test_consents(self, fields):
    assert _v(fields, "SIM") == "/On"     # consent_data True
    assert _v(fields, "NÃO") != "/On"
    assert _v(fields, "NÃO_2") == "/On"   # consent_communications False
    assert _v(fields, "SIM_2") != "/On"
    assert _v(fields, "SIM_3") == "/On"   # consent_marketing True

  def test_header_fields(self, fields):
    assert _v(fields, "primeira") == "/On"       # tipo_inscricao 1
    assert _v(fields, "revalidacao") != "/On"
    assert _v(fields, "nr_licenca") == "301772"
    assert _v(fields, "Clube") == "Sport Algés e Dafundo"
    assert _v(fields, "associacao") == "AB Lisboa"
    assert _v(fields, "Nome Completo") == "Rita Constança Silva"
    assert _v(fields, "Nacionalidade") == "Portuguesa"
    assert _v(fields, "País de Nascimento") == "Portugal"

  def test_gender_and_escalao_checkboxes(self, fields):
    assert _v(fields, "Feminino") == "/On"       # genero by name
    assert _v(fields, "Masculino") != "/On"
    assert _v(fields, "Sub14") == "/On"          # escalao by name
    assert _v(fields, "Sub16") != "/On"

  def test_guardian_id_document(self, fields):
    assert _v(fields, "titular do Cartão Cidadão") == "/On"  # guardian_id_type 1
    assert _v(fields, "passaporte_2") != "/On"
    assert _v(fields, "Outro_2") != "/On"
    assert _v(fields, "paternal_id") == "11223344"
    assert (_v(fields, "paternal_dia"), _v(fields, "paternal_mes"), _v(fields, "paternal_ano")) == ("20", "05", "2031")

  def test_signature_date_filled_but_line_has_no_field(self, fields):
    # The signature *date* is fillable; the signature lines / club stamp are
    # physical (no form fields), so they are inherently left blank.
    assert (_v(fields, "ass_dia"), _v(fields, "ass_mes"), _v(fields, "ass_ano")) == ("08", "07", "2026")

  def test_checkbox_widget_as_is_synced(self, pdf_bytes):
    # Regression: 1ª Inscrição originated as a /Kids field; interactive viewers
    # render the widget /AS, so ticking must sync it (not only the field /V).
    assert _widget_as(pdf_bytes, "primeira") == "/On"      # tipo_inscricao 1
    assert _widget_as(pdf_bytes, "revalidacao") in (None, "/Off")
    assert _widget_as(pdf_bytes, "Feminino") == "/On"      # flat checkbox too

  def test_text_fields_have_appearance_streams(self, pdf_bytes):
    # Regression guard: text must render without relying on NeedAppearances.
    assert _has_appearance(pdf_bytes, "Morada")
    assert _has_appearance(pdf_bytes, "Nr Contribuinte")

  def test_unmapped_fields_stay_empty(self, fields):
    # Insurance / estatuto / player-outro-description are intentionally not filled.
    for name in ("N Apólice", "Companhia", "outro_descricao"):
      assert _v(fields, name) == ""


def test_blank_and_unknown_values_are_skipped():
  f = _fields(render_mod1({"morada": "", "nif": None, "bogus_key": "x"}))
  assert _v(f, "Morada") == ""
  assert _v(f, "Nr Contribuinte") == ""


def test_eu_date_format_also_splits():
  f = _fields(render_mod1({"nasc": "07-03-2010"}))
  assert (_v(f, "dn_dia"), _v(f, "dn_mes"), _v(f, "dn_ano")) == ("07", "03", "2010")


def test_checkgroups_accept_int_or_name():
  # genero by int, escalao aliases resolve to the same boxes.
  f = _fields(render_mod1({"genero": 2}))
  assert _v(f, "Feminino") == "/On"
  f = _fields(render_mod1({"escalao": "Baby-Basket"}))
  assert _v(f, "BabyBasket") == "/On"
  f = _fields(render_mod1({"escalao": "Masters / Veteranos"}))
  assert _v(f, "Master") == "/On"


def test_empty_values_render_a_valid_blank_form():
  out = render_mod1({})
  assert out[:5] == b"%PDF-"
  assert len(PdfReader(io.BytesIO(out)).pages) == 1


def test_mapping_targets_exist_in_template():
  """Every PDF field name referenced by the mapping must exist on the template —
  guards against typos in the (accented) field names."""
  with pikepdf.open(render_mod1_template_path()) as pdf:
    names = {str(f.T) for f in pdf.Root.AcroForm.Fields if "/T" in f}
  targets: list[str] = []
  for spec in MOD1_FILL_MAPPING.values():
    for attr in ("field", "dia", "mes", "ano", "cod4", "cp3", "yes", "no"):
      val = getattr(spec, attr, None)
      if val:
        targets.append(val)
    for group_attr in ("by_int", "by_name"):
      group = getattr(spec, group_attr, None)
      if group:
        targets.extend(group.values())
  missing = sorted(t for t in targets if t not in names)
  assert not missing, f"mapping references PDF fields not in the template: {missing}"


def render_mod1_template_path():
  from sav_shared.fpb_mod1 import _MOD1_TEMPLATE
  return str(_MOD1_TEMPLATE)
