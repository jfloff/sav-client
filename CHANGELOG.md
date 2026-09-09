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
key changes this output automatically. Nothing here is hand-maintained, which is
also why `label` is `null` for the 13 fields that have no `FieldDef` (`nome`,
`genero`, `escalao`, `tipo_inscricao`, …) — a `null` means "no such value exists
upstream", not "unknown". For the same reason `exam_date` gets no row: it is a
`submit_enrollment` field with no Modelo 1 slot and no field definition to derive
from.

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
