"""
MCP server for the FPB SAV2 system.

Exposes read access to players, clubs, games, and registration batches, plus a
multi-step player-enrollment workflow that orchestrates OCR-parsed FPB forms
against the SAV2 API.  Tools are designed to be stateless-friendly so an LLM
agent can drive them without an interactive UI — the chat is the confirmation
loop.

Enrollment workflow:
    1. parse_enrollment_forms  → mod1_id(s) / medical_exam_id(s) / mod4_id(s) + parsed metadata
    2. find_open_batch / create_batch  → batch_number
    3. resolve_player  → license (or candidate list if ambiguous)
    4. preview_enrollment  → full reconciled profile (+ optional medical exam sidecar)
    5. submit_enrollment  → player_id (auto-uploads fpb_modelo_1 and optional exame_medico)

Standalone Subida (type-4) uses the mod4 OCR fields (licenca_nr/name/escalao_subida):
    1. parse_enrollment_forms  → mod4_id + parsed metadata
    2. resolve_subida_target   → license + tier_id + gender_id (or candidates)
    3. find_open_batch(reg_type=4, …) / create_batch  → Subida batch_number
    4. submit_subida_enrollment(batch_number, license, mod4_id)

Document tools (post-enrollment, ad-hoc):
    list_player_documents / upload_player_document /
    delete_player_document / replace_player_document
    use sav-parsers doc_type strings and translate to SAV2 tipo_doc internally.
"""

from __future__ import annotations

import base64
import binascii
import logging
import os
import re
import tempfile
import uuid
from typing import Any

from mcp.server.fastmcp import FastMCP

from sav_client import SavClient
from sav_client.exceptions import (
    LicenseNotEnrolledError,
    SavConfigError,
    SavConnectionError,
    SavError,
    SavResponseError,
)

logger = logging.getLogger(__name__)
from sav_shared.files import ensure_pdf
from sav_shared.enrollment import (
    REGISTRATION_TYPE_SUBIDA,
    build_primeira_kwargs,
    build_primeira_preview_fields,
    compute_enrollment_checklist,
    create_and_fetch_batch,
    derive_enrollment_params,
    gender_id_for_license,
    parse_missing_guardian_fields,
    parsed_bool,
    resolve_player_candidates,
    resolve_subida_player,
    resolve_subida_tier,
    try_replace_document,
    validate_subida_combo,
)
from sav_shared.fields import ENROLLMENT_FIELD_META, KWARG_TO_ENTITY
from sav_shared.fpb_mod1 import (
    carimbo_overlay,
    inscricao_overlay,
    overlaid_pdf,
    read_carimbo,
    read_tipo_inscricao,
    reconcile_fpb_mod1,
)
from sav_shared.games import filter_games, game_sort_key
from sav_shared.lookups import (
    GENERO,
    REGISTRATION_TYPE_LABELS,
    TIER_MIN_AGE_IN_SEASON,
    doc_type_to_tipo_doc,
    player_registration_tiers,
    tier_birth_years_for_season,
    tipo_doc_to_doc_type,
)
from sav_shared.medical_exam import extract_medical_exam_info
from sav_shared.serializers import (
    batch_to_dict,
    club_to_dict,
    coach_to_dict,
    game_to_dict,
    player_to_dict,
)
from sav_parsers.types import DocType

server = FastMCP("FPB SAV")

# ── Singleton SAV client ──────────────────────────────────────────────────────

_client: SavClient | None = None


def _get_client() -> SavClient:
    global _client
    if _client is None:
        _client = SavClient.from_env()
        _client.login()
    return _client


def _verify_nif_claim(form: dict[str, Any], claimed_nif: str | None) -> None:
    """Defense-in-depth: refuse if the caller's claimed NIF disagrees with the
    form's OCR'd NIF.

    Wrappers pass the caller-asserted dependent NIF as the `nif` argument on
    enrollment tools so they can enforce self-scope without sav-mcp having to
    enforce anything itself. When the form OCR yielded a NIF and the caller's
    claim disagrees, we still raise — uploading a form for one athlete while
    claiming another is almost certainly a mistake (or an attack).
    """
    if not claimed_nif:
        return
    parsed = form.get("parsed") or {}
    nif_field = parsed.get("nif")
    parsed_nif = (
        str(nif_field.value) if nif_field and nif_field.value else None
    )
    if not parsed_nif:
        return
    norm_claim = re.sub(r"\D", "", claimed_nif)
    norm_parsed = re.sub(r"\D", "", parsed_nif)
    if norm_claim and norm_parsed and norm_claim != norm_parsed:
        raise ValueError(
            f"Claimed NIF {claimed_nif!r} does not match the form's OCR'd "
            f"NIF {parsed_nif!r}; refusing to proceed."
        )


def _resolve_license_batch(client: SavClient, license: int) -> int | dict:
    """Resolve the open batch for a license.

    Returns the batch_id on success, or a structured error dict shaped as
    ``{"error": "license_not_enrolled", "license": int, "open_batches": [...]}``
    when the license is not enrolled in any open batch. Tools should return
    that dict directly so the LLM client can act on it.
    """
    try:
        return client.resolve_batch_id_by_license(license)
    except LicenseNotEnrolledError as exc:
        return {
            "error": "license_not_enrolled",
            "license": exc.license,
            "open_batches": exc.open_batches,
        }


# ── Session ───────────────────────────────────────────────────────────────────

@server.tool()
def get_session_info() -> dict:
    """
    Return the authenticated session's context.

    Useful for the LLM to know what "the session's club" resolves to before
    calling tools that default to it (search_players, list_games, list_batches,
    etc.).

    Returns ``{user, profile, club_id, season_id}``. season_id is the current
    epoch — pass it (or omit / pass 0 for all-seasons) to tools that accept a
    season parameter.
    """
    client = _get_client()
    session = client.session
    if session is None:
        raise ValueError("Session not initialized")
    return {
        "user": session.get("user"),
        "profile": session.get("perfil"),
        "club_id": int(session.get("organizacao") or 0),
        "season_id": int(session.get("epoca_id") or 0),
    }


# ── Players ───────────────────────────────────────────────────────────────────

@server.tool()
def search_players(
    name: str = "",
    license: str = "",
    club_id: int | None = None,
    association_id: int | None = None,
    tier: str = "",
    gender: int = 0,
    status: str = "active",
    birth_year: list[int] | None = None,
    season: int | None = None,
    limit: int | None = None,
    with_details: bool = False,
) -> list[dict]:
    """
    Search for players in the SAV system.

    club_id defaults to the session's own club when omitted.
    Pass club_id=0 to search all clubs (federation-wide or scoped by association_id).
    status: "active" | "inactive" | "all"
    with_details: when true, issue one extra request per player to fill
        photo_url and mobile_phone in the returned rows. Off by default
        because it is N+1.
    """
    client = _get_client()
    effective_club: int | list[int] = (
        club_id if club_id is not None
        else int(client.session.get("organizacao") or 0)
    )
    players = client.search_players(
        name=name,
        license=license,
        club=effective_club,
        association=association_id,
        tier=tier,
        gender=gender,
        status=status,
        birth_year=birth_year,
        season=season,
        limit=limit,
        with_details=with_details,
    )
    return [player_to_dict(p, with_details=with_details) for p in players]


@server.tool()
def get_player(
    license: str,
    club_id: int | None = None,
    with_details: bool = False,
) -> dict | None:
    """
    Return details for a single player by licence number.

    club_id defaults to the session's own club when omitted.
    with_details: when true, also fetch photo_url and mobile_phone.
    Returns null if no player is found with that licence.
    """
    client = _get_client()
    effective_club: int = (
        club_id if club_id is not None
        else int(client.session.get("organizacao") or 0)
    )
    results = client.search_players(
        license=license, club=effective_club, with_details=with_details,
    )
    if not results:
        return None
    return player_to_dict(results[0], with_details=with_details)


@server.tool()
def find_player_by_nif(
    nif: str,
    club_id: int | None = None,
    with_details: bool = False,
) -> dict | None:
    """
    Resolve a player by Portuguese NIF (9 digits) — inverse of get_player.

    Returns the same shape as get_player, or null if no player in the club
    roster matches. club_id defaults to the session's own club.
    with_details: when true, also fetch photo_url and mobile_phone.
    """
    digits = re.sub(r"\D", "", nif or "")
    if len(digits) != 9:
        return None
    client = _get_client()
    effective_club: int = (
        club_id if club_id is not None
        else int(client.session.get("organizacao") or 0)
    )
    if not effective_club:
        return None
    license = client.find_license_by_nif(digits, club_id=effective_club)
    if license is None:
        return None
    results = client.search_players(
        license=str(license), club=effective_club, with_details=with_details,
    )
    if not results:
        return None
    return player_to_dict(results[0], with_details=with_details)


@server.tool()
def get_player_profile(license: str, club_id: int | None = None) -> dict:
    """
    Read-only player profile suitable for OCR reconciliation.

    Single fetch from jogadoresdb.php?op=2 — the same data the enrollment
    wizard prefills from. Richer than get_player: includes address fields
    (morada, codpostal, localidade_txt, distrito, concelho), document IDs
    (numi, dataval, tipo), and contact details (tele, telef, email, nif).

    Distrito and concelho come back as integer ID strings; use
    list_associations / list_clubs-style consumers if names are needed
    (the distrito static map and concelho list are exposed elsewhere).

    club_id, when supplied, scopes the bridge search and avoids the slow
    federation-wide path on cache miss. Omit to use whatever's already
    cached from prior search_players / resolve_player calls.
    """
    client = _get_client()
    return client.load_player_profile(int(license), club_id=club_id)


# ── Clubs & associations ──────────────────────────────────────────────────────

@server.tool()
def list_associations() -> list[dict]:
    """List all associations registered in the SAV system."""
    client = _get_client()
    return [{"id": a.id, "name": a.name} for a in client.list_associations()]


@server.tool()
def list_clubs(association_id: int) -> list[dict]:
    """List clubs belonging to an association. Use list_associations to find association IDs."""
    client = _get_client()
    clubs = client.list_clubs(association=association_id)
    return [club_to_dict(c) for c in clubs]


