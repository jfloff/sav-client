"""Shared enrollment workflow helpers used by CLI and MCP."""

from __future__ import annotations

import logging
import re
from typing import Any

from sav_client.exceptions import SavError

from .fields import ENROLLMENT_FIELD_META
from .text import normalise_text

logger = logging.getLogger(__name__)


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


def _build_club_nif_map(client: Any, club_id: int) -> dict[str, int]:
  """Build {nif → license} for the given club's roster (all seasons).

  SAV2 has no NIF-based search, so we pay one profile fetch per unique
  license (parallelised, max 8 workers). Used internally by
  find_player_license_by_nif and cached on the client per club.
  """
  from concurrent.futures import ThreadPoolExecutor

  try:
    roster = client.search_players(club=club_id, season=0)
  except (SavError, ValueError):
    logger.debug("Could not list roster for club_id=%s", club_id, exc_info=True)
    return {}

  seen: set[int] = set()
  licenses: list[int] = []
  for p in roster:
    try:
      lic = int(p.license)
    except (ValueError, TypeError):
      continue
    if lic not in seen:
      seen.add(lic)
      licenses.append(lic)
  if not licenses:
    return {}

  def _fetch(lic: int) -> tuple[str, int]:
    try:
      profile = client.load_player_profile(lic, club_id=club_id)
    except (SavError, ValueError):
      logger.debug("Could not load profile for license=%s", lic, exc_info=True)
      return "", 0
    return (profile.get("nif") or "").strip(), lic

  nif_map: dict[str, int] = {}
  with ThreadPoolExecutor(max_workers=min(8, len(licenses))) as pool:
    for nif_val, lic in pool.map(_fetch, licenses):
      if nif_val and lic:
        nif_map[nif_val] = lic
  return nif_map


def find_player_license_by_nif(
  parsed: dict, client: Any, *, club_id: int | None = None,
) -> int | None:
  """Return the license of the player with the OCR'd NIF in the login's club roster.

  Lookup tiers (cheapest first):
    1. SQLite license↔NIF cache — O(1), survives across processes.
    2. Per-club roster build, persisted into the SQLite cache for
       future runs. Done at most once per club per process.

  Used both to decide reg_type when neither tipo_inscricao box is checked
  (hit → revalidação, miss → primeira) and to recover a missing licença
  on the form when the player is already in the roster.

  Returns None when NIF or session club is missing, or when no roster
  profile carries a matching NIF.
  """
  nif_field = parsed.get("nif")
  nif = str(nif_field.value).strip() if (nif_field and nif_field.value) else ""
  if not nif:
    return None

  sqlite_cache = getattr(client, "_cache", None)
  if sqlite_cache is not None:
    hit = sqlite_cache.get_license_by_nif(nif)
    if hit:
      return hit

  if club_id is None:
    club_id = int(client.session.get("organizacao") or 0) if client.session else 0
  if not club_id:
    return None

  built = getattr(client, "_nif_clubs_built", None)
  if built is None:
    built = set()
    # Real SavClient initialises this attribute in __init__, so this path is
    # only taken by test stubs / unusual client shapes. Frozen dataclasses
    # and slot-only classes reject assignment — fall back to a throwaway set
    # so callers don't crash; the per-process roster cache just won't persist.
    try:
      client._nif_clubs_built = built
    except (AttributeError, TypeError):
      pass
  if club_id in built:
    return None

  nif_map = _build_club_nif_map(client, club_id)
  built.add(club_id)
  if nif_map and sqlite_cache is not None:
    sqlite_cache.record_player_nifs([(lic, n) for n, lic in nif_map.items()])
  return nif_map.get(nif)


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


def parse_missing_guardian_fields(exc: Exception) -> list[str]:
  """Extract the missing-field list from a SavConfigError raised on minor enrollment."""
  m = re.search(r"missing required fields: (.+)$", str(exc))
  if not m:
    return list(_DEFAULT_GUARDIAN_FIELDS)
  return [s.strip() for s in m.group(1).split(",")]


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
