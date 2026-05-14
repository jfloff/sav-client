"""Static SAV2 ID ↔ name lookups, plus a generic name-to-id resolver.

These are the small, stable maps the SAV2 wizard exposes as integer IDs but
that callers (and OCR forms) deal in human names. Keeping them in one place
means callers can render a numeric SAV value as text and resolve a piece of
OCR text back to its SAV ID without each consumer writing the table again.

When you add a new lookup here:
  - Use a simple dict[int, str] keyed by the SAV-side integer.
  - Strip trailing whitespace from values up front so callers don't have to.
  - Provide an id → name accessor only if the lookup needs special handling
    (most don't; callers just do `MAP.get(id, "")`).
"""
from __future__ import annotations

from sav_parsers.types import DocType

from .text import normalise_text


# ── Generic ────────────────────────────────────────────────────────────────────

def find_id_by_name(name: str | None, mapping: dict[int, str]) -> int | None:
  """Resolve a name (typically OCR text) to its SAV2 integer ID.

  Accent- and case-insensitive. Returns None when no entry matches; callers
  should treat that as "OCR did not yield a usable value" and fall back to
  whatever SAV had stored.
  """
  if not name or not mapping:
    return None
  needle = normalise_text(name)
  if not needle:
    return None
  for key, value in mapping.items():
    if normalise_text(value) == needle:
      return key
  return None


# ── Distritos (SAV2 step-2 dropdown) ───────────────────────────────────────────

DISTRITOS: dict[int, str] = {
   1: "Aveiro",
   2: "Beja",
   3: "Braga",
   4: "Bragança",
   5: "Castelo Branco",
   6: "Coimbra",
   7: "Évora",
   8: "Faro",
   9: "Guarda",
  10: "Leiria",
  11: "Lisboa",
  12: "Portalegre",
  13: "Porto",
  14: "Santarém",
  15: "Setúbal",
  16: "Viana do Castelo",
  17: "Vila Real",
  18: "Viseu",
  31: "Ilha da Madeira",
  32: "Ilha de Porto Santo",
  41: "Ilha de Santa Maria",
  42: "Ilha de São Miguel",
  43: "Ilha Terceira",
  44: "Ilha da Graciosa",
  45: "Ilha de São Jorge",
  46: "Ilha do Pico",
  47: "Ilha do Faial",
  48: "Ilha das Flores",
  49: "Ilha do Corvo",
  50: "Horta",
}


def find_distrito_id(name: str | None) -> int | None:
  """OCR distrito text → SAV2 distrito ID, accent-/case-insensitive."""
  return find_id_by_name(name, DISTRITOS)


def distrito_name(distrito_id: int | str | None) -> str:
  """SAV2 distrito ID → name. Empty string for missing/unknown."""
  if distrito_id in (None, ""):
    return ""
  try:
    return DISTRITOS.get(int(distrito_id), "")
  except (ValueError, TypeError):
    return ""


# ── Player registration types ──────────────────────────────────────────────────

REGISTRATION_TYPE_LABELS: dict[int, str] = {
  1: "1ª Inscrição",
  2: "Revalidação",
  3: "Transferência",
  4: "Subida",
}


# ── ID document types (tipo_identificacao) ─────────────────────────────────────

ID_TYPES: dict[int, str] = {
  1: "Cartão de Cidadão",
  2: "Passaporte",
  3: "Título de Residência",
}


# ── Guardian relation (tipoRegulacao) ──────────────────────────────────────────

GUARDIAN_RELATIONS: dict[int, str] = {
  1: "Pai",
  2: "Mãe",
  3: "Tutor",
}


# ── Gender (genero) ────────────────────────────────────────────────────────────

GENERO: dict[int, str] = {
  1: "Masculino",
  2: "Feminino",
}


# ── Document types ─────────────────────────────────────────────────────────────

DOC_TYPE_CHOICES: tuple[str, ...] = tuple(doc_type.value for doc_type in DocType)

_DOC_TYPE_TO_TIPO_DOC: dict[DocType, int] = {
  DocType.FPB_MOD1: 1,
  DocType.EM: 2,
  DocType.FPB_MOD4: 6,
}
_TIPO_DOC_TO_DOC_TYPE: dict[int, DocType] = {
  tipo_doc: doc_type
  for doc_type, tipo_doc in _DOC_TYPE_TO_TIPO_DOC.items()
}


def normalize_doc_type(value: DocType | str) -> DocType:
  """Normalize a CLI/MCP doc-type input to the canonical sav-parsers enum."""
  if isinstance(value, DocType):
    return value
  if not isinstance(value, str):
    raise ValueError(
      f"Unknown doc_type {value!r}. Use one of: {', '.join(DOC_TYPE_CHOICES)}."
    )

  normalized = value.strip().lower()
  try:
    return DocType(normalized)
  except ValueError as exc:
    raise ValueError(
      f"Unknown doc_type {value!r}. Use one of: {', '.join(DOC_TYPE_CHOICES)}."
    ) from exc


def doc_type_to_tipo_doc(doc_type: DocType | str) -> int:
  """Translate a sav-parsers doc type to the SAV2 upload modal integer."""
  normalized = normalize_doc_type(doc_type)
  if normalized not in _DOC_TYPE_TO_TIPO_DOC:
    raise ValueError(
      f"Document type {normalized.value!r} is recognized by sav-parsers "
      "but has no SAV2 tipo_doc mapping yet."
    )
  return _DOC_TYPE_TO_TIPO_DOC[normalized]


def tipo_doc_to_doc_type(tipo_doc: int) -> DocType | None:
  """Reverse-map a SAV2 tipo_doc integer back to a sav-parsers doc type."""
  return _TIPO_DOC_TO_DOC_TYPE.get(tipo_doc)
