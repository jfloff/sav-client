"""
Simple SQLite cache for slow/static API data (clubs, associations).

Cache file: ~/.sav/cache.db
Default TTL: 86400 s (24 h)
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
  from sav_client import SavClient
  from sav_client.models import Club

_CACHE_DIR = Path.home() / ".sav"
_CACHE_FILE = _CACHE_DIR / "cache.db"
_DEFAULT_TTL = 7 * 86_400  # 7 days


def _db() -> sqlite3.Connection:
  _CACHE_DIR.mkdir(parents=True, exist_ok=True)
  con = sqlite3.connect(_CACHE_FILE)
  con.execute("""
    CREATE TABLE IF NOT EXISTS clubs (
      association_id  INTEGER NOT NULL,
      club_id         INTEGER NOT NULL,
      club_name       TEXT    NOT NULL,
      full_name       TEXT    NOT NULL DEFAULT '',
      code            TEXT    NOT NULL DEFAULT '',
      cached_at       REAL    NOT NULL,
      PRIMARY KEY (association_id, club_id)
    )
  """)
  # Migrate existing tables that predate the full_name / code columns
  for col, typedef in [
    ("full_name", "TEXT NOT NULL DEFAULT ''"),
    ("code",      "TEXT NOT NULL DEFAULT ''"),
  ]:
    try:
      con.execute(f"ALTER TABLE clubs ADD COLUMN {col} {typedef}")
    except sqlite3.OperationalError:
      pass  # Column already exists
  con.commit()
  return con


def get_clubs(
  client: "SavClient",
  association: int | None = None,
  ttl: int = _DEFAULT_TTL,
) -> list["Club"]:
  """
  Return clubs for `association`, using a cached result when fresh.

  Args:
      client:      Authenticated SavClient used to fetch on cache miss.
      association: Numeric association ID, or None for the session default.
      ttl:         Cache lifetime in seconds (default 24 h).
  """
  from sav_client.models import Club

  assoc_key = association if association is not None else -1
  now = time.time()

  con = _db()
  try:
    rows = con.execute(
      "SELECT club_id, club_name, full_name, code, cached_at FROM clubs WHERE association_id = ?",
      (assoc_key,),
    ).fetchall()

    if rows and (now - rows[0][4]) < ttl:
      return [Club(id=r[0], name=r[1], full_name=r[2], code=r[3]) for r in rows]

    # Cache miss or stale — fetch from API and enrich with full name + code
    clubs = client.list_clubs(association=association)
    enriched: list[Club] = []
    for c in clubs:
      full_name, code = client._fetch_club_names(c.id)
      enriched.append(Club(id=c.id, name=c.name, full_name=full_name, code=code))

    con.execute("DELETE FROM clubs WHERE association_id = ?", (assoc_key,))
    con.executemany(
      "INSERT INTO clubs (association_id, club_id, club_name, full_name, code, cached_at) VALUES (?, ?, ?, ?, ?, ?)",
      [(assoc_key, c.id, c.name, c.full_name, c.code, now) for c in enriched],
    )
    con.commit()
    return enriched
  finally:
    con.close()


def invalidate_clubs(association: int | None = None) -> None:
  """Remove cached clubs for the given association (or all if association is None)."""
  if not _CACHE_FILE.exists():
    return
  con = _db()
  try:
    if association is None:
      con.execute("DELETE FROM clubs")
    else:
      con.execute("DELETE FROM clubs WHERE association_id = ?", (association,))
    con.commit()
  finally:
    con.close()