# ── Coaches ───────────────────────────────────────────────────────────────────

@server.tool()
def list_coaches(
    club_id: int | None = None,
    season: int | None = None,
    status: str = "active",
    gender: int = 0,
    name: str = "",
    tptd: str = "",
    with_details: bool = False,
) -> list[dict]:
    """
    List coaches (treinadores) registered to a club for one season.

    club_id defaults to the session's own club when omitted.
    season defaults to the current epoch.
    status: "active" | "inactive" | "all" (default: active).
    gender: 0 = any, 1 = Masculino, 2 = Feminino.
    name: prefix match on full name (starts-with), not substring.
    tptd: filter by TPTD number; note the result rows do not include TPTD.
    with_details: when true, issue one extra request per coach to fill
        nif, tptd, and tptd_expiry in the returned rows. Off by default
        because it is N+1.
    """
    client = _get_client()
    effective_club: int = (
        club_id if club_id is not None
        else int(client.session.get("organizacao") or 0)
    )
    coaches = client.list_coaches(
        effective_club,
        season=season,
        status=status,
        gender=gender,
        name=name,
        tptd=tptd,
        with_details=with_details,
    )
    return [coach_to_dict(c, with_details=with_details) for c in coaches]


# ── Games & sheets ────────────────────────────────────────────────────────────

@server.tool()
def list_games(
    tier: str = "",
    gender: int = 0,
    date_from: str = "",
    date_to: str = "",
    status: str = "",
    season: int | None = None,
) -> list[dict]:
    """
    List games for the session's club, sorted by date (earliest first).

    date_from / date_to: DD-MM-YYYY format.
    status: e.g. "Marcado", "Não Marcado". Omit to return all statuses.
    """
    client = _get_client()
    # SAV2 ignores the inicio/fim date window server-side, leaking out-of-range
    # games, so we still send it (it narrows the payload when honored) but
    # guarantee the bounds with a client-side pass via filter_games.
    results = client.list_games(
        season=season, tier=tier, gender=gender,
        date_from=date_from, date_to=date_to,
    )
    results = filter_games(
        results, status=status, date_from=date_from, date_to=date_to,
    )
    return [game_to_dict(g) for g in sorted(results, key=game_sort_key)]


@server.tool()
def list_game_sheets(
    tier: str = "",
    date_from: str = "",
    date_to: str = "",
    competition: str = "",
    status: str = "",
    season: int | None = None,
) -> list[dict]:
    """
    List games that have or may have a game sheet available.

    date_from / date_to: DD-MM-YYYY format.
    competition: case-insensitive name fragment filter.
    status: game status filter (e.g. "Realizado"). Omit for all.
    """
    client = _get_client()
    results = filter_games(
        client.list_games(season=season, tier=tier),
        competition=competition, status=status,
        date_from=date_from, date_to=date_to,
    )
    return [game_to_dict(g) for g in results]


def _resolve_game(client: SavClient, game_number: str) -> Any:
    """Look up a game by its human-readable number. Raises ValueError if missing/unfetchable."""
    games = [g for g in client.list_games(game_number=game_number) if g.number == game_number]
    if not games:
        raise ValueError(f"No game found with number {game_number!r}")
    game = games[0]
    if game.id == 0:
        raise ValueError(f"Game {game_number!r} has no internal ID — cannot fetch sheet")
    return game


@server.tool()
def get_game_sheet(game_number: str, team: str) -> dict:
    """
    Return eligible players, coaches, and staff for one team in a game.

    game_number: the human-readable game number (from list_games).
    team: "home" or "away".
    """
    if team not in ("home", "away"):
        raise ValueError("team must be 'home' or 'away'")

    client = _get_client()
    game = _resolve_game(client, game_number)
    val = 1 if team == "home" else 2
    data = client.get_eligible_players(game.id, val=val)

    return {
        "game_number": game.number,
        "team": team,
        "team_name": game.home if team == "home" else game.away,
        "date": game.date,
        "players": data.get("players", []),
        "coaches_pri": data.get("coaches_pri", []),
        "coaches_adj": data.get("coaches_adj", []),
        "staff": data.get("staff", []),
    }


@server.tool()
def generate_game_sheet_pdf(
    game_number: str,
    team: str,
    player_licences: list[int] | None = None,
    coaches_pri: list[int] | None = None,
    coaches_adj: list[int] | None = None,
) -> dict:
    """
    Generate the eligible-players PDF for one team and return it base64-encoded.

    game_number: the human-readable game number (from list_games).
    team: "home" or "away".
    player_licences: licence numbers to include. Omit to include every eligible player.
    coaches_pri: head-coach wallet numbers to include. Omit to include all eligible.
    coaches_adj: adjunct-coach wallet numbers to include. Omit to include all eligible.

    Returns ``{filename, size_bytes, pdf_b64}``. Decode pdf_b64 to obtain the PDF bytes.
    """
    if team not in ("home", "away"):
        raise ValueError("team must be 'home' or 'away'")

    client = _get_client()
    game = _resolve_game(client, game_number)
    val = 1 if team == "home" else 2

    pdf = client.get_eligible_players_pdf(
        game.id, val=val,
        player_licences=player_licences,
        coaches_pri=coaches_pri,
        coaches_adj=coaches_adj,
    )
    if pdf is None:
        raise ValueError(f"No eligible-players PDF available for the {team} team of game {game_number!r}")

    return {
        "filename": f"game_{game.number}_{team}.pdf",
        "size_bytes": len(pdf),
        "pdf_b64": base64.b64encode(pdf).decode("ascii"),
    }


# ── Registration batches ──────────────────────────────────────────────────────

@server.tool()
def list_tiers(gender_id: int) -> list[dict]:
    """
    List the registration tiers (escalões) available for a given gender.

    The tier set differs by gender (some categories are male- or female-only),
    so the gender_id (1=Masculino, 2=Feminino) is required.

    Use this when the LLM needs a valid tier_id for create_batch /
    find_open_batch without first parsing an enrollment PDF. Cached 7 days
    server-side.
    """
    client = _get_client()
    tiers = client.list_player_registration_tiers(gender_id=gender_id)
    return [{"tier_id": tid, "tier_name": name} for tid, name in tiers.items()]


@server.tool()
def roster_for_escalao(
    tier_id: int,
    gender_id: int,
    when: str = "next",
    season_year: int | None = None,
    club_id: int | None = None,
) -> dict:
    """
    Resolve the roster of players for an escalão in a current, past, or upcoming season.

    Designed for natural roster questions ("Que jogadores são Sub-14 masculinos
    próxima época?") so the LLM doesn't have to compute birth-year arithmetic,
    handle the season transition, or override status filters by hand. Birth
    years are resolved deterministically from ``tier_id`` + the target
    season's start year, and the tool runs a fallback cascade so empty
    club-scoped results silently expand:

        (a) session/given club + status="active"
        (b) session/given club + status="all"           ← not-yet-renewed
        (c) club_id=0 + status="all" (federation)       ← wider fallback

    The first non-empty step wins; the ``step`` label tells the caller which
    path matched.

    The target season comes from ``season_year`` when given (an absolute season,
    e.g. ``2020`` for "2020/2021"), otherwise from ``when`` (``"current"`` or
    ``"next"``). Three regimes follow from how the target relates to today:

    - **Past or current season** → actual enrollment. The tool queries that
      season's own SAV2 epoch and reports ``is_projection=False`` with
      ``source`` in {``"club"``, ``"federation"``, ``"none"``}.
    - **Future season** (``when="next"`` or a ``season_year`` ahead of today)
      → a *projection*, not a query for next-season enrollment: enrollment only
      ever exists for the current season, so there is nothing to fetch. The
      tool takes the players we already know and keeps those whose birth year
      falls into the requested tier's window for that season, flagging the
      result with ``is_projection=True`` and
      ``source="projection_by_birth_year"`` so callers phrase it honestly ("os
      jogadores que, pela idade, passam a Sub-14"). An empty ``players`` list
      then simply means no known player projects into that cohort — not that
      enrollment data is missing.

    Args:
        tier_id: Numeric escalão ID. From ``list_tiers(gender_id)`` or
            ``parse_enrollment_forms``. The mapping varies by gender.
        gender_id: 1 = Masculino, 2 = Feminino.
        when: "current" or "next" — a season *relative* to today, resolved
            server-side. "next" advances the season by 1. Defaults to "next"
            because that's the common roster-planning question. Use this (not
            ``season_year``) for "current/próxima época" questions: the caller
            does not need to know today's season, and it avoids the
            calendar-year-vs-season trap. Ignored when ``season_year`` is given.
        season_year: Absolute target season, as its start year (``2020`` means
            "2020/2021"). Overrides ``when``. Use it only when the user names a
            specific season ("em 2020/2021"). A past or current year reflects
            actual enrollment; a future year is a birth-year projection just
            like ``when="next"``.
        club_id: Defaults to the session's club. Pass an explicit ID to query
            another club; the cascade still applies. Pass 0 to skip straight
            to federation-wide.

    Returns:
        {
          "tier": str,
          "tier_id": int,
          "gender_id": int,
          "season": str,            # "YYYY/YYYY+1"
          "birth_years": list[int] | None,  # None for open-ended tiers (Sénior)
          "is_projection": bool,    # True for a future season (by birth year)
          "source": str,            # "projection_by_birth_year" (future season)
                                    #   | "club" | "federation" | "none"
          "step": str,              # short label of the matching cascade step
          "players": list[dict],
        }

    Raises:
        ValueError: ``tier_id`` not valid for ``gender_id``; ``when`` not in
            {"current", "next"}; tier's birth-year window not modelled
            (Masters/Veteranos, BCR — query ``search_players`` with ``tier``
            directly).
        SavResponseError: Cannot resolve the current season's start year.
    """
    if when not in ("current", "next"):
        raise ValueError(f"when must be 'current' or 'next', got {when!r}")

    tiers = player_registration_tiers(gender_id)
    tier_name = tiers.get(tier_id)
    if tier_name is None:
        raise ValueError(
            f"tier_id={tier_id} not valid for gender_id={gender_id}. "
            f"Use list_tiers(gender_id) to discover valid IDs."
        )

    client = _get_client()
    current_year = client.get_current_season_start_year()
    current_epoca_id = int(client.session.get("epoca_id") or 0)

    # season_year, when given, names an absolute season and overrides the
    # relative `when`. SAV2 epoca_id is sequential, so a season's epoch is the
    # current one shifted by the year delta.
    if season_year is not None:
        target_year = season_year
    else:
        target_year = current_year + (1 if when == "next" else 0)
    target_epoca_id = current_epoca_id + (target_year - current_year)
    season_str = f"{target_year}/{target_year + 1}"

    # A future season has no enrollment to fetch, so it can only be a projection
    # of the current pool forward by birth year. The current season and any past
    # season reflect actual enrollment, so we query that season's own epoch.
    is_projection = target_year > current_year
    query_epoca_id = current_epoca_id if is_projection else target_epoca_id

    birth_years = tier_birth_years_for_season(tier_name, target_year)
    if birth_years is None and tier_name not in TIER_MIN_AGE_IN_SEASON:
        raise ValueError(
            f"Birth-year window for {tier_name!r} is not modelled. "
            f"Query search_players(tier={tier_name!r}, gender_id={gender_id}, ...) "
            f"directly."
        )

    effective_club: int = (
        club_id if club_id is not None
        else int(client.session.get("organizacao") or 0)
    )

    cascade: list[tuple[str, str, dict[str, Any]]] = []
    if effective_club != 0:
        cascade.append(("club + active", "club", {
            "club": effective_club, "status": "active",
        }))
        cascade.append(("club + all", "club", {
            "club": effective_club, "status": "all",
        }))
    cascade.append(("federation + all", "federation", {
        "club": 0, "status": "all",
    }))

    common: dict[str, Any] = {
        "gender": gender_id,
        "season": query_epoca_id,
        "with_details": False,
    }
    if birth_years is not None:
        common["birth_year"] = birth_years
    else:
        common["tier"] = tier_name

    chosen_label = cascade[-1][0]
    chosen_source = "none"
    chosen_players: list[Any] = []
    for label, source, kw in cascade:
        try:
            players = client.search_players(**common, **kw)
        except (SavResponseError, ValueError):
            logger.debug("roster_for_escalao step %r failed", label, exc_info=True)
            continue
        if players:
            chosen_label, chosen_source, chosen_players = label, source, players
            break

    # A future season is a projection over the current pool, so the source
    # reflects the projection rather than where the players were found (the
    # cascade step still carries that). An empty roster then means "no player
    # projects into this cohort", which is honest — never "none"/"missing data".
    result_source = "projection_by_birth_year" if is_projection else chosen_source

    return {
        "tier": tier_name,
        "tier_id": tier_id,
        "gender_id": gender_id,
        "season": season_str,
        "birth_years": birth_years,
        "is_projection": is_projection,
        "source": result_source,
        "step": chosen_label,
        "players": [player_to_dict(p) for p in chosen_players],
    }


