"""
MCP server for the FPB SAV2 system.

Exposes read access to players, clubs, games, and registration batches, plus a
multi-step player-enrollment workflow that orchestrates OCR-parsed FPB forms
against the SAV2 API.  Tools are designed to be stateless-friendly so an LLM
agent can drive them without an interactive UI — the chat is the confirmation
loop.

Enrollment workflow:
    1. parse_enrollment_forms  → mod1_id(s) / medical_exam_id(s) / mod4_id(s) + parsed metadata
    2. ensure_open_batch  → batch_number (get-or-create; find_open_batch / create_batch still exist)
    3. preview_enrollment(license=null)  → full reconciled profile (auto-resolves the
       player from the form; returns {resolved: false, candidates} when ambiguous, so the
       user picks and preview is re-called with an explicit license)
    4. submit_enrollment  → player_id (auto-uploads fpb_modelo_1 and optional exame_medico)

    resolve_player still exists for explicit control (show the candidate list before
    previewing); preview_enrollment folds it in for the unambiguous common case.

Standalone Subida (type-4) uses the mod4 OCR fields (licenca_nr/name/escalao_subida):
    1. parse_enrollment_forms  → mod4_id + parsed metadata
    2. resolve_subida_target   → license + tier_id + gender_id (or candidates)
    3. ensure_open_batch(reg_type=4, …)  → Subida batch_number
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
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from mcp.server.fastmcp import FastMCP

from sav_client import SavClient
from sav_client.models import Player, Season
from sav_client.exceptions import (
    LicenseNotEnrolledError,
    SavConfigError,
    SavConnectionError,
    SavError,
    SavResponseError,
)

logger = logging.getLogger(__name__)
from sav_shared.files import (
    ensure_pdf,
    load_image_bytes,
    rect_has_overlay,
)
from sav_shared.enrollment import (
    REGISTRATION_TYPE_REVALIDACAO,
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
    CLUB_STAMP_RECT,
    carimbo_overlay,
    inscricao_overlay,
    is_filled_mod1_template,
    mod1_acroform_to_fields,
    mod1_values_to_fields,
    overlaid_pdf,
    player_is_minor,
    read_carimbo,
    read_mod1_acroform,
    read_tipo_inscricao,
    reconcile_fpb_mod1,
    render_mod1,
)
from sav_shared.fpb_mod4 import (
    club_signature_overlay,
    detentor_signature_overlay,
    read_club_signature,
    read_detentor_signature,
)
from sav_shared.games import filter_games, game_sort_key
from sav_shared.lookups import (
    GENERO,
    REGISTRATION_TYPE_LABELS,
    TIER_AGE_RANGE_IN_SEASON,
    doc_type_to_tipo_doc,
    player_registration_tiers,
    reference_data,
    tier_birth_years_for_season,
    tipo_doc_to_doc_type,
)
from sav_shared.medical_exam import extract_medical_exam_info
from sav_shared.serializers import (
    batch_to_dict,
    club_game_to_dict,
    club_to_dict,
    coach_to_dict,
    game_to_dict,
    player_to_dict,
)
from sav_parsers.types import DocType, ParsedField

server = FastMCP("FPB SAV")

# ── Singleton SAV client ──────────────────────────────────────────────────────

_client: SavClient | None = None


def _get_client() -> SavClient:
    global _client
    if _client is None:
        _client = SavClient.from_env()
        _client.login()
    return _client


# ── Reference-data resources ──────────────────────────────────────────────────
# Static SAV lookups (genders, escalões, distritos, id/guardian/doc types, tier
# eligibility) an app builds its UI/backend around. Exposed as MCP resources
# rather than tools: reference data a client loads once, not an action. These
# sit outside authz.toml (its drift check governs @server.tool only) — safe
# because the data is federation-public and non-sensitive, the same tier already
# opened to every role via list_associations / list_clubs / list_tiers.


@server.resource("sav://lookups", mime_type="application/json")
def lookups_resource() -> dict:
    """All federation-public SAV lookups plus tier eligibility.

    Genders, registration types, distritos, ID/guardian/document types, and the
    per-gender escalão (tier) ids — the dropdown and validation values an app
    builds its UI/backend around. Tier age windows carry the eligible birth
    years for the *current* season (resolved server-side). For another season
    (e.g. next-season planning) read ``sav://lookups/{season_start_year}``.
    """
    season = _get_client().get_current_season().start_year
    return reference_data(season_start_year=season)


@server.resource("sav://lookups/{season_start_year}", mime_type="application/json")
def lookups_for_season_resource(season_start_year: str) -> dict:
    """Same bundle as ``sav://lookups``, for an explicit season.

    ``season_start_year`` is the season's start year (e.g. ``2025`` → season
    2025/2026); tier birth years are computed for it. No network call — pure
    static reference data.
    """
    return reference_data(season_start_year=int(season_start_year))


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

    This is the source of truth for the current season — read ``season`` /
    ``season_start_year`` / ``season_id`` from here rather than inferring the
    season from registration batches, game rows, or player rows (those can be
    absent off-season, e.g. before the new época's batches are opened).

    Returns ``{user, profile, club_id, season_id, season, season_start_year}``.

    season_id is the current epoch — pass it (or omit / pass 0 for all-seasons)
    to tools that accept a season parameter.

    season is the human-readable label (e.g. ``"2025/2026"``) and
    season_start_year its starting calendar year (e.g. ``2025``). SAV2 stores
    the season only as the opaque season_id, so the label is resolved from
    SAV2's season table (the active época); it does not depend on any
    registration batch existing, so it resolves off-season too. Both fields
    fall back to ``None`` only if that lookup itself fails.
    """
    client = _get_client()
    session = client.session
    if session is None:
        raise ValueError("Session not initialized")

    # The label isn't in the session dict; it comes from SAV2's season table
    # (the active época). Best-effort so a transient lookup failure still
    # yields valid (label-less) session info instead of raising.
    season_start_year: int | None = None
    season: str | None = None
    try:
        current = client.get_current_season()
        season = current.label
        season_start_year = current.start_year
    except SavError:
        logger.debug("Could not resolve current season label", exc_info=True)

    return {
        "user": session.get("user"),
        "profile": session.get("perfil"),
        "club_id": int(session.get("organizacao") or 0),
        "season_id": int(session.get("epoca_id") or 0),
        "season": season,
        "season_start_year": season_start_year,
    }


def _effective_club(client: SavClient, club_id: int | None) -> int:
    """Resolve the club a tool call should be scoped to.

    Returns ``club_id`` when the caller supplied one, otherwise the session's
    own club, read from ``client.session["organizacao"]``. The result is 0 when
    the caller passed ``club_id=0`` or when there is no session club: for the
    federation-wide tools that is a legitimate "all clubs" scope, while tools
    that genuinely need one club (NIF resolution, ``club_roster``,
    ``warm_nif_index``) reject a falsy result with a ``ValueError``.
    """
    if club_id is not None:
        return club_id
    return int(client.session.get("organizacao") or 0)


# ── Players ───────────────────────────────────────────────────────────────────

def _most_recent(players: list[Player]) -> Player | None:
    """Return the latest-season row, or None when no rows were found.

    A defensive invariant guard, not a fix for an observed bug. It was added on
    the assumption that a licence search with ``jc_epoca=0`` returns one row per
    season, making ``results[0]`` arbitrary. Live verification (2026-08-20, club
    2430) refuted that: a licence-scoped all-seasons search returned a single
    row, and the club-wide all-seasons search returned 684 rows for 684 distinct
    licences — no duplicates. SAV2 does not *document* single-row, so this stays
    as a cheap guard; just don't mistake it for load-bearing.

    ``Player.season`` is a "YYYY/YYYY" string, so lexicographic max is
    chronological. With one row this is identical to ``results[0]``.
    """
    if not players:
        return None
    return max(players, key=lambda player: player.season or "")


