"""Tests for render_mod1 — filling the Modelo 1 AcroForm from field values.

Offline and deterministic: the bundled blank template is a fillable PDF form,
so we assert on the resulting field values (`/V`) rather than rendered pixels.
"""
import io
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import pikepdf
import pytest
from pypdf import PdfReader, PdfWriter

from sav_shared.fpb_mod1 import (
  MOD1_FILL_MAPPING,
  CLUB_STAMP_RECT,
  _MOD1_GUARDIAN_KEYS,
  carimbo_overlay,
  fill_signature_date,
  render_mod1 as _render_mod1,
  validate_mod1_values,
)
from sav_shared.files import rect_has_overlay

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

SEASON = "2026/2027"


def render_mod1(values, **kwargs):
  """Render with a fixed explicit season unless a test targets that contract."""
  return _render_mod1(values, season=SEASON, **kwargs)


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


def _page_xobject_count(pdf_bytes):
  """Number of XObjects referenced by page 0. overlay_image_on_pdf adds one
  Form XObject per signature/stamp, so this grows by exactly one per overlay."""
  with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
    return len(pdf.pages[0].get("/Resources", {}).get("/XObject", {}))


def _png_bytes(size=(60, 24)):
  from PIL import Image
  buf = io.BytesIO()
  Image.new("RGBA", size, (0, 0, 180, 255)).save(buf, "PNG")
  return buf.getvalue()


def _set_field(pdf_bytes, name, value):
  """Write one raw AcroForm value, bypassing render_mod1 — for the half-filled
  date a human leaves behind."""
  with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
    for f in pdf.Root.AcroForm.Fields:
      if "/T" in f and str(f.T) == name:
        f.V = pikepdf.String(value)
    out = io.BytesIO()
    pdf.save(out)
  return out.getvalue()


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

  def test_sav_season_years(self, fields):
    # The template's names are reversed: epoca2 is visually left/start.
    assert _v(fields, "epoca2") == "2026"
    assert _v(fields, "epoca1") == "2027"

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
    # Insurance / estatuto / player-outro-description, plus the player Telefone
    # (landline, never a param), are intentionally not filled.
    for name in ("N Apólice", "Companhia", "outro_descricao", "Telefone"):
      assert _v(fields, name) == ""

  def test_seguro_fpb_always_ticked(self, fields, pdf_bytes):
    # Club policy: every render ships with Seguro FPB pre-ticked (not a
    # caller-supplied value), so both /V and the widget /AS must be /On.
    assert _v(fields, "Seguro FPB") == "/On"
    assert _widget_as(pdf_bytes, "Seguro FPB") == "/On"

  def test_other_insurance_boxes_stay_off(self, fields):
    for name in ("Seguro Clube", "semfpb_com", "sem_fpb_naocom"):
      assert _v(fields, name) != "/On"

  def test_seguro_fpb_ticked_on_bare_render(self):
    # Even a validate=False call with no values gets the default insurance tick.
    out = render_mod1({}, validate=False)
    assert _v(_fields(out), "Seguro FPB") == "/On"
    assert _widget_as(out, "Seguro FPB") == "/On"


