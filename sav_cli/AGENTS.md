# sav — Agent Reference

CLI client for the FPB SAV2 basketball management system. Authoritative reference for AI agents.

## Terminology

| Term | Meaning |
|------|---------|
| **game sheet** | Pre-game eligible players list. Generated via `sav game-sheet --out`. |
| **referee sheet** | Post-game document uploaded by the referee. **No CLI command** — use `get_game_sheet_pdf()` in the library. |
| **licence** | Player registration number (numeric string, e.g. `"301772"`). |
| **wallet** | Coach registration number. Distinct from licences. Used with `--coach-pri` / `--coach-adj`. |
| **val** | `1` = home, `2` = away. Exposed as `--home` / `--away`. |
| **coaches_pri / coaches_adj** | Head / adjunct (assistant) coaches eligible for the game. |
| **tier / escalão** | Age category, e.g. `"Mini 12"`, `"Sub 14"`, `"Sénior"`. Free-text. |
| **active** | `true` = registered and eligible this season. Only definitive eligibility signal. |
| **association** | Regional body (e.g. AB Santarém). Numeric `id` from `sav associations`. |
| **game_status** | `"Marcado"` (scheduled) · `"Realizado"` (played) · `"Não Marcado"` · `"Adiado"` · `"Anulado"`. |

## Setup

Credentials come from env vars or a `.env` file. Every command auto-authenticates.

```
SAV_BASE_URL   # optional, default https://sav2.fpb.pt
SAV_USERNAME   # required
SAV_PASSWORD   # required
```

## Output

Top-level flags (before the subcommand):

- `--output table` (default, human-only — **do not parse**)
- `--output json` — use this when reading programmatically
- `--output csv`
- `--fields "a,b,c"` — projects JSON/CSV down to listed keys. Unknown keys error with the full field list. Applies to `associations`, `clubs`, `players`, `player`, `games`. Ignored for tables and `game-sheet`.

```sh
sav --output json --fields "name,license,active" players --tier "Sénior"
```

Use `--fields` aggressively when feeding an LLM — payload shrinks from 12 keys × N rows to just what you need.

## Commands

### `sav associations`

Regional associations with IDs.

```sh
sav --output json associations
# [{"id": 7, "name": "AB Santarém"}, ...]
```

Feed `id` into `sav clubs --association`.

### `sav clubs [QUERY]`

Clubs, optionally filtered by case-insensitive substring against short name, full name, or code.

```sh
sav --output json clubs                        # own association
sav --output json clubs "Rio Maior"            # name fragment
sav --output json clubs --association 7
# [{"id": 270, "name": "Rio Maior Basket", "full_name": "...", "code": "RMB"}]
```

`id` feeds `sav players --club`.

### `sav players`

Search players. All filters combinable.

```sh
sav --output json players --name "João"
sav --output json players --license 301772
sav --output json players --tier "Sénior"
sav --output json players --tier "Mini 12" --tier "Mini 10"    # parallel, deduplicated
sav --output json players --club 270
sav --output json players --club "Rio Maior"                   # fragment may match >1
sav --output json players --club 270 --club 666                # multiple clubs
sav --output json players --association "Santarém"             # all clubs in it
sav --output json players --all-clubs                          # federation-wide (slow)
sav --output json players --season 0                           # all seasons
sav --output json players --birth-date 1990-01-01
sav --output json players --limit 50                           # short-circuits wide scans
```

`--tier` and `--club` are repeatable. `--club` is exclusive with `--association` / `--all-clubs`.

JSON element:
```json
{"id": 12345, "license": "301772", "name": "João Silva", "club": "Rio Maior Basket",
 "association": "AB Santarém", "tier": "Sénior", "gender": "Masculino",
 "birth_date": "1990-05-12", "nationality": "Portuguesa", "status": "FBP",
 "season": "2025/2026", "active": true, "photo_url": ""}
```

`license` is a string. Only `active: true` means currently eligible.

### `sav player LICENCE_NUM`

Single-player detail. Same JSON shape as a `players` element; add `--photo` to populate `photo_url`.

```sh
sav --output json player 301772 --photo
```

### `sav games`

Games for the authenticated club. Defaults to current season, `Marcado` status.

```sh
sav --output json games
sav --output json games --date-from 01-04-2025 --date-to 30-04-2025
sav --output json games --tier "Sub 14"
sav --output json games --status "Não Marcado"
sav --output json games --status all
```

