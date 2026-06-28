"""Shared model serializers for CLI and MCP outputs."""

from __future__ import annotations

from typing import Any

from .text import iso_date, normalise_text


def player_to_dict(p: Any, *, with_details: bool = False) -> dict:
  out = {
    "id": p.id, "license": p.license, "name": p.name,
    "club": p.club, "association": p.association,
    "tier": p.tier, "tier_id": p.tier_id,
    "gender": p.gender, "gender_id": p.gender_id,
    "birth_date": p.birth_date, "nationality": p.nationality,
    "status": p.status, "season": p.season, "active": p.active,
  }
  if with_details:
    out["photo_url"] = getattr(p, "photo_url", "")
    out["mobile_phone"] = getattr(p, "mobile_phone", "")
    out["nif"] = getattr(p, "nif", "")
  return out


def game_to_dict(g: Any) -> dict:
  return {
    "id": g.id, "number": g.number,
    "date": g.date, "time": g.time,
    "home": g.home, "away": g.away,
    "home_score": g.home_score, "away_score": g.away_score,
    "competition": g.competition, "tier": g.tier, "gender": g.gender,
    "venue": g.venue, "game_status": g.game_status,
  }


def _score_to_int(value: str) -> int | None:
  value = (value or "").strip()
  return int(value) if value.lstrip("-").isdigit() else None


def _game_starts_at(g: Any) -> str:
  """ISO ``YYYY-MM-DDTHH:MM`` (date only if no time, "" if unscheduled)."""
  date = (g.date or "").strip()
  if not date:
    return ""
  iso = iso_date(date)
  time = (g.time or "").strip()
  return f"{iso}T{time}" if time else iso


def club_game_to_dict(g: Any, *, club_name: str) -> dict:
  """Serialize a Game from the queried club's perspective.

  home / our_score / opp_score / opponent are relative to ``club_name`` (not the
  sheet's home/away). The club's side is found by matching its name against the
  team strings: SAV2 appends team suffixes (" - B", "/MVP", …), so a normalised
  containment match is used. When neither side matches (e.g. the name could not
  be resolved) the home side is treated as the club's — a stable degenerate
  fallback, since a club-scoped query always has the club on one side.
  """
  club_key = normalise_text(club_name)
  away_is_ours = bool(club_key) and club_key in normalise_text(g.away)
  home_is_ours = bool(club_key) and club_key in normalise_text(g.home)
  ours_home = home_is_ours or not away_is_ours

  home_score = _score_to_int(g.home_score)
  away_score = _score_to_int(g.away_score)
  played = home_score is not None and away_score is not None

  return {
    "source_id": str(g.id) if g.id else g.number,
    "escalao": g.level or g.tier,
    "gender": g.gender or None,
    "starts_at": _game_starts_at(g),
    "opponent": g.away if ours_home else g.home,
    "home": ours_home,
    "venue": g.venue or None,
    "status": "played" if played else "scheduled",
    "our_score": (home_score if ours_home else away_score),
    "opp_score": (away_score if ours_home else home_score),
  }


def club_to_dict(c: Any) -> dict:
  return {"id": c.id, "name": c.name, "full_name": c.full_name, "code": c.code}


def coach_to_dict(c: Any, *, with_details: bool = False) -> dict:
  out = {
    "id": c.id, "carreira_id": c.carreira_id,
    "wallet": c.wallet, "name": c.name,
    "club": c.club, "association": c.association,
    "gender": c.gender, "season": c.season,
    "grade": c.grade, "birth_date": c.birth_date,
    "active": c.active,
  }
  if with_details:
    out["nif"] = getattr(c, "nif", "")
    out["tptd"] = getattr(c, "tptd", "")
    out["tptd_expiry"] = getattr(c, "tptd_expiry", "")
    out["mobile_phone"] = getattr(c, "mobile_phone", "")
    out["email"] = getattr(c, "email", "")
  return out


def batch_to_dict(b: Any) -> dict:
  return {
    "number": b.number,
    "type": b.type, "tier": b.tier, "gender": b.gender,
    "state": b.state, "state_date": b.state_date,
    "item_count": b.item_count, "season": b.season,
    "is_open": b.is_open,
  }
