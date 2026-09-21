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
  exam_date_bbox: Any = None
  birth_date: str | None = None
  raw_birth_date: str | None = None
  birth_date_confidence: float | None = None
  birth_date_bbox: Any = None
  athlete_name: str | None = None
  athlete_name_confidence: float | None = None
  athlete_name_bbox: Any = None
  doc_number: str | None = None
  doc_number_confidence: float | None = None
  doc_number_bbox: Any = None
  exam_number: str | None = None
  exam_number_confidence: float | None = None
  exam_number_bbox: Any = None
  doctor_validation_present: bool | None = None
  doctor_validation_present_confidence: float | None = None
  doctor_validation_present_bbox: Any = None


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


def _date_triple(
  parsed: dict[str, Any], key: str,
) -> tuple[str | None, str | None, float | None]:
  """Return (iso, raw, confidence) for a date entity.

  ``iso`` is the value only when it is strictly YYYY-MM-DD; anything else the
  OCR read lands in ``raw`` instead, so an unusable date stays distinguishable
  from an absent one. exam_date and birth_date share this so they cannot drift
  apart. Confidence is reported even when the value is empty.
  """
  field = parsed.get(key)
  if field is None:
    return None, None, None
  confidence = getattr(field, "confidence", None)
  raw_value = getattr(field, "value", None)
  value = _strict_iso_date(raw_value)
  raw = None
  if raw_value not in (None, "") and value is None:
    raw = str(raw_value).strip()
  return value, raw, confidence


def _plain_pair(parsed: dict[str, Any], key: str) -> tuple[Any, float | None]:
  """Return (value, confidence) for an entity that is passed through verbatim.

  No validation and no normalization: doc_number is printed as
  ``NNNNNNNN N ZXN`` on a PT card and as a passport number for a foreign
  athlete, and exam_number (``2218/2025``) is a serial, not a date.
  """
  field = parsed.get(key)
  if field is None:
    return None, None
  return getattr(field, "value", None), getattr(field, "confidence", None)


def _bbox(parsed: dict[str, Any], key: str) -> Any:
  """Return the parser's BBox for an entity, or None when it has none."""
  field = parsed.get(key)
  return getattr(field, "bbox", None) if field is not None else None


def bbox_to_json(bbox: Any) -> dict[str, Any] | None:
  """Serialize a parser bbox into JSON-compatible primitives."""
  if bbox is None:
    return None
  return {
    "page": int(getattr(bbox, "page")),
    "vertices": [
      [float(vertex[0]), float(vertex[1])]
      for vertex in getattr(bbox, "vertices")
    ],
  }


def extract_medical_exam_info(parsed: dict[str, Any]) -> MedicalExamInfo:
  """Normalize parse_em output into step-3-friendly fields.

  Every entity is optional: the date-provided fast path in the MCP server
  synthesizes a ``parsed`` holding nothing but ``exam_date``, so each lookup
  must tolerate its key being absent entirely.
  """
  exam_date, raw_exam_date, exam_date_confidence = _date_triple(parsed, "exam_date")
  birth_date, raw_birth_date, birth_date_confidence = _date_triple(parsed, "birth_date")
  athlete_name, athlete_name_confidence = _plain_pair(parsed, "athlete_name")
  doc_number, doc_number_confidence = _plain_pair(parsed, "doc_number")
  exam_number, exam_number_confidence = _plain_pair(parsed, "exam_number")
  doctor_validation_present, doctor_validation_present_confidence = _plain_pair(
    parsed, "doctor_validation_present"
  )
  return MedicalExamInfo(
    exam_date=exam_date,
    raw_exam_date=raw_exam_date,
    exam_date_confidence=exam_date_confidence,
    exam_date_bbox=_bbox(parsed, "exam_date"),
    birth_date=birth_date,
    raw_birth_date=raw_birth_date,
    birth_date_confidence=birth_date_confidence,
    birth_date_bbox=_bbox(parsed, "birth_date"),
    athlete_name=athlete_name,
    athlete_name_confidence=athlete_name_confidence,
    athlete_name_bbox=_bbox(parsed, "athlete_name"),
    doc_number=doc_number,
    doc_number_confidence=doc_number_confidence,
    doc_number_bbox=_bbox(parsed, "doc_number"),
    exam_number=exam_number,
    exam_number_confidence=exam_number_confidence,
    exam_number_bbox=_bbox(parsed, "exam_number"),
    doctor_validation_present=doctor_validation_present,
    doctor_validation_present_confidence=doctor_validation_present_confidence,
    doctor_validation_present_bbox=_bbox(parsed, "doctor_validation_present"),
  )
