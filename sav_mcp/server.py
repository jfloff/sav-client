"""
MCP server for the FPB SAV2 system.

Exposes read access to players, clubs, games, and registration batches, plus a
multi-step player-enrollment workflow that orchestrates OCR-parsed FPB forms
against the SAV2 API.  Tools are designed to be stateless-friendly so an LLM
agent can drive them without an interactive UI — the chat is the confirmation
loop.

Enrollment workflow:
    1. parse_enrollment_forms  → mod1_id(s) / medical_exam_id(s) + parsed metadata
    2. find_open_batch / create_batch  → batch_id
    3. resolve_player  → license (or candidate list if ambiguous)
    4. preview_enrollment  → full reconciled profile (+ optional medical exam sidecar)
    5. submit_enrollment  → player_id (auto-uploads fpb_modelo_1 and optional exame_medico)

Document tools (post-enrollment, ad-hoc):
    list_player_documents / upload_player_document /
    delete_player_document / replace_player_document
    use sav-parsers doc_type strings and translate to SAV2 tipo_doc internally.
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
from sav_shared.enrollment import (
    create_and_fetch_batch,
    derive_enrollment_params,
    parse_missing_guardian_fields,
    resolve_player_candidates,
)
from sav_shared.fields import ENROLLMENT_FIELD_META, KWARG_TO_ENTITY
from sav_shared.fpb_mod1 import reconcile_fpb_mod1
from sav_shared.games import filter_games
from sav_shared.lookups import GENERO, REGISTRATION_TYPE_LABELS, doc_type_to_tipo_doc, tipo_doc_to_doc_type
from sav_shared.medical_exam import extract_medical_exam_info
from sav_shared.serializers import (
    batch_to_dict,
    club_to_dict,
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
) -> dict[str, Any]:
    """Upload cached PDF bytes as a replacement registration document."""
    status = {
        "doc_type": doc_type.value,
        "status": "skipped",
        "error": None,
    }
    if not pdf_bytes:
        return status

    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(pdf_bytes)
            tmp_path = f.name
        client.replace_player_registration_document(
            batch_id,
            license,
            tmp_path,
            tipo_doc=doc_type_to_tipo_doc(doc_type),
        )
        status["status"] = "ok"
    except (SavConnectionError, SavResponseError, FileNotFoundError, ValueError) as exc:
        status["status"] = "error"
        status["error"] = str(exc)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
    return status


@server.tool()
def parse_enrollment_forms(pdfs: list[str]) -> list[dict]:
    """
    Parse one or more enrollment-related PDFs provided as base64-encoded bytes.

    fpb_modelo_1 forms are parsed for the main enrollment workflow and return
    the batch parameters (registration type, tier, gender). exame_medico
    documents are parsed for step-3 metadata and return a medical_exam_id that
    can be passed to preview_enrollment / submit_enrollment.

    Returns one entry per PDF with an artifact_id and canonical doc_type to
    reference in subsequent tools. fpb_modelo_1 entries also include mod1_id;
    exame_medico entries also include medical_exam_id. On error for a given
    PDF the entry contains an "error" key instead.
    """
    from sav_parsers import classify, parse_em, parse_fpb_mod1, train_classifier

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
            if doc_type == DocType.FPB_MOD1:
                parse_result = parse_fpb_mod1(tmp_path)
                parsed = parse_result["fields"]
                processing_id = parse_result["processing_id"]
                reg_type, tier_id, gender_id = derive_enrollment_params(parsed, client)
                tiers = client.list_player_registration_tiers(gender_id=gender_id)
                tier_name = tiers.get(tier_id, str(tier_id))
            elif doc_type == DocType.EM:
                parse_result = parse_em(tmp_path)
                parsed = parse_result["fields"]
                processing_id = parse_result["processing_id"]
                try:
                    train_classifier(tmp_path, DocType.EM)
                except Exception:
                    pass
            else:
                results.append({"index": i, "error": f"Unsupported document type: {doc_type.value!r}"})
                continue
        except Exception as exc:
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
        if doc_type == DocType.FPB_MOD1:
            artifact.update({
                "reg_type": reg_type,
                "tier_id": tier_id,
                "gender_id": gender_id,
            })
        _forms[artifact_id] = artifact

        if doc_type == DocType.FPB_MOD1:
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
        client, batch_type=reg_type, tier_id=tier_id, gender_id=gender_id,
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
def resolve_player(batch_id: int, mod1_id: str) -> dict:
    """
    Resolve the player for a parsed form against the batch's eligible list.

    Tries the OCR licence number first, then falls back to a name search
    scoped to the batch's club.

    Returns:
      resolved=true + license  when exactly one match is found.
      resolved=false + candidates  when multiple players match (user must pick).
      resolved=false + empty candidates  when no match found (user must supply licence).
    """
    form = _forms.get(mod1_id)
    if form is None:
        raise ValueError(f"Unknown mod1_id: {mod1_id!r}")
    if form.get("doc_type") != DocType.FPB_MOD1:
        raise ValueError(f"Artifact {mod1_id!r} is not an fpb_modelo_1 enrollment form")

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
def preview_enrollment(
    batch_id: int,
    license: int,
    mod1_id: str,
    medical_exam_id: str | None = None,
) -> dict:
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
    use it without repeating the network call. When medical_exam_id is
    supplied, the response also includes a `medical_exam` sidecar with the
    parsed step-3 exam metadata.
    """
    form = _forms.get(mod1_id)
    if form is None:
        raise ValueError(f"Unknown mod1_id: {mod1_id!r}")
    if form.get("doc_type") != DocType.FPB_MOD1:
        raise ValueError(f"Artifact {mod1_id!r} is not an fpb_modelo_1 enrollment form")

    client = _get_client()
    # resolve_player runs first in this workflow → search_players already
    # populated the license→id cache, so this is free.
    sav_profile = client.load_player_profile(license)
    result = reconcile_fpb_mod1(form["parsed"], sav_profile, client=client)

    form["reconcile_result"] = result
    form["sav_profile"] = sav_profile

    preview = {
        "player": {
            "name": sav_profile.get("nome", ""),
            "license": license,
            "birth_date": sav_profile.get("nasc", ""),
        },
        "fields": _build_preview_fields(result, sav_profile),
        "needs_review": result.needs_review,
    }
    if medical_exam_id is not None:
        artifact = _forms.get(medical_exam_id)
        if artifact is None:
            raise ValueError(f"Unknown medical_exam_id: {medical_exam_id!r}")
        if artifact.get("doc_type") != DocType.EM:
            raise ValueError(f"Artifact {medical_exam_id!r} is not an exame_medico parse")
        preview["medical_exam"] = _build_medical_exam_payload(medical_exam_id, artifact)
    return preview