@server.tool()
def list_batches(season: int | None = None) -> list[dict]:
    """
    List player registration batches visible to the session's club.

    Includes all states (Em construção, Devolvida, Em Validação, Em Pagamento).
    season defaults to the current season when omitted.
    """
    client = _get_client()
    batches = client.list_player_registration_batches(season=season)
    return [batch_to_dict(b) for b in batches]


@server.tool()
def get_batch(batch_number: str, season: int | None = None) -> dict | None:
    """
    Fetch a single registration batch by its human-visible number.

    season defaults to the current season; pass 0 to search across all seasons.
    Returns the batch details (same shape as list_batches entries) or null if
    no batch matches.
    """
    client = _get_client()
    batches = client.list_player_registration_batches(season=season)
    batch = next((b for b in batches if b.number == batch_number), None)
    if batch is None:
        return None
    return batch_to_dict(batch)


# ── Enrollment workflow ───────────────────────────────────────────────────────
# In-memory OCR artifact cache. Historically this held only enrollment forms,
# so the variable name remains `_forms` for compatibility with older tests and
# callers. Keys are artifact ids (UUID strings); fpb_modelo_1 results also
# expose that id as `mod1_id`, and exame_medico results expose it as
# `medical_exam_id`.

_forms: dict[str, dict[str, Any]] = {}


def _build_preview_fields(result: Any, sav_profile: dict) -> list[dict]:
    """
    Build the full field list for preview_enrollment.

    Every kwarg in result.kwargs gets an entry with a status:
      updated      — OCR value overrides SAV
      match        — SAV value kept (OCR was close enough)
      needs_review — low OCR confidence, user must decide
      ocr          — field not reconciled against SAV (id_type, guardian_*, consent_*)
    """
    fields = []
    shown: set[str] = set()

    for kwarg, (sav_val, ocr_val) in result.updated.items():
        label, _ = ENROLLMENT_FIELD_META.get(kwarg, (kwarg, ""))
        fields.append({
            "kwarg": kwarg, "label": label,
            "sav_value": sav_val, "ocr_value": ocr_val,
            "final_value": ocr_val, "status": "updated",
        })
        shown.add(kwarg)

    for kwarg, (sav_val, ocr_val, sim) in result.kept.items():
        label, _ = ENROLLMENT_FIELD_META.get(kwarg, (kwarg, ""))
        fields.append({
            "kwarg": kwarg, "label": label,
            "sav_value": sav_val, "ocr_value": ocr_val,
            "final_value": sav_val, "status": "match",
            "similarity": round(sim, 2),
        })
        shown.add(kwarg)

    for kwarg in result.needs_review:
        label, sav_key = ENROLLMENT_FIELD_META.get(kwarg, (kwarg, ""))
        sav_val = str(sav_profile.get(sav_key) or "") or None if sav_key else None
        ocr_val = result.kwargs.get(kwarg)
        fields.append({
            "kwarg": kwarg, "label": label,
            "sav_value": sav_val, "ocr_value": ocr_val,
            "final_value": None, "status": "needs_review",
        })
        shown.add(kwarg)

    for kwarg, value in result.kwargs.items():
        if kwarg in shown or kwarg == "license" or value is None:
            continue
        label, _ = ENROLLMENT_FIELD_META.get(kwarg, (kwarg, ""))
        fields.append({
            "kwarg": kwarg, "label": label,
            "sav_value": None, "ocr_value": value,
            "final_value": value, "status": "ocr",
        })

    return fields


def _build_medical_exam_payload(artifact_id: str, artifact: dict[str, Any]) -> dict:
    """Serialize a cached EM OCR artifact for MCP callers."""
    info = extract_medical_exam_info(artifact["parsed"])
    return {
        "artifact_id": artifact_id,
        "medical_exam_id": artifact_id,
        "doc_type": artifact["doc_type"].value,
        "exam_date": info.exam_date,
        "raw_exam_date": info.raw_exam_date,
        "exam_date_confidence": info.exam_date_confidence,
        "doctor_validation_present": info.doctor_validation_present,
        "needs_review": info.exam_date is None,
    }


def _replace_player_document_from_bytes(
    client: SavClient,
    batch_id: int,
    license: int,
    pdf_bytes: bytes | None,
    *,
    doc_type: DocType,
    parsed: dict | None = None,
    reg_type: int | None = None,
) -> dict[str, Any]:
    """Upload cached PDF bytes as a replacement registration document.

    `parsed` is the fpb_modelo_1 fields dict from parse_fpb_mod1 (when
    available); used to decide whether to overlay the club stamp and/or
    the inscription checkbox mark.  `reg_type` (1 or 2) drives the
    inscription overlay — pass it when known (enrollment create flow).
    """
    # has_club_stamp / stamp_warning / has_inscricao_mark / inscricao_warning
    # describe the uploaded PDF, so they're only added to status when
    # status == "ok"; on "skipped" / "error" there's no uploaded PDF to describe.
    status = {
        "doc_type": doc_type.value,
        "status": "skipped",
        "error": None,
    }
    if not pdf_bytes:
        return status

    is_mod1 = doc_type == DocType.FPB_MODELO_1 and parsed
    carimbo, carimbo_bbox = read_carimbo(parsed) if is_mod1 else (None, None)
    tipo_checked, tipo_bbox = (
        read_tipo_inscricao(parsed, reg_type)
        if (is_mod1 and reg_type is not None) else (None, None)
    )
    tmp_path: str | None = None
    try:
        tmp_path = _pdf_bytes_to_tempfile(pdf_bytes)
        with overlaid_pdf(
            tmp_path,
            inscricao_overlay(reg_type=reg_type, already_checked=tipo_checked, bbox=tipo_bbox),
            carimbo_overlay(carimbo_present=carimbo, bbox=carimbo_bbox),
        ) as (upload_path, (inscricao_r, carimbo_r)):
            ok, error = try_replace_document(
                client, batch_id, license, upload_path,
                tipo_doc=doc_type_to_tipo_doc(doc_type),
            )
            status["status"] = "ok" if ok else "error"
            status["error"] = error
            if ok:
                status["has_club_stamp"] = carimbo_r.effective
                status["stamp_warning"] = (
                    f"{carimbo_r.error} — document uploaded without the club stamp; "
                    "please stamp it manually."
                ) if carimbo_r.error else None
                status["has_inscricao_mark"] = inscricao_r.effective
                status["inscricao_warning"] = (
                    f"{inscricao_r.error} — please mark the inscription checkbox manually."
                ) if inscricao_r.error else None
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
    return status


