"""
Shared helpers used by both sav_cli and sav_mcp.

Neither presentation layer belongs here — only pure utilities and domain
knowledge that both need independently of how they handle I/O.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from .fpb_mod1 import (
    KWARG_TO_ENTITY,
    ReconcileResult,
    fpb_mod1_to_sav_kwargs,
    reconcile_fpb_mod1,
)

_DEFAULT_GUARDIAN_FIELDS = [
    "guardian_name", "guardian_relation", "guardian_phone", "guardian_email",
]


# ── Text utilities ────────────────────────────────────────────────────────────

def normalise_text(value: str) -> str:
    """Lowercase, strip accents, collapse punctuation for fuzzy matching."""
    ascii_val = "".join(
        ch for ch in unicodedata.normalize("NFKD", value)
        if not unicodedata.combining(ch)
    )
    return " ".join(re.sub(r"[^a-z0-9]+", " ", ascii_val.lower()).split())


def iso_date(date_ddmmyyyy: str) -> str:
    """Convert DD-MM-YYYY to YYYY-MM-DD for lexicographic date comparison."""
    try:
        d, m, y = date_ddmmyyyy.split("-")
        return f"{y}-{m}-{d}"
    except Exception:
        return date_ddmmyyyy


# ── Model serializers ─────────────────────────────────────────────────────────

def player_to_dict(p: Any) -> dict:
    return {
        "id": p.id, "license": p.license, "name": p.name,
        "club": p.club, "association": p.association,
        "tier": p.tier, "gender": p.gender,
        "birth_date": p.birth_date, "nationality": p.nationality,
        "status": p.status, "season": p.season, "active": p.active,
    }


def game_to_dict(g: Any) -> dict:
    return {
        "id": g.id, "number": g.number,
        "date": g.date, "time": g.time,
        "home": g.home, "away": g.away,
        "home_score": g.home_score, "away_score": g.away_score,
        "competition": g.competition, "tier": g.tier, "gender": g.gender,
        "venue": g.venue, "game_status": g.game_status,
    }


def club_to_dict(c: Any) -> dict:
    return {"id": c.id, "name": c.name, "full_name": c.full_name, "code": c.code}


def batch_to_dict(b: Any) -> dict:
    return {
        "batch_id": b.id, "number": b.number,
        "type": b.type, "tier": b.tier, "gender": b.gender,
        "state": b.state, "state_date": b.state_date,
        "item_count": b.item_count, "season": b.season,
        "is_open": b.is_open,
    }


# ── Enrollment domain ─────────────────────────────────────────────────────────

REGISTRATION_TYPE_LABELS: dict[int, str] = {
    1: "1ª Inscrição",
    2: "Revalidação",
    3: "Transferência",
    4: "Subida",
}

# kwarg → (human label, sav_profile key for reconciliation display)
ENROLLMENT_FIELD_META: dict[str, tuple[str, str]] = {
    "id_type":                ("Tipo de Documento",          "tipo"),
    "id_number":              ("Nº Identificação",           "numi"),
    "id_expiry":              ("Validade Documento",         "dataval"),
    "telemovel":              ("Telemóvel",                  "tele"),
    "morada":                 ("Morada",                     "morada"),
    "localidade_txt":         ("Localidade",                 "localidade_txt"),
    "cod_postal":             ("Código Postal",              "cod_postal"),
    "guardian_name":          ("Nome Encarregado",           ""),
    "guardian_relation":      ("Relação Encarregado",        ""),
    "guardian_phone":         ("Telefone Encarregado",       ""),
    "guardian_email":         ("Email Encarregado",          ""),
    "consent_data":           ("Consentimento Dados",        ""),
    "consent_communications": ("Consentimento Comunicações", ""),
    "consent_marketing":      ("Consentimento Marketing",    ""),
}

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


def derive_enrollment_params(
    parsed: dict, client: Any,
) -> tuple[int, int, int, dict[int, str]]:
    """
    Return (reg_type, tier_id, gender_id, tiers) from parsed OCR fields.

    `tiers` is the gender-scoped id→name mapping so callers can display tier
    names without an extra network round-trip.

    Raises ValueError when no tier is detected or the name doesn't match SAV.
    """
    reg_type = 2 if parsed_bool(parsed, "tipo_inscricao_revalidacao") else 1
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
    return reg_type, match[0], gender_id, tiers


def parse_missing_guardian_fields(exc: Exception) -> list[str]:
    """Extract the missing-field list from a SavConfigError raised on minor enrollment."""
    m = re.search(r"missing required fields: (.+)$", str(exc))
    if not m:
        return list(_DEFAULT_GUARDIAN_FIELDS)
    return [s.strip() for s in m.group(1).split(",")]


def filter_games(
    games: list[Any],
    *,
    competition: str = "",
    status: str = "",
    date_from: str = "",
    date_to: str = "",
) -> list[Any]:
    """Apply the common client-side game-sheet filters."""
    if competition:
        games = [g for g in games if competition.lower() in g.competition.lower()]
    if status:
        games = [g for g in games if g.game_status == status]
    if date_from:
        games = [g for g in games if iso_date(g.date) >= iso_date(date_from)]
    if date_to:
        games = [g for g in games if iso_date(g.date) <= iso_date(date_to)]
    return games


def resolve_player_candidates(
    parsed: dict, eligible: set[int] | list[int], client: Any, club_id: int,
) -> tuple[int | None, list[Any], str | None, int | None]:
    """
    Resolve the player for a parsed form against an eligible-licence list.

    Returns ``(license, candidates, ocr_name, ocr_license)``:
      - ``license`` is set when OCR licence matches eligible OR when name search
        yields exactly one eligible candidate.
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

    name_field = parsed.get("nome_completo")
    name_val = str(name_field.value) if name_field and name_field.value else ""
    candidates: list[Any] = []
    if name_val:
        try:
            found = client.search_players(name=name_val, club=club_id)
            candidates = [p for p in found if int(p.license) in eligible_set]
        except Exception:
            candidates = []

    if len(candidates) == 1:
        return int(candidates[0].license), [], name_val or None, ocr_license

    return None, candidates, name_val or None, ocr_license


