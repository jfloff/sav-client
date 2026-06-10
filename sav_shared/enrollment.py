"""Shared enrollment workflow helpers used by CLI and MCP."""

from __future__ import annotations

import logging
import re
from typing import Any

from sav_client.exceptions import SavError

from .fields import ENROLLMENT_FIELD_META, KWARG_TO_ENTITY
from .text import normalise_text

logger = logging.getLogger(__name__)

# Type-1 wizard required kwargs (mandatory for the SAV2 1ª Inscrição submit).
# Optional ones (telefone, nome_pai/mae, country/naturalidade overrides) stay
# off the list; the wizard defaults them.
REQUIRED_PRIMEIRA_KWARGS: tuple[str, ...] = (
  "name", "birth_date", "gender_id", "nif",
  "id_type", "id_number", "id_expiry", "email",
  "morada", "cod_postal", "distrito_id", "concelho_id",
)

# kwarg → OCR entity used to look up confidence when the kwarg isn't in
# KWARG_TO_ENTITY (those are reconciled-text fields only). For type-1 we also
# care about read-only fields (nif, birth_date) and checkbox-group fields
# (gender_id, id_type) — flagging low-confidence reads as needs_review.
_PRIMEIRA_KWARG_OCR_ENTITY: dict[str, str | tuple[str, ...]] = {
  "name": "nome_completo",
  "birth_date": "data_nascimento",
  "nif": "nif",
  "gender_id": ("genero_feminino", "genero_masculino"),
  "id_type": ("tipo_doc_cc", "tipo_doc_passaporte", "tipo_doc_outro"),
}


_DEFAULT_GUARDIAN_FIELDS = [
  "guardian_name", "guardian_relation", "guardian_phone", "guardian_email",
]

# Batch type for a standalone Subida de escalão batch (SAV `newGuia(1,4)`),
# distinct from the inline promote-on-enroll rider that can ride on a 1ª
# Inscrição (1) or Revalidação (2). See validate_subida_combo.
REGISTRATION_TYPE_SUBIDA = 4


def validate_subida_combo(reg_type: int, inline_subida: bool) -> None:
  """Reject contradictory enrollment-type combinations.

  "Subida de escalão" is two different operations:
    * inline_subida — promote the player *right away* while doing a 1ª Inscrição
      or Revalidação (op=21 escalaosubida on a type-1/2 batch).
    * a standalone Subida batch (reg_type 4) — its own batch, which IS a subida.

  An inline rider on top of a standalone Subida batch is contradictory, so we
  forbid it at the tool boundary rather than let an LLM build an invalid state.
  """
  if inline_subida and reg_type not in (1, 2):
    raise ValueError(
      f"inline_subida is only valid for 1ª Inscrição (1) or Revalidação (2) "
      f"batches; got reg_type={reg_type}. A standalone Subida batch (type "
      f"{REGISTRATION_TYPE_SUBIDA}) is itself a subida — don't add an inline rider."
    )


# kwarg → sav_profile key, for reconciled fields only (sav_key non-empty)
KWARG_TO_SAV_KEY: dict[str, str] = {
  kwarg: sav_key
  for kwarg, (_, sav_key) in ENROLLMENT_FIELD_META.items()
  if sav_key
}


def parsed_bool(parsed: dict, key: str) -> bool:
  """Return True if the ParsedField at `key` has a truthy value."""
  f = parsed.get(key)
  return bool(f and f.value)


def escalao_field_to_name(field_key: str) -> str:
  """Convert 'escalao_sub14' → 'Sub 14', 'escalao_senior' → 'Senior', etc."""
  suffix = field_key.removeprefix("escalao_")
  m = re.match(r"(sub|mini)(\d+)$", suffix, re.IGNORECASE)
  if m:
    return f"{m.group(1).capitalize()} {m.group(2)}"
  return suffix.replace("_", " ").title()