@server.tool()
def parse_enrollment_forms(
    pdfs: list[str],
    doc_types: list[str | None] | None = None,
) -> list[dict]:
    """
    Parse one or more enrollment-related PDFs provided as base64-encoded bytes.

    fpb_modelo_1 forms are parsed for the main enrollment workflow and return
    the batch parameters (registration type, tier, gender). exame_medico
    documents are parsed for step-3 metadata and return a medical_exam_id that
    can be passed to preview_enrollment / submit_enrollment. fpb_modelo_4 forms
    carry no fields — alongside an fpb_modelo_1 their presence adds an inline
    subida de escalão; they return a mod4_id to pass to preview/submit_enrollment.

    doc_types: optional per-PDF type hint list (same length as pdfs). When an
    entry is "fpb_modelo_1", "exame_medico", or "fpb_modelo_4", classification
    is skipped and the classifier is trained with the known label. Use None or
    omit the list to auto-classify every PDF.

    Returns one entry per PDF with an artifact_id and canonical doc_type to
    reference in subsequent tools. fpb_modelo_1 entries also include mod1_id;
    exame_medico entries also include medical_exam_id; fpb_modelo_4 entries
    also include mod4_id. On error for a given PDF the entry contains an
    "error" key instead.
    """
    from sav_parsers import classify, parse_em, parse_fpb_mod1, parse_fpb_mod4, train_classifier

    client = _get_client()
    results: list[dict] = []

    for i, pdf_b64 in enumerate(pdfs):
        try:
            pdf_bytes = base64.b64decode(pdf_b64)
        except (binascii.Error, ValueError) as exc:
            results.append({"index": i, "error": f"Invalid base64: {exc}"})
            continue

        hint: str | None = (doc_types[i] if doc_types and i < len(doc_types) else None)

        tmp_path: str | None = None
        try:
            tmp_path = _pdf_bytes_to_tempfile(pdf_bytes)

            if hint is not None:
                # Type is already known — skip classify and train the classifier.
                _hint_map = {
                    "fpb_modelo_1": DocType.FPB_MODELO_1,
                    "exame_medico": DocType.EXAME_MEDICO,
                    "fpb_modelo_4": DocType.FPB_MODELO_4,
                }
                if hint not in _hint_map:
                    results.append({"index": i, "error": f"Unknown doc_type hint: {hint!r}"})
                    continue
                doc_type = _hint_map[hint]
                try:
                    train_classifier(tmp_path, doc_type)
                except Exception:
                    logger.debug("train_classifier failed for hint=%r", hint, exc_info=True)
            else:
                doc_type = classify(tmp_path)

            if doc_type == DocType.FPB_MODELO_1:
                parse_result = parse_fpb_mod1(tmp_path)
                parsed = parse_result["fields"]
                processing_id = parse_result["processing_id"]
                reg_type, tier_id, gender_id = derive_enrollment_params(parsed, client)
                tiers = client.list_player_registration_tiers(gender_id=gender_id)
                tier_name = tiers.get(tier_id, str(tier_id))
            elif doc_type == DocType.EXAME_MEDICO:
                parse_result = parse_em(tmp_path)
                parsed = parse_result["fields"]
                processing_id = parse_result["processing_id"]
                try:
                    train_classifier(tmp_path, DocType.EXAME_MEDICO)
                except Exception:
                    logger.debug("train_classifier failed for EM", exc_info=True)
            elif doc_type == DocType.FPB_MODELO_4:
                # Mod4 carries nome_jogador (mandatory), licenca_nr (optional),
                # escalao_actual, escalao_subida, and the club-signature signal —
                # enough to drive a standalone Subida without --batch / --license.
                parse_result = parse_fpb_mod4(tmp_path)
                parsed = parse_result["fields"]
                processing_id = parse_result["processing_id"]
            else:
                results.append({"index": i, "error": f"Unsupported document type: {doc_type.value!r}"})
                continue
        except (SavError, ValueError, KeyError, OSError) as exc:
            results.append({"index": i, "error": str(exc)})
            continue
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

        artifact_id = str(uuid.uuid4())
        artifact = {
            "parsed": parsed,
            "processing_id": processing_id,
            "doc_type": doc_type,
            "pdf_bytes": pdf_bytes,
        }
        if doc_type == DocType.FPB_MODELO_1:
            artifact.update({
                "reg_type": reg_type,
                "tier_id": tier_id,
                "gender_id": gender_id,
            })
        _forms[artifact_id] = artifact

        if doc_type == DocType.FPB_MODELO_1:
            results.append({
                "index": i,
                "artifact_id": artifact_id,
                "mod1_id": artifact_id,
                "doc_type": doc_type.value,
                "reg_type": reg_type,
                "reg_type_label": REGISTRATION_TYPE_LABELS.get(reg_type, str(reg_type)),
                "tier_id": tier_id,
                "tier_name": tier_name,
                "gender_id": gender_id,
                "gender_label": GENERO.get(gender_id, str(gender_id)),
            })
        elif doc_type == DocType.FPB_MODELO_4:
            def _f(key: str) -> Any:
                pf = parsed.get(key)
                return pf.value if pf else None
            results.append({
                "index": i,
                "artifact_id": artifact_id,
                "mod4_id": artifact_id,
                "doc_type": doc_type.value,
                "nome_jogador": _f("nome_jogador"),
                "licenca_nr": _f("licenca_nr"),
                "escalao_actual": _f("escalao_actual"),
                "escalao_subida": _f("escalao_subida"),
            })
        else:
            payload = _build_medical_exam_payload(artifact_id, artifact)
            payload.update({"index": i})
            results.append(payload)

    return results


@server.tool()
def find_open_batch(reg_type: int, tier_id: int, gender_id: int) -> dict | None:
    """
    Find an existing open ("Em construção") registration batch matching the
    given type, tier, and gender.  Returns batch details or null if none exists.
    """
    client = _get_client()
    batch = client.find_open_player_registration_batch(
        type=reg_type, tier_id=tier_id, gender_id=gender_id,
    )
    if batch is None:
        return None
    return {
        "number": batch.number,
        "type": batch.type,
        "tier": batch.tier,
        "gender": batch.gender,
        "item_count": batch.item_count,
    }


@server.tool()
def create_batch(reg_type: int, tier_id: int, gender_id: int) -> dict:
    """
    Create a new registration batch for the given type, tier, and gender.
    Returns the new batch details including its human-visible batch number.
    """
    client = _get_client()
    _, batch = create_and_fetch_batch(
        client, batch_type=reg_type, tier_id=tier_id, gender_id=gender_id,
    )
    return {
        "number": batch.number,
        "type": batch.type,
        "tier": batch.tier,
        "gender": batch.gender,
        "item_count": batch.item_count,
    }


@server.tool()
def resolve_player(batch_number: str, mod1_id: str) -> dict:
    """
    Resolve the player for a parsed form against the batch.

    batch_number is the human-visible batch number (as shown in the SAV2 UI).

    For Revalidação (type 2): tries the OCR licence number first, then falls
    back to a name search scoped to the batch's club.

    For 1ª Inscrição (type 1): the player doesn't exist in SAV yet, so
    there's no eligibility list to match against. Returns ``{resolved: true,
    license: null, reg_type: 1, ocr_name, ocr_birth_date, ocr_gender_id}``
    so the caller proceeds directly to preview_enrollment / submit_enrollment.
    When the OCR yielded enough identifying data (gender + birth date + id
    number) the server's op=11 duplicate check is fired pre-emptively — a
    match means the player already has a SAV record and Revalidação is the
    right path, so we return ``{resolved: false, error:
    "player_already_in_sav"}`` to short-circuit before the LLM walks the
    create-player wizard.

    Returns:
      resolved=true + license  when exactly one revalidação match is found.
      resolved=true + license:null + reg_type:1  for a fresh 1ª Inscrição.
      resolved=false + candidates  when multiple players match (user must pick).
      resolved=false + error  when 1ª Inscrição duplicate is detected.
      resolved=false + empty candidates  when no match found (user must supply licence).
    """
    form = _forms.get(mod1_id)
    if form is None:
        raise ValueError(f"Unknown mod1_id: {mod1_id!r}")
    if form.get("doc_type") != DocType.FPB_MODELO_1:
        raise ValueError(f"Artifact {mod1_id!r} is not an fpb_modelo_1 enrollment form")

    client = _get_client()
    batches = client.list_player_registration_batches()
    batch = next((b for b in batches if b.number == batch_number), None)
    if batch is None:
        raise ValueError(f"Batch {batch_number!r} not found")

    if batch.type_id == 1:
        parsed = form["parsed"]
        name_f = parsed.get("nome_completo")
        bd_f = parsed.get("data_nascimento")
        id_f = parsed.get("num_doc_identificacao")
        ocr_name = str(name_f.value) if name_f and name_f.value else None
        ocr_birth = str(bd_f.value) if bd_f and bd_f.value else None
        ocr_id = str(id_f.value) if id_f and id_f.value else None
        # Default to masculino when neither checkbox parsed (mirrors the
        # downstream wizard default, so the duplicate probe still works).
        gender_id = 2 if parsed_bool(parsed, "genero_feminino") else 1

        # Pre-emptive duplicate guard: only when OCR yielded all three
        # identifying fields. A miss here is non-fatal — the wizard's own
        # op=11 call at commit time will catch the case if we let it through.
        if ocr_birth and ocr_id:
            try:
                dup = client._check_primeira_player_duplicate(
                    gender_id=gender_id, birth_date=ocr_birth, id_number=ocr_id,
                )
            except (SavError, ValueError):
                logger.debug("Pre-emptive op=11 failed at resolve time", exc_info=True)
                dup = {"existe": 0}
            if int(dup.get("existe", 0)) != 0:
                return {
                    "resolved": False,
                    "license": None,
                    "reg_type": 1,
                    "error": "player_already_in_sav",
                    "reason": (
                        "A player matching the OCR'd identifying data already "
                        "exists in SAV. 1ª Inscrição is for players not yet in "
                        "the federation — use Revalidação on the existing licence."
                    ),
                    "existing_sav_id": dup.get("id") or dup.get("atleta") or None,
                }

        return {
            "resolved": True,
            "license": None,
            "reg_type": 1,
            "ocr_name": ocr_name,
            "ocr_birth_date": ocr_birth,
            "ocr_gender_id": gender_id,
            "candidates": [],
        }

    eligible = client._list_revalidable_licenses(batch)
    license, candidates, ocr_name, ocr_license = resolve_player_candidates(
        form["parsed"], eligible, client, batch.club_id,
    )

    if license is not None:
        return {"resolved": True, "license": license, "candidates": []}

    return {
        "resolved": False,
        "license": None,
        "candidates": [
            {"license": int(p.license), "name": p.name, "birth_date": p.birth_date}
            for p in candidates
        ],
        "ocr_name": ocr_name,
        "ocr_license": ocr_license,
    }


