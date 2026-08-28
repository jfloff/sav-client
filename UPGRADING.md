# Upgrading sav-client 0.88.0 → 0.91.0

Fourteen breaking changes across two releases. They are grouped below by how
they will reach you, because that is what determines the work.

**Read §1 first.** Those three change response *shape* without raising, so
existing code keeps running and quietly reads the wrong thing.

---

## 1. Silent — code keeps running, data is different

### `read_enrollment` returns an allowlisted DTO (`195ffd3`)

It used to return SAV's raw op=30 record: all 23 wire keys, SAV's own field
names, workflow-only values. It now returns 17 named fields.

| Was (SAV wire) | Now |
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
| `estcivil`, `hab`, `profissao` | `marital_status_id`, `education_level_id`, `profession_id` (all int) |
| `id`, `existe`, `atleta`, `numeroGuiaSaold` | **removed** — workflow-only |

Every `*_id` is an `int`, `0` when absent. It is an allowlist, so a field SAV
adds later will not appear automatically.

### Game rows carry `status` / `status_raw` / `has_result` (`1aa62d8`)

`game_status` is gone from `list_games` and `list_game_sheets`.

| SAV label | `status` |
| --- | --- |
| `Marcado` | `scheduled` |
| `Realizado` | `played` |
| `Não Marcado` | `not_scheduled` |
| `Adiado` | `postponed` |
| `Anulado` | `cancelled` |

An unrecognised label gives `status: "unknown"` **plus** `status_raw`, so a new
upstream state cannot masquerade as a known one.

`has_result` is separate and true only when both scores parse. This split
matters: a cancelled fixture used to report `"scheduled"` from `list_games`
because that tool derived status purely from score presence. Anyone treating
"scheduled" as "this match is happening" was wrong for cancelled and postponed
games.

### `player_id` is gone; `license` identifies players (`51c55aa`)

- `submit_enrollment` — `player_id` removed (it already returned `license`)
- `update_enrollment`, `create_enrollment_manual` — return `license`
- `_resolve_primeira_player` duplicate error — `existing_sav_id` → `existing_license`

`existing_license` is resolved from the form's NIF. **It can legitimately be
`null`**: SAV only discloses a NIF-matched player to their own club, so a
duplicate at another club cannot be resolved. The `reason` text then tells you
to search by name or NIF. It never falls back to the internal id.

Also `Player.license` is now an `int`, not a `str` (`0627ff7`). If you stored
`"301772"` from a search and compared it against a numeric licence elsewhere,
that comparison silently failed before and will now succeed.

---

## 2. Loud — you will get an exception

| Change | What now raises |
| --- | --- |
| `list_game_sheets` filter (`1aa62d8`) | Canonical values only. `Realizado` and the other Portuguese labels are rejected; use `played` etc. `""` still means all. |
| Dates (`c80b544`) | Write paths enforce `YYYY-MM-DD`. Read-only filters (`list_games` windows) still accept `DD-MM-YYYY`. All emitted dates are ISO. |
| `exam_date` (`88524a3`) | Must be in the past and ≤ 12 months old. SAV computes validity as exam + 12 months and rejects a future date with an **empty** `msg`, so this is checked client-side where the error is legible. |
| Enrollment overrides (`a4e929b`) | Consents must be real booleans. `"false"` used to be truthy and wrote the opposite of what you asked. |
| Unresolvable club side (`3f96985`) | `list_games` raises rather than assuming the club is the home side. A single bad fixture becomes a `{source_id, error}` row instead of silently swapping scores and opponent. |
| `localidade_id` (`4adfed6`) | Parameter removed — see §3. |

---

## 3. `localidade_id` removal — wider than it looks

`4adfed6` reads like it reverts a change from the same release. It does not.
`localidade_id` also existed on `add_player_to_registration_batch` — the
1ª Inscrição path — **before any of this work**, so this removes a
pre-existing public parameter.

Practical risk is low: the default was `0`, which already sent `""`. Only a
caller passing it explicitly is affected, and they now get a `TypeError`.

Why: SAV's own form has no locality dropdown. It is free text that accepts a
town which does not exist, and a live Revalidação passing `localidade_id=1454`
had it silently ignored. A stored id is still carried forward on edits; only
the ability to set one was removed. Use `localidade_txt`.

---

## 4. New exceptions to handle

Both subclass `SavResponseError`, so existing handlers still catch them.

- **`SavServerError`** — SAV answered HTTP 200 with a PHP fatal. The raw body is
  withheld on purpose (it carries SAV's internal table and constraint names);
  enable DEBUG on the `sav_client` logger to see it.
- **`SavWriteUnverifiedError`** — a write was sent and *may* have succeeded, but
  the read that would confirm it failed. **Neither success nor failure. Do not
  auto-retry** — check SAV first, or you risk double-enrolling. Raised by the
  standalone Subida commit (`b23d2ea`) and player removal (`ca66d6e`).

---

## 5. Behaviour that is now safer (no action needed)

- **Exam-date edits preserve step-3 state** (`868c10c`). Omit a field to keep
  its stored value; pass an explicit value — including `""`, `false`, `0` — to
  overwrite it. Previously an exam-date-only edit re-sent defaults, turning
  consents on and dropping any inline promotion.
- **1ª Inscrição licence resolution** (`e468d2d`) uses a batch diff instead of a
  name match. With two same-named players in a batch, the old code attached the
  new player's documents to the wrong one — and because uploads *replace* by
  type, that overwrote their Modelo 1 and medical exam.
- **Subida and removal are verified** (`b23d2ea`, `ca66d6e`) by checking the
  postcondition rather than trusting a response body that carries no reliable
  success contract.
- **Wizard prefill endpoints are no longer treated as write-acks** (`7c2a00e`).
  `val` is not a universal success flag: op=36 uses `val:1` for success while
  op=33 returns `val:0` on a *successful* save. Treating it as a verdict blocked
  every Revalidação at step 1.
