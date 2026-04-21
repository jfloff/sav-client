# sav — Agent Reference

`sav` is a CLI client for the FPB SAV2 basketball management system.
This document is the authoritative reference for AI agents using `sav`.

---

## Terminology

| Term | Meaning |
|------|---------|
| **game sheet** | Pre-game eligible players list printed before tip-off. Generated via `sav game-sheet --out`. |
| **referee sheet** | Post-game document uploaded to SAV2 by the referee after the game. Retrieved via `get_game_sheet_pdf()` in the Python library — **no CLI command exists for this**. |
| **licence** | Player registration number — a numeric string (e.g. `"301772"`). Used to identify players across all commands. |
| **wallet** | Coach/official registration number — a separate numeric identifier, distinct from player licences. Used in `--coach-pri` / `--coach-adj` flags. |
| **val** | Team selector used internally: `1` = home team, `2` = away team. The CLI exposes this as `--home` / `--away`. |
| **coaches_pri** | Head coaches (treinadores principais) eligible for this game. |
| **coaches_adj** | Adjunct/assistant coaches eligible for this game. |
| **tier / escalão** | Age/competition category, e.g. `"Mini 12"`, `"Sub 14"`, `"Sénior"`. Free-text string as it appears in SAV2. |
| **active** | `true` when the player is registered and eligible for the current season. The definitive eligibility signal — do not assume eligibility from presence in results alone. |
| **association** | Regional basketball body (e.g. AB Santarém). Identified by a numeric `id` from `sav associations`. |
| **game_status** | Lifecycle state of a game: `"Marcado"` (scheduled), `"Realizado"` (played), `"Não Marcado"` (unscheduled), `"Adiado"` (postponed), `"Anulado"` (cancelled). |

---

## Setup

Credentials are read from environment variables (or a `.env` file in the working directory):

```
SAV_BASE_URL   # optional, defaults to https://sav2.fpb.pt
SAV_USERNAME   # required
SAV_PASSWORD   # required
```

Every command authenticates automatically. No explicit login step is needed.

---

## Output formats

All commands accept a top-level `--output` / `-o` flag:

| Flag | Use case |
|------|----------|
| `--output table` | Default. Human-readable. Do not parse this. |
| `--output json` | Structured. **Use this when you need to read the output.** |
| `--output csv` | Tabular. Use when feeding into another tool. |

**Always pass `--output json` when you need to read command output programmatically.**

---

## Commands

### `sav associations`

List all regional associations with their numeric IDs.

```sh
sav --output json associations
```

**JSON shape:** `[{"id": 7, "name": "AB Santarém"}, ...]`

Use this to discover `id` values needed by `sav clubs --association`.

---

### `sav clubs [QUERY]`

List clubs. `QUERY` is an optional case-insensitive substring matched against
short name, full name, and code.

```sh
sav --output json clubs                        # clubs in your own association
sav --output json clubs "Rio Maior"            # filter by name fragment
sav --output json clubs --association 7        # clubs in a specific association
```

**JSON shape:** `[{"id": 270, "name": "Rio Maior Basket", "full_name": "...", "code": "RMB"}, ...]`

Key fields:
- `id` — numeric club ID, used by `sav players --club`
- `name` — short display name
- `code` — abbreviation (e.g. `SBC`)

---

### `sav players`

Search players. All filters are optional and combinable.

```sh
sav --output json players
sav --output json players --name "João"
sav --output json players --license 301772
sav --output json players --tier "Sénior"
sav --output json players --tier "Mini 12" --tier "Mini 10" --tier "Baby Basket"   # multiple tiers
sav --output json players --club 270
sav --output json players --club "Rio Maior"          # name fragment, may match >1 club
sav --output json players --club 270 --club 666       # multiple clubs
sav --output json players --association "Santarém"    # all clubs in that association
sav --output json players --all-clubs                 # federation-wide (slow)
sav --output json players --season 0                  # all seasons
sav --output json players --birth-date 1990-01-01
sav --output json players --limit 50
```

**`--tier` is repeatable.** Pass it multiple times to search several tiers in parallel and get a combined, deduplicated result.

**Mutual exclusions:** `--club` cannot be combined with `--association` or `--all-clubs`.