@server.tool()
def resolve_subida_target(mod4_id: str) -> dict:
    """
    Resolve the Subida target for a parsed mod4: licence, destination tier_id,
    and gender_id, walking the same pipeline the CLI uses.

      - licença from licenca_nr when present (validated via SAV);
      - else a name search inside the session's club.
    Once a licence is known, the player's gender is fetched and the
    destination tier_id is mapped from `escalao_subida` against the
    gender-scoped tier table.

    Use the result to call find_open_batch / create_batch (reg_type=4) and
    then submit_subida_enrollment.

    Returns:
      resolved=true + license + tier_id + tier_name + gender_id + gender_label
        when a single player is identified.
      resolved=false + candidates  when the name search returns multiple hits
        (caller picks).
      resolved=false + empty candidates  when no candidate is found (caller
        supplies a licence directly).
    """
    form = _forms.get(mod4_id)
    if form is None:
        raise ValueError(f"Unknown mod4_id: {mod4_id!r}")
    if form.get("doc_type") != DocType.FPB_MODELO_4:
        raise ValueError(f"Artifact {mod4_id!r} is not an fpb_modelo_4 form")

    client = _get_client()
    club_id = int(client.session.get("organizacao") or 0) if client.session else 0
    license, candidates, ocr_name, ocr_license = resolve_subida_player(
        form["parsed"], client, club_id=club_id,
    )
    if license is None:
        return {
            "resolved": False,
            "license": None,
            "candidates": [
                {
                    "license": int(p.license),
                    "name": p.name,
                    "gender": p.gender,
                    "birth_date": p.birth_date,
                }
                for p in candidates
            ],
            "ocr_name": ocr_name,
            "ocr_license": ocr_license,
        }

    gender_id = gender_id_for_license(client, license)
    tier_id = resolve_subida_tier(form["parsed"], client, gender_id=gender_id)
    tiers = client.list_player_registration_tiers(gender_id=gender_id)
    return {
        "resolved": True,
        "license": license,
        "tier_id": tier_id,
        "tier_name": tiers.get(tier_id, str(tier_id)),
        "gender_id": gender_id,
        "gender_label": GENERO.get(gender_id, str(gender_id)),
        "reg_type": REGISTRATION_TYPE_SUBIDA,
    }


@server.tool()
def preview_enrollment(
    batch_number: str,
    license: int | None,
    mod1_id: str,
    medical_exam_id: str | None = None,
    mod4_id: str | None = None,
    nif: str | None = None,
) -> dict:
    """
    Preview the enrollment for a player.

    batch_number is the human-visible batch number (as shown in the SAV2 UI).

    Revalidação (license is a real SAV licence): fetches the player's current
    SAV profile, runs OCR reconciliation, and returns the full field-by-field
    picture:
      - Fields that match SAV (status: match) — shown for transparency
      - Fields where OCR overrides SAV (status: updated)
      - Fields where OCR confidence is too low to trust (status: needs_review)
        — the user should confirm or correct these before submitting
      - Fields with no SAV equivalent (status: ocr) — id_type, guardian_*, consent_*

    1ª Inscrição (license is null/0 — the resolve_player step returned
    reg_type=1): there is no SAV profile to reconcile against. The preview
    echoes the OCR'd fields as-is, marking any required-but-missing or
    low-confidence reads as needs_review so the caller supplies them via
    field_overrides on submit_enrollment.

    The reconciliation result is cached internally so submit_enrollment can
    use it without repeating the network call. When medical_exam_id is
    supplied, the response also includes a `medical_exam` sidecar with the
    parsed step-3 exam metadata.

    The response always states the enrollment route so it can be confirmed
    before submit: `reg_type` (1/2) + `reg_type_label`, `inline_subida` (true
    when mod4_id is supplied → the player is also promoted right away), and a
    plain-language `enrollment_route`. Pass the same mod4_id to
    submit_enrollment to actually commit the inline subida.

    nif: optional explicit subject claim — the athlete's NIF that the caller
    asserts this enrollment is for. Used by downstream wrappers to enforce
    self-scope on 1ª Inscrição (where there is no licence yet) and as a
    defense-in-depth cross-check against the form's OCR'd NIF: if both are
    set and disagree, the call is rejected.

    batch_number is accepted for workflow symmetry; only the form/license are
    needed at this stage. It is validated when submit_enrollment is called.
    """
    del batch_number
    form = _forms.get(mod1_id)
    if form is None:
        raise ValueError(f"Unknown mod1_id: {mod1_id!r}")
    if form.get("doc_type") != DocType.FPB_MODELO_1:
        raise ValueError(f"Artifact {mod1_id!r} is not an fpb_modelo_1 enrollment form")
    _verify_nif_claim(form, nif)

    client = _get_client()
    reg_type = form.get("reg_type")
    inline_subida = mod4_id is not None
    if reg_type is not None:
        validate_subida_combo(reg_type, inline_subida)
    reg_type_label = REGISTRATION_TYPE_LABELS.get(reg_type, str(reg_type))
    enrollment_route = (
        f"Will promote during the {reg_type_label} (inline subida de escalão)"
        if inline_subida
        else f"{reg_type_label} (no subida)"
    )

    if reg_type == 1:
        # No SAV profile, no reconciliation — echo the OCR fields and mark
        # missing-required / low-confidence as needs_review.
        # concelhos lookup so concelho_id resolves; distrito is OCR-derived
        # so we only fetch when OCR yielded a distrito.
        parsed = form["parsed"]
        from sav_shared.fpb_mod1 import effective_distrito_id
        distrito_id = effective_distrito_id(parsed, {})
        concelhos = client.list_concelhos(distrito_id) if distrito_id else {}
        kwargs = build_primeira_kwargs(parsed, concelhos=concelhos)
        fields, needs_review = build_primeira_preview_fields(parsed, kwargs)
        form["primeira_kwargs"] = kwargs
        form["primeira_concelhos"] = concelhos
        form["previewed"] = True
        preview = {
            "player": {
                "name": kwargs.get("name") or "",
                "license": None,
                "birth_date": kwargs.get("birth_date") or "",
            },
            "fields": fields,
            "needs_review": needs_review,
            "reg_type": reg_type,
            "reg_type_label": reg_type_label,
            "inline_subida": inline_subida,
            "enrollment_route": enrollment_route,
        }
    else:
        if license in (None, 0):
            raise ValueError(
                f"Revalidação preview requires a non-zero license; got {license!r}."
            )
        # resolve_player runs first in this workflow → search_players already
        # populated the license→id cache, so this is free.
        sav_profile = client.load_player_profile(license)
        result = reconcile_fpb_mod1(form["parsed"], sav_profile, client=client)

        form["reconcile_result"] = result
        form["sav_profile"] = sav_profile
        form["previewed"] = True

        preview = {
            "player": {
                "name": sav_profile.get("nome", ""),
                "license": license,
                "birth_date": sav_profile.get("nasc", ""),
            },
            "fields": _build_preview_fields(result, sav_profile),
            "needs_review": result.needs_review,
            "reg_type": reg_type,
            "reg_type_label": reg_type_label,
            "inline_subida": inline_subida,
            "enrollment_route": enrollment_route,
        }
    if medical_exam_id is not None:
        artifact = _forms.get(medical_exam_id)
        if artifact is None:
            raise ValueError(f"Unknown medical_exam_id: {medical_exam_id!r}")
        if artifact.get("doc_type") != DocType.EXAME_MEDICO:
            raise ValueError(f"Artifact {medical_exam_id!r} is not an exame_medico parse")
        preview["medical_exam"] = _build_medical_exam_payload(medical_exam_id, artifact)
    if mod4_id is not None:
        mod4 = _forms.get(mod4_id)
        if mod4 is None:
            raise ValueError(f"Unknown mod4_id: {mod4_id!r}")
        if mod4.get("doc_type") != DocType.FPB_MODELO_4:
            raise ValueError(f"Artifact {mod4_id!r} is not an fpb_modelo_4 form")
    return preview


