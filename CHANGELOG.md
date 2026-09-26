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

## 0.114.2 — 2026-09-26

### Fixed

**Passport and other doc numbers ignore case and separators**
`IMPACT: silent` — `identify_player` compared non-CC doc numbers as literal
strings, so `ejg252806` vs `EJG252806`, or `EJG 252 806`, was reported as an
`id_number` conflict. Case and separators (spaces, dots, dashes, slashes) no
longer count; every character still does, so a different document is still
reported (verified live: `E2203397` vs `N1F62X3D0`). The same applies when
SAV's document type is unknown. The Cartão de Cidadão rule is unchanged.
`DETECT: grep -rn "\.upper()\|\.lower()\|replace(\" \"" <your code>` — a local
normalisation of doc numbers before calling `identify_player` can go.

---

## 0.114.1 — 2026-09-26

### Fixed

**Cartão de Cidadão numbers compared as the civil number, not the literal string**
`IMPACT: silent` — `identify_player` reported an `id_number` conflict for what
is one document: the club holds the 8-digit civil number (`15932997`), while
older SAV records hold the full card number with its check digit and version,
spaced inconsistently (`15932997 3ZW6`, `305439731 ZW4`, `30927726 4 ZX1`;
verified live on 7 licences). For SAV `tipo` 1 the 8-digit civil numbers are now
compared; the card's check digit and version change on renewal. Passports and
residence permits still compare whole, and an unreadable document type stays
conservative (the conflict is kept). Real differences are still reported
(verified live: `E2203397` vs `N1F62X3D0`).
`DETECT: grep -rn "canonical_id_number\|first 8 digits\|id_number" <your code>`
— a local workaround that reduces CC numbers before comparing, or drops
`id_number` conflicts, can go.

**A doc number that finds nobody no longer vetoes name + birth date**
`IMPACT: silent` — SAV's doc-number search is an exact string match, so
`identify_player(name, birth_date, id_number)` whose number SAV spells
differently (or doesn't hold) returned `null` — "no player" — for a player the
name and birth date found. Now the number only adds evidence: it is checked
against the answer's profile and reported in `conflicts` when it differs.

**`id_number` alone that finds nobody: `identity_unverifiable` for a CC number**
`IMPACT: silent` — a CC civil number cannot find a record stored as the full
card number, so a miss proves nothing; it answered `null`. It now answers
`{error: "identity_unverifiable"}`. A passport that finds nobody is still `null`.
`DETECT: grep -rn "identify_player(id_number" <your code>` — pass name + birth
date with the number.

---

## 0.114.0 — 2026-09-25

Lookups whose scope was narrower than their flow. The key case is a
Revalidação athlete: by definition they have no row in the current season, so
any current-season search for them comes back empty (verified live, licence
315784, last row 2025/2026).

### Fixed

**Inline subida on a Revalidação always failed ("Player with licence … not found in SAV")**
`IMPACT: raises` → now succeeds. `add_enrollment` with a `mod4_id` looked the
player's gender up in SAV with a current-season search. It now takes the gender
it already holds: the lote's (gender-keyed, SAV's own record), checked against
the Modelo 1 artifact's `gender_id` — a disagreement raises `ValueError`
instead of resolving the subida against the wrong gender's tiers.
`DETECT: grep -rn "not found in SAV" <your code>` — a workaround for this error
(e.g. deleting the lote and retrying without the Modelo 4) can go.

**`gender_id_for_license` removed; the gender comes from the row already in hand**
`IMPACT: raises` for library callers. It re-asked SAV for a gender the caller
already held, with a current-season search (so a player not enrolled this
season was "not found"), and silently answered Masculino for an unknown label.
Now:
- `resolve_subida_player` returns a 5th element, the SAV row it matched on —
  `resolve_subida_target` and the CLI read the gender from it (no lookup);
- a licence typed into the CLI by hand is looked up with
  `sav_shared.players.resolve_license_row`, the same current → previous → all
  seasons ladder `lookup_player` / `get_player` use (moved from sav-mcp);
- `sav_shared.players.gender_id_of(row)` raises on an unrecognised gender.
`DETECT: grep -rn "gender_id_for_license\|= resolve_subida_player(" <your code>`
`FIX: unpack five values; read the gender with gender_id_of(player)`.

**`load_player_profile` on a cold cache**
`IMPACT: raises` → now succeeds. Resolving a licence to SAV's internal id used
a current-season search, so a Revalidação athlete whose id was not cached
raised "No player with license …" (verified live). Callers:
`preview_enrollment`, `update_enrollment_with_document`, `get_player_profile`,
`add_subida_enrollment`'s name, and the document checklist — which silently
fell back to the stricter foreign-born list.

**Revalidação name fallback (`resolve_player`, CLI)**
`IMPACT: silent` — when the Modelo 1 has no usable licence, the name search
that offers eligible candidates searched the current season only, so it could
not find the Revalidação athletes it exists for. It now searches every season;
the eligible-list filter still decides.

**1ª Inscrição duplicate hint (`resolve_player`, `preview_enrollment`)**
`IMPACT: silent` — SAV's op=11 detects a duplicate federation-wide, but the
licence behind `existing_license` was looked up in your own club only. The
doc-number / birth-date searches are now federation-wide (the NIF stays own
club), so a player registered at another club gets their licence named.

### Changed

**`identify_player`: a NIF match is never vetoed by another key**
`IMPACT: silent` — a supplied `name`, `birth_date` or `id_number` that disagreed
with SAV turned a clear NIF match into `null` ("no player", i.e. a duplicate 1ª
Inscrição). Verified live: a passport number SAV never stored, and a birth-date
typo. Now the NIF's player is returned with
`conflicts: [{key, given, on_file}]`; if the other keys describe a *different*
person, the answer is `ambiguous` with both. `matched_by` on a found player
lists only the keys that agree — `"nif"` is gone when `nif_on_file` is
`"different"`.
`DETECT: grep -rn "identify_player" <your code>` — a workaround that withholds
`id_number` or re-asks NIF-only after a `null` can go; read `conflicts` instead.

### Performance

**Fewer SAV requests for the same answers**
`IMPACT: none` — every response is unchanged; only the requests behind it
shrink (verified live by counting them):
- `lookup_player` / `identify_player` with `with_details` + `with_profile`
  read the op=2 player page once, not twice (~60 KB each):
  `SavClient.get_player_detail_and_profile(player_id)`.
- `identify_player(with_details=true)` reads the detail page by the matched
  row's id instead of searching the licence again.
- `add_subida_enrollment` takes the returned `name` from the lote row instead
  of loading the op=2 profile.
- `get_enrollment_status` for an enrolled player reads the nationality from
  the roster row it already fetched (op=2 only when the label does not resolve
  to a known country); for a player in a lote it reuses the lote row the batch
  resolver just read (a 5-second memo, cleared on every write).

### Added

**`add_enrollment` reports the op=21 subida offer**
`IMPACT: additive` — with an inline subida, the success response carries
`subida: {offered: [{tier_id, name}], committed: {tier_id, name}}`, and every
op=21 decision is logged at INFO. `sav_client.sav_client.record_subida_picks()`
collects the same records for library callers.

---

## 0.113.0 — 2026-09-25

A NIF is not one person. On one real club 5 NIFs sit on 147 licences: the
placeholder `999999990` (SAV accepts it for players without a NIF) on 120
different people, adults' NIFs entered for 10–13 children, and one athlete
registered twice with two licences. The NIF lookup returned the **lowest**
licence for a shared NIF (`SELECT … LIMIT 1`), which picked an athlete's stale
licence, and it also decided Revalidação vs 1ª Inscrição and recovered a missing
licence from a Modelo 1 without checking the name. This release resolves a
**person** instead, and says so when it cannot.

### Removed

**`find_player_by_nif` (MCP tool), and `lookup_player(nif=…)`**
`IMPACT: raises` — the tool no longer exists, and `lookup_player` is now
**licence only** (`license` required; `nif` is no longer a parameter).
`DETECT: grep -rn "find_player_by_nif\|lookup_player(.*nif" <your code>`
`FIX: identify_player(nif=…, birth_date=…, name=…)`, then `lookup_player` /
`get_player` with the licence it returns. Pass the birth date whenever you have
it: it is what separates siblings sharing a parent's NIF.

**`SavClient.find_license_by_nif`, `Cache.get_license_by_nif`, `sav_shared.enrollment.find_player_license_by_nif`**
`IMPACT: raises`.
`DETECT: grep -rn "find_license_by_nif\|get_license_by_nif\|find_player_license_by_nif" <your code>`
`FIX: SavClient.resolve_player_identity(nif=…, birth_date=…, name=…)` for a
person, or `find_licenses_by_nif` / `Cache.get_licenses_by_nif` for every
licence carrying the NIF. `resolve_player_from_form(parsed, client)` replaces
the Modelo 1 wrapper.

### Changed

**`identify_player` returns a person, or says it cannot**
`IMPACT: silent` for code that treats any non-null result as a player.
- One person on several licences → the **newest** licence, plus
  `other_licenses: [{license, season, active}]`. Same person = same name **and**
  birth date.
- Several people → `{ambiguous: true, candidates: [...], matched_by}`.
- Incomplete evidence → `{error: "identity_unverifiable", ...}`: never read it
  as "new player".
- **SAV's NIF can be missing or wrong, so the NIF is evidence, not a filter.**
  A NIF match outranks everyone else. When no one carries the NIF, the other
  keys still find the player. `nif_on_file` is the only NIF field on a found
  player: "match" (confirmed), "different" (SAV holds another NIF — the
  placeholder 999999990, or another real NIF, verified live; a real one is
  accepted only when name + birth date or the doc number matched), "none" (no
  NIF on file) or "unknown" (never read, or at another club). The placeholder
  identifies no one: given alone it answers `null`. A NIF-only miss is
  therefore **not** proof of a new player: look the person up by name + birth
  date first.
- `status` judges the chosen licence instead of filtering before the pick.
`DETECT: grep -rn "identify_player" <your code>` — check each caller handles
`ambiguous` and `error` before reading `license`.

**NIF lookups no longer stop at the first hit**
`IMPACT: silent` (latency). A NIF lookup must find every licence carrying the
NIF, so without a fresh coverage marker the first one on a cold cache scans the
whole club once (minutes). Run `warm_nif_index` from a nightly job if you look
players up by NIF interactively.

**Modelo 1 flow (`derive_enrollment_params`, eligible-licence recovery, CLI)**
`IMPACT: raises` — with neither Revalidação nor 1ª Inscrição ticked, an
identity that matches several people (or cannot be verified) now raises
`ValueError` asking for the box, instead of silently choosing Revalidação on
the lowest licence. Licence recovery never auto-picks from an ambiguous
identity; it offers the eligible candidates instead.

