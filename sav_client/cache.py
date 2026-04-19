"""
SQLite-backed cache for slow/static SAV2 data (clubs, associations).

Cache file: ~/.sav/cache.db
Default TTL: 7 days
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Callable, TYPE_CHECKING

if TYPE_CHECKING:
  from .models import Club

_CACHE_DIR = Path.home() / ".sav"
_DEFAULT_TTL = 7 * 86_400  # 7 days


class Cache:
  DEFAULT_TTL = _DEFAULT_TTL

  def __init__(self) -> None:
    self.path = _CACHE_DIR / "cache.db"

  def _db(self) -> sqlite3.Connection:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(self.path)
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
    con.execute("""
      CREATE TABLE IF NOT EXISTS associations (
        id        INTEGER PRIMARY KEY,
        name      TEXT NOT NULL,
        cached_at REAL NOT NULL
      )
    """)
    for col, typedef in [
      ("full_name", "TEXT NOT NULL DEFAULT ''"),
      ("code",      "TEXT NOT NULL DEFAULT ''"),
    ]:
      try:
        con.execute(f"ALTER TABLE clubs ADD COLUMN {col} {typedef}")
      except sqlite3.OperationalError:
        pass
    con.commit()
    return con

  def get_clubs(
    self,
    fetcher: Callable[[int | None], list["Club"]],
    association: int | None = None,
    ttl: int = _DEFAULT_TTL,
  ) -> list["Club"]:
    """Return clubs, reading from cache when fresh or calling fetcher on miss."""
    from .models import Club

    assoc_key = association if association is not None else -1
    now = time.time()
    con = self._db()
    try:
      rows = con.execute(
        "SELECT club_id, club_name, full_name, code, cached_at FROM clubs WHERE association_id = ? ORDER BY club_name",
        (assoc_key,),
      ).fetchall()

      if rows and (now - rows[0][4]) < ttl:
        return [Club(id=r[0], name=r[1], full_name=r[2], code=r[3]) for r in rows]

      clubs = fetcher(association)
      con.execute("DELETE FROM clubs WHERE association_id = ?", (assoc_key,))
      con.executemany(
        "INSERT INTO clubs (association_id, club_id, club_name, full_name, code, cached_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [(assoc_key, c.id, c.name, c.full_name, c.code, now) for c in clubs],
      )
      con.commit()
      return clubs
    finally:
      con.close()

  def get_associations(
    self,
    fetcher: Callable[[], list["Club"]],
    ttl: int = _DEFAULT_TTL,
  ) -> list["Club"]:
    """Return associations, reading from cache when fresh or calling fetcher on miss."""
    from .models import Club

    now = time.time()
    con = self._db()
    try:
      rows = con.execute(
        "SELECT id, name, cached_at FROM associations ORDER BY name"
      ).fetchall()

      if rows and (now - rows[0][2]) < ttl:
        return [Club(id=r[0], name=r[1]) for r in rows]

      associations = fetcher()
      con.execute("DELETE FROM associations")
      con.executemany(
        "INSERT INTO associations (id, name, cached_at) VALUES (?, ?, ?)",
        [(a.id, a.name, now) for a in associations],
      )
      con.commit()
      return associations
    finally:
      con.close()

  def invalidate(self) -> None:
    """Clear all cached data."""
    if not self.path.exists():
      return
    con = self._db()
    try:
      con.execute("DELETE FROM clubs")
      con.execute("DELETE FROM associations")
      con.commit()
    finally:
      con.close()

