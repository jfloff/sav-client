# sav_client — Agent Reference

`sav_client` is a Python library for automating the FPB SAV2 basketball
management system. This document is the authoritative reference for AI agents
using the library directly.

---

## Setup

```python
from sav_client import SavClient

# From environment variables / .env file
client = SavClient.from_env()   # reads SAV_BASE_URL, SAV_USERNAME, SAV_PASSWORD
client.login()                  # must be called before any other method

# Explicit construction
client = SavClient("https://sav2.fpb.pt", "username", "plaintext_password")
client.login()
```

**Environment variables:**

| Variable | Required | Default |
|----------|----------|---------|
| `SAV_USERNAME` | yes | — |
| `SAV_PASSWORD` | yes | — |
| `SAV_BASE_URL` | no | `https://sav2.fpb.pt` |
| `SAV_TIMEOUT` | no | `30` (seconds) |
| `SAV_LOG_LEVEL` | no | `WARNING` |

`login()` must succeed before calling any other method. It populates
`client.session`. Calling any method without login raises `SavResponseError`.

---

## Data models

All models are **frozen dataclasses** (immutable). Import them from the top-level package.

### `Player`

```python
@dataclass(frozen=True)
class Player:
    id: int           # internal SAV2 database ID — use for get_player_detail()
    license: str      # licence number (string) — human identifier
    name: str
    association: str  # association display name
    club: str         # club display name
    tier: str         # escalão, e.g. "Sénior", "Sub 14"
    gender: str       # "Masculino" / "Feminino"
    birth_date: str   # "YYYY-MM-DD"
    nationality: str
    status: str       # registration status, e.g. "FBP"
    season: str       # e.g. "2025/2026"; only from search results
    active: bool      # True = currently registered and eligible
    photo_url: str    # populated only by get_player_detail(photo=True)
```

`active: True` is the definitive eligibility signal. A player present in
search results but with `active=False` is not eligible for the current season.

### `Game`

```python
@dataclass(frozen=True)
class Game:
    id: int           # internal SAV2 ID — required for game-sheet methods
    number: str       # human-readable game number, e.g. "S14M-001"
    competition: str
    phase: str
    round: str
    date: str         # "DD-MM-YYYY"
    time: str         # "HH:MM"
    home: str         # home team name
    away: str         # away team name
    home_score: str   # empty string if not played yet
    away_score: str
    venue: str
    game_status: str  # "Marcado" = scheduled, "Não Marcado" = unscheduled
    result_status: str  # "Com Resultado" / "Sem Resultado"
    tier: str
    gender: str
    level: str
```

`game.id` is the internal SAV2 key, distinct from `game.number`. Always use
`game.id` when calling `get_eligible_players()` or `get_game_sheet_pdf()`.

### `Club`

```python
@dataclass(frozen=True)
class Club:
    id: int        # numeric club or association ID
    name: str      # short display name, e.g. "Santarém BC"
    full_name: str # official full name
    code: str      # abbreviation, e.g. "SBC"
```

`list_associations()` also returns `Club` objects — in that context only `id`
and `name` are populated.

### `LoginResult`

```python
@dataclass(frozen=True)
class LoginResult:
    success: bool
    message: str        # human-readable server message
    session: Session | None
    redirect: str | None
    raw: dict           # full server payload for debugging
```

### `Session`

Opaque session container. Access known fields via properties:

```python
session.user_id   # id_utilizador
session.username  # utilizador
session.role      # perfil
session.get("key", default)  # raw key access
```

---

## Methods

### `login() → LoginResult`

Authenticate. Must be called first. Raises `SavAuthError` on bad credentials.

```python
result = client.login()
assert result.success
```

---

### `search_players(**kwargs) → list[Player]`

```python
players = client.search_players()                          # own club, current season
players = client.search_players(name="João")
players = client.search_players(license="301772")          # exact match; other filters ignored by server
players = client.search_players(tier="Sénior")
players = client.search_players(club=270)                  # specific club by ID
players = client.search_players(club=[270, 666, 2430])     # multiple clubs, parallel, deduplicated
players = client.search_players(club=0, association=7)     # all clubs in one association
players = client.search_players(club=0)                    # all clubs, federation-wide (slow)
players = client.search_players(season=0)                  # all seasons
players = client.search_players(birth_date="1990-05-12")
players = client.search_players(page=2)                    # pagination (single-club only)
```

