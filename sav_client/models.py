"""
Data models for SavClient.

All models are immutable dataclasses. Raw server payloads are preserved in
`raw` fields so callers can access undocumented keys without requiring a
library update.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


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
class Game:
  """
  Represents a scheduled or played game.

  Attributes:
      id:             Internal SAV2 game ID (used for game-sheet lookup).
      number:         SAV2 game number (human-readable).
      competition:    Competition/tournament name.
      phase:          Phase name (e.g. "1ª Fase - Série A").
      round:          Round/matchday number string.
      date:           Game date string as returned by the server (DD-MM-YYYY).
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
class Player:
  """
  Represents a player in the SAV2 system.

  Attributes:
      id:           Internal SAV2 database ID.
      license:      Licence number.
      name:         Full name.
      association:  Association name (e.g. "AB Santarém").
      club:         Club name (e.g. "Rio Maior Basket").
      tier:         Age/competition tier (escalão), e.g. "Sénior".
      gender:       Gender string, e.g. "Masculino" / "Feminino".
      birth_date:   Birth date string (YYYY-MM-DD).
      nationality:  Nationality string.
      status:       Registration status string, e.g. "FBP".
      season:       Season string, e.g. "2025/2026" (search only).
      active:       True when the status icon indicates "Activo" (search only).
      photo_url:    Photo URL; populated only via get_player_detail(photo=True).
  """

  id: int
  license: str
  name: str
  association: str
  club: str
  tier: str
  gender: str
  birth_date: str
  nationality: str
  status: str
  season: str = ""
  active: bool = False
  photo_url: str = ""

  def __repr__(self) -> str:
    return (
      f"Player(id={self.id}, license={self.license!r}, "
      f"name={self.name!r}, tier={self.tier!r}, active={self.active})"
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
