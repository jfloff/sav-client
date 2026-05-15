"""
SQLite-backed cache for slow/static SAV2 data (clubs, associations,
concelhos, tiers).

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
    con.execute("""
      CREATE TABLE IF NOT EXISTS concelhos (
        distrito_id INTEGER NOT NULL,
        concelho_id INTEGER NOT NULL,
        name        TEXT    NOT NULL,
        cached_at   REAL    NOT NULL,
        PRIMARY KEY (distrito_id, concelho_id)
      )
    """)
    con.execute("""
      CREATE TABLE IF NOT EXISTS tiers (
        gender_id INTEGER NOT NULL,
        tier_id   INTEGER NOT NULL,
        name      TEXT    NOT NULL,
        cached_at REAL    NOT NULL,
        PRIMARY KEY (gender_id, tier_id)
      )
    """)
    # license → internal player id is essentially immutable (FPB licences
    # don't get reassigned, internal ids are stable), so no cached_at column
    # and no TTL — a stale row would surface as a 404 from op=2 anyway.
    con.execute("""
      CREATE TABLE IF NOT EXISTS license_to_id (
        license   INTEGER PRIMARY KEY,
        player_id INTEGER NOT NULL
      )
    """)
    # license ↔ NIF is also stable (NIF is the player's tax id, set once at
    # registration). Same rationale: no TTL — invalidate() wipes it if a
    # NIF correction ever surfaces a stale row.
    con.execute("""
      CREATE TABLE IF NOT EXISTS license_nif (
        license INTEGER PRIMARY KEY,
        nif     TEXT    NOT NULL
      )
    """)
    con.execute(
      "CREATE INDEX IF NOT EXISTS idx_license_nif_nif ON license_nif(nif)"
    )
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

      if rows and (now - min(r[4] for r in rows)) < ttl:
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

      if rows and (now - min(r[2] for r in rows)) < ttl:
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

  def get_concelhos(
    self,
    fetcher: Callable[[int], dict[int, str]],
    distrito_id: int,
    ttl: int = _DEFAULT_TTL,
  ) -> dict[int, str]:
    """Return {concelho_id → name} for a distrito, refreshing on miss/expiry."""
    now = time.time()
    con = self._db()
    try:
      rows = con.execute(
        "SELECT concelho_id, name, cached_at FROM concelhos WHERE distrito_id = ? ORDER BY name",
        (distrito_id,),
      ).fetchall()

      if rows and (now - min(r[2] for r in rows)) < ttl:
        return {r[0]: r[1] for r in rows}

      concelhos = fetcher(distrito_id)
      con.execute("DELETE FROM concelhos WHERE distrito_id = ?", (distrito_id,))
      con.executemany(
        "INSERT INTO concelhos (distrito_id, concelho_id, name, cached_at) VALUES (?, ?, ?, ?)",
        [(distrito_id, cid, name, now) for cid, name in concelhos.items()],
      )
      con.commit()
      return concelhos
    finally:
      con.close()

  def get_tiers(
    self,
    fetcher: Callable[[int], dict[int, str]],
    gender_id: int,
    ttl: int = _DEFAULT_TTL,
  ) -> dict[int, str]:
    """Return {tier_id → name} for a gender, refreshing on miss/expiry."""
    now = time.time()
    con = self._db()
    try:
      rows = con.execute(
        "SELECT tier_id, name, cached_at FROM tiers WHERE gender_id = ? ORDER BY name",
        (gender_id,),
      ).fetchall()

      if rows and (now - min(r[2] for r in rows)) < ttl:
        return {r[0]: r[1] for r in rows}

      tiers = fetcher(gender_id)
      con.execute("DELETE FROM tiers WHERE gender_id = ?", (gender_id,))
      con.executemany(
        "INSERT INTO tiers (gender_id, tier_id, name, cached_at) VALUES (?, ?, ?, ?)",
        [(gender_id, tid, name, now) for tid, name in tiers.items()],
      )
      con.commit()
      return tiers
    finally:
      con.close()

  def get_player_id(self, license: int) -> int | None:
    """Return the internal player id for a licence, or None if unknown."""
    con = self._db()
    try:
      row = con.execute(
        "SELECT player_id FROM license_to_id WHERE license = ?",
        (license,),
      ).fetchone()
      return row[0] if row else None
    finally:
      con.close()

  def record_player_ids(self, pairs: list[tuple[int, int]]) -> None:
    """Bulk-upsert (license, player_id) pairs. No-op when pairs is empty."""
    if not pairs:
      return
    con = self._db()
    try:
      con.executemany(
        "INSERT OR REPLACE INTO license_to_id (license, player_id) VALUES (?, ?)",
        pairs,
      )
      con.commit()
    finally:
      con.close()

  def get_license_by_nif(self, nif: str) -> int | None:
    """Return the licence whose NIF matches, or None if unknown."""
    con = self._db()
    try:
      row = con.execute(
        "SELECT license FROM license_nif WHERE nif = ? LIMIT 1",
        (nif,),
      ).fetchone()
      return row[0] if row else None
    finally:
      con.close()

  def record_player_nifs(self, pairs: list[tuple[int, str]]) -> None:
    """Bulk-upsert (license, nif) pairs. No-op when pairs is empty."""
    if not pairs:
      return
    con = self._db()
    try:
      con.executemany(
        "INSERT OR REPLACE INTO license_nif (license, nif) VALUES (?, ?)",
        pairs,
      )
      con.commit()
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
      con.execute("DELETE FROM concelhos")
      con.execute("DELETE FROM tiers")
      con.execute("DELETE FROM license_to_id")
      con.execute("DELETE FROM license_nif")
      con.commit()
    finally:
      con.close()