@server.tool()
def submit_enrollment(
    batch_id: int,
    license: int,
    mod1_id: str,
    field_overrides: dict[str, Any] | None = None,
    medical_exam_id: str | None = None,
) -> dict:
    """
    Submit the player enrollment using the reconciled data from preview_enrollment.

    field_overrides should supply values for every field listed in
    needs_review plus any guardian fields required for minors
    (guardian_name, guardian_relation, guardian_phone, guardian_email).
    It must include exam_date (YYYY-MM-DD) when no usable medical exam date
    is available. It may also override any parsed exame_medico date when
    medical_exam_id is supplied.

    Returns:
      success=true + player_id  on success.
      success=false + missing_guardian_fields  when the player is a minor and
        guardian info is absent — call submit_enrollment again with those fields
        added to field_overrides.
      success=true also includes source_document_upload and
      medical_exam_upload with {doc_type, status, error}.
    """
    from sav_parsers import close_processing

    form = _forms.get(mod1_id)
    if form is None:
        raise ValueError(f"Unknown mod1_id: {mod1_id!r}")
    if form.get("doc_type") != DocType.FPB_MOD1:
        raise ValueError(f"Artifact {mod1_id!r} is not an fpb_modelo_1 enrollment form")

    result = form.get("reconcile_result")
    if result is None:
        raise ValueError("Call preview_enrollment before submit_enrollment")

    client = _get_client()
    medical_exam: dict[str, Any] | None = None
    medical_exam_info = None
    if medical_exam_id is not None:
        medical_exam = _forms.get(medical_exam_id)
        if medical_exam is None:
            raise ValueError(f"Unknown medical_exam_id: {medical_exam_id!r}")
        if medical_exam.get("doc_type") != DocType.EM:
            raise ValueError(f"Artifact {medical_exam_id!r} is not an exame_medico parse")
        medical_exam_info = extract_medical_exam_info(medical_exam["parsed"])

    kwargs = dict(result.kwargs)
    kwargs.pop("license", None)
    if medical_exam_info and medical_exam_info.exam_date:
        kwargs["exam_date"] = medical_exam_info.exam_date
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

    try:
        player_id = client.add_player_to_registration_batch(batch_id, license, **kwargs)
    except SavConfigError as exc:
        return {
            "success": False,
            "missing_guardian_fields": parse_missing_guardian_fields(exc),
        }

    # Auto-upload the source PDF as fpb_modelo_1 (parity with `sav enroll`).
    # Non-fatal: enrollment is already committed, so we just record the
    # outcome on the response and let the caller retry via
    # upload_player_document if it fails.
    upload_status = _replace_player_document_from_bytes(
        client,
        batch_id,
        license,
        form.get("pdf_bytes"),
        doc_type=form["doc_type"],
    )
    medical_exam_upload = (
        _replace_player_document_from_bytes(
            client,
            batch_id,
            license,
            medical_exam.get("pdf_bytes"),
            doc_type=medical_exam["doc_type"],
        )
        if medical_exam is not None
        else None
    )

    # Only send corrections the user explicitly answered (needs_review).
    # Updated/kept were silent paths — staging them risks dataset noise.
    # retrain_corrections are SAV-side truths for read-only fields (nif,
    # data_nascimento) — always merged so the labeled doc anchors to them.
    corrections: dict[str, str] = {}
    for kwarg in result.needs_review:
        entity = KWARG_TO_ENTITY.get(kwarg)
        val = kwargs.get(kwarg)
        if entity and val is not None:
            corrections[entity] = str(val)
    corrections.update(result.retrain_corrections)
    try:
        close_processing(form["processing_id"], corrections=corrections or None)
    except Exception:
        pass
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
            pass

    sav_profile = form.get("sav_profile", {})
    return {
        "success": True,
        "player_id": player_id,
        "license": license,
        "name": sav_profile.get("nome", ""),
        "source_document_upload": upload_status,
        "medical_exam_upload": medical_exam_upload,
    }


