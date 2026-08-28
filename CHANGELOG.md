# Changelog

Written for the coding agents that consume this package, not for release notes.

**How to use this file.** Find the version you are pinned to, then read every
entry above it. Each breaking entry carries:

- `IMPACT: raises` — your code will fail loudly. You will find these anyway.
- `IMPACT: silent` — your code keeps running and reads different data. **These
  are the dangerous ones. Start here.**
- `DETECT:` — a grep to run against *your* codebase to find affected call sites.
- `FIX:` — what to change them to.

Dates are ISO. Newest first.

---

## 0.91.0 — 2026-08-28

### Breaking

**Wizard prefill endpoints are no longer treated as write-acks** (`7c2a00e`)
`IMPACT: raises` — but it *unblocks* code that was already failing.
op=33 returns `val: 0` on a **successful** save, alongside the step-2 prefill.
Treating `val` as a universal success flag rejected every Revalidação at step 1.
`val` is now stripped from prefills entirely so it cannot be misread again.
`FIX:` none needed. If Revalidação was failing with
`"Registration step 1 failed"`, it now works.

**Player removal is verified** (`ca66d6e`)
`IMPACT: raises`
`remove_player_from_registration_batch` used to accept any non-rejecting
response. It now confirms the licence is actually gone from the batch, per
licence — not by batch item count, which a concurrent removal of a different
player would also satisfy.
`FIX:` handle `SavResponseError` (removal did not happen) and
`SavWriteUnverifiedError` (see 0.90.0 — do not auto-retry).

**`localidade_id` removed** (`4adfed6`)
`IMPACT: raises`
Note this parameter also existed on `add_player_to_registration_batch` **before
0.89.0**, so this is not merely reverting a same-release addition.
SAV's own form has no locality dropdown — it is free text that accepts a
nonexistent town — and a live Revalidação passing `localidade_id=1454` had it
silently ignored. A stored id is still carried forward on edits.
`DETECT:` `grep -rn "localidade_id" .`
`FIX:` use `localidade_txt`.

### Fixed

- Undersized overlay images report their real cause (`d829e3e`). A 1×1 PNG said
  *"Page size must be between 3 and 14400 PDF units"*; it now names the image.
- An eligible-players response carrying no `msg` raises instead of reporting
  "nobody is eligible" (`46ba543`).

### Internal

- Live tests carry `@pytest.mark.live`; the offline suite is
  `pytest tests/ -m "not live"` and genuinely runs with no network.
- CLI output assertions are colour-independent.

---

## 0.90.0 — 2026-08-27

### Breaking — silent

**`read_enrollment` returns an allowlisted DTO** (`195ffd3`)
`IMPACT: silent`
Was SAV's raw op=30 record — 23 wire keys, SAV's field names. Now 17 named
fields. Every `*_id` is an `int` (`0` when absent). Allowlisted, so a field SAV
adds later will not appear.

| Was | Now |
| --- | --- |
| `nome` | `name` |
| `nasc` / `datenasc` | `birth_date` (ISO) |
| `tipo` | `id_type` (int) |
| `numi` | `id_number` |
| `dataval` | `id_expiry` (ISO) |
| `tele` / `telef` | `telemovel` / `telefone` |
| `pai` / `mae` | `nome_pai` / `nome_mae` |
| `nacional` / `nacionalidade` | `nationality_id` (int) |
| `naturalidade` | `naturalidade_id` (int) |
| `estcivil` / `hab` / `profissao` | `marital_status_id` / `education_level_id` / `profession_id` (int) |
| `id`, `existe`, `atleta`, `numeroGuiaSaold` | **gone** — workflow-only |

`DETECT:` `grep -rnE '\["(nome|nasc|datenasc|numi|dataval|tele|telef|pai|mae|nacional|estcivil|hab|profissao)"\]' .`

