"""Shared model serializers for CLI and MCP outputs."""

from __future__ import annotations

from typing import Any


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
    "number": b.number,
    "type": b.type, "tier": b.tier, "gender": b.gender,
    "state": b.state, "state_date": b.state_date,
    "item_count": b.item_count, "season": b.season,
    "is_open": b.is_open,
  }