class TestSignatureOverlays:
  # These probe only the overlay plumbing, so they render partial forms with
  # validate=False to skip the mandatory-field rules (covered in TestValidation).
  def test_default_render_has_no_overlays(self, pdf_bytes):
    # The module SAMPLE render passes no signatures, so the overlay areas stay
    # blank — same XObject count as an empty form (just the printed logo).
    assert _page_xobject_count(pdf_bytes) == _page_xobject_count(render_mod1({}, validate=False))

  def test_each_signature_adds_one_overlay(self):
    base = _page_xobject_count(render_mod1({}, validate=False))
    png = _png_bytes()
    assert _page_xobject_count(render_mod1({}, validate=False, player_signature=png)) == base + 1
    assert _page_xobject_count(render_mod1({}, validate=False, guardian_signature=png)) == base + 1
    assert _page_xobject_count(render_mod1({}, validate=False, club_stamp=png)) == base + 1
    three = render_mod1({}, validate=False,
                        player_signature=png, guardian_signature=png, club_stamp=png)
    assert _page_xobject_count(three) == base + 3
    assert three[:5] == b"%PDF-"
    assert len(PdfReader(io.BytesIO(three)).pages) == 1

  @pytest.mark.parametrize("slot", (
    "player_signature", "guardian_signature", "club_stamp",
  ))
  def test_undersized_overlay_reports_image_dimensions(self, slot):
    with pytest.raises(ValueError, match=r"image is too small \(1x1 pixels\)") as raised:
      render_mod1({}, validate=False, **{slot: _png_bytes((1, 1))})

    assert "Page size" not in str(raised.value)
    assert isinstance(raised.value.__cause__, ValueError)
    assert "Page size" in str(raised.value.__cause__)

  def test_four_pixel_overlay_still_works_at_default_resolution(self):
    base = _page_xobject_count(render_mod1({}, validate=False))
    out = render_mod1({}, validate=False, player_signature=_png_bytes((4, 4)))
    assert _page_xobject_count(out) == base + 1

  def test_signatures_compose_with_field_values(self):
    # Overlays don't disturb the filled fields — text/checkboxes still land.
    out = render_mod1(SAMPLE, player_signature=_png_bytes(), club_stamp=_png_bytes())
    f = _fields(out)
    assert _v(f, "Morada") == "Rua das Flores, 12"
    assert _v(f, "Feminino") == "/On"
    assert _page_xobject_count(out) == _page_xobject_count(render_mod1(SAMPLE)) + 2

  def test_signature_accepts_a_file_path(self, tmp_path):
    p = tmp_path / "stamp.png"
    p.write_bytes(_png_bytes())
    base = _page_xobject_count(render_mod1({}, validate=False))
    assert _page_xobject_count(render_mod1({}, validate=False, club_stamp=str(p))) == base + 1

  def test_blank_stamp_rect_has_no_overlay(self):
    assert rect_has_overlay(render_mod1(SAMPLE), CLUB_STAMP_RECT) is False

  def test_club_stamp_overlaps_stamp_rect(self):
    stamped = render_mod1(SAMPLE, club_stamp=_png_bytes())
    assert rect_has_overlay(stamped, CLUB_STAMP_RECT) is True

  def test_player_signature_does_not_overlap_stamp_rect(self):
    signed = render_mod1(SAMPLE, player_signature=_png_bytes())
    assert rect_has_overlay(signed, CLUB_STAMP_RECT) is False


class TestStampFillsTheSignatureDate:
  """carimbo_overlay dates the form it stamps (the Assinaturas 'Data' line).

  A form we stamp ourselves would otherwise reach the federation stamped but
  undated, since nobody is left to write the date by hand.
  """

  @pytest.fixture
  def stamp_path(self, tmp_path, monkeypatch):
    p = tmp_path / "stamp.png"
    p.write_bytes(_png_bytes())
    monkeypatch.setenv("CLUB_STAMP_PATH", str(p))
    return p

  @staticmethod
  def _stamp(pdf_bytes, *, carimbo_present=False):
    return carimbo_overlay(
      carimbo_present=carimbo_present, bbox=None, rect=CLUB_STAMP_RECT,
    )(pdf_bytes)

  @staticmethod
  def _date_fields(pdf_bytes):
    f = _fields(pdf_bytes)
    return _v(f, "ass_dia"), _v(f, "ass_mes"), _v(f, "ass_ano")

  def test_undated_form_gets_todays_date(self, stamp_path):
    undated = render_mod1({k: v for k, v in SAMPLE.items() if k != "data_assinatura"})
    stamped, result = self._stamp(undated)
    today = date.today()
    assert result.applied is True
    assert self._date_fields(stamped) == (
      f"{today.day:02d}", f"{today.month:02d}", str(today.year),
    )

  def test_date_renders_in_every_viewer(self, stamp_path):
    undated = render_mod1({k: v for k, v in SAMPLE.items() if k != "data_assinatura"})
    stamped, _ = self._stamp(undated)
    assert all(_has_appearance(stamped, n) for n in ("ass_dia", "ass_mes", "ass_ano"))

  def test_stamp_and_other_values_survive_the_date_fill(self, stamp_path):
    undated = render_mod1({k: v for k, v in SAMPLE.items() if k != "data_assinatura"})
    stamped, _ = self._stamp(undated)
    assert rect_has_overlay(stamped, CLUB_STAMP_RECT) is True
    f = _fields(stamped)
    assert _v(f, "Nome Completo") == "Rita Constança Silva"
    assert _v(f, "Feminino") == "/On"
    assert _v(f, "dn_ano") == "2010"

  def test_a_date_already_on_the_form_is_never_overwritten(self, stamp_path):
    stamped, _ = self._stamp(render_mod1(SAMPLE))
    assert self._date_fields(stamped) == ("08", "07", "2026")

  def test_a_partly_filled_date_is_left_alone(self, stamp_path):
    partial = render_mod1(
      {k: v for k, v in SAMPLE.items() if k != "data_assinatura"},
    )
    partial = _set_field(partial, "ass_ano", "2026")
    stamped, _ = self._stamp(partial)
    assert self._date_fields(stamped) == ("", "", "2026")

  def test_a_form_we_do_not_stamp_is_not_dated(self, stamp_path):
    undated = render_mod1({k: v for k, v in SAMPLE.items() if k != "data_assinatura"})
    stamped, result = self._stamp(undated, carimbo_present=True)
    assert result.applied is None
    assert self._date_fields(stamped) == ("", "", "")

  def test_no_stamp_configured_leaves_the_form_untouched(self, monkeypatch):
    monkeypatch.delenv("CLUB_STAMP_PATH", raising=False)
    undated = render_mod1({k: v for k, v in SAMPLE.items() if k != "data_assinatura"})
    stamped, result = self._stamp(undated)
    assert result.applied is None
    assert stamped == undated


