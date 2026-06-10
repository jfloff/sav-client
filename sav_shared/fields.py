"""Single source of truth for the FPB enrollment field surface.

Each enrollment field has up to four lives:
  - the player's SAV profile (op=2 player form, parsed in sav_client)
  - the OCR'd FPB form (parsed in sav_parsers)
  - the SAV submit kwargs (add_player_to_registration_batch)
  - the human label used in the CLI/MCP summary table

`FieldDef` ties those four together. The constants below
(`_RECONCILE_TEXT`, `_RECONCILE_READONLY`, `ENROLLMENT_FIELD_META`,
`KWARG_TO_ENTITY`) are derived from `FIELDS` so adding a field means
editing one row.

Note: sav_client.py's `load_player_profile` parses the op=2 HTML
into the canonical key shape and intentionally keeps a *small* duplicated
mapping there — adding a profile-backed field requires editing FIELDS
(here) and the parser (there). The duplication is documented; the
alternative was a sav_client → sav_shared dependency we don't want.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FieldDef:
  key: str
  """Canonical sav_profile dict key — where the raw value lives."""

  label: str
  """Human-readable Portuguese label for display."""

  ocr_entity: str | None = None
  """FPB Document AI entity name. None = no OCR source."""

  sav_kwarg: str | None = None
  """add_player_to_registration_batch kwarg. None = read-only / display-only."""

  profile_html: tuple[str, str] | None = None
  """('input'|'select', html_id) tuple for the op=2 form parser. None = not in op=2."""

  reconcile_key: str | None = None
  """sav_profile key the reconcile loop compares against. Defaults to `key`.
  Differs only when SAV stores an int needing translation to comparable text —
  distrito (id) → distrito_name (resolved via lookup table)."""

  is_read_only: bool = False
  """OCR cross-checked against SAV but never submitted. Mismatches feed
  retrain_corrections at close_processing time instead of overriding SAV."""

  is_bool: bool = False
  """Boolean OCR (consents). Skipped by the text reconcile loop; the value
  flows through fpb_mod1_to_sav_kwargs unchanged."""

  is_submit_only: bool = False
  """OCR + submit but never compared against SAV. Guardian fields are
  per-enrollment (the player's profile has no guardian record), so the
  reconcile loop has nothing to compare against. The OCR value flows
  straight into the submit kwargs."""


# Order here drives the order of rows in the submission summary table.
FIELDS: list[FieldDef] = [
  # ── Read-only personal (cross-checked, never submitted) ─────────────────
  FieldDef(
    key="nif", label="NIF",
    ocr_entity="nif",
    profile_html=("input", "nif"),
    is_read_only=True,
  ),
  FieldDef(
    key="nasc", label="Data Nascimento",
    ocr_entity="data_nascimento",
    profile_html=("input", "datenasc"),
    is_read_only=True,
  ),

  # ── Identity document ────────────────────────────────────────────────────
  FieldDef(
    key="tipo", label="Tipo de Documento",
    sav_kwarg="id_type",
    profile_html=("select", "tipoi"),
    # OCR side is a checkbox group (tipo_doc_cc/passaporte/outro);
    # handled bespoke in fpb_mod1_to_sav_kwargs._pick_checked.
  ),
  FieldDef(
    key="numi", label="Nº Identificação",
    ocr_entity="num_doc_identificacao", sav_kwarg="id_number",
    profile_html=("input", "numid"),
  ),
  FieldDef(
    key="dataval", label="Validade Documento",
    ocr_entity="validade_doc", sav_kwarg="id_expiry",
    profile_html=("input", "dateval"),
  ),

  # ── Contact ──────────────────────────────────────────────────────────────
  FieldDef(
    key="email", label="Email",
    ocr_entity="email_jogador", sav_kwarg="email",
    profile_html=("input", "email"),
  ),
  FieldDef(
    key="tele", label="Telemóvel",
    ocr_entity="telemovel", sav_kwarg="telemovel",
    profile_html=("input", "telem"),
  ),
  FieldDef(
    key="telef", label="Telefone",
    ocr_entity="telefone", sav_kwarg="telefone",
    profile_html=("input", "telefo"),
  ),

  # ── Address ──────────────────────────────────────────────────────────────
  FieldDef(
    key="morada", label="Morada",
    ocr_entity="morada", sav_kwarg="morada",
    profile_html=("input", "morada"),
  ),
  FieldDef(
    key="localidade_txt", label="Localidade",
    ocr_entity="localidade", sav_kwarg="localidade_txt",
    profile_html=("input", "localidadestring"),
  ),
  FieldDef(
    key="codpostal", label="Código Postal",
    ocr_entity="codigo_postal", sav_kwarg="cod_postal",
    profile_html=("input", "cod2"),
  ),
  FieldDef(
    key="distrito", label="Distrito",
    ocr_entity="distrito", sav_kwarg="distrito_id",
    profile_html=("select", "distrito"),
    reconcile_key="distrito_name",
  ),
  FieldDef(
    key="concelho", label="Concelho",
    ocr_entity="concelho", sav_kwarg="concelho_id",
    profile_html=("select", "concelho"),
    reconcile_key="concelho_name",
  ),

  # ── Submit-only OCR (1:1 entity → kwarg, not in profile) ────────────────
  FieldDef(
    key="guardian_name", label="Nome Encarregado",
    ocr_entity="nome_encarregado", sav_kwarg="guardian_name",
    is_submit_only=True,
  ),
  FieldDef(
    key="guardian_relation", label="Relação Encarregado",
    sav_kwarg="guardian_relation",
    # OCR side is a checkbox group (parentesco_encarregado_pai/mae/tutor);
    # handled bespoke in fpb_mod1_to_sav_kwargs._pick_checked.
  ),
  FieldDef(
    key="guardian_phone", label="Telefone Encarregado",
    ocr_entity="telefone_encarregado", sav_kwarg="guardian_phone",
    is_submit_only=True,
  ),
  FieldDef(
    key="guardian_email", label="Email Encarregado",
    ocr_entity="email_encarregado", sav_kwarg="guardian_email",
    is_submit_only=True,
  ),

  # ── Consents (OCR booleans → submit booleans) ───────────────────────────
  FieldDef(
    key="consent_data", label="Consentimento Dados",
    ocr_entity="consentimento_dados", sav_kwarg="consent_data",
    is_bool=True,
  ),
  FieldDef(
    key="consent_communications", label="Consentimento Comunicações",
    ocr_entity="consentimento_comunicacoes", sav_kwarg="consent_communications",
    is_bool=True,
  ),
  FieldDef(
    key="consent_marketing", label="Consentimento Marketing",
    ocr_entity="consentimento_marketing", sav_kwarg="consent_marketing",
    is_bool=True,
  ),
]


# ── Derivations ──────────────────────────────────────────────────────────────

# (ocr_entity, sav_profile_key, sav_kwarg) — text fields with full reconcile.
RECONCILE_TEXT: list[tuple[str, str, str]] = [
  (f.ocr_entity, f.reconcile_key or f.key, f.sav_kwarg)
  for f in FIELDS
  if f.ocr_entity and f.sav_kwarg and not f.is_bool and not f.is_submit_only
]

# (ocr_entity, sav_profile_key) — fields cross-checked but not submitted.
RECONCILE_READONLY: list[tuple[str, str]] = [
  (f.ocr_entity, f.key) for f in FIELDS if f.is_read_only and f.ocr_entity
]

# kwarg → ocr_entity, for callers building corrections to pass to close_processing.
KWARG_TO_ENTITY: dict[str, str] = {kwarg: ent for ent, _, kwarg in RECONCILE_TEXT}


def _meta_key(f: FieldDef) -> str | None:
  """Hybrid keying: read-only fields key by OCR entity (matches result.ocr's
  entity-keyed read-only entries); submittable fields key by sav_kwarg."""
  if f.is_read_only and f.ocr_entity:
    return f.ocr_entity
  return f.sav_kwarg


def _meta_sav_key(f: FieldDef) -> str:
  """Where in sav_profile to look up the SAV-side display value. Empty when
  the field has no SAV profile representation (consents, guardian fields)."""
  return (f.reconcile_key or f.key) if f.profile_html else ""


# meta_key → (label, sav_profile key). Drives the summary table in cli/mcp.
ENROLLMENT_FIELD_META: dict[str, tuple[str, str]] = {
  _meta_key(f): (f.label, _meta_sav_key(f))
  for f in FIELDS
  if _meta_key(f)
}

# (kind, html_id, canonical_key) — a hint at what sav_client's parser handles.
# Kept here for documentation; the parser itself maintains its own copy.
PROFILE_HTML_FIELDS: list[tuple[str, str, str]] = [
  (kind, html_id, f.key)
  for f in FIELDS
  if f.profile_html
  for kind, html_id in [f.profile_html]
]
