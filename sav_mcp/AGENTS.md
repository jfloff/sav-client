# sav-mcp — Agent Reference

MCP server for the FPB SAV2 basketball management system. Authoritative reference for an LLM driving this server through tool calls.

This file is intended to be loaded as the LLM's system prompt (or first context message). It documents the workflow, terminology, and enum tables an LLM needs to use the tools effectively without making extra discovery calls.

## Terminology

| Term | Meaning |
|------|---------|
| **licence** (licença) | Player registration number, numeric (e.g. `301772`). Human identifier. |
| **wallet** (carteira) | Coach registration number. Distinct from licences. |
| **player_id** | Internal SAV2 numeric ID. Returned by `submit_enrollment`. Not the same as licence. |
| **batch** (lote / guia) | A "Lote de Inscrição" — group of player registration requests of one type, locked to one tier+gender. |
| **batch_number** | Human-visible batch identifier (string). All MCP tools accept the number, not the internal id. |
| **tier** (escalão) | Age category (e.g. "Mini 12", "Sub 14", "Sénior"). `tier_id` is numeric; `tier_name` is free-text. |
| **association** (associação) | Regional body. Numeric `id` from `list_associations`. |
| **club** (clube / organizacao) | Numeric club ID. `club_id=0` means federation-wide search. |
| **season** (época) | SAV2 epoch ID. `None` defaults to current season; `0` means all seasons. |
| **val** | `1` = home team, `2` = away team. Tools expose this as `team: "home" \| "away"`. |
| **artifact_id** | UUID returned by `parse_enrollment_forms` referencing a cached OCR result. fpb_modelo_1 results expose this also as `mod1_id`; exame_medico results expose it also as `medical_exam_id`. |
| **needs_review** | Field-level OCR confidence is too low to trust; the user must confirm or correct. |
| **player** | Canonical English term — never "athlete". Tool names, parameters, and English responses use `player`. Portuguese user-facing replies may use `jogador` or `atleta` (both natural to coaches). The upstream SAV2 API uses `atleta` as a JSON field name — wire contract, untouched. |

## Sessions

`get_session_info` returns the authenticated context — `club_id`, `season_id`, `season` (label like `"2025/2026"`, best-effort), `season_start_year`, `user`, `profile`. Tools that scope by "the session's club" default to that `club_id`. Pass an explicit `club_id` to override, or `0` to search federation-wide. Use `season`/`season_start_year` for the human-readable current-season label instead of scraping it off a resolved player.

## PDF convention

All PDFs cross the MCP boundary as **base64-encoded strings**.

- Inputs: `parse_enrollment_forms(pdfs=[b64, b64, ...])`, `upload_player_document(pdf_base64=...)`, `replace_player_document(pdf_base64=...)`, `update_enrollment_with_document(pdf=...)`.
- Outputs: `generate_game_sheet_pdf` returns `{filename, size_bytes, pdf_b64}` — decode `pdf_b64` to bytes to use.

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
`Marcado` (scheduled), `Realizado` (played), `Não Marcado`, `Adiado`, `Anulado`.

### Document types (`doc_type` strings)
`fpb_modelo_1` — main enrollment form. `exame_medico` — medical exam. Other types may be returned by parsers but are not yet wired into the enrollment workflow.

Use `list_tiers(gender_id)` to discover `tier_id` values dynamically — the set differs per gender and varies by season.

## Domain rules

For roster questions about an escalão ("Que jogadores são Sub-X?", "atletas para o próximo ano") call **`roster_for_escalao(tier_id, gender_id, when="next"|"current")`**. The tool resolves both birth years deterministically and runs a fallback cascade (`club + active → club + all → federation + all`), reporting which `step` matched — so the LLM never does the arithmetic or the retries. Fall back to `search_players(birth_year=[...])` only for genuinely custom queries (e.g. multiple escalões at once).

**Targeting a season — relative vs absolute:**
- `when="current"`/`"next"` is a season *relative to today*, resolved server-side. **Prefer this** for "current / próxima época" questions: you do not need to know today's season, and it avoids guessing the season from the calendar year (they diverge May–Sept).
- `season_year` (start year; `2020` = "2020/2021") names an *absolute* season and overrides `when`. Use it only when the user names a specific season ("em 2020/2021"). Do **not** try to compute "next" yourself by passing `season_year` — that reintroduces the calendar trap; use `when="next"`.

