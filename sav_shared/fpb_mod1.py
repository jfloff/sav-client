"""SAV-side mapping + reconciliation for FPB mod 1 forms.

sav-parsers is SAV2-agnostic — it returns ParsedField values from Document AI.
This module owns the translation from those entities into SAV2 add-player
kwargs and the reconciliation against a stored SAV profile.
"""
from __future__ import annotations

import difflib
import io
import logging
import os
import re
import tempfile
import unicodedata
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pikepdf
from pypdf import PdfReader, PdfWriter

from .files import bbox_to_pdf_rect, overlay_image_on_pdf

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
  from sav_parsers import ParsedField
  from sav_parsers.types import BBox

from .fields import (
  RECONCILE_READONLY as _RECONCILE_READONLY,
  RECONCILE_TEXT    as _RECONCILE_TEXT,
)
from .lookups import distrito_name, find_distrito_id, find_id_by_name


# tipo_inscricao SAV2 int values → OCR entity name.  When neither checkbox is
# checked on the form, derive_enrollment_params falls back to a NIF lookup and
# we then need to overlay the mark ourselves before uploading.
_INSCRICAO_FIELD: dict[int, str] = {
  1: "tipo_inscricao_primeira",
  2: "tipo_inscricao_revalidacao",
}

# tipo_identificacao SAV2 int values — OCR side is a multi-checkbox group,
# not a single text entity, so this stays bespoke instead of going through
# fields.FIELDS / _RECONCILE_TEXT.
_ID_TYPE: dict[str, int] = {
  "tipo_doc_cc":         1,  # Cartão de Cidadão
  "tipo_doc_passaporte": 2,
  "tipo_doc_outro":      3,  # defaults to Título de residência
}

# tipoRegulacao SAV2 int values — same checkbox-group pattern as _ID_TYPE.
_GUARDIAN_RELATION: dict[str, int] = {
  "parentesco_encarregado_pai":   1,
  "parentesco_encarregado_mae":   2,
  "parentesco_encarregado_tutor": 3,
}

_CONFIDENCE_LOW  = 0.60
_SIMILARITY_KEEP = 0.50
# Set deliberately low so values that are clearly the same with formatting
# diffs (abbreviations, accents, punctuation, occasional OCR misreads) keep
# SAV silently. OCR only wins when the strings are genuinely divergent —
# real address change, totally different document, etc.


def _digits_only(value: str) -> str:
  return re.sub(r"\D", "", value)


@dataclass
class ReconcileResult:
  kwargs: dict[str, Any]                           # final kwargs to submit (after pop+override)
  ocr:    dict[str, Any]                           # OCR-derived values (submittable + read-only)
  updated: dict[str, tuple]                        # kwarg → (sav_value, ocr_value)
  kept: dict[str, tuple]                           # kwarg → (sav_value, ocr_value, similarity)
  needs_review: list[str]                          # kwarg names: low confidence, no reliable SAV match
  mismatches: list[dict]                           # {entity, ocr, sav, confidence} for retraining
  retrain_corrections: dict[str, str] = field(default_factory=dict)
  # entity name → SAV value, for read-only fields where OCR diverged from SAV.
  # Merged into the corrections dict at close_processing time.
  concelhos: dict[int, str] = field(default_factory=dict)
  # Per-distrito concelho lookup, so display layers can format concelho_id → name.


def _normalize(s: str) -> str:
  return unicodedata.normalize("NFD", s.upper()).encode("ascii", "ignore").decode()


def _similarity(a: str, b: str) -> float:
  return difflib.SequenceMatcher(None, _normalize(a), _normalize(b)).ratio()


def _pick_checked(fields: dict[str, ParsedField], mapping: dict[str, int]) -> int | None:
  for entity, int_val in mapping.items():
    f = fields.get(entity)
    if f and f.value:
      return int_val
  return None


def read_carimbo(parsed: dict[str, ParsedField]) -> tuple[bool | None, BBox | None]:
  """Read 'carimbo_clube_presente' from a parse_fpb_mod1 fields dict.

  Returns (present, bbox). present is True/False when OCR resolved the
  field, None when the field is missing or unresolved. bbox is the
  normalized stamp-slot box from Document AI, or None when the entity
  wasn't detected or had no page_anchor.
  """
  field = parsed.get("carimbo_clube_presente")
  if field is None:
    return (None, None)
  present = None if field.value is None else bool(field.value)
  return (present, field.bbox)


def read_tipo_inscricao(
  parsed: dict[str, ParsedField], reg_type: int,
) -> tuple[bool | None, BBox | None]:
  """Read whether the tipo_inscricao checkbox for `reg_type` is already marked.

  Returns (already_checked, bbox):
    True  — OCR found the checkbox already marked → skip overlay.
    False — OCR found it blank → overlay needed; bbox is the page_anchor.
    None  — field absent or unresolved → skip (safe, avoid double-mark).

  `reg_type` 1 = 1ª Inscrição, 2 = Revalidação (any other value → None, None).
  Mirrors read_carimbo.
  """
  entity = _INSCRICAO_FIELD.get(reg_type)
  if entity is None:
    return (None, None)
  field = parsed.get(entity)
  if field is None:
    return (None, None)
  already_checked = None if field.value is None else bool(field.value)
  return (already_checked, field.bbox)