@server.tool()
def submit_enrollment(
    batch_number: str,
    license: int | None,
    mod1_id: str,
    field_overrides: dict[str, Any] | None = None,
    medical_exam_id: str | None = None,
    mod4_id: str | None = None,
    nif: str | None = None,
) -> dict:
    """
    Submit the player enrollment using the data prepared by preview_enrollment.

    batch_number is the human-visible batch number (as shown in the SAV2 UI).

    Revalidação (license is a real SAV licence): the reconciled kwargs from
    preview_enrollment are used; field_overrides supply values for every
    field listed in needs_review.

    1ª Inscrição (license is null/0): the OCR-derived demographics from
    preview_enrollment are used; field_overrides supply values for the
    needs_review list (typically missing-or-low-confidence reads). The
    wizard's op=11 duplicate check inside the SAV client guards against
    accidentally creating a player who already exists.

    field_overrides should supply guardian fields required for minors
    (guardian_name, guardian_relation, guardian_phone, guardian_email) and
    must include exam_date (YYYY-MM-DD) when no usable medical exam date is
    available. It may also override any parsed exame_medico date when
    medical_exam_id is supplied.

    mod4_id (from parse_enrollment_forms) adds an inline subida de escalão to
    this 1ª Inscrição / Revalidação: the target tier is fetched from SAV and
    committed, and the mod4 is uploaded as a supporting document. Submitting
    fails if SAV offers no subida tier for the player. (This is the inline
    rider, not a standalone type-4 Subida batch.)

    nif: optional explicit subject claim — the athlete's NIF that the caller
    asserts this enrollment is for. Used by downstream wrappers to enforce
    self-scope on 1ª Inscrição (where there is no licence yet) and as a
    defense-in-depth cross-check against the form's OCR'd NIF: if both are
    set and disagree, the call is rejected.

    Returns:
      success=true + player_id (+ license for 1ª Inscrição) on success.
      success=false + missing_guardian_fields  when the player is a minor and
        guardian info is absent — call submit_enrollment again with those fields
        added to field_overrides.
      success=true also includes source_document_upload and
      medical_exam_upload with {doc_type, status, error}. When status=="ok"
      these also carry has_club_stamp (True/False/None — whether the
      uploaded PDF has the club stamp; None when no OCR ran) and
      stamp_warning (str when the overlay was attempted but failed, else
      None — surface it so the user can stamp manually).
    """
    from sav_parsers import close_processing

    form = _forms.get(mod1_id)
    if form is None:
        raise ValueError(f"Unknown mod1_id: {mod1_id!r}")
    if form.get("doc_type") != DocType.FPB_MODELO_1:
        raise ValueError(f"Artifact {mod1_id!r} is not an fpb_modelo_1 enrollment form")
    _verify_nif_claim(form, nif)

    reg_type = form.get("reg_type")
    # Revalidação caches a ReconcileResult; type-1 sets `previewed: True`
    # (no reconcile to cache). Either signal counts as "preview ran".
    previewed = form.get("previewed") or (form.get("reconcile_result") is not None)
    if not previewed:
        raise ValueError("Call preview_enrollment before submit_enrollment")

    medical_exam: dict[str, Any] | None = None
    medical_exam_info = None
    if medical_exam_id is not None:
        medical_exam = _forms.get(medical_exam_id)
        if medical_exam is None:
            raise ValueError(f"Unknown medical_exam_id: {medical_exam_id!r}")
        if medical_exam.get("doc_type") != DocType.EXAME_MEDICO:
            raise ValueError(f"Artifact {medical_exam_id!r} is not an exame_medico parse")
        medical_exam_info = extract_medical_exam_info(medical_exam["parsed"])

    mod4: dict[str, Any] | None = None
    if mod4_id is not None:
        mod4 = _forms.get(mod4_id)
        if mod4 is None:
            raise ValueError(f"Unknown mod4_id: {mod4_id!r}")
        if mod4.get("doc_type") != DocType.FPB_MODELO_4:
            raise ValueError(f"Artifact {mod4_id!r} is not an fpb_modelo_4 form")
    inline_subida = mod4 is not None
    if reg_type is not None:
        validate_subida_combo(reg_type, inline_subida)

    client = _get_client()

    # Build the wizard kwargs from the cached preview state. For Revalidação
    # this is the ReconcileResult; for 1ª Inscrição it's the OCR-derived dict.
    if reg_type == 1:
        kwargs = dict(form.get("primeira_kwargs") or {})
        needs_review: list[str] = []
        retrain_corrections: dict[str, str] = {}
    else:
        if license in (None, 0):
            raise ValueError(
                f"Revalidação submit requires a non-zero license; got {license!r}."
            )
        result = form.get("reconcile_result")
        if result is None:
            raise ValueError("Call preview_enrollment before submit_enrollment")
        kwargs = dict(result.kwargs)
        kwargs.pop("license", None)
        needs_review = result.needs_review
        retrain_corrections = result.retrain_corrections

    if medical_exam_info and medical_exam_info.exam_date:
        kwargs["exam_date"] = medical_exam_info.exam_date
    if mod4 is not None:
        # The mod4 names the target escalão — resolve it to a SAV tier_id and
        # hand it to the wizard as promote_to_tier_id. _pick_subida_tier
        # enforces that the form's stated target matches what SAV offers.
        # OCR miss on escalao_subida → skip the hint and let the wizard pick.
        # Type-1 has no licence yet, so we read gender from the OCR kwargs;
        # type-2 looks it up against SAV.
        escalao_field = mod4["parsed"].get("escalao_subida")
        if escalao_field and escalao_field.value:
            gender_for_subida = (
                kwargs.get("gender_id")
                if reg_type == 1
                else gender_id_for_license(client, license)
            )
            kwargs["promote_to_tier_id"] = resolve_subida_tier(
                mod4["parsed"], client, gender_id=gender_for_subida,
            )
    if field_overrides:
        kwargs.update(field_overrides)
    manual_exam_override = bool(
        field_overrides and field_overrides.get("exam_date") not in (None, "")
    )
    if medical_exam is not None and not kwargs.get("exam_date"):
        raise ValueError(
            "Medical exam OCR did not yield a usable exam_date; pass "
            "field_overrides={'exam_date': 'YYYY-MM-DD'}."
        )
    if not kwargs.get("exam_date"):
        raise ValueError(
            "Enrollment requires exam_date; pass "
            "field_overrides={'exam_date': 'YYYY-MM-DD'}."
        )

    batch_id = client.resolve_batch_id(batch_number)
    try:
        player_id = client.add_player_to_registration_batch(
            batch_id, license or 0, inline_subida=inline_subida, **kwargs,
        )
    except SavConfigError as exc:
        # Only minor/guardian errors are retry cases; they carry the field list
        # ("…missing required fields: …"). Other config errors (e.g. subida
        # requested but SAV offers no tier) are not retryable — surface them.
        if "missing required fields" not in str(exc):
            raise
        return {
            "success": False,
            "missing_guardian_fields": parse_missing_guardian_fields(exc),
        }

    # For 1ª Inscrição, SAV assigned a brand-new licence at commit time but
    # op=27 doesn't return it. Look it up by matching the just-created
    # player's name in the batch listing so the document uploads can target it.
    upload_license: int | None = license if license else None
    if reg_type == 1:
        name_supplied = (kwargs.get("name") or "").strip().casefold()
        try:
            for item in client.list_player_registration_batch_items(batch_id):
                if item["name"].strip().casefold() == name_supplied:
                    upload_license = int(item["license"])
                    break
        except (SavError, ValueError):
            logger.debug(
                "Could not resolve new licence for type-1 upload", exc_info=True,
            )

    # Auto-upload the source PDF as fpb_modelo_1 (parity with `sav enroll`).
    # Non-fatal: enrollment is already committed, so we just record the
    # outcome on the response and let the caller retry via
    # upload_player_document if it fails. For type-1, when the licence
    # lookup failed we skip the upload and surface a clear status.
    skipped_upload = {
        "doc_type": form["doc_type"].value, "status": "skipped",
        "error": "Could not resolve new licence after type-1 commit",
    }
    upload_status = (
        _replace_player_document_from_bytes(
            client, batch_id, upload_license, form.get("pdf_bytes"),
            doc_type=form["doc_type"],
            parsed=form.get("parsed"),
            reg_type=form.get("reg_type"),
        )
        if upload_license else skipped_upload
    )
    medical_exam_upload = (
        _replace_player_document_from_bytes(
            client, batch_id, upload_license,
            medical_exam.get("pdf_bytes"), doc_type=medical_exam["doc_type"],
        )
        if (medical_exam is not None and upload_license) else None
    )
    subida_document_upload = (
        _replace_player_document_from_bytes(
            client, batch_id, upload_license,
            mod4.get("pdf_bytes"), doc_type=mod4["doc_type"],
        )
        if (mod4 is not None and upload_license) else None
    )

    # Only send corrections the user explicitly answered (needs_review).
    # Updated/kept were silent paths — staging them risks dataset noise.
    # retrain_corrections are SAV-side truths for read-only fields (nif,
    # data_nascimento) — always merged so the labeled doc anchors to them.
    corrections: dict[str, str] = {}
    for kwarg in needs_review:
        entity = KWARG_TO_ENTITY.get(kwarg)
        val = kwargs.get(kwarg)
        if entity and val is not None:
            corrections[entity] = str(val)
    corrections.update(retrain_corrections)
    try:
        close_processing(form["processing_id"], corrections=corrections or None)
    except Exception:
        logger.debug("close_processing failed for form", exc_info=True)
    if medical_exam is not None:
        exam_corrections = {}
        if manual_exam_override and kwargs.get("exam_date") is not None:
            exam_corrections["exam_date"] = str(kwargs["exam_date"])
        try:
            close_processing(
                medical_exam["processing_id"],
                corrections=exam_corrections or None,
            )
        except Exception:
            logger.debug("close_processing failed for medical exam", exc_info=True)

    sav_profile = form.get("sav_profile", {})
    return {
        "success": True,
        "player_id": player_id,
        "license": upload_license,
        "name": (
            kwargs.get("name") if reg_type == 1 else sav_profile.get("nome", "")
        ) or "",
        "source_document_upload": upload_status,
        "medical_exam_upload": medical_exam_upload,
        "inline_subida": inline_subida,
        "subida_document_upload": subida_document_upload,
    }


@server.tool()
def submit_subida_enrollment(
    batch_number: str,
    license: int,
    mod4_id: str,
) -> dict:
    """
    Submit a standalone Subida de escalão enrollment (type-4 batch).

    Distinct from submit_enrollment's inline-subida rider: this commits the
    player to a *standalone* Subida batch via the SAV2 "add player to a
    Subida batch" web flow (eligibility list → cascades → commit op=50).
    The mod4 carries no OCR fields, so there is no preview/reconciliation
    step — the licence must be passed directly and is checked against the
    server's eligible list. The mod4 PDF is uploaded after the commit as
    the supporting document (tipo_doc=6).

    Args:
        batch_number:  Human-visible Subida batch number.
        license:       Player licence (must already exist in SAV).
        mod4_id:       Artifact id of an fpb_modelo_4 from parse_enrollment_forms.

    Returns:
        success=true + license + name + subida_document_upload on success.
    """
    form = _forms.get(mod4_id)
    if form is None:
        raise ValueError(f"Unknown mod4_id: {mod4_id!r}")
    if form.get("doc_type") != DocType.FPB_MODELO_4:
        raise ValueError(f"Artifact {mod4_id!r} is not an fpb_modelo_4 form")

    client = _get_client()
    batch_id = client.resolve_batch_id(batch_number)
    batch = next(
        (b for b in client.list_player_registration_batches() if b.id == batch_id),
        None,
    )
    if batch is None:
        raise ValueError(f"Batch {batch_number!r} not found")
    if batch.type_id != 4:
        raise ValueError(
            f"Batch {batch_number!r} is type {batch.type_id} ({batch.type!r}); "
            f"submit_subida_enrollment requires a Subida (type-4) batch. For an "
            f"inline subida on a 1ª Inscrição / Revalidação, use submit_enrollment "
            f"with mod4_id."
        )

    client.add_player_to_registration_batch(batch_id, license)

    subida_document_upload = _replace_player_document_from_bytes(
        client, batch_id, license, form.get("pdf_bytes"), doc_type=form["doc_type"],
    )

    sav_profile: dict[str, Any] = {}
    try:
        sav_profile = client.load_player_profile(license)
    except (SavConnectionError, SavResponseError):
        logger.debug("Could not load player profile for subida response", exc_info=True)

    return {
        "success": True,
        "license": license,
        "name": sav_profile.get("nome", ""),
        "subida_document_upload": subida_document_upload,
    }