**JSON shape:**
```json
[{
  "id": 12345,
  "license": "301772",
  "name": "João Silva",
  "club": "Rio Maior Basket",
  "association": "AB Santarém",
  "tier": "Sénior",
  "gender": "Masculino",
  "birth_date": "1990-05-12",
  "nationality": "Portuguesa",
  "status": "FBP",
  "season": "2025/2026",
  "active": true,
  "photo_url": ""
}]
```

`license` is a string. `active` is a boolean — only `true` means the player is
currently registered and eligible.

---

### `sav player LICENCE_NUM`

Fetch full detail for a single player by licence number.

```sh
sav --output json player 301772
sav --output json player 301772 --photo   # also fetches photo_url
```

**JSON shape:** same as one element from `sav players`, with `photo_url` populated
when `--photo` is passed.

---

### `sav games`

List games for the authenticated profile's club. Defaults to scheduled
(`Marcado`) games for the current season.

```sh
sav --output json games
sav --output json games --date-from 01-04-2025 --date-to 30-04-2025
sav --output json games --tier "Sub 14"
sav --output json games --status "Não Marcado"
sav --output json games --status all             # every status
```

**Date format:** `DD-MM-YYYY`.

**`--status` default is `Marcado`.** Pass `--status all` to see every status.

**JSON shape:**
```json
[{
  "id": 98765,
  "number": "S14M-001",
  "competition": "Campeonato Nacional Sub 14",
  "phase": "1ª Fase - Série A",
  "round": "1",
  "date": "12-04-2025",
  "time": "10:00",
  "home": "Rio Maior Basket",
  "away": "Santarém BC",
  "home_score": "",
  "away_score": "",
  "venue": "Pavilhão Municipal",
  "game_status": "Marcado",
  "result_status": "Sem Resultado",
  "tier": "Sub 14",
  "gender": "Masculino",
  "level": "Sub 14 F"
}]
```

Key fields:
- `number` — human-readable game number (e.g. `S14M-001`), used by `sav game-sheet`.
- `id` — internal SAV2 ID. Not needed by any CLI command; present for completeness.
- `game_status` values:

| Value | Meaning |
|-------|---------|
| `"Marcado"` | Scheduled (default filter) |
| `"Não Marcado"` | Unscheduled |
| `"Realizado"` | Played / completed |
| `"Anulado"` | Cancelled |
| `"Adiado"` | Postponed |

---

### `sav game-sheets`

List games across all statuses, with client-side date filtering. Unlike
`sav games`, this works correctly for completed (`Realizado`) games — the SAV2
server drops completed games when date-filtering, so this command fetches the
full season and filters locally.

**Use this instead of `sav games` whenever you need `game_number` values for
`sav game-sheet`**, especially for games that may already have been played.

```sh
sav --output json game-sheets                               # all games, current season
sav --output json game-sheets --date 19-04-2026            # single date
sav --output json game-sheets --date-from 01-04-2026 --date-to 30-04-2026
sav --output json game-sheets --tier "Sub 14"
sav --output json game-sheets --competition "Ribas"        # name fragment, case-insensitive
sav --output json game-sheets --status Realizado           # played games only
sav --output json game-sheets --date 19-04-2026 --tier "Sub 14"
```

**Date format:** `DD-MM-YYYY`. `--date` is shorthand for setting both `--date-from`
and `--date-to` to the same value.

**`--status` is optional** — omit to see all statuses.

**JSON shape:** same as `sav games`.

---

### `sav game-sheet GAME_NUMBER`

Show the eligible players list or generate the **pre-game game sheet PDF** for
one or both teams.

`GAME_NUMBER` is the human-readable number from `sav games` or `sav game-sheets`
(e.g. `S14M-001`).

> **Not to be confused with the referee sheet.** The referee sheet is the
> post-game document uploaded by the referee — it is not accessible via the CLI.
> Use `get_game_sheet_pdf()` from the Python library for that.

#### List mode (default)

`--home` and `--away` are optional. When neither is given, both teams are shown.

```sh
sav game-sheet S14M-001                      # both teams, all sections
sav game-sheet S14M-001 --home               # home team only
sav game-sheet S14M-001 --away               # away team only
sav game-sheet S14M-001 --coaches            # coaches only, both teams
sav game-sheet S14M-001 --home --players     # players only, home team
sav game-sheet S14M-001 --players --coaches  # players + coaches, both teams
sav game-sheet S14M-001 --away --staff       # staff only, away team
```