def _resolve_rows(
    client: SavClient,
    *,
    license: int,
    club_id: int,
    status: str,
    season: int | None,
    with_details: bool,
) -> Player | None:
    """Resolve one licence using the current → previous → all ladder.

    ``club_id=0`` is federation-wide, which SavClient implements as one search
    request per club in every association (``_search_all_clubs``) — not one
    query. Since nearly every lookup is for one of our own players, each
    fixed-season rung probes the session club first and only pays for the sweep
    on a miss: one request instead of N. The all-seasons rung deliberately
    skips the probe — there a stale own-club row from an older season would
    outrank the player's current row at the club they transferred to, which is
    exactly the case federation-wide search exists to answer.
    """
    own_club = (
        int(client.session.get("organizacao") or 0) if club_id == 0 else 0
    )

    def search(rung: int | None, club: int) -> list[Player]:
        return client.search_players(
            license=str(license), club=club, status=status, season=rung,
            with_details=with_details,
        )

    def search_rung(rung: int | None) -> list[Player]:
        # ``rung != 0`` keeps the all-seasons rung (and an explicit season=0)
        # federation-only; see the docstring for why the probe is skipped there.
        if own_club and rung != 0:
            hit = search(rung, own_club)
            if hit:
                return hit
        return search(rung, club_id)

    if season is not None:
        return _most_recent(search_rung(season))

    # Keep the common current-enrollment case to one query. A player last
    # enrolled in the previous season costs two; the full sweep is deliberately
    # the last resort.
    current = search_rung(None)
    if current:
        return _most_recent(current)

    previous_season: int | None = None
    try:
        recent_seasons = client._recent_season_ids()
        if len(recent_seasons) > 1:
            previous_season = recent_seasons[1]
    except Exception:
        # Deliberately broad, unlike the SavError handlers elsewhere in this
        # file: this rung is only an optimisation and the all-seasons query
        # below returns the correct answer without it. Failing a caller's
        # lookup because an optional shortcut broke would be the worse trade.
        logger.debug("Could not resolve the previous SAV season", exc_info=True)

    if previous_season is not None:
        previous = search_rung(previous_season)
        if previous:
            return _most_recent(previous)

    return _most_recent(search_rung(0))


# Both NIF entry points reject a falsy club with the same explanation. This is
# a SAV2 platform limit, not one of ours: SAV2 only discloses a player's NIF to
# their own club, so there is no federation-wide NIF data to index in the first
# place. Our per-club index (SavClient.find_license_by_nif) is shaped by that
# limit rather than the cause of it — which is why no amount of pre-warming
# makes club_id=0 work here.
_NIF_CLUB_REQUIRED = (
    "NIF resolution is club-scoped: SAV2 only exposes a player's NIF to their "
    "own club, so club_id=0 has nothing to search. Pass an explicit club_id "
    "for a club whose players you can see, or resolve the player by licence "
    "instead."
)