def find_player_license_by_nif(
  parsed: dict, client: Any, *, club_id: int | None = None,
) -> int | None:
  """Return the license of the player with the OCR'd NIF in the login's club roster.

  Thin wrapper around :meth:`SavClient.find_license_by_nif` that pulls the
  NIF from a parsed OCR dict. Used both to decide reg_type when neither
  tipo_inscricao box is checked (hit → revalidação, miss → primeira) and
  to recover a missing licença on the form when the player is already in
  the roster.
  """
  nif_field = parsed.get("nif")
  nif = str(nif_field.value) if (nif_field and nif_field.value) else ""
  return client.find_license_by_nif(nif, club_id=club_id)


def derive_enrollment_params(
  parsed: dict, client: Any,
) -> tuple[int, int, int]:
  """
  Return (reg_type, tier_id, gender_id) from parsed OCR fields.

  The gender-scoped tier lookup is cached on the client, so callers that
  need the {id → name} map for display can re-call
  list_player_registration_tiers(gender_id=...) for free.

  When neither tipo_inscricao_revalidacao nor tipo_inscricao_primeira is
  checked on the form, the player is looked up by NIF in the login's
  club roster — found means revalidação, miss means primeira.

  Raises ValueError when no tier is detected or the name doesn't match SAV.
  """
  if parsed_bool(parsed, "tipo_inscricao_revalidacao"):
    reg_type = 2
  elif parsed_bool(parsed, "tipo_inscricao_primeira"):
    reg_type = 1
  else:
    reg_type = 2 if find_player_license_by_nif(parsed, client) is not None else 1
  gender_id = 2 if parsed_bool(parsed, "genero_feminino") else 1

  tier_field = next(
    (k for k, f in parsed.items() if k.startswith("escalao_") and f.value),
    None,
  )
  if not tier_field:
    raise ValueError("No tier (escalão) found in form")

  raw_name = escalao_field_to_name(tier_field)
  tiers = client.list_player_registration_tiers(gender_id=gender_id)
  wanted = normalise_text(raw_name)
  match = next(
    ((tid, tname) for tid, tname in tiers.items() if normalise_text(tname) == wanted),
    None,
  )
  if not match:
    gender_label = "Feminino" if gender_id == 2 else "Masculino"
    available = ", ".join(sorted(tiers.values()))
    raise ValueError(
      f"Tier {raw_name!r} not found for {gender_label}. Available: {available}"
    )
  return reg_type, match[0], gender_id


def build_primeira_kwargs(
  parsed: dict, *, concelhos: dict[int, str] | None = None,
) -> dict[str, Any]:
  """Map a parsed mod1 to type-1 wizard kwargs (no SAV reconciliation).

  Builds on ``fpb_mod1_to_sav_kwargs`` (id-doc / contact / address / guardian
  / consents) and adds the type-1-only demographics — ``name``,
  ``birth_date``, ``gender_id``, ``nif`` — that revalidação reads from the
  existing SAV record. The returned dict is suitable to splat directly into
  ``add_player_to_registration_batch(batch_id, **kwargs)`` on a type-1 batch
  (drop ``license`` and the wizard will create it via op=12).

  ``concelhos`` is the distrito-scoped {id → name} lookup; without it the
  concelho_id resolution will fail and the field appears in the preview's
  needs_review list so the caller supplies it explicitly.
  """
  from .fpb_mod1 import fpb_mod1_to_sav_kwargs

  base = fpb_mod1_to_sav_kwargs(parsed, concelhos=concelhos)
  base.pop("license", None)

  def val(key: str) -> Any:
    f = parsed.get(key)
    return f.value if f else None

  base["name"] = val("nome_completo")
  base["birth_date"] = val("data_nascimento")
  base["nif"] = val("nif")
  # genero_feminino takes precedence; default to masculino when neither is
  # checked rather than dropping the field — the wizard requires a value.
  base["gender_id"] = 2 if parsed_bool(parsed, "genero_feminino") else 1
  return base


