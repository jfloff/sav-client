# sav-mcp — Agent Reference

MCP server for the FPB SAV2 basketball management system. Authoritative reference for an LLM driving this server through tool calls.

> **Pinned to an older release?** This document describes the *current* one.
> Read [`CHANGELOG.md`](../CHANGELOG.md) for everything that changed since yours, and
> start with the entries marked `IMPACT: silent` — those change response
> shape without raising, so your code keeps running and reads the wrong
> field. Each carries a `DETECT:` grep to run against your own codebase.

This file is intended to be loaded as the LLM's system prompt (or first context message). It documents the workflow, terminology, and enum tables an LLM needs to use the tools effectively without making extra discovery calls.

## Terminology

| Term | Meaning |
|------|---------|
| **licence** (licença) | Player registration number, numeric (e.g. `301772`). Human identifier. |
| **wallet** (carteira) | Coach registration number. Distinct from licences. |
| **batch** (lote / guia) | A "Lote de Inscrição" — group of player registration requests of one type, locked to one tier+gender. |
| **batch_number** | Human-visible batch identifier (string). All MCP tools accept the number, not the internal id. |
| **tier** (escalão) | Age category (e.g. "Mini 12", "Sub 14", "Sénior"). `tier_id` is numeric; `tier_name` is free-text. |
| **association** (associação) | Regional body. Numeric `id` from `list_associations`. |
| **club** (clube / organizacao) | Numeric club ID. `club_id=0` means federation-wide search. |
| **season** (época) | SAV2 epoch ID. `None` defaults to current season; `0` means all seasons. |
| **val** | `1` = home team, `2` = away team. Tools expose this as `team: "home" \| "away"`. |
| **artifact_id** | UUID returned by `parse_enrollment_forms` referencing a cached parse result. fpb_modelo_1 results expose this also as `mod1_id`; exame_medico results expose it also as `medical_exam_id`. |
| **needs_review** | Field-level OCR confidence is too low to trust; the user must confirm or correct. |
| **player** | Canonical English term — never "athlete". Tool names, parameters, and English responses use `player`. Portuguese user-facing replies may use `jogador` or `atleta` (both natural to coaches). The upstream SAV2 API uses `atleta` as a JSON field name — wire contract, untouched. |

## Sessions

`get_session_info` returns the authenticated context — `club_id`, `season_id`, `season` (label like `"2025/2026"`), `season_start_year`, `season_end_year`, `user`, `profile`. It is the **source of truth for the current season**, read from SAV2's dedicated season table (the active época), so it resolves even off-season — before any registration batch for the new época exists (the season label/year fields fall back to `None` only if that lookup itself fails). Tools that scope by "the session's club" default to that `club_id`. Pass an explicit `club_id` to override, or `0` to search federation-wide. Read the season fields from here — never infer them from a resolved player, batch, or game row.

## PDF convention

All PDFs cross the MCP boundary as **base64-encoded strings**.

- Inputs: `parse_enrollment_forms(documents=[{"pdf": b64}, ...])`, `upload_player_document(pdf_base64=...)`, `replace_player_document(pdf_base64=...)`, `update_enrollment_with_document(pdf=...)`.
- Outputs: `generate_game_sheet_pdf` and `fill_mod1` return `{filename, size_bytes, pdf_b64}` — decode `pdf_b64` to bytes to use.

## Date convention

**Every date this server emits is `YYYY-MM-DD`, and every date it accepts should be written that way.** SAV2 itself is not consistent — its wizard and profile endpoints use ISO while its listing endpoints use `DD-MM-YYYY` — and normalising that away is part of what this server is for.

| Direction | Rule |
| --- | --- |
| Values that reach a SAV commit (`exam_date`, `id_expiry`, `birth_date`, and every date in a `values` dict) | `YYYY-MM-DD` **enforced**. `13/05/2026` is rejected, not converted — guessing the caller's convention is how a wrong date gets filed with the federation. |
| Read-only filters (`list_games`, `list_game_sheets` `date_from`/`date_to`) | `YYYY-MM-DD` preferred; `DD-MM-YYYY` still accepted, since nothing is written. |
| Everything returned | `YYYY-MM-DD`, including `date` on game rows and `tptd_expiry` on coaches. |

`exam_date` additionally has to fall inside SAV's validity window — see [Domain rules](#domain-rules).

## Identifier convention

**Licences are integers everywhere on this surface** — inputs and outputs, search rows and batch rows alike. `Player.license` used to be a string while batch rows were ints, so a wrapper that stored `"301772"` from a search and compared it against `{301772}` silently failed its authorization check.