def create_and_fetch_batch(
    client: Any, *, type: int, tier_id: int, gender_id: int,
) -> tuple[int, Any]:
    """Create a registration batch and return ``(batch_id, batch)`` from the listing."""
    new_id = client.create_player_registration_batch(
        type=type, tier=tier_id, gender_id=gender_id,
    )
    batches = client.list_player_registration_batches()
    batch = next((b for b in batches if b.id == new_id), None)
    if batch is None:
        raise RuntimeError(f"Newly created batch {new_id} not found in listing")
    return new_id, batch


# ── Game ordering ─────────────────────────────────────────────────────────────

def game_sort_key(game: Any) -> tuple:
    """Return a (date, time) tuple for sorting games chronologically."""
    try:
        d, m, y = game.date.split("-")
        date_key = (int(y), int(m), int(d))
    except Exception:
        date_key = (9999, 99, 99)
    try:
        h, mi = game.time.split(":")
        time_key = (int(h), int(mi))
    except Exception:
        time_key = (99, 99)
    return date_key + time_key


# ── Fuzzy club matching ───────────────────────────────────────────────────────

_CLUB_FIELDS = ("name", "full_name", "code")
_FUZZY_MIN_SCORE = 82
_FUZZY_TIE_BAND = 3


def _field_aliases(value: str) -> tuple[str, set[str], set[str]]:
    """Return normalised field text plus token/acronym aliases for fuzzy matching."""
    normalised = normalise_text(value)
    if not normalised:
        return "", set(), set()

    tokens = tuple(normalised.split())
    aliases = {normalised, "".join(tokens)}
    if len(tokens) >= 2:
        aliases.add("".join(token[0] for token in tokens))
        # Tail acronyms stopping at 2 remaining tokens so we never generate a 1-letter alias.
        for start in range(1, len(tokens) - 1):
            aliases.add("".join(token[0] for token in tokens[start:]))
    return normalised, set(tokens), {a for a in aliases if len(a) >= 2}


def _club_matches_query(club: Any, query: str) -> bool:
    """Match a club query against name/full-name/code with accent-tolerant aliases."""
    normalised_query = normalise_text(query)
    if not normalised_query:
        return True

    query_tokens = normalised_query.split()
    for raw in (getattr(club, f, "") for f in _CLUB_FIELDS):
        field_text, field_tokens, aliases = _field_aliases(raw)
        if not field_text:
            continue
        if normalised_query in field_text or normalised_query in aliases:
            return True
        if all(token in field_tokens or token in aliases for token in query_tokens):
            return True
    return False


def _club_match_candidates(club: Any) -> set[str]:
    """Build normalised candidate strings for fuzzy club matching."""
    candidates: set[str] = set()
    for raw in (getattr(club, f, "") for f in _CLUB_FIELDS):
        field_text, field_tokens, aliases = _field_aliases(raw)
        if field_text:
            candidates.add(field_text)
        candidates.update(field_tokens)
        candidates.update(aliases)
    return {c for c in candidates if c}


def _rapidfuzz_best_score(query: str, candidates: set[str]) -> float:
    """Return the best fuzzy score for a query/candidate set."""
    from rapidfuzz import fuzz

    normalised_query = normalise_text(query)
    if not normalised_query:
        return 0.0

    best = 0.0
    for candidate in candidates:
        best = max(
            best,
            fuzz.ratio(normalised_query, candidate),
            fuzz.partial_ratio(normalised_query, candidate),
            fuzz.token_sort_ratio(normalised_query, candidate),
            fuzz.token_set_ratio(normalised_query, candidate),
        )
    return float(best)


def find_club_matches(clubs: list[Any], query: str) -> list[Any]:
    """Find direct matches first, then fuzzy matches ranked by score."""
    direct_matches = [c for c in clubs if _club_matches_query(c, query)]
    if direct_matches:
        return direct_matches

    scored: list[tuple[float, Any]] = []
    for club in clubs:
        score = _rapidfuzz_best_score(query, _club_match_candidates(club))
        if score >= _FUZZY_MIN_SCORE:
            scored.append((score, club))

    if not scored:
        return []

    scored.sort(key=lambda item: (-item[0], getattr(item[1], "name", "")))
    best_score = scored[0][0]
    return [club for score, club in scored if score >= best_score - _FUZZY_TIE_BAND]