Dates are `DD-MM-YYYY`.

JSON element:
```json
{"id": 98765, "number": "S14M-001", "competition": "...", "phase": "...", "round": "1",
 "date": "12-04-2025", "time": "10:00", "home": "Rio Maior Basket", "away": "Santarém BC",
 "home_score": "", "away_score": "", "venue": "...", "game_status": "Marcado",
 "result_status": "Sem Resultado", "tier": "Sub 14", "gender": "Masculino", "level": "Sub 14 F"}
```

`number` feeds `sav game-sheet`. `id` is internal; no CLI needs it.

### `sav game-sheets`

**Prefer this over `sav games` whenever you need `number` values** — especially for played games. SAV2 drops `Realizado` games from server-side date filters; this command fetches the full season and filters locally.

```sh
sav --output json game-sheets
sav --output json game-sheets --date 19-04-2026                       # single day
sav --output json game-sheets --date-from 01-04-2026 --date-to 30-04-2026
sav --output json game-sheets --tier "Sub 14"
sav --output json game-sheets --competition "Ribas"                   # fragment
sav --output json game-sheets --status Realizado
```

`--date` is shorthand for setting both bounds. `--status` optional (all by default). JSON shape matches `sav games`.

### `sav game-sheet GAME_NUMBER`

Eligible players list or **pre-game PDF**. `GAME_NUMBER` from `sav games` / `sav game-sheets` (e.g. `S14M-001`).

> Not the referee sheet — that's the post-game uploaded doc, library-only via `get_game_sheet_pdf()`.

**List mode** (no `--out`). `--home` / `--away` optional; both shown if omitted.

```sh
sav game-sheet S14M-001                      # both teams, all sections
sav game-sheet S14M-001 --home --players
sav game-sheet S14M-001 --away --staff
sav --output json game-sheet S14M-001 --home
```

Section flags (`--players`, `--coaches`, `--staff`) affect table output only; JSON always returns all keys.

Single-team JSON:
```json
{"game_number": "S14M-001",
 "players":     [{"licence": "301772", "name": "...", "birth_date": "...", "status": "FBP"}],
 "coaches_pri": [{"wallet": "44321", "name": "...", "grade": "...", "function": "..."}],
 "coaches_adj": [], "staff": []}
```

Both teams: `{"home": {...}, "away": {...}}`.

**PDF mode** — `--out` plus `--home` or `--away` (required).

```sh
sav game-sheet S14M-001 --home --out                               # all eligible
sav game-sheet S14M-001 --home --out /tmp/lineup.pdf               # custom path
sav game-sheet S14M-001 --home --out --player 301772 --player 285943
sav game-sheet S14M-001 --home --out --coach-pri 44321 --coach-adj 55432
```

Defaults: all eligible players / coaches included; OUTROS TREINADORES and ENQUADRAMENTO HUMANO always excluded (no CLI flags). `--out` with no path writes `game_<NUMBER>_<home|away>.pdf` in cwd.

**Coach pool vs PDF slot:** `coaches_pri` and `coaches_adj` are eligibility pools, not exclusive slots. A coach listed only in `coaches_pri` can still be passed as `--coach-adj`. Search **both pools** when matching a name → wallet; only reject when absent from both.

## Workflows

**Find active player:** `sav --output json players --name "João Silva"` → filter `active: true`. Narrow with `--tier` / `--club` if ambiguous.

**Resolve club ID:** `sav --output json clubs "Rio Maior"` → use `id`.

**Generate game sheet PDF:**
```sh
sav --output json game-sheets --date 19-04-2026                    # 1. find game number
sav --output json game-sheet S14M-001 --home                       # 2. inspect eligible + wallets
sav game-sheet S14M-001 --home --out /tmp/sheet.pdf \              # 3. render
  --player 301772 --player 285943 --coach-pri 44321
```

## Errors

Printed to stderr; non-zero exit.

| Pattern | Meaning |
|---------|---------|
| `Authentication failed` | Bad creds or SAV2 unreachable |
| `No player found with licence …` | Licence not in scope |
| `No game found with number …` | Game not visible to this account |
| `Game … has no internal ID` | Row has no action button; sheet unavailable |
| `No club found matching …` | Run `sav clubs` to browse |
| `Multiple associations match …` | Use numeric ID |
| `No eligible players PDF available` | SAV2 returned nothing for this team |