**1ª Inscrição duplicate (`resolve_player`, `preview_enrollment`)**
`IMPACT: silent` — the "existing licence" hint comes from the identity resolver
(NIF + OCR'd birth date + doc number). When several players match, it is
`null` and the new `existing_license_candidates` lists their licences.

### Added

- **`identify_player(nif?, id_number?, name?, birth_date?, club_id?)`** (MCP):
  find a player from who they are — any usable combination of keys. Parents and
  players self-scope on `nif`; the other keys are not wrapper-verified subjects
  (an accepted trade-off, documented in `authz.toml`). `club_id=0` makes the
  doc-number / birth-date searches one federation-wide request (verified live:
  both match exactly across every club).
- **`SavClient.resolve_player_identity`**, **`find_licenses_by_nif`**,
  `IdentityMatch`, `NifLicenses`, `Cache.get_licenses_by_nif`, and
  `sav_shared.identity` (`is_placeholder_nif`, `names_match`,
  `group_same_person`).
- **`search_players(birth_date=…)`**: exact ISO birth date (`nr_dtnasc`). A
  `club=0` search by `number` or `birth_date` is now one request instead of a
  fan-out over every club. The `number` parameter was always the doc. ident.
  number; its docstring called it a shirt number.
- **`warm_nif_index`**: `shared_nif_groups` / `licenses_on_shared_nifs` (counts
  only; no NIFs or licences, because parents and players may call it).

---

## 0.112.3 — 2026-09-25

### Fixed

**`open_batches` on a not-enrolled answer offered lotes that had already been submitted**
`IMPACT: silent`. `read_enrollment`, `get_enrollment_status`,
`list_player_documents`, `download_player_document` and the CLI search every
in-flight lote for the licence ("Em Validação", "Em Pagamento", "Devolvida"
too). They also built the `open_batches` they offer a not-enrolled player from
that same wide list, so they suggested adding the player to lotes that had
left the club and accept no one. The search stays wide; `open_batches` now
lists only "Em construção" lotes, matching `enrollment_status_bulk` (fixed
the same way in 0.102.2). Verified live: of five lotes, four in "Em
Pagamento", only the open one is offered now.
`DETECT: grep -rn "open_batches" <your code>`. If you picked a lote from it
to add a player, you may have been handed one SAV would refuse.

---

## 0.112.2 — 2026-09-25

### Fixed

**Subida lotes read back as empty, so a successful standalone Subida add was reported as a failure**
SAV renders a Subida lote's items with an `editSub(...)` button (other lotes
use `editJogador(...)`) and emits their cells into `<tbody>` with no `<tr>`.
The row parser matched only `editJogador`, so **every Subida lote read as
empty**. Verified live on lote 335: op=50 answered `{"val":1,"msg":""}`, the
lote listing counted 1 player, and the client still raised "licence … is not
present in batch … after the commit".

- `add_subida_enrollment` — `IMPACT: raises`. It no longer raises after a
  successful add. If SAV acknowledges the add (`val == 1`) but the lote
  doesn't list the licence, it now raises `SavWriteUnverifiedError` (a
  `SavResponseError`): do not retry. Failure messages now include op=50's
  `val` and `msg`; a non-JSON or PHP-fatal body is described, never quoted.
  **If you retried, or deleted the lote, after the old error, check SAV:** the
  add had most likely gone through.
  `DETECT: grep -rn "is not present in batch" <your code>`
- `list_batch_enrollments` — `IMPACT: silent`. It returned `[]` for every
  Subida lote; it now returns the rows.
  `DETECT: grep -rn "list_batch_enrollments" <your code>`
- `enrollment_status_bulk` / `get_enrollment_status` — `IMPACT: silent`. A
  player in a Subida lote was never found in it, so they read `"enrolled"` or
  `"not_enrolled"`; they now read `"pending"`, with `batch.type_id == 4` and
  `subida.status == "pending"`. For a Subida lote, `get_enrollment_status` no
  longer calls op=30 (SAV fatals on it there), and its checklist carries no
  `nationality_source`.
  `DETECT: grep -rn "enrollment_status_bulk\|get_enrollment_status" <your code>`
- `delete_batch` — `IMPACT: raises`. It now also refuses when the lote
  listing's own player count is higher than the rows it can read. The
  "empty lote" guard relied on the same parser and let a Subida lote holding a
  player be deleted, which strands that player.
- Subida-lote `subida` — `IMPACT: silent`. `tier_to` falls back to the lote's
  escalão (a Subida lote is keyed by the destination; confirmed by the club,
  and the live row reads `"Sub 14"` on a Sub 14 lote). `tier_from` stays `null`.
- `get_player(with_details=true)` — no code change, but it is now verified that
  a player in an "Em construção" lote reads `subida.status == "none"` there.
  Read `enrollment_status_bulk` first, as `sav_mcp/AGENTS.md` describes.
- The Subida eligibility check read every `<option>` in op=48's body,
  including the fee `<select>`. A fee id (e.g. 1096) passed as an "eligible
  licence". It now reads only the player list.

---

## 0.112.1 — 2026-09-24

### Fixed

**`rich` is now a declared dependency**
`IMPACT: none` for MCP and library callers. `sav_cli` imports `rich`, but the
package never declared it, so a fresh install could not import the CLI or
collect its tests. Environments that already had `rich` are unaffected.

---

## 0.112.0 — 2026-09-24

### Added

**`subida` on lote rows: `enrollment_status_bulk`, `get_enrollment_status`, `list_batch_enrollments`**
`IMPACT: additive`. The rows these return for a player in a lote now carry the
same `subida: {status, tier_from, tier_to, approved_on}` as `get_player`.
It comes from the "Subida" column of SAV's lote detail (op=10), which these
calls already fetched. Status rows gain it only when `"pending"`;
`list_batch_enrollments` rows always. `sav_client` item dicts from
`list_player_registration_batch_items` gain a `subida` key, and
`SavClient.batch_item_subida(batch_id, license)` is new.

- Covers every in-flight state, **"Em construção" included**. That closes the
  gap `get_player`'s `"none"` documents for players in an open lote.