**MCP responses identify players by licence and never by SAV's internal player id.** Internal ids remain private workflow state and are not MCP parameters or outputs; use `existing_license` on a 1ª Inscrição duplicate result when it can be resolved.

`0` means "no licence" (e.g. a detail-only row), never a real player.

**NIF is nine digits**, with separators accepted on input and stripped on output. The same rule now applies at every entry point — the Modelo 1 validator previously only checked the field was non-blank, so `nif="abc"` passed the form and reached SAV's create-player call. Digits are never scraped out of surrounding text: `"NIF 289463491"` is rejected rather than silently accepted.

## Enum tables

### Registration types (`reg_type`)
| ID | Label |
|----|-------|
| 1 | 1ª Inscrição |
| 2 | Revalidação |
| 3 | Transferência |
| 4 | Subida |

### Gender (`gender_id`)
| ID | Label |
|----|-------|
| 1 | Masculino |
| 2 | Feminino |

### ID document types (`id_type`, used in `field_overrides`)
| ID | Label |
|----|-------|
| 1 | Cartão de Cidadão |
| 2 | Passaporte |
| 3 | Título de Residência |

### Guardian relations (`guardian_relation`, for minors)
| ID | Label |
|----|-------|
| 1 | Pai |
| 2 | Mãe |
| 3 | Tutor |

### Batch states
| State | Open for new items? |
|-------|---------------------|
| Em construção | yes |
| Devolvida | no |
| Em Validação | no |
| Em Pagamento | no |

### Game statuses
MCP game tools expose the same canonical `status`: `scheduled` (`Marcado`),
`played` (`Realizado`), `not_scheduled` (`Não Marcado`), `postponed`
(`Adiado`), or `cancelled` (`Anulado`). `unknown` is not a SAV state; it is an
explicit sentinel for an unmappable SAV value, preserved in `status_raw`.
`has_result` is a separate boolean: true only when both scores are valid
integers, independent of fixture status.

### Document types (`doc_type` strings)
`fpb_modelo_1` — main enrollment form (`tipo_doc=1`). `exame_medico` — medical exam (`tipo_doc=2`). `fpb_modelo_4` — Subida form (`tipo_doc=6`). Supplementary uploads are also wired: `atestado_residencia` (`15`), `documento_identificacao` (`18`), `certidao_matricula` (`24`), and `outros` (`22`).

Use `list_tiers(gender_id)` to get the gender-specific numeric `tier_id`: tier names/categories are identical across genders, but IDs differ by gender. The hardcoded table is stable across seasons.

## Domain rules

For roster questions about an escalão ("Que jogadores são Sub-X?", "atletas para o próximo ano") call **`roster_for_escalao(tier_id, gender_id, when="next"|"current")`**. The tool resolves both birth years deterministically and runs a fallback cascade (`club + active → club + all`), reporting which `step` matched — so the LLM never does the arithmetic or the retries. It is single-club: `club_id` must resolve to a real club (it raises otherwise), and it never widens to the federation. Fall back to `search_players(birth_year=[...])` only for genuinely custom queries (e.g. multiple escalões at once, or a federation-wide search via `club_id=0`).

**Targeting a season — relative vs absolute:**
- `when="current"`/`"next"` is a season *relative to today*, resolved server-side. **Prefer this** for "current / próxima época" questions: you do not need to know today's season, and it avoids guessing the season from the calendar year (they diverge May–Sept).
- `season_year` (start year; `2020` = "2020/2021") names an *absolute* season and overrides `when`. Use it only when the user names a specific season ("em 2020/2021"). Do **not** try to compute "next" yourself by passing `season_year` — that reintroduces the calendar trap; use `when="next"`.

**Three regimes** follow from how the target relates to today:
- **Future season** (`when="next"`, or a `season_year` ahead of today) is a **projection**, not a query for that season's enrollment — enrollment only ever exists for the current season. The tool keeps known players whose birth year lands in that season's window, returning `is_projection=true` and `source="projection_by_birth_year"`. An empty `players` list means "no known player projects into that cohort", never "missing data".
- **Current season** (`when="current"`) and **past seasons** (`season_year` ≤ today) reflect actual enrollment, queried at that season's own epoch; `is_projection=false` and `source` stays `club`/`none`.

Knowledge to drive the tool correctly:

- Each escalão spans **two consecutive birth years**. For season `Y/Y+1`, Sub-X = born in `Y+1−X` and `Y+2−X`; same for Mini 8/10/12.
- **Sénior** is open-ended below (no upper birth year — the tool filters by tier name). **Baby-Basket** spans three years (ages 4–6 in `Y+1`); the two youngest require the child to have completed 4 years before enrollment. **Masters / Veteranos** and **BCR** — `<TODO: confirm with user>`; `roster_for_escalao` raises so the LLM doesn't guess.
- "Próximo ano / próxima época" advances the season label by one (`season_id + 1`; SAV2 `epoca_id` is sequential), never the calendar year. But there is **no next-season roster to fetch** — enrollment only happens in the current season — so a next-season roster is always a projection of the current pool by birth year. `roster_for_escalao(when="next")` does this; doing it by hand means querying the *current* `epoca_id` and filtering by next season's birth years, never `season_id + 1`.
- Between May and September the wall clock straddles a season transition: a player listed as inactive in the current season is almost certainly "not yet re-registered", not retired — the tool's `club + all` cascade step (status="all") surfaces them.

### Medical exam validity (`exam_date`)

SAV derives a licence's validity as **exam date + 12 months** (the commit returns it, e.g. `dataexame 2026-08-01` → `2027/08/31`). So `exam_date` must be in the past and no more than 12 months old, and sav-client rejects anything outside that window before the commit runs.

This matters because SAV's own rejection is unreadable: a future date comes back as `{"val":0,"msg":"","resultfunction":"-1"}` — an empty reason, which SAV2's web UI cannot explain to its users either. A client-side `ValueError` naming the bound is the only legible error available, so treat it as the real answer rather than retrying with a different guess.

### Birth-year windows

For season `Y/Y+1`. Concrete column shows 2025/2026 (`Y = 2025`).

| Escalão | Birth years | 2025/2026 |
|---------|-------------|-----------|
| Baby-Basket | `Y+1−6 .. Y+1−4` (ages 4–6 in `Y+1`; two youngest need 4 completed years) | 2020, 2021, 2022 |
| Mini 8 | `Y+1−8`, `Y+2−8` | 2018, 2019 |
| Mini 10 | `Y+1−10`, `Y+2−10` | 2016, 2017 |
| Mini 12 | `Y+1−12`, `Y+2−12` | 2014, 2015 |
| Sub 14 | `Y+1−14`, `Y+2−14` | 2012, 2013 |
| Sub 16 | `Y+1−16`, `Y+2−16` | 2010, 2011 |
| Sub 18 | `Y+1−18`, `Y+2−18` | 2008, 2009 |
| Sénior | `Y+1−18` and earlier | 2007 and earlier |

When falling back to `search_players` directly: never drop one of the two birth years; for a next-season projection query the **current** `epoca_id` (not `season_id + 1`) filtered by next season's birth years.

### Worked example

Coach: *"Que jogadores são para o ano Sub-14 masculinos?"* (next season). One call:

`roster_for_escalao(tier_id=5, gender_id=1, when="next")`
  → `{tier: "Sub 14", season: "2026/2027", birth_years: [2014, 2013], is_projection: true, source: "projection_by_birth_year", step, players}`.

Report the `players` list, framing it as a projection — "atletas que, pelo ano de nascimento, passam a Sub-14 na próxima época". An empty `players` list here means no known club player projects into that cohort by birth year — say so honestly rather than implying enrollment data is missing.

## Enrollment workflow

The canonical pipeline. Each step's output feeds the next.

```
1. parse_enrollment_forms(documents=[{"pdf": b64, ...}, ...])
     → [{artifact_id, mod1_id, doc_type, reg_type, tier_id, gender_id, ...}, ...]
       (one entry per document; medical exams return medical_exam_id instead of mod1_id)

2. ensure_open_batch(reg_type, tier_id, gender_id)  → {..., created: bool}
     → batch_number
   (get-or-create in one call. find_open_batch / create_batch still exist for
    explicit control, but prefer ensure_open_batch — one round-trip, no race.)

3. preview_enrollment(batch_number, license: null, mod1_id, medical_exam_id?)
     → {player, resolved?, fields: [{kwarg, status, sav_value, ocr_value, final_value}, ...], needs_review: [...]}
       license: null auto-resolves the player from the form (folds in resolve_player).
       On a clean single match the normal preview proceeds (player.license carries the
       resolved licence, plus resolved: true for a Revalidação). Otherwise it returns:
         {resolved: false, candidates: [...]}         ── ask user to pick, re-call with explicit license
         {resolved: false, candidates: []}            ── ask user for licence, re-call
         {resolved: false, error: "player_already_in_sav"}  ── 1ª Inscrição duplicate → use Revalidação
       Field status values:
         "updated"      OCR overrides SAV
         "match"        SAV kept (OCR matched)
         "needs_review" low OCR confidence, OR (for a minor) an absent guardian field — user must supply
         "ocr"          field not in SAV (id_type, guardian_*, consent_*)
       For a minor, the four guardian fields not already carrying a value are appended to
       needs_review here so they're collected before submit. Call resolve_player explicitly
       first only when you need to show the candidate list before previewing.

4. submit_enrollment(batch_number, license, mod1_id, field_overrides={...}, medical_exam_id?)
     → {success: true, license, source_document_upload, medical_exam_upload}
     or {success: false, missing_guardian_fields: [...]}  ── fallback: retry with guardian fields added
```

