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

import json
import logging
import os
from dataclasses import replace as _dc_replace
from datetime import date
from typing import Any
from urllib.parse import urljoin

import requests
from dotenv import load_dotenv

from .exceptions import (
  LicenseNotEnrolledError,
  SavAuthError,
  SavConfigError,
  SavConnectionError,
  SavError,
  SavRecordNotFoundError,
  SavResponseError,
)
from .cache import Cache
from .models import Coach, Player, Club, Game, LoginResult, PlayerRegistrationBatch, Session
from .utils import md5_hex, strip_html

from sav_shared.lookups import player_registration_tiers
from sav_shared.text import iso_date

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Internal constants
# ---------------------------------------------------------------------------

_LOGIN_PATH = "php/logindb.php"
_LOGIN_OP = "1"
_PLAYERS_PATH = "php/jogadoresdb.php"
_PLAYERS_OP = "1"
_CLUBS_BY_ORG_PATH = "php/resultadosdb.php"
_CLUBS_BY_ORG_OP = "17"
_CLUBS_BY_ASSOC_PATH = "php/jogadoresdb.php"
_CLUBS_BY_ASSOC_OP = "25"
_GAMES_PATH = "php/jogosdb.php"
_GAMES_OP = "3"
_COACHES_PATH = "php/treinadordb.php"
_COACHES_OP = "1"
_COACHES_PERFIL = "4"
_COACH_DETAIL_PATH = "php/treinadordb.php"
_COACH_DETAIL_OP = "2"
_PLAYER_DETAIL_PATH = "php/jogadoresdb.php"
_PLAYER_DETAIL_OP = "2"
_CLUB_DETAIL_PATH = "php/clubesdb.php"
_CLUB_DETAIL_OP = "9"
_GAME_SHEET_PATH = "php/maindb.php"
_GAME_SHEET_OP = "29"
_REGISTRATIONS_PATH = "php/incricoesdb.php"
_REGISTRATIONS_LIST_OP = "170"
_REGISTRATIONS_SUBIDA_TIERS_OP = "21"
_REGISTRATIONS_CREATE_OP = "4"
_REGISTRATIONS_DELETE_OP = "9"
_REGISTRATIONS_REMOVE_ITEM_OP = "29"
_REGISTRATIONS_LIST_REVALIDABLE_OP = "139"
_REGISTRATIONS_LIST_SUBIDA_OP = "48"
_REGISTRATIONS_LOAD_SUBIDA_ORIGIN_OP = "49"
_REGISTRATIONS_LOAD_SUBIDA_SEGURO_OP = "128"
_REGISTRATIONS_LOAD_SUBIDA_TAXA_OP = "134"
_REGISTRATIONS_LOAD_SUBIDA_COMPANHIA_OP = "126"
_REGISTRATIONS_SUBIDA_COMMIT_OP = "50"
_REGISTRATIONS_LOAD_PLAYER_OP = "35"
_REGISTRATIONS_BATCH_DETAIL_OP = "10"
_REGISTRATIONS_LOAD_EXISTING_PLAYER_OP = "30"
_REGISTRATIONS_SAVE_STEP1_OP = "33"
_REGISTRATIONS_SAVE_STEP2_OP = "31"
_REGISTRATIONS_LIST_CONCELHOS_OP = "18"
_REGISTRATIONS_LOAD_SEGURO_OP = "87"
_REGISTRATIONS_LOAD_COMPANHIA_OP = "175"
_REGISTRATIONS_LOAD_APOLICE_OP = "24"
_REGISTRATIONS_TAXA_PRECHECK_OP = "162"
_REGISTRATIONS_LOAD_TAXA_OP = "26"
_REGISTRATIONS_PRECOMMIT_OP = "165"
_REGISTRATIONS_COMMIT_OP = "36"
_REGISTRATIONS_DOC_LIST_OP = "91"
_REGISTRATIONS_DOC_UPLOAD_OP = "92"
_REGISTRATIONS_DOC_DELETE_OP = "94"
_REGISTRATIONS_AGENTE_PLAYER = 1
_REGISTRATIONS_STATE_OPEN = 1
_REGISTRATIONS_TYPE_PRIMEIRA = 1
_REGISTRATIONS_TYPE_REVALIDACAO = 2
_REGISTRATIONS_TYPE_SUBIDA = 4
# 1ª Inscrição (type-1) wizard ops — distinct from Revalidação because the player
# doesn't exist in SAV yet; op=12 creates the record and yields the userid that
# everything downstream keys off.
_REGISTRATIONS_PRIMEIRA_DUPLICATE_OP = "11"      # POST {info=<json>} — pre-create duplicate check
_REGISTRATIONS_PRIMEIRA_CREATE_OP = "12"         # POST text/plain JSON — creates player, returns userid
_REGISTRATIONS_PRIMEIRA_AGE_GATE_OP = "14"       # GET datanasc&guia — confirms birthdate fits tier age window
_REGISTRATIONS_PRIMEIRA_STEP2_OP = "20"          # POST text/plain JSON — saves address, returns menor_idade
_REGISTRATIONS_PRIMEIRA_COMMIT_OP = "27"         # POST text/plain JSON — final commit
_REGISTRATIONS_PRIMEIRA_BATCH_CTX_OP = "161"     # POST guiaid — refreshes batch header context
_REGISTRATIONS_PRIMEIRA_ID_DOC_CHECK_OP = "163"  # POST numid&userid — id-doc uniqueness check
_REGISTRATIONS_PRIMEIRA_ESTATUTOS_OP = "151"     # POST userid&tipo&guiaid — loads estatuto dropdown for new player
# tipo_doc values (from <select id="tipo1"> in the upload modal)
_REGISTRATIONS_DOC_TIPO_MODELO_1 = 1   # Modelo 1 - Inscrição jogadores
_REGISTRATIONS_DOC_TIPO_EXAME_MEDICO = 2
_REGISTRATIONS_DOC_TIPO_MODELO_4 = 6
_REGISTRATIONS_DOC_TIPO_DOC_IDENTIFICACAO = 18
_REGISTRATIONS_TIPOSEGURO_FEDERACAO = 1
_REGISTRATIONS_PORTUGAL_ID = 155
# id_type / tipo_identificacao values (from <select id="tipoi"> in club registration form)
_ID_TYPE_CARTAO_CIDADAO = 1        # Cartão de Cidadão
_ID_TYPE_PASSAPORTE = 2            # Passaporte
_ID_TYPE_TITULO_RESIDENCIA = 3     # Título de residência
_ID_TYPE_BI_COMUNITARIO = 5        # BI Cidadão Comunitário
# guardian_relation / tipoRegulacao values (from <select id="tipoRegulacao"> in SAV2 form)
_GUARDIAN_RELATION_NONE = 0        # - Não selecionado –
_GUARDIAN_RELATION_PAI = 1         # Pai
_GUARDIAN_RELATION_MAE = 2         # Mãe
_GUARDIAN_RELATION_TUTOR = 3       # Tutor
_DEFAULT_TIMEOUT = 30