def _resolve_by_nif(
    client: SavClient,
    *,
    nif: str,
    club_id: int,
    status: str,
    with_details: bool,
) -> Player | None:
    digits = re.sub(r"\D", "", nif or "")
    if len(digits) != 9 or not club_id:
        return None
    license = client.find_license_by_nif(digits, club_id=club_id)
    if license is None:
        return None
    return _resolve_rows(
        client, license=license, club_id=club_id, status=status,
        season=None, with_details=with_details,
    )


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
        photo_url, mobile_phone and nif in the returned rows. Off by default
        because it is N+1.
    """
    client = _get_client()
    effective_club: int | list[int] = _effective_club(client, club_id)
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
    license: int,
    club_id: int | None = None,
    status: str = "active",
    season: int | None = None,
    with_details: bool = False,
) -> dict | None:
    """
    Return details for a single player by licence number.

    club_id defaults to the session's own club when omitted; club_id=0
    searches federation-wide, with the session club probed first. Without an
    explicit season, resolution widens from the current season to the previous
    season and finally all seasons. Therefore null means the licence has no
    matching row in that search, not "not currently at this club".
    status: "active" (default) | "inactive" | "all"; passed unchanged at
        every season rung.
    season: explicit SAV2 epoch id. When supplied, bypasses the widening ladder.
    with_details: when true, also fetch photo_url, mobile_phone and nif.
    Returns null if no player is found with that licence.
    """
    client = _get_client()
    effective_club: int = _effective_club(client, club_id)
    row = _resolve_rows(
        client, license=license, club_id=effective_club, status=status,
        season=season, with_details=with_details,
    )
    return player_to_dict(row, with_details=with_details) if row else None


@server.tool()
def find_player_by_nif(
    nif: str,
    club_id: int | None = None,
    status: str = "active",
    with_details: bool = False,
) -> dict | None:
    """
    Resolve a player by Portuguese NIF (9 digits) — inverse of get_player.

    Returns the same shape as get_player, or null if no player row matches.
    club_id defaults to the session's own club. NIF lookup is club-scoped
    because SAV2 only exposes a player's NIF to their own club, so club_id=0
    raises rather than silently returning null. Resolution widens from the current season to the
    previous season and finally all seasons, so null no longer means "not
    currently at this club".
    status: "active" (default) | "inactive" | "all"; passed unchanged at
        every season rung.
    with_details: when true, also fetch photo_url, mobile_phone and nif.
    """
    digits = re.sub(r"\D", "", nif or "")
    if len(digits) != 9:
        return None
    client = _get_client()
    effective_club: int = _effective_club(client, club_id)
    if not effective_club:
        raise ValueError(_NIF_CLUB_REQUIRED)
    row = _resolve_by_nif(
        client, nif=digits, club_id=effective_club, status=status,
        with_details=with_details,
    )
    return player_to_dict(row, with_details=with_details) if row else None


@server.tool()
def warm_nif_index(
    club_id: int | None = None,
    scope: str = "recent",
    force: bool = False,
) -> dict:
    """Build or reuse the club's node-local NIF lookup index.

    club_id defaults to the session's own club, and a falsy club (an explicit
    club_id=0, or no resolvable session club) raises ValueError: the index is
    built from a single club roster, so there is nothing federation-wide to
    build. ``scope="full"`` is SLOW and offline-only: it makes one profile POST
    per not-yet-indexed licence across all seasons at 8-way concurrency. A
    large club can take minutes, likely beyond a default MCP client timeout.
    Its purpose is to let an importer or nightly job pay that cost outside a
    user request.
    """
    client = _get_client()
    effective_club: int = _effective_club(client, club_id)
    if not effective_club:
        raise ValueError(
            "warm_nif_index requires a club_id (SAV2 only exposes NIFs to a "
            "player's own club, so the index is per-club; the session club "
            "could not be resolved)."
        )
    result = client.build_nif_index(
        effective_club, scope=scope, force=force,
    )
    built_at = float(result["built_at"])
    if not built_at:
        # The roster listing failed, so nothing was indexed and no freshness
        # marker was written. Say so plainly rather than rendering epoch 0 as a
        # 1970 timestamp that reads like a successful build.
        return {**result, "built_at": None, "error": "roster_unavailable"}
    return {
        **result,
        "built_at": datetime.fromtimestamp(
            built_at, tz=timezone.utc,
        ).isoformat(),
    }


@server.tool()
def lookup_player(
    nif: str | None = None,
    license: int | None = None,
    club_id: int | None = None,
    status: str = "active",
    with_profile: bool = False,
    with_details: bool = False,
) -> dict | None:
    """Resolve exactly one NIF or licence, optionally with its full profile.

    The season ladder widens from current to previous to all seasons. A null
    result therefore means no matching row was found, not "not currently at
    this club". ``profile`` remains nested because it has fields such as nif
    and name whose provenance differs from the roster row.

    club_id defaults to the session's own club when omitted. club_id=0 searches
    federation-wide, and only for a licence: the session club is probed first at
    each fixed-season rung, so one of our own players still costs one request.
    A nif requires a real club because SAV2 only exposes a player's NIF to
    their own club, so club_id=0 with a nif raises instead of returning null.
    """
    if (nif is None) == (license is None):
        raise ValueError("exactly one of nif or license must be supplied")

    if nif is not None:
        digits = re.sub(r"\D", "", nif or "")
        if len(digits) != 9:
            return None

    client = _get_client()
    session_club = _effective_club(client, None)
    effective_club: int = _effective_club(client, club_id)

    if nif is not None:
        if not effective_club:
            raise ValueError(_NIF_CLUB_REQUIRED)
        row = _resolve_by_nif(
            client, nif=digits, club_id=effective_club, status=status,
            with_details=with_details,
        )
    else:
        assert license is not None
        # An explicit club_id=0 is a deliberate federation-wide request; a
        # missing one with no session club is just an unusable lookup.
        if club_id is None and not session_club:
            raise ValueError(
                "lookup_player needs a club: no club_id was given and the "
                "session club could not be resolved. Pass club_id=0 to search "
                "federation-wide."
            )
        row = _resolve_rows(
            client, license=license, club_id=effective_club,
            status=status, season=None, with_details=with_details,
        )
    if row is None:
        return None

    result = player_to_dict(row, with_details=with_details)
    if with_profile:
        # Scope the profile to the club the row actually came from: rows are
        # stamped per source club even inside a federation sweep, so this keeps
        # the licence → id bridge club-scoped instead of sweeping again.
        result["profile"] = client.load_player_profile(
            row.license, club_id=row.club_id or effective_club,
        )
    return result


@server.tool()
def club_roster(
    club_id: int | None = None,
    season: int | None = None,
    tier: str = "",
    gender: int = 0,
) -> dict:
    """
    Return a club's current-season active roster in a single call.

    Purpose-built for roster and projection work (e.g. grouping a club's players
    by birth year for a next-season escalão projection): every row is scoped to
    one club and one season, so the caller relays the roster rather than
    assembling or re-attributing it.

    Guarantees, so a projection can't drift onto stale or cross-club players:
      - single club: only players enrolled at club_id — no federation bleed.
      - current season: season defaults to the current epoch, so a player who
        lapsed in a prior season has no current-season row and is excluded.
      - active only: only players whose current-season licence is active.

    Every row carries club_id + club_name and birth_date + birth_year alongside
    name / licence / escalão, so a multi-club caller groups strictly by the
    source club without the model re-attributing players.

    Args:
        club_id: Defaults to the session's own club. Pass an explicit id for
            another club. club_id=0 is rejected — a roster is single-club.
        season: SAV2 epoch id. Defaults to the current epoch (recommended). Pass
            a past epoch to read that season's actual roster.
        tier: Optional escalão filter (e.g. "Sub 14"). Empty = all tiers.
        gender: Optional gender filter (1=Masculino, 2=Feminino, 0=any).

    Returns:
        {
          "club_id": int,
          "club_name": str,
          "season": str,       # label, e.g. "2025/2026" ("" if not the current)
          "season_id": int,    # the epoch actually queried
          "count": int,
          "players": [ {license, name, birth_date, birth_year, tier, tier_id,
                        gender, gender_id, club_id, club_name}, ... ],
        }
    """
    client = _get_client()
    effective_club: int = _effective_club(client, club_id)
    if not effective_club:
        raise ValueError(
            "club_roster requires a club_id (a roster is single-club; the "
            "session club could not be resolved)."
        )

    current: Season | None = None
    try:
        current = client.get_current_season()
    except (SavResponseError, ValueError):
        logger.debug("Could not resolve current season for club_roster", exc_info=True)

    if season is not None:
        query_season = season
    elif current is not None:
        query_season = current.id
    else:
        query_season = int(client.session.get("epoca_id") or 0)

    players = client.search_players(
        club=effective_club, status="active",
        tier=tier, gender=gender, season=query_season,
    )

    full_name, code = client._fetch_club_names(effective_club)
    club_name = full_name or code or ""
    season_label = current.label if current and query_season == current.id else ""

    def _birth_year(bd: str) -> int | None:
        head = (bd or "").split("-", 1)[0]
        return int(head) if head.isdigit() else None

    return {
        "club_id": effective_club,
        "club_name": club_name,
        "season": season_label,
        "season_id": query_season,
        "count": len(players),
        "players": [
            {
                "license": p.license,
                "name": p.name,
                "birth_date": p.birth_date,
                "birth_year": _birth_year(p.birth_date),
                "tier": p.tier,
                "tier_id": p.tier_id,
                "gender": p.gender,
                "gender_id": p.gender_id,
                "club_id": p.club_id or effective_club,
                "club_name": club_name,
            }
            for p in players
        ],
    }


@server.tool()
def get_player_profile(license: int, club_id: int | None = None) -> dict:
    """
    Read-only player profile suitable for OCR reconciliation.

    Single fetch from jogadoresdb.php?op=2 — the same data the enrollment
    wizard prefills from. Richer than get_player: includes address fields
    (morada, codpostal, localidade_txt, distrito, concelho), document IDs
    (numi, dataval, tipo), and contact details (tele, telef, email, nif).

    Select-backed fields come back as integer ID strings together with
    human-readable siblings: tipo_label, nacional_label, naturalidade_label,
    distrito_label, and concelho_label.

    club_id, when supplied, scopes the bridge search and avoids the slow
    federation-wide path on cache miss. Omit to use whatever's already
    cached from prior search_players / resolve_player calls.
    """
    client = _get_client()
    return client.load_player_profile(license, club_id=club_id)


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
        nif, tptd, tptd_expiry, mobile_phone, and email in the returned
        rows. Off by default because it is N+1.
    """
    client = _get_client()
    effective_club: int = _effective_club(client, club_id)
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
    club_id: int | None = None,
    season: int | None = None,
    status: str = "all",
    tier: str = "",
    gender: int = 0,
    date_from: str = "",
    date_to: str = "",
) -> list[dict]:
    """
    List a club's game-sheets (fixtures + results), sorted by date (earliest
    first), each row from the club's own perspective.

    club_id defaults to the session's own club. NOTE: SAV2's games endpoint only
    ever returns the *session* club's fixtures, so a non-session club_id cannot
    be honored — it only selects which side counts as "ours", and yields no
    rows when it isn't a team in the session club's games.
    season defaults to the current epoch.
    status: "scheduled" | "played" | "all" (default). A game is "played" once it
        has both scores.
    tier: escalão filter (e.g. "Sub 14"). Omit for all tiers.
    gender: 0 = any, 1 = Masculino, 2 = Feminino.
    date_from / date_to: inclusive DD-MM-YYYY window. For "games in the last 10
        days", pass date_from = today-10 days and date_to = today.

    Each row is relative to the queried club (home / our_score / opp_score /
    opponent are the club's, not the sheet's home/away):
      source_id  — stable FPB game id (for idempotent upsert)
      escalao    — tier label as on the sheet (e.g. "Sub 14 M", "Sen M")
      gender     — "Masculino" / "Feminino" (or null)
      starts_at  — ISO YYYY-MM-DDTHH:MM ("" when not yet scheduled)
      opponent   — the other team's name
      home       — true when the club plays at home
      venue      — pavilion / location, or null
      status     — "scheduled" or "played"
      our_score / opp_score — ints from the club's perspective, null until played
    """
    status_key = status.strip().lower()
    if status_key not in ("scheduled", "played", "all"):
        raise ValueError(
            f"status must be 'scheduled', 'played', or 'all'; got {status!r}"
        )

    client = _get_client()
    effective_club: int = _effective_club(client, club_id)
    # Resolve the club's name so each row can be oriented to its perspective;
    # team strings carry suffixes (" - B", "/MVP"), matched on the name.
    full_name, code = client._fetch_club_names(effective_club)
    club_name = full_name or code

    # SAV2 ignores the inicio/fim date window server-side, leaking out-of-range
    # games, so we still send it (it narrows the payload when honored) but
    # guarantee the bounds with a client-side pass via filter_games.
    games = client.list_games(
        season=season, tier=tier, gender=gender,
        date_from=date_from, date_to=date_to,
    )
    games = filter_games(games, date_from=date_from, date_to=date_to)
    rows = [
        club_game_to_dict(g, club_name=club_name)
        for g in sorted(games, key=game_sort_key)
    ]
    if status_key != "all":
        rows = [r for r in rows if r["status"] == status_key]
    return rows


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

    game_number: the human-readable game number (from list_game_sheets).
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

    game_number: the human-readable game number (from list_game_sheets).
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


