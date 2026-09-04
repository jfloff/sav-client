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

**Maintaining this file.** Add the entry in the same change that makes it,
not afterwards. The 0.90.0 section was reconstructed from `git log` and the
first attempt missed a fix and miscounted the breaking changes — the details
that make an entry useful (what silently changes, what to grep for) are the
ones that do not survive being remembered later.

---

## 0.95.0 — 2026-09-04

### Changed

**A Modelo 1 stamped at generation is now dated at generation**
`IMPACT: silent` — `render_mod1(values, club_stamp=...)` (CLI `sav mod1 fill
--club-stamp`, MCP `fill_mod1(club_stamp_b64=...)`) now also fills the
Assinaturas date with today's, so a stamped form is a dated form on the
generation path as it already was on the upload path. It no-ops when `values`
carried `data_assinatura` — `fill_signature_date` never overwrites a date
somebody else wrote — so only forms that were previously left blank change.

Before this, pre-stamping at generation produced forms that reached the FPB
**stamped but undated**, and nothing reported it: `submit_enrollment` uploads
the source PDF, `mod1_values_to_fields` emits no `carimbo_clube_presente`, so
the upload falls back to inspecting `CLUB_STAMP_RECT`, finds the stamp already
there, and `carimbo_overlay` returns `applied=None, effective=True` before the
`overlay_club_stamp` / `fill_signature_date` pair it owns. `has_club_stamp` was
`True` and the form was undated.

`DETECT:` `grep -rn "club_stamp=\|club_stamp_b64\|--club-stamp" .`
`FIX:` nothing to change if you want the date. If you were relying on a stamped
form staying undated, pass `data_assinatura` explicitly, or stop stamping at
generation.

### Deprecated

**Stamping at generation is discouraged, for the enrolment path and generally**
`IMPACT: silent` — no behaviour change; `club_stamp` still works. Two reasons
now documented on `render_mod1` / `fill_mod1` / `sav mod1 fill`:

- For an enrolment, leave the stamp off and let `submit_enrollment` (`sav
  enroll`) stamp and date the form as it files it — that path owns both, and
  pre-stamping only makes it skip its own step.
- `fill_mod1` output is distributable. A form carrying the club carimbo reads to
  the federation as club-endorsed, so a stamped form is an attestation, not a
  preview — never hand one to the member it is about.

`DETECT:` `grep -rn "club_stamp_b64\|--club-stamp" .`
`FIX:` drop the stamp from any member-facing or preview generation path.

---

## 0.94.0 — 2026-09-01

### Breaking

**`render_mod1` requires an explicit SAV season label**
`IMPACT: raises` — direct Python callers that omit `season=` now get a
`TypeError`. Pass the authoritative label returned by
`client.get_current_season().label`; do not derive it from the opaque epoch id
or the wall-clock year.
`DETECT:` `grep -rn "render_mod1(" .`
`FIX:` call `render_mod1(values, season=client.get_current_season().label)`.

### Changed

**Generated Modelo 1 forms use SAV's active Época**
`IMPACT: silent` — the bundled template no longer contains a pre-filled season,
and the CLI/MCP generation paths now fetch SAV's active season and write its two
years into the form. `fill_mod1` exposes no season parameter and rejects
season-like keys in `values`, so callers cannot generate a form for a stale or
fabricated season. `Season.end_year` and MCP `season_end_year` expose the second
year of the authoritative label.
`DETECT:` `grep -rn "fill_mod1\|mod1 fill\|season_start_year" .`
`FIX:` ensure generation has valid SAV credentials; remove any season-like key
from the values dict.

---

## 0.93.0 — 2026-08-29

### Changed