class TestGenerationStampFillsTheSignatureDate:
  """render_mod1 dates the form it stamps, exactly as carimbo_overlay does.

  The invariant is "a Modelo 1 carrying the club stamp carries its Assinaturas
  date", and it has to hold whichever path applied the stamp. It matters most
  on the path that combines them: a form pre-stamped here arrives at the upload
  already stamped, so carimbo_overlay skips — and with it the date fill it owns.
  """

  @staticmethod
  def _date_fields(pdf_bytes):
    f = _fields(pdf_bytes)
    return _v(f, "ass_dia"), _v(f, "ass_mes"), _v(f, "ass_ano")

  @staticmethod
  def _undated():
    return {k: v for k, v in SAMPLE.items() if k != "data_assinatura"}

  def test_stamping_at_generation_dates_the_form(self):
    stamped = render_mod1(self._undated(), club_stamp=_png_bytes())
    today = date.today()
    assert self._date_fields(stamped) == (
      f"{today.day:02d}", f"{today.month:02d}", str(today.year),
    )

  def test_a_date_in_values_is_never_overwritten(self):
    stamped = render_mod1(SAMPLE, club_stamp=_png_bytes())
    assert self._date_fields(stamped) == ("08", "07", "2026")

  def test_an_unstamped_form_is_left_undated(self):
    assert self._date_fields(render_mod1(self._undated())) == ("", "", "")

  def test_a_signature_alone_does_not_date_the_form(self):
    """Only the club stamp is an endorsement to date; signatures are not."""
    signed = render_mod1(
      self._undated(),
      player_signature=_png_bytes(),
      guardian_signature=_png_bytes(),
    )
    assert self._date_fields(signed) == ("", "", "")

  def test_the_date_renders_in_every_viewer(self):
    stamped = render_mod1(self._undated(), club_stamp=_png_bytes())
    assert all(_has_appearance(stamped, n) for n in ("ass_dia", "ass_mes", "ass_ano"))

  def test_the_stamp_and_the_other_values_survive_the_date_fill(self):
    stamped = render_mod1(self._undated(), club_stamp=_png_bytes())
    assert rect_has_overlay(stamped, CLUB_STAMP_RECT) is True
    f = _fields(stamped)
    assert _v(f, "Nome Completo") == "Rita Constança Silva"
    assert _v(f, "Feminino") == "/On"
    assert _v(f, "dn_ano") == "2010"

  def test_a_form_stamped_at_generation_stays_dated_through_the_upload(self, tmp_path, monkeypatch):
    """The case that used to slip through: pre-stamped, so the upload skips."""
    stamp = tmp_path / "stamp.png"
    stamp.write_bytes(_png_bytes())
    monkeypatch.setenv("CLUB_STAMP_PATH", str(stamp))
    generated = render_mod1(self._undated(), club_stamp=_png_bytes())

    # What the upload path resolves for a template PDF with no OCR: it inspects
    # the fixed stamp rect itself (see _mod1_overlay_fields).
    carimbo_present = rect_has_overlay(generated, CLUB_STAMP_RECT)
    assert carimbo_present is True
    uploaded, result = carimbo_overlay(
      carimbo_present=carimbo_present, bbox=None, rect=CLUB_STAMP_RECT,
    )(generated)

    assert result.applied is None and result.effective is True   # not stamped twice
    today = date.today()
    assert self._date_fields(uploaded) == (
      f"{today.day:02d}", f"{today.month:02d}", str(today.year),
    )