def _coerce_exam_date(value: str | None) -> str:
  """Return a strict ISO exam date, rejecting missing values."""
  if value is None:
    raise ValueError("exam_date must be YYYY-MM-DD; got None.")
  try:
    return date.fromisoformat(str(value)).isoformat()
  except ValueError as exc:
    raise ValueError(f"exam_date must be YYYY-MM-DD; got {value!r}.") from exc


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
    # Tracks which club rosters we've already scanned to build the
    # license↔NIF map in SavClient.find_license_by_nif.
    self._nif_clubs_built: set[int] = set()

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
    with_details: bool = False,
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
        with_details: When True, issue one extra ``jogadoresdb.php?op=2``
                     request per player to populate ``photo_url`` and
                     ``mobile_phone``. Off by default because it is N+1
                     and applied after all other filters/limits.

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
      results = results[:limit] if limit is not None else results
      if with_details:
        results = [
          _dc_replace(p, photo_url=d.photo_url, mobile_phone=d.mobile_phone)
          for p, d in (
            (p, self.get_player_detail(p.id, with_details=True)) for p in results
          )
        ]
      return results

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
    failures = 0
    with ThreadPoolExecutor(max_workers=min(max_workers, len(tiers))) as pool:
      futures = {pool.submit(self.search_players, tier=t, **kwargs): t for t in tiers}
      for future in as_completed(futures):
        try:
          for p in future.result():
            if p.id not in seen:
              seen[p.id] = p
        except (SavError, ValueError):
          failures += 1
          logger.debug("Skipping tier=%r", futures[future], exc_info=True)
        if limit is not None and len(seen) >= limit:
          for f in futures:
            f.cancel()
          break
    if failures and failures == len(futures):
      raise SavConnectionError(
        f"All {failures} parallel tier searches failed; see DEBUG logs for details"
      )
    if failures:
      logger.warning("%d of %d parallel tier searches failed", failures, len(futures))
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
    failures = 0
    with ThreadPoolExecutor(max_workers=min(max_workers, len(club_ids))) as pool:
      futures = {pool.submit(_fetch, cid): cid for cid in club_ids}
      for future in as_completed(futures):
        try:
          for p in future.result():
            if p.id not in seen:
              seen[p.id] = p
        except (SavError, ValueError):
          failures += 1
          logger.debug("Skipping club id=%s", futures[future], exc_info=True)
        if limit is not None and len(seen) >= limit:
          for f in futures:
            f.cancel()
          break
    if failures and failures == len(futures):
      raise SavConnectionError(
        f"All {failures} parallel club searches failed; see DEBUG logs for details"
      )
    if failures:
      logger.warning("%d of %d parallel club searches failed", failures, len(futures))
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
          except (SavError, ValueError):
            logger.warning("Could not list clubs for association id=%s", assoc.id, exc_info=True)
      except SavError:
        logger.warning("Could not fetch associations; falling back to own club", exc_info=True)

      if not clubs_by_id:
        org = int(self.session.get("organizacao") or 0)
        if org:
          clubs_by_id[org] = Club(id=org, name=f"Club {org}")

    def _fetch(club_id: int) -> list[Player]:
      return self._search_players_single(club=club_id, page=1, **filters)

    seen: dict[int, Player] = {}
    failures = 0
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
      futures = {pool.submit(_fetch, c.id): c.id for c in clubs_by_id.values()}
      for future in as_completed(futures):
        club_id = futures[future]
        try:
          for p in future.result():
            if p.id not in seen:
              seen[p.id] = p
        except (SavError, ValueError):
          failures += 1
          logger.debug("Skipping club id=%s during all-clubs search", club_id, exc_info=True)
        if limit is not None and len(seen) >= limit:
          for f in futures:
            f.cancel()
          break
    if failures and failures == len(futures):
      raise SavConnectionError(
        f"All {failures} parallel club searches failed during all-clubs sweep; see DEBUG logs"
      )
    if failures:
      logger.warning("%d of %d clubs failed during all-clubs search", failures, len(futures))
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
      "jc_associacao": "" if association is None else association,
      "jc_epoca": season,
      "perfil": self.session.get("perfil", 0),
      "user": self.session.get("user", ""),
      "organizacao": self.session.get("organizacao", 0),
      "numpag": page,
      "nr_dtnasc": "",
      "nr_clube": club,
    }

    logger.info("Searching players club=%s filters: %s", club, payload)
    html = self._post_form(_PLAYERS_PATH, payload, params={"op": _PLAYERS_OP})
    players = self._parse_players_response(html)
    # Opportunistic license → internal id cache fill (persisted in SQLite).
    pairs: list[tuple[int, int]] = []
    for p in players:
      try:
        pairs.append((int(p.license), p.id))
      except (ValueError, TypeError):
        continue
    self._cache.record_player_ids(pairs)
    return players

  def get_player_detail(self, player_id: int, *, with_details: bool = False) -> Player:
    """
    Fetch the detail page for a single player to obtain fields not returned
    by the listing: ``photo_url`` and ``mobile_phone``.

    Pass ``with_details=True`` to fetch and parse the detail page. With the
    default ``with_details=False`` this is a no-op that returns a minimal
    Player with only ``id`` set (prefer ``search_players`` for full data).

    Args:
        player_id:    The internal SAV2 database ID (from Player.id).
        with_details: When True, fetch the detail page and parse
                      ``photo_url`` and ``mobile_phone``.

    Raises:
        SavResponseError:   If the response cannot be parsed.
        SavConnectionError: On network errors.
    """
    if self.session is None:
      raise SavResponseError("Must call login() before get_player_detail()")

    if not with_details:
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

    logger.info("Fetching photo for player id=%s", player_id)
    text = self._post_form(_PLAYER_DETAIL_PATH, payload, params={"op": _PLAYER_DETAIL_OP})
    try:
      raw = json.loads(text)
    except ValueError as exc:
      raise SavResponseError(
        f"Player detail response was not valid JSON: {text[:200]!r}"
      ) from exc
    return self._parse_player_detail_response(raw, player_id=player_id)

  def find_license_by_nif(
    self, nif: str, *, club_id: int | None = None,
  ) -> int | None:
    """Return the license of the player with the given NIF in a club's roster.

    Lookup tiers (cheapest first):
      1. SQLite license↔NIF cache — O(1), survives across processes.
      2. Per-club roster build, persisted into the SQLite cache for
         future runs. Done at most once per club per process.

    ``club_id`` defaults to the session's own club. Returns None when the
    NIF or session club is missing, or when no roster profile matches.
    """
    nif = nif.strip() if nif else ""
    if not nif:
      return None

    hit = self._cache.get_license_by_nif(nif)
    if hit:
      return hit

    if club_id is None:
      club_id = int(self.session.get("organizacao") or 0) if self.session else 0
    if not club_id:
      return None

    if club_id in self._nif_clubs_built:
      return None

    nif_map = self._build_club_nif_map(club_id)
    self._nif_clubs_built.add(club_id)
    if nif_map:
      self._cache.record_player_nifs([(lic, n) for n, lic in nif_map.items()])
    return nif_map.get(nif)

  def _build_club_nif_map(self, club_id: int) -> dict[str, int]:
    """Build {nif → license} for the given club's roster (all seasons).

    SAV2 has no NIF-based search, so we pay one profile fetch per unique
    license (parallelised, max 8 workers). Used internally by
    find_license_by_nif and cached on the client per club.
    """
    from concurrent.futures import ThreadPoolExecutor

    try:
      roster = self.search_players(club=club_id, season=0)
    except (SavError, ValueError):
      logger.debug("Could not list roster for club_id=%s", club_id, exc_info=True)
      return {}

    seen: set[int] = set()
    licenses: list[int] = []
    for p in roster:
      try:
        lic = int(p.license)
      except (ValueError, TypeError):
        continue
      if lic not in seen:
        seen.add(lic)
        licenses.append(lic)
    if not licenses:
      return {}

    def _fetch(lic: int) -> tuple[str, int]:
      try:
        profile = self.load_player_profile(lic, club_id=club_id)
      except (SavError, ValueError):
        logger.debug("Could not load profile for license=%s", lic, exc_info=True)
        return "", 0
      return (profile.get("nif") or "").strip(), lic

    nif_map: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=min(8, len(licenses))) as pool:
      for nif_val, lic in pool.map(_fetch, licenses):
        if nif_val and lic:
          nif_map[nif_val] = lic
    return nif_map

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

    # SAV2's server-side date window is finicky: it only engages when BOTH
    # inicio AND fim are present, and only parses ISO (YYYY-MM-DD), not the
    # DD-MM-YYYY this method accepts. So translate, and when the caller gives
    # only one bound, backfill the other with an open sentinel so the server
    # still applies the half-open range we asked for. (Empty both = no window.)
    inicio = fim = ""
    if date_from or date_to:
      inicio = iso_date(date_from) if date_from else "1900-01-01"
      fim = iso_date(date_to) if date_to else "2999-12-31"

    payload = {
      "user": self.session.get("user", ""),
      "organizacao": self.session.get("organizacao", 0),
      "perfil": self.session.get("perfil", 0),
      "associacao": association,
      "prova": competition,
      "numJogo": game_number,
      "fase": phase,
      "jornada": round_,
      "inicio": inicio,
      "fim": fim,
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
      raw = json.loads(text)
    except ValueError as exc:
      raise SavResponseError(
        f"Games response was not valid JSON: {text[:200]!r}"
      ) from exc
    return self._parse_games_response(raw)

  def list_coaches(
    self,
    club: int,
    *,
    season: int | None = None,
    association: int | None = None,
    name: str = "",
    wallet: str = "",
    status: str = "active",
    gender: int = 0,
    formation_level: int = 0,
    competitive_level: int = 0,
    tptd: str = "",
    id_doc: str = "",
    birth_date: str = "",
    with_details: bool = False,
  ) -> list[Coach]:
    """
    List coaches (treinadores) registered to a club for one season.

    The SAV2 form endpoint is shared with the federation-wide coach search,
    so this client always sends ``perfil=4`` (the coaches profile) regardless
    of the session's own perfil.

    Args:
        club:              SAV2 club ID (required).
        season:            SAV2 epoch ID. Defaults to the current session epoch.
        association:       SAV2 association ID. ``None`` means no association
                           filter.
        name:              Filter by coach name. Server matches as a prefix
                           on the full name (starts-with), not a substring.
        wallet:            Filter by carteira number (exact).
        status:            ``"active"`` (default), ``"inactive"``, or ``"all"``.
                           Applied client-side using ``Coach.active``.
        gender:            0 = any, 1 = Masculino, 2 = Feminino.
        formation_level:   ``nr_formacao`` numeric code (0 = any).
        competitive_level: ``nr_competitivo`` numeric code (0 = any).
        tptd:              Filter by TPTD number.
        id_doc:            Filter by ID document number.
        birth_date:        Filter by birth date (YYYY-MM-DD).
        with_details:      When True, issue one extra request per coach to
                           populate ``nif``, ``tptd``, ``tptd_expiry``, and
                           ``mobile_phone``. Off by default because it is N+1.

    Returns:
        List of Coach objects parsed from the HTML response.

    Raises:
        SavResponseError:   If the response cannot be parsed.
        SavConnectionError: On network errors.
    """
    if self.session is None:
      raise SavResponseError("Must call login() before list_coaches()")

    status_filter = self._parse_coach_status_filter(status)

    if season is None:
      season = int(self.session.get("epoca_id") or 0)

    payload = {
      "treinadorClube": club,
      "jc_epocaTrei": season,
      "jc_treinadorAss": "" if association is None else association,
      "jc_treiNome": name,
      "jc_numCarteira": wallet,
      "perfil": _COACHES_PERFIL,
      "user": self.session.get("user", ""),
      "organizacao": self.session.get("organizacao", 0),
      "nr_numidentificacao": tptd,
      "nr_formacao": formation_level,
      "nr_competitivo": competitive_level,
      "genero": gender,
      "identificação": id_doc,
      "findByBirth": birth_date,
    }

    logger.info("Listing coaches club=%s filters: %s", club, payload)
    html = self._post_form(_COACHES_PATH, payload, params={"op": _COACHES_OP})
    coaches = self._parse_coaches_response(html)
    if status_filter is not None:
      coaches = [c for c in coaches if c.active == status_filter]

    if with_details:
      enriched: list[Coach] = []
      for c in coaches:
        detail = self.get_coach_detail(c.id)
        enriched.append(_dc_replace(
          c, nif=detail.nif, tptd=detail.tptd, tptd_expiry=detail.tptd_expiry,
          mobile_phone=detail.mobile_phone,
        ))
      coaches = enriched

    return coaches

  def get_coach_detail(self, coach_id: int) -> Coach:
    """
    Fetch the SAV2 coach profile page to obtain ``nif``, ``tptd``,
    ``tptd_expiry``, and ``mobile_phone`` for a single coach.

    The SAV2 coaches listing does not expose these fields; this method
    issues an extra ``treinadordb.php?op=2`` request per coach and parses
    the embedded HTML form. Use sparingly: this is the N+1 hop behind
    ``list_coaches(with_details=True)``.

    Args:
        coach_id: Internal SAV2 person ID (``Coach.id``).

    Returns:
        A partial Coach with ``id``, ``name``, ``nif``, ``tptd``,
        ``tptd_expiry``, and ``mobile_phone`` populated; listing-only
        fields (wallet, club, association, …) are left empty.

    Raises:
        SavResponseError:   If the response cannot be parsed.
        SavConnectionError: On network errors.
    """
    if self.session is None:
      raise SavResponseError("Must call login() before get_coach_detail()")

    payload = {
      "user_id": coach_id,
      "user": self.session.get("user", ""),
      "perfil": _COACHES_PERFIL,
      "organizacao": self.session.get("organizacao", 0),
    }

    logger.info("Fetching coach detail id=%s", coach_id)
    text = self._post_form(_COACH_DETAIL_PATH, payload, params={"op": _COACH_DETAIL_OP})
    try:
      # SAV2 occasionally emits raw CR/LF inside string values; strict=False
      # tolerates that without losing the embedded HTML.
      raw = json.loads(text, strict=False)
    except ValueError as exc:
      raise SavResponseError(
        f"Coach detail response was not valid JSON: {text[:200]!r}"
      ) from exc
    return self._parse_coach_detail_response(raw, coach_id=coach_id)

  @staticmethod
  def _parse_coach_status_filter(status: str) -> bool | None:
    """Normalise a coach status filter to active/inactive/all."""
    wanted = status.strip().lower()
    if wanted in ("", "all"):
      return None
    if wanted == "active":
      return True
    if wanted == "inactive":
      return False
    raise ValueError("status must be 'active', 'inactive', or 'all'")

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
    except requests.exceptions.RequestException as exc:
      raise SavConnectionError(f"Could not fetch game sheet info: {exc}") from exc

    try:
      raw = json.loads(resp.text)
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
    except requests.exceptions.RequestException as exc:
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
    except requests.exceptions.RequestException as exc:
      raise SavConnectionError(f"Could not fetch eligible players page: {exc}") from exc

    try:
      raw = json.loads(resp.text)
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
    except requests.exceptions.RequestException as exc:
      raise SavConnectionError(f"Could not fetch eligible players page: {exc}") from exc

    try:
      raw = json.loads(resp.text)
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
      "arrayJogadores": json.dumps(licences),
      "arrayNumCamisola": json.dumps(num_camisola),
      "arrayTreinadoresPRI": json.dumps(pri),
      "arrayTreinadoresADJ": json.dumps(adj),
      "arrayTreinadoresOutros": json.dumps(outros),
      "arrayEnq": json.dumps(enq),
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
    except requests.exceptions.RequestException as exc:
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

    info = {
      "epoca": str(season),
      "perfil": self.session.get("perfil", 0),
      "user": self.session.get("user", ""),
      "organizacao": self.session.get("organizacao", 0),
      "agente": _REGISTRATIONS_AGENTE_PLAYER,
    }
    payload = {"info": json.dumps(info)}

    raw = self._post_form(
      _REGISTRATIONS_PATH,
      payload,
      params={"op": _REGISTRATIONS_LIST_OP},
    )

    try:
      data = json.loads(raw)
    except ValueError as exc:
      raise SavResponseError(
        f"Could not parse registration batches response: {raw[:200]!r}"
      ) from exc

    rows = data.get("data") or []
    batches = [self._parse_registration_batch(row) for row in rows]
    self._cache.record_batches([(b.number, b.id) for b in batches if b.number])
    return batches

  def resolve_batch_id(self, number: str) -> int:
    """Translate a human-visible batch number (`numero_guia`) to internal batch_id.

    Hits the local cache first; on miss refreshes via
    ``list_player_registration_batches()`` (which repopulates the cache).
    Raises ``ValueError`` if no batch with that number exists.
    """
    if not number:
      raise ValueError("batch number must not be empty")
    cached = self._cache.get_batch_id(number)
    if cached is not None:
      return cached
    self.list_player_registration_batches()
    cached = self._cache.get_batch_id(number)
    if cached is not None:
      return cached
    raise ValueError(f"Batch {number!r} not found")

  def resolve_batch_id_by_license(self, license: int) -> int:
    """Find the batch_id for a license's current enrollment in an open batch.

    SAV constrains a player to at most one open batch at a time, so the
    answer is single-valued.

    Strategy:
      1. Fetch the current batch list (single HTTP call) so we know which
         batches are open *right now* — server-side state transitions
         (admin submits a batch) can invalidate a cached entry without any
         client-side write.
      2. If the cache points at a still-open batch, validate it cheaply
         with ``load_existing_registration_record``. A successful probe
         returns; a "not in this batch" response (``SavResponseError``)
         forgets and falls through. Transport errors propagate so the
         caller sees a real failure instead of a misleading "not enrolled".
      3. Otherwise scan open batches' items in order. Transport / parse
         errors propagate — silently skipping a batch that actually holds
         the player would surface as a false ``LicenseNotEnrolledError``.
      4. If no open batch contains the licence, raise
         ``LicenseNotEnrolledError`` with the open-batch list.
    """
    if license is None:
      raise ValueError("license must not be None")

    batches = self.list_player_registration_batches()
    open_batches = [b for b in batches if b.is_open]
    open_ids = {b.id for b in open_batches}

    cached = self._cache.get_batch_id_by_license(license)
    if cached is not None:
      if cached in open_ids:
        try:
          self.load_existing_registration_record(cached, license)
          return cached
        except SavRecordNotFoundError:
          # Probe came back well-formed but the player is no longer in
          # this batch — cache is stale. Fall through to a full scan.
          # SavConnectionError and parse-shape SavResponseError are NOT
          # caught here on purpose: a transport blip or a broken response
          # must surface, not get papered over as a stale entry.
          self._cache.forget_license_batch(license)
      else:
        # The cached batch is closed or no longer visible — forget it.
        self._cache.forget_license_batch(license)

    for batch in open_batches:
      items = self.list_player_registration_batch_items(batch.id)
      if any(int(item.get("license", 0)) == int(license) for item in items):
        self._cache.record_license_batch(license, batch.id)
        return batch.id

    raise LicenseNotEnrolledError(
      license=license,
      open_batches=[
        {"number": b.number, "tier": b.tier, "gender": b.gender}
        for b in open_batches
      ],
    )

  def list_player_registration_tiers(
    self, *, gender_id: int,
  ) -> dict[int, str]:
    """
    Return the registration tiers (escalões) for a given gender, as a mapping
    of numeric tier ID -> human name (e.g. 5 -> "Sub 14" for Masculino).

    SAV2 renumbers the same tier names per gender, so a gender_id is required.
    The table is stable across seasons and hardcoded in
    ``sav_shared.lookups.PLAYER_REGISTRATION_TIERS``; this no longer hits the
    network, so it works without a prior login().

    Args:
        gender_id: 1=Masculino, 2=Feminino.

    Returns:
        Dict mapping tier ID to display name. The "Não selecionado" placeholder
        (id=0) is excluded.

    Raises:
        ValueError: If gender_id is not 1 or 2.
    """
    return player_registration_tiers(gender_id)

  def get_current_season_start_year(self) -> int:
    """Return the start year of the session's current season (e.g. 2025 for "2025/2026").

    SAV2 stores the season as an opaque ``epoca_id`` integer in the session,
    so the only reliable way to map it to a calendar year is to read it back
    from any object the server tags with the season string. Registration
    batches carry ``epoca`` as ``"YYYY/YYYY+1"``; this method picks the first
    one returned for the session's club + current epoca_id and parses the
    starting year.

    Raises:
        SavResponseError: If the session has no batches in the current
        season, or the server returns a malformed season string. Callers
        that need to work in newly-created clubs should pass the year
        explicitly to whatever consumer needs it.
    """
    if self.session is None:
      raise SavResponseError(
        "Must call login() before get_current_season_start_year()"
      )
    batches = self.list_player_registration_batches()
    if not batches:
      raise SavResponseError(
        "Cannot resolve current season start year: no registration batches "
        "exist for the session's epoca_id."
      )
    season_str = batches[0].season
    try:
      return int(season_str.split("/", 1)[0])
    except (ValueError, IndexError) as exc:
      raise SavResponseError(
        f"Could not parse season string {season_str!r} from batch"
      ) from exc

  def _list_subida_tier_options(self, internal_id: int) -> list[tuple[int, str]]:
    """Op=21 — return every selectable (tier_id, name) for the player's
    escalaosubida dropdown. Empty list when SAV only exposes the
    "- Não selecionado –" placeholder.

    The subida tier is player-specific and server-computed (it is not the
    batch tier). The response is ``{"msg": "<option…>", "val": 1}`` with the
    same ``<option value='..'>`` shape as op=3.
    """
    import re

    try:
      resp = self._http.get(
        self._url(_REGISTRATIONS_PATH),
        params={"op": _REGISTRATIONS_SUBIDA_TIERS_OP, "id": internal_id},
        timeout=self._timeout,
        headers={"Accept": "*/*"},
      )
      resp.raise_for_status()
      msg = json.loads(resp.text).get("msg", "")
    except (requests.exceptions.RequestException, ValueError) as exc:
      raise SavConnectionError(f"Could not fetch subida tiers (op=21): {exc}") from exc

    # Tolerate single- or double-quoted values; option text may span newlines.
    options = re.findall(r"""<option value=['"](\d+)['"]\s*>([^<]+)</option>""", msg)
    return [(int(i), name.strip()) for i, name in options if int(i) != 0]

  def _pick_subida_tier(
    self, internal_id: int, prefer_tier_id: int | None = None,
  ) -> tuple[int, str] | None:
    """Decide which subida tier to commit, given SAV's offered options and
    an optional caller hint (the mod4-derived tier from
    ``resolve_subida_tier``).

    Rules:
      * No options offered → ``None`` (caller must surface "no subida tier").
      * ``prefer_tier_id`` set → must match one of the offered tiers; raises
        ``SavConfigError`` when the hint is incompatible with what SAV will
        accept (e.g. mod4 says Sub 18 but SAV only offers Sub 16).
      * No hint + exactly one option → auto-pick it.
      * No hint + multiple options → raise ``SavConfigError`` with the listing
        so the caller passes ``promote_to_tier_id`` explicitly.

    Returns the ``(tier_id, name)`` to be committed, or ``None`` for the
    no-subida case.
    """
    options = self._list_subida_tier_options(internal_id)
    if not options:
      return None
    if prefer_tier_id is not None:
      for tier_id, name in options:
        if tier_id == int(prefer_tier_id):
          return (tier_id, name)
      listing = ", ".join(f"{i}={n!r}" for i, n in options)
      raise SavConfigError(
        f"Requested subida tier_id={prefer_tier_id} is not among SAV's "
        f"offered options for player {internal_id}: {listing}. The form "
        f"and the server disagree — pick one of the offered tiers."
      )
    if len(options) == 1:
      return options[0]
    listing = ", ".join(f"{i}={n!r}" for i, n in options)
    raise SavConfigError(
      f"SAV offers multiple subida tiers for player {internal_id}: "
      f"{listing}. Pass promote_to_tier_id= to disambiguate (or supply a "
      f"mod4 whose escalao_subida names the desired tier)."
    )


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
      except (SavError, ValueError):
        logger.debug("Could not list clubs for association id=%s", assoc.id, exc_info=True)
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
    text = resp.text
    logger.info("Create batch response: %s", text[:500])

    try:
      data = json.loads(text)
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
    self._cache.forget_licenses_in_batch(batch_id)

  def remove_player_from_registration_batch(
    self,
    batch_id: int,
    license: int,
  ) -> None:
    """
    Remove a single player (by licence) from an open registration batch.

    Args:
        batch_id: Target batch.
        license:  Licence number of the player to remove.

    Raises:
        SavConnectionError: On network errors.
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

    try:
      resp = self._http.get(
        self._url(_REGISTRATIONS_PATH),
        params={
          "op": _REGISTRATIONS_REMOVE_ITEM_OP,
          "id": license,
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
      "Removed player license=%s from batch %s — response: %s",
      license, batch.id, resp.text[:200],
    )
    self._cache.forget_license_batch(license)

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
    license: int = 0,
    *,
    # ─── New-player demographics (type-1 only; ignored for types 2/4) ─────────
    # 1ª Inscrição has no SAV record to load from, so the caller must supply
    # the full demographic surface up front. Revalidação reads the same
    # values from op=35; Subida reuses the existing licence.
    name: str | None = None,
    birth_date: str | None = None,
    gender_id: int | None = None,
    nif: str | None = None,
    nationality_id: int = _REGISTRATIONS_PORTUGAL_ID,
    naturalidade_id: int = _REGISTRATIONS_PORTUGAL_ID,
    country_id: int = _REGISTRATIONS_PORTUGAL_ID,
    localidade_id: int = 0,
    estatuto: int | None = None,
    # ─── STEP 1 — Personal data (op=33 revalidação / op=12 primeira) ──────────
    # Revalidação: auto-derived from op=35; None = keep stored value.
    # 1ª Inscrição: id_type/id_number/id_expiry are required.
    id_type: int | None = None,
    id_number: str | None = None,
    id_expiry: str | None = None,
    telemovel: str | None = None,
    telefone: str | None = None,
    email: str | None = None,
    nome_pai: str | None = None,
    nome_mae: str | None = None,
    # ─── STEP 2 — Address (op=31 revalidação / op=20 primeira) ────────────────
    # Revalidação: pais locked to Portugal; address overrides None-default.
    # 1ª Inscrição: morada/cod_postal/distrito_id/concelho_id all required.
    morada: str | None = None,
    cod_postal: str | None = None,
    localidade_txt: str | None = None,
    distrito_id: int | None = None,
    concelho_id: int | None = None,
    # ─── STEP 3 — Sport-specific + consents (op=36 / op=27) ───────────────────
    taxa_id: int | None = None,
    exam_date: str | None = None,
    promote_to_tier_id: int | None = None,
    inline_subida: bool = False,
    guardian_name: str | None = None,
    guardian_relation: int | None = None,
    guardian_phone: str | None = None,
    guardian_email: str | None = None,
    consent_data: bool = True,
    consent_communications: bool = True,
    consent_marketing: bool = False,
  ) -> int:
    """
    Add a player to an open registration batch.

    Dispatches by ``batch.type_id``:
      * type 1 (1ª Inscrição) — walks the SAV2 wizard for a player not yet in
        the federation (duplicate check → age gate → create player → save
        address → estatuto/taxa/insurance cascades → commit op=27). Requires
        the full demographic surface (``name``, ``birth_date``, ``gender_id``,
        ``nif``, id-doc + address fields); ``license`` is ignored. Returns
        the new SAV ``userid``.
      * type 2 (Revalidação) — walks the multi-step edit wizard against an
        existing licence (load player → save step 1 → save step 2 →
        insurance cascade → pre-commit → commit op=36) and returns the
        player's internal SAV2 id.
      * type 4 (Subida)      — submits the standalone Subida flow (eligibility
        list → origin → seguro/companhia/taxa cascade → commit op=50) and
        returns the player's licence. Most kwargs are ignored on Subida
        except ``taxa_id``.

    Transferência (type 3) remains unsupported.

    Args:
        batch_id: Target open batch (must be in 'Em construção' state).
        license:  Licence number of the player to revalidate.

        Step 1 overrides (None = keep stored value from player record):
          id_type, id_number, id_expiry, telemovel, telefone, email,
          nome_pai, nome_mae.

        Step 2 overrides (None = keep stored value):
          morada, cod_postal, localidade_txt, distrito_id, concelho_id.

        Step 3:
          exam_date:           Required YYYY-MM-DD date.
                               (The medical exam itself is always assumed done.)
          promote_to_tier_id:  Numeric escalão ID for Subida; usually unset
                               (overrides the op=21 lookup when given).
          inline_subida:       Promote the player right away as part of this
                               1ª Inscrição / Revalidação (the inline rider, not
                               a standalone type-4 Subida batch). When True, the
                               target tier is fetched from SAV (op=21) and sent
                               as sub/escalaosubida_txt; raises SavConfigError if
                               SAV offers no option.
          guardian_*:          Required when the player is a minor; raises
                               SavConfigError otherwise.
          consent_*:           GDPR consents.

    Returns:
        Internal SAV2 user id of the added player.

    Raises:
        ValueError:        Missing/invalid exam_date, unknown batch, or
                           player not eligible for revalidation.
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
    if batch.type_id == _REGISTRATIONS_TYPE_SUBIDA:
      return self._add_player_to_subida_batch(batch, license, taxa_id=taxa_id)
    if batch.type_id == _REGISTRATIONS_TYPE_PRIMEIRA:
      missing = [
        label for label, value in [
          ("name", name), ("birth_date", birth_date), ("gender_id", gender_id),
          ("nif", nif), ("id_type", id_type), ("id_number", id_number),
          ("id_expiry", id_expiry), ("email", email),
          ("morada", morada), ("cod_postal", cod_postal),
          ("distrito_id", distrito_id), ("concelho_id", concelho_id),
        ]
        if value in (None, "")
      ]
      if missing:
        raise ValueError(
          f"1ª Inscrição (type-1) requires: {', '.join(missing)}. "
          f"Pass them as keyword arguments to add_player_to_registration_batch."
        )
      return self._add_player_to_primeira_batch(
        batch,
        name=name, birth_date=birth_date, gender_id=gender_id, nif=nif,
        id_type=id_type, id_number=id_number, id_expiry=id_expiry,
        email=email, telemovel=telemovel, telefone=telefone,
        nationality_id=nationality_id, naturalidade_id=naturalidade_id,
        nome_pai=nome_pai, nome_mae=nome_mae,
        country_id=country_id,
        morada=morada, cod_postal=cod_postal,
        distrito_id=distrito_id, concelho_id=concelho_id,
        localidade_id=localidade_id, localidade_txt=localidade_txt or "",
        exam_date=exam_date, taxa_id=taxa_id, estatuto=estatuto,
        promote_to_tier_id=promote_to_tier_id, inline_subida=inline_subida,
        guardian_name=guardian_name, guardian_relation=guardian_relation,
        guardian_phone=guardian_phone, guardian_email=guardian_email,
        consent_data=consent_data,
        consent_communications=consent_communications,
        consent_marketing=consent_marketing,
      )
    if batch.type_id != _REGISTRATIONS_TYPE_REVALIDACAO:
      raise NotImplementedError(
        f"Transferência (type_id=3) batches are not supported; "
        f"got type_id={batch.type_id} ({batch.type!r})."
      )

    eligible = self._list_revalidable_licenses(batch)
    if license not in eligible:
      enrolled = {
        item["license"] for item in self.list_player_registration_batch_items(batch.id)
      }
      if license in enrolled:
        return self._update_existing_player_in_batch(
          batch, license,
          id_type=id_type, id_number=id_number, id_expiry=id_expiry,
          telemovel=telemovel, telefone=telefone, email=email,
          nome_pai=nome_pai, nome_mae=nome_mae,
          morada=morada, cod_postal=cod_postal,
          localidade_txt=localidade_txt,
          distrito_id=distrito_id, concelho_id=concelho_id,
        )
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

    exam_date = _coerce_exam_date(exam_date)

    # ── Inline subida de escalão — fetch the player-specific target tier (op=21) ─
    # The subida tier is server-computed per player (not the batch tier).
    # When a mod4 is involved, its escalao_subida text resolves upstream to a
    # tier_id passed here as promote_to_tier_id; _pick_subida_tier enforces
    # that the form's stated target is one SAV will accept. With no caller
    # hint, single-option lists auto-pick and multi-option lists raise.
    sub_tier = (
      self._pick_subida_tier(internal_id, prefer_tier_id=promote_to_tier_id)
      if inline_subida else None
    )
    if inline_subida and sub_tier is None:
      raise SavConfigError(
        f"Inline subida requested (mod4 present) but SAV offers no subida tier "
        f"for licence {license} (op=21 returned only '- Não selecionado –')."
      )
    if sub_tier:
      logger.info(
        "Subida de escalão for licence %s → %s (id=%s)",
        license, sub_tier[1], sub_tier[0],
      )

    commit_body = {
      "guiaid": batch.id,
      "userid": internal_id,
      "transf": 0,
      "estatuto": str(step3_prefill.get("estatuto", "")),
      "exame": "1",
      "sub": str(sub_tier[0]) if sub_tier else "-1",
      "obs": "",
      "dataexame": exam_date,
      "escalaosubida_txt": sub_tier[1] if sub_tier else "- Não selecionado –",
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
    self._cache.record_license_batch(license, batch.id)
    return internal_id

  def _update_existing_player_in_batch(
    self,
    batch: PlayerRegistrationBatch,
    license: int,
    *,
    id_type: int | None,
    id_number: str | None,
    id_expiry: str | None,
    telemovel: str | None,
    telefone: str | None,
    email: str | None,
    nome_pai: str | None,
    nome_mae: str | None,
    morada: str | None,
    cod_postal: str | None,
    localidade_txt: str | None,
    distrito_id: int | None,
    concelho_id: int | None,
  ) -> int:
    """
    Patch an already-enrolled player's personal data and (optionally)
    address. Mirrors the wizard's edit flow: op=30 (load) → op=33 (step 1)
    → op=31 (step 2, only when an address field is overridden). Skips the
    insurance/taxa cascade and op=36 commit — those are creation-time and
    the existing inscricao already carries them.
    """
    record = self.load_existing_registration_record(batch.id, license)
    internal_id = int(record["id"])

    step1_send = self._build_step1_send(
      record,
      id_type=id_type, id_number=id_number, id_expiry=id_expiry,
      telemovel=telemovel, telefone=telefone, email=email,
      nome_pai=nome_pai, nome_mae=nome_mae,
    )
    step2_prefill = self._save_registration_step1(batch.id, internal_id, step1_send)

    address_fields = (morada, cod_postal, localidade_txt, distrito_id, concelho_id)
    if any(v is not None for v in address_fields):
      step2_send = self._build_step2_send(
        step2_prefill,
        morada=morada, cod_postal=cod_postal, localidade_txt=localidade_txt,
        distrito_id=distrito_id, concelho_id=concelho_id,
      )
      self._save_registration_step2(
        batch.type_id, batch.id, internal_id, license, step2_send,
      )

    logger.info(
      "Updated player license=%s (id=%s) in batch %s",
      license, internal_id, batch.id,
    )
    return internal_id

  def update_player_in_registration_batch(
    self,
    batch_id: int,
    license: int,
    *,
    id_type: int | None = None,
    id_number: str | None = None,
    id_expiry: str | None = None,
    telemovel: str | None = None,
    telefone: str | None = None,
    email: str | None = None,
    nome_pai: str | None = None,
    nome_mae: str | None = None,
    morada: str | None = None,
    cod_postal: str | None = None,
    localidade_txt: str | None = None,
    distrito_id: int | None = None,
    concelho_id: int | None = None,
  ) -> int:
    """
    Patch fields on a player already enrolled in an open Revalidação batch.

    Only step-1 (personal data) and step-2 (address) fields are supported;
    those persist via op=33/op=31 without re-firing the op=36 commit. Pass
    only the fields you want to change — the rest are loaded from the
    existing inscricao via op=30 and kept as-is.

    Guardian, taxa, insurance, and consent fields are commit-time only
    (op=36) and require a separate edit-flow trace before they can be
    safely patched on an existing enrolment.

    Args:
        batch_id: Open Revalidação batch holding the enrolment.
        license:  Licence of the already-enrolled player.

    Raises:
        ValueError:        Batch unknown, not open, wrong type, or licence
                           not currently enrolled in this batch.
        SavResponseError:  Server returned an error.
        SavConnectionError: Network errors.
    """
    if self.session is None:
      raise SavResponseError(
        "Must call login() before update_player_in_registration_batch()"
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
        "only 'Em construção' batches can be edited."
      )
    if batch.type_id != _REGISTRATIONS_TYPE_REVALIDACAO:
      raise NotImplementedError(
        f"Only Revalidação batches are supported (type_id=2); "
        f"got type_id={batch.type_id} ({batch.type!r})."
      )

    enrolled = {
      item["license"] for item in self.list_player_registration_batch_items(batch.id)
    }
    if license not in enrolled:
      raise ValueError(
        f"Licence {license} is not enrolled in batch {batch.id}; "
        f"use add_player_to_registration_batch() to add new players."
      )

    return self._update_existing_player_in_batch(
      batch, license,
      id_type=id_type, id_number=id_number, id_expiry=id_expiry,
      telemovel=telemovel, telefone=telefone, email=email,
      nome_pai=nome_pai, nome_mae=nome_mae,
      morada=morada, cod_postal=cod_postal,
      localidade_txt=localidade_txt,
      distrito_id=distrito_id, concelho_id=concelho_id,
    )

  def upload_player_registration_document(
    self,
    batch_id: int,
    license: int,
    file_path: Any,
    *,
    tipo_doc: int = _REGISTRATIONS_DOC_TIPO_MODELO_1,
  ) -> None:
    """
    Upload a document (PDF or JPG) attached to a player's registration.

    Mirrors the SAV2 upload modal flow: op=91 fetches the modal HTML — we
    parse the per-batch ``inscricao`` id from the embedded
    ``checkDoc(n, inscricao, licenca, guia, ...)`` onclick — then op=92
    POSTs the file with hardcoded ``n=1`` and multipart key ``file0``. The
    SAV2 web UI always uses those constants (op=92 takes exactly one file
    per request); the PHP handler reads ``$_FILES["file" . ($n - 1)]``, so
    any other combination causes ``Undefined array key`` errors.
    ``inscricao`` is the registration record id created by op=36, NOT the
    user_id from op=35; the only reliable way to obtain it is via op=91
    once the player has been added to the batch.

    Args:
        batch_id:    Target batch (guia).
        license:     Player licence.
        file_path:   Path to a ``.pdf`` or ``.jpg``/``.jpeg`` file.
        tipo_doc:    Document type id (default: 1 = Modelo 1).
                     Common values: 1 Modelo 1, 2 Exame Médico, 6 Modelo 4,
                     18 Doc. Identificação. The full list lives in the
                     upload modal's ``<select id="tipo1">``.

    Raises:
        SavResponseError:   not logged in, or server signals failure.
        SavConnectionError: network errors.
        FileNotFoundError:  the file doesn't exist.
        ValueError:         unsupported file type, or batch_id not found.
    """
    from pathlib import Path

    if self.session is None:
      raise SavResponseError(
        "Must call login() before upload_player_registration_document()"
      )

    path = Path(file_path)
    if not path.is_file():
      raise FileNotFoundError(f"File not found: {file_path}")

    suffix = path.suffix.lower()
    if suffix == ".pdf":
      content_type = "application/pdf"
    elif suffix in (".jpg", ".jpeg"):
      content_type = "image/jpeg"
    else:
      raise ValueError(
        f"Unsupported file type {suffix!r}; SAV2 accepts .pdf and .jpg only."
      )

    batch = next(
      (b for b in self.list_player_registration_batches() if b.id == batch_id),
      None,
    )
    if batch is None:
      raise ValueError(f"Batch id={batch_id} not found")

    # `inscricao` (the per-batch registration record id) is the only value
    # we still need from op=91. We deliberately ignore the modal's `num`
    # field: the SAV2 web UI hardcodes `n=1` and multipart key `file0` for
    # every upload (op=92 accepts exactly one file per request — captured
    # from a live browser session). Passing `num` here causes the PHP
    # handler to look up `$_FILES["file" . ($n - 1)]` against the wrong
    # slot, which manifests as `Undefined array key "fileN"` on the second
    # and subsequent uploads to the same player.
    _, inscricao, _ = self._fetch_registration_documents(batch, license)

    url = (
      f"{self._url(_REGISTRATIONS_PATH)}?"
      f"op={_REGISTRATIONS_DOC_UPLOAD_OP}"
      f"&inscricao={inscricao}&n=1&licenca={license}"
      f"&tipo_doc={tipo_doc}&agente={_REGISTRATIONS_AGENTE_PLAYER}"
    )
    with path.open("rb") as fh:
      files = {"file0": (path.name, fh, content_type)}
      try:
        resp = self._http.post(
          url, files=files, timeout=self._timeout, headers={"Accept": "*/*"},
        )
        resp.raise_for_status()
      except requests.exceptions.RequestException as exc:
        raise SavConnectionError(f"Could not upload document: {exc}") from exc
    try:
      data = json.loads(resp.text)
    except ValueError as exc:
      raise SavResponseError(
        f"Could not parse upload response: {resp.text[:200]!r}"
      ) from exc
    if data.get("val") != 1:
      raise SavResponseError(
        f"Document upload failed: {data!r}"
      )
    logger.info(
      "Uploaded %s (tipo_doc=%s) for license=%s in batch %s",
      path.name, tipo_doc, license, batch.id,
    )

  def delete_player_registration_document(self, doc_id: int) -> None:
    """
    Delete a previously uploaded registration document by its galeria id.

    The galeria id is the first argument of the ``deleteDoc(...)`` onclick
    handler in the upload modal — exposed via
    ``list_player_registration_documents()``.

    Args:
        doc_id: Galeria id of the document to delete.

    Raises:
        SavResponseError:   not logged in, or server signals failure.
        SavConnectionError: network errors.
    """
    if self.session is None:
      raise SavResponseError(
        "Must call login() before delete_player_registration_document()"
      )
    try:
      resp = self._http.get(
        self._url(_REGISTRATIONS_PATH),
        params={
          "op": _REGISTRATIONS_DOC_DELETE_OP,
          "galeria": doc_id,
          "agente": _REGISTRATIONS_AGENTE_PLAYER,
        },
        timeout=self._timeout,
      )
      resp.raise_for_status()
    except requests.exceptions.RequestException as exc:
      raise SavConnectionError(
        f"Could not delete document {doc_id}: {exc}"
      ) from exc
    logger.info("Deleted registration document galeria=%s", doc_id)

  def list_player_registration_documents(
    self, batch_id: int, license: int,
  ) -> list[dict[str, int]]:
    """
    List the documents currently uploaded for a player in a batch.

    Returns one ``{"doc_id": int, "tipo_doc": int}`` entry per document, in
    the order the SAV2 server lists them. ``doc_id`` is the galeria id used
    by ``delete_player_registration_document()``; ``tipo_doc`` matches the
    upload modal's type select (1 = Modelo 1, 2 = Exame Médico, ...).

    Raises:
        SavResponseError:   not logged in.
        SavConnectionError: network errors.
        ValueError:         batch_id not found.
    """
    if self.session is None:
      raise SavResponseError(
        "Must call login() before list_player_registration_documents()"
      )
    batch = next(
      (b for b in self.list_player_registration_batches() if b.id == batch_id),
      None,
    )
    if batch is None:
      raise ValueError(f"Batch id={batch_id} not found")
    _, _, docs = self._fetch_registration_documents(batch, license)
    return docs

  def replace_player_registration_document(
    self,
    batch_id: int,
    license: int,
    file_path: Any,
    *,
    tipo_doc: int = _REGISTRATIONS_DOC_TIPO_MODELO_1,
  ) -> None:
    """
    Replace any existing documents of ``tipo_doc`` for this player+batch with
    ``file_path``: delete each match via op=94, then upload the new file.

    Idempotent on the upload side — when no existing doc of ``tipo_doc`` is
    found, behaves like a plain upload. Args mirror
    ``upload_player_registration_document()``.
    """
    if self.session is None:
      raise SavResponseError(
        "Must call login() before replace_player_registration_document()"
      )

    batch = next(
      (b for b in self.list_player_registration_batches() if b.id == batch_id),
      None,
    )
    if batch is None:
      raise ValueError(f"Batch id={batch_id} not found")

    _, _, docs = self._fetch_registration_documents(batch, license)
    for doc in docs:
      if doc["tipo_doc"] == tipo_doc:
        self.delete_player_registration_document(doc["doc_id"])

    self.upload_player_registration_document(
      batch_id, license, file_path, tipo_doc=tipo_doc,
    )

  def _fetch_registration_documents(
    self, batch: PlayerRegistrationBatch, license: int,
  ) -> tuple[int, int, list[dict[str, int]]]:
    """
    Op=91 — fetch a player's existing registration documents.

    Returns ``(next_slot, inscricao, docs)`` where:
      - ``next_slot`` is the ``num`` field (1-indexed slot for the next
        upload via op=92).
      - ``inscricao`` is the per-batch registration record id, parsed from
        the embedded ``checkDoc(n, inscricao, licenca, guia, ...)`` onclick
        in the HTML (this is NOT the user_id from op=35; it's created when
        the player is added to the batch via op=36).
      - ``docs`` is a list of ``{"doc_id": int, "tipo_doc": int}`` parsed
        from each row's ``deleteDoc(doc_id, licenca, guia, tipo_doc, ...)``
        onclick handler in the embedded HTML.
    """
    import re
    try:
      resp = self._http.get(
        self._url(_REGISTRATIONS_PATH),
        params={
          "op": _REGISTRATIONS_DOC_LIST_OP,
          "guia": batch.id,
          "licenca": license,
          "agente": _REGISTRATIONS_AGENTE_PLAYER,
          "tipo": batch.type_id,
        },
        timeout=self._timeout,
      )
      resp.raise_for_status()
      data = json.loads(resp.text)
    except (requests.exceptions.RequestException, ValueError) as exc:
      raise SavConnectionError(
        f"Could not list registration documents for license {license}: {exc}"
      ) from exc

    body = data.get("body") or ""
    next_slot = int(data.get("num", 1))
    m_check = re.search(r"checkDoc\(\s*\d+\s*,\s*(\d+)", body)
    if not m_check:
      raise SavResponseError(
        f"Could not find inscricao id in op=91 response for license "
        f"{license}, batch {batch.id}: {body[:200]!r}"
      )
    inscricao = int(m_check.group(1))
    docs: list[dict[str, int]] = []
    for m in re.finditer(
      r"deleteDoc\((\d+)\s*,\s*\d+\s*,\s*\d+\s*,\s*(\d+)",
      body,
    ):
      docs.append({"doc_id": int(m.group(1)), "tipo_doc": int(m.group(2))})
    return next_slot, inscricao, docs

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

  def _list_subida_licenses(
    self, batch: PlayerRegistrationBatch,
  ) -> set[int]:
    """
    Op=48 — list licences eligible for Subida (type-4) in a batch.

    Body params mirror the SAV2 web flow: ``perfil``, ``guia``, ``user``,
    ``organizacao``. The response is a JSON wrapper with a ``body`` field
    carrying the form-prefill HTML (origin/destination fields + initial
    novataxa list) and a ``footer``; only the player ``<option value='N'>``
    list inside ``body`` is consumed here.
    """
    import re
    try:
      resp = self._http.post(
        self._url(_REGISTRATIONS_PATH),
        params={"op": _REGISTRATIONS_LIST_SUBIDA_OP},
        data={
          "perfil": self.session.get("perfil", 0),
          "guia": batch.id,
          "user": self.session.get("user", ""),
          "organizacao": self.session.get("organizacao", 0),
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=self._timeout,
      )
      resp.raise_for_status()
    except requests.exceptions.RequestException as exc:
      raise SavConnectionError(
        f"Could not list subida players for batch {batch.id}: {exc}"
      ) from exc
    body = resp.text
    try:
      body = json.loads(resp.text).get("body", body)
    except ValueError:
      pass
    return {
      int(m) for m in re.findall(r"<option value='(\d+)'", body) if int(m) > 0
    }

  def _load_subida_origin(self, license: int) -> dict[str, Any]:
    """Op=49 — load a Subida candidate's origin (escalão/taxa/seguro/etc).

    Returns the raw JSON; the only commit-relevant field is ``estatuto``,
    but the rest mirrors the disabled origin inputs on the SAV2 modal and
    is kept for debug parity.
    """
    try:
      resp = self._http.post(
        self._url(_REGISTRATIONS_PATH),
        params={"op": _REGISTRATIONS_LOAD_SUBIDA_ORIGIN_OP},
        data={"atleta": license},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=self._timeout,
      )
      resp.raise_for_status()
    except requests.exceptions.RequestException as exc:
      raise SavConnectionError(
        f"Could not load subida origin for licence {license}: {exc}"
      ) from exc
    try:
      return json.loads(resp.text)
    except ValueError as exc:
      raise SavResponseError(
        f"Could not parse subida origin: {resp.text[:200]!r}"
      ) from exc

  def _resolve_subida_taxa_id(
    self, batch: PlayerRegistrationBatch, license: int,
  ) -> int:
    """Op=134 — refine the novataxa dropdown for this player+batch; auto-pick
    when exactly one real option is offered (multi-option → raise so the
    caller passes ``taxa_id``).

    Distinct from op=26 (Revalidação) which keys off ``estatuto``+``user``;
    op=134 keys off the licence directly and is Subida-only.
    """
    import re
    try:
      r = self._http.post(
        self._url(_REGISTRATIONS_PATH),
        params={"op": _REGISTRATIONS_LOAD_SUBIDA_TAXA_OP},
        data={"n_licenca": license, "guiaid": batch.id},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=self._timeout,
      )
      r.raise_for_status()
    except requests.exceptions.RequestException as exc:
      raise SavConnectionError(
        f"Could not load subida taxa options: {exc}"
      ) from exc
    try:
      msg = json.loads(r.text).get("taxas", "")
    except ValueError as exc:
      raise SavResponseError(
        f"Could not parse subida taxa response: {r.text[:200]!r}"
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
        f"No subida taxa options returned for batch {batch.id}, "
        f"licence {license}: {msg!r}"
      )
    listing = ", ".join(f"{i}={n!r}" for i, n in sorted(options.items()))
    raise SavConfigError(
      f"Multiple subida taxa options for batch {batch.id}, licence {license}: "
      f"{listing}. Pass taxa_id= to disambiguate."
    )

  def _resolve_subida_insurance_cascade(
    self, batch: PlayerRegistrationBatch, license: int,
  ) -> tuple[int, int]:
    """Subida insurance cascade: op=128 (novoseguro) → op=126 (novacomp) →
    op=24 (apólice, display-only).

    Returns ``(seguro_id, companhia_id)``. Op=50 only sends ``companhia``,
    but op=126 needs the picked seguro to enumerate companhia options, so
    we run both even though seguro_id is dropped at commit time.
    """
    import re
    # op=128: novoseguro options (auto-pick when single)
    try:
      r = self._http.post(
        self._url(_REGISTRATIONS_PATH),
        params={"op": _REGISTRATIONS_LOAD_SUBIDA_SEGURO_OP},
        data={"atleta": license, "guiaid": batch.id, "seguro": 0},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=self._timeout,
      )
      r.raise_for_status()
    except requests.exceptions.RequestException as exc:
      raise SavConnectionError(
        f"Subida insurance op=128 failed: {exc}"
      ) from exc
    try:
      msg = json.loads(r.text).get("msg", "")
    except ValueError as exc:
      raise SavResponseError(
        f"Could not parse subida seguro response: {r.text[:200]!r}"
      ) from exc
    seguros = {
      int(val): label.strip()
      for val, label in re.findall(
        r"<option value='(-?\d+)'[^>]*>\s*([^<]+?)\s*<", msg
      )
      if int(val) > 0
    }
    if not seguros:
      raise SavResponseError(
        f"No subida seguro options for batch {batch.id}, licence {license}: {msg!r}"
      )
    if len(seguros) > 1:
      listing = ", ".join(f"{i}={n!r}" for i, n in sorted(seguros.items()))
      raise SavConfigError(
        f"Multiple subida seguro options for batch {batch.id}, licence "
        f"{license}: {listing}."
      )
    seguro_id = next(iter(seguros))

    # op=126: novacomp options for the picked seguro (auto-pick when single)
    try:
      r = self._http.post(
        self._url(_REGISTRATIONS_PATH),
        params={"op": _REGISTRATIONS_LOAD_SUBIDA_COMPANHIA_OP},
        data={"seguro": seguro_id, "guia": batch.id},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=self._timeout,
      )
      r.raise_for_status()
    except requests.exceptions.RequestException as exc:
      raise SavConnectionError(
        f"Subida insurance op=126 failed: {exc}"
      ) from exc
    try:
      msg = json.loads(r.text).get("msg", "")
    except ValueError as exc:
      raise SavResponseError(
        f"Could not parse subida companhia response: {r.text[:200]!r}"
      ) from exc
    companhias = {
      int(val): label.strip()
      for val, label in re.findall(
        r"<option value='(-?\d+)'[^>]*>\s*([^<]+?)\s*<", msg
      )
      if int(val) > 0
    }
    if not companhias:
      raise SavResponseError(
        f"No subida companhia options for seguro {seguro_id}, batch {batch.id}: "
        f"{msg!r}"
      )
    if len(companhias) > 1:
      listing = ", ".join(f"{i}={n!r}" for i, n in sorted(companhias.items()))
      raise SavConfigError(
        f"Multiple subida companhia options for seguro {seguro_id}: {listing}."
      )
    companhia_id = next(iter(companhias))

    # op=24: apólice (display-only — fire for parity, ignore the result)
    try:
      self._http.get(
        self._url(_REGISTRATIONS_PATH),
        params={"op": _REGISTRATIONS_LOAD_APOLICE_OP, "companhia": companhia_id},
        timeout=self._timeout,
      )
    except requests.exceptions.RequestException:
      logger.debug("op=24 subida apolice fetch failed — non-fatal", exc_info=True)

    return seguro_id, companhia_id

  def _add_player_to_subida_batch(
    self,
    batch: PlayerRegistrationBatch,
    license: int,
    *,
    taxa_id: int | None,
  ) -> int:
    """Submit a standalone Subida (type-4) enrollment.

    The Subida flow is much shorter than Revalidação: the player already
    exists in SAV, so there's no personal-data / address wizard — just
    eligibility check → cascades → op=50 commit. Returns the licence
    (SAV2 surfaces no separate internal id for Subida).
    """
    eligible = self._list_subida_licenses(batch)
    if license not in eligible:
      enrolled = {
        item["license"] for item in self.list_player_registration_batch_items(batch.id)
      }
      if license in enrolled:
        logger.info(
          "Licence %s already enrolled in subida batch %s — skipping commit.",
          license, batch.id,
        )
        return license
      raise ValueError(
        f"Licence {license} is not eligible for subida in batch {batch.id} "
        f"({batch.tier} {batch.gender}). The server's eligible list has "
        f"{len(eligible)} player(s); pass one of those licences."
      )

    # op=49 origin fetch — fired for browser-flow parity; estatuto and the
    # origin display fields aren't part of the op=50 commit body.
    self._load_subida_origin(license)

    _seguro_id, companhia_id = self._resolve_subida_insurance_cascade(batch, license)
    if taxa_id is None:
      taxa_id = self._resolve_subida_taxa_id(batch, license)

    try:
      resp = self._http.post(
        self._url(_REGISTRATIONS_PATH),
        params={"op": _REGISTRATIONS_SUBIDA_COMMIT_OP},
        data={
          "atleta": license,
          "guia": batch.id,
          "taxa": taxa_id,
          "companhia": companhia_id,
          "epoca": batch.season,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=self._timeout,
      )
      resp.raise_for_status()
    except requests.exceptions.RequestException as exc:
      raise SavConnectionError(f"Subida commit failed: {exc}") from exc
    # The SAV2 web UI doesn't inspect op=50's body; it may return JSON
    # ({"val":1,...}) or a bare success string. Treat HTTP 2xx as success
    # unless the response is a JSON dict with an explicit val!=1.
    try:
      data = json.loads(resp.text)
    except ValueError:
      data = None
    if isinstance(data, dict) and "val" in data and str(data["val"]) != "1":
      raise SavResponseError(
        f"Subida commit failed: {data.get('msg') or data!r}"
      )
    logger.info(
      "Added licence %s to subida batch %s (tier=%s, taxa=%s, companhia=%s).",
      license, batch.id, batch.tier, taxa_id, companhia_id,
    )
    self._cache.record_license_batch(license, batch.id)
    return license

  def list_player_registration_batch_items(
    self, batch_id: int,
  ) -> list[dict[str, Any]]:
    """
    Op=10 — list players currently enrolled in a registration batch.

    The SAV2 batch detail page renders one row per item; each row carries
    ``editJogador(license, batch, type)`` and similar onclick handlers from
    which we recover the licence. Returns a list of
    ``{"license": int, "name": str}`` in the order the server lists them.

    Used to detect "already enrolled" players so a re-submit can patch the
    existing item via op=30 + op=33/op=31 instead of erroring on the
    revalidable-list gate (op=139 excludes anyone already in any open batch).

    Raises:
        SavResponseError:   not logged in, or response cannot be parsed.
        SavConnectionError: network errors.
        ValueError:         batch_id not found.
    """
    if self.session is None:
      raise SavResponseError(
        "Must call login() before list_player_registration_batch_items()"
      )

    batch = next(
      (b for b in self.list_player_registration_batches() if b.id == batch_id),
      None,
    )
    if batch is None:
      raise ValueError(f"Batch id={batch_id} not found")
    import re

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
        f"Could not list items for batch {batch_id}: {exc}"
      ) from exc

    body = resp.text
    try:
      body = json.loads(body).get("msg", body)
    except ValueError:
      pass

    from bs4 import BeautifulSoup
    soup = BeautifulSoup(body, "html.parser")
    items: list[dict[str, Any]] = []
    seen: set[int] = set()
    for btn in soup.find_all(attrs={"onclick": re.compile(r"editJogador\(")}):
      m = re.search(r"editJogador\((\d+)\s*,\s*(\d+)", btn.get("onclick", ""))
      if not m:
        continue
      license = int(m.group(1))
      if license in seen:
        continue
      seen.add(license)
      row = btn.find_parent("tr")
      cells = [c.get_text(strip=True) for c in row.find_all("td")] if row else []
      name = cells[2] if len(cells) > 2 else ""
      items.append({"license": license, "name": name})
    return items

  def load_existing_registration_record(
    self, batch_id: int, license: int,
  ) -> dict[str, Any]:
    """Op=30 — load an already-enrolled item's record for the edit wizard.

    Same response shape as op=35 (id, nome, nasc/datenasc, nif, email, …)
    plus an ``existe: 1`` flag. Required entry point for the update path:
    op=33/op=31 then key by (internal_id, guia) and patch the existing
    inscricao without firing op=36 again.
    """
    try:
      resp = self._http.get(
        self._url(_REGISTRATIONS_PATH),
        params={
          "op": _REGISTRATIONS_LOAD_EXISTING_PLAYER_OP,
          "id": license,
          "guia": batch_id,
        },
        timeout=self._timeout,
      )
      resp.raise_for_status()
    except requests.exceptions.RequestException as exc:
      raise SavConnectionError(
        f"Could not load existing registration record: {exc}"
      ) from exc
    try:
      data = json.loads(resp.text)
    except ValueError as exc:
      raise SavResponseError(
        f"Could not parse existing record: {resp.text[:200]!r}"
      ) from exc
    if not isinstance(data, dict):
      # Valid JSON but the wrong top-level shape (e.g. `null`, `[]`, a bare
      # string or number). Raising plainly here keeps callers from
      # crashing with `AttributeError` on `.get(...)` further down.
      raise SavResponseError(
        f"Unexpected existing-record payload type ({type(data).__name__}) "
        f"for licence {license!r} in batch {batch_id}: {data!r}"
      )
    if not data.get("id"):
      # No id field — could be a clean "not found" or a server-side error
      # payload. We need a *positive* not-found signal to tell them apart;
      # otherwise a 500-ish payload like {"error": "..."} would silently
      # look identical to "this player isn't in this batch".
      #
      #   - Empty body (``{}``)                  → clean "not found"
      #   - ``existe`` explicitly falsy (0/"0")  → clean "not found"
      #   - Anything else                        → unexpected shape, raise
      #     the broader SavResponseError so callers don't mask it.
      not_found = not data or str(data.get("existe", "")).strip() in ("0", "false", "False")
      if not_found:
        raise SavRecordNotFoundError(
          f"Existing record for licence {license!r} in batch {batch_id} not "
          f"found: {data!r}"
        )
      raise SavResponseError(
        f"Unexpected existing-record response for licence {license!r} in "
        f"batch {batch_id}: {data!r}"
      )
    # op=30 uses different keys than op=35 ("datenasc" vs "nasc",
    # "nacionalidade" vs "nacional"); normalise so the shared
    # _build_step1_send helper can treat both records identically.
    if "datenasc" in data and "nasc" not in data:
      data["nasc"] = data["datenasc"]
    if "nacionalidade" in data and "nacional" not in data:
      data["nacional"] = data["nacionalidade"]
    return data

  def list_concelhos(self, distrito_id: int) -> dict[int, str]:
    """Op=18 — concelhos under a given distrito (id → name), cached 7 days.

    The SAV2 wizard renders concelho as a dropdown that's populated on
    distrito change; this hits the same endpoint the browser does. Returns
    an empty dict for distrito_id=0 (no selection).
    """
    if not distrito_id:
      return {}
    return self._cache.get_concelhos(self._fetch_concelhos, distrito_id=distrito_id)

  def _fetch_concelhos(self, distrito_id: int) -> dict[int, str]:
    try:
      resp = self._http.get(
        self._url(_REGISTRATIONS_PATH),
        params={"op": _REGISTRATIONS_LIST_CONCELHOS_OP, "dist": distrito_id},
        timeout=self._timeout,
      )
      resp.raise_for_status()
    except requests.exceptions.RequestException as exc:
      raise SavConnectionError(f"Could not load concelhos: {exc}") from exc

    from bs4 import BeautifulSoup
    soup = BeautifulSoup(resp.text, "html.parser")
    out: dict[int, str] = {}
    for opt in soup.find_all("option"):
      raw = (opt.get("value") or "").strip()
      if not raw or raw == "0":
        continue
      try:
        out[int(raw)] = opt.get_text(strip=True)
      except ValueError:
        continue
    return out

  def load_player_profile(
    self, license: int, *, club_id: int | None = None,
  ) -> dict[str, Any]:
    """Read-only player profile by license, suitable for OCR reconciliation.

    Single source: op=2 (jogadoresdb.php) — the player form view, which
    carries everything we reconcile (personal data + the address block).
    No wizard saves, no validation gates; server-side validation surfaces
    only at real submit, which is where it belongs.

    license → internal-id is bridged via search_players, with two perf
    helpers:
      - hits the SQLite-persisted license_to_id cache when this licence
        has been seen before (in this run or any prior CLI invocation);
      - club_id, when given, scopes the bridge search instead of the slow
        federation-wide path.

    Field IDs in the rendered op=2 HTML are translated to the canonical
    keys used elsewhere (datenasc → nasc, numid → numi, telem → tele,
    cod2 → codpostal, localidadestring → localidade_txt, tipoi → tipo,
    …) so the result drops cleanly into reconcile_fpb_mod1 alongside the
    op=35 record.
    """
    if self.session is None:
      raise SavResponseError("Must call login() before load_player_profile()")

    player_id = self._cache.get_player_id(int(license))
    if player_id is None:
      results = self.search_players(license=str(license), club=club_id or 0)
      if not results:
        raise SavResponseError(f"No player with license {license}")
      player_id = results[0].id

    payload = {
      "user_id":     player_id,
      "user":        self.session.get("user", ""),
      "perfil":      self.session.get("perfil", 0),
      "organizacao": self.session.get("organizacao", 0),
    }
    text = self._post_form(_PLAYER_DETAIL_PATH, payload, params={"op": _PLAYER_DETAIL_OP})
    try:
      raw = json.loads(text)
    except ValueError as exc:
      raise SavResponseError(
        f"Player profile response was not valid JSON: {text[:200]!r}"
      ) from exc
    if "msg" not in raw:
      raise SavResponseError(
        f"Player profile response missing 'msg': keys={list(raw.keys())}"
      )

    from bs4 import BeautifulSoup
    soup = BeautifulSoup(raw["msg"], "html.parser")

    def text_value(elem_id: str) -> str:
      el = soup.find(id=elem_id)
      return (el.get("value", "") or "").strip() if el else ""

    def select_value(elem_id: str) -> str:
      sel = soup.find("select", id=elem_id)
      if not sel:
        return ""
      opt = sel.find("option", selected=True)
      return ((opt.get("value") or "").strip()) if opt else ""

    profile = {
      "nome":           raw.get("nome") or text_value("nome"),
      "nasc":           text_value("datenasc"),
      "tipo":           select_value("tipoi"),
      "numi":           text_value("numid"),
      "dataval":        text_value("dateval"),
      "nif":            text_value("nif"),
      "tele":           text_value("telem"),
      "telef":          text_value("telefo"),
      "email":          text_value("email"),
      "nacional":       select_value("nacionalidade"),
      "naturalidade":   select_value("paisNascimento"),
      "morada":         text_value("morada"),
      "codpostal":      text_value("cod2"),
      "localidade_txt": text_value("localidadestring"),
      "distrito":       select_value("distrito"),
      "concelho":       select_value("concelho"),
    }
    # Empty values dropped so they don't shadow op=35's data on merge.
    return {k: v for k, v in profile.items() if v}

  def _load_player_record(self, batch_id: int, license: int) -> dict[str, Any]:
    """Op=35 — fetch a player's stored demographics for prefill."""
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
      data = json.loads(resp.text)
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
      return json.loads(resp.text)
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
      return json.loads(resp.text)
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
      seguro_id = int(json.loads(r.text)["companhia"])
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
      companhia_id = int(json.loads(r.text)["companhia"])
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
    try:
      msg = json.loads(r.text).get("msg", "")
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
    try:
      resp = self._http.post(
        self._url(_REGISTRATIONS_PATH),
        params={"op": _REGISTRATIONS_COMMIT_OP},
        data=json.dumps(body, ensure_ascii=False),
        headers={"Content-Type": "text/plain;charset=UTF-8"},
        timeout=self._timeout,
      )
      resp.raise_for_status()
    except requests.exceptions.RequestException as exc:
      raise SavConnectionError(f"Could not commit registration: {exc}") from exc
    try:
      return json.loads(resp.text)
    except ValueError as exc:
      raise SavResponseError(
        f"Could not parse commit response: {resp.text[:200]!r}"
      ) from exc

  # ------------------------------------------------------------------
  # 1ª Inscrição (type-1) wizard helpers
  # ------------------------------------------------------------------
  #
  # Unlike Revalidação, the player doesn't exist in SAV yet — op=12 creates
  # the record and returns the userid. The wizard then mirrors the
  # Revalidação shape (step-2 → estatuto → taxa → insurance → commit) but
  # with renamed fields and a different commit op (27, not 36).

  def _check_primeira_player_duplicate(
    self, *, gender_id: int, birth_date: str, id_number: str,
  ) -> dict[str, Any]:
    """Op=11 — pre-create duplicate check on (genero, datanasc, n_identificacao).

    Sent form-encoded with the JSON URL-encoded under ``info=`` (the modal
    re-fires it on "Seguinte"; the typeahead variant uses text/plain JSON
    but the form-encoded shape is the one the server treats as authoritative
    so we mirror that here).

    Returns the parsed JSON. ``existe:0`` is the happy path — anything else
    means a player with the same id already exists in SAV, in which case the
    caller must surface the duplicate rather than silently re-create.
    """
    info = json.dumps({
      "num": 3, "tipo_guia": _REGISTRATIONS_TYPE_PRIMEIRA,
      "input0": str(gender_id), "bd0": "genero_id",
      "input1": birth_date,     "bd1": "data_nascimento",
      "input2": str(id_number), "bd2": "n_identificacao",
    }, ensure_ascii=False)
    try:
      r = self._http.post(
        self._url(_REGISTRATIONS_PATH),
        params={"op": _REGISTRATIONS_PRIMEIRA_DUPLICATE_OP},
        data={"info": info},
        headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
        timeout=self._timeout,
      )
      r.raise_for_status()
      return json.loads(r.text)
    except (requests.exceptions.RequestException, ValueError) as exc:
      raise SavConnectionError(f"Duplicate-check op=11 failed: {exc}") from exc

  def _check_primeira_id_doc(self, id_number: str) -> None:
    """Op=163 — fire-and-forget id-doc uniqueness probe; empty body on success."""
    try:
      self._http.post(
        self._url(_REGISTRATIONS_PATH),
        params={"op": _REGISTRATIONS_PRIMEIRA_ID_DOC_CHECK_OP},
        data={"numid": id_number, "userid": 0},
        headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
        timeout=self._timeout,
      )
    except requests.exceptions.RequestException:
      logger.debug("op=163 id-doc check failed — non-fatal", exc_info=True)

  def _check_primeira_birthdate_fits_tier(
    self, batch: PlayerRegistrationBatch, birth_date: str,
  ) -> None:
    """Op=14 — hard gate: birthdate must fall inside the batch tier's age window.

    Response shape: ``{"de", "a", "es", "datanas", "val"}`` — ``val:1`` is OK,
    anything else means the player is too young / too old for the tier and
    the wizard refuses to create them. Raises ValueError with the window so
    the caller can route to a different batch.
    """
    try:
      r = self._http.get(
        self._url(_REGISTRATIONS_PATH),
        params={
          "op": _REGISTRATIONS_PRIMEIRA_AGE_GATE_OP,
          "datanasc": birth_date,
          "guia": batch.id,
        },
        timeout=self._timeout,
      )
      r.raise_for_status()
      data = json.loads(r.text)
    except (requests.exceptions.RequestException, ValueError) as exc:
      raise SavConnectionError(f"Age-gate op=14 failed: {exc}") from exc
    if int(data.get("val", 0)) != 1:
      raise ValueError(
        f"Birthdate {birth_date} does not fit tier {batch.tier!r} "
        f"(server window {data.get('de')}..{data.get('a')}). "
        f"Pick a batch with an age range that includes the player."
      )

  def _primeira_batch_context_refresh(self, batch_id: int) -> None:
    """Op=161 — fire-and-forget batch-header context refresh the modal does.

    Fired between step transitions for parity with the browser flow; the
    response (escalao/entidade/epoca/genero/descri_escalao) is purely for
    the UI header and not consumed by the wizard logic.
    """
    try:
      self._http.post(
        self._url(_REGISTRATIONS_PATH),
        params={"op": _REGISTRATIONS_PRIMEIRA_BATCH_CTX_OP},
        data={"guiaid": batch_id},
        headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
        timeout=self._timeout,
      )
    except requests.exceptions.RequestException:
      logger.debug("op=161 batch-context refresh failed — non-fatal", exc_info=True)

  @staticmethod
  def _quote_phone(value: str | None) -> str:
    """Mirror SAV2's quirk of wrapping phone numbers in single quotes in the wire payload."""
    return f"'{value}'" if value else ""

  def _create_primeira_player(
    self,
    *,
    batch: PlayerRegistrationBatch,
    name: str,
    birth_date: str,
    gender_id: int,
    email: str,
    telemovel: str | None,
    telefone: str | None,
    nif: str,
    id_type: int,
    id_number: str,
    id_expiry: str,
    nationality_id: int,
    naturalidade_id: int,
    nome_pai: str | None,
    nome_mae: str | None,
  ) -> int:
    """Op=12 — create a new player record. Returns the new SAV userid.

    Body is text/plain raw JSON (not form-encoded). Phone fields are
    wrapped in literal single quotes; ``hab`` is the literal string
    ``"NULL"`` when empty; ``profissao`` and ``estadoc`` default to 0
    (the UI exposes them as optional). ``role:1`` flags the record as an
    atleta — coach/etc. profiles would use different roles.
    """
    body = {
      "guiaid": batch.id,
      "nome": name,
      "datenasc": birth_date,
      "email": email or "",
      "genero": str(gender_id),
      "telem": self._quote_phone(telemovel),
      "telefo": self._quote_phone(telefone),
      "profissao": 0,
      "nif": nif,
      "tipoi": str(id_type),
      "numid": str(id_number),
      "dataval": id_expiry,
      "estadoc": "0",
      "hab": "NULL",
      "nacionalidade": str(nationality_id),
      "naturalidade": str(naturalidade_id),
      "mae": nome_mae or "",
      "pai": nome_pai or "",
      "role": 1,
    }
    try:
      r = self._http.post(
        self._url(_REGISTRATIONS_PATH),
        params={"op": _REGISTRATIONS_PRIMEIRA_CREATE_OP},
        data=json.dumps(body, ensure_ascii=False),
        headers={"Content-Type": "text/plain;charset=UTF-8"},
        timeout=self._timeout,
      )
      r.raise_for_status()
      data = json.loads(r.text)
    except (requests.exceptions.RequestException, ValueError) as exc:
      raise SavConnectionError(f"Create-player op=12 failed: {exc}") from exc
    if int(data.get("val", 0)) != 1 or not data.get("userid"):
      raise SavResponseError(
        f"Create-player op=12 rejected: {data.get('msg') or data!r}"
      )
    return int(data["userid"])

  def _save_primeira_step2(
    self,
    *,
    batch: PlayerRegistrationBatch,
    userid: int,
    country_id: int,
    distrito_id: int,
    concelho_id: int,
    localidade_id: int,
    localidade_txt: str,
    morada: str,
    cod_postal: str,
  ) -> dict[str, Any]:
    """Op=20 — save the address step; response carries the menor_idade flag.

    Distinct from Revalidação's op=31:
      * text/plain JSON body (vs multipart form).
      * Renamed keys (``codpostal`` not ``cod_postal``; ``locastring`` is the
        free-text city; ``pais`` is mandatory rather than implicit Portugal).
      * Response carries ``clube``/``clube_id``/``nome``/``flagEstadaPerm``
        alongside ``menor_idade`` — only the latter is consumed by the
        commit logic, but we surface the full dict so callers can log it.
    """
    body = {
      "guiaid": batch.id,
      "userid": userid,
      "pais": str(country_id),
      "distrito": str(distrito_id),
      "concelho": str(concelho_id),
      "localidade": str(localidade_id) if localidade_id else "",
      "morada": morada,
      "codpostal": cod_postal,
      "locastring": localidade_txt or "",
    }
    try:
      r = self._http.post(
        self._url(_REGISTRATIONS_PATH),
        params={"op": _REGISTRATIONS_PRIMEIRA_STEP2_OP},
        data=json.dumps(body, ensure_ascii=False),
        headers={"Content-Type": "text/plain;charset=UTF-8"},
        timeout=self._timeout,
      )
      r.raise_for_status()
      data = json.loads(r.text)
    except (requests.exceptions.RequestException, ValueError) as exc:
      raise SavConnectionError(f"Step-2 op=20 failed: {exc}") from exc
    if int(data.get("val", 0)) != 1:
      raise SavResponseError(
        f"Step-2 op=20 rejected: {data.get('msg') or data!r}"
      )
    return data

  def _load_primeira_estatuto(
    self, batch: PlayerRegistrationBatch, userid: int,
  ) -> int:
    """Op=151 — pick the player's estatuto from the dropdown SAV exposes.

    Revalidação reads estatuto off the stored player record; for type-1
    there's nothing stored yet so SAV serves a dropdown. The UI locks
    Portuguese FBP players to a single option, so we auto-pick when exactly
    one real (>0) option is returned. Multi-option means a non-PT player
    (Comunitário / Não Comunitário / Equiparado) — surface a config error
    asking the caller to pass an explicit choice.
    """
    import re

    try:
      r = self._http.post(
        self._url(_REGISTRATIONS_PATH),
        params={"op": _REGISTRATIONS_PRIMEIRA_ESTATUTOS_OP},
        data={
          "userid": userid,
          "tipo": _REGISTRATIONS_TYPE_PRIMEIRA,
          "guiaid": batch.id,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
        timeout=self._timeout,
      )
      r.raise_for_status()
      data = json.loads(r.text)
    except (requests.exceptions.RequestException, ValueError) as exc:
      raise SavConnectionError(f"Estatuto op=151 failed: {exc}") from exc

    options = {
      int(val): label.strip()
      for val, label in re.findall(
        r"<option value='?(-?\d+)'?[^>]*>\s*([^<]+?)\s*<",
        data.get("estatutos", ""),
      )
      if int(val) > 0
    }
    if len(options) == 1:
      return next(iter(options))
    if not options:
      raise SavResponseError(
        f"No estatuto options returned for batch {batch.id}, "
        f"player {userid}: {data!r}"
      )
    listing = ", ".join(f"{i}={n!r}" for i, n in sorted(options.items()))
    raise SavConfigError(
      f"Multiple estatuto options for batch {batch.id}, player {userid}: "
      f"{listing}. Pass estatuto= to disambiguate (non-PT players)."
    )

  def _resolve_primeira_taxa_id(
    self,
    batch: PlayerRegistrationBatch,
    userid: int,
    estatuto: int,
    *,
    subida_tier_id: int = -1,
    subida_escalao_id: int = 0,
  ) -> int:
    """Op=26 — same taxa endpoint Revalidação uses, but the type-1 wizard
    also sends ``subida`` / ``subida_escalao`` (defaulting to -1/0 when no
    inline subida) since the available taxa can differ for promotion cases.

    Auto-picks a single real option; raises with the listing when ambiguous
    so the caller passes ``taxa_id`` explicitly.
    """
    import re

    try:
      r = self._http.get(
        self._url(_REGISTRATIONS_PATH),
        params={
          "op": _REGISTRATIONS_LOAD_TAXA_OP,
          "estatuto": estatuto,
          "guia": batch.id,
          "esc": 1,
          "subida": subida_tier_id,
          "subida_escalao": subida_escalao_id,
          "user": userid,
        },
        timeout=self._timeout,
      )
      r.raise_for_status()
      msg = json.loads(r.text).get("msg", "")
    except (requests.exceptions.RequestException, ValueError) as exc:
      raise SavConnectionError(f"Taxa op=26 failed: {exc}") from exc

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
        f"No taxa options for primeira batch {batch.id}, player {userid}: {msg!r}"
      )
    listing = ", ".join(f"{i}={n!r}" for i, n in sorted(options.items()))
    raise SavConfigError(
      f"Multiple taxa options for primeira batch {batch.id}, player {userid}: "
      f"{listing}. Pass taxa_id= to disambiguate."
    )

  def _resolve_primeira_insurance_cascade(
    self, batch: PlayerRegistrationBatch, userid: int,
  ) -> tuple[int, int, str]:
    """Run the type-1 seguro → companhia → apolice cascade.

    Returns ``(seguro_id, companhia_id, apolice)``. For the single-option
    case observed in practice op=175 returns an empty body and the commit
    just reuses the seguro_id for the ``companhia`` field — so we collapse
    them. op=24's apólice string IS sent in the op=27 body (unlike the
    Revalidação flow where it's display-only).
    """
    season_param = f"'{batch.season}'"

    # op=87: pick tipoSeguro=1 → returns seguro id under (overloaded) "companhia"
    try:
      r = self._http.get(
        self._url(_REGISTRATIONS_PATH),
        params={
          "op": _REGISTRATIONS_LOAD_SEGURO_OP,
          "id": userid,
          "agente": _REGISTRATIONS_AGENTE_PLAYER,
          "epoca": season_param,
          "guia": batch.id,
          "tiposeguro": _REGISTRATIONS_TIPOSEGURO_FEDERACAO,
        },
        timeout=self._timeout,
      )
      r.raise_for_status()
      seguro_id = int(json.loads(r.text)["companhia"])
    except (requests.exceptions.RequestException, ValueError, KeyError) as exc:
      raise SavConnectionError(
        f"Primeira insurance op=87 failed: {exc}"
      ) from exc

    # op=175: fire-and-forget for the single-option case (empty body). We
    # still send it for parity with the browser flow; a future multi-option
    # case would need to parse the response for a real companhia id.
    try:
      self._http.get(
        self._url(_REGISTRATIONS_PATH),
        params={
          "op": _REGISTRATIONS_LOAD_COMPANHIA_OP,
          "seguro": seguro_id,
          "escalao": batch.tier_id,
          "guia": batch.id,
          "epoca": season_param,
          "nivel": 0,
        },
        timeout=self._timeout,
      )
    except requests.exceptions.RequestException:
      logger.debug("op=175 primeira companhia fetch failed — non-fatal", exc_info=True)

    # op=24: apólice — text/plain string in the body (not JSON). Required
    # at commit-time for type-1 (the body carries it verbatim).
    try:
      r = self._http.get(
        self._url(_REGISTRATIONS_PATH),
        params={"op": _REGISTRATIONS_LOAD_APOLICE_OP, "companhia": seguro_id},
        timeout=self._timeout,
      )
      r.raise_for_status()
      apolice = r.text.strip()
    except requests.exceptions.RequestException as exc:
      raise SavConnectionError(
        f"Primeira apolice op=24 failed: {exc}"
      ) from exc

    # The single-insurance cascade collapses seguro_id and companhia_id —
    # both are sent as the same value in the op=27 commit body.
    return seguro_id, seguro_id, apolice

  def _primeira_commit(self, body: dict[str, Any]) -> dict[str, Any]:
    """Op=27 — type-1 final commit. JSON body sent with text/plain Content-Type."""
    try:
      resp = self._http.post(
        self._url(_REGISTRATIONS_PATH),
        params={"op": _REGISTRATIONS_PRIMEIRA_COMMIT_OP},
        data=json.dumps(body, ensure_ascii=False),
        headers={"Content-Type": "text/plain;charset=UTF-8"},
        timeout=self._timeout,
      )
      resp.raise_for_status()
      return json.loads(resp.text)
    except (requests.exceptions.RequestException, ValueError) as exc:
      raise SavConnectionError(
        f"Primeira commit op=27 failed: {exc}"
      ) from exc

  def _add_player_to_primeira_batch(
    self,
    batch: PlayerRegistrationBatch,
    *,
    # ── new-player demographics (no SAV record to fall back on) ──
    name: str,
    birth_date: str,
    gender_id: int,
    nif: str,
    id_type: int,
    id_number: str,
    id_expiry: str,
    email: str,
    telemovel: str | None,
    telefone: str | None,
    nationality_id: int,
    naturalidade_id: int,
    nome_pai: str | None,
    nome_mae: str | None,
    # ── address ──
    country_id: int,
    morada: str,
    cod_postal: str,
    distrito_id: int,
    concelho_id: int,
    localidade_id: int,
    localidade_txt: str,
    # ── step-3 ──
    exam_date: str,
    taxa_id: int | None,
    estatuto: int | None,
    promote_to_tier_id: int | None,
    inline_subida: bool,
    guardian_name: str | None,
    guardian_relation: int | None,
    guardian_phone: str | None,
    guardian_email: str | None,
    consent_data: bool,
    consent_communications: bool,
    consent_marketing: bool,
  ) -> int:
    """Walk the 1ª Inscrição wizard and return the new SAV userid.

    Mirrors the Revalidação shape but with type-1's renamed field surface
    and a different commit op. Sequence:
        modal-open guards (op=15/op=161, fire-and-forget) →
        duplicate check (op=11) →
        id-doc check (op=163) →
        age gate (op=14) →
        create player (op=12, yields userid) →
        save address (op=20, yields menor_idade) →
        load estatuto (op=151) →
        taxa cascade (op=26) →
        insurance cascade (op=87 → op=175 → op=24) →
        commit (op=27).
    """
    # Modal-open guards (mirror what the UI does on "Novo jogador" click)
    try:
      self._http.get(
        self._url(_REGISTRATIONS_PATH),
        params={"op": "15", "guia": batch.id},
        timeout=self._timeout,
      )
    except requests.exceptions.RequestException:
      logger.debug("op=15 primeira modal guard failed — non-fatal", exc_info=True)
    self._primeira_batch_context_refresh(batch.id)

    # Pre-create checks (server-side; treat duplicate as a hard stop)
    dup = self._check_primeira_player_duplicate(
      gender_id=gender_id, birth_date=birth_date, id_number=id_number,
    )
    if int(dup.get("existe", 0)) != 0:
      existing_id = dup.get("id") or dup.get("atleta") or ""
      raise SavResponseError(
        f"A player matching (genero={gender_id}, datanasc={birth_date}, "
        f"id_number={id_number}) already exists in SAV (id={existing_id!r}); "
        f"use Revalidação on the existing licence rather than 1ª Inscrição."
      )
    self._check_primeira_id_doc(id_number)
    self._check_primeira_birthdate_fits_tier(batch, birth_date)
    self._primeira_batch_context_refresh(batch.id)

    # op=12 — create the player record; this is where userid is born.
    userid = self._create_primeira_player(
      batch=batch,
      name=name, birth_date=birth_date, gender_id=gender_id,
      email=email, telemovel=telemovel, telefone=telefone,
      nif=nif, id_type=id_type, id_number=id_number, id_expiry=id_expiry,
      nationality_id=nationality_id, naturalidade_id=naturalidade_id,
      nome_pai=nome_pai, nome_mae=nome_mae,
    )

    # op=20 — save address; response carries menor_idade for the guardian gate.
    step2 = self._save_primeira_step2(
      batch=batch, userid=userid,
      country_id=country_id,
      distrito_id=distrito_id, concelho_id=concelho_id,
      localidade_id=localidade_id, localidade_txt=localidade_txt,
      morada=morada, cod_postal=cod_postal,
    )
    is_minor = bool(step2.get("menor_idade"))
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
          f"Player ({name!r}) is a minor; missing required fields: "
          f"{', '.join(missing)}"
        )

    # Estatuto (auto-pick single FBP option for PT players)
    if estatuto is None:
      estatuto = self._load_primeira_estatuto(batch, userid)

    # Inline subida — server-computed via op=21 like Revalidação. Skipped on
    # tiers without a higher escalão (op=21 returns only the placeholder).
    # promote_to_tier_id comes from the mod4's escalao_subida resolution and
    # is enforced against SAV's offered options by _pick_subida_tier.
    sub_tier = (
      self._pick_subida_tier(userid, prefer_tier_id=promote_to_tier_id)
      if inline_subida else None
    )
    if inline_subida and sub_tier is None:
      raise SavConfigError(
        f"Inline subida requested but SAV offers no subida tier for new "
        f"player {name!r} in batch {batch.id} (op=21 returned only "
        f"'- Não selecionado –'). Pick a tier that has a higher escalão."
      )

    # Taxa cascade — the type-1 op=26 takes the subida pair, so pass it.
    if taxa_id is None:
      taxa_id = self._resolve_primeira_taxa_id(
        batch, userid, estatuto,
        subida_tier_id=int(sub_tier[0]) if sub_tier else -1,
        subida_escalao_id=0,
      )

    # Insurance cascade
    seguro_id, companhia_id, apolice = (
      self._resolve_primeira_insurance_cascade(batch, userid)
    )

    exam_date = _coerce_exam_date(exam_date)

    commit_body = {
      "guiaid": batch.id,
      "userid": userid,
      "tipo": _REGISTRATIONS_TYPE_PRIMEIRA,
      "exame": "1",
      "subida": str(sub_tier[0]) if sub_tier else "-1",
      "obs": "",
      "dataexame": exam_date,
      "escalaosubida_txt": sub_tier[1] if sub_tier else "- Não selecionado –",
      "taxa": str(taxa_id),
      "estatuto": str(estatuto),
      "seguro": str(seguro_id),
      "companhia": str(companhia_id),
      "apolice": apolice,
      "nomeEncarregado": guardian_name or "",
      "tipoRegulacao": str(guardian_relation) if guardian_relation else "0",
      "telefoneEncarregado": guardian_phone or "",
      "emailEncarregado": guardian_email or "",
      "consentimentoDados": 1 if consent_data else 0,
      "comunicacoes": 1 if consent_communications else 0,
      "marketing": 1 if consent_marketing else 0,
    }

    result = self._primeira_commit(commit_body)
    if int(result.get("val", 0)) != 1:
      raise SavResponseError(
        f"Primeira commit failed: {result.get('msg') or result!r}"
      )
    logger.info(
      "Created primeira-inscrição player %r (userid=%s) in batch %s — "
      "result: %s",
      name, userid, batch.id, result.get("resultexame") or "ok",
    )
    return userid

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
    except requests.exceptions.RequestException as exc:
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
    rather than JSON (e.g. the player search endpoint).

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
    Parse the HTML table returned by the player search endpoint.

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

  def _parse_coaches_response(self, html: str) -> list[Coach]:
    """
    Parse the HTML table returned by the coach search endpoint
    (``treinadordb.php?op=1``).

    Each ``<tr>`` in the ``<tbody>`` maps to one Coach. The columns are:
      0: actions buttons (contain ``seeHistorico(carreira_id)`` and
                          ``seeTreinador(person_id)``)
      1: status icon     (``fa-color-activo`` → active)
      2: wallet number
      3: name
      4: association
      5: club
      6: gender
      7: season
      8: grade (formação)
      9: birth date

    Raises:
        SavResponseError: If the expected table is absent from the response.
    """
    from bs4 import BeautifulSoup
    import re

    soup = BeautifulSoup(html, "html.parser")
    tbody = soup.find("tbody")
    if tbody is None:
      raise SavResponseError(
        f"Coach search response missing expected table: {html[:200]!r}"
      )

    coaches: list[Coach] = []
    for row in tbody.find_all("tr"):
      cells = row.find_all("td")
      if len(cells) < 10:
        continue

      onclicks = " ".join(b.get("onclick", "") for b in cells[0].find_all("button"))
      person_match = re.search(r"seeTreinador\((\d+)", onclicks)
      carreira_match = re.search(r"seeHistorico\((\d+)", onclicks)
      person_id = int(person_match.group(1)) if person_match else 0
      carreira_id = int(carreira_match.group(1)) if carreira_match else 0

      icon = cells[1].find("i")
      icon_classes = " ".join(icon.get("class", [])) if icon else ""
      icon_title = (icon.get("data-original-title", "") if icon else "").lower()
      active = "fa-color-activo" in icon_classes or icon_title == "activo"

      coaches.append(Coach(
        id=person_id,
        carreira_id=carreira_id,
        wallet=cells[2].get_text(strip=True),
        name=cells[3].get_text(strip=True),
        association=cells[4].get_text(strip=True),
        club=cells[5].get_text(strip=True),
        gender=cells[6].get_text(strip=True),
        season=cells[7].get_text(strip=True),
        grade=cells[8].get_text(strip=True),
        birth_date=cells[9].get_text(strip=True),
        active=active,
      ))

    logger.info("Parsed %d coaches from search response", len(coaches))
    return coaches

  def _parse_coach_detail_response(self, raw: dict[str, Any], *, coach_id: int) -> Coach:
    """
    Parse the JSON envelope returned by ``treinadordb.php?op=2``.

    The server returns ``{"nome": str, "roles": int, "msg": "<html>..."}``
    where ``msg`` carries a multi-tab profile form. We pull out:
      - ``nif``           from ``<input id='nif' value='...'>``
      - ``tptd``          from ``<input id='nrtptd' value='...'>``
      - ``tptd_expiry``   from ``<input id='validadetptd' value='...'>``
      - ``mobile_phone``  from ``<input id='telem' value='...'>``

    Any field missing from the HTML is returned as an empty string rather
    than raising — the form layout differs slightly between profile types.
    """
    if "msg" not in raw:
      raise SavResponseError(
        f"Coach detail response missing 'msg' field: {list(raw.keys())}"
      )

    html = raw["msg"]
    name = str(raw.get("nome", ""))

    import re
    def _input_value(field_id: str) -> str:
      m = re.search(
        rf"<input[^>]*\bid=['\"]{re.escape(field_id)}['\"][^>]*\bvalue=['\"]([^'\"]*)['\"]",
        html,
      )
      return m.group(1) if m else ""

    return Coach(
      id=coach_id, carreira_id=0,
      wallet="", name=name,
      association="", club="",
      gender="", season="", grade="", birth_date="",
      active=False,
      nif=_input_value("nif"),
      tptd=_input_value("nrtptd"),
      tptd_expiry=_input_value("validadetptd"),
      mobile_phone=_input_value("telem"),
    )

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
    the player detail parser.  Returns empty strings on any error so that
    callers can gracefully fall back to storing only the display name.
    """
    try:
      from bs4 import BeautifulSoup

      text = self._post_form(
        _CLUB_DETAIL_PATH, {"id": club_id}, params={"op": _CLUB_DETAIL_OP}
      )
      raw = json.loads(text)
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
    except (SavError, ValueError, AttributeError):
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
    Parse the JSON envelope returned by jogadoresdb.php?op=2.

    The server returns ``{"msg": "<html>..."}``; we scan ``<img>`` tags for
    the player's photo and pull ``<input id="telem">`` for the mobile
    phone, returning a minimal Player with id, photo_url and mobile_phone.
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

    telem = soup.find(id="telem")
    mobile_phone = (telem.get("value", "") or "").strip() if telem else ""

    return Player(
      id=player_id, license="", name="", association="", club="",
      tier="", gender="", birth_date="", nationality="", status="",
      photo_url=photo_url,
      mobile_phone=mobile_phone,
    )

  def __repr__(self) -> str:
    authenticated = self.session is not None
    return (
      f"SavClient(base_url={self.base_url!r}, "
      f"user={self._username!r}, authenticated={authenticated})"
    )
