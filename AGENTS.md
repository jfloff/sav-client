# Working on this repository

For agents **modifying** sav-client. The per-package `AGENTS.md` files
(`sav_client/`, `sav_mcp/`, `sav_cli/`) are the opposite audience — they are
consumer references for agents *calling* this package. Do not put contributor
guidance in those.

---

## SAV2 punishes assumptions

Every item below cost real debugging, and several caused wrong data to be
written to a live federation record. They are not general advice; they are this
system's specific traps.

- **HTTP 200 does not mean success.** SAV answers unhandled PHP fatals with a
  200 and the stack trace in the body. Three distinct ones were reproduced in a
  single day. Use `_looks_like_php_fatal` / `_parse_json_response`; never let a
  raw body reach an exception message, because it carries SAV's table and
  constraint names.
- **`val` is not a universal success flag.** op=36 uses `val: 1` for success;
  op=33 returns `val: 0` on a *successful* save. Treating it as universal
  blocked every Revalidação at step 1. Check per endpoint.
- **A response body is not evidence a write happened.** op=29 returned a 1-byte
  body and reported success while removing nobody. Verify the postcondition,
  per entity — a batch's item count also falls when someone else is removed.
- **Request and response field names differ.** Consents are sent as
  `consentimentoDados` and returned as `concordo_tratamento_dados`. Grepping
  only the request spelling "proved" they were unreadable when they were not.
- **A field that looks like a lookup may be free text.** `localidade_id` was
  accepted and silently ignored; SAV's own form takes a town that does not
  exist. Check the actual form before trusting an id.
- **A check that cannot run is not a check that passed.** op=163 fatals on most
  real inputs; a postcondition read can fail. Model "unverifiable" as its own
  outcome — see `SavWriteUnverifiedError` — never as success or failure.
- **Read-backs exist where docstrings claim they don't.** op=31 returns nearly
  the whole step-3 state. Mine `js/main.js` for endpoints before concluding SAV
  cannot answer something.

## Before you assert

Most errors in this codebase's history came from pattern-matching instead of
reading.

- **Open every `file:line` before citing it** — in a brief, a commit message, or
  a claim to the user. A grep hit is a candidate, not a fact. One delegation was
  rejected outright because three of its four cited sites did not share the
  contract the brief assumed.
- **Check whether a method writes before calling it as a probe.** `_save_*`,
  `_commit_*`, or any mutating POST is not read-only. Print the payload before
  sending it: one look at `morada=NULL,cod_postal=NULL` would have prevented a
  live minor's address being wiped.
- **Prefer a verification that can fail.** "The suite passed" did not prove the
  offline suite was offline; re-running with `SAV_BASE_URL=http://127.0.0.1:9`
  did, and caught a live test that had been missed. Ask what result would
  disprove the claim, then produce that result.

## Testing

    pytest tests/ -m "not live"     # offline; ~13s
    pytest tests/ -m live           # needs a real SAV account

Tests touching the network carry `@pytest.mark.live`. **Run from the repo
root** — absolute test paths from another directory break relative fixtures and
produce phantom failures. To prove a change kept the suite offline, re-run it
with `SAV_BASE_URL=http://127.0.0.1:9`.

## Delegating to Codex

- Pass `--wait`, or the job detaches and its report never arrives.
- **Codex shares this working tree.** Never hand-edit a file a running job may
  touch; edits get silently overwritten. After every dispatch check
  `git status` *and* `git branch --show-current` — a job once created and
  switched to its own branch without saying so.
- Treat the tree as the source of truth, not the report. One job's report was
  lost to a timeout while its work had completed fine.
- **Tell it to report contradictions rather than work around them.** It
  complies, and this has caught more real errors than any single implementation
  task — including a phantom dependency, a wrong description of
  `_primeira_commit`, and the four-site brief above.
- State the working agreements in the brief: stay on branch X, do not commit,
  run pytest from the repo root.

## Changelog

Add the `CHANGELOG.md` entry in the same change that makes it. It is written
for consuming agents: classify each break as `IMPACT: raises` or
`IMPACT: silent`, and give silent ones a `DETECT:` grep. Reconstructing entries
from `git log` afterwards loses exactly the details that make them useful.