class TestFillSignatureDate:
  def test_non_template_pdf_is_returned_unchanged(self):
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    buf = io.BytesIO()
    writer.write(buf)
    assert fill_signature_date(buf.getvalue()) == buf.getvalue()

  def test_explicit_date_is_used(self):
    undated = render_mod1({k: v for k, v in SAMPLE.items() if k != "data_assinatura"})
    out = fill_signature_date(undated, on=date(2026, 3, 9))
    f = _fields(out)
    assert (_v(f, "ass_dia"), _v(f, "ass_mes"), _v(f, "ass_ano")) == ("09", "03", "2026")


def test_blank_and_unknown_values_are_skipped():
  f = _fields(render_mod1({"morada": "", "nif": None, "bogus_key": "x"}, validate=False))
  assert _v(f, "Morada") == ""
  assert _v(f, "Nr Contribuinte") == ""


def test_wheel_install_can_fill_mod1(tmp_path):
  """The template must be available from a normal, non-editable wheel install.

  Installed with ``pip install --target`` (not a nested venv): a
  ``--system-site-packages`` venv built from ``sys.executable`` inherits from
  the *real* base interpreter, not from whatever venv is currently running
  pytest, so it can silently miss this environment's ``mcp``/``pikepdf``/etc.
  ``--target`` + a ``PYTHONPATH`` prefix isolates only the package under test
  (proving the wheel ships its own data) while still resolving the rest of
  the dependency graph from the environment already running this test.

  Builds with ``--no-build-isolation`` to stay offline, which requires the
  build backend to already be importable here; a venv without it skips rather
  than reporting a packaging regression it never actually tested.
  """
  pytest.importorskip("setuptools", reason="--no-build-isolation needs the backend installed")
  root = Path(__file__).resolve().parents[1]
  dist = tmp_path / "dist"
  subprocess.run(
    [sys.executable, "-m", "pip", "wheel", ".", "--no-deps", "--no-build-isolation", "--wheel-dir", str(dist)],
    cwd=root,
    check=True,
    capture_output=True,
    text=True,
  )
  wheel = next(dist.glob("sav_client-*.whl"))
  target = tmp_path / "site"
  subprocess.run(
    [sys.executable, "-m", "pip", "install", "--no-deps", "--target", str(target), str(wheel)],
    check=True, capture_output=True, text=True,
  )
  script = """
from sav_shared.fpb_mod1 import render_mod1
values = %r
pdf = render_mod1(values, season="2026/2027")
assert pdf.startswith(b"%%PDF-")
print(len(pdf))
""" % {**{k: v for k, v in SAMPLE.items() if not k.startswith("guardian_")}, "nasc": "1990-03-07"}
  env = {**os.environ, "PYTHONPATH": str(target)}
  result = subprocess.run(
    [sys.executable, "-c", script], cwd=tmp_path, env=env, check=True, capture_output=True, text=True,
  )
  assert int(result.stdout.strip()) > 0


def test_eu_date_format_also_splits():
  f = _fields(render_mod1({"nasc": "07-03-2010"}, validate=False))
  assert (_v(f, "dn_dia"), _v(f, "dn_mes"), _v(f, "dn_ano")) == ("07", "03", "2010")


def test_checkgroups_accept_int_or_name():
  # genero by int, escalao aliases resolve to the same boxes.
  f = _fields(render_mod1({"genero": 2}, validate=False))
  assert _v(f, "Feminino") == "/On"
  f = _fields(render_mod1({"escalao": "Baby-Basket"}, validate=False))
  assert _v(f, "BabyBasket") == "/On"
  f = _fields(render_mod1({"escalao": "Masters / Veteranos"}, validate=False))
  assert _v(f, "Master") == "/On"