@server.tool()
def update_enrollment(
    license: int,
    fields: dict[str, Any],
) -> dict:
    """
    Patch personal-data and/or address fields on an already-enrolled player.

    The batch is resolved automatically from the license. Only the keys
    present in `fields` are changed; everything else is preserved from the
    existing inscricao. No document is touched — pair with
    `replace_player_document` if you also want to swap the PDF.

    Supported keys (any subset, ints where applicable):
      Step 1 (personal): id_type (int), id_number, id_expiry, telemovel,
        telefone, email, nome_pai, nome_mae.
      Step 2 (address): morada, cod_postal, localidade_txt,
        distrito_id (int), concelho_id (int).

    Guardian/taxa/exam/consent fields are commit-time only on creation and
    are not (yet) patchable on existing enrolments — pass them via
    submit_enrollment when adding a new player.

    Returns: {"success": True, "player_id": int} on success, or
    {"error": "license_not_enrolled", "license": int, "open_batches": [...]}
    if the licence is not enrolled in any open batch.
    """
    allowed = {
        "id_type", "id_number", "id_expiry", "telemovel", "telefone",
        "email", "nome_pai", "nome_mae",
        "morada", "cod_postal", "localidade_txt",
        "distrito_id", "concelho_id",
    }
    unknown = sorted(set(fields) - allowed)
    if unknown:
        raise ValueError(
            f"Unsupported field(s) for update_enrollment: {unknown}. "
            f"Allowed: {sorted(allowed)}."
        )
    int_keys = {"id_type", "distrito_id", "concelho_id"}
    coerced: dict[str, Any] = {}
    for k, v in fields.items():
        if v is None:
            continue
        if k in int_keys and not isinstance(v, int):
            try:
                coerced[k] = int(v)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Field {k!r} expects an integer; got {v!r}.") from exc
        else:
            coerced[k] = v

    client = _get_client()
    batch_id = _resolve_license_batch(client, license)
    if isinstance(batch_id, dict):
        return batch_id
    player_id = client.update_player_in_registration_batch(
        batch_id, license, **coerced,
    )
    return {"success": True, "player_id": player_id}


@server.tool()
def create_enrollment_manual(
    batch_number: str,
    license: int,
    fields: dict[str, Any] | None = None,
) -> dict:
    """
    Enroll a player in a batch using their existing SAV profile, with optional
    field overrides — no PDF required.

    Equivalent of `sav enrollment create --batch BATCH_NUMBER --license LICENSE [--field ...]`.

    batch_number is the human-visible batch number (as shown in the SAV2 UI).

    fields: optional subset of the same keys accepted by update_enrollment (id_type,
    id_number, id_expiry, telemovel, telefone, email, nome_pai, nome_mae, morada,
    cod_postal, localidade_txt, distrito_id, concelho_id) plus create-time fields
    (exam_date, guardian_name, guardian_relation, guardian_phone, guardian_email,
    consent_data, consent_communications, consent_marketing).

    Returns: {"success": True, "player_id": int} on success.
    """
    client = _get_client()
    batch_id = client.resolve_batch_id(batch_number)
    player_id = client.add_player_to_registration_batch(
        batch_id, license, **(fields or {}),
    )
    return {"success": True, "player_id": player_id}


@server.tool()
def update_enrollment_with_document(
    license: int,
    pdf: str,
    doc_type: str | None = None,
    field_overrides: dict[str, Any] | None = None,
    file_only: bool = False,
) -> dict:
    """
    Reconcile a new PDF against an existing enrolment and patch fields / replace document.

    Equivalent of `sav enrollment update --license LICENSE FILE [--mod1] [--field ...] [--file-only]`.

    The batch is resolved automatically from the license.

    pdf: base64-encoded PDF.
    doc_type: optional type hint — "fpb_modelo_1" or "exame_medico". When given,
    classification is skipped and the classifier is trained with the known label.
    field_overrides: optional field values applied on top of reconcile result before
    submitting (same keys as update_enrollment). Only valid when file_only=False.
    file_only: when True, replace the document without touching fields.

    Returns: {"success": True, "fields_updated": bool, "document_uploaded": bool} on
    success, or {"error": "license_not_enrolled", "license": int, "open_batches": [...]}
    if the licence is not enrolled in any open batch.
    """
    from sav_parsers import classify, close_processing, parse_fpb_mod1, train_classifier

    try:
        pdf_bytes = base64.b64decode(pdf)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"Invalid base64 for pdf: {exc}") from exc

    tmp_path: str | None = None
    try:
        tmp_path = _pdf_bytes_to_tempfile(pdf_bytes)

        _hint_map = {"fpb_modelo_1": DocType.FPB_MODELO_1, "exame_medico": DocType.EXAME_MEDICO}
        if doc_type is not None:
            if doc_type not in _hint_map:
                raise ValueError(f"Unknown doc_type: {doc_type!r}. Use 'fpb_modelo_1' or 'exame_medico'.")
            active_doc_type = _hint_map[doc_type]
            try:
                train_classifier(tmp_path, active_doc_type)
            except Exception:
                logger.debug("train_classifier failed for doc_type=%r", doc_type, exc_info=True)
        else:
            active_doc_type = classify(tmp_path)

        tipo_doc = doc_type_to_tipo_doc(active_doc_type)
        client = _get_client()
        batch_id = _resolve_license_batch(client, license)
        if isinstance(batch_id, dict):
            return batch_id

        if file_only:
            # No OCR ran → can't tell if the club stamp is already present, so skip stamping.
            client.replace_player_registration_document(batch_id, license, tmp_path, tipo_doc=tipo_doc)
            return {"success": True, "fields_updated": False, "document_uploaded": True}

        if active_doc_type != DocType.FPB_MODELO_1:
            raise ValueError(
                f"Document type {active_doc_type.value!r} cannot be reconciled; "
                "only fpb_modelo_1 forms are supported. Use file_only=True to upload as-is."
            )

        parse_result = parse_fpb_mod1(tmp_path)
        parsed = parse_result["fields"]
        processing_id = parse_result["processing_id"]

        close_called = False
        try:
            sav_profile = client.load_player_profile(license)
            result = reconcile_fpb_mod1(parsed, sav_profile, client=client)
            kwargs = {k: v for k, v in {**result.updated, **result.kept}.items()}
            if field_overrides:
                kwargs.update(field_overrides)

            allowed = {
                "id_type", "id_number", "id_expiry", "telemovel", "telefone",
                "email", "nome_pai", "nome_mae", "morada", "cod_postal",
                "localidade_txt", "distrito_id", "concelho_id",
            }
            patch_kwargs = {k: v for k, v in kwargs.items() if k in allowed}
            client.update_player_in_registration_batch(batch_id, license, **patch_kwargs)
            carimbo, carimbo_bbox = read_carimbo(parsed)
            # Derive reg_type from OCR checkboxes only (no NIF lookup in update flow).
            _ocr_reg_type = (
                2 if parsed.get("tipo_inscricao_revalidacao") and parsed["tipo_inscricao_revalidacao"].value
                else 1 if parsed.get("tipo_inscricao_primeira") and parsed["tipo_inscricao_primeira"].value
                else None
            )
            tipo_checked, tipo_bbox = (
                read_tipo_inscricao(parsed, _ocr_reg_type)
                if _ocr_reg_type is not None else (None, None)
            )
            with overlaid_pdf(
                tmp_path,
                inscricao_overlay(reg_type=_ocr_reg_type, already_checked=tipo_checked, bbox=tipo_bbox),
                carimbo_overlay(carimbo_present=carimbo, bbox=carimbo_bbox),
            ) as (upload_path, (_, carimbo_r)):
                client.replace_player_registration_document(batch_id, license, upload_path, tipo_doc=tipo_doc)

            corrections: dict[str, str] = {}
            for kwarg in result.needs_review:
                entity = KWARG_TO_ENTITY.get(kwarg)
                val = kwargs.get(kwarg)
                if entity and val is not None:
                    corrections[entity] = str(val)
            corrections.update(result.retrain_corrections)
            close_called = True
            try:
                close_processing(processing_id, corrections=corrections or None)
            except Exception:
                logger.debug("close_processing failed", exc_info=True)
        finally:
            if not close_called:
                try:
                    close_processing(processing_id)
                except Exception:
                    logger.debug("close_processing fallback failed", exc_info=True)

        response = {
            "success": True,
            "fields_updated": True,
            "document_uploaded": True,
            "has_club_stamp": carimbo_r.effective,
        }
        if carimbo_r.error:
            response["stamp_warning"] = (
                f"{carimbo_r.error} — document uploaded without the club stamp; "
                "please stamp it manually."
            )
        return response
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


@server.tool()
def read_enrollment(license: int) -> dict:
    """
    Show one player's enrolment detail by licence.

    The batch is resolved automatically from the license.

    Returns the enrollment record dict on success, or
    {"error": "license_not_enrolled", "license": int, "open_batches": [...]}
    if the licence is not enrolled in any open batch.

    To list every player in a batch, use list_batch_enrollments(batch_number).
    """
    client = _get_client()
    batch_id = _resolve_license_batch(client, license)
    if isinstance(batch_id, dict):
        return batch_id
    return client.load_existing_registration_record(batch_id, license)


