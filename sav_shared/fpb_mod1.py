"""SAV-side mapping + reconciliation for FPB mod 1 forms.

sav-parsers is SAV2-agnostic — it returns ParsedField values from Document AI.
This module owns the translation from those entities into SAV2 add-player
kwargs and the reconciliation against a stored SAV profile.
"""
from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
  from sav_parsers import ParsedField

from .fields import (
  RECONCILE_READONLY as _RECONCILE_READONLY,
  RECONCILE_TEXT    as _RECONCILE_TEXT,
)
from .lookups import distrito_name, find_distrito_id, find_id_by_name


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
