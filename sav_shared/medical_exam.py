"""Shared helpers for exame_medico OCR results."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as _date
from typing import Any


@dataclass(frozen=True)
class MedicalExamInfo:
  exam_date: str | None
  raw_exam_date: str | None
  exam_date_confidence: float | None


def _strict_iso_date(value: Any) -> str | None:
  """Return value only when it is a valid, strictly YYYY-MM-DD date string.

  Uses fromisoformat for calendar validation (rejects impossible dates like
  2026-99-99) then round-trips through isoformat() to reject non-YYYY-MM-DD
  forms that Python 3.11+ fromisoformat accepts (e.g. "20260513").
  """
  if value in (None, ""):
    return None
  text = str(value).strip()
  try:
    canonical = _date.fromisoformat(text).isoformat()
  except ValueError:
    return None
  return canonical if canonical == text else None


def extract_medical_exam_info(parsed: dict[str, Any]) -> MedicalExamInfo:
  """Normalize parse_em output into step-3-friendly fields."""
  exam_field = parsed.get("exam_date")
  exam_date = None
  raw_exam_date = None
  confidence = None
  if exam_field is not None:
    confidence = getattr(exam_field, "confidence", None)
    raw_value = getattr(exam_field, "value", None)
    exam_date = _strict_iso_date(raw_value)
    if raw_value not in (None, "") and exam_date is None:
      raw_exam_date = str(raw_value).strip()

  # Doctor's validation/stamp is assumed present on every uploaded exam PDF,
  # so it is neither read nor surfaced.
  return MedicalExamInfo(
    exam_date=exam_date,
    raw_exam_date=raw_exam_date,
    exam_date_confidence=confidence,
  )