def _ocr_confidence(parsed: dict, kwarg: str) -> float | None:
  """Look up the OCR confidence for the entity backing a wizard kwarg.

  Returns ``None`` when no entity is known for the kwarg (the field has no
  OCR source — derived defaults like nationality_id), when the entity didn't
  parse, or when the entity has no recorded confidence. For checkbox-group
  kwargs (gender_id, id_type) we pick the confidence of whichever checkbox
  was actually marked, since the unchecked ones carry no useful signal.
  """
  entity = _PRIMEIRA_KWARG_OCR_ENTITY.get(kwarg) or KWARG_TO_ENTITY.get(kwarg)
  if entity is None:
    return None
  entities = (entity,) if isinstance(entity, str) else entity
  for e in entities:
    f = parsed.get(e)
    if f and f.value:
      return f.confidence
  return None


def build_primeira_preview_fields(
  parsed: dict,
  kwargs: dict[str, Any],
  *,
  low_confidence_threshold: float = 0.60,
) -> tuple[list[dict], list[str]]:
  """Echo a type-1 kwargs dict as preview field rows (no SAV reconciliation).

  Status mapping:
    * ``"ocr"`` — value present and OCR confidence is acceptable (or no
      confidence is recorded, e.g. derived defaults).
    * ``"needs_review"`` — value missing for a required kwarg, OR the OCR
      confidence is below ``low_confidence_threshold``.

  Optional kwargs (telefone, nome_pai, etc.) that are missing are omitted
  rather than shown as needs_review — they're not required to commit.
  """
  fields: list[dict] = []
  needs_review: list[str] = []
  shown: set[str] = set()
  required = set(REQUIRED_PRIMEIRA_KWARGS)

  ordered = list(REQUIRED_PRIMEIRA_KWARGS) + [
    k for k in kwargs if k not in required
  ]
  for kwarg in ordered:
    if kwarg in shown:
      continue
    shown.add(kwarg)
    value = kwargs.get(kwarg)
    missing = value in (None, "")
    if missing and kwarg not in required:
      continue
    confidence = _ocr_confidence(parsed, kwarg)
    if missing:
      status = "needs_review"
    elif confidence is not None and confidence < low_confidence_threshold:
      status = "needs_review"
    else:
      status = "ocr"
    if status == "needs_review":
      needs_review.append(kwarg)
    label = ENROLLMENT_FIELD_META.get(kwarg, (kwarg, ""))[0]
    fields.append({
      "kwarg": kwarg, "label": label,
      "sav_value": None,
      "ocr_value": value,
      "final_value": value if status == "ocr" else None,
      "status": status,
      **({"confidence": round(confidence, 2)} if confidence is not None else {}),
    })

  return fields, needs_review


def parse_missing_guardian_fields(exc: Exception) -> list[str]:
  """Extract the missing-field list from a SavConfigError raised on minor enrollment."""
  m = re.search(r"missing required fields: (.+)$", str(exc))
  if not m:
    return list(_DEFAULT_GUARDIAN_FIELDS)
  return [s.strip() for s in m.group(1).split(",")]


def _canonical_tier_name_from_ocr(ocr_text: str) -> str:
  """Return the canonical SAV tier name matching OCR'd text, or ''.

  Used to tighten Subida name search: SAV's tier names are shared across
  genders, so a single normalised lookup against PLAYER_REGISTRATION_TIERS
  is enough. Falls back to '' when no entry matches — callers should then
  search without the tier filter so a slightly off OCR doesn't drop the
  player entirely.
  """
  from .lookups import PLAYER_REGISTRATION_TIERS

  needle = normalise_text(ocr_text)
  if not needle:
    return ""
  for tiers in PLAYER_REGISTRATION_TIERS.values():
    for name in tiers.values():
      if normalise_text(name) == needle:
        return name
  return ""


