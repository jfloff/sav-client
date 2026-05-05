"""
MCP server for the FPB SAV2 system.

Exposes read access to players, clubs, games, and registration batches, plus a
multi-step player-enrollment workflow that orchestrates OCR-parsed FPB forms
against the SAV2 API.  Tools are designed to be stateless-friendly so an LLM
agent can drive them without an interactive UI — the chat is the confirmation
loop.

Enrollment workflow:
    1. parse_enrollment_forms  → form_id(s) + batch params per PDF
    2. find_open_batch / create_batch  → batch_id
    3. resolve_player  → license (or candidate list if ambiguous)
    4. preview_enrollment  → full reconciled profile for user review
    5. submit_enrollment  → player_id  (or missing_guardian_fields on minor)
"""

from __future__ import annotations

import base64
import os
import tempfile
import uuid
from typing import Any

from mcp.server.fastmcp import FastMCP

from sav_client import SavClient
from sav_client.exceptions import SavConfigError, SavConnectionError, SavResponseError
from sav_shared import (
    batch_to_dict,
    club_to_dict,
    create_and_fetch_batch,
    derive_enrollment_params,
    ENROLLMENT_FIELD_META,
    filter_games,
    game_to_dict,
    parse_missing_guardian_fields,
    player_to_dict,
    REGISTRATION_TYPE_LABELS,
    resolve_player_candidates,
)

server = FastMCP("FPB SAV")

# ── Singleton SAV client ──────────────────────────────────────────────────────

_client: SavClient | None = None


def _get_client() -> SavClient:
    global _client
    if _client is None:
        _client = SavClient.from_env()
        _client.login()
    return _client


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
) -> list[dict]:
    """
    Search for players in the SAV system.

    club_id defaults to the session's own club when omitted.
    Pass club_id=0 to search all clubs (federation-wide or scoped by association_id).
    status: "active" | "inactive" | "all"
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
    )
    return [player_to_dict(p) for p in players]


@server.tool()
def get_player(license: str, club_id: int | None = None) -> dict | None:
    """
    Return details for a single player by licence number.

    club_id defaults to the session's own club when omitted.
    Returns null if no player is found with that licence.
    """
    client = _get_client()
    effective_club: int = (
        club_id if club_id is not None
        else int(client.session.get("organizacao") or 0)
    )
    results = client.search_players(license=license, club=effective_club)
    if not results:
        return None
    return player_to_dict(results[0])


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
    List games for the session's club.

    date_from / date_to: DD-MM-YYYY format.
    status: e.g. "Marcado", "Não Marcado". Omit to return all statuses.
    """
    client = _get_client()
    results = client.list_games(
        season=season, tier=tier, gender=gender,
        date_from=date_from, date_to=date_to,
    )
    if status:
        results = [g for g in results if g.game_status == status]
    return [game_to_dict(g) for g in results]


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
def list_batches(season: int | None = None) -> list[dict]:
    """
    List player registration batches visible to the session's club.

    Includes all states (Em construção, Devolvida, Em Validação, Em Pagamento).
    season defaults to the current season when omitted.
    """
    client = _get_client()
    batches = client.list_player_registration_batches(season=season)
    return [batch_to_dict(b) for b in batches]


# ── Enrollment workflow ───────────────────────────────────────────────────────
# In-memory form cache, keyed by form_id (UUID string). Populated by
# parse_enrollment_forms, extended by preview_enrollment (adds reconcile_result
# + sav_profile).

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