**Parameter reference:**

| Parameter | Type | Default | Notes |
|-----------|------|---------|-------|
| `name` | `str` | `""` | Partial match |
| `license` | `str` | `""` | Exact; overrides other filters server-side |
| `number` | `str` | `""` | Shirt number |
| `gender` | `int` | `0` | 0 = any |
| `tier` | `str` | `""` | e.g. `"Sénior"` |
| `season` | `int\|None` | current | `0` = all seasons |
| `club` | `int\|list[int]\|None` | own club | `None` = own; `0` = all; list = parallel search |
| `association` | `int` | `0` | Used only when `club=0` |
| `birth_date` | `str` | `""` | `YYYY-MM-DD` |
| `page` | `int` | `1` | Ignored for multi-club searches |

**Mutual exclusion:** `club` (non-None, non-0) and `association` cannot be
meaningfully combined — `association` is only used when `club=0`.

---

### `get_player_detail(player_id, *, photo=False) → Player`

Fetch photo URL for a player. Only useful when `photo=True`; otherwise returns
a stub with only `id` set.

```python
player = client.search_players(license="301772")[0]
detail = client.get_player_detail(player.id, photo=True)
print(detail.photo_url)
```

The `player_id` argument is `Player.id` (internal SAV2 ID), not the licence number.

---

### `list_games(**kwargs) → list[Game]`

```python
games = client.list_games()                                     # current season, all games
games = client.list_games(date_from="01-04-2025", date_to="30-04-2025")
games = client.list_games(game_number="S14M-001")               # exact game number
games = client.list_games(tier="Sub 14")
games = client.list_games(game_status=1)                        # numeric status code
```

Date strings are `DD-MM-YYYY`. Returns games for the authenticated profile's
club. `Game.id` is required for all game-sheet operations.

---

### `get_eligible_players(game_id, *, val=1) → dict`

Return eligible players and staff for one team. Does not generate a PDF.

```python
data = client.get_eligible_players(game.id, val=1)   # val=1 home, val=2 away
```

**Return shape:**

```python
{
  "game_number": "S14M-001",    # human-readable game number
  "players": [
    {"licence": "301772", "name": "João Silva", "birth_date": "1990-05-12", "status": "FBP"},
    ...
  ],
  "coaches_pri": [
    {"wallet": "44321", "name": "Carlos Coach", "grade": "...", "function": "..."},
    ...
  ],
  "coaches_adj": [...],   # same shape as coaches_pri
  "staff": [
    {"licence": "...", "name": "...", "function": "..."},
    ...
  ],
}
```

**Important — coach pool vs PDF slot:**
`coaches_pri` and `coaches_adj` are the pools SAV considers eligible for each role,
but they are **not exclusive**. A coach who appears only in `coaches_pri` can still
be assigned as adjunct coach in the PDF (and vice versa). When matching a coach name
to a slot, search **both pools** for the wallet number, then pass it into whichever
slot the user requested (`coaches_pri` or `coaches_adj` argument of
`get_eligible_players_pdf`). Never reject a coach just because they don't appear in
the "matching" pool — only reject if they are absent from both pools entirely.

Use `data["players"][n]["licence"]` to collect licence numbers for the PDF call.
Search both `data["coaches_pri"]` and `data["coaches_adj"]` when looking up a coach wallet number.

---

### `get_eligible_players_pdf(game_id, *, val=1, ...) → bytes | None`

Generate and download the eligible-players PDF from SAV. Replicates the
browser's "Imprimir listagem" button.

```python
# All eligible players, both coaches selected, OUTROS/staff excluded (defaults)
pdf = client.get_eligible_players_pdf(game.id, val=1)

# Specific players only
pdf = client.get_eligible_players_pdf(
    game.id, val=1,
    player_licences=[301772, 285943],
)

# Specific coaches
pdf = client.get_eligible_players_pdf(
    game.id, val=1,
    coaches_pri=[44321],
    coaches_adj=[55432],
)

# Include OUTROS TREINADORES and ENQUADRAMENTO HUMANO
pdf = client.get_eligible_players_pdf(
    game.id, val=1,
    coaches_other=None,   # None = all; () = none (default)
    staff=None,
)
```