def resolve_subida_player(
  parsed: dict, client: Any, *, club_id: int,
) -> tuple[int | None, list[Any], str | None, int | None]:
  """Resolve the player for a parsed mod4 (Subida) by licence or name.

  Mod4 carries no NIF — only ``licenca_nr`` (optional), ``nome_jogador``
  (mandatory), and ``escalao_actual`` (origin tier). Name search is scoped
  to the user's club, the current season, AND the origin tier when it
  resolves to a known SAV name — a far tighter filter than club-only,
  which collapses common-name collisions. When the tier-scoped search
  returns nothing, we retry without the tier filter so OCR drift on the
  tier text doesn't lose an otherwise-resolvable player.

  Returns ``(license, candidates, ocr_name, ocr_license)``:
    - ``license`` is set when OCR licence resolves to a real SAV player, OR
      when name search returns exactly one match.
    - ``candidates`` is the name-search list (empty when license is set).
    - ``ocr_name`` / ``ocr_license`` echo what was read from the form.
  """
  lic_field = parsed.get("licenca_nr")
  ocr_license: int | None = None
  if lic_field and lic_field.value:
    try:
      ocr_license = int(str(lic_field.value).strip())
    except (ValueError, TypeError):
      ocr_license = None

  if ocr_license is not None:
    try:
      hits = client.search_players(license=str(ocr_license), club=0)
    except (SavError, ValueError):
      logger.debug("Subida licence lookup failed for %s", ocr_license, exc_info=True)
      hits = []
    if hits:
      return ocr_license, [], None, ocr_license

  name_field = parsed.get("nome_jogador")
  name_val = str(name_field.value).strip() if name_field and name_field.value else ""
  candidates: list[Any] = []
  if name_val:
    origin_tier_field = parsed.get("escalao_actual")
    origin_tier_raw = (
      str(origin_tier_field.value).strip()
      if origin_tier_field and origin_tier_field.value else ""
    )
    origin_tier_name = _canonical_tier_name_from_ocr(origin_tier_raw)
    try:
      if origin_tier_name:
        candidates = client.search_players(
          name=name_val, club=club_id, tier=origin_tier_name,
        )
      if not candidates:
        # Fallback: club-scoped without the tier filter. Catches OCR drift
        # on the escalão text and players whose stored tier differs from
        # the mod4's printed value.
        candidates = client.search_players(name=name_val, club=club_id)
    except (SavError, ValueError):
      logger.debug("Subida name search failed for club_id=%s", club_id, exc_info=True)
      candidates = []

  if len(candidates) == 1:
    return int(candidates[0].license), [], name_val or None, ocr_license

  return None, candidates, name_val or None, ocr_license


def resolve_subida_tier(
  parsed: dict, client: Any, *, gender_id: int,
) -> int:
  """Map a parsed mod4's ``escalao_subida`` text to the SAV tier_id for a gender.

  Raises ValueError when the field is empty or doesn't match a known tier.
  """
  tier_field = parsed.get("escalao_subida")
  raw_name = str(tier_field.value).strip() if tier_field and tier_field.value else ""
  if not raw_name:
    raise ValueError("No escalão de subida found in mod4 (escalao_subida is empty)")
  tiers = client.list_player_registration_tiers(gender_id=gender_id)
  wanted = normalise_text(raw_name)
  match = next(
    ((tid, tname) for tid, tname in tiers.items() if normalise_text(tname) == wanted),
    None,
  )
  if match is None:
    gender_label = "Feminino" if gender_id == 2 else "Masculino"
    available = ", ".join(sorted(tiers.values()))
    raise ValueError(
      f"Tier {raw_name!r} not found for {gender_label}. Available: {available}"
    )
  return match[0]


def gender_id_for_license(client: Any, license: int) -> int:
  """Look up a player's gender_id via the federation-wide search.

  Mod4 carries no gender field — we need a roundtrip to SAV to decide which
  tier table (Masculino / Feminino) the parsed `escalao_subida` lives in.
  Defaults to 1 (Masculino) when the player is found but the gender string
  doesn't match SAV's canonical labels, since SAV's tier table accepts that
  fallback gracefully (and the alternative is failing the whole flow).
  """
  results = client.search_players(license=str(license), club=0)
  if not results:
    raise ValueError(f"Player with licence {license} not found in SAV")
  return 2 if results[0].gender == "Feminino" else 1


