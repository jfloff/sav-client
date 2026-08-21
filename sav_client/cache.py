"""
SQLite-backed cache for slow/static SAV2 data (clubs, associations,
concelhos).

Cache file: ~/.sav/cache.db
Default TTL: 7 days
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Callable, TYPE_CHECKING

if TYPE_CHECKING:
  from .models import Club, Game

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
    # batch_number (numero_guia) ↔ batch_id (guia_id) is permanent: SAV2
    # assigns the number once and never reassigns it. Same rationale as
    # license_to_id — no TTL, invalidate() wipes it.
    con.execute("""
      CREATE TABLE IF NOT EXISTS batch_number_to_id (
        number   TEXT PRIMARY KEY,
        batch_id INTEGER NOT NULL
      )
    """)
    con.execute(
      "CREATE INDEX IF NOT EXISTS idx_batch_number_to_id_id ON batch_number_to_id(batch_id)"
    )
    # license → batch_id maps a player to the open batch they are currently
    # enrolled in. SAV enforces that a player belongs to at most one open
    # batch at a time, so the mapping is single-valued. Validated on read
    # via load_existing_registration_record — stale entries are wiped on miss.
    con.execute("""
      CREATE TABLE IF NOT EXISTS license_to_batch_id (
        license  INTEGER PRIMARY KEY,
        batch_id INTEGER NOT NULL
      )
    """)
    con.execute(
      "CREATE INDEX IF NOT EXISTS idx_license_to_batch_id_bid ON license_to_batch_id(batch_id)"
    )
    # Game listings, keyed by a signature of the list_games query (season +
    # filters). Unlike the tables above, game data is volatile (status, scores),
    # so this row carries a cached_at and is read under a deliberately short TTL
    # — just long enough to collapse the back-to-back listings a game-sheet
    # workflow issues within a minute or two. The payload is the JSON-serialised
    # list of Game rows.
    con.execute("""
      CREATE TABLE IF NOT EXISTS games (
        signature TEXT PRIMARY KEY,
        payload   TEXT NOT NULL,
        cached_at REAL NOT NULL
      )
    """)
    # A license↔NIF pair is immutable, but a club roster's completeness is not:
    # new enrolments can appear after a scan. Scope markers therefore carry a
    # TTL even though the license_nif rows themselves do not.
    con.execute("""
      CREATE TABLE IF NOT EXISTS nif_index (
        club_id      INTEGER NOT NULL,
        scope        TEXT    NOT NULL,
        built_at     REAL    NOT NULL,
        player_count INTEGER NOT NULL,
        PRIMARY KEY (club_id, scope)
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

  def get_games(
    self,
    fetcher: Callable[[], list["Game"]],
    signature: str,
    ttl: float,
  ) -> list["Game"]:
    """Return games for a query signature, reading from cache when fresh.

    ``signature`` uniquely identifies the list_games query (season + filters);
    ``ttl`` is intentionally short because game data is volatile. On a miss or
    expiry the fetcher runs and its result replaces the cached row.
    """
    import json

    from dataclasses import asdict

    from .models import Game

    now = time.time()
    con = self._db()
    try:
      row = con.execute(
        "SELECT payload, cached_at FROM games WHERE signature = ?",
        (signature,),
      ).fetchone()
      if row and (now - row[1]) < ttl:
        return [Game(**r) for r in json.loads(row[0])]

      games = fetcher()
      con.execute(
        "INSERT OR REPLACE INTO games (signature, payload, cached_at) VALUES (?, ?, ?)",
        (signature, json.dumps([asdict(g) for g in games]), now),
      )
      con.commit()
      return games
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

  def known_nif_licenses(self, licenses: list[int]) -> set[int]:
    """Return the licences that already have a cached NIF row."""
    if not licenses:
      return set()
    known: set[int] = set()
    con = self._db()
    try:
      for start in range(0, len(licenses), 999):
        chunk = licenses[start:start + 999]
        placeholders = ",".join("?" for _ in chunk)
        rows = con.execute(
          f"SELECT license FROM license_nif WHERE license IN ({placeholders})",
          chunk,
        ).fetchall()
        known.update(row[0] for row in rows)
      return known
    finally:
      con.close()

  def get_nif_index(
    self, club_id: int, scope: str, ttl: float,
  ) -> tuple[float, int] | None:
    """Return a fresh (built_at, player_count) marker, or None."""
    now = time.time()
    con = self._db()
    try:
      row = con.execute(
        "SELECT built_at, player_count FROM nif_index "
        "WHERE club_id = ? AND scope = ?",
        (club_id, scope),
      ).fetchone()
      if row and (now - row[0]) < ttl:
        return row[0], row[1]
      return None
    finally:
      con.close()

  def record_nif_index(
    self, club_id: int, scope: str, player_count: int,
  ) -> None:
    """Record that a club's NIF index scope was built successfully."""
    con = self._db()
    try:
      con.execute(
        "INSERT OR REPLACE INTO nif_index "
        "(club_id, scope, built_at, player_count) VALUES (?, ?, ?, ?)",
        (club_id, scope, time.time(), player_count),
      )
      con.commit()
    finally:
      con.close()

  def clear_nif_index(
    self, club_id: int | None = None, scope: str | None = None,
  ) -> None:
    """Clear one scope, every scope for a club, or every NIF index marker."""
    con = self._db()
    try:
      if club_id is None:
        con.execute("DELETE FROM nif_index")
      elif scope is None:
        con.execute("DELETE FROM nif_index WHERE club_id = ?", (club_id,))
      else:
        con.execute(
          "DELETE FROM nif_index WHERE club_id = ? AND scope = ?",
          (club_id, scope),
        )
      con.commit()
    finally:
      con.close()

  def get_batch_id(self, number: str) -> int | None:
    """Return the internal batch_id for a human-visible batch number, or None."""
    con = self._db()
    try:
      row = con.execute(
        "SELECT batch_id FROM batch_number_to_id WHERE number = ?",
        (number,),
      ).fetchone()
      return row[0] if row else None
    finally:
      con.close()

  def get_batch_number(self, batch_id: int) -> str | None:
    """Return the human-visible batch number for an internal batch_id, or None."""
    con = self._db()
    try:
      row = con.execute(
        "SELECT number FROM batch_number_to_id WHERE batch_id = ? LIMIT 1",
        (batch_id,),
      ).fetchone()
      return row[0] if row else None
    finally:
      con.close()

  def record_batches(self, pairs: list[tuple[str, int]]) -> None:
    """Bulk-upsert (number, batch_id) pairs. No-op when pairs is empty."""
    if not pairs:
      return
    con = self._db()
    try:
      con.executemany(
        "INSERT OR REPLACE INTO batch_number_to_id (number, batch_id) VALUES (?, ?)",
        pairs,
      )
      con.commit()
    finally:
      con.close()

  def get_batch_id_by_license(self, license: int) -> int | None:
    """Return the cached batch_id for a license's current enrollment, or None."""
    con = self._db()
    try:
      row = con.execute(
        "SELECT batch_id FROM license_to_batch_id WHERE license = ?",
        (license,),
      ).fetchone()
      return row[0] if row else None
    finally:
      con.close()

  def record_license_batch(self, license: int, batch_id: int) -> None:
    """Upsert a (license, batch_id) entry."""
    con = self._db()
    try:
      con.execute(
        "INSERT OR REPLACE INTO license_to_batch_id (license, batch_id) VALUES (?, ?)",
        (license, batch_id),
      )
      con.commit()
    finally:
      con.close()

  def forget_license_batch(self, license: int) -> None:
    """Remove the cached batch entry for a license."""
    con = self._db()
    try:
      con.execute("DELETE FROM license_to_batch_id WHERE license = ?", (license,))
      con.commit()
    finally:
      con.close()

  def forget_licenses_in_batch(self, batch_id: int) -> None:
    """Remove every cached license entry that points at this batch_id."""
    con = self._db()
    try:
      con.execute("DELETE FROM license_to_batch_id WHERE batch_id = ?", (batch_id,))
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
      con.execute("DELETE FROM games")
      con.execute("DELETE FROM nif_index")
      con.execute("DELETE FROM license_to_id")
      con.execute("DELETE FROM license_nif")
      con.execute("DELETE FROM batch_number_to_id")
      con.execute("DELETE FROM license_to_batch_id")
      con.commit()
    finally:
      con.close()