**Three regimes** follow from how the target relates to today:
- **Future season** (`when="next"`, or a `season_year` ahead of today) is a **projection**, not a query for that season's enrollment — enrollment only ever exists for the current season. The tool keeps known players whose birth year lands in that season's window, returning `is_projection=true` and `source="projection_by_birth_year"`. An empty `players` list means "no known player projects into that cohort", never "missing data".
- **Current season** (`when="current"`) and **past seasons** (`season_year` ≤ today) reflect actual enrollment, queried at that season's own epoch; `is_projection=false` and `source` stays `club`/`federation`/`none`.

Knowledge to drive the tool correctly:

- Each escalão spans **two consecutive birth years**. For season `Y/Y+1`, Sub-X = born in `Y+1−X` and `Y+2−X`; same for Mini 8/10/12.
- **Sénior** is open-ended below (no upper birth year — the tool filters by tier name). **Baby-Basket** spans three years (ages 4–6 in `Y+1`); the two youngest require the child to have completed 4 years before enrollment. **Masters / Veteranos** and **BCR** — `<TODO: confirm with user>`; `roster_for_escalao` raises so the LLM doesn't guess.
- "Próximo ano / próxima época" advances the season label by one (`season_id + 1`; SAV2 `epoca_id` is sequential), never the calendar year. But there is **no next-season roster to fetch** — enrollment only happens in the current season — so a next-season roster is always a projection of the current pool by birth year. `roster_for_escalao(when="next")` does this; doing it by hand means querying the *current* `epoca_id` and filtering by next season's birth years, never `season_id + 1`.
- Between May and September the wall clock straddles a season transition: a player listed as inactive in the current season is almost certainly "not yet re-registered", not retired — the tool's `club + all` cascade step (status="all") surfaces them.

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

When falling back to `search_players` directly: never drop one of the two birth years; for a next-season projection query the **current** `epoca_id` (not `season_id + 1`) filtered by next season's birth years, and if a club-scoped query returns empty, retry at `club_id=0` with `status="all"` before reporting empty.

### Worked example

Coach: *"Que jogadores são para o ano Sub-14 masculinos?"* (next season). One call:

`roster_for_escalao(tier_id=5, gender_id=1, when="next")`
  → `{tier: "Sub 14", season: "2026/2027", birth_years: [2014, 2013], is_projection: true, source: "projection_by_birth_year", step, players}`.

Report the `players` list, framing it as a projection — "atletas que, pelo ano de nascimento, passam a Sub-14 na próxima época". If `step="federation + all"`, those players came from the wider federation pool, not this club's current roster; say so in domain terms ("elegíveis por ano de nascimento, ainda sem inscrição neste clube") rather than naming `club_id=0`.

## Enrollment workflow

The canonical pipeline. Each step's output feeds the next.

```
1. parse_enrollment_forms(pdfs=[b64, ...])
     → [{artifact_id, mod1_id, doc_type, reg_type, tier_id, gender_id, ...}, ...]
       (one entry per PDF; medical exams return medical_exam_id instead of mod1_id)

2. find_open_batch(reg_type, tier_id, gender_id)  → batch | null
   or create_batch(reg_type, tier_id, gender_id)  → batch
     → batch_number

3. resolve_player(batch_number, mod1_id)
     → {resolved: true, license}  ── proceed
     or {resolved: false, candidates: [...]}  ── ask user to pick
     or {resolved: false, candidates: []}  ── ask user for licence

4. preview_enrollment(batch_number, license, mod1_id, medical_exam_id?)
     → {player, fields: [{kwarg, status, sav_value, ocr_value, final_value}, ...], needs_review: [...]}
       Status values:
         "updated"      OCR overrides SAV
         "match"        SAV kept (OCR matched)
         "needs_review" low OCR confidence — user must confirm
         "ocr"          field not in SAV (id_type, guardian_*, consent_*)

5. submit_enrollment(batch_number, license, mod1_id, field_overrides={...}, medical_exam_id?)
     → {success: true, player_id, source_document_upload, medical_exam_upload}
     or {success: false, missing_guardian_fields: [...]}  ── retry with guardian fields added
```

### Required overrides for `submit_enrollment`

`field_overrides` must include:

- Every field listed in `preview.needs_review`.
- `exam_date: "YYYY-MM-DD"` when no medical exam was parsed (or to override the parsed date).
- For minors, all four guardian fields when prompted: `guardian_name`, `guardian_relation` (id), `guardian_phone`, `guardian_email`.

Re-call `submit_enrollment` with the added fields after a `missing_guardian_fields` response.

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
- `read_enrollment(license)` — show current enrollment.
- `update_enrollment(license, fields={...})` — patch contact / address / id fields.
- `update_enrollment_with_document(license, pdf=b64, doc_type?, field_overrides={...}, file_only?)` — re-reconcile from a fresh PDF and optionally upload it.

### Manual enrollment (no PDF)
- `create_enrollment_manual(batch_number, license, fields={...})`.

### Ad-hoc documents
- `list_player_documents(license)` — what's uploaded for this player.
- `upload_player_document(license, pdf_base64, doc_type?)`.
- `replace_player_document(license, pdf_base64, doc_type?)` — replaces existing doc of that type.
- `delete_player_document(doc_id)` — by galeria id from `list_player_documents`.

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
- `search_players(...)`, `get_player(license, ...)`, and `find_player_by_nif(nif, ...)` accept `with_details=false` (default). Pass `with_details=true` to issue one extra `jogadoresdb.php?op=2` request per player and add `photo_url` and `mobile_phone` (N+1).
- `find_player_by_nif(nif, club_id?, with_details?)` is the inverse of `get_player(license=...)`: resolves a player by Portuguese NIF (9 digits) against the club roster. `club_id` defaults to the session's own club. Returns null when the NIF is malformed (not 9 digits) or no roster player matches. Useful for external importers (e.g. federation signup form) that key players by NIF.

## Error handling

Two kinds of failure surface:

- **Structured error dicts** (LLM-actionable, no exception raised):
  - `{error: "license_not_enrolled", license, open_batches: [...]}` — from `read_enrollment`, `update_enrollment`, `update_enrollment_with_document`, `delete_enrollment`, `list_player_documents`, `upload_player_document`, `replace_player_document`.
  - `{success: false, missing_guardian_fields: [...]}` — from `submit_enrollment` when the player is a minor.
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
| `read` | Pure lookups, no SAV2 state change (also covers OCR-only steps that cache nothing in SAV2). | `search_players`, `get_game_sheet`, `parse_enrollment_forms`, `get_artifact_subject_claim`. |
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

The `caller.allowed` sets are wrapper-owned state, hydrated at onboarding:

- **Player (≥18 self-enrolling):** `caller.allowed = {"license": [own_license?], "nif": [own_nif]}`. `caller.nif` is captured at onboarding; license appears once known.
- **Parent:** `caller.allowed = {"license": [dep.license for dep], "nif": [dep.nif for dep]}` over the registered dependents `[{nif, license?, name, birth_date}, ...]`. New dependents have license `None` until 1ª Inscrição succeeds, after which the wrapper writes the assigned license (returned in `submit_enrollment`'s response payload) back into the matching dependent row.

The wrapper SHOULD use `find_player_by_nif` and `get_player_profile` during onboarding to verify a parent's claim (match `nome_pai` / `nome_mae` before adding a dependent row); those calls happen with the wrapper's own SAV session, not on behalf of the end user.

### Policy

**Every tool MUST have a `[tools.<name>]` block in `authz.toml`.** Adding a `@server.tool()` without one fails at import time — the loader raises `RuntimeError` on drift between the live registry and the policy. Pick the narrower role set and lower capability tier when in doubt; omitted fields inherit from `[defaults]`, which is read-only / no-roles / no-scope.

## Things to avoid

- Don't fabricate `tier_id` values — call `list_tiers(gender_id)` or get them from `parse_enrollment_forms`.
- Don't call `submit_enrollment` before `preview_enrollment` for the same `mod1_id` — the reconciliation state is cached and required.
- Don't pass the internal SAV batch `id`; tools always use the human-visible `batch_number`.
- Don't loop blindly through batches to find one — use `get_batch(batch_number)`.
- Don't ask the user for the current club/season — call `get_session_info()`.
- Don't surface tool internals to end users (`season_id + 1`, `club_id=0`, `status="all"`, kwarg names, op codes). Phrase actions in domain terms — "para a próxima época", "alargado ao nível federativo", "incluindo inativos".