**Stamping a Modelo 1 now dates it** (`fill_signature_date`)
`IMPACT: silent` — the uploaded PDF gains a value it did not have.
Every path that overlays the club stamp (`carimbo_overlay`, so
`upload_player_document`, `replace_player_document`,
`update_enrollment_with_document`, `submit_enrollment`, and the CLI upload
paths) now also writes the Assinaturas "Data" line — `ass_dia` / `ass_mes` /
`ass_ano` — with the date it stamped. A form we stamp ourselves has nobody
left to date it by hand, so it used to reach the federation stamped but
undated.
Only a PDF carrying this repository's Modelo 1 AcroForm is touched; a scan has
no field to write. A date already on the form is **never** overwritten, and a
partly-filled date is left alone rather than completed. The stamp never fails
over the date: if the fill raises, the stamped-but-undated PDF is uploaded and
a warning is logged.
`DETECT:` `grep -rn "ass_dia\|data_assinatura" .`
`FIX:` nothing to change unless you asserted the date stays blank after an
upload. To choose the date yourself, keep passing `data_assinatura` in
`values` / `fill_mod1` — a form that already carries one is left as it is.

---

## 0.92.0 — 2026-08-29

### Breaking

**The NIF index has no scopes** (`863b83b`)
`IMPACT: raises`
`build_nif_index(scope=...)` and the `warm_nif_index` MCP tool's `scope`
parameter are gone, along with `scope` on `Cache.get_nif_index` /
`record_nif_index` / `clear_nif_index`. The index was keyed
`(club_id, scope)` with `scope in {"recent", "full"}`, and a partial
`"recent"` scan wrote a marker that later reads could not tell apart from
full coverage. There is now one marker per club asserting one thing: every
licence the club has ever held is indexed.
`DETECT:` `grep -rn "scope=[\"']\(recent\|full\)[\"']\|_nif_index(" .`
`FIX:` drop the argument. `build_nif_index()` is now always the exhaustive
scan. You do **not** need it to get the fast path — the narrowing that
`scope="recent"` used to give you now lives inside `find_license_by_nif`,
which needs no pre-warming. Pre-warm only to make a *miss* free.
The `nif_index` table is dropped and recreated on first open after upgrading:
one rebuild, and `license_nif` rows are untouched.

**`build_nif_index`'s result dict changed shape** (`863b83b`)
`IMPACT: silent` — **read this one.**
`players_indexed` used to report the *roster size*, counted before
already-indexed licences were filtered out, so a warm no-op build reported
the whole roster as freshly indexed. It now counts profiles that call
actually fetched and resolved. New keys `players_enumerated` (the roster
size), `no_nif`, `unresolved`, and `complete`; `scope` is gone from the dict.
**`no_nif` and `unresolved` are different answers** — see below.
`DETECT:` `grep -rn "players_indexed" .`
`FIX:` use `players_enumerated` for the roster size. Check `complete` before
treating a scan as authoritative — `warm_nif_index` also reports
`error: "incomplete_scan"` alongside the unresolved licences.

### Fixed

- **A NIF miss is no longer authoritative after a scan that lost profiles**
  (`863b83b`).
  `IMPACT: silent`, and the dangerous one. Profile fetches were swallowed on
  error and a blank NIF was dropped, but the freshness marker was stamped
  regardless — so `find_license_by_nif` returned an authoritative `None` for
  every dropped licence for the whole 7-day TTL. A caller that maps a miss to
  "new player" (`derive_enrollment_params` picks `reg_type = 1`) would create
  a duplicate SAV record for a player who already exists. Any unresolved
  licence now blocks the marker, and unresolved licences are logged at WARNING
  and returned.
- **A licence with no NIF on file is no longer re-fetched forever** (`863b83b`). Such a
  profile wrote no `license_nif` row, so `known_nif_licenses` never considered
  it known and every later build paid the profile POST again. It is now
  recorded with an empty NIF, which marks it scanned; `get_license_by_nif`
  refuses a blank query so those rows can never match.
  This is **not** treated as a failure. Measured on a live club, 143 of 684
  licences (21%) legitimately carry no NIF, so counting them as unresolved
  would make the coverage marker unwritable and every miss a permanent full
  rescan. A licence with no NIF is *covered* — it can never match a NIF query.
  Only a profile that could not be read lands in `unresolved` and blocks the
  marker.
- **`find_license_by_nif` stops as soon as the NIF resolves** (`863b83b`). It scans current
  season → previous → all seasons, fetching profiles concurrently and
  cancelling the rest on a match, instead of building an entire scope before
  re-probing. Work done before the match is persisted, so each call leaves the
  index warmer than it found it. This helps the *hit* path; a genuine miss
  still scans the club once, after which the coverage marker makes subsequent
  misses free.

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