@server.tool()
def fill_mod1(
    values: dict,
    player_signature_b64: str | None = None,
    guardian_signature_b64: str | None = None,
    club_stamp_b64: str | None = None,
) -> dict:
    """
    Fill the FPB Modelo 1 enrollment form from field values and return it base64-encoded.

    Produces a print-ready PDF. By default the player/guardian signature lines and
    the club stamp are left blank for hand completion. To return the completed
    form instead, pass any of player_signature_b64, guardian_signature_b64,
    club_stamp_b64 — each a base64-encoded PNG/JPG image overlaid on its area.

    values: a dict keyed by these enrollment field keys —
    tipo_inscricao, license, clube, associacao, genero, escalao, nome,
    nacionalidade, pais_nascimento, nif, nasc, tipo, numi, dataval, email, tele,
    morada, localidade_txt, codpostal, distrito, concelho, guardian_name,
    guardian_relation, guardian_id_type, guardian_id_number, guardian_id_expiry,
    guardian_phone, guardian_email, consent_data, consent_communications,
    consent_marketing, data_assinatura.

    All player fields are mandatory; the Licença FPB (license) is required only
    for a Revalidação (tipo_inscricao=2); and the guardian_* block is required in
    full only for a minor (derived from nasc) and must be empty otherwise.
    Invalid input raises an error listing every problem.

    Text fields take strings; dates are "YYYY-MM-DD"; consent_* take booleans;
    distrito/concelho/nacionalidade/pais_nascimento are names. Checkbox groups
    accept an int code or a name: tipo_inscricao (1=1ª Inscrição, 2=Revalidação),
    genero (1=Masculino, 2=Feminino), escalao (name, e.g. "Sub 14"), tipo /
    guardian_id_type (1=Cartão Cidadão, 2=Passaporte, 3=Outro), guardian_relation
    (1=pai, 2=mãe, 3=tutor).

    Insurance is always "Seguro FPB" — a fixed club policy, not a value in
    `values`. Do not attempt to pass an insurance field; there is none to set.

    Returns ``{filename, size_bytes, pdf_b64}``. Decode pdf_b64 to obtain the PDF bytes.
    """
    pdf = render_mod1(
        values,
        player_signature=base64.b64decode(player_signature_b64) if player_signature_b64 else None,
        guardian_signature=base64.b64decode(guardian_signature_b64) if guardian_signature_b64 else None,
        club_stamp=base64.b64decode(club_stamp_b64) if club_stamp_b64 else None,
    )
    return {
        "filename": "modelo1.pdf",
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
    current_year = client.get_current_season().start_year
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
    if birth_years is None and tier_name not in TIER_AGE_RANGE_IN_SEASON:
        raise ValueError(
            f"Birth-year window for {tier_name!r} is not modelled. "
            f"Query search_players(tier={tier_name!r}, gender_id={gender_id}, ...) "
            f"directly."
        )

    effective_club: int = _effective_club(client, club_id)

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


_GUARDIAN_REVIEW_FIELDS: tuple[str, ...] = (
    "guardian_name", "guardian_relation", "guardian_phone", "guardian_email",
)


def _append_minor_guardian_review(preview: dict, birth_date: object) -> None:
    """For a minor, surface any absent guardian field in preview's needs_review.

    Reuses the mod1 minor rule (`player_is_minor`, derived from the birth
    date). When the player is a minor, any of the four guardian fields not
    already carrying a value in the preview's `fields` is appended as a
    ``needs_review`` row (sav_value/ocr_value None) and added to the
    `needs_review` list — so the caller collects them up front instead of
    hitting submit_enrollment's missing_guardian_fields round-trip. Non-minors
    (and unknown birth dates) leave the preview untouched.
    """
    if not player_is_minor(birth_date):
        return
    fields = preview["fields"]
    needs_review = preview["needs_review"]
    with_value = {
        f["kwarg"] for f in fields
        if f.get("kwarg") in _GUARDIAN_REVIEW_FIELDS
        and f.get("final_value") not in (None, "")
    }
    for kwarg in _GUARDIAN_REVIEW_FIELDS:
        if kwarg in with_value or kwarg in needs_review:
            continue
        label = ENROLLMENT_FIELD_META.get(kwarg, (kwarg, ""))[0]
        fields.append({
            "kwarg": kwarg, "label": label,
            "sav_value": None, "ocr_value": None,
            "final_value": None, "status": "needs_review",
        })
        needs_review.append(kwarg)


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
    detentor_signature: bytes | None = None,
) -> dict[str, Any]:
    """Upload cached PDF bytes as a replacement registration document.

    `parsed` is the parse_fpb_mod1 / parse_fpb_mod4 fields dict (when
    available). For a mod1 it drives the club-stamp and inscription-checkbox
    overlays (`reg_type` 1 or 2 selects the checkbox). For a mod4 it drives the
    holder-signature and club-stamp overlays: `detentor_signature` (image bytes)
    is placed on the empty holder slot and $CLUB_STAMP_PATH on the empty club
    slot, mirroring the CLI submit path.
    """
    # has_club_stamp / *_warning describe the uploaded PDF, so they're only added
    # to status when status == "ok"; on "skipped" / "error" there's no uploaded
    # PDF to describe.
    status = {
        "doc_type": doc_type.value,
        "status": "skipped",
        "error": None,
    }
    if not pdf_bytes:
        return status

    is_mod1 = doc_type == DocType.FPB_MODELO_1 and parsed
    is_mod4 = doc_type == DocType.FPB_MODELO_4 and parsed
    tmp_path: str | None = None
    overlay_processing_id: str | None = None
    try:
        tmp_path = _pdf_bytes_to_tempfile(pdf_bytes)
        if is_mod4:
            det_present, det_bbox = read_detentor_signature(parsed)
            club_present, club_bbox = read_club_signature(parsed)
            club_image = load_image_bytes(os.environ.get("CLUB_STAMP_PATH"))
            overlays = (
                detentor_signature_overlay(present=det_present, bbox=det_bbox, image=detentor_signature),
                club_signature_overlay(present=club_present, bbox=club_bbox, image=club_image),
            )
        else:
            carimbo, carimbo_bbox = read_carimbo(parsed) if is_mod1 else (None, None)
            template_carimbo = False
            if is_mod1 and carimbo is None:
                overlay_fields, overlay_processing_id = _mod1_overlay_fields(tmp_path)
                carimbo, carimbo_bbox = read_carimbo(overlay_fields)
                template_carimbo = overlay_processing_id is None
            tipo_checked, tipo_bbox = (
                read_tipo_inscricao(parsed, reg_type)
                if (is_mod1 and reg_type is not None) else (None, None)
            )
            overlays = (
                inscricao_overlay(reg_type=reg_type, already_checked=tipo_checked, bbox=tipo_bbox),
                carimbo_overlay(
                    carimbo_present=carimbo,
                    bbox=carimbo_bbox,
                    rect=CLUB_STAMP_RECT if template_carimbo else None,
                ),
            )
        with overlaid_pdf(tmp_path, *overlays) as (upload_path, results):
            ok, error = try_replace_document(
                client, batch_id, license, upload_path,
                tipo_doc=doc_type_to_tipo_doc(doc_type),
            )
            status["status"] = "ok" if ok else "error"
            status["error"] = error
            if ok and is_mod4:
                detentor_r, club_r = results
                status["has_detentor_signature"] = detentor_r.effective
                status["signature_warning"] = (
                    f"{detentor_r.error} — document uploaded without the holder "
                    "signature; please sign it manually."
                ) if detentor_r.error else None
                status["has_club_stamp"] = club_r.effective
                status["stamp_warning"] = (
                    f"{club_r.error} — document uploaded without the club stamp; "
                    "please stamp it manually."
                ) if club_r.error else None
            elif ok:
                inscricao_r, carimbo_r = results
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
        if overlay_processing_id is not None:
            from sav_parsers import close_processing
            try:
                close_processing(overlay_processing_id)
            except Exception:
                logger.debug("close_processing failed for mod1 overlay", exc_info=True)
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
    return status


def _read_mod1_template_fields(pdf_bytes: bytes):
    """If `pdf_bytes` is a Modelo 1 filled from our fillable template, return its
    values as the same entity-keyed ParsedField dict parse_fpb_mod1 produces —
    read straight from the AcroForm, no classification or OCR. Returns None when
    the PDF isn't a filled template (a scan/photo/other doc), so the caller falls
    back to the Document AI path.
    """
    raw = read_mod1_acroform(pdf_bytes)
    if raw is None or not is_filled_mod1_template(raw):
        return None
    return mod1_acroform_to_fields(raw)


def _mod1_overlay_fields(tmp_path: str) -> tuple[dict, str | None]:
    """Resolve Modelo 1 fields for overlays, using AcroForm before OCR.

    The carimbo is a visual signal, so a PDF without our AcroForm must be sent
    to Document AI to locate it. For a PDF carrying our template AcroForm, the
    fixed stamp rectangle is inspected locally. This cannot distinguish a club
    stamp from another graphic placed in that rectangle; it is safe here only
    because the rectangle is otherwise empty on our template.
    """
    with open(tmp_path, "rb") as f:
        pdf_bytes = f.read()
    fields = _read_mod1_template_fields(pdf_bytes)
    if fields is not None:
        fields["carimbo_clube_presente"] = ParsedField(
            value=rect_has_overlay(pdf_bytes, CLUB_STAMP_RECT),
            confidence=1.0,
        )
        return fields, None

    from sav_parsers import parse_fpb_mod1
    parse_result = parse_fpb_mod1(tmp_path)
    return parse_result["fields"], parse_result["processing_id"]


