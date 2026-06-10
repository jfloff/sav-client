"""Unit tests for sav_shared.enrollment helpers."""

import pytest

from sav_parsers.types import ParsedField
from sav_shared.enrollment import (
  PORTUGAL_NATIONALITY_ID,
  REGISTRATION_TYPE_SUBIDA,
  REQUIRED_PRIMEIRA_KWARGS,
  build_primeira_kwargs,
  build_primeira_preview_fields,
  compute_enrollment_checklist,
  validate_subida_combo,
)


@pytest.mark.parametrize("reg_type", [1, 2])
def test_validate_subida_combo_allows_inline_on_type_1_and_2(reg_type):
  # Inline subida rides on a 1ª Inscrição (1) or Revalidação (2).
  validate_subida_combo(reg_type, inline_subida=True)


@pytest.mark.parametrize("reg_type", [1, 2, REGISTRATION_TYPE_SUBIDA])
def test_validate_subida_combo_allows_no_rider(reg_type):
  # Without an inline rider, every batch type is fine (incl. standalone subida).
  validate_subida_combo(reg_type, inline_subida=False)


def test_validate_subida_combo_rejects_inline_on_standalone_subida():
  # A standalone Subida batch IS a subida — an inline rider on top is contradictory.
  with pytest.raises(ValueError, match="inline_subida is only valid"):
    validate_subida_combo(REGISTRATION_TYPE_SUBIDA, inline_subida=True)


# ─── 1ª Inscrição mapping helpers ───────────────────────────────────────────

def _parsed_with(**values) -> dict:
  """Build a parsed-fields dict where each kwarg becomes a ParsedField at
  confidence 0.95 — high enough that the preview status stays "ocr"."""
  return {k: ParsedField(value=v, confidence=0.95) for k, v in values.items()}


def test_build_primeira_kwargs_extracts_type1_demographics():
  parsed = _parsed_with(
    nome_completo="João Loff",
    data_nascimento="2020-09-26",
    nif="277544319",
    genero_masculino=True,
    num_doc_identificacao="12345699",
    validade_doc="2029-09-26",
    tipo_doc_cc=True,
    email_jogador="x@y.pt",
    morada="Praceta",
    codigo_postal="1300-536",
    localidade="Lisboa",
  )
  kwargs = build_primeira_kwargs(parsed)
  assert kwargs["name"] == "João Loff"
  assert kwargs["birth_date"] == "2020-09-26"
  assert kwargs["nif"] == "277544319"
  assert kwargs["gender_id"] == 1  # genero_masculino → default masculino
  assert kwargs["id_type"] == 1    # tipo_doc_cc → 1
  assert kwargs["id_number"] == "12345699"


def test_build_primeira_kwargs_picks_feminino_when_marked():
  parsed = _parsed_with(genero_feminino=True, nome_completo="Maria")
  assert build_primeira_kwargs(parsed)["gender_id"] == 2


def test_preview_fields_flag_missing_required_as_needs_review():
  parsed = _parsed_with(nome_completo="João", genero_masculino=True)
  # birth_date is required but absent → needs_review.
  kwargs = build_primeira_kwargs(parsed)
  fields, needs_review = build_primeira_preview_fields(parsed, kwargs)
  assert "birth_date" in needs_review
  bd_row = next(f for f in fields if f["kwarg"] == "birth_date")
  assert bd_row["status"] == "needs_review"
  assert bd_row["final_value"] is None


def test_preview_fields_flag_low_confidence_as_needs_review():
  # OCR'd name is present but with low confidence — needs human review.
  parsed = {
    "nome_completo": ParsedField(value="J. Loff", confidence=0.40),
    "data_nascimento": ParsedField(value="2020-09-26", confidence=0.95),
    "nif": ParsedField(value="277544319", confidence=0.95),
    "genero_masculino": ParsedField(value=True, confidence=0.95),
  }
  kwargs = build_primeira_kwargs(parsed)
  fields, needs_review = build_primeira_preview_fields(parsed, kwargs)
  assert "name" in needs_review
  name_row = next(f for f in fields if f["kwarg"] == "name")
  assert name_row["status"] == "needs_review"
  assert name_row["confidence"] == 0.40


def test_preview_fields_cover_every_required_kwarg():
  # Even with empty parsed, every required kwarg must appear in the preview
  # so the LLM can see what it needs to supply via field_overrides.
  parsed = {}
  kwargs = build_primeira_kwargs(parsed)
  fields, needs_review = build_primeira_preview_fields(parsed, kwargs)
  shown_kwargs = {f["kwarg"] for f in fields}
  for required in REQUIRED_PRIMEIRA_KWARGS:
    assert required in shown_kwargs, (
      f"required kwarg {required!r} missing from preview rows"
    )


