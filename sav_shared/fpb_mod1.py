"""SAV-side mapping + reconciliation for FPB mod 1 forms.

sav-parsers is SAV2-agnostic — it returns ParsedField values from Document AI.
This module owns the translation from those entities into SAV2 add-player
kwargs and the reconciliation against a stored SAV profile.
"""
from __future__ import annotations

import difflib
import unicodedata
from dataclasses import dataclass
from typing import Any

from sav_parsers import ParsedField


# tipo_identificacao SAV2 int values
_ID_TYPE: dict[str, int] = {
  "tipo_doc_cc":         1,  # Cartão de Cidadão
  "tipo_doc_passaporte": 2,
  "tipo_doc_outro":      3,  # defaults to Título de residência
}

# tipoRegulacao SAV2 int values
_GUARDIAN_RELATION: dict[str, int] = {
  "parentesco_encarregado_pai":   1,
  "parentesco_encarregado_mae":   2,
  "parentesco_encarregado_tutor": 3,
}

# OCR entity → (sav_profile key, sav kwarg) for text reconciliation
# sav_profile is the merged op=35 record + step2 address prefill
_RECONCILE_TEXT: list[tuple[str, str, str]] = [
  ("num_doc_identificacao", "numi",          "id_number"),
  ("validade_doc",          "dataval",       "id_expiry"),
  ("telemovel",             "tele",          "telemovel"),
  ("morada",                "morada",        "morada"),
  ("localidade",            "localidade_txt","localidade_txt"),
  ("codigo_postal",         "cod_postal",    "cod_postal"),
]

# Reverse mapping for callers building corrections to pass to close_processing.
KWARG_TO_ENTITY: dict[str, str] = {kwarg: entity for entity, _, kwarg in _RECONCILE_TEXT}

_CONFIDENCE_LOW  = 0.60
_SIMILARITY_KEEP = 0.80


@dataclass
class ReconcileResult:
  kwargs: dict[str, Any]
  updated: dict[str, tuple]   # kwarg → (sav_value, ocr_value)
  kept: dict[str, tuple]      # kwarg → (sav_value, ocr_value, similarity)
  needs_review: list[str]     # kwarg names: low confidence, no reliable SAV match
  mismatches: list[dict]      # {entity, ocr, sav, confidence} for retraining


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


def fpb_mod1_to_sav_kwargs(fields: dict[str, ParsedField]) -> dict:
  """Map parse_fpb_mod1 output to add_player_to_registration_batch kwargs.

  Returns a dict that includes 'license' (pop it out before **-spreading):
    kwargs = fpb_mod1_to_sav_kwargs(fields)
    license = kwargs.pop("license")
    client.add_player_to_registration_batch(batch_id, license, **kwargs)
  """
  def val(key: str):
    f = fields.get(key)
    return f.value if f else None

  if not val("tipo_inscricao_revalidacao"):
    raise ValueError("not a revalidação form")

  kwargs: dict = {
    "license":                val("licenca_fpb"),
    "id_type":                _pick_checked(fields, _ID_TYPE),
    "id_number":              val("num_doc_identificacao"),
    "id_expiry":              val("validade_doc"),
    "cod_postal":             val("codigo_postal"),
    "morada":                 val("morada"),
    "localidade_txt":         val("localidade"),
    "telemovel":              val("telemovel"),
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
) -> ReconcileResult:
  """Compare OCR fields against the player's current SAV profile.

  sav_profile is the merged op=35 record + step2 address prefill dict.
  Fields with OCR ≈ SAV (similarity >= 0.80) silently keep the SAV value.
  When OCR diverges from SAV: high confidence (>= 0.60) auto-accepts OCR
  (updated); low confidence asks the user (needs_review).
  Genuine divergences are collected in mismatches for retraining.
  """
  base = fpb_mod1_to_sav_kwargs(parsed)

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
        updated[kwarg] = (sav_val, ocr_val)
        mismatches.append({
          "entity": entity, "ocr": ocr_val,
          "sav": sav_val, "confidence": conf,
        })
    else:
      if conf < _CONFIDENCE_LOW:
        needs_review.append(kwarg)

  return ReconcileResult(
    kwargs=base,
    updated=updated,
    kept=kept,
    needs_review=needs_review,
    mismatches=mismatches,
  )
