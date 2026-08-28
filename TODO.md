# TODO

Deferred items. Each was found while doing something else and deliberately not
fixed there, to keep that change reviewable.

---

## 1. `-m "not live"` keeps the offline suite offline

**Problem.** Several tests outside `*_live.py` hit production SAV through the
session-scoped `client` / `sample_player` fixtures in `tests/conftest.py` —
`tests/test_player_detail.py`, `tests/test_search_players.py`,
`tests/test_clubs.py`, `tests/test_game_sheet.py` among them.

**Why it matters.** The documented "offline" command is not offline. Observed
2026-08-28: the same suite ran in **37s green** and **252s with 3 failures and 5
errors**, purely on SAV's availability. Every one of those failures passed in
isolation. This makes the suite untrustworthy at exactly the moment it is most
needed — immediately before a merge — and it has already produced one false
regression alarm this session.

**Resolution.** Live tests are marked with `@pytest.mark.live`, registered in
`pyproject.toml`, and the offline suite runs with `pytest tests/ -q -m "not live"`.

---

## 2. Two CLI tests break whenever the terminal advertises colour

**Problem.** `tests/test_cli_enrollment.py` asserts plain substrings against
rich-rendered output:

- `test_enrollment_read_lists_batch_items` — expects `"2 player(s) enrolled"`
- `test_enrollment_create_auto_classifies_two_positionals_into_form_and_exam`
  — expects `"form.pdf (fpb_modelo_1)"`

With colour enabled the output is correct but interleaved with ANSI codes
(`\x1b[1;36m2\x1b[0m\x1b[36m player...`), so the assertions fail.

**Why it matters.** Colour detection changed mid-session and cost a false
regression investigation. Every run now needs `NO_COLOR=1 TERM=dumb`, which is
easy to forget and silently changes what the suite proves.

**Suggested fix.** Force colour off for CLI tests — a `CliRunner(env=...)`
setting, an autouse fixture setting `NO_COLOR`, or strip ANSI before asserting.
Prefer fixing the tests over documenting the workaround.

---

## 3. Release note: `localidade_id` removal is wider than a revert

**Problem.** `4adfed6` removed `localidade_id` from the public surface. It is
easy to read that as reverting `ea0178b` (same session), but the parameter also
existed on `add_player_to_registration_batch` — the 1ª Inscrição path — **before
this session**, so it is the removal of a pre-existing public parameter.

**Why it matters.** Low practical risk (the default was `0`, which already sent
`""`), but a caller passing it explicitly now gets a `TypeError`. It deserves
its own line in the release note rather than being folded into "reverted
something we added today".

**Context.** Removed because SAV's own form has no locality dropdown — it is
free text that accepts a nonexistent town — and a live Revalidação
(licence 298337, 2026-08-28) passing `localidade_id=1454` had it silently
ignored. A stored id is still carried forward; only setting one was removed.

---

## 4. Consolidated release note for gedai (0.88.0 → 0.91.0)

**Problem.** Breaking changes are spread across two version bumps: 11 in 0.90.0
and 3 more in 0.91.0. gedai is believed to be on 0.88.0, so it faces all 14 in
one jump.

**Why it matters.** Three of them change response *shape* rather than raising,
so they fail silently:

- `read_enrollment` returns an allowlisted DTO, not SAV's raw record
- game rows carry `status` / `status_raw` / `has_result` instead of `game_status`
- `submit_enrollment` drops `player_id`; `update_enrollment` and
  `create_enrollment_manual` return `license` instead

Plus one that breaks loudly on input: `list_game_sheets` no longer accepts
`Realizado`-style filter values.

**Suggested fix.** One consolidated note covering both versions, leading with
the silent-shape changes. `git log --oneline 4e37780~12..HEAD | grep '!:'`
lists the full set.