@server.tool()
def update_enrollment(
    batch_id: int,
    license: int,
    fields: dict[str, Any],
) -> dict:
    """
    Patch personal-data and/or address fields on an already-enrolled player.

    The player must be in batch_id (open Revalidação). Only the keys present
    in `fields` are changed; everything else is preserved from the existing
    inscricao. No document is touched — pair with `replace_player_document`
    if you also want to swap the PDF.

    Supported keys (any subset, ints where applicable):
      Step 1 (personal): id_type (int), id_number, id_expiry, telemovel,
        telefone, email, nome_pai, nome_mae.
      Step 2 (address): morada, cod_postal, localidade_txt,
        distrito_id (int), concelho_id (int).

    Guardian/taxa/exam/consent fields are commit-time only on creation and
    are not (yet) patchable on existing enrolments — pass them via
    submit_enrollment when adding a new player.

    Returns: {"success": True, "player_id": int} on success.
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

    player_id = _get_client().update_player_in_registration_batch(
        batch_id, license, **coerced,
    )
    return {"success": True, "player_id": player_id}


# ── Registration documents ────────────────────────────────────────────────────

@server.tool()
def list_player_documents(batch_id: int, license: int) -> list[dict]:
    """
    List documents currently uploaded for a player in a batch.

    Each entry: {"doc_id": int, "doc_type": str | null}. doc_id is the
    galeria id expected by delete_player_document. SAV2-only document types
    with no sav-parsers equivalent are returned with doc_type=null.
    """
    docs = _get_client().list_player_registration_documents(batch_id, license)
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


def _decode_pdf_to_tempfile(pdf_base64: str, suffix: str = ".pdf") -> str:
    """Decode a base64-encoded payload into a temp file; caller must unlink."""
    pdf_bytes = base64.b64decode(pdf_base64)
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        f.write(pdf_bytes)
        return f.name


def _resolve_document_upload_type(tmp_path: str, doc_type: str | None) -> str:
    """Return explicit doc_type or classify tmp_path when omitted."""
    if doc_type is not None:
        return doc_type
    from sav_parsers import classify
    return classify(tmp_path).value


@server.tool()
def upload_player_document(
    batch_id: int,
    license: int,
    pdf_base64: str,
    doc_type: str | None = None,
) -> dict:
    """
    Upload a document (PDF, base64-encoded) attached to a player's registration.

    doc_type: one of exame_medico, fpb_modelo_1, fpb_modelo_4, outros.
    When omitted, sav-parsers classifies the PDF first.
    Recognized but unmapped types such as outros fail before the SAV2 call.

    Returns {"success": True} on success.
    """
    tmp_path = _decode_pdf_to_tempfile(pdf_base64)
    try:
        resolved_doc_type = _resolve_document_upload_type(tmp_path, doc_type)
        tipo_doc = doc_type_to_tipo_doc(resolved_doc_type)
        _get_client().upload_player_registration_document(
            batch_id, license, tmp_path, tipo_doc=tipo_doc,
        )
        return {"success": True}
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


@server.tool()
def delete_player_document(doc_id: int) -> dict:
    """
    Delete a previously uploaded document by its galeria id (from list_player_documents).
    """
    _get_client().delete_player_registration_document(doc_id)
    return {"success": True}


@server.tool()
def replace_player_document(
    batch_id: int,
    license: int,
    pdf_base64: str,
    doc_type: str | None = None,
) -> dict:
    """
    Replace any existing documents of `doc_type` for this player+batch with a
    new PDF (base64-encoded). Idempotent on the upload side: when no existing
    doc of the translated SAV2 tipo_doc is found, behaves like a plain upload.
    """
    tmp_path = _decode_pdf_to_tempfile(pdf_base64)
    try:
        resolved_doc_type = _resolve_document_upload_type(tmp_path, doc_type)
        tipo_doc = doc_type_to_tipo_doc(resolved_doc_type)
        _get_client().replace_player_registration_document(
            batch_id, license, tmp_path, tipo_doc=tipo_doc,
        )
        return {"success": True}
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def main() -> None:
    server.run()
