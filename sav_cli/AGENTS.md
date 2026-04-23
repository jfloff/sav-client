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

## Error handling in JSON mode

When `--output json` is set, errors emit a structured payload **on stderr** instead of the usual `Error: …` text. Exit code is non-zero.

```json
{"error": "No player found with licence(s): '99999999'.", "code": "not_found"}
```

Branch on `code` for programmatic error handling:

| Code | Meaning |
|------|---------|
| `auth_failed` | Bad credentials |
| `connection_error` | Network / DNS / HTTP |
| `response_error` | Server returned malformed payload |
| `config_error` | Missing/invalid env vars |
| `not_found` | Licence/game/club/association didn't match |
| `ambiguous_match` | Multiple matches; be more specific |
| `no_internal_id` | Game row has no action button; sheet unavailable |
| `no_pdf` | SAV2 returned nothing for this team |
| `fetch_failed` | Could not load associations/clubs list |
| `error` | Generic fallback |

Usage errors (missing arg, invalid flag) still use Click's default formatter.

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
sav --output json clubs --association 7
sav --output json clubs "Rio Maior" --association 7
sav --output json clubs --all-associations
# [{"id": 270, "name": "Rio Maior Basket", "full_name": "...", "code": "RMB"}]
```

`id` feeds `sav players --club`. Exactly one scope is required: `--association` or `--all-associations`.

### `sav players`

Search players. All filters combinable.

```sh
sav --output json players --name "João" --club 270
sav --output json players --license 301772 --all-clubs
sav --output json players --status active --club 270
sav --output json players --tier "Sénior" --club 270
sav --output json players --tier "Mini 12" --tier "Mini 10" --all-clubs   # parallel, deduplicated
sav --output json players --club 270
sav --output json players --club "Rio Maior"                   # fragment may match >1
sav --output json players --club 270 --club 666                # multiple clubs
sav --output json players --association "Santarém"             # all clubs in it
sav --output json players --all-clubs                          # federation-wide (slow)
sav --output json players --season 0 --all-clubs               # all seasons
sav --output json players --birth-date 1990-01-01 --club 270
sav --output json players --limit 50 --all-clubs               # short-circuits wide scans
sav --output json players --tier "Sénior" --count --club 270   # {"count": 23} — skips the payload
```

Exactly one scope is required: `--club`, `--association`, or `--all-clubs`.
`--tier` and `--club` are repeatable. `--club` is exclusive with `--association` / `--all-clubs`. `--count` is exclusive with `--limit`.

`--status` filters on the parsed player eligibility state: `active`, `inactive`, or `all`. It is applied client-side using the `active` flag.
`--association` is one possible scope; use `--all-clubs` to search across all associations explicitly.

Results are sorted by `id` for reproducible `--limit` output across runs. (Note: on `--all-clubs` with `--limit`, the short-circuit stops fetching once N unique players are collected — the *set* may vary across runs due to network timing, but the returned list is always sorted.)

JSON element:
```json
{"id": 12345, "license": "301772", "name": "João Silva", "club": "Rio Maior Basket",
 "association": "AB Santarém", "tier": "Sénior", "gender": "Masculino",
 "birth_date": "1990-05-12", "nationality": "Portuguesa", "status": "FBP",
 "season": "2025/2026", "active": true, "photo_url": ""}
```

`license` is a string. Only `active: true` means currently eligible.

### `sav player LICENCE_NUM...`

Player detail for one or more licences. Multiple licences fetched in parallel. JSON/CSV always returns a **list**; a single licence yields a 1-element list.

```sh
sav --output json player 301772 --all-clubs
sav --output json player 301772 302000 303000 --all-clubs   # batch, parallel
sav --output json player 301772 --photo --club 270          # include photo_url
```

Exactly one scope is required: `--club`, `--association`, or `--all-clubs`.

Element shape matches a `players` row. Missing licences are logged to stderr as warnings; the command only errors (`code: not_found`) when **every** licence was missing.

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

**When reading programmatically, always pass `--home` or `--away`.** The both-teams `{home, away}` shape is for human inspection; splitting the call keeps the JSON shape flat and predictable.

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

**Find active player:** `sav --output json players --name "João Silva" --status active --all-clubs`. Narrow with `--tier` / `--club` if ambiguous. Pair with `--limit` if you only need one or two hits.

**Resolve club ID:** `sav --output json clubs "Rio Maior"` → use `id`.

**Generate game sheet PDF:**
```sh
sav --output json game-sheets --date 19-04-2026                    # 1. find game number
sav --output json game-sheet S14M-001 --home                       # 2. inspect eligible + wallets
sav game-sheet S14M-001 --home --out /tmp/sheet.pdf \              # 3. render
  --player 301772 --player 285943 --coach-pri 44321
```

## Errors

All errors go to stderr and exit non-zero. With `--output json`, they're emitted as structured JSON (see **Error handling in JSON mode** above).