### Per-document keys — what each one skips

Every `documents` entry is self-contained. Unknown keys are errors, so misspellings do not silently trigger OCR.

| Key | Required | Meaning |
| --- | --- | --- |
| `pdf` | yes | Base64-encoded PDF bytes. |
| `doc_type` | no | `"fpb_modelo_1"`, `"exame_medico"`, or `"fpb_modelo_4"`; omit to auto-classify. |
| `values` | no | Canonical Modelo 1 values dict (the `fill_mod1` shape); implies `doc_type="fpb_modelo_1"`. |
| `exam_date` | no | `"YYYY-MM-DD"`, validated on entry; implies `doc_type="exame_medico"`. |

| Entry | Classification | Document AI extraction | Classifier trained |
| --- | --- | --- | --- |
| `pdf` only | runs | runs | — |
| `pdf` + `doc_type` | skipped | **runs** | yes, with the given label |
| `pdf` + `values` | skipped | skipped | — |
| `pdf` + `exam_date` | skipped | skipped | — |
| `pdf`, filled Modelo 1 template | skipped | skipped (AcroForm probe) | — |

**`doc_type` skips classification only. `values` and `exam_date` are the trusted-value paths that skip Document AI field extraction.** A filled Modelo 1 produced from this repository's template is also read directly from its AcroForm without OCR, including after signature or stamp images have been overlaid.

These fast paths skip parse-time OCR, not every possible OCR call. At `submit_enrollment`, the club-stamp logic must determine whether a stamp is physically present. A non-AcroForm Modelo 1 is therefore sent to `parse_fpb_mod1` at submit time even when `values` supplied its fields. A handwritten scan costs zero Document AI calls during parse and one during submit; a filled template costs zero at both stages.

**Stamping also dates the form.** Whenever an upload path applies the club stamp to a Modelo 1 carrying this repository's AcroForm, it fills the Assinaturas "Data" line with the date it stamped — otherwise a form nobody completes by hand reaches the federation stamped but undated. A date already on the form (even a half-filled one) is never overwritten, and a scan has no field to write, so only the template path is affected.

### Required overrides for `submit_enrollment`

`field_overrides` must include:

- Every field listed in `preview.needs_review`. For a minor this already includes any absent guardian field (`guardian_name`, `guardian_relation` (id), `guardian_phone`, `guardian_email`) — answer them from the preview rather than waiting for a submit-time failure.
- `exam_date: "YYYY-MM-DD"` when no medical exam was parsed (or to override the parsed date).