@server.tool()
def get_enrollment_status(license: int) -> dict:
    """
    Return a player's enrollment status with a required-document checklist.

    Status values:
      "enrolled"     — license is active in the session's club roster and
                        not in any open batch.
      "pending"      — license is in an open batch (Em construção /
                        Devolvida / Em Validação / Em Pagamento); a
                        document checklist is included.
      "not_enrolled" — license is neither in an open batch nor in the
                        active roster.

    For "pending" the response carries `batch` ({number, type_id, type,
    state}) and `checklist` ({scenario, reg_type, required, optional,
    missing}). Scenario is "portuguese" or "foreign_born" for reg_type 1/2,
    "subida_standalone" for reg_type 4, and the checklist is null for
    reg_type 3 (Transferência is not handled yet).

    The checklist mirrors FPB policy (the SAV API doesn't expose it):
      portuguese: fpb_modelo_1, exame_medico; optional fpb_modelo_4.
      foreign_born: fpb_modelo_1, exame_medico, atestado_residencia,
        certidao_matricula, documento_identificacao × 2 (passaporte +
        título de residência, player's or parent's). Both fall under
        SAV tipo_doc=18, so the rule reports counts (need ≥2, found N).

    For "enrolled" the response carries `player` ({license, name, tier,
    club}). For "not_enrolled" it carries `open_batches` — the currently
    open batches the caller could join.
    """
    client = _get_client()
    try:
        batch_id = client.resolve_batch_id_by_license(license)
    except LicenseNotEnrolledError as exc:
        club_id = int(client.session.get("organizacao") or 0)
        roster_hits = (
            client.search_players(
                license=str(license), club=club_id, status="active",
            )
            if club_id else []
        )
        if roster_hits:
            return {
                "license": license,
                "status": "enrolled",
                "player": player_to_dict(roster_hits[0]),
            }
        return {
            "license": license,
            "status": "not_enrolled",
            "open_batches": exc.open_batches,
        }

    batch = next(
        (b for b in client.list_player_registration_batches() if b.id == batch_id),
        None,
    )
    record = client.load_existing_registration_record(batch_id, license)
    raw_docs = client.list_player_registration_documents(batch_id, license)
    doc_types = [
        (mapped.value if (mapped := tipo_doc_to_doc_type(d["tipo_doc"])) else None)
        for d in raw_docs
    ]
    nacional_raw = record.get("nacional")
    try:
        nacional_id = (
            int(nacional_raw) if nacional_raw not in (None, "") else None
        )
    except (TypeError, ValueError):
        nacional_id = None
    reg_type = batch.type_id if batch else 0
    return {
        "license": license,
        "status": "pending",
        "batch": {
            "number": batch.number if batch else "",
            "type_id": reg_type,
            "type": batch.type if batch else "",
            "state": batch.state if batch else "",
        },
        "checklist": compute_enrollment_checklist(reg_type, nacional_id, doc_types),
    }


@server.tool()
def list_batch_enrollments(batch_number: str) -> list[dict]:
    """
    List every player enrolled in a batch.

    batch_number is the human-visible batch number (as shown in the SAV2 UI).

    Returns: list of {"license": int, "name": str}.

    To inspect a single player by licence, use read_enrollment(license).
    """
    client = _get_client()
    batch_id = client.resolve_batch_id(batch_number)
    return client.list_player_registration_batch_items(batch_id)


@server.tool()
def delete_enrollment(license: int) -> dict:
    """
    Remove one player's enrolment by licence.

    The batch is resolved automatically from the license.

    Returns {"removed": True, "license": int, "batch_number": str} on success,
    or {"error": "license_not_enrolled", "license": int, "open_batches": [...]}
    if the licence is not enrolled in any open batch.

    To delete a whole batch (all enrollments in it), use delete_batch(batch_number).
    """
    client = _get_client()
    batch_id = _resolve_license_batch(client, license)
    if isinstance(batch_id, dict):
        return batch_id
    client.remove_player_from_registration_batch(batch_id, license)
    return {
        "removed": True,
        "license": license,
        "batch_number": client._cache.get_batch_number(batch_id) or f"#{batch_id}",
    }


@server.tool()
def delete_batch(batch_number: str) -> dict:
    """
    Delete an entire registration batch and every enrolment in it.

    batch_number is the human-visible batch number (as shown in the SAV2 UI).
    Only open ("Em construção") batches can be deleted; submitted batches
    will raise an error from SAV2.

    Returns {"deleted": True, "batch_number": str} on success.

    To remove a single player from a batch, use delete_enrollment(license).
    """
    client = _get_client()
    batch_id = client.resolve_batch_id(batch_number)
    client.delete_player_registration_batch(batch_id)
    return {"deleted": True, "batch_number": batch_number}


# ── Registration documents ────────────────────────────────────────────────────

@server.tool()
def list_player_documents(license: int) -> list[dict] | dict:
    """
    List documents currently uploaded for a player.

    The batch is resolved automatically from the license.

    Each entry: {"doc_id": int, "doc_type": str | null}. doc_id is the
    galeria id expected by delete_player_document. SAV2-only document types
    with no sav-parsers equivalent are returned with doc_type=null.

    Returns {"error": "license_not_enrolled", ...} if the licence is not
    enrolled in any open batch.
    """
    client = _get_client()
    batch_id = _resolve_license_batch(client, license)
    if isinstance(batch_id, dict):
        return batch_id
    docs = client.list_player_registration_documents(batch_id, license)
    return [
        {
            "doc_id": doc["doc_id"],
            "doc_type": (
                mapped.value if (mapped := tipo_doc_to_doc_type(doc["tipo_doc"])) is not None
                else None
            ),
        }
        for doc in docs
    ]


def _pdf_bytes_to_tempfile(data: bytes) -> str:
    """Write PDF/image bytes to a .pdf temp file (converting images); caller must unlink."""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(ensure_pdf(data))
        return f.name


def _decode_pdf_to_tempfile(pdf_base64: str) -> str:
    """Decode a base64-encoded payload into a .pdf temp file; caller must unlink."""
    return _pdf_bytes_to_tempfile(base64.b64decode(pdf_base64))

def _resolve_document_upload_type(tmp_path: str, doc_type: str | None) -> str:
    """Return explicit doc_type or classify tmp_path when omitted."""
    if doc_type is not None:
        return doc_type
    from sav_parsers import classify
    return classify(tmp_path).value


@server.tool()
def upload_player_document(
    license: int,
    pdf_base64: str,
    doc_type: str | None = None,
) -> dict:
    """
    Upload a document (PDF, base64-encoded) attached to a player's registration.

    The batch is resolved automatically from the license.

    doc_type: one of exame_medico, fpb_modelo_1, fpb_modelo_4,
    atestado_residencia, documento_identificacao, certidao_matricula, outros.
    When omitted, sav-parsers classifies the PDF first.
    Types recognized by sav-parsers but without a SAV2 tipo_doc mapping fail
    before the SAV2 call.

    Returns {"success": True} on success, or
    {"error": "license_not_enrolled", "license": int, "open_batches": [...]}
    if the licence is not enrolled in any open batch.
    """
    tmp_path = _decode_pdf_to_tempfile(pdf_base64)
    try:
        resolved_doc_type = _resolve_document_upload_type(tmp_path, doc_type)
        tipo_doc = doc_type_to_tipo_doc(resolved_doc_type)
        client = _get_client()
        batch_id = _resolve_license_batch(client, license)
        if isinstance(batch_id, dict):
            return batch_id
        # classify-only path: no OCR field parse, so we don't know whether the
        # club stamp is already present → skip stamping to avoid double-stamping.
        client.upload_player_registration_document(
            batch_id, license, tmp_path, tipo_doc=tipo_doc,
        )
        return {"success": True}
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


@server.tool()
def delete_player_document(license: int, doc_id: int) -> dict:
    """
    Delete a previously uploaded document by its galeria id (from list_player_documents).

    The `license` is the document owner's SAV license. The server verifies
    `doc_id` is one of that player's documents in the open batch before
    deleting — without this check, a caller could pass their own license with
    someone else's doc_id.

    Returns {"success": True} on success,
    {"error": "license_not_enrolled", "license": int, "open_batches": [...]}
    if the licence is not enrolled in any open batch, or
    {"error": "doc_not_found", "license": int, "doc_id": int} if `doc_id` does
    not belong to that player in the resolved batch.
    """
    client = _get_client()
    batch_id = _resolve_license_batch(client, license)
    if isinstance(batch_id, dict):
        return batch_id
    docs = client.list_player_registration_documents(batch_id, license)
    if not any(d["doc_id"] == doc_id for d in docs):
        return {"error": "doc_not_found", "license": license, "doc_id": doc_id}
    client.delete_player_registration_document(doc_id)
    return {"success": True}


@server.tool()
def replace_player_document(
    license: int,
    pdf_base64: str,
    doc_type: str | None = None,
) -> dict:
    """
    Replace any existing documents of `doc_type` for this player with a
    new PDF (base64-encoded). Idempotent on the upload side: when no existing
    doc of the translated SAV2 tipo_doc is found, behaves like a plain upload.

    The batch is resolved automatically from the license.

    Returns {"success": True} on success, or
    {"error": "license_not_enrolled", "license": int, "open_batches": [...]}
    if the licence is not enrolled in any open batch.
    """
    tmp_path = _decode_pdf_to_tempfile(pdf_base64)
    try:
        resolved_doc_type = _resolve_document_upload_type(tmp_path, doc_type)
        tipo_doc = doc_type_to_tipo_doc(resolved_doc_type)
        client = _get_client()
        batch_id = _resolve_license_batch(client, license)
        if isinstance(batch_id, dict):
            return batch_id
        # classify-only path: see comment in upload_player_document. Skip stamping.
        client.replace_player_registration_document(
            batch_id, license, tmp_path, tipo_doc=tipo_doc,
        )
        return {"success": True}
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


# ── Authorization metadata ──────────────────────────────────────────────────
# Per-tool policy (capability tier, role allowlist, self-scope, subject /
# identity parameter markers) lives in `authz.toml`. Loading it here stamps
# each registered tool's `_meta` and inputSchema with the `x-sav-*` extension
# fields documented in AGENTS.md → "Authorization metadata for downstream
# consumers". sav-mcp itself does NOT enforce — the wrapper does.

from pathlib import Path

from sav_mcp.authz import apply_to_server, load_policy

_AUTHZ_POLICY, _ = load_policy(Path(__file__).with_name("authz.toml"))
apply_to_server(server, _AUTHZ_POLICY)


def main() -> None:
    server.run()