@contextmanager
def _stamped_upload_path(
    tmp_path: str, doc_type: DocType | str, *, detentor_signature: bytes | None = None,
):
    """Yield ``(upload_path, status)`` for a staged PDF about to be uploaded.

    When the doc is a mod1 or mod4 *and* there is something to apply, OCR it and
    overlay the club stamp ($CLUB_STAMP_PATH) — plus, for a mod4, the holder
    signature — onto whichever slot OCR found empty, yielding the stamped copy.
    Any other doc type (or a mod1/mod4 with nothing to apply) yields `tmp_path`
    unchanged with an empty status. OCR runs only when an overlay is possible, so
    a plain attach of an exam/atestado/etc. is untouched. `status` carries
    has_club_stamp / has_detentor_signature and any *_warning for the response.

    An OCR failure never blocks the upload — the original path is yielded with a
    warning. This is the stamp-if-missing path shared by upload_player_document,
    replace_player_document, and update_enrollment_with_document (file_only).
    """
    if isinstance(doc_type, str):
        try:
            doc_type = DocType(doc_type)
        except ValueError:
            doc_type = None
    club_stamp = load_image_bytes(os.environ.get("CLUB_STAMP_PATH"))
    is_mod1 = doc_type == DocType.FPB_MODELO_1 and club_stamp
    is_mod4 = doc_type == DocType.FPB_MODELO_4 and (detentor_signature or club_stamp)
    if not (is_mod1 or is_mod4):
        yield tmp_path, {}
        return

    from sav_parsers import close_processing
    processing_id = None
    try:
        try:
            if is_mod1:
                fields, processing_id = _mod1_overlay_fields(tmp_path)
                carimbo, carimbo_bbox = read_carimbo(fields)
                # No processing_id means the AcroForm answered: no OCR bbox, so
                # stamp our template's fixed slot instead.
                overlays = (carimbo_overlay(
                    carimbo_present=carimbo,
                    bbox=carimbo_bbox,
                    rect=CLUB_STAMP_RECT if processing_id is None else None,
                ),)
            else:
                from sav_parsers import parse_fpb_mod4
                parse_result = parse_fpb_mod4(tmp_path)
                fields = parse_result["fields"]
                processing_id = parse_result["processing_id"]
                det_present, det_bbox = read_detentor_signature(fields)
                club_present, club_bbox = read_club_signature(fields)
                overlays = (
                    detentor_signature_overlay(present=det_present, bbox=det_bbox, image=detentor_signature),
                    club_signature_overlay(present=club_present, bbox=club_bbox, image=club_stamp),
                )
        except Exception as exc:
            logger.warning("OCR for stamp overlay failed; uploading as-is", exc_info=True)
            yield tmp_path, {"stamp_warning": f"OCR failed ({exc}); uploaded without overlays."}
            return

        with overlaid_pdf(tmp_path, *overlays) as (upload_path, results):
            status: dict[str, Any] = {}
            if is_mod1:
                (carimbo_r,) = results
                status["has_club_stamp"] = carimbo_r.effective
                if carimbo_r.error:
                    status["stamp_warning"] = (
                        f"{carimbo_r.error} — document uploaded without the club "
                        "stamp; please stamp it manually."
                    )
            else:
                detentor_r, club_r = results
                status["has_detentor_signature"] = detentor_r.effective
                status["has_club_stamp"] = club_r.effective
                if detentor_r.error:
                    status["signature_warning"] = (
                        f"{detentor_r.error} — document uploaded without the holder "
                        "signature; please sign it manually."
                    )
                if club_r.error:
                    status["stamp_warning"] = (
                        f"{club_r.error} — document uploaded without the club stamp; "
                        "please stamp it manually."
                    )
            yield upload_path, status
    finally:
        if processing_id is not None:
            try:
                close_processing(processing_id)
            except Exception:
                logger.debug("close_processing failed for stamped upload", exc_info=True)