@server.tool()
def parse_enrollment_forms(pdfs: list[str]) -> list[dict]:
    """
    Parse one or more FPB registration PDFs provided as base64-encoded bytes.

    Classifies each document, extracts fields via OCR, and derives the batch
    parameters (registration type, tier, gender) from the form checkboxes.

    Returns one entry per PDF with a form_id to reference in subsequent tools.
    On error for a given PDF the entry contains an "error" key instead.
    """
    from sav_parsers import classify, parse_fpb_mod1

    client = _get_client()
    results: list[dict] = []

    for i, pdf_b64 in enumerate(pdfs):
        try:
            pdf_bytes = base64.b64decode(pdf_b64)
        except Exception as exc:
            results.append({"index": i, "error": f"Invalid base64: {exc}"})
            continue

        tmp_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
                f.write(pdf_bytes)
                tmp_path = f.name

            doc_type = classify(tmp_path)
            if doc_type != "fpb-mod1":
                results.append({"index": i, "error": f"Unsupported document type: {doc_type!r}"})
                continue

            parse_result = parse_fpb_mod1(tmp_path)
            parsed = parse_result["fields"]
            processing_id = parse_result["processing_id"]
            reg_type, tier_id, gender_id, tiers = derive_enrollment_params(parsed, client)
            tier_name = tiers.get(tier_id, str(tier_id))
        except Exception as exc:
            results.append({"index": i, "error": str(exc)})
            continue
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

        form_id = str(uuid.uuid4())
        _forms[form_id] = {
            "parsed": parsed,
            "processing_id": processing_id,
            "doc_type": doc_type,
            "reg_type": reg_type,
            "tier_id": tier_id,
            "gender_id": gender_id,
        }

        results.append({
            "index": i,
            "form_id": form_id,
            "reg_type": reg_type,
            "reg_type_label": REGISTRATION_TYPE_LABELS.get(reg_type, str(reg_type)),
            "tier_id": tier_id,
            "tier_name": tier_name,
            "gender_id": gender_id,
            "gender_label": "Feminino" if gender_id == 2 else "Masculino",
        })

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
        "batch_id": batch.id,
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
    Returns the new batch details including its batch_id.
    """
    client = _get_client()
    _, batch = create_and_fetch_batch(
        client, type=reg_type, tier_id=tier_id, gender_id=gender_id,
    )
    return {
        "batch_id": batch.id,
        "number": batch.number,
        "type": batch.type,
        "tier": batch.tier,
        "gender": batch.gender,
        "item_count": batch.item_count,
    }


@server.tool()
def resolve_player(batch_id: int, form_id: str) -> dict:
    """
    Resolve the player for a parsed form against the batch's eligible list.

    Tries the OCR licence number first, then falls back to a name search
    scoped to the batch's club.

    Returns:
      resolved=true + license  when exactly one match is found.
      resolved=false + candidates  when multiple players match (user must pick).
      resolved=false + empty candidates  when no match found (user must supply licence).
    """
    form = _forms.get(form_id)
    if form is None:
        raise ValueError(f"Unknown form_id: {form_id!r}")

    client = _get_client()
    batches = client.list_player_registration_batches()
    batch = next((b for b in batches if b.id == batch_id), None)
    if batch is None:
        raise ValueError(f"Batch {batch_id} not found")

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
def preview_enrollment(batch_id: int, license: int, form_id: str) -> dict:
    """
    Preview the enrollment for a player.

    Fetches the player's current SAV profile, runs OCR reconciliation, and
    returns the full field-by-field picture:
      - Fields that match SAV (status: match) — shown for transparency
      - Fields where OCR overrides SAV (status: updated)
      - Fields where OCR confidence is too low to trust (status: needs_review)
        — the user should confirm or correct these before submitting
      - Fields with no SAV equivalent (status: ocr) — id_type, guardian_*, consent_*

    The reconciliation result is cached internally so submit_enrollment can
    use it without repeating the network call.
    """
    from sav_shared import reconcile_fpb_mod1

    form = _forms.get(form_id)
    if form is None:
        raise ValueError(f"Unknown form_id: {form_id!r}")

    client = _get_client()
    sav_profile = client._load_player_record(batch_id, license)
    result = reconcile_fpb_mod1(form["parsed"], sav_profile)

    form["reconcile_result"] = result
    form["sav_profile"] = sav_profile

    return {
        "player": {
            "name": sav_profile.get("nome", ""),
            "license": license,
            "birth_date": sav_profile.get("nasc", ""),
        },
        "fields": _build_preview_fields(result, sav_profile),
        "needs_review": result.needs_review,
    }


@server.tool()
def submit_enrollment(
    batch_id: int,
    license: int,
    form_id: str,
    field_overrides: dict[str, Any] | None = None,
) -> dict:
    """
    Submit the player enrollment using the reconciled data from preview_enrollment.

    field_overrides should supply values for every field listed in
    needs_review plus any guardian fields required for minors
    (guardian_name, guardian_relation, guardian_phone, guardian_email).

    Returns:
      success=true + player_id  on success.
      success=false + missing_guardian_fields  when the player is a minor and
        guardian info is absent — call submit_enrollment again with those fields
        added to field_overrides.
    """
    from sav_parsers import close_processing
    from sav_shared import KWARG_TO_ENTITY

    form = _forms.get(form_id)
    if form is None:
        raise ValueError(f"Unknown form_id: {form_id!r}")

    result = form.get("reconcile_result")
    if result is None:
        raise ValueError("Call preview_enrollment before submit_enrollment")

    client = _get_client()

    kwargs = dict(result.kwargs)
    kwargs.pop("license", None)
    if field_overrides:
        kwargs.update(field_overrides)

    try:
        player_id = client.add_player_to_registration_batch(batch_id, license, **kwargs)
    except SavConfigError as exc:
        return {
            "success": False,
            "missing_guardian_fields": parse_missing_guardian_fields(exc),
        }

    # Only send corrections the user explicitly answered (needs_review).
    # Updated/kept were silent paths — staging them risks dataset noise.
    corrections: dict[str, str] = {}
    for kwarg in result.needs_review:
        entity = KWARG_TO_ENTITY.get(kwarg)
        val = kwargs.get(kwarg)
        if entity and val is not None:
            corrections[entity] = str(val)
    try:
        close_processing(form["processing_id"], corrections=corrections or None)
    except Exception:
        pass

    sav_profile = form.get("sav_profile", {})
    return {
        "success": True,
        "player_id": player_id,
        "license": license,
        "name": sav_profile.get("nome", ""),
    }


def main() -> None:
    server.run()