**Parameter reference:**

| Parameter | Type | Default | Meaning |
|-----------|------|---------|---------|
| `val` | `int` | `1` | `1` = home, `2` = away |
| `player_licences` | `list[int]\|None` | `None` | `None` = all eligible; list = only these |
| `coaches_pri` | `list[int]\|None` | `None` | Head coach wallet numbers; `None` = all |
| `coaches_adj` | `list[int]\|None` | `None` | Adjunct coach wallet numbers; `None` = all |
| `coaches_other` | `list[int]\|None\|tuple` | `()` | Other coaches; **`()` = none by default** |
| `staff` | `list[int]\|None\|tuple` | `()` | Enquadramento humano; **`()` = none by default** |

Returns `None` if the game has no eligible players page (e.g. `equipa_id` not
found in the SAV response). Returns PDF bytes starting with `b"%PDF"` on
success.

IDs are **integers**: licence numbers for players/staff, wallet numbers for
coaches. Collect them from `get_eligible_players()` output.

---

### `get_game_sheet_pdf(game_id) → bytes | None`

Download the **uploaded** game-sheet PDF (post-game referee sheet), if one
exists. This is different from `get_eligible_players_pdf` — it retrieves a
file uploaded by officials, not a generated pre-game list.

```python
pdf = client.get_game_sheet_pdf(game.id)
if pdf is not None:
    open("sheet.pdf", "wb").write(pdf)
```

Returns `None` if no sheet has been uploaded yet.

---

### `list_associations() → list[Club]`

Return all regional associations. Returns `Club` objects; only `id` and `name`
are populated.

```python
associations = client.list_associations()
# [Club(id=7, name="AB Santarém"), ...]
```

---

### `list_clubs(association=None) → list[Club]`

Return clubs in the given association, or in the logged-in organisation's own
association when `association=None`.

```python
clubs = client.list_clubs()                  # own association
clubs = client.list_clubs(association=7)     # specific association
```

---

## Exceptions

```
SavError                  ← base; catch this to handle all SAV errors
├── SavConfigError        ← missing/invalid env vars or constructor args
├── SavConnectionError    ← network timeout, DNS failure, HTTP error
├── SavAuthError          ← server rejected credentials (login only)
└── SavResponseError      ← unexpected server response shape
```

```python
from sav_client.exceptions import SavError, SavAuthError, SavConnectionError

try:
    client.login()
except SavAuthError:
    # wrong credentials
except SavConnectionError:
    # network unreachable
except SavError:
    # any other SAV error
```

`SavResponseError` is also raised when a method is called before `login()`.
The message always starts with `"Must call login()"` in that case.

---

## Typical agent workflows

### Find an active player by name

```python
players = client.search_players(name="João Silva")
active = [p for p in players if p.active]
```

### Look up a player's club ID

```python
associations = client.list_associations()
assoc = next(a for a in associations if "Santarém" in a.name)
clubs = client.list_clubs(association=assoc.id)
club = next(c for c in clubs if "Rio Maior" in c.name)
# club.id is the numeric ID for search_players(club=...)
```

### Search players across several known clubs

```python
players = client.search_players(club=[270, 666, 2430])
# Parallel fetch, deduplicated by player ID
```

### Generate a pre-game eligible players PDF

```python
games = client.list_games(game_number="S14M-001")
game = games[0]

# Inspect who is eligible
data = client.get_eligible_players(game.id, val=1)
licences = [int(p["licence"]) for p in data["players"] if p.get("licence")]

# Download PDF with all eligible players
pdf = client.get_eligible_players_pdf(game.id, val=1)
open("lineup.pdf", "wb").write(pdf)
```

### Check whether a game has an uploaded sheet

```python
pdf = client.get_game_sheet_pdf(game.id)
has_sheet = pdf is not None
```