The `missing_guardian_fields` response from `submit_enrollment` is now a fallback only (for when a minor's guardian fields were still absent at submit time); re-call `submit_enrollment` with the added fields if you hit it.

### Required documents depend on nationality and reg_type

For any *"que documentos precisa o jogador X para se inscrever"* question — including future-season / not-yet-enrolled ones — **call `get_enrollment_status(license, reg_type?)` and report its `checklist`**, never answer from general knowledge. The list changes with the player's nationality, and only the tool grounds nationality in their actual record. The checklist is returned for every status: `pending` reflects the live batch's uploads; `enrolled` / `not_enrolled` return a `projected: true` checklist (nationality from the stored record, `reg_type` defaulting to Revalidação/1ª Inscrição). When the player has no SAV record yet (brand-new 1ª Inscrição, no licence), apply the rule below from their stated nationality.

For 1ª Inscrição (reg_type 1) and Revalidação (reg_type 2) the document set splits on nationality:

| Scenario | nacional | Required documents |
| --- | --- | --- |
| `portuguese` | Portugal (id 155) | `fpb_modelo_1`, `exame_medico` |
| `foreign_born` | any other / unknown | `fpb_modelo_1`, `exame_medico`, `atestado_residencia`, `certidao_matricula`, `documento_identificacao` × 2 (passaporte + título de residência — the player's or a parent's) |

`fpb_modelo_4` is optional in both (only when promoting an escalão inline — Subida). reg_type 4 (standalone Subida) requires only `fpb_modelo_4`; reg_type 3 (Transferência) is not handled yet (`checklist` is null). Unknown nationality is treated as `foreign_born` on purpose — asking for the extra documents is the safe error.

## Other workflows

### Read / update an already-enrolled player
- `read_enrollment(license)` — show current enrollment as the stable DTO
  `{license, name, birth_date, nif, id_type, id_number, id_expiry, email,
  telemovel, telefone, nome_pai, nome_mae, nationality_id,
  naturalidade_id, marital_status_id, education_level_id, profession_id}`.
  `license` is an int; date fields are ISO where SAV sends a parseable date.
  SAV's raw internal `id` and workflow keys are not returned.
- `update_enrollment(license, fields={...})` — patch contact / address / id fields.
- `update_enrollment_with_document(license, pdf=b64, doc_type?, mod1_values?, field_overrides={...}, file_only?)` — re-reconcile from a fresh PDF and optionally upload it. `mod1_values` is the same trusted values shape as `parse_enrollment_forms` and skips classification and field extraction.

### Manual enrollment (no PDF)
- `create_enrollment_manual(batch_number, license, fields={...})`.

### Generate a Modelo 1 form (outbound)
- `fill_mod1(values, player_signature_b64?, guardian_signature_b64?, club_stamp_b64?)` — the reverse of parse/reconcile: fill a blank FPB Modelo 1 from a values dict and return `{filename, size_bytes, pdf_b64}` (a print-ready enrollment form). The Época is always fetched from SAV's active-season table; the tool exposes no season argument, and season-like keys in `values` are rejected. `values` keys mirror `field_overrides` plus the header/identity fields (`tipo_inscricao`, `license`, `clube`, `associacao`, `genero`, `escalao`, `nome`, `nacionalidade`, `pais_nascimento`, `data_assinatura`). **Every player field is mandatory; the Licença FPB only for a Revalidação; the guardian block in full only for a minor (from `nasc`) and empty otherwise — invalid input raises.** The player Telefone (landline) is never filled. The three `*_b64` params are optional PNG/JPG signature/stamp images overlaid on their areas — omit them for a form to sign offline, pass any subset for the completed form. A photo or scan on white paper is fine: the background is keyed to transparent and the image cropped to the ink before it is overlaid, so it never paints a box over the printed line and blank margins never shrink it. A transparent PNG (a canvas or tablet capture) is cropped too but never keyed, so a cutout keeps its artwork. **`club_stamp_b64` also fills the Assinaturas date** with today's (unless `values` carried `data_assinatura`) — stamping and dating are one action, so a stamped form is never undated. **Prefer not to stamp here at all:** for an enrollment leave it unset and let `submit_enrollment` stamp and date the form as it files it, and never stamp a form you hand to the player — this output is distributable and the carimbo reads to the federation as club-endorsed. Forms produced here are read back through the AcroForm with no OCR, and adding any of the three images does not break that path. A caller holding the authoritative record can skip the PDF round-trip entirely by passing the same dict as a document entry's `values`.

### Ad-hoc documents
- `list_player_documents(license)` — what's uploaded for this player.
- `upload_player_document(license, pdf_base64, doc_type?)`.
- `replace_player_document(license, pdf_base64, doc_type?)` — replaces existing doc of that type.
- `delete_player_document(license, doc_id)` — by galeria id from `list_player_documents`, scoped to the player's licence.

### Batch admin
- `list_batch_enrollments(batch_number)` — every player in a batch.
- `delete_enrollment(license)` — remove one player.
- `delete_batch(batch_number)` — only allowed for open ("Em construção") batches.

### Coaches (treinadores)
- `list_coaches(club_id?, season?, status="active", gender=0, name="", tptd="", with_details=false)` — all coaches registered to a club for a season.
  - `club_id` defaults to the session's own club; pass an explicit ID to query another club.
  - `season` defaults to the current epoch.
  - `status` is `"active"` | `"inactive"` | `"all"`.
  - `gender`: 0 = any, 1 = Masculino, 2 = Feminino.
  - `name` is a server-side **prefix** match on the full name (`"João Ferreira"` matches; `"Loff"` does not). For substring matching, request a broader set and filter locally.
  - `tptd` is a server-side filter only — the result rows do not contain the TPTD, NIF, or mobile phone. Pass `with_details=true` to issue one extra request per coach and populate `nif`, `tptd`, `tptd_expiry`, and `mobile_phone` (N+1, off by default).
  - Returns rows of `{id, carreira_id, wallet, name, association, club, gender, season, grade, birth_date, active}` plus `{nif, tptd, tptd_expiry, mobile_phone}` when `with_details=true`. `wallet` is a string; `carreira_id` is the integer used by SAV2's internal history URL.

### Players
- `search_players(...)`, `get_player(license, ...)`, and `find_player_by_nif(nif, ...)` accept `with_details=false` (default). Pass `with_details=true` to issue one extra `jogadoresdb.php?op=2` request per player and add `photo_url` and `mobile_phone` (N+1). **`nif` comes back populated only for your own club's players** — SAV2 discloses a NIF only to the player's own club, so in a `club_id=0` search an empty `nif` means "not visible to you", not "none on file". Do not treat a blank `nif` from a federation-wide search as evidence the player has none.
- `get_player(license, ...)` and `find_player_by_nif(nif, ...)` use the same season ladder when no explicit season is supplied: current epoch first, previous epoch second, then `season=0` (all seasons) as the last resort. The first non-empty rung wins, and its newest season row is returned. `status` is passed through unchanged on every rung.
- `status` is a **client-side** filter over a per-player eligibility flag — it is not "was registered in that season". A player can carry `active=true` on a row from many seasons ago, while a genuinely inactive player is filtered out at *every* rung and resolves to null. Pass `status="all"` to reach those. (Measured on one club: 612 of 684 all-time licences are active, 72 are not.)
- **Widened null semantics:** these tools can now return a player's most recent active row even when their licence lapsed two or more seasons ago. `null` means no matching row was found by the full ladder; it no longer means "not currently at this club." Parent-onboarding code must not treat a non-null result as proof of current enrollment; see the onboarding guidance below.
- `find_player_by_nif(nif, status?, with_details?)` is the inverse of `get_player(license=...)`: resolves a player by Portuguese NIF (9 digits) against the session club's roster. It takes **no `club_id`**: SAV2 only exposes a player's NIF to their own club, so the session club is the only roster a NIF can be resolved in; a session with no resolvable club raises `ValueError` rather than silently returning null. Returns null when the NIF is malformed or no player matches after the season ladder. Useful for external importers (e.g. federation signup form) that key players by NIF.
- `lookup_player(nif?, license?, club_id?, status="active", with_profile=false, with_details=false)` accepts exactly one of NIF or licence and uses the same ladder. `with_profile=true` adds the SAV profile under a nested `profile` key, keeping profile and roster field provenance separate.
  - `club_id` defaults to the session's own club. `club_id=0` searches federation-wide, but **only with `license`** — use it for a player transferring in from another club. With a `nif`, the only accepted `club_id` is your own club's id (omitting it is the normal spelling): **SAV2 only exposes a player's NIF to their own club**, so any other id — including `club_id=0` — raises `ValueError`, as does a session with no resolvable club. This is a platform limit, not an indexing gap — pre-warming cannot work around it.
  - **A `club_id=0` licence lookup is a single request per ladder rung** — no more expensive than a club-scoped one. SAV2 matches a licence federation-wide natively on `nr_clube=0`, so `get_player` and `lookup_player` send exactly one search per rung and no club sweep happens. Verified live 2026-08-22 with licence 249503, which resolved to a club that is not the session club in one request.
  - This holds only for **licence** lookups with no `association_id`. A broad `club=0` search (by name, tier, birth year, …) still fans out one request per club in every association — on FPB that is ~1000. The verified reason is that `jc_associacao` is **ignored** when `nr_clube=0` (an association-scoped search returned rows from many other associations), so the fan-out is the only way to honour `association_id`. Whether the single-request form also caps result count is **not established**: a `name="Silva"` probe returned 48 rows on every page, but `name` is a prefix match so 48 may simply be the true total. An unfiltered federation-wide request times out at 30s, so the server does real work rather than returning a cheap capped page. If a use case ever needs broad federation-wide search to be fast, settle the cap question first — it may collapse the fan-out the way the licence case collapsed.
- `warm_nif_index(force=false)` pre-pays NIF-index work for the session's own club in the node-local SQLite cache. It takes **no `club_id`** — the index only ever covers your own club, because SAV2 only exposes a player's NIF to their own club — and raises `ValueError` when the session club cannot be resolved. It is always the exhaustive scan (**there is no `scope` — it was removed in 0.92.0**): one profile POST per not-yet-indexed licence across all seasons, at 8-way concurrency. Offline-only; it can take minutes on a large club and will likely outlast a default MCP timeout. Use it from an importer or nightly job, not an interactive request.
  - **You do not need to pre-warm to get the fast path.** `find_player_by_nif` scans current season → previous → all seasons and stops the moment the NIF resolves, persisting what it fetched. Pre-warming buys one thing: it makes a *miss* free, by establishing that the club is fully covered.
  - **Check `complete` before trusting a scan.** It is false when the roster could not be listed (`error: "roster_unavailable"`) or when a profile could not be read (`error: "incomplete_scan"`, with the licences in `unresolved`). No coverage marker is written in either case, so a later NIF miss stays non-authoritative — deliberately. A marker written over an incomplete scan is what used to make `find_player_by_nif` report an existing player as new.
  - **`no_nif` is not a failure.** Licences whose profile loads but carries no NIF are listed separately and do *not* block `complete`: they can never match a NIF query, so they are covered. Around a fifth of a real club's licences are in this state.
  - `players_indexed` counts profiles this call actually fetched and resolved; `players_enumerated` is the roster size. Before 0.92.0 `players_indexed` reported the roster size, so a warm no-op looked like a full rebuild.
- Any role may call `warm_nif_index`, including with `force=true`. `authz.toml` cannot gate individual parameter values, so the downstream wrapper is responsible for rate-limiting expensive scans.

## Error handling

Two kinds of failure surface:

- **Structured error dicts** (LLM-actionable, no exception raised):
  - `{error: "license_not_enrolled", license, open_batches: [...]}` — from `read_enrollment`, `update_enrollment`, `update_enrollment_with_document`, `delete_enrollment`, `list_player_documents`, `upload_player_document`, `replace_player_document`.
  - `{resolved: false, candidates: [...]}` / `{resolved: false, error: "player_already_in_sav"}` — from `resolve_player` **and** from `preview_enrollment` when called with `license: null` and the player doesn't resolve to one licence. Ask the user to pick / switch to Revalidação, then re-call `preview_enrollment` with an explicit `license`.
  - `{success: false, missing_guardian_fields: [...]}` — fallback from `submit_enrollment` when a minor's guardian fields were still absent at submit time (`preview_enrollment` surfaces them in `needs_review` up front).
- **Raised exceptions** — programming errors (unknown `mod1_id`, invalid `team`, malformed base64). Surface these to the user; they indicate a bug or a malformed input.

## Authorization metadata for downstream consumers

sav-mcp itself is a stdio subprocess with no attested caller identity — it trusts whatever client is on the other end of the pipe. Downstream wrappers (e.g. the gedai-bot Telegram frontend) carry the trust boundary: they authenticate the end user, decide what subset of tools to expose to the LLM, and rewrite caller-identity arguments before forwarding.

The per-tool policy lives in **`sav_mcp/authz.toml`** — single source of truth. The loader (`sav_mcp/authz.py`) stamps every tool's MCP `_meta` and `inputSchema` properties with the `x-sav-*` extension fields documented below. Wrappers consume the same TOML directly:

```python
from pathlib import Path
from sav_mcp.authz import load_policy

policy, _ = load_policy(Path(".../sav_mcp/authz.toml"))
```

### Extension fields

On each tool's `_meta`:

- **`x-sav-capability`** (`str`) — one of `"read"` | `"write"` | `"delete"`. The verb the tool performs, used by wrappers for audit logs and confirmation UX.
- **`x-sav-roles`** (`list[str]`) — caller roles permitted to invoke the tool unconditionally, drawn from `{"coach","parent","player"}`. **An empty list combined with any capability means admin-only.** `"admin"` is implicit and never appears here.
- **`x-sav-self-scope`** (`list[str]`, optional) — caller roles ⊂ `{"parent","player"}` permitted to invoke the tool *only when the subject belongs to them*. Layered on top of `x-sav-roles`. Wrappers verify ownership via the subject markers below before forwarding.

On a parameter's JSON Schema property inside `inputSchema`:

- **`x-sav-subject`** (`str`) — declares this parameter carries a subject identifier. Value is one of `"license"` (parameter is an integer SAV2 licence) or `"nif"` (parameter is a Portuguese NIF string). Wrappers verify `args[param] ∈ caller.allowed[kind]` before forwarding. Tools that need both flows (e.g. `submit_enrollment`, `preview_enrollment`) carry the marker on both a `license` and an `nif` parameter — exactly one is set per call; for 1ª Inscrição only `nif` is set, for Revalidação only `license`.
- **`x-sav-identity`** (`bool`) — the parameter carries the *caller's* identity. Wrappers MUST overwrite the LLM-supplied value with the authenticated user's so a jailbroken LLM cannot impersonate another club member. No tool today takes such a parameter; the marker is in place for future ones.

### Role vocabulary

| Role | Meaning |
|------|---------|
| `coach` | Club coach or staff using the bot for day-to-day operations. |
| `parent` | Parent or guardian of an enrolled player. |
| `player` | Player enrolled in the club. |
| `admin` | Club administrator. Implicit — never appears in `x-sav-roles`; admin-only tools have `x-sav-roles: []`. |

### Capability tiers

| Tier | Meaning | Examples |
|------|---------|----------|
| `read` | Pure lookups, no SAV2 state change (also covers OCR-only steps that cache nothing in SAV2). | `search_players`, `get_game_sheet`, `parse_enrollment_forms`, `list_player_documents`. |
| `write` | Mutates SAV2 (create / update). | `submit_enrollment`, `update_enrollment`, `upload_player_document`, `create_batch`. |
| `delete` | Destructive (removes records or files). Conventionally `roles = []` (admin-only). | `delete_enrollment`, `delete_batch`, `delete_player_document`. |

### Wrapper enforcement model

```
caller asks to invoke T with args:
  if caller.role == "admin":                          allow
  elif caller.role in T.roles:                        allow
  elif caller.role in T.self_scope:
        # iterate the marked subject params; each carries its kind in the marker
        for each param p in T.inputSchema where x-sav-subject is set:
            kind = inputSchema[p]["x-sav-subject"]      # "license" or "nif"
            value = args.get(p)
            if value is not None and value not in caller.allowed[kind]:
                                                        deny
        # if no marked param is populated, the tool is wrapper-gated by
        # conversation context alone (e.g. parse_enrollment_forms,
        # resolve_player) — wrapper applies its own policy
                                                        allow
  else:                                                 deny
```

Every `subject_license` parameter across MCP tools is an `int`, and every `subject_nif` parameter is a string of exactly 9 digits. The wrapper MUST therefore hold `caller.allowed["license"]` as ints and `caller.allowed["nif"]` as strings, and compare like-for-like. A type mismatch silently fails the subject check (`"301772" in {301772}` is `False` in Python), incorrectly denying a parent access to their own dependent.

The `caller.allowed` sets are wrapper-owned state, hydrated at onboarding:

- **Player (≥18 self-enrolling):** `caller.allowed = {"license": [own_license?], "nif": [own_nif]}`. `caller.nif` is captured at onboarding; license appears once known.
- **Parent:** `caller.allowed = {"license": [dep.license for dep], "nif": [dep.nif for dep]}` over the registered dependents `[{nif, license?, name, birth_date}, ...]`. New dependents have license `None` until 1ª Inscrição succeeds, after which the wrapper writes the assigned license (returned in `submit_enrollment`'s response payload) back into the matching dependent row.

The wrapper SHOULD use `find_player_by_nif` and `get_player_profile` during onboarding to verify a parent's claim (match `nome_pai` / `nome_mae` before adding a dependent row); those calls happen with the wrapper's own SAV session, not on behalf of the end user. **Do not use a non-null `find_player_by_nif` result as proof that the player is currently at the club:** as warned in “Players” above, its season ladder now returns lapsed players too. Check the returned season/active fields or perform an explicitly current-season search when current enrollment matters.

### Policy

**Every tool MUST have a `[tools.<name>]` block in `authz.toml`.** Adding a `@server.tool()` without one fails at import time — the loader raises `RuntimeError` on drift between the live registry and the policy. Pick the narrower role set and lower capability tier when in doubt; omitted fields inherit from `[defaults]`, which is read-only / no-roles / no-scope.

## Things to avoid

- Don't fabricate `tier_id` values — call `list_tiers(gender_id)` or get them from `parse_enrollment_forms`.
- Don't call `submit_enrollment` before `preview_enrollment` for the same `mod1_id` — the reconciliation state is cached and required.
- Don't pass the internal SAV batch `id`; tools always use the human-visible `batch_number`.
- Don't loop blindly through batches to find one — use `get_batch(batch_number)`.
- Don't ask the user for the current club/season — call `get_session_info()`.
- Don't surface tool internals to end users (`season_id + 1`, `club_id=0`, `status="all"`, kwarg names, op codes). Phrase actions in domain terms — "para a próxima época", "alargado ao nível federativo", "incluindo inativos".