def _parse_one_enrollment_pdf(
    client: SavClient,
    index: int,
    document: dict,
) -> tuple[dict, dict[str, Any] | None]:
    """Parse one enrollment document: the per-entry body of parse_enrollment_forms.

    Runs on a worker thread when several documents are parsed at once (each is
    an independent Document AI round-trip), so it must not touch shared
    mutable state — it returns ``(result_row, artifact | None)`` and the
    caller inserts the artifact into ``_forms`` on the main thread. Never
    raises: every failure comes back as ``{"index": ..., "error": ...}``.
    """
    from sav_parsers import classify, parse_em, parse_fpb_mod1, parse_fpb_mod4, train_classifier

    if not isinstance(document, dict):
        return {"index": index, "error": "Document entry must be an object."}, None
    allowed_keys = {"pdf", "doc_type", "values", "exam_date"}
    unknown_keys = sorted(set(document) - allowed_keys)
    if unknown_keys:
        return {
            "index": index,
            "error": f"Unknown document keys: {', '.join(unknown_keys)}",
        }, None

    pdf_b64 = document.get("pdf")
    if not isinstance(pdf_b64, str) or not pdf_b64.strip():
        return {"index": index, "error": "Missing or invalid required key: pdf"}, None
    hint = document.get("doc_type")
    exam_value = document.get("exam_date")
    exam_date_hint = str(exam_value).strip() if exam_value not in (None, "") else None
    values_hint = document.get("values")
    if values_hint is not None and exam_date_hint is not None:
        return {
            "index": index,
            "error": "A document entry cannot contain both values and exam_date.",
        }, None
    if values_hint is not None and hint not in (None, "fpb_modelo_1"):
        return {
            "index": index,
            "error": "values requires doc_type=\"fpb_modelo_1\" when doc_type is provided.",
        }, None
    if exam_date_hint is not None and hint not in (None, "exame_medico"):
        return {
            "index": index,
            "error": "exam_date requires doc_type=\"exame_medico\" when doc_type is provided.",
        }, None

    try:
        pdf_bytes = base64.b64decode(pdf_b64)
    except (binascii.Error, ValueError) as exc:
        return {"index": index, "error": f"Invalid base64: {exc}"}, None

    tmp_path: str | None = None
    try:
        tmp_path = _pdf_bytes_to_tempfile(pdf_bytes)

        # Date-provided fast path: when the caller already knows the exam
        # date, trust it and accept the PDF as an exame_medico with no OCR —
        # cache the bytes for upload and skip classification + parse_em.
        # Filled-template fast path: a Modelo 1 filled from our fillable
        # template carries its values in the AcroForm — read them directly
        # and skip classification + OCR. An explicit non-mod1 hint opts out.
        mod1_fields = (
            _read_mod1_template_fields(pdf_bytes)
            if (
                hint in (None, "fpb_modelo_1")
                and exam_date_hint is None
                and values_hint is None
            ) else None
        )
        if exam_date_hint is not None:
            doc_type = DocType.EXAME_MEDICO
            parsed = {"exam_date": ParsedField(value=exam_date_hint, confidence=1.0)}
            processing_id = None  # no Document AI session for a date-provided exam
        elif values_hint is not None:
            if not isinstance(values_hint, dict):
                raise ValueError("values must be an object containing canonical Modelo 1 values")
            doc_type = DocType.FPB_MODELO_1
            parsed = mod1_values_to_fields(values_hint)
            processing_id = None  # no Document AI session for caller-supplied values
            reg_type, tier_id, gender_id = derive_enrollment_params(parsed, client)
            tiers = client.list_player_registration_tiers(gender_id=gender_id)
            tier_name = tiers.get(tier_id, str(tier_id))
        elif mod1_fields is not None:
            doc_type = DocType.FPB_MODELO_1
            parsed = mod1_fields
            processing_id = None  # no Document AI session for a form-read doc
            reg_type, tier_id, gender_id = derive_enrollment_params(parsed, client)
            tiers = client.list_player_registration_tiers(gender_id=gender_id)
            tier_name = tiers.get(tier_id, str(tier_id))
        elif hint is not None:
            # Type is already known — skip classify and train the classifier.
            _hint_map = {
                "fpb_modelo_1": DocType.FPB_MODELO_1,
                "exame_medico": DocType.EXAME_MEDICO,
                "fpb_modelo_4": DocType.FPB_MODELO_4,
            }
            if hint not in _hint_map:
                return {"index": index, "error": f"Unknown doc_type hint: {hint!r}"}, None
            doc_type = _hint_map[hint]
            try:
                train_classifier(tmp_path, doc_type)
            except Exception:
                logger.debug("train_classifier failed for hint=%r", hint, exc_info=True)
        else:
            doc_type = classify(tmp_path)

        if exam_date_hint is not None or values_hint is not None or mod1_fields is not None:
            pass  # already handled by a fast path above
        elif doc_type == DocType.FPB_MODELO_1:
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
            return {"index": index, "error": f"Unsupported document type: {doc_type.value!r}"}, None
    except (SavError, ValueError, KeyError, OSError) as exc:
        return {"index": index, "error": str(exc)}, None
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)

    artifact_id = str(uuid.uuid4())
    artifact = {
        "artifact_id": artifact_id,
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

    if doc_type == DocType.FPB_MODELO_1:
        row = {
            "index": index,
            "artifact_id": artifact_id,
            "mod1_id": artifact_id,
            "doc_type": doc_type.value,
            "reg_type": reg_type,
            "reg_type_label": REGISTRATION_TYPE_LABELS.get(reg_type, str(reg_type)),
            "tier_id": tier_id,
            "tier_name": tier_name,
            "gender_id": gender_id,
            "gender_label": GENERO.get(gender_id, str(gender_id)),
        }
    elif doc_type == DocType.FPB_MODELO_4:
        def _f(key: str) -> Any:
            pf = parsed.get(key)
            return pf.value if pf else None
        row = {
            "index": index,
            "artifact_id": artifact_id,
            "mod4_id": artifact_id,
            "doc_type": doc_type.value,
            "nome_jogador": _f("nome_jogador"),
            "licenca_nr": _f("licenca_nr"),
            "escalao_actual": _f("escalao_actual"),
            "escalao_subida": _f("escalao_subida"),
        }
    else:
        row = _build_medical_exam_payload(artifact_id, artifact)
        row.update({"index": index})
    return row, artifact


# Bounded fan-out for multi-PDF parses: each PDF is an independent Document AI
# round-trip (seconds each), so a small pool cuts wall-clock latency without
# hammering the OCR quota.
_PARSE_MAX_WORKERS = 4


@server.tool()
def parse_enrollment_forms(documents: list[dict]) -> list[dict]:
    """
    Parse enrollment-related documents from self-describing entries.

    fpb_modelo_1 forms are parsed for the main enrollment workflow and return
    the batch parameters (registration type, tier, gender). exame_medico
    documents are parsed for step-3 metadata and return a medical_exam_id that
    can be passed to preview_enrollment / submit_enrollment. fpb_modelo_4 forms
    carry no fields — alongside an fpb_modelo_1 their presence adds an inline
    subida de escalão; they return a mod4_id to pass to preview/submit_enrollment.

    Each entry requires ``pdf`` (base64 PDF bytes). ``doc_type`` optionally
    labels it as fpb_modelo_1, exame_medico, or fpb_modelo_4 and skips
    classification. ``values`` supplies trusted canonical Modelo 1 values and
    implies fpb_modelo_1. ``exam_date`` supplies a trusted YYYY-MM-DD date and
    implies exame_medico. Trusted values skip classification and field
    extraction; a filled Modelo 1 AcroForm is also read without OCR.

    Returns one entry per PDF with an artifact_id and canonical doc_type to
    reference in subsequent tools. fpb_modelo_1 entries also include mod1_id;
    exame_medico entries also include medical_exam_id; fpb_modelo_4 entries
    also include mod4_id. On error for a given PDF the entry contains an
    "error" key instead.

    Multiple documents are parsed concurrently (each is an independent Document AI
    round-trip), so a multi-document call costs roughly one parse, not N.
    """
    client = _get_client()
    jobs = list(enumerate(documents))

    # TODO: emit MCP progress per completed PDF. FastMCP's
    # Context.report_progress is async-only, so that requires converting this
    # tool to `async def` (and every direct-call test with it) — deferred.
    if len(jobs) <= 1:
        outcomes = [
            _parse_one_enrollment_pdf(client, index, document)
            for index, document in jobs
        ]
    else:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(_PARSE_MAX_WORKERS, len(jobs))) as pool:
            outcomes = list(pool.map(
                lambda job: _parse_one_enrollment_pdf(client, *job), jobs,
            ))

    # Artifacts are registered on the main thread, in input order, so _forms
    # never sees a partially-built entry from a worker.
    results: list[dict] = []
    for row, artifact in outcomes:
        if artifact is not None:
            _forms[artifact.pop("artifact_id")] = artifact
        results.append(row)
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
def ensure_open_batch(reg_type: int, tier_id: int, gender_id: int) -> dict:
    """
    Get-or-create the open ("Em construção") registration batch for the given
    type, tier, and gender in a single call.

    Returns the existing open batch if one already matches, otherwise creates a
    new one — the same dict shape as find_open_batch / create_batch plus a
    ``created`` flag (true when a fresh batch was created, false when an
    existing open batch was reused). Prefer this over the find/create pair: it
    is one round-trip and avoids the create-while-open race.
    """
    client = _get_client()
    batch = client.find_open_player_registration_batch(
        type=reg_type, tier_id=tier_id, gender_id=gender_id,
    )
    created = False
    if batch is None:
        _, batch = create_and_fetch_batch(
            client, batch_type=reg_type, tier_id=tier_id, gender_id=gender_id,
        )
        created = True
    return {
        "number": batch.number,
        "type": batch.type,
        "tier": batch.tier,
        "gender": batch.gender,
        "item_count": batch.item_count,
        "created": created,
    }


def _find_batch_by_number(client: SavClient, batch_number: str):
    """Return the batch object with `number == batch_number` (one listing call).

    Raises ValueError with the same message resolve_batch_id uses when no batch
    matches, so callers surface an identical "not found" error.
    """
    batch = next(
        (b for b in client.list_player_registration_batches() if b.number == batch_number),
        None,
    )
    if batch is None:
        raise ValueError(f"Batch {batch_number!r} not found")
    return batch


def _resolve_primeira_player(client: SavClient, form: dict[str, Any]) -> dict:
    """Resolve a 1ª Inscrição (type-1) form: OCR echo + pre-emptive dup guard.

    Shared by resolve_player and preview_enrollment. Returns the resolved OCR
    dict, or the ``player_already_in_sav`` structured error when the op=11
    duplicate probe hits.
    """
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