# ─── compute_enrollment_checklist ──────────────────────────────────────────


@pytest.mark.parametrize("reg_type", [1, 2])
def test_checklist_portuguese_satisfied_when_mod1_and_em_uploaded(reg_type):
  result = compute_enrollment_checklist(
    reg_type, PORTUGAL_NATIONALITY_ID,
    ["fpb_modelo_1", "exame_medico"],
  )
  assert result["scenario"] == "portuguese"
  assert result["missing"] == []
  required = {row["doc_type"]: row for row in result["required"]}
  assert required["fpb_modelo_1"]["satisfied"]
  assert required["exame_medico"]["satisfied"]
  # mod4 is optional (subida rider), shown but not in missing.
  optional = {row["doc_type"]: row for row in result["optional"]}
  assert optional["fpb_modelo_4"]["found_count"] == 0


@pytest.mark.parametrize("reg_type", [1, 2])
def test_checklist_portuguese_flags_missing_exame_medico(reg_type):
  result = compute_enrollment_checklist(
    reg_type, PORTUGAL_NATIONALITY_ID, ["fpb_modelo_1"],
  )
  assert result["missing"] == ["exame_medico"]


@pytest.mark.parametrize("reg_type", [1, 2])
def test_checklist_foreign_born_lists_all_extras(reg_type):
  # No docs uploaded — every required entry must show up missing.
  result = compute_enrollment_checklist(reg_type, nacional_id=200, uploaded_doc_types=[])
  assert result["scenario"] == "foreign_born"
  assert "fpb_modelo_1" in result["missing"]
  assert "exame_medico" in result["missing"]
  assert "atestado_residencia" in result["missing"]
  assert "certidao_matricula" in result["missing"]
  # documento_identificacao needs 2 — formatted with count detail.
  doc_id_missing = next(m for m in result["missing"] if m.startswith("documento_identificacao"))
  assert "need 2" in doc_id_missing and "found 0" in doc_id_missing


def test_checklist_foreign_born_documento_identificacao_requires_two():
  # One uploaded is still insufficient — need ≥ 2 (passaporte + título).
  result = compute_enrollment_checklist(
    1, nacional_id=200,
    uploaded_doc_types=[
      "fpb_modelo_1", "exame_medico", "atestado_residencia",
      "certidao_matricula", "documento_identificacao",
    ],
  )
  doc_id_row = next(r for r in result["required"] if r["doc_type"] == "documento_identificacao")
  assert doc_id_row["found_count"] == 1
  assert not doc_id_row["satisfied"]
  assert any("documento_identificacao" in m for m in result["missing"])


def test_checklist_foreign_born_two_documento_identificacao_satisfies():
  result = compute_enrollment_checklist(
    1, nacional_id=200,
    uploaded_doc_types=[
      "fpb_modelo_1", "exame_medico", "atestado_residencia",
      "certidao_matricula", "documento_identificacao",
      "documento_identificacao",
    ],
  )
  assert result["missing"] == []


def test_checklist_subida_standalone_only_needs_mod4():
  result = compute_enrollment_checklist(
    REGISTRATION_TYPE_SUBIDA, nacional_id=PORTUGAL_NATIONALITY_ID,
    uploaded_doc_types=["fpb_modelo_4"],
  )
  assert result["scenario"] == "subida_standalone"
  assert result["missing"] == []
  assert [r["doc_type"] for r in result["required"]] == ["fpb_modelo_4"]
  assert result["optional"] == []


def test_checklist_subida_standalone_flags_missing_mod4():
  result = compute_enrollment_checklist(
    REGISTRATION_TYPE_SUBIDA, nacional_id=PORTUGAL_NATIONALITY_ID,
    uploaded_doc_types=[],
  )
  assert result["missing"] == ["fpb_modelo_4"]


def test_checklist_transferencia_returns_none():
  # reg_type=3 (Transferência) is not handled; callers should surface that.
  assert compute_enrollment_checklist(3, PORTUGAL_NATIONALITY_ID, []) is None


def test_checklist_ignores_unmapped_doc_types():
  # SAV2 doc types with no sav-parsers mapping come through as None — should
  # not crash, and should not satisfy any requirement.
  result = compute_enrollment_checklist(
    1, PORTUGAL_NATIONALITY_ID,
    uploaded_doc_types=[None, "fpb_modelo_1", None],
  )
  assert any(
    r["doc_type"] == "exame_medico" and not r["satisfied"]
    for r in result["required"]
  )


def test_checklist_unknown_nationality_treated_as_foreign_born():
  # Defensive: nacional missing/unparseable → treat as foreign_born (asks for
  # more docs; safer error direction than under-requesting).
  result = compute_enrollment_checklist(1, nacional_id=None, uploaded_doc_types=[])
  assert result["scenario"] == "foreign_born"