**Always use `--output json` when reading the output programmatically.**

```sh
sav --output json game-sheet S14M-001 --home
```

Single-team JSON shape:
```json
{
  "game_number": "S14M-001",
  "players":     [{"licence": "301772", "name": "João Silva", "birth_date": "1990-05-12", "status": "FBP"}],
  "coaches_pri": [{"wallet": "44321", "name": "Carlos Coach", "grade": "...", "function": "..."}],
  "coaches_adj": [],
  "staff":       []
}
```

Both-teams JSON shape (no `--home` / `--away`):
```json
{
  "home": {"game_number": "S14M-001", "players": [...], "coaches_pri": [...], "coaches_adj": [...], "staff": [...]},
  "away": {"game_number": "S14M-001", "players": [...], "coaches_pri": [...], "coaches_adj": [...], "staff": [...]}
}
```

Section flags `--players`, `--coaches`, `--staff` apply to table output only; JSON always returns all keys.

#### PDF mode

Pass `--out` (and optionally `--player`, `--coach-pri`, `--coach-adj`) to generate
and save the pre-game game sheet PDF. **`--home` or `--away` is required in PDF mode.**

```sh
# All eligible, default filename game_S14M-001_home.pdf
sav game-sheet S14M-001 --home --out

# Custom path
sav game-sheet S14M-001 --home --out /tmp/lineup.pdf

# Specific players (licence numbers)
sav game-sheet S14M-001 --home --out --player 301772 --player 285943

# Specific head coach (wallet number)
sav game-sheet S14M-001 --home --out --coach-pri 44321

# Specific adjunct coach
sav game-sheet S14M-001 --home --out --coach-adj 55432
```

Defaults when generating a PDF:
- `--player` not set → all eligible players included.
- `--coach-pri` not set → all head coaches included.
- `--coach-adj` not set → all adjunct coaches included.
- OUTROS TREINADORES and ENQUADRAMENTO HUMANO are always excluded (no CLI flags for them).

**Important — coach pool vs PDF slot:** `coaches_pri` and `coaches_adj` from the
eligible list are eligibility pools, not exclusive slots. A coach who appears only
in `coaches_pri` can still be passed as `--coach-adj` and vice versa. Search both
pools when matching a name to a wallet number. Only reject if the coach is absent
from both pools entirely.

`--out` without a path saves as `game_<NUMBER>_<home|away>.pdf` in the current directory.

---

## Typical agent workflows

### Find a player by name

```sh
sav --output json players --name "João Silva"
```

If results are ambiguous, narrow with `--tier`, `--club`, or `--association`.
Use the `license` field from the result for follow-up calls.

### Look up a club ID from a name

```sh
sav --output json clubs "Rio Maior"
```

Use the `id` field in subsequent `sav players --club <id>` calls.

### Check whether a player is currently active

```sh
sav --output json players --license 301772
```

Look at `"active": true`. A player with `active: false` is registered in SAV
but not eligible for the current season.

### Generate a pre-game game sheet PDF

1. Find the game number (use `game-sheets` — it handles completed games correctly):
   ```sh
   sav --output json game-sheets --date 19-04-2026
   ```
2. Inspect eligible players and coaches (use `--coaches` to find wallet numbers):
   ```sh
   sav --output json game-sheet S14M-001 --home --coaches
   ```
3. Generate the PDF with selected players and coach:
   ```sh
   sav game-sheet S14M-001 --home --out /tmp/sheet.pdf \
     --player 301772 --player 285943 \
     --coach-pri 44321
   ```

---

## Error handling

All errors are printed to stderr and the process exits with a non-zero code.

| Error message pattern | Meaning |
|-----------------------|---------|
| `Authentication failed` | Wrong credentials or SAV2 unreachable |
| `No player found with licence …` | Licence does not exist in the current search scope |
| `No game found with number …` | Game number not visible to this account |
| `Game … has no internal ID` | The game row has no SAV2 action button; sheet unavailable |
| `No club found matching …` | Name fragment matched nothing; run `sav clubs` to browse |
| `Multiple associations match …` | Be more specific or use the numeric ID |
| `No eligible players PDF available` | SAV2 returned no eligible page for this team |