def _resolve_revalidacao_player(client: SavClient, form: dict[str, Any], batch) -> dict:
    """Resolve a Revalidação (type-2) form against the batch's eligible list.

    Shared by resolve_player and preview_enrollment. Returns
    ``{resolved: true, license}`` on a single match, else
    ``{resolved: false, candidates, ocr_name, ocr_license}``.
    """
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

    preview_enrollment(license=null) folds this resolution in for the common
    unambiguous case; call resolve_player directly when you need explicit
    control (e.g. to show the candidate list before previewing).

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
    batch = _find_batch_by_number(client, batch_number)

    if batch.type_id == 1:
        return _resolve_primeira_player(client, form)
    return _resolve_revalidacao_player(client, form, batch)


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

    license: pass null (or 0) to auto-resolve the player from the form —
    preview folds in resolve_player's logic (the common unambiguous case, no
    user decision needed between the two steps). For a Revalidação that
    resolves to exactly one licence the normal preview proceeds and the
    response's `player.license` carries it plus `resolved: true`. When
    resolution is ambiguous or fails, preview returns the same
    ``{resolved: false, candidates, ocr_name, ocr_license}`` shape
    resolve_player does (Revalidação) or ``{resolved: false, error:
    "player_already_in_sav", ...}`` (1ª Inscrição duplicate guard) instead of
    raising — the caller shows the choice to the user and re-calls preview with
    an explicit licence. Pass an explicit licence to skip auto-resolution
    entirely (behaviour is identical to before this fold-in).

    batch_number is load-bearing when license is null (it names the batch whose
    eligible list drives auto-resolution); with an explicit licence it is
    validated only when submit_enrollment is called.
    """
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

    # Auto-resolve when no licence was supplied: fold in resolve_player's
    # logic so the unambiguous common case skips the extra round-trip. The
    # type-1 duplicate guard runs here regardless of licence-source symmetry;
    # the type-2 branch below both resolves and short-circuits on ambiguity.
    resolved_flag = False
    if license in (None, 0):
        if reg_type == 1:
            resolution = _resolve_primeira_player(client, form)
            if not resolution.get("resolved"):
                return resolution  # player_already_in_sav
        else:
            batch = _find_batch_by_number(client, batch_number)
            resolution = _resolve_revalidacao_player(client, form, batch)
            if not resolution.get("resolved"):
                return resolution  # ambiguous / no match → caller picks
            license = resolution["license"]
            resolved_flag = True

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
        _append_minor_guardian_review(preview, kwargs.get("birth_date"))
    else:
        if license in (None, 0):
            raise ValueError(
                f"Preview requires a licence for this registration type; pass "
                f"license (or license: null to auto-resolve from the form). "
                f"Got {license!r}."
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
            # Copy: _append_minor_guardian_review may extend this list, and the
            # cached result.needs_review must stay pristine — submit stages OCR
            # corrections from it, and chat-supplied guardian answers must not
            # be labeled onto a form whose guardian block may be blank.
            "needs_review": list(result.needs_review),
            "reg_type": reg_type,
            "reg_type_label": reg_type_label,
            "inline_subida": inline_subida,
            "enrollment_route": enrollment_route,
        }
        if resolved_flag:
            preview["resolved"] = True
        _append_minor_guardian_review(preview, sav_profile.get("nasc"))
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
    detentor_signature_b64: str | None = None,
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
    medical_exam_id is supplied. For minors these guardian fields already
    surface in preview_enrollment's needs_review list (up front), so answer
    them there; the missing_guardian_fields response below is a fallback for
    when they are still absent at submit time.

    mod4_id (from parse_enrollment_forms) adds an inline subida de escalão to
    this 1ª Inscrição / Revalidação: the target tier is fetched from SAV and
    committed, and the mod4 is uploaded as a supporting document. Submitting
    fails if SAV offers no subida tier for the player. (This is the inline
    rider, not a standalone type-4 Subida batch.) When detentor_signature_b64
    is supplied, it is overlaid onto the mod4's empty holder-signature slot
    (and $CLUB_STAMP_PATH onto the club slot) before that upload.

    nif: optional explicit subject claim — the athlete's NIF that the caller
    asserts this enrollment is for. Used by downstream wrappers to enforce
    self-scope on 1ª Inscrição (where there is no licence yet) and as a
    defense-in-depth cross-check against the form's OCR'd NIF: if both are
    set and disagree, the call is rejected.

    Returns:
      success=true + player_id (+ license for 1ª Inscrição) on success.
      success=false + missing_guardian_fields  fallback for when a minor's
        guardian info is still absent at submit time (preview_enrollment
        already lists these in needs_review) — call submit_enrollment again
        with those fields added to field_overrides.
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
            parsed=mod4.get("parsed"),
            detentor_signature=(
                base64.b64decode(detentor_signature_b64) if detentor_signature_b64 else None
            ),
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
    # form-read (template-filled) mod1s have no Document AI session to close.
    if form["processing_id"] is not None:
        try:
            close_processing(form["processing_id"], corrections=corrections or None)
        except Exception:
            logger.debug("close_processing failed for form", exc_info=True)
    # date-provided (no-OCR) exams have no Document AI session to close.
    if medical_exam is not None and medical_exam["processing_id"] is not None:
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
    detentor_signature_b64: str | None = None,
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

    When detentor_signature_b64 is supplied it is overlaid onto the mod4's
    holder-signature slot (and $CLUB_STAMP_PATH onto the club slot) if OCR found
    the slot empty, before the upload — mirroring the CLI submit path.

    Args:
        batch_number:  Human-visible Subida batch number.
        license:       Player licence (must already exist in SAV).
        mod4_id:       Artifact id of an fpb_modelo_4 from parse_enrollment_forms.
        detentor_signature_b64: Optional base64 PNG/JPG of the holder (detentor
                       paternal) signature to overlay onto the empty holder slot.

    Returns:
        success=true + license + name + subida_document_upload on success.
    """
    form = _forms.get(mod4_id)
    if form is None:
        raise ValueError(f"Unknown mod4_id: {mod4_id!r}")
    if form.get("doc_type") != DocType.FPB_MODELO_4:
        raise ValueError(f"Artifact {mod4_id!r} is not an fpb_modelo_4 form")

    client = _get_client()
    # One listing call: find the batch by number and read its id directly.
    # resolve_batch_id would list too (on cache miss) and we still needed the
    # batch object for the type guard — folding both into a single lookup.
    # Unknown number raises the same "Batch {number!r} not found" as
    # resolve_batch_id did.
    batch = _find_batch_by_number(client, batch_number)
    batch_id = batch.id
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
        parsed=form.get("parsed"),
        detentor_signature=(
            base64.b64decode(detentor_signature_b64) if detentor_signature_b64 else None
        ),
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
      Exam (re-commits step-3): exam_date (YYYY-MM-DD). Setting it re-fires
        the op=36 commit to write the new exam date. SAV2 has no read-back of
        the item's saved step-3 selections, so the re-commit re-derives
        taxa/insurance and takes guardian/consent from the values passed here:
        an exam_date edit that omits them resets consents on / subida off.
        Pass guardian_name, guardian_relation (int), guardian_phone,
        guardian_email (required for minors) and consent_data,
        consent_communications, consent_marketing (bools) to preserve them.

    Returns: {"success": True, "player_id": int} on success, or
    {"error": "license_not_enrolled", "license": int, "open_batches": [...]}
    if the licence is not enrolled in any open batch.
    """
    allowed = {
        "id_type", "id_number", "id_expiry", "telemovel", "telefone",
        "email", "nome_pai", "nome_mae",
        "morada", "cod_postal", "localidade_txt",
        "distrito_id", "concelho_id",
        "exam_date",
        "guardian_name", "guardian_relation", "guardian_phone", "guardian_email",
        "consent_data", "consent_communications", "consent_marketing",
    }
    unknown = sorted(set(fields) - allowed)
    if unknown:
        raise ValueError(
            f"Unsupported field(s) for update_enrollment: {unknown}. "
            f"Allowed: {sorted(allowed)}."
        )
    int_keys = {"id_type", "distrito_id", "concelho_id", "guardian_relation"}
    bool_keys = {"consent_data", "consent_communications", "consent_marketing"}
    coerced: dict[str, Any] = {}
    for k, v in fields.items():
        if v is None:
            continue
        if k in bool_keys:
            coerced[k] = bool(v)
        elif k in int_keys and not isinstance(v, int):
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
    mod1_values: dict | None = None,
    field_overrides: dict[str, Any] | None = None,
    file_only: bool = False,
    detentor_signature_b64: str | None = None,
) -> dict:
    """
    Reconcile a new PDF against an existing enrolment and patch fields / replace document.

    Equivalent of `sav enrollment update --license LICENSE FILE [--mod1] [--field ...] [--file-only]`.

    The batch is resolved automatically from the license.

    pdf: base64-encoded PDF.
    doc_type: optional type hint — "fpb_modelo_1" or "exame_medico". When given,
    classification is skipped and the classifier is trained with the known label.
    mod1_values: trusted canonical Modelo 1 values (the fill_mod1 shape). When
    supplied, the document is treated as fpb_modelo_1 and classification and
    field extraction are skipped, even when the PDF has no AcroForm.
    field_overrides: optional field values applied on top of reconcile result before
    submitting (same keys as update_enrollment). Only valid when file_only=False.
    file_only: when True, replace the document without touching fields. A mod1/mod4
    still gets the club stamp ($CLUB_STAMP_PATH) — and, for a mod4,
    detentor_signature_b64 — overlaid onto empty slots before the replace.
    detentor_signature_b64: base64 PNG/JPG of the holder (detentor paternal)
    signature to overlay onto an empty mod4 holder slot.

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

        if mod1_values is not None and doc_type not in (None, "fpb_modelo_1"):
            raise ValueError(
                "mod1_values requires doc_type='fpb_modelo_1' when doc_type is provided."
            )

        # Caller-values take precedence over the filled-template AcroForm probe;
        # both skip classification and Document AI field extraction.
        mod1_fields = (
            mod1_values_to_fields(mod1_values)
            if mod1_values is not None
            else (
                _read_mod1_template_fields(pdf_bytes)
                if doc_type in (None, "fpb_modelo_1") else None
            )
        )

        _hint_map = {"fpb_modelo_1": DocType.FPB_MODELO_1, "exame_medico": DocType.EXAME_MEDICO}
        if mod1_fields is not None:
            active_doc_type = DocType.FPB_MODELO_1
        elif doc_type is not None:
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
            # Replace without touching fields — but for a mod1/mod4 still overlay
            # the club stamp (and mod4 holder signature) onto empty slots.
            detentor_signature = (
                base64.b64decode(detentor_signature_b64) if detentor_signature_b64 else None
            )
            with _stamped_upload_path(
                tmp_path, active_doc_type, detentor_signature=detentor_signature,
            ) as (upload_path, status):
                client.replace_player_registration_document(batch_id, license, upload_path, tipo_doc=tipo_doc)
            return {"success": True, "fields_updated": False, "document_uploaded": True, **status}

        if active_doc_type != DocType.FPB_MODELO_1:
            raise ValueError(
                f"Document type {active_doc_type.value!r} cannot be reconciled; "
                "only fpb_modelo_1 forms are supported. Use file_only=True to upload as-is."
            )

        if mod1_fields is not None:
            parsed = mod1_fields
            processing_id = None  # no Document AI session for a form-read doc
        else:
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
            # form-read (template-filled) mod1s have no Document AI session.
            if processing_id is not None:
                try:
                    close_processing(processing_id, corrections=corrections or None)
                except Exception:
                    logger.debug("close_processing failed", exc_info=True)
        finally:
            if not close_called and processing_id is not None:
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


