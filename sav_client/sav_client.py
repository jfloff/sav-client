"""
SavClient — automation client for the FPB SA2.0 (SAV2) web system.

Usage
-----
    from sav_client import SavClient

    client = SavClient.from_env()   # reads SAV_BASE_URL / SAV_USERNAME / SAV_PASSWORD
    client.login()
    # client.session is now populated and ready for subsequent calls

Configuration (.env keys)
--------------------------
    SAV_BASE_URL   — optional base URL (default: https://sav2.fpb.pt)
    SAV_USERNAME   — login username
    SAV_PASSWORD   — plaintext password (hashed with MD5 before sending)
    SAV_TIMEOUT    — optional request timeout in seconds (default: 30)
    SAV_LOG_LEVEL  — optional logging level for this module (default: WARNING)
"""

from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import urljoin

import requests
from dotenv import load_dotenv

from .exceptions import (
  SavAuthError,
  SavConfigError,
  SavConnectionError,
  SavResponseError,
)
from .cache import Cache
from .models import Player, Club, Game, LoginResult, Session
from .utils import md5_hex, strip_html

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Internal constants
# ---------------------------------------------------------------------------

_LOGIN_PATH = "php/logindb.php"
_LOGIN_OP = "1"
_ATHLETES_PATH = "php/jogadoresdb.php"
_ATHLETES_OP = "1"
_CLUBS_BY_ORG_PATH = "php/resultadosdb.php"
_CLUBS_BY_ORG_OP = "17"
_CLUBS_BY_ASSOC_PATH = "php/jogadoresdb.php"
_CLUBS_BY_ASSOC_OP = "25"
_GAMES_PATH = "php/jogosdb.php"
_GAMES_OP = "3"
_ATHLETE_DETAIL_PATH = "php/jogadoresdb.php"
_ATHLETE_DETAIL_OP = "2"
_CLUB_DETAIL_PATH = "php/clubesdb.php"
_CLUB_DETAIL_OP = "9"
_GAME_SHEET_PATH = "php/maindb.php"
_GAME_SHEET_OP = "29"
_DEFAULT_TIMEOUT = 30


