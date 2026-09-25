"""
Data models for SavClient.

All models are immutable dataclasses. Raw server payloads are preserved in
`raw` fields so callers can access undocumented keys without requiring a
library update.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class Session:
    """
    Represents an authenticated SAV session.

    The server returns a JSON object under the `sessao` key on successful
    login. Its full shape is opaque, but downstream requests are expected to
    include it verbatim.  All known top-level keys are surfaced as properties;
    everything else is accessible via `get()` or the `raw` dict.
    """

    raw: dict[str, Any]

    # ------------------------------------------------------------------
    # Convenience accessors — add more as the response shape is discovered
    # ------------------------------------------------------------------

    @property
    def user_id(self) -> Any:
        return self.raw.get("id_utilizador") or self.raw.get("id")

    @property
    def username(self) -> Any:
        return self.raw.get("utilizador") or self.raw.get("username")

    @property
    def role(self) -> Any:
        return self.raw.get("perfil") or self.raw.get("role")

    def get(self, key: str, default: Any = None) -> Any:
        """Return a value from the raw session dict."""
        return self.raw.get(key, default)

    def __bool__(self) -> bool:
        return bool(self.raw)

    def __repr__(self) -> str:
        # Avoid leaking session tokens in logs
        keys = list(self.raw.keys())
        return f"Session(keys={keys})"


@dataclass(frozen=True)
class Season:
    """
    Represents a SAV2 season ("época").

    SAV2 identifies a season only by an opaque, sequential ``epoca_id`` — it is
    *not* the calendar year (e.g. id=64 is season "2025/2026"). The human label
    ``descricao`` ("YYYY/YYYY+1") is the only reliable way to map an id to a
    calendar year.

    Attributes:
        id:          Internal SAV2 season id (``epoca_id``).
        label:       Human label, e.g. "2025/2026".
        start_year:  Starting calendar year parsed from ``label`` (e.g. 2025).
        end_year:    Ending calendar year parsed from ``label`` (e.g. 2026).
        is_active:   True for the season SAV2 marks as current (``activa == 1``).
    """

    id: int
    label: str
    start_year: int
    is_active: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def end_year(self) -> int:
        """Ending calendar year parsed from the authoritative SAV label."""
        return int(self.label.split("/", 1)[1])

    def __repr__(self) -> str:
        return (
            f"Season(id={self.id}, label={self.label!r}, "
            f"active={self.is_active})"
        )


@dataclass(frozen=True)
class Game:
  """
  Represents a scheduled or played game.

  Attributes:
      id:             Internal SAV2 game ID (used for game-sheet lookup).
      number:         SAV2 game number (human-readable).
      competition:    Competition/tournament name.
      phase:          Phase name (e.g. "1ª Fase - Série A").
      round:          Round/matchday number string.
      date:           Game date, normalised to YYYY-MM-DD. SAV sends this
                      endpoint's dates as DD-MM-YYYY; the client converts so
                      every date it emits has one shape.
      time:           Kick-off time string (HH:MM).
      home:           Home team name.
      away:           Away team name.
      home_score:     Home team score (empty if not played yet).
      away_score:     Away team score (empty if not played yet).
      venue:          Venue/arena name.
      game_status:    Game status (e.g. "Não Marcado", "Marcado").
      result_status:  Result status (e.g. "Sem Resultado", "Com Resultado").
      tier:           Age/competition tier (e.g. "Sub 14").
      gender:         Gender string (e.g. "Masculino").
      level:          Competitive level string (e.g. "Sub 14 F").
  """

  id: int
  number: str
  competition: str
  phase: str
  round: str
  date: str
  time: str
  home: str
  away: str
  home_score: str
  away_score: str
  venue: str
  game_status: str
  result_status: str
  tier: str
  gender: str
  level: str

  def __repr__(self) -> str:
    score = f"{self.home_score}-{self.away_score}" if self.home_score else "vs"
    return (
      f"Game(id={self.id}, number={self.number!r}, date={self.date!r}, "
      f"{self.home!r} {score} {self.away!r})"
    )


@dataclass(frozen=True)
class Club:
  """
  Represents a club returned by the clubs listing.

  Attributes:
      id:        SAV2 numeric club ID.
      name:      Short display name (Nome Reduzido), e.g. "Santarém BC".
      full_name: Full official name (Nome do Clube), e.g. "Santarém Basket Clube".
      code:      Short code / abbreviation (Código), e.g. "SBC".
  """

  id: int
  name: str
  full_name: str = ""
  code: str = ""

  def __repr__(self) -> str:
    return f"Club(id={self.id}, name={self.name!r})"


@dataclass(frozen=True)
class SubidaStatus:
  """
  Whether a Subida de escalão is on file for a player in the current season.

  Read from the "Inscrições" tab of the player detail page
  (``jogadoresdb.php?op=2``), SAV's own per-licence registration history. It is
  the only read-only source that shows a promotion: the search row's ``tier``,
  the op=29 history and op=49's origin all report the *base* escalão, and the
  batch listing drops a lote once it is validated.

  Both routes count: an inline subida on a 1ª Inscrição/Revalidação (SAV
  renders the escalão as ``"Sub 14 >> Sub 16"``) and a standalone "Subida de
  Escalão" lote.

  Attributes:
      status:      One of:

                   * ``"approved"`` — a subida row this season with an approval
                     date.
                   * ``"pending"`` — a subida row whose lote has been filed but
                     not approved yet (blank approval date).
                   * ``"none"`` — this season's rows were read and none is a
                     subida, or the player has no row this season at all.
                     **Wrong for a lote still "Em construção"**: the player
                     page does not list open lotes (verified live
                     2026-09-24). For a player in an in-flight lote, use the
                     lote-row status (``SavClient.batch_item_subida``).
                   * ``"unknown"`` — SAV's answer could not be read (tab or
                     columns missing, current season unresolved), or SAV
                     rendered the reduced table it shows when another club holds
                     the player's last approved registration: it has no approval
                     date, and whether it marks an inline subida is unverified.
                     Never treat ``"unknown"`` as ``"none"``.
      tier_from:   Escalão before the promotion, e.g. ``"Sub 14"``; ``None``
                   when there is no subida row.
      tier_to:     Escalão promoted to, e.g. ``"Sub 16"``; ``None`` when there
                   is no subida row or SAV did not render the destination.
      approved_on: ISO approval date for ``"approved"``; ``None`` otherwise.
  """

  status: str
  tier_from: str | None = None
  tier_to: str | None = None
  approved_on: str | None = None


@dataclass(frozen=True)
class Player:
  """
  Represents a player in the SAV2 system.

  Attributes:
      id:           Internal SAV2 database ID.
      license:      Licence number.
      name:         Full name.
      association:  Association name (e.g. "AB Santarém").
      club:         Club name (e.g. "Rio Maior Basket").
      club_id:      SAV2 numeric club ID this row belongs to. Stamped from the
                    club a search was scoped to (search results carry no id
                    column), so it is authoritative for the source club even in
                    a multi-club search. 0 when unknown (e.g. a minimal Player
                    from ``get_player_detail`` without a search).
      tier:         Age/competition tier (escalão), e.g. "Sénior".
      tier_id:      Numeric escalão ID for this (gender, tier), e.g. 5 = Sub 14
                    (Masculino). Resolved in-memory from the tier/gender names
                    via the static ``sav_shared.lookups`` tables — SAV renumbers
                    the same tier per gender, so it depends on ``gender_id``. 0
                    when the name is blank or unrecognised (graceful fallback).
      gender:       Gender string, e.g. "Masculino" / "Feminino".
      gender_id:    1=Masculino, 2=Feminino; 0 when unknown. Resolved from the
                    ``gender`` name.
      birth_date:   Birth date string (YYYY-MM-DD).
      nationality:  Nationality string.
      status:       Registration status string, e.g. "FBP".
      season:       Season string, e.g. "2025/2026" (search only).
      license:      FPB licence number as an int; 0 when the row carries no
                    licence (e.g. a detail-only fetch). SAV renders it as
                    table text — the client normalises so every licence on
                    the public surface is the same type and comparisons
                    against batch rows and authz sets can't silently fail.
      active:       True when the status icon indicates "Activo" (search only).
      photo_url:    Photo URL. Empty unless the detail was fetched via
                    ``get_player_detail(with_details=True)`` or
                    ``search_players(with_details=True)``.
      mobile_phone: Mobile phone number (telemóvel). Empty unless detail
                    was fetched.
      nif:          Portuguese tax number. Empty unless detail was fetched.
      subida:       Current-season Subida de escalão, as a ``SubidaStatus``.
                    ``None`` unless detail was fetched.
  """

  id: int
  license: int
  name: str
  association: str
  club: str
  tier: str
  gender: str
  birth_date: str
  nationality: str
  status: str
  season: str = ""
  # SAV2 club id this row was scoped to; 0 when unknown. See class docstring.
  club_id: int = 0
  active: bool = False
  photo_url: str = ""
  mobile_phone: str = ""
  nif: str = ""
  # Resolved in-memory from the tier/gender *names* (see Player docstring);
  # 0 means "unknown / unresolved", same convention as the id=0 placeholder.
  tier_id: int = 0
  gender_id: int = 0
  subida: SubidaStatus | None = None

  def __repr__(self) -> str:
    return (
      f"Player(id={self.id}, license={self.license!r}, "
      f"name={self.name!r}, tier={self.tier!r}, active={self.active})"
    )


@dataclass(frozen=True)
class NifLicenses:
  """Licences found for one NIF and whether the club scan was exhaustive.

  Attributes:
      licenses: Matching licences in ascending order.
      complete: Whether club coverage was exhaustive for this lookup.
  """

  licenses: list[int]
  complete: bool


@dataclass(frozen=True)
class IdentityMatch:
  """Outcome of resolving a person from one or more identity attributes.

  ``player`` and ``other_licenses`` describe a unique person; ``candidates``
  contains one representative row per person for an ambiguous result. For
  unknown or absent answers, ``player`` is ``None`` and candidate lists are
  empty.
  """

  status: Literal["found", "ambiguous", "not_found", "unknown"]
  player: Player | None
  other_licenses: list[Player]
  candidates: list[Player]
  matched_by: list[str]
  placeholder_nif: bool
  # Set on a found player when a NIF was a key — how SAV's NIF on file compares
  # with it: "match" (confirmed), "different" (SAV holds another real NIF —
  # usually SAV's record is wrong; the placeholder 999999990 included), "none"
  # (no NIF on file), or "unknown" (never read, or hidden at another club).
  nif_on_file: Literal[
    "match", "different", "none", "unknown",
  ] | None = None
  # When the NIF matched a person but another supplied key disagrees with
  # SAV: one {key, given, on_file} per disagreeing key ("name", "birth_date",
  # "id_number"). The NIF's person is still the answer.
  conflicts: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class Coach:
  """
  Represents a coach (treinador) returned by the coaches listing.

  Attributes:
      id:           Internal SAV2 person ID (from ``seeTreinador(N)``).
      carreira_id:  Internal SAV2 career-record ID (from ``seeHistorico(N)``).
                    Identifies the coach's career row for the listed season.
      wallet:       Carteira/licença number, e.g. "22174".
      name:         Full name.
      association:  Association name (e.g. "AB Santarém").
      club:         Club name (e.g. "Rio Maior Basket").
      gender:       Gender string, e.g. "Masculino" / "Feminino".
      season:       Season string, e.g. "2025/2026".
      grade:        Formation grade, e.g. "Grau 3", "Estagiário Grau 2".
      birth_date:   Birth date string (YYYY-MM-DD).
      active:       True when the status icon indicates "Activo".
      nif:          Portuguese tax number. Empty unless fetched via
                    ``get_coach_detail`` or ``list_coaches(with_details=True)``.
      tptd:         TPTD number (Nr. TPTD). Empty unless detail was fetched.
      tptd_expiry:  TPTD expiry date (YYYY-MM-DD; SAV sends DD-MM-YYYY and the
                    client converts). Empty unless detail was fetched.
      mobile_phone: Mobile phone number (telemóvel). Empty unless detail
                    was fetched.
      email:        Email address. Empty unless detail was fetched.
  """

  id: int
  carreira_id: int
  wallet: str
  name: str
  association: str
  club: str
  gender: str
  season: str
  grade: str
  birth_date: str
  active: bool = False
  nif: str = ""
  tptd: str = ""
  tptd_expiry: str = ""
  mobile_phone: str = ""
  email: str = ""

  def __repr__(self) -> str:
    return (
      f"Coach(id={self.id}, wallet={self.wallet!r}, "
      f"name={self.name!r}, grade={self.grade!r}, active={self.active})"
    )


@dataclass(frozen=True)
class PlayerRegistrationBatch:
  """
  Represents a player registration batch ("Lote" / "Guia") from the
  Pesquisa Lotes page.

  A batch groups player registration requests of one type (1ª Inscrição,
  Revalidação, Transferência, Subida) constrained to a single tier+gender.
  Only batches in state "Em construção" (idestado=1) are open for new items.

  Attributes:
      id:           Internal SAV2 batch ID (`guia_id`).
      number:       Human-readable batch number (`numero_guia`).
      type_id:      1=1ª Inscrição, 2=Revalidação, 3=Transferência, 4=Subida.
      type:         Type display name (e.g. "1ª Inscrição").
      association_id, association: Regional association.
      club_id, club:               Owning club.
      tier_id, tier:               Tier/escalão (e.g. id=5, name="Sub 14").
      gender_id, gender:           1=Masculino, 2=Feminino.
      state_id, state:             1=Em construção, 9=Em Validação; Devolvida and
                                    Em Pagamento (ids unknown).
      state_date:   ISO date the batch entered its current state.
      item_count:   Number of players currently in the batch.
      season_id, season: Season epoch.
  """

  id: int
  number: str
  type_id: int
  type: str
  association_id: int
  association: str
  club_id: int
  club: str
  tier_id: int
  tier: str
  gender_id: int
  gender: str
  state_id: int
  state: str
  state_date: str
  item_count: int
  season_id: int
  season: str

  @property
  def is_open(self) -> bool:
    """True when the batch is in 'Em construção' state and accepts items."""
    return self.state_id == 1

  @property
  def is_pending(self) -> bool:
    """True while the batch is still in flight, in any of SAV's four states.

    The listing only ever returns 'Em construção', 'Devolvida', 'Em Validação'
    and 'Em Pagamento' — a batch that has completed drops out of it — so being
    listed at all is what "pending" means.

    Distinct from :attr:`is_open`, which is the narrower "accepts new items".
    Mutations must gate on `is_open`; questions about whether a player is
    already being processed want this one, or they report someone sitting in
    'Em Validação' as not enrolled.
    """
    return True

  def __repr__(self) -> str:
    return (
      f"PlayerRegistrationBatch(id={self.id}, number={self.number!r}, "
      f"type={self.type!r}, tier={self.tier!r}, gender={self.gender!r}, "
      f"state={self.state!r}, items={self.item_count})"
    )


@dataclass(frozen=True)
class LoginResult:
    """
    Result of a login attempt.

    Attributes:
        success:  True when `val == 1` in the server response.
        message:  Human-readable message returned by the server (stripped of HTML).
        session:  Populated on success; None on failure.
        redirect: The path the server wants the browser to navigate to after
                  login.  Useful if subsequent requests target a specific page.
        raw:      Full parsed JSON response for debugging.
    """

    success: bool
    message: str
    session: Session | None = None
    redirect: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:
        return (
            f"LoginResult(success={self.success}, message={self.message!r}, "
            f"redirect={self.redirect!r})"
        )