def _make_cross_png(width: int, height: int) -> bytes:
  """Return PNG bytes of a bold × on a transparent background, sized `width`×`height`."""
  from PIL import Image, ImageDraw
  img  = Image.new("RGBA", (width, height), (0, 0, 0, 0))
  draw = ImageDraw.Draw(img)
  pad  = max(2, min(width, height) // 6)
  lw   = max(2, min(width, height) // 5)
  draw.line([(pad, pad), (width - pad, height - pad)], fill=(0, 0, 0, 255), width=lw)
  draw.line([(width - pad, pad), (pad, height - pad)], fill=(0, 0, 0, 255), width=lw)
  buf = io.BytesIO()
  img.save(buf, format="PNG")
  return buf.getvalue()


def overlay_tipo_inscricao(
  pdf_bytes: bytes,
  *,
  reg_type: int,
  already_checked: bool | None = None,
  bbox: BBox | None = None,
) -> bytes:
  """Overlay an × mark on the tipo_inscricao checkbox when it is not already marked.

  already_checked:
    False - OCR ran and found the box blank → mark it.
    True  - OCR ran and found it already checked → skip.
    None  - no OCR result (unknown) → skip, to avoid marking incorrectly.

  Raises ValueError when marking is wanted but `bbox` is None (OCR didn't
  return a location). Raises on overlay failures.
  Use inscricao_overlay() to get error-catching + OverlayResult wrapping.
  """
  if already_checked is not False:
    return pdf_bytes
  if bbox is None:
    entity = _INSCRICAO_FIELD.get(reg_type, f"tipo_inscricao (reg_type={reg_type})")
    raise ValueError(f"OCR did not return a location for {entity}")
  rect = bbox_to_pdf_rect(pdf_bytes, bbox.vertices, page_index=bbox.page)
  x0, y0, x1, y1 = rect
  w = max(1, int(round(x1 - x0)))
  h = max(1, int(round(y1 - y0)))
  cross_bytes = _make_cross_png(w, h)
  return overlay_image_on_pdf(pdf_bytes, cross_bytes, rect=rect, page_index=bbox.page)


# The OCR carimbo slot is sized to the form's printed box, which is smaller
# than the physical stamp. Scale the placement rect (about its center) so the
# overlaid stamp reads at a realistic size, then nudge it up by a fraction of
# its scaled height so it sits above the printed slot rather than over it.
_CLUB_STAMP_SCALE = 5.5
_CLUB_STAMP_Y_SHIFT = 0.5


def _scale_rect(
  rect: tuple[float, float, float, float], scale: float
) -> tuple[float, float, float, float]:
  """Scale a (x0, y0, x1, y1) rect by `scale` about its center."""
  x0, y0, x1, y1 = rect
  cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
  half_w, half_h = (x1 - x0) / 2 * scale, (y1 - y0) / 2 * scale
  return (cx - half_w, cy - half_h, cx + half_w, cy + half_h)


def overlay_club_stamp(
  pdf_bytes: bytes,
  *,
  carimbo_present: bool | None = None,
  bbox: BBox | None = None,
) -> bytes:
  """Overlay the club stamp from CLUB_STAMP_PATH onto a mod 1 PDF at the
  location OCR detected for the carimbo_clube_presente entity.

  carimbo_present:
    False - OCR ran and found no club stamp → stamp the PDF at `bbox`.
    True  - OCR ran and detected an existing club stamp → skip.
    None  - no OCR ran (unknown) → skip, to avoid double-stamping.

  Raises ValueError when stamping is wanted but `bbox` is None (OCR didn't
  return a stamp location). Raises on other overlay failures.
  Use carimbo_overlay() to get error-catching + OverlayResult wrapping.
  """
  if carimbo_present is not False:
    return pdf_bytes
  stamp_path = os.environ.get("CLUB_STAMP_PATH")
  if not stamp_path:
    return pdf_bytes
  if bbox is None:
    raise ValueError("OCR did not return a location for carimbo_clube_presente")

  with open(stamp_path, "rb") as f:
    stamp_bytes = f.read()
  rect = bbox_to_pdf_rect(pdf_bytes, bbox.vertices, page_index=bbox.page)
  rect = _scale_rect(rect, _CLUB_STAMP_SCALE)
  x0, y0, x1, y1 = rect
  dy = (y1 - y0) * _CLUB_STAMP_Y_SHIFT  # PDF origin is bottom-left, so up is +y
  rect = (x0, y0 + dy, x1, y1 + dy)
  return overlay_image_on_pdf(pdf_bytes, stamp_bytes, rect=rect, page_index=bbox.page)


@dataclass
class OverlayResult:
  """Outcome of a single PDF overlay pass.

  applied:
    True  — the overlay was applied successfully.
    None  — skipped; not needed or precondition unknown (see effective).
    False — attempted but failed; `error` is set.

  effective:
    True  — the feature is present in the uploaded PDF (was already there
            or we just added it).
    False — the feature is absent (missing and we couldn't / didn't add it).
    None  — unknown (OCR didn't resolve the pre-condition).

  error: human-readable failure description when applied is False.
  """
  applied:   bool | None
  effective: bool | None
  error:     str | None = None


def carimbo_overlay(
  *, carimbo_present: bool | None, bbox: BBox | None,
) -> Callable[[bytes], tuple[bytes, OverlayResult]]:
  """Return an overlay callable that applies the club stamp when OCR says it's missing.

  Skips (applied=None) when carimbo_present is not False or CLUB_STAMP_PATH is
  unset.  effective reflects the final stamp state in the PDF regardless of
  whether *we* applied it.  Captures params via closure.
  """
  def apply(pdf_bytes: bytes) -> tuple[bytes, OverlayResult]:
    if carimbo_present is True:
      return pdf_bytes, OverlayResult(applied=None, effective=True)
    if carimbo_present is None:
      return pdf_bytes, OverlayResult(applied=None, effective=None)
    # carimbo_present is False — stamp is missing
    if not os.environ.get("CLUB_STAMP_PATH"):
      return pdf_bytes, OverlayResult(applied=None, effective=False)
    try:
      return (
        overlay_club_stamp(pdf_bytes, carimbo_present=carimbo_present, bbox=bbox),
        OverlayResult(applied=True, effective=True),
      )
    except Exception as exc:
      logger.warning("carimbo overlay failed", exc_info=True)
      return pdf_bytes, OverlayResult(applied=False, effective=False, error=f"club stamp failed: {exc}")
  return apply


def inscricao_overlay(
  *, reg_type: int | None, already_checked: bool | None, bbox: BBox | None,
) -> Callable[[bytes], tuple[bytes, OverlayResult]]:
  """Return an overlay callable that marks the tipo_inscricao checkbox when it's blank.

  Skips when already_checked is not False or reg_type is None (unknown type).
  effective reflects whether the correct checkbox is marked in the PDF.
  Captures params via closure.
  """
  def apply(pdf_bytes: bytes) -> tuple[bytes, OverlayResult]:
    if already_checked is True:
      return pdf_bytes, OverlayResult(applied=None, effective=True)
    if already_checked is None or reg_type is None:
      return pdf_bytes, OverlayResult(applied=None, effective=None)
    # already_checked is False — checkbox is blank
    try:
      return (
        overlay_tipo_inscricao(pdf_bytes, reg_type=reg_type, already_checked=already_checked, bbox=bbox),
        OverlayResult(applied=True, effective=True),
      )
    except Exception as exc:
      logger.warning("inscricao overlay failed", exc_info=True)
      return pdf_bytes, OverlayResult(applied=False, effective=False, error=f"inscription checkbox failed: {exc}")
  return apply


@contextmanager
def overlaid_pdf(
  pdf_path: str,
  *overlays: Callable[[bytes], tuple[bytes, OverlayResult]],
  dest_dir: str | os.PathLike[str] | None = None,
) -> Iterator[tuple[str, list[OverlayResult]]]:
  """Yield (upload_path, has_club_stamp, stamp_error, has_inscricao_mark, inscricao_error).

  `dest_dir`, when given, is where the modified copy is written (as
  `stamped.pdf`) instead of a standalone temp file; the caller then owns its
  lifecycle (e.g. an OCR processing dir that gets cleaned up wholesale). When
  None, a NamedTemporaryFile is used and removed on context exit.

  Applies up to two overlays in order: inscription checkbox mark first, then
  Each overlay is a ``Callable[[bytes], tuple[bytes, OverlayResult]]`` — use
  carimbo_overlay() and inscricao_overlay() (or any compatible factory) to
  build them.  Overlays run in order; failures are caught inside each factory
  so one bad overlay never blocks the next.

  When no overlay fires (all results have applied=None), the original
  `pdf_path` is yielded unchanged — no temp file is written.

  `dest_dir`, when given, is where the modified copy is written (as
  ``stamped.pdf``); the caller owns that directory's lifecycle.  When None,
  a NamedTemporaryFile is used and removed on context exit.
  """
  if not overlays:
    yield pdf_path, []
    return

  with open(pdf_path, "rb") as f:
    pdf_bytes = f.read()

  results: list[OverlayResult] = []
  changed = False
  for fn in overlays:
    pdf_bytes, r = fn(pdf_bytes)
    results.append(r)
    if r.applied is True:
      changed = True

  if not changed:
    # Nothing was modified — yield the original path, no I/O needed.
    yield pdf_path, results
    return

  tmp_path: str | None = None
  # In dest_dir mode the caller owns the directory's lifecycle, so we never
  # unlink the file we wrote — it is swept when the dir is.
  owns_file = dest_dir is None
  try:
    if dest_dir is not None:
      tmp_path = os.path.join(os.fspath(dest_dir), "stamped.pdf")
      with open(tmp_path, "wb") as f:
        f.write(pdf_bytes)
    else:
      with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        tmp_path = f.name
        f.write(pdf_bytes)
  except Exception:
    logger.warning(
      "Failed to write overlaid PDF for %r; uploading the original.",
      pdf_path, exc_info=True,
    )
    tmp_path = None

  try:
    yield tmp_path or pdf_path, results
  finally:
    if owns_file and tmp_path and os.path.exists(tmp_path):
      os.unlink(tmp_path)


def effective_distrito_id(
  parsed: dict[str, ParsedField], sav_profile: dict,
) -> int:
  """Pick the distrito to scope the concelho lookup to.

  Prefers the OCR-resolved distrito (so cross-distrito moves use the new
  distrito's concelho list). Falls back to SAV's stored distrito if the OCR
  text doesn't resolve. Returns 0 when neither is usable — list_concelhos
  treats that as "no list" and concelho gracefully falls to needs_review.
  """
  field = parsed.get("distrito")
  ocr_id = find_distrito_id(field.value) if (field and field.value) else None
  if ocr_id:
    return ocr_id
  try:
    return int(sav_profile.get("distrito") or 0)
  except (ValueError, TypeError):
    return 0


def fpb_mod1_to_sav_kwargs(
  fields: dict[str, ParsedField],
  *,
  concelhos: dict[int, str] | None = None,
) -> dict:
  """Map parse_fpb_mod1 output to add_player_to_registration_batch kwargs.

  `concelhos` is the distrito-scoped {id → name} lookup from the SAV2
  wizard (op=18); without it, concelho_id resolves to None and SAV will
  fall back to its prefill value.

  Returns a dict that includes 'license' (pop it out before **-spreading):
    kwargs = fpb_mod1_to_sav_kwargs(fields)
    license = kwargs.pop("license")
    client.add_player_to_registration_batch(
      batch_id, license, exam_date="YYYY-MM-DD", **kwargs,
    )
  """
  def val(key: str):
    f = fields.get(key)
    return f.value if f else None

  kwargs: dict = {
    "license":                val("licenca_fpb"),
    "id_type":                _pick_checked(fields, _ID_TYPE),
    "id_number":              val("num_doc_identificacao"),
    "id_expiry":              val("validade_doc"),
    "telemovel":              val("telemovel"),
    "telefone":               val("telefone"),
    "email":                  val("email_jogador"),
    "cod_postal":             val("codigo_postal"),
    "morada":                 val("morada"),
    "localidade_txt":         val("localidade"),
    "distrito_id":            find_distrito_id(val("distrito")),
    "concelho_id":            find_id_by_name(val("concelho"), concelhos or {}),
    "guardian_name":          val("nome_encarregado"),
    "guardian_relation":      _pick_checked(fields, _GUARDIAN_RELATION),
    "guardian_phone":         val("telefone_encarregado"),
    "guardian_email":         val("email_encarregado"),
    "consent_data":           val("consentimento_dados") or False,
    "consent_communications": val("consentimento_comunicacoes") or False,
    "consent_marketing":      val("consentimento_marketing") or False,
  }
  return {k: v for k, v in kwargs.items() if v is not None or k == "license"}


def reconcile_fpb_mod1(
  parsed: dict[str, ParsedField],
  sav_profile: dict,
  *,
  client: Any = None,
) -> ReconcileResult:
  """Compare OCR fields against the player's current SAV profile.

  sav_profile is the merged op=35 record + step2 address prefill dict.
  Fields with OCR ≈ SAV (similarity >= 0.80) silently keep the SAV value.
  When OCR diverges from SAV: high confidence (>= 0.60) auto-accepts OCR
  (updated); low confidence asks the user (needs_review).
  Genuine divergences are collected in mismatches for retraining.

  When `client` is given, the distrito-scoped concelho list is fetched (and
  cached) from SAV so concelho_id resolves and the result carries the lookup
  for downstream display. Without it, concelho silently falls to needs_review.
  Network errors from the lookup propagate to the caller.
  """
  concelhos: dict[int, str] = {}
  if client is not None:
    distrito_id = effective_distrito_id(parsed, sav_profile)
    if distrito_id:
      concelhos = client.list_concelhos(distrito_id)
  base = fpb_mod1_to_sav_kwargs(parsed, concelhos=concelhos)
  ocr_snapshot = dict(base)

  # Enrich sav_profile with the resolved distrito/concelho names so the
  # text-vs-text reconcile loop (and downstream display in cli/mcp) can
  # compare and render them. Mutates in place — callers reuse this dict
  # for the submission summary, so the new keys must persist past return.
  sav_profile["distrito_name"] = distrito_name(sav_profile.get("distrito"))
  raw_concelho = sav_profile.get("concelho")
  if str(raw_concelho or "").isdigit():
    sav_profile["concelho_name"] = concelhos.get(int(raw_concelho), "")
  else:
    sav_profile["concelho_name"] = ""

  # For ID-lookup kwargs, ocr_snapshot should hold the raw OCR text (for the
  # SAV/OCR display columns), not the resolved int. The submit-side `base`
  # still holds the int (or None if unresolved).
  distrito_field = parsed.get("distrito")
  if distrito_field and distrito_field.value:
    ocr_snapshot["distrito_id"] = str(distrito_field.value)
  concelho_field = parsed.get("concelho")
  if concelho_field and concelho_field.value:
    ocr_snapshot["concelho_id"] = str(concelho_field.value)

  updated: dict[str, tuple] = {}
  kept: dict[str, tuple] = {}
  needs_review: list[str] = []
  mismatches: list[dict] = []

  for entity, sav_key, kwarg in _RECONCILE_TEXT:
    field = parsed.get(entity)
    if field is None:
      continue

    ocr_val  = str(field.value) if field.value is not None else ""
    sav_val  = str(sav_profile.get(sav_key) or "")
    conf     = field.confidence

    if not ocr_val:
      continue

    if sav_val:
      sim = _similarity(ocr_val, sav_val)
      if sim >= _SIMILARITY_KEEP:
        base.pop(kwarg, None)
        if ocr_val != sav_val:
          kept[kwarg] = (sav_val, ocr_val, sim)
      elif conf < _CONFIDENCE_LOW:
        base.pop(kwarg, None)
        needs_review.append(kwarg)
        mismatches.append({
          "entity": entity, "ocr": ocr_val,
          "sav": sav_val, "confidence": conf,
        })
      else:
        if kwarg in base:
          updated[kwarg] = (sav_val, ocr_val)
        else:
          # OCR yielded a value but it couldn't be translated to a
          # submittable kwarg (e.g. distrito text not in lookup table).
          # Surface for review so the user can confirm or correct.
          needs_review.append(kwarg)
        mismatches.append({
          "entity": entity, "ocr": ocr_val,
          "sav": sav_val, "confidence": conf,
        })
    else:
      if conf < _CONFIDENCE_LOW:
        needs_review.append(kwarg)

  retrain_corrections: dict[str, str] = {}
  for entity, sav_key in _RECONCILE_READONLY:
    parsed_field = parsed.get(entity)
    if parsed_field is None or parsed_field.value in (None, ""):
      continue
    ocr_val = str(parsed_field.value)
    ocr_snapshot[entity] = ocr_val
    sav_val = str(sav_profile.get(sav_key) or "")
    if not sav_val:
      continue
    if _digits_only(ocr_val) and _digits_only(ocr_val) == _digits_only(sav_val):
      continue
    if _normalize(ocr_val) == _normalize(sav_val):
      continue
    retrain_corrections[entity] = sav_val
    mismatches.append({
      "entity":     entity,
      "ocr":        ocr_val,
      "sav":        sav_val,
      "confidence": parsed_field.confidence,
    })

  return ReconcileResult(
    kwargs=base,
    ocr=ocr_snapshot,
    updated=updated,
    kept=kept,
    needs_review=needs_review,
    mismatches=mismatches,
    retrain_corrections=retrain_corrections,
    concelhos=concelhos,
  )


# ── Modelo 1 form rendering (outbound: values → filled PDF) ───────────────────
#
# The reconcile path above is inbound (scanned form → SAV kwargs). This section
# goes the other way: given a dict of values keyed by the canonical
# sav_shared.fields.FIELDS keys, it fills the blank Modelo 1 **AcroForm** and
# returns print-ready PDF bytes.
#
# The template is a real fillable PDF form (72 named fields), so filling is done
# by field *name* — no coordinates, no overlays. pypdf is used (not pikepdf)
# because it regenerates field appearance streams, so the text renders/prints in
# every viewer (Quartz/Preview included); pikepdf + NeedAppearances leaves text
# blank in viewers that don't regenerate appearances. Only data fields are
# written; the player signature, guardian signature, and club stamp have no form
# fields (physical lines), so they are naturally left blank for hand completion.

# Bundled blank AcroForm. Resolved relative to the repo root (files/mod1/), with
# an env override mirroring CLUB_STAMP_PATH so an installed deployment can point
# elsewhere.
_MOD1_TEMPLATE = Path(__file__).resolve().parent.parent / "files" / "mod1" / "fpb-mod1.template.pdf"

# Checkbox on-state export value shared by every /Btn field on this form.
_ON_STATE = "/On"

# int value → PDF checkbox field name. The ints come straight from the inbound
# maps (_ID_TYPE / _GUARDIAN_RELATION) so a `tipo` / `guardian_relation` value
# means the same box in both directions.
_ID_TYPE_PDF_FIELD: dict[int, str] = {
  _ID_TYPE["tipo_doc_cc"]:         "Cartão Cidadão",
  _ID_TYPE["tipo_doc_passaporte"]: "Passaporte",
  _ID_TYPE["tipo_doc_outro"]:      "Outro",
}
_GUARDIAN_RELATION_PDF_FIELD: dict[int, str] = {
  _GUARDIAN_RELATION["parentesco_encarregado_pai"]:   "pai",
  _GUARDIAN_RELATION["parentesco_encarregado_mae"]:   "mae",
  _GUARDIAN_RELATION["parentesco_encarregado_tutor"]: "Tutor",
}

# The guardian id-document checkboxes mirror the player's id-type ints.
_GUARDIAN_ID_TYPE_PDF_FIELD: dict[int, str] = {
  _ID_TYPE["tipo_doc_cc"]:         "titular do Cartão Cidadão",
  _ID_TYPE["tipo_doc_passaporte"]: "passaporte_2",
  _ID_TYPE["tipo_doc_outro"]:      "Outro_2",
}

# tipo_inscricao ints match the inbound _INSCRICAO_FIELD map (1=1ª, 2=Revalidação).
_INSCRICAO_PDF_FIELD: dict[int, str] = {1: "primeira", 2: "revalidacao"}
_INSCRICAO_NAME_FIELD: dict[str, str] = {
  "primeira": "primeira", "primeirainscricao": "primeira", "1ainscricao": "primeira",
  "revalidacao": "revalidacao", "reval": "revalidacao",
}

# genero ints match sav_shared.lookups.GENERO (1=Masculino, 2=Feminino).
_GENERO_PDF_FIELD: dict[int, str] = {1: "Masculino", 2: "Feminino"}
_GENERO_NAME_FIELD: dict[str, str] = {
  "masculino": "Masculino", "m": "Masculino",
  "feminino": "Feminino", "f": "Feminino",
}

# Escalão has one gender-agnostic checkbox per tier, but SAV tier ids are
# gender-dependent, so this group resolves by *name* only (Player.tier gives it
# directly). Keys are normalized tokens; several aliases fold onto one box.
_ESCALAO_NAME_FIELD: dict[str, str] = {
  "babybasket": "BabyBasket",
  "mini8": "Mini8", "mini10": "Mini10", "mini12": "Mini12",
  "sub14": "Sub14", "sub16": "Sub16", "sub18": "Sub18",
  "senior": "Sénior",
  "master": "Master", "masters": "Master", "mastersveteranos": "Master", "veteranos": "Master",
  "bcr": "BCR",
}


# Signature / stamp overlay areas. Unlike the data fields these are *not* form
# fields — the form has only printed lines here — so they are placed by
# coordinate, like the inbound club-stamp overlay. Rects are (x0, y0, x1, y1) in
# PDF points on the A4 template (595.2 × 841.92, origin bottom-left), calibrated
# against the printed labels: player = the "Jogador(a)" column of the Assinaturas
# box, club stamp = the "Diretor(a) e Carimbo Clube" column, guardian = the
# "Assinatura ____" line in the poder-paternal section. overlay_image_on_pdf
# centers each image inside its rect preserving aspect, so a wide signature sits
# on the line and a roughly-square stamp fills its column.
_PLAYER_SIGNATURE_RECT:   tuple[float, float, float, float] = (55.0, 196.0, 245.0, 228.0)
_GUARDIAN_SIGNATURE_RECT: tuple[float, float, float, float] = (215.0, 72.0, 500.0, 97.0)
_CLUB_STAMP_RECT:         tuple[float, float, float, float] = (410.0, 193.0, 545.0, 231.0)


def _load_image_bytes(arg: bytes | str | os.PathLike[str] | None) -> bytes | None:
  """Coerce a signature/stamp argument to image bytes.

  Accepts raw image bytes (as MCP passes them after base64-decoding) or a path
  to an image file (as the CLI passes). None passes through as None so the
  caller can leave that area blank for hand completion.
  """
  if arg is None:
    return None
  if isinstance(arg, (bytes, bytearray)):
    return bytes(arg)
  with open(os.fspath(arg), "rb") as f:
    return f.read()


def _norm_token(s: str) -> str:
  """Lowercase, strip accents, drop non-alphanumerics — for matching names."""
  n = unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode().lower()
  return "".join(ch for ch in n if ch.isalnum())


@dataclass(frozen=True)
class _Text:
  """Write the value straight into one text field."""
  field: str


@dataclass(frozen=True)
class _Date:
  """Split an ISO/EU date into day / month / year text fields."""
  dia: str
  mes: str
  ano: str


@dataclass(frozen=True)
class _Postal:
  """Split a Portuguese postal code (XXXX-XXX) into two text fields."""
  cod4: str
  cp3: str


@dataclass(frozen=True)
class _CheckGroup:
  """Tick one box of a mutually-exclusive group.

  The chosen box is resolved from an int code (`by_int`) and/or a name
  (`by_name`, keyed by normalized token). A value is tried as an int first,
  then as a normalized name — so callers may pass ``1``/``2``/``3`` or
  ``"Masculino"``/``"Sub 14"`` interchangeably.
  """
  by_int: dict[int, str] | None = None
  by_name: dict[str, str] | None = None


@dataclass(frozen=True)
class _Consent:
  """Tick SIM or NÃO from a boolean value."""
  yes: str
  no: str


# Canonical FIELDS key → how to place it on the form. Keys match the vocabulary
# load_player_profile returns and the reconcile loop uses, so a SAV profile
# could later be fed in directly. Distrito/Concelho are free-text on this form,
# so callers pass names (this renderer does no SAV lookup).
MOD1_FILL_MAPPING: dict[str, object] = {
  # ── Header: enrollment type / licence / club / association / season ──────────
  "tipo_inscricao":         _CheckGroup(by_int=_INSCRICAO_PDF_FIELD, by_name=_INSCRICAO_NAME_FIELD),
  "license":                _Text("nr_licenca"),
  "clube":                  _Text("Clube"),
  "associacao":             _Text("associacao"),
  "genero":                 _CheckGroup(by_int=_GENERO_PDF_FIELD, by_name=_GENERO_NAME_FIELD),
  "escalao":                _CheckGroup(by_name=_ESCALAO_NAME_FIELD),

  # ── Player identity ──────────────────────────────────────────────────────────
  "nome":                   _Text("Nome Completo"),
  "nacionalidade":          _Text("Nacionalidade"),
  "pais_nascimento":        _Text("País de Nascimento"),
  "nif":                    _Text("Nr Contribuinte"),
  "nasc":                   _Date("dn_dia", "dn_mes", "dn_ano"),
  "tipo":                   _CheckGroup(by_int=_ID_TYPE_PDF_FIELD),
  "numi":                   _Text("nr_identificacao"),
  "dataval":                _Date("val_dia", "val_mes", "val_ano"),

  # ── Contact / address ────────────────────────────────────────────────────────
  # The player's Telefone (landline) is intentionally always left blank — only
  # Telemóvel (`tele`) is captured — so there is deliberately no `telef` mapping.
  "email":                  _Text("Email"),
  "tele":                   _Text("Telemóvel"),
  "morada":                 _Text("Morada"),
  "localidade_txt":         _Text("Localidade"),
  "codpostal":              _Postal("codpostal", "cp3"),
  "distrito":               _Text("Distrito"),
  "concelho":               _Text("Concelho"),

  # ── Guardian ─────────────────────────────────────────────────────────────────
  "guardian_name":          _Text("nome_paternal"),
  "guardian_relation":      _CheckGroup(by_int=_GUARDIAN_RELATION_PDF_FIELD),
  "guardian_id_type":       _CheckGroup(by_int=_GUARDIAN_ID_TYPE_PDF_FIELD),
  "guardian_id_number":     _Text("paternal_id"),
  "guardian_id_expiry":     _Date("paternal_dia", "paternal_mes", "paternal_ano"),
  "guardian_phone":         _Text("paternal_telefone"),
  "guardian_email":         _Text("email_paternal"),

  # ── Consents / signature date ────────────────────────────────────────────────
  "consent_data":           _Consent("SIM", "NÃO"),
  "consent_communications": _Consent("SIM_2", "NÃO_2"),
  "consent_marketing":      _Consent("SIM_3", "NÃO_3"),
  "data_assinatura":        _Date("ass_dia", "ass_mes", "ass_ano"),
}


def _split_date(value: str) -> tuple[str, str, str] | None:
  """(dia, mes, ano) from an ISO (YYYY-MM-DD) or EU (DD-MM-YYYY) date string.

  Tolerant of ``/`` or ``.`` separators. Returns None when it can't be parsed.
  """
  parts = [p for p in re.split(r"[-/.]", value.strip()) if p]
  if len(parts) != 3:
    return None
  a, _, c = parts
  if len(a) == 4:          # YYYY-MM-DD
    ano, mes, dia = parts
  elif len(c) == 4:        # DD-MM-YYYY
    dia, mes, ano = parts
  else:
    return None
  return dia, mes, ano


def _split_postal(value: str) -> tuple[str, str]:
  """(first-4, last-3) from a Portuguese postal code like ``1234-567``.

  Tolerant of a missing ``-`` (falls back to a 4/3 digit split).
  """
  raw = value.strip()
  if "-" in raw:
    left, right = raw.split("-", 1)
    return left.strip(), right.strip()
  digits = "".join(ch for ch in raw if ch.isdigit())
  return digits[:4], digits[4:7]


def _is_blank(value: object) -> bool:
  return value is None or (isinstance(value, str) and not value.strip())


def _resolve_checkbox(spec: _CheckGroup, value: object) -> str | None:
  """Resolve a checkbox-group value (int code or name) to its PDF box name.

  Tries the value as an int against `by_int` first, then as a normalized name
  against `by_name` — so callers may pass ``1`` or ``"Masculino"``. Returns None
  when it matches neither.
  """
  if spec.by_int is not None:
    try:
      box = spec.by_int.get(int(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
      box = None
    if box is not None:
      return box
  if spec.by_name is not None:
    return spec.by_name.get(_norm_token(str(value)))
  return None


def _mod1_field_updates(values: dict) -> tuple[dict[str, str], set[str]]:
  """Resolve canonical `values` into (text_updates, checkbox_names).

  text_updates maps PDF text-field name → string (dates/postals split across
  sub-fields). checkbox_names is the set of PDF checkbox field names to tick.
  Blank values and unknown keys are skipped.
  """
  text: dict[str, str] = {}
  checks: set[str] = set()
  for key, spec in MOD1_FILL_MAPPING.items():
    value = values.get(key)
    if _is_blank(value):
      continue
    if isinstance(spec, _Text):
      text[spec.field] = str(value)
    elif isinstance(spec, _Date):
      split = _split_date(str(value))
      if split:
        text[spec.dia], text[spec.mes], text[spec.ano] = split
    elif isinstance(spec, _Postal):
      cod4, cp3 = _split_postal(str(value))
      if cod4:
        text[spec.cod4] = cod4
      if cp3:
        text[spec.cp3] = cp3
    elif isinstance(spec, _CheckGroup):
      box = _resolve_checkbox(spec, value)
      if box:
        checks.add(box)
    elif isinstance(spec, _Consent):
      checks.add(spec.yes if value else spec.no)
  return text, checks


def _tick_checkboxes(pdf_bytes: bytes, names: set[str]) -> bytes:
  """Tick the named checkboxes with pikepdf, preserving their original
  appearance streams.

  Sets the field ``/V`` **and** the on-page widget ``/AS`` to ``/On``. Both
  matter: Quartz/QuickLook renders off the field ``/V``, but interactive viewers
  (Preview, Acrobat) render off the widget annotation ``/AS`` — so a checkbox
  whose original structure had the widget in ``/Kids`` (1ª Inscrição /
  Revalidação / Estatuto FPB here) shows unchecked unless the widget ``/AS`` is
  synced. Done in pikepdf, not pypdf, because pypdf rewrites checkbox
  appearances and leaves that widget ``/AS`` out of sync.
  """
  on = pikepdf.Name("/On")
  with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
    for f in pdf.Root.AcroForm.Fields:
      if "/T" in f and str(f.T) in names:
        f.V = on
    for page in pdf.pages:
      for annot in page.get("/Annots", []):
        name = None
        if "/T" in annot:
          name = str(annot.T)
        elif "/Parent" in annot and "/T" in annot.Parent:
          name = str(annot.Parent.T)
        if name in names:
          annot.AS = on
    out = io.BytesIO()
    pdf.save(out)
    return out.getvalue()


# ── Validation (the form's "Preenchimento obrigatório" rules) ─────────────────
#
# The printed form marks every data field mandatory, with two conditionals:
# the Licença FPB only exists on a Revalidação (a brand-new player doing a 1ª
# Inscrição has none yet), and the guardian ("Detentor do Poder Paternal") block
# is filled only for a minor. render_mod1 enforces these by default.

_MOD1_MINOR_AGE = 18

_MOD1_CONSENT_KEYS: tuple[str, ...] = (
  "consent_data", "consent_communications", "consent_marketing",
)

# Always required (non-blank). Excludes: `license` (Revalidação-only, checked
# separately), `data_assinatura` + the three signatures (part of the
# hand-completed block), and the `guardian_*` block (minor-only, below). The
# player's Telefone (landline) is not a field at all — always left blank.
_MOD1_REQUIRED_CORE: tuple[str, ...] = (
  "tipo_inscricao", "clube", "associacao", "genero", "escalao",
  "nome", "nacionalidade", "pais_nascimento", "nif", "nasc",
  "tipo", "numi", "dataval",
  "email", "tele",
  "morada", "localidade_txt", "codpostal", "distrito", "concelho",
  *_MOD1_CONSENT_KEYS,
)

_MOD1_GUARDIAN_KEYS: tuple[str, ...] = (
  "guardian_name", "guardian_relation", "guardian_id_type",
  "guardian_id_number", "guardian_id_expiry", "guardian_phone", "guardian_email",
)


def _to_date(value: object) -> date | None:
  """Parse an ISO/EU date value to a date, or None when unparseable/blank."""
  parts = _split_date(str(value)) if not _is_blank(value) else None
  if not parts:
    return None
  dia, mes, ano = parts
  try:
    return date(int(ano), int(mes), int(dia))
  except ValueError:
    return None


def _age_on(born: date, ref: date) -> int:
  """Whole years from `born` to `ref` (handles the birthday-not-yet-reached case)."""
  return ref.year - born.year - ((ref.month, ref.day) < (born.month, born.day))


def _mod1_unusable_values(values: dict) -> list[str]:
  """Problems for present-but-unusable values (would silently render blank):
  checkbox options that don't resolve, dates that don't parse, bad postal codes.
  """
  problems: list[str] = []
  for key, spec in MOD1_FILL_MAPPING.items():
    value = values.get(key)
    if _is_blank(value):
      continue
    if isinstance(spec, _CheckGroup):
      if _resolve_checkbox(spec, value) is None:
        problems.append(f"{key}={value!r} is not a valid option")
    elif isinstance(spec, _Date):
      if _to_date(value) is None:
        problems.append(f"{key}={value!r} is not a valid date (use YYYY-MM-DD)")
    elif isinstance(spec, _Postal):
      cod4, cp3 = _split_postal(str(value))
      if not (cod4 and cp3):
        problems.append(f"{key}={value!r} is not a valid postal code (NNNN-NNN)")
  return problems


def validate_mod1_values(values: dict) -> list[str]:
  """Return a list of human-readable problems with `values`; empty when valid.

  Enforces the form's mandatory-fill rules: every player field is required; the
  Licença FPB is required only for a Revalidação (tipo_inscricao=2); and the
  guardian block is required in full when — and only when — the player is a
  minor, derived from `nasc` as of the signature date (`data_assinatura`, else
  today). Values that are present but unusable (bad option/date/postal) are
  flagged too. The three signatures and the signature date are never required —
  they belong to the hand-completed block.
  """
  problems: list[str] = []

  for key in _MOD1_REQUIRED_CORE:
    if key in _MOD1_CONSENT_KEYS:
      if not isinstance(values.get(key), bool):
        problems.append(f"{key} is required (true or false)")
    elif _is_blank(values.get(key)):
      problems.append(f"{key} is required")

  problems += _mod1_unusable_values(values)

  # Licença FPB — mandatory only for a Revalidação.
  insc = _resolve_checkbox(MOD1_FILL_MAPPING["tipo_inscricao"], values.get("tipo_inscricao"))
  if insc == "revalidacao" and _is_blank(values.get("license")):
    problems.append("license is required for a Revalidação")

  # Guardian block — required in full for a minor, forbidden for an adult.
  born = _to_date(values.get("nasc"))
  if born is not None:
    age = _age_on(born, _to_date(values.get("data_assinatura")) or date.today())
    if age < _MOD1_MINOR_AGE:
      missing = [k for k in _MOD1_GUARDIAN_KEYS if _is_blank(values.get(k))]
      if missing:
        problems.append(
          f"player is a minor (age {age}); the guardian block is mandatory — "
          f"missing: {', '.join(missing)}"
        )
    else:
      present = [k for k in _MOD1_GUARDIAN_KEYS if not _is_blank(values.get(k))]
      if present:
        problems.append(
          f"player is an adult (age {age}); the guardian block must be empty — "
          f"remove: {', '.join(present)}"
        )
  return problems


def render_mod1(
  values: dict,
  *,
  template_path: str | os.PathLike[str] | None = None,
  validate: bool = True,
  player_signature: bytes | str | os.PathLike[str] | None = None,
  guardian_signature: bytes | str | os.PathLike[str] | None = None,
  club_stamp: bytes | str | os.PathLike[str] | None = None,
) -> bytes:
  """Fill the blank Modelo 1 AcroForm from `values` and return the PDF bytes.

  `values` is keyed by the canonical enrollment field keys (see
  MOD1_FILL_MAPPING). Text fields take strings; checkbox groups (`tipo`,
  `guardian_id_type` = 1/2/3; `guardian_relation` = 1/2/3; `genero`, `escalao`,
  `tipo_inscricao` by int or name) select a box; `consent_*` take booleans;
  dates are ``YYYY-MM-DD``; distrito/concelho/nacionalidade are names.

  Unless `validate` is False, the form's mandatory-fill rules are enforced (see
  validate_mod1_values) and a ValueError listing every problem is raised when
  any fail: all player fields are required; the Licença FPB only for a
  Revalidação; and the guardian block in full only for a minor (empty otherwise).

  `player_signature`, `guardian_signature` and `club_stamp` are optional images
  (raw bytes or a path to a PNG/JPG). Each is overlaid on its printed area
  (there are no form fields there): omit them to get a clean form to sign/stamp
  offline; pass them to get the completed form. Any subset may be given.

  Text is filled with pypdf (which generates appearance streams so it renders
  and prints in every viewer, not only those that honour NeedAppearances);
  checkboxes are ticked with pikepdf (see _tick_checkboxes); signatures/stamp
  are composited with overlay_image_on_pdf. The template file is never mutated —
  a filled copy is returned.
  """
  if validate:
    problems = validate_mod1_values(values)
    if problems:
      raise ValueError(
        "Invalid Modelo 1 values:\n  - " + "\n  - ".join(problems)
      )
  path = (
    os.fspath(template_path) if template_path
    else (os.environ.get("MOD1_TEMPLATE_PATH") or str(_MOD1_TEMPLATE))
  )
  text_updates, checkbox_names = _mod1_field_updates(values)

  reader = PdfReader(path)
  writer = PdfWriter()
  writer.append(reader)
  if text_updates:
    writer.update_page_form_field_values(writer.pages[0], text_updates, auto_regenerate=False)
  out = io.BytesIO()
  writer.write(out)
  pdf_bytes = out.getvalue()

  if checkbox_names:
    pdf_bytes = _tick_checkboxes(pdf_bytes, checkbox_names)

  for image, rect in (
    (player_signature,   _PLAYER_SIGNATURE_RECT),
    (guardian_signature, _GUARDIAN_SIGNATURE_RECT),
    (club_stamp,         _CLUB_STAMP_RECT),
  ):
    image_bytes = _load_image_bytes(image)
    if image_bytes:
      pdf_bytes = overlay_image_on_pdf(pdf_bytes, image_bytes, rect=rect, page_index=0)
  return pdf_bytes