class SavClient:
  """
  Automation client for the FPB SA2.0 player registration system.

  All interaction details (HTTP transport, credential hashing, session
  management) are encapsulated here.  External code depends only on the
  public methods and the models returned by them.

  Attributes:
      base_url:  Root URL of the SAV2 instance.
      session:   Populated after a successful `login()` call; None before.
  """

  def __init__(
    self,
    base_url: str,
    username: str,
    password: str,
    *,
    timeout: int = _DEFAULT_TIMEOUT,
  ) -> None:
    """
    Args:
        base_url:  Root URL of the SAV2 instance (trailing slash optional).
        username:  Login username.
        password:  Plaintext password — hashed with MD5 before transmission.
        timeout:   Network timeout in seconds applied to every request.
    """
    if not base_url:
      raise SavConfigError("base_url must not be empty")
    if not username:
      raise SavConfigError("username must not be empty")
    if not password:
      raise SavConfigError("password must not be empty")

    self.base_url = base_url.rstrip("/") + "/"
    self._username = username
    self._password = password
    self._timeout = timeout

    # Populated after login()
    self.session: Session | None = None

    self._cache = Cache()

    # Reuse a single requests.Session for connection pooling and automatic
    # cookie handling (the server may set cookies in addition to returning
    # the JSON session object).
    self._http = requests.Session()
    self._http.headers.update(
      {
        "Content-Type": "application/json",
        "Accept": "application/json",
        # Mimic the browser user-agent the system expects
        "User-Agent": (
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
          "AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/124.0.0.0 Safari/537.36"
        ),
      }
    )

    logger.debug("SavClient initialised for %s (user=%s)", self.base_url, username)

  # ------------------------------------------------------------------
  # Construction helpers
  # ------------------------------------------------------------------

  @classmethod
  def from_env(cls, env_file: str | None = ".env") -> "SavClient":
    """
    Build a SavClient from environment variables (and an optional .env file).

    Expected keys:
        SAV_BASE_URL   — optional, defaults to https://sav2.fpb.pt
        SAV_USERNAME   — required
        SAV_PASSWORD   — required
        SAV_TIMEOUT    — optional, integer seconds (default 30)
        SAV_LOG_LEVEL  — optional, e.g. DEBUG / INFO / WARNING

    Args:
        env_file: Path to the .env file to load.  Pass None to skip file
                  loading and rely solely on the process environment.

    Raises:
        SavConfigError: If any required variable is absent.
    """
    if env_file:
      load_dotenv(env_file, override=False)

    log_level = os.getenv("SAV_LOG_LEVEL", "").upper()
    if log_level:
      logging.getLogger(__name__).setLevel(log_level)

    base_url = os.getenv("SAV_BASE_URL", "https://sav2.fpb.pt")
    username = os.getenv("SAV_USERNAME", "")
    password = os.getenv("SAV_PASSWORD", "")
    timeout_raw = os.getenv("SAV_TIMEOUT", str(_DEFAULT_TIMEOUT))

    missing = [k for k, v in {
      "SAV_USERNAME": username,
      "SAV_PASSWORD": password,
    }.items() if not v]

    if missing:
      raise SavConfigError(
        f"Missing required environment variable(s): {', '.join(missing)}"
      )

    try:
      timeout = int(timeout_raw)
    except ValueError:
      raise SavConfigError(
        f"SAV_TIMEOUT must be an integer, got: {timeout_raw!r}"
      )

    return cls(base_url, username, password, timeout=timeout)

  # ------------------------------------------------------------------
  # Public API
  # ------------------------------------------------------------------

  def login(self) -> LoginResult:
    """
    Authenticate with the SAV2 system.

    Sends the username and MD5-hashed password to the login endpoint and
    stores the resulting session for use by subsequent calls.

    Returns:
        LoginResult with success=True and a populated session on success.

    Raises:
        SavAuthError:      The server rejected the credentials.
        SavConnectionError: A network error prevented the request.
        SavResponseError:  The server returned an unparseable response.
    """
    payload: dict[str, Any] = {
      "user": self._username,
      "pass": md5_hex(self._password),
    }

    logger.info("Attempting login for user %r", self._username)
    raw = self._post(_LOGIN_PATH, payload, params={"op": _LOGIN_OP})

    return self._parse_login_response(raw)

  def search_players(
    self,
    *,
    name: str = "",
    license: str = "",
    number: str = "",
    status: str = "",
    gender: int = 0,
    tier: str | list[str] = "",
    season: int | None = None,
    association: int = 0,
    club: int | list[int] | None = None,
    birth_date: str = "",
    page: int = 1,
    limit: int | None = None,
  ) -> list[Player]:
    """
    Search for players in the SAV2 system.

    When ``club`` is omitted the logged-in club is used.  Pass ``club=0``
    to search across all clubs (scoped by ``association`` when non-zero, or
    federation-wide).  Pass a list of IDs to search specific clubs in
    parallel and return a merged, deduplicated result.

    Pass a list of strings to ``tier`` to search multiple tiers in parallel
    and return a merged, deduplicated result.

    Args:
        name:        Filter by player name (partial match).
        license:     Filter by licence number (exact).  When set, all other
                     filters are ignored by the server.
        number:      Filter by shirt number.
        status:      Filter by eligibility status: ``"active"``,
                     ``"inactive"``, or ``"all"``. Applied client-side using
                     ``Player.active`` because SAV2's player search request
                     parameter is not yet documented in this client.
        gender:      0 = any, or the server's numeric gender code.
        tier:        Filter by age/competition tier (escalão), e.g. "Sénior".
                     Pass a list to search multiple tiers in parallel.
        season:      SAV2 epoch ID.  Defaults to the current session epoch.
                     Pass ``0`` to return players across all seasons.
        association: SAV2 association ID.  Defaults to 0 (any/auto).
        club:        SAV2 club ID, list of IDs, or None (own club).
                     Pass ``0`` to search all clubs.
        birth_date:  Filter by birth date string (YYYY-MM-DD).
        page:        Result page number (1-based).  Ignored when multiple
                     clubs are searched.
        limit:       Stop aggregating once this many unique players have been
                     collected.  Only affects multi-club / multi-tier parallel
                     searches — cancels remaining in-flight requests to short
                     circuit wide scans (e.g. ``--all-clubs``).

    Returns:
        List of Player objects parsed from the HTML response.

    Raises:
        SavResponseError:   If the response cannot be parsed.
        SavConnectionError: On network errors.
    """
    if self.session is None:
      raise SavResponseError("Must call login() before search_players()")

    status_filter = self._parse_player_status_filter(status)
    parallel_limit = None if status_filter is not None else limit

    if isinstance(tier, list):
      results = self._search_tier_list(
        tier, name=name, license=license, number=number, gender=gender,
        season=season, association=association, club=club,
        birth_date=birth_date, status=status, limit=parallel_limit,
      )
      results = self._filter_players_status(results, status_filter)
      return results[:limit] if limit is not None else results

    if season is None:
      season = int(self.session.get("epoca_id") or 0)
    elif season == 0:
      pass  # 0 means "all seasons" — send as-is

    filters = dict(
      name=name, license=license, number=number, gender=gender,
      tier=tier, season=season, birth_date=birth_date,
    )

    if isinstance(club, list):
      results = self._search_club_list(club, limit=parallel_limit, **filters)
      results = self._filter_players_status(results, status_filter)
      return results[:limit] if limit is not None else results

    if club is None:
      club = int(self.session.get("organizacao") or 0)

    if club == 0:
      results = self._search_all_clubs(association=association, limit=parallel_limit, **filters)
      results = self._filter_players_status(results, status_filter)
      return results[:limit] if limit is not None else results

    results = self._search_players_single(
      association=association, club=club, page=page, **filters,
    )
    results = self._filter_players_status(results, status_filter)
    results = sorted(results, key=lambda p: p.id)
    return results[:limit] if limit is not None else results

  @staticmethod
  def _parse_player_status_filter(status: str) -> bool | None:
    """Normalise a player status filter to active/inactive/all."""
    wanted = status.strip().lower()
    if not wanted or wanted == "all":
      return None
    if wanted == "active":
      return True
    if wanted == "inactive":
      return False
    raise ValueError("status must be 'active', 'inactive', or 'all'")

  @staticmethod
  def _filter_players_status(players: list[Player], status_filter: bool | None) -> list[Player]:
    """Filter players by their parsed active/inactive eligibility flag."""
    if status_filter is None:
      return sorted(players, key=lambda p: p.id)
    return sorted(
      [p for p in players if p.active is status_filter],
      key=lambda p: p.id,
    )

  def _search_tier_list(
    self, tiers: list[str], *, max_workers: int = 8,
    limit: int | None = None, **kwargs,
  ) -> list[Player]:
    """Search multiple tiers in parallel and return deduplicated results."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    seen: dict[int, Player] = {}
    with ThreadPoolExecutor(max_workers=min(max_workers, len(tiers))) as pool:
      futures = {pool.submit(self.search_players, tier=t, **kwargs): t for t in tiers}
      for future in as_completed(futures):
        try:
          for p in future.result():
            if p.id not in seen:
              seen[p.id] = p
        except Exception:
          logger.debug("Skipping tier=%r", futures[future], exc_info=True)
        if limit is not None and len(seen) >= limit:
          for f in futures:
            f.cancel()
          break
    ordered = sorted(seen.values(), key=lambda p: p.id)
    return ordered[:limit] if limit is not None else ordered

  def _search_club_list(
    self, club_ids: list[int], *, max_workers: int = 8,
    limit: int | None = None, **filters,
  ) -> list[Player]:
    """Search a specific list of clubs in parallel and return deduplicated results."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _fetch(club_id: int) -> list[Player]:
      return self._search_players_single(club=club_id, page=1, **filters)

    seen: dict[int, Player] = {}
    with ThreadPoolExecutor(max_workers=min(max_workers, len(club_ids))) as pool:
      futures = {pool.submit(_fetch, cid): cid for cid in club_ids}
      for future in as_completed(futures):
        try:
          for p in future.result():
            if p.id not in seen:
              seen[p.id] = p
        except Exception:
          logger.debug("Skipping club id=%s", futures[future], exc_info=True)
        if limit is not None and len(seen) >= limit:
          for f in futures:
            f.cancel()
          break
    ordered = sorted(seen.values(), key=lambda p: p.id)
    return ordered[:limit] if limit is not None else ordered

  def _search_all_clubs(
    self,
    *,
    association: int = 0,
    max_workers: int = 8,
    limit: int | None = None,
    **filters,
  ) -> list[Player]:
    """
    Search every club in parallel and aggregate results, deduplicating by player id.

    When ``association`` is non-zero, only clubs from that association are
    searched.  Otherwise every club across every association is searched.
    Up to ``max_workers`` clubs are queried concurrently.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    clubs_by_id: dict[int, Any] = {}

    if association != 0:
      for c in self.list_clubs(association=association):
        clubs_by_id[c.id] = c
    else:
      try:
        for assoc in self.list_associations():
          try:
            for c in self.list_clubs(association=assoc.id):
              clubs_by_id[c.id] = c
          except Exception:
            logger.debug("Skipping association id=%s", assoc.id, exc_info=True)
      except Exception:
        logger.debug("Could not fetch associations; falling back to own", exc_info=True)

      if not clubs_by_id:
        for c in self.list_clubs():
          clubs_by_id[c.id] = c

    def _fetch(club_id: int) -> list[Player]:
      return self._search_players_single(club=club_id, page=1, **filters)

    seen: dict[int, Player] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
      futures = {pool.submit(_fetch, c.id): c.id for c in clubs_by_id.values()}
      for future in as_completed(futures):
        club_id = futures[future]
        try:
          for p in future.result():
            if p.id not in seen:
              seen[p.id] = p
        except Exception:
          logger.debug("Skipping club id=%s during all-clubs search", club_id, exc_info=True)
        if limit is not None and len(seen) >= limit:
          for f in futures:
            f.cancel()
          break
    ordered = sorted(seen.values(), key=lambda p: p.id)
    return ordered[:limit] if limit is not None else ordered

  def _search_players_single(
    self,
    *,
    name: str = "",
    license: str = "",
    number: str = "",
    gender: int = 0,
    tier: str = "",
    season: int = 0,
    association: int = 0,
    club: int,
    birth_date: str = "",
    page: int = 1,
  ) -> list[Player]:
    """Issue a single search request for one club and return results."""
    payload = {
      "jc_findByLicense": license,
      "jc_findByName": name,
      "jc_findByNumber": number,
      "jc_sexo": gender,
      "jc_escalao": tier,
      "jc_associacao": association,
      "jc_epoca": season,
      "perfil": self.session.get("perfil", 0),
      "user": self.session.get("user", ""),
      "organizacao": self.session.get("organizacao", 0),
      "numpag": page,
      "nr_dtnasc": birth_date,
      "nr_clube": club,
    }

    logger.info("Searching players club=%s filters: %s", club, payload)
    html = self._post_form(_ATHLETES_PATH, payload, params={"op": _ATHLETES_OP})
    return self._parse_players_response(html)

  def get_player_detail(self, player_id: int, *, photo: bool = False) -> Player:
    """
    Fetch the detail page for a single athlete to obtain their photo URL.

    Because the search endpoint does not return a photo, this method makes
    an additional request to the detail page.  Pass ``photo=True`` to
    populate ``Player.photo_url``; with the default ``photo=False`` the
    method is a no-op and returns a minimal Player with only ``id`` set
    (prefer using search_players for full data).

    Args:
        player_id: The internal SAV2 database ID (from Player.id).
        photo:      When True, fetch the detail page and parse the photo URL.

    Raises:
        SavResponseError:   If the response cannot be parsed.
        SavConnectionError: On network errors.
    """
    if self.session is None:
      raise SavResponseError("Must call login() before get_player_detail()")

    if not photo:
      return Player(
        id=player_id, license="", name="", association="", club="",
        tier="", gender="", birth_date="", nationality="", status="",
      )

    payload = {
      "user_id": player_id,
      "user": self.session.get("user", ""),
      "perfil": self.session.get("perfil", 0),
      "organizacao": self.session.get("organizacao", 0),
    }

    logger.info("Fetching photo for athlete id=%s", player_id)
    text = self._post_form(_ATHLETE_DETAIL_PATH, payload, params={"op": _ATHLETE_DETAIL_OP})
    try:
      import json as _json
      raw = _json.loads(text)
    except ValueError as exc:
      raise SavResponseError(
        f"Player detail response was not valid JSON: {text[:200]!r}"
      ) from exc
    return self._parse_player_detail_response(raw, player_id=player_id)

  def list_games(
    self,
    *,
    season: int | None = None,
    association: str | int = 0,
    competition: int = 0,
    phase: int = 0,
    round_: int = 0,
    date_from: str = "",
    date_to: str = "",
    game_status: int = 0,
    result_status: int = 0,
    gender: int = 0,
    tier: str = "",
    venue: int = 0,
    game_number: str = "",
  ) -> list[Game]:
    """
    Search for games involving the profile's club.

    All parameters are optional — bare ``client.list_games()`` returns all
    games for the current season.

    Args:
        season:        Season epoch ID. Defaults to the session's current epoch.
        association:   Association filter, e.g. ``"ass,7"`` or numeric ID.
        competition:   SAV2 competition/prova ID.
        phase:         Phase ID within the competition.
        round_:        Round/jornada number.
        date_from:     Start date filter (DD-MM-YYYY).
        date_to:       End date filter (DD-MM-YYYY).
        game_status:   Game status code (0 = any).
        result_status: Result status code (0 = any).
        gender:        Gender code (0 = any).
        tier:          Tier/escalão text (empty = any).
        venue:         Venue ID (0 = any).
        game_number:   Specific game number string.

    Returns:
        List of Game objects.

    Raises:
        SavResponseError:   If the response cannot be parsed.
        SavConnectionError: On network errors.
    """
    if self.session is None:
      raise SavResponseError("Must call login() before list_games()")

    if season is None:
      season = int(self.session.get("epoca_id") or 0)

    payload = {
      "user": self.session.get("user", ""),
      "organizacao": self.session.get("organizacao", 0),
      "perfil": self.session.get("perfil", 0),
      "associacao": association,
      "prova": competition,
      "numJogo": game_number,
      "fase": phase,
      "jornada": round_,
      "inicio": date_from,
      "fim": date_to,
      "epoca": season,
      "estadojogo": game_status,
      "estadoresult": result_status,
      "genero": gender,
      "escalao": tier,
      "recinto": venue,
    }

    logger.info("Searching games with filters: %s", payload)
    text = self._post_form(_GAMES_PATH, payload, params={"op": _GAMES_OP})
    try:
      import json as _json
      raw = _json.loads(text)
    except ValueError as exc:
      raise SavResponseError(
        f"Games response was not valid JSON: {text[:200]!r}"
      ) from exc
    return self._parse_games_response(raw)

  def get_game_sheet_pdf(self, game_id: int) -> bytes | None:
    """
    Download the uploaded game-sheet PDF for a game.

    Uses ``maindb.php?op=29`` to retrieve the PDF URL, then downloads
    the file using the authenticated session.

    Args:
        game_id: Internal SAV2 game ID (``Game.id`` from ``list_games``).

    Returns:
        PDF bytes, or None if no game sheet has been uploaded yet.

    Raises:
        SavConnectionError: On network errors.
        SavResponseError:   If the response cannot be parsed.
    """
    if self.session is None:
      raise SavResponseError("Must call login() before get_game_sheet_pdf()")

    try:
      resp = self._http.get(
        self._url(f"{_GAME_SHEET_PATH}?op={_GAME_SHEET_OP}&jogo={game_id}"),
        timeout=self._timeout,
      )
      resp.raise_for_status()
    except Exception as exc:
      raise SavConnectionError(f"Could not fetch game sheet info: {exc}") from exc

    try:
      import json as _json
      raw = _json.loads(resp.text)
    except ValueError as exc:
      raise SavResponseError(
        f"Game sheet response was not valid JSON: {resp.text[:200]!r}"
      ) from exc

    from bs4 import BeautifulSoup
    soup = BeautifulSoup(raw.get("body", ""), "html.parser")
    link = soup.find("a", href=True)
    if link is None:
      return None

    pdf_url = self._url(link["href"])
    logger.info("Downloading game sheet PDF from %s", pdf_url)
    try:
      pdf_resp = self._http.get(pdf_url, timeout=self._timeout)
      pdf_resp.raise_for_status()
    except Exception as exc:
      raise SavConnectionError(f"Could not download game sheet PDF: {exc}") from exc

    return pdf_resp.content

  def get_eligible_players(self, game_id: int, *, val: int = 1) -> dict:
    """
    Return the eligible players and staff for one team in a game.

    Fetches ``maindb.php?op=16`` and parses the eight tables into a dict with
    keys ``game_number``, ``players``, ``coaches_pri``, ``coaches_adj``,
    ``coaches_other``, and ``staff``.  Each entry is a list of dicts with
    whatever columns the server returns.

    Args:
        game_id: Internal SAV2 game ID (``Game.id`` from ``list_games``).
        val:     1 = home team, 2 = away team.

    Returns:
        Dict with parsed eligible players data, or empty lists if none found.
    """
    if self.session is None:
      raise SavResponseError("Must call login() before get_eligible_players()")

    from bs4 import BeautifulSoup

    try:
      resp = self._http.get(
        self._url(f"php/maindb.php?op=16&jogo={game_id}"),
        timeout=self._timeout,
      )
      resp.raise_for_status()
    except Exception as exc:
      raise SavConnectionError(f"Could not fetch eligible players page: {exc}") from exc

    try:
      import json as _json
      raw = _json.loads(resp.text)
    except ValueError as exc:
      raise SavResponseError(
        f"Eligible players response was not valid JSON: {resp.text[:200]!r}"
      ) from exc

    soup = BeautifulSoup(raw.get("msg", ""), "html.parser")
    tables = soup.find_all("table")

    # Tables 0-3 = home; 4-7 = away
    offset = 0 if val == 1 else 4

    def _parse_rows(table_idx: int, keys: list[str]) -> list[dict]:
      if table_idx >= len(tables):
        return []
      rows = tables[table_idx].find_all("tr")[1:]  # skip header
      result = []
      for row in rows:
        cells = [td.get_text(strip=True) for td in row.find_all("td")]
        if not any(cells):
          continue
        entry = dict(zip(keys, cells))
        result.append({k: v for k, v in entry.items() if k and v})
      return result

    return {
      "game_number": raw.get("numero_jogo_sa_old", ""),
      "players":       _parse_rows(offset + 0, ["_sel", "_icon", "licence", "name", "birth_date", "status", "_act"]),
      "coaches_pri":   _parse_rows(offset + 1, ["_pri", "_adj", "wallet", "name", "birth_date", "tptd", "grade", "function"]),
      "coaches_adj":   _parse_rows(offset + 2, ["_sel", "wallet", "name", "birth_date", "tptd", "grade", "function"]),
      "staff":         _parse_rows(offset + 3, ["_sel", "licence", "name", "function"]),
    }

  def get_eligible_players_pdf(
    self,
    game_id: int,
    *,
    val: int = 1,
    player_licences: list[int] | None = None,
    coaches_pri: list[int] | None = None,
    coaches_adj: list[int] | None = None,
    coaches_other: list[int] | None = (),
    staff: list[int] | None = (),
  ) -> bytes | None:
    """
    Generate and download the eligible-players PDF for one team in a game.

    Replicates the browser's "Imprimir listagem" button:  fetches the eligible-
    players page, selects all players/staff, and POSTs to ``pdf/listagemjogo.php``
    to receive a PDF from the server.

    Args:
        game_id:          Internal SAV2 game ID (``Game.id`` from ``list_games``).
        val:              Team selector — 1 for home (equipa_casa), 2 for away (equipa_fora).
        player_licences:  If given, only these licence numbers are included.
                          Any licence not in the eligible list is silently ignored.
                          Pass ``None`` (default) to include all eligible players.
        coaches_pri:      Wallet numbers for head coaches to include. ``None`` = all.
        coaches_adj:      Wallet numbers for adjunct coaches to include. ``None`` = all.
        coaches_other:    Wallet numbers for other coaching roles to include. ``None`` = all;
                          default ``()`` = none (excluded by default).
        staff:            Licence numbers for other staff (enquadramento humano) to include.
                          ``None`` = all; default ``()`` = none (excluded by default).

    Returns:
        PDF bytes, or None if no eligible players page exists for this game.

    Raises:
        SavConnectionError: On network errors.
        SavResponseError:   If the response cannot be parsed.
    """
    if self.session is None:
      raise SavResponseError("Must call login() before get_eligible_players_pdf()")

    import re as _re
    from bs4 import BeautifulSoup

    # Fetch the eligible players page
    try:
      resp = self._http.get(
        self._url(f"php/maindb.php?op=16&jogo={game_id}"),
        timeout=self._timeout,
      )
      resp.raise_for_status()
    except Exception as exc:
      raise SavConnectionError(f"Could not fetch eligible players page: {exc}") from exc

    try:
      import json as _json
      raw = _json.loads(resp.text)
    except ValueError as exc:
      raise SavResponseError(
        f"Eligible players response was not valid JSON: {resp.text[:200]!r}"
      ) from exc

    soup = BeautifulSoup(raw.get("msg", ""), "html.parser")

    # Find the exportElegiveis button for the requested team
    equipa_id: int | None = None
    for btn in soup.find_all(attrs={"onclick": _re.compile(r"exportElegiveis")}):
      m = _re.search(rf"exportElegiveis\({val}\s*,\s*\d+\s*,\s*(\d+)\)", btn.get("onclick", ""))
      if m:
        equipa_id = int(m.group(1))
        break

    if equipa_id is None:
      return None

    # Prefix for this team's checkboxes: "casa" (val=1) or "fora" (val=2)
    side = "casa" if val == 1 else "fora"

    def _ids(pattern: str) -> list[int]:
      return [
        int(_re.search(pattern, cb["id"]).group(1))
        for cb in soup.find_all("input", id=_re.compile(pattern))
      ]

    def _filter(ids: list[int], keep: list[int] | None) -> list[int]:
      return ids if keep is None else [i for i in ids if i in set(keep)]

    licences = _filter(_ids(rf"^jog{side}(\d+)$"),       player_licences)
    pri      = _filter(_ids(rf"^trepri{side}(\d+)$"),    coaches_pri)
    adj      = _filter(_ids(rf"^treadj{side}(\d+)$"),    coaches_adj)
    outros   = _filter(_ids(rf"^treoutros{side}(\d+)$"), coaches_other)
    enq      = _filter(_ids(rf"^enq{side}(\d+)$"),       staff)

    num_camisola = {str(lic): "" for lic in licences}

    payload = {
      "jogo": game_id,
      "equipa": equipa_id,
      "numJogadores": len(licences),
      "arrayJogadores": _json.dumps(licences),
      "arrayNumCamisola": _json.dumps(num_camisola),
      "arrayTreinadoresPRI": _json.dumps(pri),
      "arrayTreinadoresADJ": _json.dumps(adj),
      "arrayTreinadoresOutros": _json.dumps(outros),
      "arrayEnq": _json.dumps(enq),
    }

    logger.info(
      "Generating eligible players PDF for game_id=%s team=%s (%d players)",
      game_id, side, len(licences),
    )
    try:
      pdf_resp = self._http.post(
        self._url("pdf/listagemjogo.php"),
        data=payload,
        timeout=self._timeout,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
      )
      pdf_resp.raise_for_status()
    except Exception as exc:
      raise SavConnectionError(f"Could not generate PDF: {exc}") from exc

    if not pdf_resp.content.startswith(b"%PDF"):
      raise SavResponseError(
        f"Eligible players PDF endpoint returned non-PDF: {pdf_resp.text[:200]!r}"
      )

    return pdf_resp.content

  def list_associations(self) -> list[Club]:
    """
    Return all associations available in the system (cached, TTL 7 days).

    Scraped from the server-rendered dropdown on the players search page.

    Returns:
        List of Club objects (id = association numeric ID, name = display
        name) sorted by name.

    Raises:
        SavResponseError:   If the page cannot be parsed.
        SavConnectionError: On network errors.
    """
    if self.session is None:
      raise SavResponseError("Must call login() before list_associations()")
    return self._cache.get_associations(self._fetch_associations)

  def list_clubs(self, association: int | None = None) -> list[Club]:
    """
    Return clubs, optionally filtered by association (cached, TTL 7 days).

    When `association` is omitted the clubs in the logged-in organisation's
    own association are returned (mirrors the "Clubes" dropdown default).

    Args:
        association: Numeric association ID (from list_associations()).
                     If None, defaults to the session organisation's
                     association.

    Returns:
        List of Club objects sorted by name.

    Raises:
        SavResponseError:   If the response cannot be parsed.
        SavConnectionError: On network errors.
    """
    if self.session is None:
      raise SavResponseError("Must call login() before list_clubs()")
    return self._cache.get_clubs(self._fetch_and_enrich_clubs, association=association)

  def invalidate_cache(self) -> None:
    """Clear all locally cached data (clubs, associations)."""
    self._cache.invalidate()

  def _fetch_associations(self) -> list[Club]:
    import re

    try:
      resp = self._http.get(self._url("jogadores.php"), timeout=self._timeout)
      resp.raise_for_status()
    except Exception as exc:
      raise SavConnectionError(f"Could not fetch associations: {exc}") from exc

    options = re.findall(r"value='ass,(\d+)'[^>]*>([^<]+)<", resp.text)
    if not options:
      raise SavResponseError(
        "Could not find associations in page — page structure may have changed"
      )

    return sorted(
      [Club(id=int(i), name=name.strip()) for i, name in options],
      key=lambda c: c.name,
    )

  def _fetch_and_enrich_clubs(self, association: int | None) -> list[Club]:
    if association is not None:
      html = self._post_form(
        _CLUBS_BY_ASSOC_PATH,
        {"associacao": f"ass,{association}"},
        params={"op": _CLUBS_BY_ASSOC_OP},
      )
      clubs = self._parse_clubs_html(html)
    else:
      org = self.session.get("organizacao", 0)
      text = self._post_form(
        _CLUBS_BY_ORG_PATH, {"clube": org}, params={"op": _CLUBS_BY_ORG_OP}
      )
      try:
        import json as _json
        raw = _json.loads(text)
      except ValueError as exc:
        raise SavResponseError(
          f"Clubs response was not valid JSON: {text[:200]!r}"
        ) from exc
      clubs = self._parse_clubs_response(raw)

    enriched = []
    for c in clubs:
      full_name, code = self._fetch_club_names(c.id)
      enriched.append(Club(id=c.id, name=c.name, full_name=full_name, code=code))
    return enriched

  # ------------------------------------------------------------------
  # Internal helpers
  # ------------------------------------------------------------------

  def _url(self, path: str) -> str:
    """Resolve a relative path against the base URL."""
    return urljoin(self.base_url, path.lstrip("/"))

  def _post(
    self,
    path: str,
    payload: dict[str, Any],
    *,
    params: dict[str, str] | None = None,
  ) -> dict[str, Any]:
    """
    POST JSON to `path` and return the parsed response body.

    Raises:
        SavConnectionError: On any network or HTTP error.
        SavResponseError:   If the response body is not valid JSON.
    """
    url = self._url(path)
    logger.debug("POST %s params=%s payload=%s", url, params, payload)

    try:
      response = self._http.post(
        url,
        json=payload,
        params=params,
        timeout=self._timeout,
      )
      response.raise_for_status()
    except requests.exceptions.Timeout as exc:
      raise SavConnectionError(
        f"Request to {url} timed out after {self._timeout}s"
      ) from exc
    except requests.exceptions.ConnectionError as exc:
      raise SavConnectionError(
        f"Could not connect to {url}: {exc}"
      ) from exc
    except requests.exceptions.HTTPError as exc:
      raise SavConnectionError(
        f"HTTP {exc.response.status_code} from {url}"
      ) from exc
    except requests.exceptions.RequestException as exc:
      raise SavConnectionError(f"Request failed: {exc}") from exc

    try:
      data: dict[str, Any] = response.json()
    except ValueError as exc:
      snippet = response.text[:200]
      raise SavResponseError(
        f"Server returned non-JSON response from {url}: {snippet!r}"
      ) from exc

    logger.debug("Response from %s: %s", url, data)
    return data

  def _parse_login_response(self, raw: dict[str, Any]) -> LoginResult:
    """
    Translate the raw login JSON into a LoginResult.

    Expected shape:
        {"val": 1, "msg": "...", "sessao": {...}, "redirect": "..."}

    Raises:
        SavResponseError: If `val` is absent (malformed response).
        SavAuthError:     If `val != 1` (server-side rejection).
    """
    if "val" not in raw:
      raise SavResponseError(
        f"Login response missing 'val' field: {raw!r}"
      )

    message = strip_html(str(raw.get("msg", "")))
    val = raw["val"]

    if val != 1:
      logger.warning("Login rejected for user %r: %s", self._username, message)
      raise SavAuthError(message or "Login rejected by server")

    sessao_raw = raw.get("sessao")
    if not isinstance(sessao_raw, dict):
      raise SavResponseError(
        f"Login succeeded but 'sessao' is missing or not a dict: {raw!r}"
      )

    self.session = Session(raw=sessao_raw)
    redirect = raw.get("redirect")

    logger.info(
      "Login successful for user %r — session keys: %s, redirect: %s",
      self._username,
      list(sessao_raw.keys()),
      redirect,
    )

    return LoginResult(
      success=True,
      message=message,
      session=self.session,
      redirect=redirect,
      raw=raw,
    )

  def _post_form(
    self,
    path: str,
    payload: dict[str, Any],
    *,
    params: dict[str, str] | None = None,
  ) -> str:
    """
    POST form-encoded data to `path` and return the raw response text.

    Used for endpoints that expect ``application/x-www-form-urlencoded``
    rather than JSON (e.g. the athlete search endpoint).

    Raises:
        SavConnectionError: On any network or HTTP error.
    """
    url = self._url(path)
    logger.debug("POST (form) %s params=%s payload=%s", url, params, payload)

    try:
      response = self._http.post(
        url,
        data=payload,
        params=params,
        timeout=self._timeout,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
      )
      response.raise_for_status()
    except requests.exceptions.Timeout as exc:
      raise SavConnectionError(
        f"Request to {url} timed out after {self._timeout}s"
      ) from exc
    except requests.exceptions.ConnectionError as exc:
      raise SavConnectionError(
        f"Could not connect to {url}: {exc}"
      ) from exc
    except requests.exceptions.HTTPError as exc:
      raise SavConnectionError(
        f"HTTP {exc.response.status_code} from {url}"
      ) from exc
    except requests.exceptions.RequestException as exc:
      raise SavConnectionError(f"Request failed: {exc}") from exc

    logger.debug("Response from %s: %d bytes", url, len(response.text))
    return response.text

  def _parse_players_response(self, html: str) -> list[Player]:
    """
    Parse the HTML table returned by the athlete search endpoint.

    Each ``<tr>`` in the ``<tbody>`` maps to one Player.  The columns are:
      0: actions button  (contains seeJogador(id, ...) — extracts DB id)
      1: status icon     (fa-color-activo → active)
      2: licence number
      3: name
      4: association
      5: club
      6: tier
      7: gender
      8: season
      9: status string
      10: birth date
      11: nationality

    Raises:
        SavResponseError: If the expected table is absent from the response.
    """
    from bs4 import BeautifulSoup
    import re

    soup = BeautifulSoup(html, "html.parser")
    tbody = soup.find("tbody")
    if tbody is None:
      raise SavResponseError(
        f"Player search response missing expected table: {html[:200]!r}"
      )

    players: list[Player] = []
    for row in tbody.find_all("tr"):
      cells = row.find_all("td")
      if len(cells) < 12:
        continue

      # Extract internal DB id from onclick="seeJogador(123, ...)"
      btn = cells[0].find("button")
      onclick = btn.get("onclick", "") if btn else ""
      id_match = re.search(r"seeJogador\((\d+)", onclick)
      db_id = int(id_match.group(1)) if id_match else 0

      # Active when the icon CSS class contains 'activo' but NOT 'inactivo'
      icon = cells[1].find("i")
      icon_classes = " ".join(icon.get("class", [])) if icon else ""
      icon_title = (icon.get("data-original-title", "") if icon else "").lower()
      active = "fa-color-activo" in icon_classes or icon_title == "activo"

      players.append(Player(
        id=db_id,
        license=cells[2].get_text(strip=True),
        name=cells[3].get_text(strip=True),
        association=cells[4].get_text(strip=True),
        club=cells[5].get_text(strip=True),
        tier=cells[6].get_text(strip=True),
        gender=cells[7].get_text(strip=True),
        season=cells[8].get_text(strip=True),
        status=cells[9].get_text(strip=True),
        birth_date=cells[10].get_text(strip=True),
        nationality=cells[11].get_text(strip=True),
        active=active,
      ))

    logger.info("Parsed %d players from search response", len(players))
    return players

  def _parse_games_response(self, raw: dict[str, Any]) -> list[Game]:
    """
    Parse the games JSON returned by jogosdb.php?op=3.

    The server returns ``{"msg": "<table>...</table>", ...}``.
    Each ``<tr>`` in the ``<tbody>`` maps to one Game with columns:
      0: game number
      1: competition
      2: phase       (hidden)
      3: round       (hidden)
      4: date
      5: time
      6: home team
      7: away team
      8: home score
      9: away score
      10: venue
      11: game status
      12: result status  (hidden)
      13: tier           (hidden)
      14: gender         (hidden)
      15: level          (hidden)
      16: actions        (ignored)

    Raises:
        SavResponseError: If the expected table is absent.
    """
    from bs4 import BeautifulSoup

    if "msg" not in raw:
      raise SavResponseError(
        f"Games response missing 'msg' field: {list(raw.keys())}"
      )

    soup = BeautifulSoup(raw["msg"], "html.parser")
    tbody = soup.find("tbody")
    if tbody is None:
      raise SavResponseError("Games response missing expected table")

    import re

    games: list[Game] = []
    for row in tbody.find_all("tr"):
      tds = row.find_all("td")
      if len(tds) < 16:
        continue
      cells = [td.get_text(strip=True) for td in tds]

      # Extract internal game ID from the actions button onclick="seeJogo(id)"
      btn = tds[16].find("button") if len(tds) > 16 else None
      onclick = btn.get("onclick", "") if btn else ""
      id_match = re.search(r"seeJogo\((\d+)\)", onclick)
      game_id = int(id_match.group(1)) if id_match else 0

      games.append(Game(
        id=game_id,
        number=cells[0],
        competition=cells[1],
        phase=cells[2],
        round=cells[3],
        date=cells[4],
        time=cells[5],
        home=cells[6],
        away=cells[7],
        home_score=cells[8],
        away_score=cells[9],
        venue=cells[10],
        game_status=cells[11],
        result_status=cells[12],
        tier=cells[13],
        gender=cells[14],
        level=cells[15],
      ))

    logger.info("Parsed %d games from response", len(games))
    return games

  def _fetch_club_names(self, club_id: int) -> tuple[str, str]:
    """
    Return ``(full_name, code)`` for a single club from clubesdb.php?op=9.

    Parses the detail form HTML using the same label→sibling strategy as
    the athlete detail parser.  Returns empty strings on any error so that
    callers can gracefully fall back to storing only the display name.
    """
    try:
      import json as _json
      from bs4 import BeautifulSoup

      text = self._post_form(
        _CLUB_DETAIL_PATH, {"id": club_id}, params={"op": _CLUB_DETAIL_OP}
      )
      raw = _json.loads(text)
      soup = BeautifulSoup(raw.get("msg", ""), "html.parser")
      fields: dict[str, str] = {}
      for label in soup.find_all("label"):
        key = label.get_text(strip=True).rstrip("*").strip()
        sib = label.find_next_sibling(["input", "select", "textarea"])
        if sib is None:
          continue
        if sib.name in ("input", "textarea"):
          val = (sib.get("value", "") or sib.get_text(strip=True)).strip()
        else:
          opt = sib.find("option", selected=True)
          val = opt.get_text(strip=True) if opt else ""
        if key:
          fields[key] = val
      return fields.get("Nome do Clube", "").strip(), fields.get("Código", "").strip()
    except Exception:
      logger.debug("Could not fetch club detail for id=%s", club_id, exc_info=True)
      return "", ""

  def _parse_clubs_html(self, html: str) -> list[Club]:
    """Parse a bare ``<option>`` list (response from jogadoresdb.php?op=25)."""
    import re

    clubs: list[Club] = []
    for match in re.finditer(r"value='(\d+)'[^>]*>([^<]+)<", html):
      club_id = int(match.group(1))
      name = match.group(2).strip()
      if club_id != 0:
        clubs.append(Club(id=club_id, name=name))
    return sorted(clubs, key=lambda c: c.name)

  def _parse_clubs_response(self, raw: dict[str, Any]) -> list[Club]:
    """Parse the JSON envelope returned by resultadosdb.php?op=17."""
    if "clubes" not in raw:
      raise SavResponseError(
        f"Clubs response missing 'clubes' field: {raw!r}"
      )
    return self._parse_clubs_html(raw["clubes"])

  def _parse_player_detail_response(self, raw: dict[str, Any], *, player_id: int) -> Player:
    """
    Parse the photo URL from the JSON envelope returned by jogadoresdb.php?op=2.

    The server returns ``{"msg": "<html>..."}``; we scan ``<img>`` tags for
    the athlete's photo and return a minimal Player with only id + photo_url.
    """
    from bs4 import BeautifulSoup

    if "msg" not in raw:
      raise SavResponseError(
        f"Player detail response missing 'msg' field: {list(raw.keys())}"
      )

    soup = BeautifulSoup(raw["msg"], "html.parser")

    photo_url = ""
    for img in soup.find_all("img"):
      src = img.get("src", "")
      if any(k in src.lower() for k in ("uploads", "foto", "photo", "jogador")):
        photo_url = src
        break

    return Player(
      id=player_id, license="", name="", association="", club="",
      tier="", gender="", birth_date="", nationality="", status="",
      photo_url=photo_url,
    )

  def __repr__(self) -> str:
    authenticated = self.session is not None
    return (
      f"SavClient(base_url={self.base_url!r}, "
      f"user={self._username!r}, authenticated={authenticated})"
    )