def test_empty_values_render_a_valid_blank_form():
  out = render_mod1({}, validate=False)
  assert out[:5] == b"%PDF-"
  assert len(PdfReader(io.BytesIO(out)).pages) == 1


def test_render_requires_explicit_season():
  with pytest.raises(TypeError, match="season"):
    _render_mod1({}, validate=False)


@pytest.mark.parametrize("season", ("2026", "2026-2027", "2026/2028", 2026))
def test_render_rejects_invalid_season(season):
  with pytest.raises(ValueError, match="season"):
    _render_mod1({}, season=season, validate=False)


@pytest.mark.parametrize("key", (
  "season", "season_id", "season_start_year", "season_end_year",
  "epoca", "epoca_id", "epoca1", "epoca2",
))
def test_render_rejects_season_overrides_in_values(key):
  with pytest.raises(ValueError, match="supplied separately"):
    _render_mod1({key: "wrong"}, season=SEASON, validate=False)


def _without(values, *keys):
  return {k: v for k, v in values.items() if k not in keys}


# A valid adult sample: born 1990, guardian block omitted. data_assinatura
# (kept from SAMPLE) pins the age reference so the result doesn't drift with the
# wall clock.
ADULT = {**_without(SAMPLE, *_MOD1_GUARDIAN_KEYS), "nasc": "1990-01-01"}


class TestValidation:
  def test_complete_minor_sample_is_valid(self):
    # SAMPLE: nasc 2010 + data_assinatura 2026 -> age 16, full guardian block.
    assert validate_mod1_values(SAMPLE) == []

  def test_complete_adult_sample_is_valid(self):
    assert validate_mod1_values(ADULT) == []

  def test_render_raises_listing_problems(self):
    with pytest.raises(ValueError, match="required"):
      render_mod1({})

  def test_render_succeeds_on_valid_sample(self):
    assert render_mod1(SAMPLE)[:5] == b"%PDF-"  # validate=True by default

  def test_missing_core_field_is_reported(self):
    assert any("morada" in p for p in validate_mod1_values(_without(SAMPLE, "morada")))

  def test_consent_must_be_present(self):
    assert any("consent_data" in p for p in validate_mod1_values(_without(SAMPLE, "consent_data")))
    # False is a valid choice, not "missing".
    assert validate_mod1_values({**SAMPLE, "consent_data": False}) == []

  def test_license_optional_for_primeira_inscricao(self):
    # SAMPLE is a 1ª Inscrição (tipo_inscricao=1) — no licence exists yet.
    assert validate_mod1_values(_without(SAMPLE, "license")) == []

  def test_license_required_for_revalidacao(self):
    reval = {**_without(SAMPLE, "license"), "tipo_inscricao": 2}
    assert any("license" in p and "Revalida" in p for p in validate_mod1_values(reval))

  def test_minor_requires_full_guardian_block(self):
    problems = validate_mod1_values(_without(SAMPLE, "guardian_email"))
    assert any("minor" in p and "guardian_email" in p for p in problems)

  def test_adult_must_leave_guardian_block_empty(self):
    problems = validate_mod1_values({**ADULT, "guardian_name": "Someone"})
    assert any("adult" in p and "guardian_name" in p for p in problems)

  def test_unusable_values_are_flagged(self):
    problems = validate_mod1_values({**SAMPLE, "escalao": "Sub 99", "dataval": "nope"})
    assert any("escalao" in p for p in problems)
    assert any("dataval" in p for p in problems)


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
  assert {"epoca1", "epoca2"} <= names


def test_bundled_template_has_no_prefilled_values():
  with pikepdf.open(render_mod1_template_path()) as pdf:
    values = {
      str(f.T): str(f.V) if "/V" in f else ""
      for f in pdf.Root.AcroForm.Fields if "/T" in f
    }
  assert all(value in ("", "/Off") for value in values.values())


def render_mod1_template_path():
  from sav_shared.fpb_mod1 import _MOD1_TEMPLATE
  return str(_MOD1_TEMPLATE)
