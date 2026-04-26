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
from .models import Player, Club, Game, LoginResult, PlayerRegistrationBatch, Session
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
_REGISTRATIONS_PATH = "php/incricoesdb.php"
_REGISTRATIONS_LIST_OP = "170"
_REGISTRATIONS_TIERS_OP = "3"
_REGISTRATIONS_CREATE_OP = "4"
_REGISTRATIONS_DELETE_OP = "9"
_REGISTRATIONS_BATCH_DETAIL_OP = "10"
_REGISTRATIONS_REMOVE_ITEM_OP = "29"
_REGISTRATIONS_LIST_REVALIDABLE_OP = "139"
_REGISTRATIONS_LOAD_PLAYER_OP = "35"
_REGISTRATIONS_SAVE_STEP1_OP = "33"
_REGISTRATIONS_SAVE_STEP2_OP = "31"
_REGISTRATIONS_LOAD_SEGURO_OP = "87"
_REGISTRATIONS_LOAD_COMPANHIA_OP = "175"
_REGISTRATIONS_LOAD_APOLICE_OP = "24"
_REGISTRATIONS_TAXA_PRECHECK_OP = "162"
_REGISTRATIONS_LOAD_TAXA_OP = "26"
_REGISTRATIONS_PRECOMMIT_OP = "165"
_REGISTRATIONS_COMMIT_OP = "36"
_REGISTRATIONS_AGENTE_PLAYER = 1
_REGISTRATIONS_STATE_OPEN = 1
_REGISTRATIONS_TYPE_REVALIDACAO = 2
_REGISTRATIONS_TIPOSEGURO_FEDERACAO = 1
_REGISTRATIONS_PORTUGAL_ID = 155
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
        "Accept": "application/json",
        # Mimic the browser user-agent the system expects
        "User-Agent": (
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
          "AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/124.0.0.0 Safari/537.36"
        ),
      }
    )
    # Note: Content-Type is intentionally NOT set as a session default — it
    # would override per-request values set automatically by `json=` (JSON)
    # or `files=` (multipart with boundary). Each call sets its own.

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
    association: int | None = None,
    club: int | list[int] | None = None,
    birth_year: int | list[int] | None = None,
    page: int = 1,
    limit: int | None = None,
  ) -> list[Player]:
    """
    Search for players in the SAV2 system.

    Pass ``club=0`` to search across all clubs (scoped by ``association``
    when provided, or federation-wide). Pass a list of IDs to search
    specific clubs in parallel and return a merged, deduplicated result.

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
        association: SAV2 association ID. ``None`` means no association
                     filter. Pass a real association ID when narrowing an
                     all-clubs search.
        club:        SAV2 club ID, list of IDs, or ``0`` for all clubs.
                     Required explicitly for predictable search scope.
        birth_year:  Filter by birth year. Pass a single int or a list of ints
                     (OR semantics). Applied client-side against the year
                     component of ``Player.birth_date``.
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
    if association == 0:
      raise ValueError(
        "association=0 is no longer supported. Omit association for no filter, "
        "or use club=0 with association=None for federation-wide search."
      )
    if club is None:
      raise ValueError(
        "club is required. Pass a club id/list, or 0 for all clubs."
      )

    status_filter = self._parse_player_status_filter(status)
    birth_years = self._parse_birth_year_filter(birth_year)
    # Status and birth-year are applied client-side after the server returns,
    # so a parallel short-circuit on `limit` would cut off pre-filter.
    parallel_limit = (
      None if status_filter is not None or birth_years is not None else limit
    )

    def _post_filter(results: list[Player]) -> list[Player]:
      results = self._filter_players_status(results, status_filter)
      results = self._filter_players_birth_year(results, birth_years)
      return results[:limit] if limit is not None else results

    if isinstance(tier, list):
      results = self._search_tier_list(
        tier, name=name, license=license, number=number, gender=gender,
        season=season, association=association, club=club,
        status=status, limit=parallel_limit,
      )
      return _post_filter(results)

    if season is None:
      season = int(self.session.get("epoca_id") or 0)
    elif season == 0:
      pass  # 0 means "all seasons" — send as-is

    filters = dict(
      name=name, license=license, number=number, gender=gender,
      tier=tier, season=season,
    )

    if isinstance(club, list):
      results = self._search_club_list(club, limit=parallel_limit, **filters)
      return _post_filter(results)

    if club == 0:
      results = self._search_all_clubs(association=association, limit=parallel_limit, **filters)
      return _post_filter(results)

    results = self._search_players_single(
      association=association, club=club, page=page, **filters,
    )
    return _post_filter(sorted(results, key=lambda p: p.id))

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
      [p for p in players if p.active == status_filter],
      key=lambda p: p.id,
    )

  @staticmethod
  def _parse_birth_year_filter(birth_year: int | list[int] | None) -> set[int] | None:
    """Normalise birth_year input to a set of ints, or None for no filter."""
    if birth_year is None:
      return None
    years = [birth_year] if isinstance(birth_year, int) else list(birth_year)
    result: set[int] = set()
    for y in years:
      try:
        result.add(int(y))
      except (TypeError, ValueError):
        raise ValueError(f"birth_year must be int or list[int], got {y!r}")
    return result or None

  @staticmethod
  def _filter_players_birth_year(
    players: list[Player], birth_years: set[int] | None,
  ) -> list[Player]:
    """Filter players whose birth_date year matches any of the requested years."""
    if not birth_years:
      return players
    def _year_of(p: Player) -> int | None:
      head = (p.birth_date or "").split("-", 1)[0]
      return int(head) if head.isdigit() else None
    return [p for p in players if _year_of(p) in birth_years]

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
    association: int | None = None,
    max_workers: int = 8,
    limit: int | None = None,
    **filters,
  ) -> list[Player]:
    """
    Search every club in parallel and aggregate results, deduplicating by player id.

    When ``association`` is provided, only clubs from that association are
    searched. Otherwise every club across every association is searched.
    Up to ``max_workers`` clubs are queried concurrently.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    clubs_by_id: dict[int, Any] = {}

    if association is not None:
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
        org = int(self.session.get("organizacao") or 0)
        if org:
          clubs_by_id[org] = Club(id=org, name=f"Club {org}")

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
    association: int | None = None,
    club: int,
    page: int = 1,
  ) -> list[Player]:
    """Issue a single search request for one club and return results."""
    payload = {
      "jc_findByLicense": license,
      "jc_findByName": name,
      "jc_findByNumber": number,
      "jc_sexo": gender,
      "jc_escalao": tier,
      "jc_associacao": 0 if association is None else association,
      "jc_epoca": season,
      "perfil": self.session.get("perfil", 0),
      "user": self.session.get("user", ""),
      "organizacao": self.session.get("organizacao", 0),
      "numpag": page,
      "nr_dtnasc": "",
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

  def list_player_registration_batches(
    self,
    *,
    season: int | None = None,
  ) -> list[PlayerRegistrationBatch]:
    """
    Return player registration batches ("Lotes" / "Guias de Inscrição")
    visible to the authenticated club for one season.

    The Pesquisa Lotes page renders four states (`Em construção`,
    `Devolvida`, `Em Validação`, `Em Pagamento`); this method returns all
    of them. To find a batch you can still add players to, prefer
    ``find_open_player_registration_batch()`` or filter by
    ``Batch.is_open``.

    Args:
        season: SAV2 epoch ID. Defaults to the current session epoch.

    Returns:
        List of PlayerRegistrationBatch objects, ordered as the server
        returns them (most recent first).

    Raises:
        SavResponseError:   If the response cannot be parsed.
        SavConnectionError: On network errors.
    """
    if self.session is None:
      raise SavResponseError(
        "Must call login() before list_player_registration_batches()"
      )

    if season is None:
      season = int(self.session.get("epoca_id") or 0)

    import json as _json

    info = {
      "epoca": str(season),
      "perfil": self.session.get("perfil", 0),
      "user": self.session.get("user", ""),
      "organizacao": self.session.get("organizacao", 0),
      "agente": _REGISTRATIONS_AGENTE_PLAYER,
    }
    payload = {"info": _json.dumps(info)}

    raw = self._post_form(
      _REGISTRATIONS_PATH,
      payload,
      params={"op": _REGISTRATIONS_LIST_OP},
    )

    try:
      data = _json.loads(raw)
    except ValueError as exc:
      raise SavResponseError(
        f"Could not parse registration batches response: {raw[:200]!r}"
      ) from exc

    rows = data.get("data") or []
    return [self._parse_registration_batch(row) for row in rows]

  def list_player_registration_tiers(
    self, *, gender_id: int,
  ) -> dict[int, str]:
    """
    Return the registration tiers (escalões) available for a given gender,
    as a mapping of numeric tier ID -> human name (e.g. 5 -> "Sub 14").

    The set differs by gender (some categories are male- or female-only),
    so a gender_id is required.

    Args:
        gender_id: 1=Masculino, 2=Feminino.

    Returns:
        Dict mapping tier ID to display name. The "Não selecionado" placeholder
        (id=0) is excluded.

    Raises:
        SavResponseError:   If the response cannot be parsed.
        SavConnectionError: On network errors.
    """
    if self.session is None:
      raise SavResponseError(
        "Must call login() before list_player_registration_tiers()"
      )
    if gender_id not in (1, 2):
      raise ValueError("gender_id must be 1 (Masculino) or 2 (Feminino)")

    import re

    url = self._url(_REGISTRATIONS_PATH)
    try:
      resp = self._http.get(
        url,
        params={"op": _REGISTRATIONS_TIERS_OP, "genero": gender_id},
        timeout=self._timeout,
      )
      resp.raise_for_status()
    except requests.exceptions.RequestException as exc:
      raise SavConnectionError(f"Could not fetch tiers: {exc}") from exc

    options = re.findall(r"<option value='(\d+)'\s*>([^<]+)</option>", resp.text)
    return {int(i): name.strip() for i, name in options if int(i) != 0}

  def _resolve_club_association_id(self, club_id: int) -> int:
    """Walk associations to find the one containing the club. Cached per client."""
    cache = getattr(self, "_club_assoc_cache", None)
    if cache is None:
      cache = {}
      self._club_assoc_cache = cache
    if club_id in cache:
      return cache[club_id]
    for assoc in self.list_associations():
      try:
        clubs = self.list_clubs(association=assoc.id)
      except Exception:
        continue
      if any(c.id == club_id for c in clubs):
        cache[club_id] = assoc.id
        return assoc.id
    raise ValueError(
      f"Could not find association for club_id={club_id}; pass association_id explicitly."
    )

  def _resolve_tier_id(self, tier: int | str, gender_id: int) -> int:
    """Accept a tier name or ID and return the numeric ID."""
    if isinstance(tier, int):
      return tier
    if isinstance(tier, str) and tier.strip().isdigit():
      return int(tier.strip())
    tiers = self.list_player_registration_tiers(gender_id=gender_id)
    wanted = tier.strip().casefold()
    for tier_id, name in tiers.items():
      if name.strip().casefold() == wanted:
        return tier_id
    available = ", ".join(sorted(tiers.values()))
    raise ValueError(
      f"Tier {tier!r} not found for gender_id={gender_id}. Available: {available}"
    )

  def create_player_registration_batch(
    self,
    *,
    type: int,
    tier: int | str,
    gender_id: int,
    association_id: int | None = None,
    club_id: int | None = None,
    season: int | None = None,
  ) -> int:
    """
    Create a new player registration batch ("Lote") for one
    (type, tier, gender) combination.

    SAV2 does NOT prevent duplicate open batches: calling this twice
    with the same args yields two distinct "Em construção" batches.
    Callers that want to reuse an existing batch must check first via
    ``find_open_player_registration_batch()``.

    Args:
        type:           1=1ª Inscrição, 2=Revalidação, 3=Transferência,
                        4=Subida de escalão.
        tier:           Tier ID (e.g. 5) or display name (e.g. "Sub 14").
        gender_id:      1=Masculino, 2=Feminino.
        association_id: Defaults to the session's organization association.
                        Required only when the user can manage multiple
                        associations.
        club_id:        Defaults to the session's organization. Required only
                        when the user can manage multiple clubs.
        season:         SAV2 epoch ID. Defaults to the current session epoch.

    Returns:
        The new batch's internal ID (the same value as ``Batch.id`` from
        ``list_player_registration_batches()``).

    Raises:
        SavResponseError:   If the response cannot be parsed or signals failure.
        SavConnectionError: On network errors.
    """
    if self.session is None:
      raise SavResponseError(
        "Must call login() before create_player_registration_batch()"
      )

    tier_id = self._resolve_tier_id(tier, gender_id)
    if season is None:
      season = int(self.session.get("epoca_id") or 0)
    if club_id is None:
      club_id = int(self.session.get("organizacao") or 0)
    if association_id is None:
      association_id = self._resolve_club_association_id(club_id)

    params = {
      "op": _REGISTRATIONS_CREATE_OP,
      "tipo": type,
      "agente": _REGISTRATIONS_AGENTE_PLAYER,
      "associacao": f"ass,{association_id}",
      "escalao": tier_id,
      "genero": gender_id,
      "clube": club_id,
      "epoca": season,
    }

    url = self._url(_REGISTRATIONS_PATH)
    logger.info("Creating player registration batch: %s", params)
    try:
      resp = self._http.get(url, params=params, timeout=self._timeout)
      resp.raise_for_status()
    except requests.exceptions.RequestException as exc:
      raise SavConnectionError(f"Could not create batch: {exc}") from exc

    import json as _json
    text = resp.text
    logger.info("Create batch response: %s", text[:500])

    try:
      data = _json.loads(text)
    except ValueError as exc:
      raise SavResponseError(
        f"Could not parse create-batch response: {text[:200]!r}"
      ) from exc

    new_id = data.get("id")
    if not isinstance(new_id, int):
      raise SavResponseError(
        f"Create batch did not return an id: {data!r}"
      )
    return new_id

  def delete_player_registration_batch(self, batch_id: int) -> None:
    """
    Delete a player registration batch ("Lote") by ID.

    Only batches in state "Em construção" (open) can be deleted; submitted
    batches typically cannot. The server response is currently ignored —
    this method raises only on transport/HTTP errors.

    Args:
        batch_id: The internal batch ID (``Batch.id``).

    Raises:
        SavConnectionError: On network errors.
    """
    if self.session is None:
      raise SavResponseError(
        "Must call login() before delete_player_registration_batch()"
      )

    url = self._url(_REGISTRATIONS_PATH)
    logger.info("Deleting player registration batch id=%s", batch_id)
    try:
      resp = self._http.get(
        url,
        params={"op": _REGISTRATIONS_DELETE_OP, "id": batch_id},
        timeout=self._timeout,
      )
      resp.raise_for_status()
    except requests.exceptions.RequestException as exc:
      raise SavConnectionError(f"Could not delete batch: {exc}") from exc

    logger.info("Delete batch response: %s", resp.text[:200])

  def remove_player_from_registration_batch(
    self,
    batch_id: int,
    license: int,
  ) -> None:
    """
    Remove a single player (by licence) from an open registration batch.

    Loads the batch's items via op=10, finds the row whose licence matches,
    then fires op=29 with that row's `item_id`. The id passed to op=29 is
    the per-row batch-item id (from `eliJogador(item_id,...)` in the
    rendered HTML), *not* the player's internal id from op=35.

    Args:
        batch_id: Target batch.
        license:  Licence number of the player to remove.

    Raises:
        SavConnectionError: On network errors.
        SavResponseError:   If the licence isn't currently in the batch.
    """
    if self.session is None:
      raise SavResponseError(
        "Must call login() before remove_player_from_registration_batch()"
      )

    batch = next(
      (b for b in self.list_player_registration_batches() if b.id == batch_id),
      None,
    )
    if batch is None:
      raise ValueError(f"Batch id={batch_id} not found")

    items = self._load_batch_items(batch)
    item = next((it for it in items if it["license"] == license), None)
    if item is None:
      raise SavResponseError(
        f"Licence {license} is not in batch {batch_id} "
        f"(current licences: {[it['license'] for it in items]})"
      )
    item_id = int(item["item_id"])

    try:
      resp = self._http.get(
        self._url(_REGISTRATIONS_PATH),
        params={
          "op": _REGISTRATIONS_REMOVE_ITEM_OP,
          "id": item_id,
          "tipo": batch.type_id,
          "guia": batch.id,
        },
        timeout=self._timeout,
      )
      resp.raise_for_status()
    except requests.exceptions.RequestException as exc:
      raise SavConnectionError(
        f"Could not remove player {license} from batch {batch_id}: {exc}"
      ) from exc

    logger.info(
      "Removed player license=%s (item_id=%s) from batch %s — response: %s",
      license, item_id, batch.id, resp.text[:200],
    )

  def find_open_player_registration_batch(
    self,
    *,
    type: int,
    tier_id: int,
    gender_id: int,
    season: int | None = None,
  ) -> PlayerRegistrationBatch | None:
    """
    Return the first open ("Em construção") player registration batch
    matching the requested type/tier/gender, or None if none exists.

    A batch is locked to a single (type, tier, gender) combination, so
    items can only be added to a matching open batch. When more than one
    matches, the most recent (server-order first) is returned.

    Args:
        type:      Registration type ID:
                   1=1ª Inscrição, 2=Revalidação, 3=Transferência,
                   4=Subida de escalão.
        tier_id:   Numeric escalão ID (e.g. 5 = Sub 14).
        gender_id: 1=Masculino, 2=Feminino.
        season:    SAV2 epoch ID. Defaults to the current session epoch.

    Returns:
        Matching PlayerRegistrationBatch, or None if no open batch fits.
    """
    batches = self.list_player_registration_batches(season=season)
    for b in batches:
      if (
        b.is_open
        and b.type_id == type
        and b.tier_id == tier_id
        and b.gender_id == gender_id
      ):
        return b
    return None

  def add_player_to_registration_batch(
    self,
    batch_id: int,
    license: int,
    *,
    # ─── STEP 1 — Personal data (op=33) ───────────────────────────────────────
    # Auto-derived from op=35: nome, data_nascimento, genero, nacionalidade,
    # paisnascimento, nif. None on overrides = keep player's stored value.
    id_type: int | None = None,
    id_number: str | None = None,
    id_expiry: str | None = None,
    telemovel: str | None = None,
    telefone: str | None = None,
    email: str | None = None,
    nome_pai: str | None = None,
    nome_mae: str | None = None,
    # ─── STEP 2 — Address (op=31) ─────────────────────────────────────────────
    # Auto-derived: pais (always Portugal=155, locked in UI).
    morada: str | None = None,
    cod_postal: str | None = None,
    localidade_txt: str | None = None,
    distrito_id: int | None = None,
    concelho_id: int | None = None,
    # ─── STEP 3 — Sport-specific + consents (op=36) ───────────────────────────
    # Auto-derived from batch metadata: tipo, escalao, epoca, transf=0.
    # Auto-derived from op=87/175 cascade: tipoSeguro=1, seguro, comp.
    # Auto-derived from op=31 prefill: estatuto, menor_idade.
    # Auto-derived from op=26 (errors with available list when ambiguous): taxa.
    taxa_id: int | None = None,
    exam_date: str | None = None,
    promote_to_tier_id: int | None = None,
    guardian_name: str | None = None,
    guardian_relation: int | None = None,
    guardian_phone: str | None = None,
    guardian_email: str | None = None,
    consent_data: bool = True,
    consent_communications: bool = True,
    consent_marketing: bool = False,
  ) -> int:
    """
    Add a player to an open Revalidação batch by walking the SAV2 multi-step
    enrolment wizard (load player → save step 1 → save step 2 → insurance
    cascade → pre-commit → commit) and return the player's internal SAV2 id.

    Currently only Revalidação (type_id=2) is supported; 1ª Inscrição,
    Transferência, and Subida have different field surfaces and follow-up
    in a later release.

    Args:
        batch_id: Target open batch (must be in 'Em construção' state).
        license:  Licence number of the player to revalidate.

        Step 1 overrides (None = keep stored value from player record):
          id_type, id_number, id_expiry, telemovel, telefone, email,
          nome_pai, nome_mae.

        Step 2 overrides (None = keep stored value):
          morada, cod_postal, localidade_txt, distrito_id, concelho_id.

        Step 3:
          exam_date:           YYYY-MM-DD; defaults to today when omitted.
                               (The medical exam itself is always assumed done.)
          promote_to_tier_id:  Numeric escalão ID for Subida; usually unset.
          guardian_*:          Required when the player is a minor; raises
                               SavConfigError otherwise.
          consent_*:           GDPR consents.

    Returns:
        Internal SAV2 user id of the added player.

    Raises:
        SavConfigError:    Missing guardian fields for a minor; or batch is
                           not a Revalidação.
        SavResponseError:  Server signals failure on commit.
        SavConnectionError: Network errors.
    """
    if self.session is None:
      raise SavResponseError(
        "Must call login() before add_player_to_registration_batch()"
      )

    batch = next(
      (b for b in self.list_player_registration_batches() if b.id == batch_id),
      None,
    )
    if batch is None:
      raise ValueError(f"Batch id={batch_id} not found")
    if not batch.is_open:
      raise ValueError(
        f"Batch {batch.id} is not open (state={batch.state!r}); "
        "items can only be added to 'Em construção' batches."
      )
    if batch.type_id != _REGISTRATIONS_TYPE_REVALIDACAO:
      raise NotImplementedError(
        f"Only Revalidação batches are supported (type_id=2); "
        f"got type_id={batch.type_id} ({batch.type!r})."
      )

    eligible = self._list_revalidable_licenses(batch)
    if license not in eligible:
      raise ValueError(
        f"Licence {license} is not eligible for revalidation in batch "
        f"{batch.id} ({batch.tier} {batch.gender}). The server's eligible "
        f"list has {len(eligible)} player(s); pass one of those licences."
      )

    # ── STEP 1: Load player demographics, save personal data ──────────────────
    record = self._load_player_record(batch.id, license)
    internal_id = int(record["id"])

    step1_send = self._build_step1_send(
      record,
      id_type=id_type, id_number=id_number, id_expiry=id_expiry,
      telemovel=telemovel, telefone=telefone, email=email,
      nome_pai=nome_pai, nome_mae=nome_mae,
    )
    step2_prefill = self._save_registration_step1(batch.id, internal_id, step1_send)

    # ── STEP 2: Save address, get step 3 prefill ──────────────────────────────
    step2_send = self._build_step2_send(
      step2_prefill,
      morada=morada, cod_postal=cod_postal, localidade_txt=localidade_txt,
      distrito_id=distrito_id, concelho_id=concelho_id,
    )
    step3_prefill = self._save_registration_step2(
      batch.type_id, batch.id, internal_id, license, step2_send,
    )

    # ── Validate guardian fields for minors ───────────────────────────────────
    is_minor = bool(step3_prefill.get("menor_idade"))
    if is_minor:
      missing = [
        n for n, v in [
          ("guardian_name", guardian_name),
          ("guardian_relation", guardian_relation),
          ("guardian_phone", guardian_phone),
          ("guardian_email", guardian_email),
        ]
        if not v
      ]
      if missing:
        raise SavConfigError(
          f"Player (license={license}) is a minor; missing required fields: "
          f"{', '.join(missing)}"
        )

    # ── Insurance cascade ─────────────────────────────────────────────────────
    # Note: only companhia is used downstream — seguro_id is fetched and used
    # internally by op=175 to derive companhia, then discarded.
    escalao = int(step3_prefill.get("escalao") or batch.tier_id)
    _, companhia_id = self._resolve_insurance_cascade(
      internal_id, batch, escalao,
    )

    # ── Taxa (registration fee) cascade — auto-pick when only one option ──────
    estatuto = step3_prefill.get("estatuto", "")
    if taxa_id is None:
      taxa_id = self._resolve_taxa_id(batch, internal_id, estatuto)

    # ── Pre-commit hook + final commit ────────────────────────────────────────
    self._registration_precommit(batch.id, internal_id)

    if exam_date is None:
      from datetime import date
      exam_date = date.today().isoformat()

    commit_body = {
      "guiaid": batch.id,
      "userid": internal_id,
      "transf": 0,
      "estatuto": str(step3_prefill.get("estatuto", "")),
      "exame": "1",
      "sub": str(promote_to_tier_id) if promote_to_tier_id is not None else "-1",
      "obs": "",
      "dataexame": exam_date,
      "escalaosubida_txt": "- Não selecionado –",
      "taxa": str(taxa_id),
      "comp": str(companhia_id),
      "nomeEncarregado": guardian_name or "",
      "tipoRegulacao": str(guardian_relation) if guardian_relation else "0",
      "telefoneEncarregado": guardian_phone or "",
      "emailEncarregado": guardian_email or "",
      "consentimentoDados": 1 if consent_data else 0,
      "comunicacoes": 1 if consent_communications else 0,
      "marketing": 1 if consent_marketing else 0,
    }

    result = self._registration_commit(commit_body)
    if result.get("val") != 1:
      raise SavResponseError(
        f"Add player commit failed: {result.get('msg') or result!r}"
      )
    logger.info(
      "Added player license=%s (id=%s) to batch %s — validity: %s",
      license, internal_id, batch.id, result.get("resultfunction"),
    )
    return internal_id

  # ------------------------------------------------------------------
  # Registration wizard helpers
  # ------------------------------------------------------------------

  @staticmethod
  def _serialize_send(fields: list[tuple[str, str, Any]]) -> str:
    """
    Serialise (key, type, value) tuples into SAV2's quirky comma-separated
    format used by ops 33 (step 1) and 31 (step 2).

    `type` is "str" (value double-quoted) or "int" (bare numeric). None or
    empty values are emitted as bare ``NULL``. A trailing comma is appended
    to mimic the browser's output exactly.
    """
    parts: list[str] = []
    for key, typ, value in fields:
      if value is None or value == "":
        parts.append(f"{key}=NULL")
      elif typ == "str":
        parts.append(f'{key}="{value}"')
      elif typ == "int":
        parts.append(f"{key}={int(value)}")
      else:
        raise ValueError(f"Unknown send-format type {typ!r}")
    return ",".join(parts) + ","

  def _list_revalidable_licenses(
    self, batch: PlayerRegistrationBatch,
  ) -> set[int]:
    """
    Op=139 — list licences eligible for revalidation in a batch.

    The server scopes the result by the batch's club, type, and (implicitly
    via guia) tier+gender. Returns a bare ``<option>`` HTML list keyed by
    licence number; we collect the licences as ints, dropping the empty
    placeholder.
    """
    import re
    try:
      resp = self._http.post(
        self._url(_REGISTRATIONS_PATH),
        params={"op": _REGISTRATIONS_LIST_REVALIDABLE_OP},
        data={"clube": batch.club_id, "tipo": batch.type_id, "guia": batch.id},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=self._timeout,
      )
      resp.raise_for_status()
    except requests.exceptions.RequestException as exc:
      raise SavConnectionError(
        f"Could not list revalidable players for batch {batch.id}: {exc}"
      ) from exc
    return {
      int(m) for m in re.findall(r"<option value='(\d+)'", resp.text) if int(m) > 0
    }

  def _load_batch_items(
    self, batch: PlayerRegistrationBatch,
  ) -> list[dict[str, Any]]:
    """
    Op=10 — render a batch's player rows and parse out (item_id, license).

    The op returns JSON wrapping the batch panel HTML in `msg`. Each row's
    delete button carries `onclick='eliJogador(item_id, batch_id, tipo)'`
    where ``item_id`` is the per-row id we need for op=29 (it is *not* the
    player's internal id from op=35).
    """
    import json as _json
    import re
    from bs4 import BeautifulSoup

    try:
      resp = self._http.get(
        self._url(_REGISTRATIONS_PATH),
        params={
          "op": _REGISTRATIONS_BATCH_DETAIL_OP,
          "id": batch.id,
          "tipo": batch.type_id,
          "perfil": self.session.get("perfil", 0),
          "user": self.session.get("user", ""),
          "organizacao": self.session.get("organizacao", 0),
        },
        timeout=self._timeout,
      )
      resp.raise_for_status()
    except requests.exceptions.RequestException as exc:
      raise SavConnectionError(
        f"Could not load batch {batch.id} detail: {exc}"
      ) from exc
    try:
      data = _json.loads(resp.text)
    except ValueError as exc:
      raise SavResponseError(
        f"Batch detail not valid JSON: {resp.text[:200]!r}"
      ) from exc

    soup = BeautifulSoup(data.get("msg", ""), "html.parser")
    items: list[dict[str, Any]] = []
    for row in soup.select("table#main5 tbody tr"):
      btn = row.find(attrs={"onclick": re.compile(r"eliJogador\(")})
      if btn is None:
        continue
      m = re.search(r"eliJogador\((\d+)\s*,", btn["onclick"])
      if not m:
        continue
      item_id = int(m.group(1))
      cells = [td.get_text(strip=True) for td in row.find_all("td")]
      # Columns: [icon, licença, nome, nasc, nacionalidade, estatuto, taxa, seguro, exame, subida]
      if len(cells) < 3:
        continue
      try:
        license = int(cells[1])
      except ValueError:
        continue
      items.append({"item_id": item_id, "license": license, "name": cells[2]})
    return items

  def _load_player_record(self, batch_id: int, license: int) -> dict[str, Any]:
    """Op=35 — fetch a player's stored demographics for prefill."""
    import json as _json
    try:
      resp = self._http.get(
        self._url(_REGISTRATIONS_PATH),
        params={
          "op": _REGISTRATIONS_LOAD_PLAYER_OP,
          "licenca": license,
          "guia": batch_id,
        },
        timeout=self._timeout,
      )
      resp.raise_for_status()
    except requests.exceptions.RequestException as exc:
      raise SavConnectionError(f"Could not load player record: {exc}") from exc
    try:
      data = _json.loads(resp.text)
    except ValueError as exc:
      raise SavResponseError(
        f"Could not parse player record: {resp.text[:200]!r}"
      ) from exc
    if not data.get("id"):
      raise SavResponseError(
        f"Player record for licence {license!r} not found: {data!r}"
      )
    return data

  def _build_step1_send(
    self,
    record: dict[str, Any],
    *,
    id_type: int | None,
    id_number: str | None,
    id_expiry: str | None,
    telemovel: str | None,
    telefone: str | None,
    email: str | None,
    nome_pai: str | None,
    nome_mae: str | None,
  ) -> str:
    """Build the step-1 `send` payload from the op=35 record + caller overrides."""
    def _pick(override: Any, stored: Any) -> Any:
      return override if override is not None else stored

    return self._serialize_send([
      ("data_nascimento",        "str", record.get("nasc")),
      ("email",                  "str", _pick(email, record.get("email"))),
      ("genero_id",              "int", record.get("genero")),
      ("telemovel",              "str", _pick(telemovel, record.get("tele"))),
      ("telefone",               "str", _pick(telefone, record.get("telef"))),
      ("profissao",              "int", record.get("profissao")),
      ("nif",                    "str", record.get("nif")),
      ("nome_mae",               "str", _pick(nome_mae, record.get("mae"))),
      ("nome_pai",               "str", _pick(nome_pai, record.get("pai"))),
      ("nacionalidade",          "int", record.get("nacional")),
      # paisnascimento is quoted in the browser payload despite being an int — UI quirk we mirror
      ("paisnascimento",         "str", record.get("naturalidade")),
      ("tipo_identificacao",     "int", _pick(id_type, record.get("tipo"))),
      ("n_identificacao",        "str", _pick(id_number, record.get("numi"))),
      ("data_val_identificacao", "str", _pick(id_expiry, record.get("dataval"))),
      ("estado_civil",           "int", record.get("estcivil")),
      ("hab_literarias",         "int", record.get("hab")),
    ])

  def _save_registration_step1(
    self, batch_id: int, internal_id: int, send: str,
  ) -> dict[str, Any]:
    """Op=33 — save personal data; response carries step 2's address prefill."""
    import json as _json
    import urllib.parse

    # The `send` value embeds raw `=` and `,` chars; preserve them through
    # URL encoding (browser leaves them, requests would normally encode `=`).
    query = (
      f"op={_REGISTRATIONS_SAVE_STEP1_OP}"
      f"&id={internal_id}&guia={batch_id}"
      f"&send={urllib.parse.quote(send, safe='=,')}"
    )
    url = f"{self._url(_REGISTRATIONS_PATH)}?{query}"

    try:
      resp = self._http.get(url, timeout=self._timeout)
      resp.raise_for_status()
    except requests.exceptions.RequestException as exc:
      raise SavConnectionError(f"Could not save step 1: {exc}") from exc
    try:
      return _json.loads(resp.text)
    except ValueError as exc:
      raise SavResponseError(
        f"Could not parse step 1 response: {resp.text[:200]!r}"
      ) from exc

  @staticmethod
  def _build_step2_send(
    prefill: dict[str, Any],
    *,
    morada: str | None,
    cod_postal: str | None,
    localidade_txt: str | None,
    distrito_id: int | None,
    concelho_id: int | None,
  ) -> str:
    """Build the step-2 `send` payload — pais is locked to Portugal."""
    def _pick(override: Any, stored: Any) -> Any:
      return override if override is not None else stored

    return SavClient._serialize_send([
      ("pais",           "int", _REGISTRATIONS_PORTUGAL_ID),
      ("distrito",       "int", _pick(distrito_id, prefill.get("distrito"))),
      ("concelho",       "int", _pick(concelho_id, prefill.get("concelho"))),
      ("localidade",     "int", prefill.get("localidade")),
      ("morada",         "str", _pick(morada, prefill.get("morada"))),
      ("cod_postal",     "str", _pick(cod_postal, prefill.get("codpostal"))),
      ("localidade_txt", "str", _pick(localidade_txt, prefill.get("localidade_txt"))),
    ])

  def _save_registration_step2(
    self,
    batch_type: int,
    batch_id: int,
    internal_id: int,
    license: int,
    send: str,
  ) -> dict[str, Any]:
    """Op=31 — multipart POST that saves address + returns step 3 prefill."""
    import json as _json
    files = {
      "tipo":    (None, str(batch_type)),
      "guiaid":  (None, str(batch_id)),
      "id":      (None, str(internal_id)),
      "licenca": (None, str(license)),
      "send":    (None, send),
    }
    url = self._url(_REGISTRATIONS_PATH)
    try:
      # Override the session's default JSON content-type by passing a custom
      # headers dict that excludes Content-Type (requests sets multipart).
      resp = self._http.post(
        url,
        params={"op": _REGISTRATIONS_SAVE_STEP2_OP},
        files=files,
        timeout=self._timeout,
        headers={"Accept": "*/*"},
      )
      resp.raise_for_status()
    except requests.exceptions.RequestException as exc:
      raise SavConnectionError(f"Could not save step 2: {exc}") from exc
    try:
      return _json.loads(resp.text)
    except ValueError as exc:
      raise SavResponseError(
        f"Could not parse step 2 response: {resp.text[:200]!r}"
      ) from exc

  def _resolve_insurance_cascade(
    self, internal_id: int, batch: PlayerRegistrationBatch, escalao: int,
  ) -> tuple[int, int]:
    """
    Run the seguro → companhia → apolice cascade.

    Returns ``(seguro_id, companhia_id)``. The op=24 apolice fetch is fired
    for parity with the browser flow but its value (a string like
    ``100.268/1-2-16``) is not needed for the commit.

    Note: the JSON key `companhia` is overloaded by the server — in op=87 it
    actually carries the *seguro* id, while in op=175 it carries the
    insurer (seguradora) id. They are distinct entities despite the name.
    """
    import json as _json

    season_param = f"'{batch.season}'"  # The literal `'2025/2026'` form

    # op=87: pick tipoSeguro=1 → returns seguro id (despite key name 'companhia')
    try:
      r = self._http.get(
        self._url(_REGISTRATIONS_PATH),
        params={
          "op": _REGISTRATIONS_LOAD_SEGURO_OP,
          "id": internal_id,
          "agente": _REGISTRATIONS_AGENTE_PLAYER,
          "epoca": season_param,
          "guia": batch.id,
          "tiposeguro": _REGISTRATIONS_TIPOSEGURO_FEDERACAO,
        },
        timeout=self._timeout,
      )
      r.raise_for_status()
      seguro_id = int(_json.loads(r.text)["companhia"])
    except (requests.exceptions.RequestException, ValueError, KeyError) as exc:
      raise SavConnectionError(f"Insurance cascade op=87 failed: {exc}") from exc

    # op=175: with seguro id → returns companhia (seguradora) id
    try:
      r = self._http.get(
        self._url(_REGISTRATIONS_PATH),
        params={
          "op": _REGISTRATIONS_LOAD_COMPANHIA_OP,
          "seguro": seguro_id,
          "escalao": escalao,
          "guia": batch.id,
          "epoca": season_param,
          "nivel": 0,
        },
        timeout=self._timeout,
      )
      r.raise_for_status()
      companhia_id = int(_json.loads(r.text)["companhia"])
    except (requests.exceptions.RequestException, ValueError, KeyError) as exc:
      raise SavConnectionError(f"Insurance cascade op=175 failed: {exc}") from exc

    # op=24: apolice (display-only, ignored)
    try:
      self._http.get(
        self._url(_REGISTRATIONS_PATH),
        params={"op": _REGISTRATIONS_LOAD_APOLICE_OP, "companhia": companhia_id},
        timeout=self._timeout,
      )
    except requests.exceptions.RequestException:
      logger.debug("op=24 apolice fetch failed — non-fatal, ignoring", exc_info=True)

    return seguro_id, companhia_id

  def _resolve_taxa_id(
    self,
    batch: PlayerRegistrationBatch,
    internal_id: int,
    estatuto: str | int,
  ) -> int:
    """
    Resolve the registration fee (taxa) id via the op=162 → op=26 cascade.

    op=162 is a per-batch pre-check that returns ``"1"`` when taxa selection
    is required; we fire it for parity with the browser flow. op=26 returns
    the actual ``<option>`` HTML for the taxa dropdown, scoped by
    (estatuto, batch, esc, user). When exactly one real option is present
    we auto-pick it; otherwise we raise with the full list so the caller
    can pass ``taxa_id`` explicitly.
    """
    import re

    # op=162 (precondition flag — fire-and-forget for parity)
    try:
      self._http.post(
        self._url(_REGISTRATIONS_PATH),
        params={"op": _REGISTRATIONS_TAXA_PRECHECK_OP},
        data={"guiaid": batch.id},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=self._timeout,
      )
    except requests.exceptions.RequestException:
      logger.debug("op=162 taxa pre-check failed — non-fatal", exc_info=True)

    # op=26 — taxa options HTML
    try:
      r = self._http.get(
        self._url(_REGISTRATIONS_PATH),
        params={
          "op": _REGISTRATIONS_LOAD_TAXA_OP,
          "estatuto": estatuto,
          "guia": batch.id,
          "esc": 1,  # observed constant; meaning unclear (likely a UI flag)
          "user": internal_id,
        },
        timeout=self._timeout,
      )
      r.raise_for_status()
    except requests.exceptions.RequestException as exc:
      raise SavConnectionError(f"Could not load taxa options: {exc}") from exc

    import json as _json
    try:
      msg = _json.loads(r.text).get("msg", "")
    except ValueError as exc:
      raise SavResponseError(
        f"Could not parse taxa response: {r.text[:200]!r}"
      ) from exc

    options = {
      int(val): label.strip()
      for val, label in re.findall(
        r"<option value='(-?\d+)'[^>]*>\s*([^<]+?)\s*<", msg
      )
      if int(val) > 0
    }

    if len(options) == 1:
      return next(iter(options))
    if not options:
      raise SavResponseError(
        f"No taxa options returned for batch {batch.id}, player {internal_id}: "
        f"{msg!r}"
      )
    listing = ", ".join(f"{i}={n!r}" for i, n in sorted(options.items()))
    raise SavConfigError(
      f"Multiple taxa options for batch {batch.id}, player {internal_id}: "
      f"{listing}. Pass taxa_id= to disambiguate."
    )

  def _registration_precommit(self, batch_id: int, internal_id: int) -> None:
    """Op=165 — pre-commit log/lock hook fired right before op=36."""
    try:
      self._http.post(
        self._url(_REGISTRATIONS_PATH),
        params={"op": _REGISTRATIONS_PRECOMMIT_OP},
        data={"guiaid": batch_id, "userid": internal_id},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=self._timeout,
      )
    except requests.exceptions.RequestException:
      logger.debug("op=165 pre-commit hook failed — non-fatal", exc_info=True)

  def _registration_commit(self, body: dict[str, Any]) -> dict[str, Any]:
    """Op=36 — final commit. Body is JSON sent with text/plain Content-Type."""
    import json as _json
    try:
      resp = self._http.post(
        self._url(_REGISTRATIONS_PATH),
        params={"op": _REGISTRATIONS_COMMIT_OP},
        data=_json.dumps(body, ensure_ascii=False),
        headers={"Content-Type": "text/plain;charset=UTF-8"},
        timeout=self._timeout,
      )
      resp.raise_for_status()
    except requests.exceptions.RequestException as exc:
      raise SavConnectionError(f"Could not commit registration: {exc}") from exc
    try:
      return _json.loads(resp.text)
    except ValueError as exc:
      raise SavResponseError(
        f"Could not parse commit response: {resp.text[:200]!r}"
      ) from exc

  @staticmethod
  def _parse_registration_batch(row: dict[str, Any]) -> PlayerRegistrationBatch:
    """Map an op=170 row into a PlayerRegistrationBatch."""
    def _int(value: Any, default: int = 0) -> int:
      try:
        return int(value)
      except (TypeError, ValueError):
        return default

    return PlayerRegistrationBatch(
      id=_int(row.get("guia_id")),
      number=str(row.get("numero_guia", "")),
      type_id=_int(row.get("idtipo_guia")),
      type=str(row.get("tipo_guia", "")),
      association_id=_int(row.get("idassociacao")),
      association=str(row.get("associacao", "")),
      club_id=_int(row.get("idclube")),
      club=str(row.get("clube", "")),
      tier_id=_int(row.get("idescalao")),
      tier=str(row.get("escalao", "")),
      gender_id=_int(row.get("idgenero")),
      gender=str(row.get("genero", "")),
      state_id=_int(row.get("idestado")),
      state=str(row.get("estado", "")),
      state_date=str(row.get("dataestado", "")),
      item_count=_int(row.get("num")),
      season_id=_int(row.get("idepoca")),
      season=str(row.get("epoca", "")),
    )

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

  def list_clubs(
    self,
    association: int | None = None,
    *,
    all_associations: bool = False,
  ) -> list[Club]:
    """
    Return clubs for an explicit association scope (cached, TTL 7 days).

    Args:
        association: Numeric association ID (from list_associations()).
        all_associations: When True, aggregate clubs from every association.

    Returns:
        List of Club objects sorted by name.

    Raises:
        SavResponseError:   If the response cannot be parsed.
        SavConnectionError: On network errors.
    """
    if self.session is None:
      raise SavResponseError("Must call login() before list_clubs()")
    if association is None and not all_associations:
      raise ValueError(
        "association or all_associations=True is required for explicit club scope."
      )
    if association is not None and all_associations:
      raise ValueError("association and all_associations=True are mutually exclusive.")
    if all_associations:
      clubs_by_id: dict[int, Club] = {}
      for assoc in self.list_associations():
        for club in self.list_clubs(association=assoc.id):
          clubs_by_id[club.id] = club
      return sorted(clubs_by_id.values(), key=lambda c: c.name)
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
    if association is None:
      raise ValueError("association is required when fetching clubs for a single scope.")
    html = self._post_form(
      _CLUBS_BY_ASSOC_PATH,
      {"associacao": f"ass,{association}"},
      params={"op": _CLUBS_BY_ASSOC_OP},
    )
    clubs = self._parse_clubs_html(html)

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