def resolve_player_candidates(
  parsed: dict, eligible: set[int] | list[int], client: Any, club_id: int,
) -> tuple[int | None, list[Any], str | None, int | None]:
  """
  Resolve the player for a parsed form against an eligible-licence list.

  Returns ``(license, candidates, ocr_name, ocr_license)``:
    - ``license`` is set when OCR licence matches eligible, when a NIF
      lookup against the club roster yields an eligible licence, OR when
      name search yields exactly one eligible candidate.
    - ``candidates`` is the eligible-name-search list (empty when license is
      set or when no name search ran).
    - ``ocr_name`` / ``ocr_license`` echo what was read from the form.
  """
  eligible_set = set(eligible)

  lic_field = parsed.get("licenca_fpb")
  ocr_license: int | None = None
  if lic_field and lic_field.value:
    try:
      ocr_license = int(lic_field.value)
    except (ValueError, TypeError):
      ocr_license = None
    if ocr_license is not None and ocr_license in eligible_set:
      return ocr_license, [], None, ocr_license

  # If OCR gave us a licence but it isn't eligible, stop here — the form
  # already identifies the player, so a name-search fallback would just
  # surface noise. Bubble the OCR licence up to the manual-entry prompt
  # so the caller can override (already-registered players, etc.).
  if ocr_license is not None:
    return None, [], None, ocr_license

  # No OCR licence: try the same NIF-based club roster lookup we use to
  # decide reg_type. The map is cached on the client so this is free if
  # derive_enrollment_params already ran.
  nif_license = find_player_license_by_nif(parsed, client, club_id=club_id)
  if nif_license is not None and nif_license in eligible_set:
    return nif_license, [], None, None

  name_field = parsed.get("nome_completo")
  name_val = str(name_field.value) if name_field and name_field.value else ""
  candidates: list[Any] = []
  if name_val:
    try:
      found = client.search_players(name=name_val, club=club_id)
      candidates = [p for p in found if int(p.license) in eligible_set]
    except (SavError, ValueError):
      logger.debug("Name-search fallback failed for club_id=%s", club_id, exc_info=True)
      candidates = []

  if len(candidates) == 1:
    return int(candidates[0].license), [], name_val or None, ocr_license

  return None, candidates, name_val or None, ocr_license


def create_and_fetch_batch(
  client: Any, *, batch_type: int, tier_id: int, gender_id: int,
) -> tuple[int, Any]:
  """Create a registration batch and return ``(batch_id, batch)`` from the listing."""
  new_id = client.create_player_registration_batch(
    type=batch_type, tier=tier_id, gender_id=gender_id,
  )
  batches = client.list_player_registration_batches()
  batch = next((b for b in batches if b.id == new_id), None)
  if batch is None:
    raise RuntimeError(f"Newly created batch {new_id} not found in listing")
  return new_id, batch


def try_replace_document(
  client: Any, batch_id: int, license: int, source: str, *, tipo_doc: int,
) -> tuple[bool, str | None]:
  """Replace a player document, swallowing transport/validation errors.

  Returns ``(ok, error_message)`` — both CLI and MCP need this shape because
  enrollment is already committed by the time a document upload runs, and the
  caller wants to surface the failure without rolling back the registration.
  """
  try:
    client.replace_player_registration_document(
      batch_id, license, source, tipo_doc=tipo_doc,
    )
    return True, None
  except (SavError, FileNotFoundError, ValueError) as exc:
    return False, str(exc)


def try_upload_document(
  client: Any, batch_id: int, license: int, source: str, *, tipo_doc: int,
) -> tuple[bool, str | None]:
  """Upload (append) a player document, swallowing transport/validation errors.

  Like :func:`try_replace_document` but without the delete-existing step, so
  several files can share one ``tipo_doc``. Use it for the 2nd+ file of a doc
  type that allows multiples (documento_identificacao, outros) after the first
  has already cleared any prior docs via ``try_replace_document``.
  """
  try:
    client.upload_player_registration_document(
      batch_id, license, source, tipo_doc=tipo_doc,
    )
    return True, None
  except (SavError, FileNotFoundError, ValueError) as exc:
    return False, str(exc)