def _projected_enrollment_checklist(
    client, license: int, reg_type: int, club_id: int,
) -> dict | None:
    """Document checklist for a licence that is *not* in an open batch.

    Grounds the portuguese-vs-foreign-born split in the player's actual
    record — the `nacional` (nationality) id read from the op=2 profile —
    so "what will player X need to enrol" can be answered before any batch
    exists. There are no uploaded documents to count yet, so every required
    entry comes back unsatisfied; `projected=True` flags this so callers
    don't read `found_count`/`missing` as a verified gap (unlike the
    "pending" checklist, which counts docs attached to the live batch).

    Nationality lookup failures fall back to `nacional_id=None`, which
    `compute_enrollment_checklist` treats as foreign_born — the safe error,
    since it asks for more documents rather than fewer.
    """
    try:
        profile = client.load_player_profile(license, club_id=club_id or None)
    except SavError:
        logger.debug(
            "Profile lookup failed for projected checklist (license=%s)",
            license, exc_info=True,
        )
        profile = {}
    nacional_raw = profile.get("nacional")
    try:
        nacional_id = int(nacional_raw) if nacional_raw not in (None, "") else None
    except (TypeError, ValueError):
        nacional_id = None
    checklist = compute_enrollment_checklist(reg_type, nacional_id, [])
    if checklist is not None:
        checklist["projected"] = True
    return checklist


@server.tool()
def get_enrollment_status(
    license: int, reg_type: int = REGISTRATION_TYPE_REVALIDACAO,
) -> dict:
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

    Every status carries a `checklist` ({scenario, reg_type, required,
    optional, missing}). Scenario is "portuguese" or "foreign_born" for
    reg_type 1/2, "subida_standalone" for reg_type 4, and the checklist is
    null for reg_type 3 (Transferência is not handled yet).

    For "pending" the checklist reflects the live batch — its `reg_type`
    and the documents actually uploaded to it (the `reg_type` argument is
    ignored). For "enrolled" and "not_enrolled" there is no batch, so the
    checklist is a *projection* of what the player would need to enrol:
    nationality is grounded in their stored record, `reg_type` selects the
    scenario (defaults to Revalidação/1ª Inscrição, which share the same
    document split), and it carries `projected: true` with every required
    entry unsatisfied (no batch to hold uploads yet).

    The checklist mirrors FPB policy (the SAV API doesn't expose it):
      portuguese: fpb_modelo_1, exame_medico; optional fpb_modelo_4.
      foreign_born: fpb_modelo_1, exame_medico, atestado_residencia,
        certidao_matricula, documento_identificacao × 2 (passaporte +
        título de residência, player's or parent's). Both fall under
        SAV tipo_doc=18, so the rule reports counts (need ≥2, found N).

    For "enrolled" the response also carries `player` ({license, name,
    tier, club}). For "not_enrolled" it carries `open_batches` — the
    currently open batches the caller could join.
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
        checklist = _projected_enrollment_checklist(
            client, license, reg_type, club_id,
        )
        if roster_hits:
            return {
                "license": license,
                "status": "enrolled",
                "player": player_to_dict(roster_hits[0]),
                "checklist": checklist,
            }
        return {
            "license": license,
            "status": "not_enrolled",
            "open_batches": exc.open_batches,
            "checklist": checklist,
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
def enrollment_status_bulk(licenses: list[int]) -> list[dict]:
    """
    Classify many players' enrollment status in one pass.

    The bulk counterpart to get_enrollment_status. Calling that per licence
    is N+1 — each one re-lists batches, scans every open batch's items, then
    probes the roster. This shares that work (one batch listing, one item
    scan per open batch, one roster query) and classifies every licence in
    memory, so cost is O(open batches) + O(roster), independent of how many
    licences you pass.

    Returns one row per input licence, in the order given:
      pending      → {"license", "status", "batch": {number, type_id, type,
                      state}, "name"}
      enrolled     → {"license", "status", "name"}   (active in the roster)
      not_enrolled → {"license", "status", "open_batches": [...]}

    Each row carries the status label plus just enough context to act on it
    (which batch a pending player sits in; which open batches a not-enrolled
    player could join). Rows deliberately omit the per-player document
    `checklist` that get_enrollment_status returns: that reads the live batch
    or the player's stored nationality, so it stays a single-player call. Use
    get_enrollment_status(license) when you need the checklist.
    """
    client = _get_client()
    classified = client.classify_enrollment_status(licenses)
    return [{"license": int(lic), **classified[int(lic)]} for lic in licenses]


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
    detentor_signature_b64: str | None = None,
) -> dict:
    """
    Upload a document (PDF, base64-encoded) attached to a player's registration.

    The batch is resolved automatically from the license.

    doc_type: one of exame_medico, fpb_modelo_1, fpb_modelo_4,
    atestado_residencia, documento_identificacao, certidao_matricula, outros.
    When omitted, sav-parsers classifies the PDF first.
    Types recognized by sav-parsers but without a SAV2 tipo_doc mapping fail
    before the SAV2 call.

    For a mod1 or mod4 the club stamp ($CLUB_STAMP_PATH) is overlaid onto its
    slot when OCR finds it empty, and detentor_signature_b64 (base64 PNG/JPG) is
    overlaid onto a mod4's empty holder-signature slot. Other doc types are
    uploaded as-is with no OCR.

    Returns {"success": True} (plus has_club_stamp / has_detentor_signature for
    mod1/mod4) on success, or {"error": "license_not_enrolled", "license": int,
    "open_batches": [...]} if the licence is not enrolled in any open batch.
    """
    tmp_path = _decode_pdf_to_tempfile(pdf_base64)
    try:
        resolved_doc_type = _resolve_document_upload_type(tmp_path, doc_type)
        tipo_doc = doc_type_to_tipo_doc(resolved_doc_type)
        client = _get_client()
        batch_id = _resolve_license_batch(client, license)
        if isinstance(batch_id, dict):
            return batch_id
        detentor_signature = (
            base64.b64decode(detentor_signature_b64) if detentor_signature_b64 else None
        )
        with _stamped_upload_path(
            tmp_path, resolved_doc_type, detentor_signature=detentor_signature,
        ) as (upload_path, status):
            client.upload_player_registration_document(
                batch_id, license, upload_path, tipo_doc=tipo_doc,
            )
        return {"success": True, **status}
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
    detentor_signature_b64: str | None = None,
) -> dict:
    """
    Replace any existing documents of `doc_type` for this player with a
    new PDF (base64-encoded). Idempotent on the upload side: when no existing
    doc of the translated SAV2 tipo_doc is found, behaves like a plain upload.

    The batch is resolved automatically from the license.

    For a mod1 or mod4 the club stamp ($CLUB_STAMP_PATH) is overlaid onto its
    slot when OCR finds it empty, and detentor_signature_b64 (base64 PNG/JPG) is
    overlaid onto a mod4's empty holder-signature slot. Other doc types are
    replaced as-is with no OCR.

    Returns {"success": True} (plus has_club_stamp / has_detentor_signature for
    mod1/mod4) on success, or {"error": "license_not_enrolled", "license": int,
    "open_batches": [...]} if the licence is not enrolled in any open batch.
    """
    tmp_path = _decode_pdf_to_tempfile(pdf_base64)
    try:
        resolved_doc_type = _resolve_document_upload_type(tmp_path, doc_type)
        tipo_doc = doc_type_to_tipo_doc(resolved_doc_type)
        client = _get_client()
        batch_id = _resolve_license_batch(client, license)
        if isinstance(batch_id, dict):
            return batch_id
        detentor_signature = (
            base64.b64decode(detentor_signature_b64) if detentor_signature_b64 else None
        )
        with _stamped_upload_path(
            tmp_path, resolved_doc_type, detentor_signature=detentor_signature,
        ) as (upload_path, status):
            client.replace_player_registration_document(
                batch_id, license, upload_path, tipo_doc=tipo_doc,
            )
        return {"success": True, **status}
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