**Game rows carry `status` / `status_raw` / `has_result`** (`1aa62d8`)
`IMPACT: silent` — `game_status` is gone from both listing tools.

| SAV label | `status` |
| --- | --- |
| `Marcado` | `scheduled` |
| `Realizado` | `played` |
| `Não Marcado` | `not_scheduled` |
| `Adiado` | `postponed` |
| `Anulado` | `cancelled` |

An unrecognised label yields `status: "unknown"` **plus** `status_raw`, so a new
upstream state cannot masquerade as a known one. `has_result` is separate and
true only when both scores parse.
Why it matters: `list_games` previously derived status from score presence
alone, so a **cancelled** fixture reported `"scheduled"`. Anything reading
"scheduled" as "this match is happening" was wrong.
`DETECT:` `grep -rn "game_status" .`

**`player_id` gone; players identified by licence** (`51c55aa`)
`IMPACT: silent`
- `submit_enrollment` — `player_id` removed (it already returned `license`)
- `update_enrollment`, `create_enrollment_manual` — return `license`
- duplicate guard — `existing_sav_id` → `existing_license`

`existing_license` **can legitimately be `null`**: SAV discloses a NIF-matched
player only to their own club, so a duplicate at another club cannot be
resolved. The `reason` then tells you to search by name or NIF. It never falls
back to the internal id.
`DETECT:` `grep -rnE "player_id|existing_sav_id" .`

**`Player.license` is `int`, not `str`** (`0627ff7`)
`IMPACT: silent`
If you stored `"301772"` from a search and compared it against a numeric licence
elsewhere, that comparison silently failed before.
`DETECT:` `grep -rn "str(.*license\|license.*==.*\"" .`

### Breaking — raises

| Change | Now |
| --- | --- |
| `list_game_sheets` filter (`1aa62d8`) | Canonical values only; `Realizado` etc. rejected. `""` still means all. |
| Dates (`c80b544`) | Writes enforce `YYYY-MM-DD`; read-only filters still accept `DD-MM-YYYY`. All emitted dates are ISO. |
| `exam_date` (`88524a3`) | Must be past and ≤ 12 months old. SAV rejects a future date with an **empty** `msg`, so this is checked client-side where the error is readable. |
| Enrollment overrides (`a4e929b`) | Consents must be real booleans. `"false"` used to be truthy and wrote the opposite of the intent. |
| Unresolvable club side (`3f96985`) | Raises rather than assuming home; `list_games` returns a `{source_id, error}` row for that fixture. |

### Added

Two exception types, both subclassing `SavResponseError` so existing handlers
still catch them:

- **`SavServerError`** — SAV answered HTTP 200 with a PHP fatal. The body is
  withheld deliberately (it carries SAV's internal table and constraint names);
  enable DEBUG on the `sav_client` logger to see it.
- **`SavWriteUnverifiedError`** — a write was sent and **may** have succeeded,
  but the confirming read failed. Neither success nor failure.
  **Do not auto-retry** — check SAV first, or risk double-enrolling.

### Fixed — no action needed

- **Exam-date edits preserve step-3 state** (`868c10c`). Omit a field to keep
  its stored value; pass an explicit value — including `""`, `false`, `0` — to
  overwrite. Previously an exam-date-only edit re-sent defaults, turning
  consents on and dropping any inline promotion.
- **1ª Inscrição licence resolution** (`e468d2d`) uses a batch diff, not a name
  match. With two same-named players in a batch the old code attached documents
  to the wrong one — and since uploads *replace* by type, that destroyed their
  Modelo 1 and medical exam.
- **Registration document types read correctly** (`3ec5e95`). `deleteDoc()`'s
  4th argument is `agente`, always `1` — not the document type. Every document
  therefore reported as `fpb_modelo_1`, and `replace_*` deleted **all** of a
  player's documents instead of the one type requested.
- **Subida commits are verified** by postcondition (`b23d2ea`) rather than by a
  response body with no reliable success contract.