- `"pending"` when the row names a promoted escalão (`tier_from` is the
  lote's escalão), or whenever the lote is a Subida lote; `"none"` when blank;
  `"unknown"` when the row can't be read. `approved_on` is always `null`.
- Verified live on "Em Pagamento" Revalidação lotes. Not yet seen: a filled row
  in an open lote, and any row in a Subida lote, so `tier_from` stays `null`
  there until the lote's escalão is confirmed as origin or destination.

---

## 0.111.0 — 2026-09-24

### Added

**`subida` on every player read with `with_details=true`**
`IMPACT: additive`. `search_players`, `get_player` and `find_player_by_nif`
rows gain `subida: {status, tier_from, tier_to, approved_on}`: whether a
Subida de escalão is on file for the **current** season, by either route
(inline on a 1ª Inscrição/Revalidação, or a standalone Subida lote). It is
parsed from the "Inscrições" tab of the `jogadoresdb.php?op=2` page, which
`with_details` already fetched, so there is no extra request per player. The
only addition is the season table, read once per client. `sav_client.Player` gains
`subida: SubidaStatus | None`. The CLI's JSON output for `players`/`player`
gains a `subida` key: `null` without `--with-details`.

- `status` is `"approved"`, `"pending"` (filed, lote not yet approved),
  `"none"`, or `"unknown"`. **Never read `"unknown"` as `"none"`**: it covers
  an unreadable page and players whose last approved registration is at
  another club (SAV renders them a reduced history).
- `"none"` is unverified for a lote still "Em construção". Cross-check
  `enrollment_status_bulk` before trusting it for a player in an open lote.
- **`tier` is the base escalão of a promoted player, not the destination.**
  Verified live: 18 of one club's 83 approved athletes this season are
  promoted, and every one reads its pre-promotion `tier`. `subida.tier_to` is
  the escalão they play in. `tier` itself is unchanged; if you read the
  playing escalão from it, it was already wrong for promoted players.
- A current-season `search_players` returns only *approved* registrations,
  so pending athletes are absent from it rather than reported `"none"`.

---

## 0.110.0 — 2026-09-21

Pin moved to sav-parsers 0.11.2 (`43ccf15`), which added four identity
entities to the `exame_medico` processor. This surfaces them, plus everything
else `parse_em` already returned and this package was throwing away.

### Added

**Every `exame_medico` entity now reaches the row, with its confidence and its
bounding box**
`IMPACT: additive` — nothing that existed changed value or disappeared. An
`exame_medico` entry from `parse_enrollment_forms`, and
`preview_enrollment`'s `medical_exam` block, now also carry:

- `athlete_name`, `doc_number`, `exam_number` — the examined athlete's name,
  their ID/passport number, and the exam's serial number.
- `birth_date` + `raw_birth_date` — mirrors `exam_date` exactly: strict
  `YYYY-MM-DD` or `None`, with whatever the OCR read landing in
  `raw_birth_date` when it is not a usable ISO date.
- `doctor_validation_present` — previously read from the parse and dropped.
- a `<field>_confidence` and a `<field>_bbox` for each of the six, the bbox
  serialized as `{"page": int, "vertices": [[x, y], …]}` or `null`.

`DETECT:` grep for `medical_exam_id`, `parse_enrollment_forms` and
`preview_enrollment` call sites that compare the returned row with `==`
against a literal dict — those break on the added keys. Reads of individual
keys are unaffected.
`FIX:` compare the keys you care about, or extend the literal.

**`doc_number` and `exam_number` are emitted exactly as printed.** No
digit-coercion, no space-stripping, no date-parsing. A Portuguese card prints
`31727922 0 ZY2` and a foreign athlete carries a passport (`GF741951`) —
normalizing breaks both. `2218/2025` is a serial; its `/2025` half is not a
year.

**`needs_review` did not change meaning.** It is still exactly
`exam_date is None`. A missing or low-confidence identity field does not set
it — those fields are context for the caller to judge, and an exam with no
`athlete_name` still submits fine. If you want to gate on them, test them
yourself.

## 0.109.1 — 2026-09-21

### Fixed

**`create_enrollment_manual` validated its `estatuto` the way `add_enrollment`
already did**
`IMPACT: raises` — this path splatted the caller's `estatuto` straight to the
client, so `12` (`Equiparado FBP`) and plainly invalid ids reached SAV here
while being refused one function away. It now runs the same
`_require_submittable_estatuto` check and raises `ValueError` instead.
`DETECT:` grep your code for `create_enrollment_manual` calls passing
`estatuto=`.
`FIX:` pass 6, 10 or 11, or leave it unset and let SAV resolve it. `12` is an
official status FPB assigns, with no box on the Modelo 1; it reaches an
enrolment only by already being on the player's SAV record.

## 0.109.0 — 2026-09-18

0.108.0 taught this package SAV's Estatuto rule. This removes everything that
existed to second-guess it.

SAV's type-1 wizard reads the `mini` flag on its op=151 response — `mini == 1`
→ 6 (FBP), otherwise 10 (Sem FBP Comunitário) — and **disables the Estatuto
select unless the session is a federation profile** (`perfil == 2`). This
package logs in as a club (`perfil: 4`), so SAV decides and no operator here can
choose. The OCR/nationality decision engine, its review prompts, its conflict
field and the refusal to commit all existed to support a choice SAV does not
accept from us — and the engine placed Portugal on the community list, so it
resolved a Portuguese child to the one estatuto with no fee in a Mini batch and
killed the enrolment *after* the person record had been created.

### Removed

**The Estatuto decision engine**
`IMPACT: raises` — `sav_shared.estatuto` no longer exports `EstatutoDecision`,
`resolve_estatuto`, `apply_sav_batch_rule`, `ESTATUTO_OCR_ENTITY` or any
`SOURCE_*` constant. Importing them raises `ImportError`.
`DETECT:` `grep -rn "resolve_estatuto\|EstatutoDecision\|SOURCE_MODELO1" .`
`FIX:` read `estatuto_decision` off the preview instead, or call
`SavClient.primeira_estatuto_for_batch(batch)` for SAV's answer directly.
**Kept in the same module**: `LOW_CONFIDENCE`, `is_portuguese_nationality`,
`resolve_country`, `nationality_branch` and the FPB country lists. They serve
the `nationality_id` review, which is a *different field* and is unchanged.

**`SavClient._load_estatuto_default`** — dead since before 0.108.0; the type-2
path calls `_estatuto_default_from` directly.

### Changed

**`estatuto` is never a `needs_review` field, and `add_enrollment` never refuses
over it**
`IMPACT: silent` — a call that used to raise `ValueError` ("no determined
Estatuto FBP … supply one with `field_overrides`") now enrols. `preview_enrollment`
no longer lists `"estatuto"` in `needs_review`.
`DETECT:` grep your code for `"estatuto"` in `needs_review` handling, and for
catches around `add_enrollment` mentioning Estatuto.
`FIX:` delete the prompt. Nothing needs answering; SAV decides.

**`estatuto_decision` is reduced to a report**
`IMPACT: raises` — the shape is now `{value, label, source, reason}` with
`source` always `"sav_rule"`. The keys `needs_review`, `conflict` and
`confidence` are **gone**, and the whole key is **omitted** when SAV does not
state the rule or it cannot be read. The `fields` row for estatuto carries
`status: "sav_rule"`.
`DETECT:` grep for `estatuto_decision` and any read of its removed keys.
`FIX:` render `reason`; stop branching on `needs_review`. Use
`.get("estatuto_decision")` — it may be absent.

**A marked Estatuto box on the Modelo 1 no longer affects a type-1 enrolment**
`IMPACT: silent` — this reverses the conflict behaviour added earlier in
0.108.0. SAV does not accept an Estatuto choice from a club profile, so a box
disagreeing with SAV's rule could never be honoured; the conflict it raised was
unanswerable except by overriding back to SAV's value. `fill_mod1` still writes
the box to the PDF, unchanged.

### Added

**The filed Estatuto is verified after the commit**
op=151's `id` is SAV's *stored* Estatuto for a player in a batch — `0` before
the enrolment, the real id after — so it is only readable once the commit has
landed. The client re-reads it and raises `SavWriteUnverifiedError` when SAV
stored something other than what was sent, or returned no usable id. As with
the `check_menor_idade` check, that exception means **the enrolment is filed**
and only its classification is unconfirmed — it is not a rejection, so do not
answer it by re-enrolling. Costs one op=151 request per type-1 enrolment.

- `SavClient.primeira_estatuto_for_batch(batch) -> int | None` — SAV's Estatuto
  for a batch, or `None` when SAV did not state `mini`. Needs no player.

### Fixed

`tests/test_estatuto_decision.py` had a broken `from tests.…` import that failed
on a clean tree, so the offline suite was never actually green. It is fixed, and
the suite now passes with zero failures.

## 0.108.0 — 2026-09-18

A 1ª Inscrição died at the fee step — `No taxa options for primeira batch
634571` — because the estatuto in play had no fee configured in that batch. The
failure lands *after* op=12, so every attempt left an orphan person record
behind. SAV was stating the right answer the whole time, in a response this
package already fetched and discarded.

SAV's own wizard (`js/inscricoesjogador.js:8334-8347`) picks the type-1 estatuto
from the `mini` flag on the op=151 response — `mini == 1` → 6 (FBP), otherwise
10 (Sem FBP Comunitário) — and *disables the select* for any non-federation
profile. It is a rule SAV enforces, not a default it offers. Verified live
2026-09-18: `mini: 1` for the Mini 10 batch, `mini: 0` for every Sub 14/16/18
batch, and in the Mini batch only estatuto 6 has a fee — 10, 11 and 12 all
return a bare placeholder.

### Changed

**The type-1 estatuto now follows SAV's per-batch rule**
`IMPACT: silent` — a 1ª Inscrição that previously resolved to Sem FBP
Comunitário from nationality now resolves to FBP in a Mini batch, and
`estatuto_decision.source` reads `sav_rule` instead of `nationality`. This
applies to **Portuguese and foreign players alike**: the engine placed Portugal
on the community list, so it filed a Portuguese child as Sem FBP Comunitário —
the estatuto with no fee — which is precisely what broke. FBP is *Formação
Basquetebolística Portuguesa*, about formation rather than citizenship, and SAV
assigns it to every Mini player regardless of nationality.
`DETECT:` grep for `estatuto_decision` and for any assumption that `source`
is one of the pre-0.108.0 values.
`FIX:` none needed to keep enrolling. If you displayed the estatuto to an
operator, render the new `reason` — it names the batch, tier and flag.

**`resolve_estatuto` is unchanged** — it still refuses to infer FBP from
citizenship. The new value comes from SAV stating a constraint, applied *after*
the engine has had its say, never from local inference.

**A marked Modelo 1 box is never overridden**
When the signed form or an official FPB status disagrees with SAV's rule, the
form's value is kept and the new `conflict` field is set naming both sides;
`needs_review` becomes true and `add_enrollment` refuses until a caller resolves
it with `field_overrides={"estatuto": <id>}`.

**Taxa failures no longer dump SAV's option HTML**
`_resolve_primeira_taxa_id`'s messages now name the batch, tier, estatuto id and
label. Exception types are unchanged (`SavResponseError` for none,
`SavConfigError` for several).

### Fixed

**op=27 no longer reports a server fault as a connection failure**
`IMPACT: raises` — `_primeira_commit` parsed with a bare `json.loads` and
wrapped any failure in `SavConnectionError`. SAV answers unhandled PHP fatals
with HTTP 200 and a stack trace, so a commit that reached SAV and broke partway
was indistinguishable from a request that never arrived, and the body was
discarded. It now parses through `_parse_json_response`, raising
`SavServerError` with the body logged at DEBUG on the `sav_client` logger and
SAV's schema kept out of the message. A genuine transport failure still raises
`SavConnectionError`.
`DETECT:` grep for `except SavConnectionError` around
`add_player_to_registration_batch`.
`FIX:` catch `SavError` (the shared base) if you need both, and treat
`SavServerError` as "the commit may have landed" — verify the postcondition
rather than blindly retrying. This mattered on 2026-09-18: an enrolment failed
here, the person record it had been working with was gone afterwards, and there
was no way to tell whether the commit caused that.

**op=151 is fetched once per enrolment, not twice**
Internal only, no contract change. `_load_primeira_estatuto` read the response
for the `mini` flag and then read it again for the option list. One response now
serves both via `_mini_from`.

### Added

- `EstatutoDecision.conflict` (`str | None`) and `SOURCE_SAV_RULE`
  (`"sav_rule"`). `conflict` is additive on `to_dict()`; `needs_review` now also
  returns true when it is set.
- `sav_shared.estatuto.apply_sav_batch_rule(decision, *, mini, batch_number, tier)`.
- `SavClient.primeira_estatuto_mini_flag(batch)` — SAV's `mini` for a batch, or
  `None` when SAV did not state it. Needs no player: op=151 does not depend on
  the userid.

### Known gap

Every type-1 batch available for testing is `mini`, so the `mini == 0 → 10` half
follows SAV's JS but is unverified against a real fee table. `mini` absent *or*
null falls through to the previous sole-option behaviour rather than guessing —
reading null as a stated `0` would select the estatuto with no fee in a Mini
batch, which is the bug this release fixes.

## 0.107.1 — 2026-09-18

### Fixed

**0.107.0 could not be imported by a `sav_shared`-first consumer**
`IMPACT: raises` — `ImportError: cannot import name 'classify_primeira_duplicate'
from partially initialized module 'sav_shared.enrollment'`, and the same for
`decode_sav_flag` from `sav_shared.flags`. 0.107.0 added module-scope
`from sav_shared.enrollment import ...` and `from sav_shared.flags import ...`
to `sav_client/sav_client.py`. Both modules import `sav_client.exceptions`,
which cannot be reached without executing `sav_client/__init__.py`, which
imports `sav_client.sav_client` — a cycle. Importing `sav_client` first masked
it, which is what every test in this repo did, so the suite stayed green while
any consumer whose first import was a `sav_shared.*` module died on startup.
Every drive-to-sav command was affected.
`DETECT:` `python -c "import sav_shared.enrollment"` in a fresh interpreter.
`FIX:` upgrade to 0.107.1. No API changed — `sav_shared.flags` now binds
`SavResponseError` at raise time instead of import time, and
`classify_primeira_duplicate` is imported inside its single use site.
`tests/test_import_cycles.py` now imports every public module as the first
import in a fresh interpreter, which is the only way this class of bug is
visible.

## 0.107.0 — 2026-09-17

The 1ª Inscrição duplicate guard rejected players who hold no licence at all,
and routed the caller to a Revalidação that cannot be performed — there is no
licence to revalidate. Three real players were stuck that way, unable to enrol
by any route.

`op=11` answers two questions with one flag. `existe` means "a person with this
identifying data is on file"; `inscricaovalida` means "that person holds a valid
enrolment". This package read only `existe`. Across 23 real candidates plus 3
controls, `inscricaovalida` separated the two cases without a single exception.

### Changed

**`existe:1` + `inscricaovalida:0` now enrols instead of failing**
`IMPACT: silent` — a call that used to raise `SavResponseError` now succeeds.
`add_player_to_registration_batch` reuses the existing person record, skipping
op=12 and op=20, and returns that pre-existing SAV `userid` rather than a newly
minted one. `resolve_player` / `preview_enrollment` return an ordinary
`{resolved: true}` where they previously returned
`{error: "player_already_in_sav"}`.
`DETECT:` grep your code for `player_already_in_sav`, and for `except
SavResponseError` around `add_player_to_registration_batch`.
`FIX:` nothing to change if you treated the error as "this player can't be
enrolled here". If you relied on it to mean "this person is new to SAV", that
inference was never sound and is now wrong — the returned `userid` may name a
record that predates your call.

**A reused person's address is not updated**
`IMPACT: silent` — `morada`, `cod_postal`, `distrito_id`, `concelho_id`,
`localidade_txt` and `country_id` are ignored when an existing record is reused;
the player keeps whatever address SAV already holds. op=20 is a bare `INSERT`
primary-keyed by userid and returns a duplicate-key PHP fatal for anyone who
already has an address row (confirmed against production: `Duplicate entry
'278342' for key 'PRIMARY'`), and no address-update op is known. The parameters
stay required because the caller cannot know in advance whether reuse applies.
`DETECT:` grep your logs for `address fields are ignored` — a WARNING names the
dropped fields on every reuse.
`FIX:` none available in this release. Verify the stored address separately if
it matters.

**The minor gate derives its own answer on the reuse path**
Skipping op=20 removes its `menor_idade` flag, so reuse derives minor status
from the birth date via `player_is_minor` and fails closed — an underivable age
counts as a minor rather than opening the gate. op=27's `check_menor_idade` is
then compared against that derivation; a disagreement, or an absent flag, raises
`SavWriteUnverifiedError`. That exception means **the enrolment is filed** and
only its guardian gating is unconfirmed — it is not a rejection, so do not
re-enrol on seeing it.

### Added

- `sav_shared.enrollment.classify_primeira_duplicate` → `PrimeiraDuplicate`
  (`existing_id`, `blocking`, `reusable`). The client wizard and the MCP
  resolver share it, so a preview and its commit cannot disagree. A missing
  `inscricaovalida` is treated as blocking: fail closed if SAV changes shape.
  There is deliberately no `atleta` fallback for `existing_id` — `atleta` is a
  boolean flag reading `1` on every real response, so using it as a userid would
  enrol person id 1.
- `sav_shared.flags.decode_sav_flag` — `_decode_sav_flag` moved out of
  `sav_client.sav_client` so `sav_shared` can decode SAV's flags too. The
  client keeps `_decode_sav_flag` as an alias; no call site changed.

## 0.106.0 — 2026-09-16

0.105.0 fixed how the step-3 commit *settles* an estatuto. Nothing upstream of
that could *determine* one: this package could not decide an Estatuto FBP,
could not carry it on a Modelo 1, and could not tell a caller what it had
concluded. This is that half.

### Added

**`sav_shared.estatuto` — the Estatuto FBP decision engine**
`resolve_estatuto(parsed, *, fpb_status=None, local_fbp_eligible=False)` returns
a frozen `EstatutoDecision` (`value`, `label`, `source`, `reason`,
`confidence`, a derived `needs_review`, and `to_dict()`), deciding in this
order and stopping at the first step that answers:

1. an official FPB status wins — an unrecognised one is a review request, not a
   reason to fall through to a default;
2. more than one Estatuto box marked on the Modelo 1 → review;
3. apparent local FBP eligibility → review, never promotion (the parameter is
   wired; no caller sets it yet, and it can only ever raise a review);
4. exactly one marked box is the signed form's answer, carried with its OCR
   confidence — below 0.60 it is reported in the reason, not dropped;
5. nationality chooses **only** which *Sem FBP* branch applies, and only for a
   country recognised exactly;
6. otherwise review. There is no fallback value.

**The rule the whole module exists for: `FBP` is never inferred from
citizenship.** It means *Formação Basquetebolística Portuguesa*; a Portuguese
passport is evidence for `Sem FBP Comunitário` and never for Portuguese
formation. Step 5 is structurally incapable of returning 6 or 12.

The country lists come from FPB Comunicado da Direção nº 091 (26/04/2024,
effective 2024/2025) and are hardcoded with the URL in a comment, so a
republication arrives as a reviewable diff against a cited, dated source.
Portugal is added explicitly — the Comunicado lists countries *with agreements
with* Portugal and so omits Portugal itself. A second, explicit list of known
non-community countries backs the negative inference: an unrecognised
nationality goes to review rather than to `Sem FBP Não Comunitário` by
elimination, because an OCR error and an unlisted spelling look identical to a
genuinely non-community country. Countries absent from the Comunicado but
holding an EU agreement of the kind it is drawn from (Reino Unido, Suíça,
México, África do Sul, Sudão, Líbia, Síria) are on neither list and go to
review. Modelo 1's Nacionalidade box carries a demonym, so an explicit alias
table maps demonym → country; nothing is fuzzy-matched.

**`ESTATUTOS` in `sav_shared.lookups`, published as `estatutos`**
The four SAV op=151 ids — 6 `FBP`, 10 `Sem FBP Comunitário`,
11 `Sem FBP Não Comunitário`, 12 `Equiparado FBP` — with `find_estatuto_id()`
and `estatuto_name()`, and a new `estatutos` key in `reference_data()` (so the
`sav://lookups` resource carries them). 12 is official/historical, accepted
from FPB and never produced by local inference.

**`preview_enrollment` returns `estatuto_decision` (1ª Inscrição)**
`{value, label, source, reason, confidence, needs_review}`. The decision is
cached alongside the form, so `add_enrollment` commits exactly what was
previewed. When it needs review it also appears in `needs_review`.

**`add_enrollment` refuses an undetermined Estatuto on a 1ª Inscrição**
`IMPACT: raises` — a type-1 enrolment whose Estatuto needs review now raises,
with the decision's `reason` in the message, unless
`field_overrides={"estatuto": 6 | 10 | 11}` (or the `estatuto=` argument)
supplies one. `field_overrides` wins over the argument. 12 is refused from a
caller on either channel **and for every registration type**, with a message
saying an official Equiparado status must come from FPB — it reaches an
enrolment only by already being on the player's SAV record. The client-level
`add_player_to_registration_batch(estatuto=12)` is unchanged; only the MCP
boundary refuses it.

This is deliberate: SAV supplies a default of its own, so the alternative to
raising is filing a legally meaningful classification nobody determined.

`DETECT:` `grep -rn "add_enrollment" --include=*.py .` — any 1ª Inscrição call
site. `FIX:` read `preview_enrollment`'s `estatuto_decision`; when
`needs_review` is true, put an estatuto in `field_overrides`. Revalidação is
unaffected.

### Fixed

**Every Modelo 1 this package rendered went out with Estatuto blank**
`IMPACT: silent` — `fpb`, `semfpb_com` and `sem_fpb_naocom` were absent from
`MOD1_FILL_MAPPING` under a comment claiming they belonged to the
club-insurance branch. They do not: they are the Estatuto FBP group. No caller
could set them and nothing reported them missing, so every form rendered by
`fill_mod1` / `render_mod1` / `sav mod1 fill` since the renderer was written
was filed with the group unticked.

`values` now accepts `estatuto` (6 / 10 / 11, by id or label), the reverse
mapping reads a filled form back into the `estatuto_fbp_*` entities, and
`enrollment_fields()` carries the row with `enum_ref: "estatutos"`.

`estatuto` is deliberately **not** in `_MOD1_REQUIRED_CORE`: a signed form may
legitimately carry no Estatuto and have it settled before submission, and
making it mandatory would break every existing `fill_mod1` caller. 12
(`Equiparado FBP`) has no box on the current form and is rejected naming that
reason rather than reported as an invalid option.

`DETECT:` `grep -rn "fill_mod1\|render_mod1\|mod1 fill" --include=*.py .`
`FIX:` nothing is required — omitting `estatuto` still renders. Re-render any
form whose Estatuto matters and which has not yet been signed.

The `Seguro FPB` insurance default is unchanged: it is a club policy with no
per-player input, and was only ever conflated with Estatuto by that comment,
which is now corrected.

**A type-1 enrolment filed an unread nationality as Portuguese**
`IMPACT: silent` — and this one is separate from Estatuto. SAV's type-1 wizard
defaults `nationality_id` to Portugal (155), so a Modelo 1 whose Nacionalidade
box was blank, unreadable, or simply not Portuguese did not merely lose
information: it filed the player as Portuguese, which also changes their
required-document checklist.

`build_primeira_kwargs` now sets `nationality_id` only when the form positively
says Portugal at a confidence worth trusting. Every other case leaves it unset
and lists it in the preview's `needs_review`, for the caller to answer with a
SAV nationality id. A `Sem FBP Comunitário` estatuto is explicitly not read as
setting nationality — it is shared by ~147 countries and answers a different
question.

`DETECT:` `grep -rn "nationality_id" --include=*.py .`
`FIX:` a type-1 caller that previously relied on the silent default must now
answer `nationality_id` through `field_overrides` for any non-Portuguese or
unread nationality. `REQUIRED_PRIMEIRA_KWARGS` gained `nationality_id`, so a
caller iterating it sees one more required field.

---

## 0.105.0 — 2026-09-13

Three bugs found while recovering 38 athletes who could not be enrolled after
their lotes were deleted. All three were observed in production against the
real SAV2 on 2026-09-12.

### Fixed

**Exact federation-wide licence lookup no longer hangs building the club map**
`IMPACT: silent` — an exact licence search with `club=0` could parse the
matching row and then stall while enriching every club in every association
just to resolve the row's display name. It now returns the native match
promptly, keeps `club_id=0` when SAV2 does not provide a source-club id, and
still caches the licence → internal player id used by detail and profile
loads.

`DETECT:` `grep -rn "club_id" --include=*.py .` — treat `club_id=0` as
unresolved on exact licence lookups; do not infer a source club from the
display name.

`FIX:` no caller change is needed unless it assumed every federation-wide
exact-licence row had a resolved `club_id`; broad federation-wide searches
retain their existing club-name resolution.

**`estatuto=` was dead code on the Revalidação path**
`IMPACT: silent` — the parameter was accepted and discarded. Passing it changed
nothing about the request. `add_player_to_registration_batch` forwarded
`estatuto` only to the 1ª Inscrição branch; for a type-2 batch the step-3
commit read `step3_prefill['estatuto']` for both the op=26 fee lookup and the
op=36 body, so the caller's value never reached SAV. That is what made the
stranded athletes look unrecoverable — the one documented way to supply a
missing estatuto did nothing.

An explicit `estatuto` now wins over the prefill everywhere, reaching the fee
lookup as well as the commit, on the add path and the exam-date edit path
alike. `update_player_in_registration_batch()` accepts it too.

`DETECT:` `grep -rn "estatuto" --include=*.py .`
`FIX:` nothing to change. If you passed `estatuto=` to a Revalidação and worked
around it having no effect, remove the workaround — and re-check any enrolment
you made believing the value had been applied, because it had not.

**An empty `estatuto` was forwarded to SAV, which answered with a SQL error**
`IMPACT: raises` — but from the server, not from us, and only after the write
had been attempted. When the stored record carried no estatuto the commit body
went out with `"estatuto": ""` and op=36 answered HTTP 200 with a PHP fatal:
`mysqli_sql_exception: You have an error in your SQL syntax ... near
'4704,23,0,1139  , '1 ', '1 ')' ... INSERT INTO ins...`. The empty value leaves
a hole in SAV's INSERT. It is SAV's bug; handing it the empty value was ours.

An empty estatuto is now never committed. `0` and `""` are treated as SAV's
blank dropdown row rather than as writable values — the one place in the step-3
commit where an explicit empty value is *not* honoured, deliberately.

**A batch with players in it could still be deleted**
`IMPACT: silent` — **and it is the bug that stranded the 38.** 0.104.0 drained
the lote before deleting it, which was better but still one call away from the
same unrecoverable mistake. `delete_player_registration_batch()` now refuses a
non-empty lote outright, naming the licences that block it. Empty it yourself
with `remove_player_from_registration_batch()` — one licence at a time, each
verified against SAV — and then delete it.

Why refuse rather than drain: `op=29` against an already-deleted lote answers
body `'0'`, a rejection. There is no repair once the lote is gone, so emptying
has to be an explicit act the caller takes and can see the result of.

`DETECT:` `grep -rn "delete_player_registration_batch\|delete_batch" --include=*.py .`
`FIX:` remove every player first, then delete. Callers that relied on 0.104.0's
automatic drain must now do it themselves.
- `SavClient.delete_player_registration_batch(batch_id)` returns `None` again,
  not the `list[int]` 0.104.0 introduced — nothing is freed by this call any
  more, and an always-empty list would be a lie. **Breaking against 0.104.0
  only**; 0.103.x and earlier also returned `None`.
- MCP `delete_batch()` dropped the `freed_licenses` key 0.104.0 added.
- `sav enrollment delete --batch` no longer prints released licences.

### Added

**`allow_ineligible=True` — a documented way past the op=139 guard**
op=139 is a listing, not a gate: SAV's own wizard accepts an enrolment for a
licence the list omits, which is how all 38 were recovered. Until now that
required monkeypatching a private method.

- `SavClient.add_player_to_registration_batch(..., allow_ineligible=False)`.
- `sav enrollment create --allow-ineligible`.
- MCP `add_enrollment(..., allow_ineligible=False)` and
  `create_enrollment_manual(..., allow_ineligible=False)`.

Off by default, and the bypass logs a WARNING naming the licence. SAV omits
players from op=139 for real reasons as well as for the deleted-lote one this
exists for; routine use would re-enroll people who genuinely cannot be.

`estatuto` is exposed alongside it on the same three surfaces, plus as
`--field estatuto=N` on `sav enrollment create` / `sav enrollment update`.

### Investigated: why those 38 had no `estatuto`

The question was whether deleting the lote *cleared* the field or whether it
only exists while a registration is in progress. It is the second, and the
field was never on the player at all.

- **`estatuto` is not part of a player's record.** Probed live on 2026-09-13:
  neither op=30 (existing item) nor op=35 (player prefill) returns an estatuto
  key in any form. It is per-inscrição state, reachable only through op=31's
  step-3 prefill, which is why deleting the lote takes it with the item.
- **An empty one is an ordinary state, not a damaged record.** `guiasjog.js`
  POSTs **op=151 for every batch type**, selects `res['id']`, and overrides it
  only when the prefill is non-empty — literally `if (res['estatuto'] != "")`.
  SAV's UI expects to find nothing stored and has a fallback; this client had
  none, so it sent the empty string.
- **op=151 answers for a Revalidação too, with a per-player default.** Against
  batch 632884 (type 2) it returned `id='6'` (FBP) for two athletes and
  `id='10'` (Sem FBP Comunitário) for a third, with the same four options for
  all three. So SAV does know each player's status — we were not asking.

So Bug 2 is neither a pure recovery edge case nor quite the ordinary path: the
empty prefill is normal and expected, and the client now handles it the way the
browser does. The resolution order is explicit value → op=31's stored selection
→ op=151's default → refuse. The refusal is the last resort it was asked to be,
not the first answer.

Note `_load_primeira_estatuto` could not simply be reused: it picks a lone real
option, and op=151 returns all four for a type-2, so that rule never fires
there. The type-2 default has to come from `id`.

---

## 0.104.0 — 2026-09-12

### Fixed

**Deleting a batch stranded every player in it**
`IMPACT: silent` — **and the players it stranded could not be enrolled
anywhere afterwards.** `delete_player_registration_batch()` fired op=9 and
nothing else, mirroring what the batch row looks like in the UI. But SAV2 does
not release a batch's items when the batch goes: each licence stays pinned to
the now-missing batch, so it appears in no batch the club can see while op=139
still refuses to offer it for a new enrolment. The player is invisible and
unenrollable at once, and nothing in the response says so — the delete reports
success.

The batch is now drained first: op=10 lists its items, op=29 removes each
licence (the call the SAV2 UI's own per-row remove button makes), and op=9
fires only once the batch is empty. Each removal keeps the existing op=30
verification, so a removal SAV silently ignored is still caught.

**A drain that cannot finish refuses the delete.** If the items cannot be
listed, or any removal fails or cannot be verified, the batch is left standing
and the error names the licence that blocked it plus how many were freed before
it. This is deliberate and is the whole point of the change: a batch we failed
to delete can be deleted again, a player stranded in a deleted batch cannot be
recovered from here. It does mean a batch whose item listing SAV is failing on
is no longer deletable through us — accept the retry, do not add a force path.

Changed:
- `SavClient.delete_player_registration_batch(batch_id)` returns `list[int]`
  (the freed licences, in removal order) instead of `None`, and can now raise
  `SavResponseError` / `SavWriteUnverifiedError` / `ValueError` from the drain.
- MCP `delete_batch()` gained `freed_licenses: [int]` in its result.
- `sav enrollment delete --batch` prints the released licences.

`DETECT:` `grep -rn "delete_player_registration_batch\|delete_batch" --include=*.py .`
`FIX:` nothing to change for a caller that ignored the return value. A caller
that treated the delete as infallible now needs to handle a refusal — and
should, since the refusal is the case where players are still in the batch.
A caller comparing `delete_batch()`'s dict for exact equality must switch to
key access.

Also: `remove_player_from_registration_batch()` split its body into a private
`_remove_batch_item(batch, license)` that takes an already-resolved batch, so
the drain loop resolves the batch once rather than re-listing every batch per
player. Public behaviour is unchanged.

---

## 0.103.0 — 2026-09-11

### Added

**Filed enrollment documents can now be read back**
`IMPACT: silent` — additive, but `list_player_registration_documents()` grew a
key. Nothing changes shape or meaning; a caller comparing a document dict for
exact equality (`doc == {"doc_id": ..., "tipo_doc": ...}`) now sees that
comparison fail.

Until now the surface could list what a player had filed and write documents,
but never read one back. That gap was worst exactly where writing is closed:
once a batch leaves "Em construção" SAV withholds every galeria id, so a
submitted enrollment was a black box — no way to re-read the medical exam's
date, confirm the Modelo 1 actually filed carries the club stamp, or hand a
parent a copy.

The op=91 response already carried the answer. Each document row renders a
`goToPage("uploads/galeria_docs/jogadores/<file>.pdf")` view button, and unlike
`checkDoc`/`deleteDoc` **that path survives submission** — verified against
batch 632315 (Em construção) and batch 632482 (Em Validação), which return the
same markup for it. So this is a parse addition to an op already called, not a
new endpoint. (SAV2's own UI uses op=107 for the read-only view; it returns the
same paths minus `num`, and is not worth a second code path.)

New:
- `SavClient.download_player_registration_documents(batch_id, license, *,
  tipo_doc=None)` → `[{tipo_doc, file_path, filename, content}]`.
- `sav enrollment documents LICENSE [--type TYPE] [--out DIR]` — lists, or
  downloads into `DIR` as `<license>_<doc_type>.<ext>`.
- MCP `download_player_document(license, doc_type?)` →
  `[{doc_type, filename, size_bytes, pdf_b64}]`.
- `list_player_registration_documents()` entries gained `file_path`.

`DETECT:` `grep -rn "tipo_doc\"\s*:" --include=*.py .` — look for equality
comparisons against a whole document dict rather than key access.
`FIX:` compare the keys you care about (`doc["tipo_doc"]`), not the dict.

**This is the one document method with no `is_open` guard, on purpose.**
Upload, replace and delete each refuse a submitted batch because SAV cannot
accept the change. Reading is the opposite case, and the submitted batch is the
motivating one — do not "restore" the missing guard by analogy.

**The stored files are served without authentication.** An anonymous GET of the
`uploads/...` path returns the same bytes as an authenticated one. The path is
therefore a world-readable link to a player's medical exam, and neither the CLI
nor the MCP tool prints or returns it — only `filename`. Keep it that way.

Known issue, not fixed here: `LicenseNotEnrolledError.open_batches` is built
from the same widened list when `include_submitted=True`, so `read_enrollment`,
`list_player_documents` and `download_player_document` advertise submitted
batches as joinable — the hazard 0.102.2 separated the two lists to avoid in
`classify_enrollment_status`. Fixing it changes what three existing tools
return, so it wants its own entry. TODO recorded at the raise site.

---

## 0.102.2 — 2026-09-11

### Fixed

**`enrollment_status_bulk` reported filed players as never enrolled**
`IMPACT: silent` — **and this one could file duplicate registrations with the
federation.** The bulk path answered `not_enrolled` for any player whose batch
had been submitted, while `get_enrollment_status` answered `pending` for the
same licence. A caller classifying a whole roster from the bulk tool — the
normal way to do roster work — sees an athlete already filed with FPB as
indistinguishable from one never enrolled, and enrolls them a second time. With
`submit_batch` now automating submission (0.101.0), that duplicate reaches the
federation and cannot be withdrawn.

0.102.1 widened `get_enrollment_status`, `read_enrollment` and
`list_player_documents` to see submitted batches. It missed
`classify_enrollment_status`, which backs the bulk tool: it scanned only
`b.is_open` batches for items, so a player in "Em Validação" was found in no
batch at all and fell through to `not_enrolled`.

The scan now covers every in-flight batch. Note the two lists are deliberately
separate and only one widened: the batches scanned for a licence (all in-flight)
versus the `open_batches` advertised on a `not_enrolled` row (still
"Em construção" only). Merging them would advertise a submitted lote as
joinable — telling a caller to add a player to a batch that has left the club.

`DETECT:` `grep -rn "enrollment_status_bulk\|classify_enrollment_status" --include=*.py .`
`FIX:` nothing to change at the call site. Re-check any logic that branched on
`not_enrolled`, since licences in submitted batches now correctly report
`pending` — in particular anything that treated `not_enrolled` as "safe to
enroll".

Cost: item scanning goes from one call per *open* batch to one per *in-flight*
batch (4 → 6 for the reference club). The existing TODO on
`list_player_registration_batch_items` re-listing batches per call therefore
bites slightly harder; not addressed here.

### Note for anyone reading the code

`PlayerRegistrationBatch.is_pending` is `return True`, not a state test — SAV
drops a batch from the listing once it completes, so being listed *is* what
pending means. The `is_pending` filter is a no-op today and is written for
intent, matching `resolve_batch_by_license`. A test that asserted non-open
batches were skipped was asserting the bug, and has been corrected.

---

## 0.102.1 — 2026-09-11

### Fixed

**Reading any enrolment in a submitted batch raised instead of returning**
`IMPACT: raises` — `get_enrollment_status(license=...)` threw
`SavResponseError("Could not find inscricao id in op=91 response")` as soon as
the player's batch reached `Em Validação`. A regression in *reach*, not in code:
before `submit_batch` existed (0.101.0) a club could not easily put a batch into
that state from this package, so the read path was never exercised against one.

Confirmed against production 2026-09-11 by capturing op=91 for a submitted batch
(licence 257901, batch 632478) and an open one (licence 296838, batch 632315).
A submitted batch still returns HTTP 200 with a full `body` — every document,
its type label, timestamp and uploader — but SAV drops the `checkDoc` and
`deleteDoc` handlers and the type `<select>`, because none of those actions are
available once the batch has left the club. The parser anchored on exactly those
handlers.

Two distinct bugs fell out of that, and the second was worse:

- `inscricao` is parsed from `checkDoc`, and its absence raised. It is now
  tolerated **only when the batch is not open**; an open batch with no
  `checkDoc` still raises, because there it means SAV's markup changed.
- `docs` was parsed by scanning for `deleteDoc`, so a submitted batch returned
  an **empty document list** — reporting "no documents" for a record that
  demonstrably had them. Silent, and worse than the crash. Rows are now matched
  on either handler. Document *types* resolve normally via the static
  `_DOC_TYPE_LABELS` fallback, which is what a checklist needs; `doc_id` comes
  back None, since SAV genuinely withholds the galeria id in this state.

`DETECT:` `grep -rn "_fetch_registration_documents\|list_player_documents" --include=*.py .`
— any caller that treats `doc_id` as always-int, or reads an empty `docs` list
as "nothing uploaded", needs to handle the submitted case.
`FIX:` treat `doc_id=None` as "exists but not actionable in this state", not as
absent. Callers needing only document types are unaffected.

**Uploading to a submitted batch was refused only by accident**
`IMPACT: silent` — worth reading even though nothing broke yet. The refusal came
from the `inscricao` parse raising, not from any deliberate check. Making reads
tolerate a missing `inscricao` removed that accidental protection, which would
have let an upload build op=92's URL with `inscricao=None` and send a corrupt
request instead of refusing. `upload_player_registration_document` and
`replace_player_registration_document` now guard explicitly on `batch.is_open`
and name the offending state in the error.

**Read-only tools reported enrolled players as not enrolled**
`IMPACT: raises` — `list_player_documents` and `read_enrollment` answered
`{"error": "license_not_enrolled"}` for any player whose batch had been
submitted. They resolve through `_resolve_license_batch`, which searched open
batches only, so a submitted batch looked like no batch at all.

`_resolve_license_batch` gained `include_submitted: bool = False`. The two
read-only tools pass True; the six mutation callers (`update_enrollment`,
`update_enrollment_with_document`, `delete_enrollment`, `upload_player_document`,
`delete_player_document`, `replace_player_document`) deliberately keep the narrow
behaviour — for a mutation, "not in an open batch" is the correct answer, since
a batch that has left the club cannot accept changes.

`DETECT:` `grep -rn "resolve_batch_id_by_license" --include=*.py .` — any test
double or wrapper implementing this method needs the new keyword in its
signature.
`FIX:` add `*, include_submitted: bool = False` to the signature. Pass True only
from read paths.

### Changed

**`list_player_documents` entries gained an `editable` field**
`IMPACT: silent` — additive, so anything reading `doc_id` / `doc_type` by key is
unaffected; only an exact-shape comparison of the returned dicts breaks. Entries
are now `{"doc_id": int | null, "doc_type": str | null, "editable": bool}`.

`editable` is False exactly when SAV withheld the galeria id, i.e. the batch is
no longer open. It exists because a bare `doc_id: null` reads as a parse failure,
when the actionable fact is "this document is real but cannot be deleted or
replaced".

`DETECT:` `grep -rn "list_player_documents" --include=*.py .`
`FIX:` read fields by key rather than comparing whole dicts. Gate any
delete/replace on `editable` rather than assuming `doc_id` is an int.

---

## 0.102.0 — 2026-09-10

### Breaking

**`submit_subida_enrollment` is now `add_subida_enrollment`**
`IMPACT: raises` — in-repo Python call sites fail at import. **Over MCP an
agent calling the old name gets "unknown tool", which reads like a transient
error and invites a retry rather than a fix.** If you see that, the tool was
renamed; do not retry.

This finishes what 0.101.0 started. That release renamed `submit_enrollment` to
`add_enrollment` but left this one alone, so the surface still had a `submit_`
prefixed tool that meant "add one player" — the exact ambiguity the rename
existed to remove, sitting next to `submit_batch`, which is irreversible.

`DETECT:` `grep -rn "submit_subida_enrollment" --include=*.py --include=*.toml .`
`FIX:` rename to `add_subida_enrollment`. Signature, return value and behaviour
are unchanged — the name is the only difference.

Every "add one player" tool is now `add_*`, and `submit_batch` is the only
`submit_*` tool. If a tool name starts with `submit_`, it sends a whole batch
to the federation and cannot be undone.

---

## 0.101.0 — 2026-09-10

### Breaking

**`submit_enrollment` is now `add_enrollment`**
`IMPACT: raises` — in-repo Python call sites fail at import. **Over MCP the
failure is softer and more dangerous: an agent calling `submit_enrollment` gets
"unknown tool", which reads like a transient error and invites a retry rather
than a fix.** If you see that, the tool was renamed; do not retry.

The old name meant "add one player to a batch", but read as "submit the batch".
Now that a real batch-submission tool exists (`submit_batch`, below, which is
irreversible), the ambiguity was a footgun pointed at the one operation that
cannot be undone.

`DETECT:` `grep -rn "submit_enrollment" --include=*.py --include=*.toml .`
`FIX:` rename to `add_enrollment`. Signature, return value and behaviour are
unchanged — the name is the only difference.

Note `submit_subida_enrollment` is **not** renamed and also means "add one
player" (the Subida variant). That inconsistency survives this release — it is
resolved in 0.102.0, which renames it to `add_subida_enrollment`.

### Added

The registration-batch lifecycle is now complete: this package can submit a
finished batch to FPB for validation, where before it could build a batch but
not send it.

`submit_batch` submits the **whole batch** and is irreversible; it is not the
same operation as `add_enrollment`, which adds **one player** to a batch.
After `submit_batch`, the batch stops accepting new players and leaves the
club's control.

`check_batch_ready` is SAV's own readiness verdict from op=116. Prefer it when
deciding whether a type-1/2 batch can be submitted: it is better grounded than
`document_requirements` / `enrollment_checklist`, which are our model of the
FPB rule. For type-3 (Transferência) and type-4 (Subida) batches SAV runs no
precheck, so `checked=False`; op=8's own response is the only gate there.

The confirmed post-submit state is returned, including the now-documented
`state_id=9` meaning `Em Validação`.

---

## 0.100.3 — 2026-09-10

### Fixed

**Every fresh enrollment filed "Taxa de inscrição: Não selecionado"**
`IMPACT: silent` — the commit succeeded and the record carried no registration
fee. `_commit_registration_step3` preserves op=31's stored step-3 selections
whenever the caller passes `None`, so that an exam-date edit does not clobber
choices someone made by hand. For `taxa` that preservation was unconditional,
and it assumed "no fee chosen" would arrive as an *absent* key.

It does not. SAV encodes an unmade step-3 choice as the sentinel `-1`, the same
way it does for `subida` and `seguro`. Confirmed live on 2026-09-10: op=31 for a
player not yet in a batch returns `taxa: "-1"`. So the preservation path faithfully
preserved "nobody has chosen one", `taxa_id` was no longer `None` by the time the
`if taxa_id is None` guard below it ran, and `_resolve_taxa_id` — the op=162 →
op=26 cascade written for exactly this case — never fired. The commit body sent
`"taxa": "-1"`.

Observed on licence 270158 (Sub 14 Feminino), whose tier offers exactly one fee
(`1093 Isento Sub14 Fem FBP`) — the auto-pick case the resolver exists to handle.

The prefill's `taxa` is now ignored when it holds a not-selected sentinel (`-1`,
any id `<= 0`, empty, or absent), so the cascade resolves the fee as intended.
An explicit `taxa_id=-1` from the caller is unchanged: that is a deliberate
instruction to clear the fee, and it is still written. Preservation of a genuine
stored fee on the update path is unchanged.

`DETECT:` `grep -rn "add_player_to_registration_batch\|submit_enrollment" --include=*.py .`
— any call that omits `taxa_id` (nearly all of them) previously filed `-1` and
now files a resolved fee. Records already filed with `-1` keep it until they are
edited; an edit that omits `taxa_id` will now resolve and fill the fee.

`FIX:` nothing to change for the single-fee case. Where a tier offers **two or
more** fees, the resolver raises `SavConfigError` ("Multiple taxa options … Pass
taxa_id= to disambiguate") instead of silently filing `-1`. Pass the id: as
`taxa_id=` on `add_player_to_registration_batch`, or as
`field_overrides={"taxa_id": <id>}` on the `submit_enrollment` MCP tool.

---

## 0.100.2 — 2026-09-10

### Fixed

**Automated enrollment filed every scanned Modelo 1 with the Revalidação licence blank**
`IMPACT: silent` — and it reached the federation. `mod1_completion_path` chose a
single *source* for its slot reads — `tipo_fields = parsed if parsed is not None
else overlay_fields` — so a caller that supplied `parsed` was never asked a
second time, per question. That is right when `parsed` comes from a real OCR
session, but a club supplying its own player data passes
`mod1_values_to_fields(values)`, which is built from the AcroForm fill mapping:
it carries `licenca_fpb` (the licence *text*) and never `licenca_fpb_presente`.
`read_licenca_fpb` therefore returned `(None, None)`, `licenca_overlay` correctly
declined to guess a location, and the licence was skipped.

The give-away is that the club stamp and the inscription mark *were* applied on
the same document: the carimbo path had already run Document AI and was holding
the licence presence entity and its writable-slot bbox the whole time. Nothing
was missing except the willingness to ask the second dict.

Nobody noticed because `has_license: None` means "not inspected", not "not
filled", and no warning is emitted for an uninspected slot.

Slot reads are now resolved per question rather than per source: `parsed` is
asked first, and where it yields an unknown presence the already-in-hand
`overlay_fields` are asked instead, with presence and bbox always taken from the
same dict. `read_tipo_inscricao` had the identical shape — the
`tipo_inscricao_*` entities exist only when the caller ticked that box — and gets
the same treatment, including the `reg_type_derived` contradiction check.

No new Document AI call is introduced: the fallback is capped at fields the
carimbo path already produced. `allow_ocr_fallback=False` is unaffected, since
in that mode `overlay_fields` *is* `parsed` and the fallback is a no-op.
`licenca_overlay`'s refusal to overwrite an existing number is untouched — the
fallback supplies a location and a presence answer, never permission to
overwrite.

`DETECT:` any Modelo 1 filed through `submit_enrollment` or another path that
passes caller-supplied values as `parsed` — grep your own code for
`mod1_values_to_fields` and for `parsed=` at `mod1_completion_path` call sites.
Check filed scans for an empty Licença FPB box, and check stored upload
responses for `"has_license": null` on a `reg_type` 2 document.

`FIX:` none needed at the call site — upgrade and the licence is filled. Modelo
1s already filed with the federation carry a blank licence and need correcting
by hand.

---

## 0.100.1 — 2026-09-10

### Changed

**`sav-parsers` pin moved to `ac80cfe`**
`IMPACT: none`. The pin advances to the tip of sav-parsers `main`; the only
delta from the previous pin (`215973a`) is a new test file in that repo, so no
shipped parser code changed and no behaviour here moves with it. Recorded so
the pin's history stays readable — a pin that jumps commits with no entry looks
like it might have carried a silent change.

---

## 0.100.0 — 2026-09-09

### Added

**`tier_for_birth_date(birth_date, season_start_year)` — the escalão rule, executable**
`IMPACT: none` (new function; nothing existing changes). `enrollment_fields()`
documents `escalao` as derivable, but until now nothing derived it: consumers had
to read `TIER_AGE_RANGE_IN_SEASON` and invert the age windows themselves. This is
that inversion, living in `sav_shared/lookups.py` next to
`tier_birth_years_for_season`, whose exact inverse it is — a test asserts every
year the forward function enumerates maps back to its tier, so the two cannot
drift into placing players in different escalões.

Accepts a `date` or an ISO/European string (same tolerance as
`split_date_parts`) and returns the tier name, or `None` for a blank/unparseable
date or a player younger than Baby-Basket.

Two properties worth knowing, both asserted by tests:

- **Only the birth year matters.** Escalões are birth-year cohorts, so the day
  and month are ignored: two players born eleven months apart in one calendar
  year always share a tier, while two born a month apart across New Year can
  differ. Passing a full date is a convenience, not a precision.
- **The tier name is gender-independent.** Names are identical across genders;
  only the SAV2 tier *ids* are renumbered. Get the id with
  `find_id_by_name(name, player_registration_tiers(gender_id))`.

**Known limitation:** ages 19-20 come back as `"Sénior"`. `Sub 20` is
deliberately absent from `TIER_AGE_RANGE_IN_SEASON` (its window would overlap
Sénior's and break the contiguous-ranges invariant), so a club that registers
those players as Sub 20 must override. Masters/Veteranos and BCR are unmodelled
for the same reason and are never returned. Treat the result as the default to
offer, not an authority to submit unreviewed.

### Changed

**`enrollment_fields()` docs: `escalao` needs `nasc` alone, not `nasc` + `genero`**
`IMPACT: none` (documentation only). 0.99.1 and 0.99.2 said `escalao` follows
from `nasc` + `genero`. The gender is not part of the derivation — the tier name
is the same for both genders, and `genero` is needed only to resolve that name to
a SAV tier id. The docstrings now say so and point at `tier_for_birth_date`.

`DETECT:` code that refuses to compute an escalão until it has a gender.
`FIX:` derive from the birth date alone; bring in `genero` only when you need the
SAV tier id.

---

## 0.99.2 — 2026-09-09

### Changed

**`enrollment_fields()` sorts the non-human fields into three kinds, and flags the weak one**
`IMPACT: none` (documentation only; no row changes shape or value). 0.99.1 said
five always-required fields are "not human input" and listed them flat. They are
not equivalent, and treating them as one bucket invites a wrong inference:

- **Computed** — `escalao`, a pure function of `nasc` + `genero` against the tier
  age windows in `sav://lookups`. Never ask for it.
- **Context constants** — `clube` and `associacao` (the session's club),
  `data_assinatura` (filled at stamping).
- **Read off the player's record, when there is one** — `license` and
  `tipo_inscricao`, which are one question: *does this player already hold a
  licence with this club?* Resolve both through `get_enrollment_status`.

The third kind is explicitly weaker than the other two. **`has a licence →
Revalidação` is wrong for a player licensed at another club** — that is a
Transferência (reg_type 3), which the Modelo 1 cannot express at all: the form
carries only the `primeira` and `revalidacao` boxes. So `tipo_inscricao` is
genuine human input exactly when the player is unknown or transferring in, and
otherwise follows from their record. `complete_mod1`'s `2 if license else 1`
stays sound only where it is, pre-submission; `submit_enrollment` continues to
use the enrollment's authoritative `reg_type`.

Still documented rather than modelled as a column — the row shape is unchanged.

`DETECT:` a consumer that derives `tipo_inscricao` from the presence of a licence.
`FIX:` resolve it through `get_enrollment_status`, and ask a human when the
player is unknown or transferring in — an inferred Revalidação silently files a
transfer as the wrong registration type.

---

## 0.99.1 — 2026-09-09

### Changed

**`enrollment_fields()` now labels every field, and says which ones are not human input**
`IMPACT: none` (additive; no key changes shape or disappears). Two gaps in
0.99.0's tool, both about it being usable to build an intake form:

`label` was `null` for the 13 fields with no `FieldDef` — including `nome`,
which is as central as a field gets. `sav_shared/fields.py:FIELDS` now carries a
**label-only** `FieldDef` (a `key` and a `label`, nothing else) for each of them,
so every row has a Portuguese label from the one field registry. These rows have
no `ocr_entity`, `sav_kwarg` or `profile_html`, and all four constants derived
from `FIELDS` (`RECONCILE_TEXT`, `RECONCILE_READONLY`, `ENROLLMENT_FIELD_META`,
`PROFILE_HTML_FIELDS`) filter on exactly those — so they are inert: the derived
values are byte-identical before and after, and a test now asserts the label-only
keys stay out of all four. If you add such a row, keep it label-only; giving one
an `ocr_entity` or `sav_kwarg` enrolls it in the reconcile and submission paths.

`required_when` also read as "a human must type this", which is wrong for four
always-required fields. The docstrings now say plainly that it describes what the
*form* requires, and that `escalao` follows from `nasc` + `genero` (the tier age
windows in `sav://lookups`), `clube`/`associacao` are club constants, `license`
comes from the player's SAV record, and `data_assinatura` is filled at stamping.
This is documented rather than modelled as a new column — the row shape is
unchanged.

`DETECT:` a consumer that renders an input for every row of `enrollment_fields()`,
or that special-cased a `null` label.
`FIX:` drop the null-label handling, and skip `escalao`, `clube`, `associacao`,
`license` and `data_assinatura` on intake forms — derive or supply them instead.

---

## 0.99.0 — 2026-09-09

### Added

**`enrollment_fields()` — the enrollment field surface, as data instead of prose**
`IMPACT: none` (new tool; nothing existing changes). Until now the only
description of which fields an enrollment takes was the `values:` paragraph of
`fill_mod1`'s docstring. `sav://lookups` exported the enum *tables* but never the
*fields*, so an application that needed to know "what fields exist, of what type,
and which are mandatory" had to hand-copy that paragraph into its own source —
and at least one did, acquiring a duplicate table that would drift the first time
a key was renamed here. This tool is that answer in machine-readable form.

Takes no arguments, reaches no SAV endpoint, and needs no session: it describes
the server's own schema. Returns one row per Modelo 1 field, in printed-form
order:

```json
{"id": "tipo", "label": "Tipo de Documento", "type": "enum",
 "required_when": "always", "enum_ref": "id_types",
 "values_key": "tipo", "field_overrides_key": "id_type"}
```

`values_key` is the key inside `fill_mod1`'s `values` dict; `field_overrides_key`
is the equivalent key inside `submit_enrollment` / `update_enrollment`'s
`field_overrides`. They differ often enough to matter (`tipo` → `id_type`,
`tele` → `telemovel`, `codpostal` → `cod_postal`, `distrito` → `distrito_id`), and
a `null` `field_overrides_key` means the field genuinely has no submit kwarg —
`nif` and `nasc` are cross-checked against SAV but never written.

`required_when` is the Modelo 1 mandatory-fill rule: `always`, `revalidacao`
(`license` only), `minor` (the seven `guardian_*` fields), or `optional`
(`data_assinatura` alone). **The `minor` rule is load-bearing.** SAV2 itself
accepts a minor with an empty guardian block, so a consumer must enforce it
rather than assume the federation will.

`enum_ref` names the `sav://lookups` key enumerating a field's legal values, so a
consumer validates enums against the live bundle instead of a second copy of the
table. It is deliberately independent of `type`: `distrito` is free text on the
form (`type: "text"`) but takes a `distrito_id` from the `distritos` table on
submission, so it carries `enum_ref: "distritos"` anyway. `concelho` carries
`null` — concelhos are distrito-dependent and fetched live from SAV, so the
bundle has no such key.

Every row is *derived* from `MOD1_FILL_MAPPING` and the mandatory-fill constants
by `sav_shared.fpb_mod1.enrollment_field_schema()`, which is the point: renaming a
key changes this output automatically. Nothing here is hand-maintained. `exam_date` gets no
row for the same reason: it is a `submit_enrollment` field with no Modelo 1 slot
and no field definition to derive from.

`authz.toml` gives it `roles = ["coach", "parent", "player"]` — it names no
subject and touches no record.

`DETECT:` `grep -rn "tipo_inscricao\|guardian_id_type\|localidade_txt" --include=*.py .`
in your own codebase — a hand-maintained copy of the enrollment field list.
`FIX:` replace it with a call to `enrollment_fields()`, and resolve enum values
through `sav://lookups` using each row's `enum_ref`.

---

## 0.98.0 — 2026-09-06

### Added

**`complete_mod1(pdf_b64, license?)` — the submission's completion overlays, without the upload**
`IMPACT: none` (new tool; nothing existing changes). A caller can now inspect
the exact Modelo 1 artifact `submit_enrollment` files: the shared completion
path marks the tipo_inscricao checkbox, fills a missing Revalidação licence, and
applies the club carimbo from the server's `$CLUB_STAMP_PATH`, including the
existing idempotent stamp/date behaviour. The optional `license` selects the
type: omitted/zero means 1ª Inscrição (`reg_type_assumed=1`), while a real
licence means Revalidação (`reg_type_assumed=2`) and is written to the blank
`nr_licenca` field. A number already on the form is never overwritten.

Returns `{filename, size_bytes, pdf_b64, has_club_stamp, has_inscricao_mark,
has_license, reg_type_assumed}` plus `stamp_warning`, `inscricao_warning`,
and/or `license_warning` only when an overlay could not be applied. A `fill_mod1`
AcroForm uses its fixed slots without OCR; a member-supplied scan uses Document
AI for the licence and carimbo slots. When no stamp path is configured or OCR
fails, `has_club_stamp` remains `None` rather than claiming the form was
inspected.

Because the type is *derived* rather than read off an enrollment, it is treated
as the weaker claim: if the form's other tipo_inscricao box is already ticked,
the mark is skipped and reported via `inscricao_warning` with
`has_inscricao_mark: None`, rather than producing an attestation with both boxes
ticked. Only reachable on a scan — a template form's unticked boxes read as
unknown, so the inscription overlay already skips them. `submit_enrollment`
uses the enrollment's authoritative `reg_type` rather than this inferred type,
and also fills the supplied licence on Revalidação forms as described below.

Touches no SAV endpoint: it creates no batch and uploads nothing, unlike
`preview_enrollment`, which mints a real batch. `authz.toml` gives it
`roles = ["coach"]` with no `self_scope` — the output is a club-endorsed
attestation, so a parent or player must not be able to mint one. The same
warning `fill_mod1` carries applies: do not hand this PDF to the player.

**`sav mod1 complete <pdf> --out <path> [--license N]`**
`IMPACT: none` (new command). The CLI counterpart of `complete_mod1`: applies
the tipo_inscrição mark, the Licença FPB and the club carimbo, and writes the
result. Contacts SAV for nothing. Accepts an image as well as a PDF. Reports
each overlay, and on an OCR failure writes **no** file rather than one that
looks completed but is not.

### Fixed

**Overlays were placed wrongly on every rotated scan**
`IMPACT: silent` — and it reached the federation. `bbox_to_pdf_rect` mapped
Document AI's normalized vertices straight onto the page's **mediabox**,
ignoring `/Rotate`. Document AI measures the page as *displayed*; the mediabox
is the unrotated space. On a scan with a landscape mediabox and `/Rotate 270` —
routine scanner and phone-camera output — the club stamp landed in the middle
of the Escalão row instead of the Diretor/Carimbo box, and the upload reported
success, because `applied` only ever meant "we drew something", not "we drew it
in the right place".

This affected every overlay path, not just the new tooling: `sav enrollment
create`, `submit_enrollment`, `upload_player_document` and
`replace_player_document` all share the conversion.

Two further errors compounded it, both now fixed by calibrating in the space OCR
measured in and converting once: the club stamp's nudge followed user-space
`+y`, which on a rotated page is *sideways* on screen; and the licence text
sized itself from the converted rect's height, which on a rotated page is the
slot's on-screen *width*.

`DETECT:` any Modelo 1 or Modelo 4 filed from a scan rather than a `fill_mod1`
form. Check for `/Rotate`:
`python -c "from pypdf import PdfReader; p=PdfReader('form.pdf').pages[0]; print(p.get('/Rotate',0))"`
— anything other than `0` means the stamp on the filed copy is misplaced.
`FIX:` nothing to change in your code. Re-stamp and re-upload affected
documents; forms produced by `fill_mod1` were never affected, since that path
uses a fixed rect and no OCR.

**`sav mod1 complete` no longer hangs on expired Document AI credentials**
`IMPACT: none` (new command). `sav_parsers.document_ai.process_document` is
called with no `timeout`, so an unusable ADC token became a silent retry loop —
observed hanging for minutes with no output. The command now verifies
credentials before any call that needs them and fails in under a second with
`Run gcloud auth application-default login`. Only definitive auth errors are
fatal, and only on the OCR path: a `fill_mod1` form is completed offline and
never touches credentials. The underlying missing timeout is a sav-parsers
issue and still open.

### Changed

**Anchor-to-slot geometry moved to sav-parsers; the parser is now pinned**
`IMPACT: raises` if you run an older parser. sav-parsers `0.10.0`+ returns the
writable **slot** on `*_presente` bboxes rather than the labelled anchor, so the
corrections that lived here (`_CLUB_STAMP_SCALE`, `_CLUB_STAMP_Y_SHIFT`,
`_LICENCA_RIGHT`/`_WIDTH`/`_HEIGHT`, and the `adjust_normalized_box` /
`anchor_to_slot` helpers) are deleted rather than kept as shims. Placement is
byte-identical — verified rect-for-rect against the pre-migration output on a
real rotated scan — because this relocated the maths without recalibrating it.
`pyproject.toml` pins the parser to a SHA instead of `@main`: an older parser
would hand back the labelled caption and silently place overlays on it.
`DETECT:` `grep -rn "adjust_normalized_box\|anchor_to_slot" .`
`FIX:` drop those calls and pass the presence bbox straight through — it is
already the slot. Do not offset it further.

**`sav enrollment create` now fills the Licença FPB on Revalidação forms**
`IMPACT: silent` — the CLI now uses the same shared completion overlays as
`submit_enrollment`, so a blank Revalidação licence field is filled before the
Modelo 1 upload. 1ª Inscrição forms still leave that field blank.
`DETECT:` `grep -rn "_prepare_club_stamp" .`
`FIX:` no caller change is required; existing CLI invocations get the corrected
upload behaviour.

**`submit_enrollment` fills the Licença FPB on Revalidação forms before upload**
`IMPACT: silent` — the shared submission completion path now writes the
supplied licence into a blank `nr_licenca` field on a `fill_mod1` AcroForm, or
into the OCR-located `licenca_fpb_presente` slot on a member-supplied scan.
Existing numbers and 1ª Inscrição forms remain unchanged; the submission
response now reports `has_license` and `license_warning` alongside the other
Modelo 1 completion status.
`DETECT:` `grep -rn "submit_enrollment\|_replace_player_document_from_bytes" .`
`FIX:` no caller change is required. If a Revalidação is already queued or was
filed before this release, inspect its Modelo 1 and add the licence manually
when `has_license` was false or a `license_warning` was returned.

---

## 0.97.0 — 2026-09-05

### Added

**`classify_documents(documents)` — read-only document typing, for every type SAV files**
`IMPACT: none` (new tool). Answers "what kind of document is this file" for a
caller holding a pile of PDFs, without reading or writing SAV. Each entry takes
exactly one key, `pdf` (base64 PDF or image bytes; images are converted);
unknown keys are an error, matching `parse_enrollment_forms`. Returns one row
per input in input order: `{index, doc_type, uploadable}` or `{index, error}`.
Documents are classified concurrently (4 workers, the same bound
`parse_enrollment_forms` uses).

This exists because there was **no read-only way to identify a supplementary
document**. `parse_enrollment_forms` errors with `Unsupported document type` on
`atestado_residencia`, `certidao_matricula` and `documento_identificacao`,
because it goes on to extract fields and only fpb_modelo_1 / exame_medico /
fpb_modelo_4 have field extractors. Those three types are precisely what a
foreign-born player's checklist turns on, so the only tool that could name them
was `upload_player_document` with `doc_type` omitted — which writes to SAV.
Reconciling a club's document store against SAV therefore required uploading
first. It no longer does.

`outros` is a normal result, not an error: "this file is not an enrollment
document" is the common answer for a folder that also holds team photos. Unlike
`parse_enrollment_forms`' `doc_type` hint path, this never calls
`train_classifier` — it has no label to train on. One Document AI round-trip per
document, and no caching, so cache against your own file identity if you scan
repeatedly.

**`document_requirements(reg_type, nationality_id?, available_doc_types?, license?)` — the FPB rule, standalone**
`IMPACT: none` (new tool). Exposes `compute_enrollment_checklist` with no SAV
lookup, for a player SAV cannot ground: `get_enrollment_status` keys on a
licence, and a genuinely new player doing a 1ª Inscrição has none. Returns the
same `{scenario, reg_type, required, optional, missing}` shape, or null for
reg_type 3 (Transferência, still not modelled).

Omitting `nationality_id` yields the **foreign_born** set, matching
`compute_enrollment_checklist`'s existing defensive default — asking for
documents that turn out to be unnecessary is recoverable; declaring someone
ready when they are not is not. Do not pass 155 from a Portuguese-looking name
or a form field; pass it only from a SAV record. Prefer `get_enrollment_status`
for anyone who has a licence, since it reads their real nationality and their
live batch instead of taking the caller's word for either.

Named for *the rule*, not for a player, because `get_enrollment_status` answers
the same question for anyone who has a licence and answers it better — grounding
nationality in their SAV record instead of the caller's word. Two guards enforce
that split: passing a `license` here **raises**, pointing at the grounded tool;
and every checklist now carries `nationality_source` (`"caller"` here,
`"sav_record"` from `get_enrollment_status`), so a consumer holding one can tell
a guessed answer from a grounded one. `IMPACT: none` for existing callers of
`get_enrollment_status` — `nationality_source` is purely additive.

`available_doc_types` takes one entry per document — duplicates are significant,
because foreign_born requires **two** `documento_identificacao` and SAV files
both under `tipo_doc=18`, so they can only be counted. An unrecognised doc type
raises rather than being dropped: a silently ignored typo would report a
document as missing that the caller believes they supplied.

### Changed

**`get_enrollment_status` accepts `available_doc_types`**
`IMPACT: none` when the parameter is omitted — the response is byte-for-byte
what it was. Supplying it folds documents the caller holds **outside** SAV into
the checklist counts, so the answer becomes "what is still missing overall"
rather than "what has SAV been given so far". The response then additionally
carries `available_doc_types` (echoed back, normalised) and the checklist gains
`counts_include_available: true`. Applies to all three statuses: `pending` takes
the union of the live batch's uploads and the supplied set; `enrolled` and
`not_enrolled` keep `projected: true` (SAV holds no batch uploads) while the
counts become meaningful against the supplied set.

---

## 0.96.1 — 2026-09-04

### Fixed

**`prepare_overlay_image` no longer skips cropping on images with incidental alpha**
`IMPACT: silent` — 0.96.0 gated *both* keying and cropping behind
`min(alpha) < 255`. A signature captured on an HTML canvas is RGBA whether or
not anything is translucent, so one column of anti-aliased edge pixels — or a
devicePixelRatio re-blit — was enough to classify a blank canvas with a stroke
on it as a deliberately prepared cutout. It was passed through with its margins
intact, and since `add_overlay` fits the image inside a fixed rect preserving
aspect, the margins dominated the fit. A 960×526 export with `min(alpha)=254`
(0.1% of pixels non-opaque) drew its ink **58×32pt** inside
`_PLAYER_SIGNATURE_RECT` where an opaque capture of the same signature drew
190×27pt — the same field rendering at a third the width depending on which
app produced the file.

Two changes:

- **Cropping is no longer gated on alpha.** It runs whenever ink can be told
  from background at all — from a cutout's own alpha, or from the luminance key.
  Blank margins misplace a transparent image exactly as much as an opaque one;
  the placement factors are calibrated against the ink either way.
- **Keying is still gated, on a test that survives anti-aliasing.** `_is_cutout`
  now asks whether the *border* is transparent (symmetric with the existing
  `_border_is_light`) instead of whether any pixel is. A genuine cutout still
  passes through un-keyed, so its artwork keeps its holes.

**New:** `prepare_overlay_image(data, crop=False)` keys without cropping, for an
image whose padding belongs to a calibrated placement. Both club-stamp paths now
say so explicitly — `fpb_mod1.overlay_club_stamp`, `render_mod1`'s `club_stamp`
argument, and `fpb_mod4.club_signature_overlay` — rather than relying on the
alpha channel to imply it. `$CLUB_STAMP_PATH` output is byte-for-byte what
0.96.0 produced.

`DETECT:` any caller-supplied signature that reaches `render_mod1` or a mod4
overlay as a PNG with an alpha channel — canvas/tablet captures in particular.
Compare rendered ink width against a known-good form.
`FIX:` nothing to change. Signatures that were rendering too small now fill
their rect; re-render any form still awaiting submission.

---

## 0.96.0 — 2026-09-04

### Changed

**Signature and stamp images are now keyed and cropped before they are overlaid**
`IMPACT: silent` — every caller-supplied overlay image (`render_mod1`'s
`player_signature` / `guardian_signature` / `club_stamp`, mod4's
`--detentor-signature`, and `$CLUB_STAMP_PATH`) passes through the new
`sav_shared.files.prepare_overlay_image` before compositing. An **opaque** image
whose border reads as paper has that background keyed to transparent and is
cropped to its ink; the overlay then renders smaller-framed and larger-inked
than before.

Two bugs motivated it, both from applications feeding photographed or scanned
signatures. An opaque raster paints a white box over the form's printed
signature line, hiding it. And whitespace margins corrupt placement, because
every overlay derives its geometry from the image's own pixel dimensions —
`fpb_mod4.overlay_signature` takes the aspect ratio from `image_size`, and
`render_mod1` relies on `add_overlay` fitting the image inside a fixed rect
preserving aspect. The placement factors are calibrated against the ink, so
padding silently shrank and misplaced the result.

Images are returned **unchanged** when the keying does not apply: one that
already carries real transparency (a deliberately prepared PNG, such as a
typical `$CLUB_STAMP_PATH` file, whose padding may be part of a calibrated
placement), one whose border is too dark to read as paper, or one that keys to
nothing. So a correct transparent stamp is untouched.

`DETECT:` `grep -rn "player_signature\|guardian_signature\|club_stamp\|detentor.signature\|CLUB_STAMP_PATH" .`
`FIX:` nothing to change to get the fix. If you were relying on an image's
whitespace to position it, crop it yourself and add the padding back as
transparency — an image with an alpha channel is passed through verbatim.

### Fixed

**The guardian signature is no longer placed to the right of its printed line**
`IMPACT: silent` — `_GUARDIAN_SIGNATURE_RECT` was `(215, 72, 500, 97)`, centred
at x=357.5. The template's "Assinatura ____" line runs x 191.5 → 408.4, centre
299.9, so every guardian signature `render_mod1` produced sat ~58pt (2cm) right
of centre, overran the line's right end by ~92pt, and floated below the line
rather than resting on it. Now `(195, 78.5, 405, 97.5)`: centred on the line,
inside its ends, bottom on the baseline where the underscore glyph draws.

Nothing reported it because these are coordinates, not form fields — there is
no readback. `TestGuardianSignatureRect` in `tests/test_mod1_fill.py` now
re-derives the line's position from the template's own text run and font
widths, so a template change fails the suite instead of shifting signatures.

`DETECT:` visual only — open a form produced by `render_mod1(...,
guardian_signature=...)` before 0.96.0.
`FIX:` nothing to change. Re-render any form still awaiting submission if the
placement bothers you; a form already filed is unaffected.

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
