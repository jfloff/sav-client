"""Shared enrollment workflow helpers used by CLI and MCP."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping

from sav_client.exceptions import SavError
from sav_client.models import IdentityMatch

from .dates import to_iso
from .identifiers import normalise_nif
from .estatuto import LOW_CONFIDENCE, is_portuguese_nationality
from .fields import ENROLLMENT_FIELD_META, KWARG_TO_ENTITY
from .flags import decode_sav_flag
from .text import normalise_text

logger = logging.getLogger(__name__)

# Type-1 wizard required kwargs (mandatory for the SAV2 1ª Inscrição submit).
# Optional ones (telefone, nome_pai/mae, country/naturalidade overrides) stay
# off the list; the wizard defaults them.
REQUIRED_PRIMEIRA_KWARGS: tuple[str, ...] = (
  "name", "birth_date", "gender_id", "nif",
  "id_type", "id_number", "id_expiry", "email",
  "morada", "cod_postal", "distrito_id", "concelho_id",
  # Required here but not by SAV, which is the whole point — see
  # build_primeira_kwargs. SAV's type-1 wizard defaults nationality to
  # Portugal, so an unread or non-Portuguese one does not merely lose
  # information: it files the player as Portuguese. Listing it as required
  # makes an unconfirmed nationality surface in the preview's needs_review
  # instead of being silently defaulted at commit time.
  "nationality_id",
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
  "nationality_id": "nacionalidade",
}


_DEFAULT_GUARDIAN_FIELDS = [
  "guardian_name", "guardian_relation", "guardian_phone", "guardian_email",
]

# Batch type for a standalone Subida de escalão batch (SAV `newGuia(1,4)`),
# distinct from the inline promote-on-enroll rider that can ride on a 1ª
# Inscrição (1) or Revalidação (2). See validate_subida_combo.
REGISTRATION_TYPE_PRIMEIRA = 1
REGISTRATION_TYPE_REVALIDACAO = 2
REGISTRATION_TYPE_TRANSFERENCIA = 3
REGISTRATION_TYPE_SUBIDA = 4

# SAV2 nationality id for Portugal (`nacional` field on the existing-record
# payload). Drives the portuguese-vs-foreign-born split in
# `compute_enrollment_checklist`.
PORTUGAL_NATIONALITY_ID = 155


@dataclass(frozen=True)
class PrimeiraDuplicate:
  """The duplicate decision for one SAV 1ª Inscrição probe response.

  ``existing_id`` is SAV's person id, or ``None`` when the response carries
  none. ``blocking`` and ``reusable`` are mutually exclusive, and both are
  false when ``existe`` is false. See ``classify_primeira_duplicate`` for the
  rule that produces them.
  """

  existing_id: int | None
  blocking: bool
  reusable: bool


def classify_primeira_duplicate(dup: Mapping[str, Any]) -> PrimeiraDuplicate:
  """Return the safe duplicate decision for a SAV 1ª Inscrição probe.

  ``existe:1`` with ``inscricaovalida:0`` is a person left behind by a
  wizard run that died before its commit, so that record is reusable rather
  than blocking. A missing ``inscricaovalida`` is blocking via
  ``absent_is=True``: fail closed if SAV changes shape or drops the key, and
  do not silently enroll somebody who holds a licence. The two decisions are
  mutually exclusive and both are false when ``existe`` is false.

  Flag decoding tolerates non-numeric values without letting the old
  ``int(dup.get("existe", 0))`` ``ValueError``/``TypeError`` escape.
  Genuinely unrecognised encodings still raise ``SavResponseError`` from the
  shared decoder, because guessing about them would be unsafe.

  ``existing_id`` comes from ``id`` alone, and is ``None`` when SAV omits or
  corrupts it. **There is deliberately no ``atleta`` fallback.** The raise
  site this replaces used ``dup.get("id") or dup.get("atleta")``, which was
  harmless while the value only decorated an error message — but ``atleta``
  is a boolean "is an athlete" flag that reads ``1`` on every real response,
  so falling back to it here would hand the reuse path person id 1 and enrol
  a stranger. A missing id must stay missing and let the caller refuse.
  """
  try:
    existing_id: int | None = int(dup.get("id"))
  except (TypeError, ValueError):
    existing_id = None

  existe_flag = decode_sav_flag(
    dup.get("existe"), field="existe", absent_is=False,
  )
  inscricao_valida = decode_sav_flag(
    dup.get("inscricaovalida"), field="inscricaovalida", absent_is=True,
  )
  blocking = existe_flag and inscricao_valida
  reusable = existe_flag and not inscricao_valida
  return PrimeiraDuplicate(existing_id, blocking, reusable)


def compute_enrollment_checklist(
  reg_type: int,
  nacional_id: int | None,
  uploaded_doc_types: list[str | None],
) -> dict[str, Any] | None:
  """Build the per-enrollment required-document checklist.

  The rules mirror FPB policy (the app doesn't encode them anywhere else):

  * reg_type 1 / 2 — checklist depends on nationality.
      portuguese (nacional == 155): fpb_modelo_1, exame_medico required;
        fpb_modelo_4 optional (inline subida).
      foreign_born (anything else): fpb_modelo_1, exame_medico,
        atestado_residencia, certidao_matricula, and **two**
        documento_identificacao docs (passaporte + título de residência —
        the título can be the player's or the parent's; SAV stores both
        under tipo_doc=18 so we can only count, not name them).
  * reg_type 3 (Transferência) — not handled yet; returns None so callers
    surface "checklist not implemented".
  * reg_type 4 (Subida standalone) — only fpb_modelo_4 required, no
    scenario distinction.

  Args:
    reg_type: SAV2 batch type id (1, 2, 3, or 4).
    nacional_id: SAV2 nationality id from the existing-record payload's
      `nacional` field. 155 = Portugal. None / unknown → treated as
      foreign_born (defensive: more docs requested is the safer error).
    uploaded_doc_types: doc_type strings (DocType.value) from
      `list_player_documents`. Entries may be None for SAV2-only types
      with no sav-parsers mapping — those are ignored.

  Returns:
    None for reg_type=3. Otherwise a dict with `scenario`, `reg_type`,
    `required`, `optional`, and `missing` (human-readable strings for
    each unsatisfied required entry).
  """
  if reg_type == REGISTRATION_TYPE_TRANSFERENCIA:
    return None

  counts: dict[str, int] = {}
  for dt in uploaded_doc_types:
    if dt:
      counts[dt] = counts.get(dt, 0) + 1

  if reg_type == REGISTRATION_TYPE_SUBIDA:
    return _format_checklist(
      scenario="subida_standalone",
      reg_type=reg_type,
      required=[("fpb_modelo_4", 1)],
      optional=[],
      counts=counts,
    )

  if nacional_id == PORTUGAL_NATIONALITY_ID:
    scenario = "portuguese"
    required = [("fpb_modelo_1", 1), ("exame_medico", 1)]
  else:
    scenario = "foreign_born"
    required = [
      ("fpb_modelo_1", 1),
      ("exame_medico", 1),
      ("atestado_residencia", 1),
      ("certidao_matricula", 1),
      ("documento_identificacao", 2),
    ]
  return _format_checklist(
    scenario=scenario,
    reg_type=reg_type,
    required=required,
    optional=[("fpb_modelo_4", "Subida de escalão")],
    counts=counts,
  )


def _format_checklist(
  *,
  scenario: str,
  reg_type: int,
  required: list[tuple[str, int]],
  optional: list[tuple[str, str]],
  counts: dict[str, int],
) -> dict[str, Any]:
  required_rows = []
  missing: list[str] = []
  for doc_type, min_count in required:
    found = counts.get(doc_type, 0)
    satisfied = found >= min_count
    required_rows.append({
      "doc_type": doc_type,
      "min_count": min_count,
      "found_count": found,
      "satisfied": satisfied,
    })
    if not satisfied:
      if min_count > 1:
        missing.append(f"{doc_type} (need {min_count}, found {found})")
      else:
        missing.append(doc_type)

  optional_rows = [
    {"doc_type": dt, "found_count": counts.get(dt, 0), "label": label}
    for dt, label in optional
  ]
  return {
    "scenario": scenario,
    "reg_type": reg_type,
    "required": required_rows,
    "optional": optional_rows,
    "missing": missing,
  }


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


def resolve_player_from_form(
  parsed: dict, client: Any,
) -> IdentityMatch | None:
  """Resolve form identity in the login club, returning None without keys.

  A NIF alone is not trusted: SAV2 placeholder NIFs and NIFs shared by family
  members can identify several people. Usable document numbers and, when a
  valid birth date is available, the form name provide additional constraints.
  The resolver always scopes the lookup to the session club.
  """
  def _value(key: str) -> str | None:
    field = parsed.get(key)
    raw = field.value if field is not None else None
    value = str(raw).strip() if raw is not None else ""
    return value or None

  # Pass only a well-formed NIF: an OCR misread (a short or noisy read) is no
  # key at all, and handing it on would make the resolver raise instead of
  # treating the form as carrying no usable NIF.
  nif = normalise_nif(_value("nif"))
  id_number = _value("num_doc_identificacao")
  raw_birth_date = _value("data_nascimento")
  birth_date: str | None = None
  if raw_birth_date:
    normalised = to_iso(raw_birth_date)
    try:
      parsed_date = date.fromisoformat(normalised)
    except ValueError:
      pass
    else:
      if parsed_date.isoformat() == normalised and re.fullmatch(
        r"\d{4}-\d{2}-\d{2}", normalised,
      ):
        birth_date = normalised

  name = _value("nome_completo") if birth_date else None
  if not (nif or id_number or (name and birth_date)):
    return None

  return client.resolve_player_identity(
    nif=nif,
    id_number=id_number,
    birth_date=birth_date,
    name=name,
    club=None,
    status="all",
  )


def derive_enrollment_params(
  parsed: dict, client: Any,
) -> tuple[int, int, int]:
  """
  Return (reg_type, tier_id, gender_id) from parsed OCR fields.

  The gender-scoped tier lookup is cached on the client, so callers that
  need the {id → name} map for display can re-call
  list_player_registration_tiers(gender_id=...) for free.

  When neither tipo_inscricao_revalidacao nor tipo_inscricao_primeira is
  checked on the form, a uniquely resolved player means revalidação and a
  definitive miss means primeira. Ambiguous or unverifiable identity requires
  an explicit form checkbox or caller-supplied registration type/licence.

  Raises ValueError when no tier is detected or the name doesn't match SAV.
  """
  if parsed_bool(parsed, "tipo_inscricao_revalidacao"):
    reg_type = 2
  elif parsed_bool(parsed, "tipo_inscricao_primeira"):
    reg_type = 1
  else:
    identity = resolve_player_from_form(parsed, client)
    if identity is None or identity.status == "not_found":
      reg_type = 1
    elif identity.status == "found":
      reg_type = 2
    elif identity.status == "ambiguous":
      raise ValueError(
        "SAV found several players for this form's identifying data. Tick "
        "the Revalidação / 1ª Inscrição box, or pass reg_type or a licence."
      )
    else:
      raise ValueError(
        "SAV could not verify this form's identity. Tick the Revalidação / "
        "1ª Inscrição box, or pass reg_type or a licence."
      )
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

  # Nationality is set ONLY when the form positively says Portugal, at a
  # confidence worth trusting. Everything else — a blank box, a low-confidence
  # read, any other country — is left unset, which surfaces `nationality_id` in
  # the preview's needs_review for the caller to answer with a SAV nationality
  # id.
  #
  # **Leaving it unset is not the same as leaving it unknown**, and that is the
  # trap: SAV's type-1 wizard defaults nationality to Portugal, so an
  # unanswered `nationality_id` is not a gap in the record, it is the assertion
  # that the player is Portuguese. This package cannot make that assertion from
  # a box it could not read.
  #
  # Note what is deliberately *not* consulted here: the Estatuto decision. A
  # `Sem FBP Comunitário` estatuto is shared by ~147 countries and says nothing
  # about which one; nationality and estatuto answer different questions and
  # neither may be read off the other (see sav_shared.estatuto).
  nationality = parsed.get("nacionalidade")
  if (
    nationality is not None
    and is_portuguese_nationality(nationality.value)
    and (nationality.confidence is None or nationality.confidence >= LOW_CONFIDENCE)
  ):
    base["nationality_id"] = PORTUGAL_NATIONALITY_ID
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
  low_confidence_threshold: float = LOW_CONFIDENCE,
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
) -> tuple[int | None, list[Any], str | None, int | None, Any]:
  """Resolve the player for a parsed mod4 (Subida) by licence or name.

  Mod4 carries no NIF — only ``licenca_nr`` (optional), ``nome_jogador``
  (mandatory), and ``escalao_actual`` (origin tier). Name search is scoped
  to the user's club, the current season, AND the origin tier when it
  resolves to a known SAV name — a far tighter filter than club-only,
  which collapses common-name collisions. When the tier-scoped search
  returns nothing, we retry without the tier filter so OCR drift on the
  tier text doesn't lose an otherwise-resolvable player.

  Returns ``(license, candidates, ocr_name, ocr_license, player)``:
    - ``license`` is set when OCR licence resolves to a real SAV player, OR
      when name search returns exactly one match.
    - ``candidates`` is the name-search list (empty when license is set).
    - ``ocr_name`` / ``ocr_license`` echo what was read from the form.
    - ``player`` is the SAV row the licence was matched on (``None`` when no
      licence is set). It already carries the gender, so callers read it from
      here instead of asking SAV again.
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
      return ocr_license, [], None, ocr_license, hits[0]

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
    return int(candidates[0].license), [], name_val or None, ocr_license, candidates[0]

  return None, candidates, name_val or None, ocr_license, None


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


def resolve_player_candidates(
  parsed: dict, eligible: set[int] | list[int], client: Any, club_id: int,
) -> tuple[int | None, list[Any], str | None, int | None]:
  """
  Resolve the player for a parsed form against an eligible-licence list.

  Returns ``(license, candidates, ocr_name, ocr_license)``:
    - ``license`` is set when OCR licence matches eligible, when identity
      resolution finds an eligible licence, or when name search yields exactly
      one eligible candidate.
    - ``candidates`` contains eligible identity or name-search rows when the
      caller must ask the user to choose.
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

  # No OCR licence: use all available form identity details. An ambiguous
  # result must remain a user choice, even if just one candidate is eligible.
  identity = resolve_player_from_form(parsed, client)
  if identity is not None and identity.status == "found":
    if identity.player is not None and int(identity.player.license) in eligible_set:
      return int(identity.player.license), [], None, None
  elif identity is not None and identity.status == "ambiguous":
    eligible_candidates = [
      player for player in identity.candidates
      if int(player.license) in eligible_set
    ]
    if eligible_candidates:
      name_field = parsed.get("nome_completo")
      name_val = str(name_field.value) if name_field and name_field.value else ""
      return None, eligible_candidates, name_val or None, None

  name_field = parsed.get("nome_completo")
  name_val = str(name_field.value) if name_field and name_field.value else ""
  candidates: list[Any] = []
  if name_val:
    try:
      # Every season, any status: the eligible list is mostly players with no
      # current-season row yet (a Revalidação athlete, by definition), so a
      # current-season name search could never find them. The eligible filter
      # below keeps the answer safe.
      found = client.search_players(
        name=name_val, club=club_id, season=0, status="all",
      )
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
