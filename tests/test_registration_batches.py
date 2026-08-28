from datetime import date, timedelta
from unittest.mock import Mock

import pytest

from sav_client import SavClient
from sav_client.exceptions import (
  SavConfigError,
  SavRecordNotFoundError,
  SavResponseError,
  SavServerError,
  SavWriteUnverifiedError,
)
from sav_client.models import PlayerRegistrationBatch
from sav_client.sav_client import _coerce_exam_date

# SAV rejects an exam date outside its validity window (exam date + 12 months),
# and `_coerce_exam_date` now enforces both bounds. A literal date would quietly
# age out of that window and start failing on a date unrelated to any code
# change, so tests reaching the real commit path anchor to today instead.
RECENT_EXAM_DATE = (date.today() - timedelta(days=30)).isoformat()


# ─── helpers ────────────────────────────────────────────────────────────────

def _first_free_slot(client, type_id: int = 2) -> tuple[int, int] | None:
  """
  Return the first (tier_id, gender_id) for which the live account has no
  open batch of `type_id`. Used to create transient test batches without
  colliding with an open batch the user is actively building.
  """
  taken = {
    (b.tier_id, b.gender_id)
    for b in client.list_player_registration_batches()
    if b.is_open and b.type_id == type_id
  }
  for gender_id in (1, 2):
    for tier_id in client.list_player_registration_tiers(gender_id=gender_id):
      if (tier_id, gender_id) not in taken:
        return tier_id, gender_id
  return None


@pytest.fixture(scope="module")
def transient_batch(client):
  """
  Create a brand-new 'Em construção' Revalidação batch and clean it up
  after the module's tests finish. Picks a free (tier, gender) slot.
  """
  slot = _first_free_slot(client, type_id=2)
  if slot is None:
    pytest.skip("No free (tier, gender) slot to create a transient Revalidação batch")
  tier_id, gender_id = slot

  new_id = client.create_player_registration_batch(
    type=2, tier=tier_id, gender_id=gender_id,
  )
  try:
    yield {"id": new_id, "type_id": 2, "tier_id": tier_id, "gender_id": gender_id}
  finally:
    client.delete_player_registration_batch(new_id)


# ─── pre-HTTP guards ────────────────────────────────────────────────────────

class TestPreHttpGuards:
  """Validation paths that fire before any network call."""

  def test_list_requires_login(self):
    c = SavClient("https://sav2.fpb.pt", "user", "pass")
    with pytest.raises(SavResponseError, match="Must call login"):
      c.list_player_registration_batches()

  def test_create_requires_login(self):
    c = SavClient("https://sav2.fpb.pt", "user", "pass")
    with pytest.raises(SavResponseError, match="Must call login"):
      c.create_player_registration_batch(type=2, tier=5, gender_id=1)

  def test_delete_requires_login(self):
    c = SavClient("https://sav2.fpb.pt", "user", "pass")
    with pytest.raises(SavResponseError, match="Must call login"):
      c.delete_player_registration_batch(1)

  def test_remove_requires_login(self):
    c = SavClient("https://sav2.fpb.pt", "user", "pass")
    with pytest.raises(SavResponseError, match="Must call login"):
      c.remove_player_from_registration_batch(1, 1)

  def test_add_requires_login(self):
    c = SavClient("https://sav2.fpb.pt", "user", "pass")
    with pytest.raises(SavResponseError, match="Must call login"):
      c.add_player_to_registration_batch(1, 1)

  def test_tiers_rejects_invalid_gender(self, client):
    with pytest.raises(ValueError, match="gender_id must be 1"):
      client.list_player_registration_tiers(gender_id=0)

  def test_add_no_longer_accepts_exam_done_kwarg(self, client):
    """`exam_done` was removed in 0.10.5; passing it must raise TypeError."""
    with pytest.raises(TypeError):
      client.add_player_to_registration_batch(1, 1, exam_done=False)

  def test_add_rejects_invalid_exam_date_before_commit(self, monkeypatch):
    client = SavClient("https://sav2.fpb.pt", "user", "pass")
    client.session = {"organizacao": "270"}
    batch = type(
      "BatchStub",
      (),
      {
        "id": 1,
        "is_open": True,
        "type_id": 2,
        "state": "Em construção",
        "tier": "Sub 14",
        "gender": "Masculino",
        "tier_id": 7,
      },
    )()

    monkeypatch.setattr(client, "list_player_registration_batches", lambda season=None: [batch])
    monkeypatch.setattr(client, "_list_revalidable_licenses", lambda batch_obj: {301772})
    monkeypatch.setattr(client, "_load_player_record", lambda batch_id, license: {"id": 88})
    monkeypatch.setattr(client, "_build_step1_send", lambda *args, **kwargs: "step1")
    monkeypatch.setattr(client, "_save_registration_step1", lambda batch_id, internal_id, send: {})
    monkeypatch.setattr(client, "_build_step2_send", lambda *args, **kwargs: "step2")
    monkeypatch.setattr(
      client,
      "_save_registration_step2",
      lambda batch_type, batch_id, internal_id, license, send: {
        "menor_idade": 0,
        "escalao": 7,
        "estatuto": "A",
      },
    )
    monkeypatch.setattr(client, "_resolve_insurance_cascade", lambda internal_id, batch_obj, escalao: (11, 22))
    monkeypatch.setattr(client, "_resolve_taxa_id", lambda batch_obj, internal_id, estatuto: 33)
    monkeypatch.setattr(client, "_registration_precommit", lambda batch_id, internal_id: None)

    def fail_commit(*args, **kwargs):
      raise AssertionError("_registration_commit should not run for invalid exam_date")

    monkeypatch.setattr(client, "_registration_commit", fail_commit)

    with pytest.raises(ValueError, match="exam_date must be YYYY-MM-DD"):
      client.add_player_to_registration_batch(1, 301772, exam_date="13/05/2026")

  def test_add_requires_exam_date_before_commit(self, monkeypatch):
    client = SavClient("https://sav2.fpb.pt", "user", "pass")
    client.session = {"organizacao": "270"}
    batch = type(
      "BatchStub",
      (),
      {
        "id": 1,
        "is_open": True,
        "type_id": 2,
        "state": "Em construção",
        "tier": "Sub 14",
        "gender": "Masculino",
        "tier_id": 7,
      },
    )()

    monkeypatch.setattr(client, "list_player_registration_batches", lambda season=None: [batch])
    monkeypatch.setattr(client, "_list_revalidable_licenses", lambda batch_obj: {301772})
    monkeypatch.setattr(client, "_load_player_record", lambda batch_id, license: {"id": 88})
    monkeypatch.setattr(client, "_build_step1_send", lambda *args, **kwargs: "step1")
    monkeypatch.setattr(client, "_save_registration_step1", lambda batch_id, internal_id, send: {})
    monkeypatch.setattr(client, "_build_step2_send", lambda *args, **kwargs: "step2")
    monkeypatch.setattr(
      client,
      "_save_registration_step2",
      lambda batch_type, batch_id, internal_id, license, send: {
        "menor_idade": 0,
        "escalao": 7,
        "estatuto": "A",
      },
    )
    monkeypatch.setattr(client, "_resolve_insurance_cascade", lambda internal_id, batch_obj, escalao: (11, 22))
    monkeypatch.setattr(client, "_resolve_taxa_id", lambda batch_obj, internal_id, estatuto: 33)
    monkeypatch.setattr(client, "_registration_precommit", lambda batch_id, internal_id: None)

    def fail_commit(*args, **kwargs):
      raise AssertionError("_registration_commit should not run for missing exam_date")

    monkeypatch.setattr(client, "_registration_commit", fail_commit)

    with pytest.raises(ValueError, match="exam_date must be YYYY-MM-DD; got None"):
      client.add_player_to_registration_batch(1, 301772)


class TestExamDateWindow:
  """`_coerce_exam_date` bounds the exam date to SAV's validity window.

  SAV derives validity as exam date + 12 months. It rejects a future date
  itself, but reports it as `{"val":0,"msg":"","resultfunction":"-1"}` — an
  empty reason, undiagnosable from outside — so the bound is enforced here
  instead. The lower bound is ours: an older exam issues a licence that is
  already expired.

  `today` is injected so these never drift out of the window.
  """

  TODAY = date(2026, 8, 27)

  def test_accepts_today(self):
    assert _coerce_exam_date("2026-08-27", today=self.TODAY) == "2026-08-27"

  def test_accepts_recent_past(self):
    assert _coerce_exam_date("2026-08-01", today=self.TODAY) == "2026-08-01"

  def test_accepts_exactly_twelve_months_old(self):
    assert _coerce_exam_date("2025-08-27", today=self.TODAY) == "2025-08-27"

  def test_rejects_tomorrow(self):
    with pytest.raises(ValueError, match="is in the future"):
      _coerce_exam_date("2026-08-28", today=self.TODAY)

  def test_rejects_future_date_from_the_live_run(self):
    # The date that cost four attempts against the real SAV2.
    with pytest.raises(ValueError, match="is in the future"):
      _coerce_exam_date("2026-09-30", today=self.TODAY)

  def test_rejects_one_day_past_the_window(self):
    with pytest.raises(ValueError, match="more than 12 months old"):
      _coerce_exam_date("2025-08-26", today=self.TODAY)

  def test_shape_check_still_runs_first(self):
    with pytest.raises(ValueError, match="exam_date must be YYYY-MM-DD"):
      _coerce_exam_date("30/09/2026", today=self.TODAY)

  def test_leap_day_window_clamps_to_february_28(self):
    # 2024-02-29 has no anniversary in 2023; the window clamps rather than raising.
    assert _coerce_exam_date("2023-02-28", today=date(2024, 2, 29)) == "2023-02-28"
    with pytest.raises(ValueError, match="more than 12 months old"):
      _coerce_exam_date("2023-02-27", today=date(2024, 2, 29))

  def test_future_exam_date_stops_before_the_commit(self, monkeypatch):
    """The whole point: the wizard must not reach op=36 with a doomed date."""
    client = SavClient("https://sav2.fpb.pt", "user", "pass")
    client.session = {"organizacao": "270"}
    batch = type(
      "BatchStub",
      (),
      {
        "id": 1,
        "is_open": True,
        "type_id": 2,
        "state": "Em construção",
        "tier": "Sub 14",
        "gender": "Masculino",
        "tier_id": 7,
      },
    )()

    monkeypatch.setattr(client, "list_player_registration_batches", lambda season=None: [batch])
    monkeypatch.setattr(client, "_list_revalidable_licenses", lambda batch_obj: {301772})
    monkeypatch.setattr(client, "_load_player_record", lambda batch_id, license: {"id": 88})
    monkeypatch.setattr(client, "_build_step1_send", lambda *args, **kwargs: "step1")
    monkeypatch.setattr(client, "_save_registration_step1", lambda batch_id, internal_id, send: {})
    monkeypatch.setattr(client, "_build_step2_send", lambda *args, **kwargs: "step2")
    monkeypatch.setattr(
      client,
      "_save_registration_step2",
      lambda batch_type, batch_id, internal_id, license, send: {
        "menor_idade": 0,
        "escalao": 7,
        "estatuto": "A",
      },
    )
    monkeypatch.setattr(client, "_resolve_insurance_cascade", lambda internal_id, batch_obj, escalao: (11, 22))
    monkeypatch.setattr(client, "_resolve_taxa_id", lambda batch_obj, internal_id, estatuto: 33)
    monkeypatch.setattr(client, "_registration_precommit", lambda batch_id, internal_id: None)

    def fail_commit(*args, **kwargs):
      raise AssertionError("_registration_commit should not run for a future exam_date")

    monkeypatch.setattr(client, "_registration_commit", fail_commit)

    future = (date.today() + timedelta(days=34)).isoformat()
    with pytest.raises(ValueError, match="is in the future"):
      client.add_player_to_registration_batch(1, 301772, exam_date=future)



# ─── subida de escalão (op=21 + commit) ──────────────────────────────────────

class TestRegistrationStepPrefillValidation:
  """op=33/op=31 responses must be safe to pass into the next wizard step."""

  STEP1_PREFILL = {
    "distrito": "", "concelho": "", "localidade": "", "morada": "",
    "codpostal": "", "localidade_txt": "",
  }
  STEP2_PREFILL = {"menor_idade": "", "escalao": "", "estatuto": ""}
  PHP_FATAL = (
    "<br /><b>Fatal error</b>: Uncaught mysqli_sql_exception: SAV internal "
    "schema detail in /var/www/html/php/incricoesdb.php:412"
  )

  def _client(self, body):
    client = SavClient.__new__(SavClient)
    client.base_url = "https://sav2.example/"
    client._timeout = 10

    class _Response:
      text = body

      def raise_for_status(self):
        pass

    class _Http:
      def get(self, *args, **kwargs):
        return _Response()

      def post(self, *args, **kwargs):
        return _Response()

    client._http = _Http()
    return client

  def _save_step(self, client, step):
    if step == 1:
      return client._save_registration_step1(1, 88, "step1")
    return client._save_registration_step2(2, 1, 88, 301772, "step2")

  def test_step1_rejection_body_never_becomes_prefill(self, monkeypatch):
    """A rejection body must not reach step 2 — but not because of `val`.

    This used to assert on the write-ack check rejecting `val != 1`. That
    check was wrong here: op=33 returns `val: 0` on a *successful* save, so it
    blocked every real Revalidação. The guard that matters is the shape one —
    a rejection body carries none of the address keys step 2 consumes, so it
    is refused as prefill regardless of what `val` says.
    """
    client = self._client('{"val": 0, "msg": "Morada recusada"}')
    client.session = {"organizacao": "270"}
    batch = type("BatchStub", (), {
      "id": 1, "is_open": True, "type_id": 2, "state": "Em construção",
      "tier": "Sub 14", "gender": "Masculino", "tier_id": 7,
    })()
    monkeypatch.setattr(client, "list_player_registration_batches", lambda: [batch])
    monkeypatch.setattr(client, "_list_revalidable_licenses", lambda batch_obj: {301772})
    monkeypatch.setattr(client, "_load_player_record", lambda batch_id, license: {"id": 88})
    monkeypatch.setattr(client, "_build_step1_send", lambda *args, **kwargs: "step1")

    reached_step2 = False

    def step2_must_not_run(*args, **kwargs):
      nonlocal reached_step2
      reached_step2 = True

    monkeypatch.setattr(client, "_save_registration_step2", step2_must_not_run)

    with pytest.raises(SavResponseError, match="missing required key"):
      client.add_player_to_registration_batch(1, 301772)
    assert reached_step2 is False

  @pytest.mark.parametrize("step, body", [
    (1, "[]"), (1, '"unexpected"'), (2, "[]"), (2, '"unexpected"'),
  ])
  def test_non_dict_response_names_the_failing_step(self, step, body):
    with pytest.raises(SavResponseError, match=fr"Registration step {step} failed: response must be a JSON object") as excinfo:
      self._save_step(self._client(body), step)
    assert not isinstance(excinfo.value, AttributeError)

  @pytest.mark.parametrize("step, prefill, missing_key", [
    (1, STEP1_PREFILL, "morada"),
    (2, STEP2_PREFILL, "estatuto"),
  ])
  def test_missing_prefill_key_names_the_step_and_key(self, step, prefill, missing_key):
    body = {key: value for key, value in prefill.items() if key != missing_key}
    import json

    with pytest.raises(
      SavResponseError,
      match=fr"Registration step {step} failed: response missing required key '{missing_key}'",
    ):
      self._save_step(self._client(json.dumps(body)), step)

  @pytest.mark.parametrize("step, prefill", [
    (1, STEP1_PREFILL),
    (2, STEP2_PREFILL),
  ])
  def test_empty_prefill_values_are_accepted(self, step, prefill):
    import json

    assert self._save_step(self._client(json.dumps(prefill)), step) == prefill

  def test_step2_rejection_is_caught_by_the_shape_check_not_val(self):
    """op=31 is a prefill endpoint too, so `val` must not gate it either.

    A real rejection carries none of the keys step 3 consumes, so it is refused
    on shape — which is the check that actually distinguishes a rejection from
    the `val: 0` a successful save legitimately returns.
    """
    import json

    body = {"val": 0, "msg": "Morada recusada"}  # no menor_idade/escalao/estatuto
    with pytest.raises(SavResponseError, match="missing required key"):
      self._save_step(self._client(json.dumps(body)), 2)

  def test_step2_val_zero_with_a_full_prefill_is_accepted(self):
    import json

    body = {**self.STEP2_PREFILL, "val": 0}
    prefill = self._save_step(self._client(json.dumps(body)), 2)
    assert "val" not in prefill
    assert prefill["menor_idade"] == ""

  def test_validator_strips_val_and_passes_the_rest_through(self):
    prefill = {
      **self.STEP1_PREFILL,
      "val": "0",
      "endpoint_specific": {"opaque": True},
    }
    validated = SavClient._validate_registration_prefill(
      prefill, step=1, required_keys=tuple(self.STEP1_PREFILL),
    )
    # `val` is dropped; nothing else is touched.
    assert "val" not in validated
    assert validated["endpoint_specific"] == {"opaque": True}
    assert {k: v for k, v in prefill.items() if k != "val"} == validated

  @pytest.mark.parametrize("step", [1, 2])
  def test_php_fatal_does_not_leak_raw_body(self, step):
    with pytest.raises(SavServerError) as excinfo:
      self._save_step(self._client(self.PHP_FATAL), step)
    message = str(excinfo.value)
    for leak in ("mysqli_sql_exception", "SAV internal schema detail", "incricoesdb.php"):
      assert leak not in message

class TestSubidaDeEscalao:
  """Subida de escalão drives the op=36 commit via the op=21 tier lookup."""

  SUBIDA_MSG = (
    "<option value='0'>\n    - Não selecionado –\n    </option>"
    "<option value='6'>\n            Sub 14 </option>"
  )

  def _stub_enroll(self, monkeypatch, *, subida_tier):
    """Wire a logged-in client through the wizard up to the commit, capturing
    the commit body. `subida_tier` is the tuple ``_pick_subida_tier`` returns
    (None when SAV offers no real option)."""
    client = SavClient("https://sav2.fpb.pt", "user", "pass")
    client.session = {"organizacao": "270"}
    batch = type("BatchStub", (), {
      "id": 1, "is_open": True, "type_id": 2, "state": "Em construção",
      "tier": "Sub 14", "gender": "Masculino", "tier_id": 7,
    })()

    monkeypatch.setattr(client, "list_player_registration_batches", lambda season=None: [batch])
    monkeypatch.setattr(client, "_list_revalidable_licenses", lambda batch_obj: {301772})
    monkeypatch.setattr(client, "_load_player_record", lambda batch_id, license: {"id": 88})
    monkeypatch.setattr(client, "_build_step1_send", lambda *a, **k: "step1")
    monkeypatch.setattr(client, "_save_registration_step1", lambda batch_id, internal_id, send: {})
    monkeypatch.setattr(client, "_build_step2_send", lambda *a, **k: "step2")
    monkeypatch.setattr(
      client, "_save_registration_step2",
      lambda batch_type, batch_id, internal_id, license, send: {
        "menor_idade": 0, "escalao": 7, "estatuto": "A",
      },
    )
    monkeypatch.setattr(client, "_resolve_insurance_cascade", lambda internal_id, batch_obj, escalao: (11, 22))
    monkeypatch.setattr(client, "_resolve_taxa_id", lambda batch_obj, internal_id, estatuto: 33)
    monkeypatch.setattr(client, "_registration_precommit", lambda batch_id, internal_id: None)
    monkeypatch.setattr(
      client, "_pick_subida_tier",
      lambda internal_id, prefer_tier_id=None: subida_tier,
    )

    captured = {}

    def capture_commit(body):
      captured["body"] = body
      return {"val": 1, "resultfunction": "ok"}

    monkeypatch.setattr(client, "_registration_commit", capture_commit)
    return client, captured

  def test_subida_true_fetches_and_commits_tier(self, monkeypatch):
    client, captured = self._stub_enroll(monkeypatch, subida_tier=(6, "Sub 14"))
    client.add_player_to_registration_batch(
      1, 301772, exam_date=RECENT_EXAM_DATE, inline_subida=True,
    )
    assert captured["body"]["sub"] == "6"
    assert captured["body"]["escalaosubida_txt"] == "Sub 14"

  def test_no_subida_sends_minus_one(self, monkeypatch):
    client, captured = self._stub_enroll(monkeypatch, subida_tier=None)
    client.add_player_to_registration_batch(
      1, 301772, exam_date=RECENT_EXAM_DATE, inline_subida=False,
    )
    assert captured["body"]["sub"] == "-1"
    assert captured["body"]["escalaosubida_txt"] == "- Não selecionado –"

  def test_subida_with_no_option_raises(self, monkeypatch):
    client, captured = self._stub_enroll(monkeypatch, subida_tier=None)
    with pytest.raises(SavConfigError, match="no subida tier"):
      client.add_player_to_registration_batch(
        1, 301772, exam_date=RECENT_EXAM_DATE, inline_subida=True,
      )
    assert "body" not in captured  # never reached the commit

  def _stub_op21(self, monkeypatch, client, msg: str):
    resp = type("Resp", (), {
      "text": '{"msg":"' + msg.replace("\n", "\\n") + '","val":1}',
      "raise_for_status": lambda self: None,
    })()
    monkeypatch.setattr(client, "_http", type("H", (), {"get": lambda self, *a, **k: resp})())

  def test_list_subida_options_parses_single_option(self, monkeypatch):
    client = SavClient("https://sav2.fpb.pt", "user", "pass")
    self._stub_op21(monkeypatch, client, self.SUBIDA_MSG)
    assert client._list_subida_tier_options(88) == [(6, "Sub 14")]

  def test_list_subida_options_returns_empty_when_only_placeholder(self, monkeypatch):
    client = SavClient("https://sav2.fpb.pt", "user", "pass")
    self._stub_op21(
      monkeypatch, client,
      "<option value='0'>- N\\u00e3o selecionado \\u2013</option>",
    )
    assert client._list_subida_tier_options(88) == []

  def test_pick_subida_auto_picks_when_single_option(self, monkeypatch):
    client = SavClient("https://sav2.fpb.pt", "user", "pass")
    self._stub_op21(monkeypatch, client, self.SUBIDA_MSG)
    assert client._pick_subida_tier(88) == (6, "Sub 14")

  def test_pick_subida_honors_caller_hint(self, monkeypatch):
    # When SAV offers Sub 16 + Sub 18, the mod4-derived tier_id wins.
    multi = (
      "<option value='0'>- N\\u00e3o selecionado \\u2013</option>"
      "<option value='3'>Sub 16</option>"
      "<option value='10'>Sub 18</option>"
    )
    client = SavClient("https://sav2.fpb.pt", "user", "pass")
    self._stub_op21(monkeypatch, client, multi)
    assert client._pick_subida_tier(88, prefer_tier_id=10) == (10, "Sub 18")

  def test_pick_subida_raises_when_hint_not_offered(self, monkeypatch):
    # mod4 says Sub 18 (tier_id=10) but SAV only offers Sub 16 — surface the
    # form/server disagreement rather than silently committing the wrong tier.
    only_sub16 = (
      "<option value='0'>- N\\u00e3o selecionado \\u2013</option>"
      "<option value='3'>Sub 16</option>"
    )
    client = SavClient("https://sav2.fpb.pt", "user", "pass")
    self._stub_op21(monkeypatch, client, only_sub16)
    with pytest.raises(SavConfigError, match="not among SAV's offered"):
      client._pick_subida_tier(88, prefer_tier_id=10)

  def test_pick_subida_raises_on_ambiguous_no_hint(self, monkeypatch):
    multi = (
      "<option value='0'>- N\\u00e3o selecionado \\u2013</option>"
      "<option value='3'>Sub 16</option>"
      "<option value='10'>Sub 18</option>"
    )
    client = SavClient("https://sav2.fpb.pt", "user", "pass")
    self._stub_op21(monkeypatch, client, multi)
    with pytest.raises(SavConfigError, match="multiple subida tiers"):
      client._pick_subida_tier(88)


class TestStandaloneSubidaCommit:
  """op=50 has no success-body contract; verify the batch state instead."""

  LICENSE = 301772

  def _client(self, monkeypatch, commit_body, items):
    client = SavClient("https://sav2.fpb.pt", "user", "pass")
    client.session = {"organizacao": "270"}
    batch = type("BatchStub", (), {
      "id": 12,
      "tier": "Sub 14",
      "gender": "Masculino",
      "season": "2026/2027",
    })()
    response = type("Resp", (), {
      "text": commit_body,
      "raise_for_status": lambda self: None,
    })()
    monkeypatch.setattr(
      client, "_http", type("Http", (), {"post": lambda self, *a, **k: response})(),
    )
    monkeypatch.setattr(client, "_list_subida_licenses", lambda batch: {self.LICENSE})
    monkeypatch.setattr(client, "_load_subida_origin", lambda license: {})
    monkeypatch.setattr(
      client, "_resolve_subida_insurance_cascade", lambda batch, license: (1, 2),
    )
    monkeypatch.setattr(client, "_resolve_subida_taxa_id", lambda batch, license: 3)
    monkeypatch.setattr(client, "list_player_registration_batch_items", items)
    record_cache = Mock()
    monkeypatch.setattr(client._cache, "record_license_batch", record_cache)
    invalidate = Mock()
    monkeypatch.setattr(client, "_invalidate_batch_memo", invalidate)
    return client, batch, record_cache, invalidate

  def test_php_fatal_is_rejected_without_caching_or_leaking_body(self, monkeypatch):
    raw_body = "<html><b>Warning</b>: broken internal_subida_schema</html>"
    items = Mock()
    client, batch, record_cache, invalidate = self._client(monkeypatch, raw_body, items)

    with pytest.raises(SavServerError) as excinfo:
      client._add_player_to_subida_batch(batch, self.LICENSE, taxa_id=None)

    assert raw_body not in str(excinfo.value)
    items.assert_not_called()
    record_cache.assert_not_called()
    invalidate.assert_not_called()

  def test_explicit_rejection_uses_sav_message_without_caching(self, monkeypatch):
    raw_body = '{"val": 0, "msg": "Guia bloqueada", "debug": "internal_schema"}'
    items = Mock()
    client, batch, record_cache, invalidate = self._client(monkeypatch, raw_body, items)

    with pytest.raises(SavResponseError, match="Guia bloqueada") as excinfo:
      client._add_player_to_subida_batch(batch, self.LICENSE, taxa_id=None)

    assert raw_body not in str(excinfo.value)
    items.assert_not_called()
    record_cache.assert_not_called()
    invalidate.assert_not_called()

  def test_verified_item_reports_success_and_updates_cache(self, monkeypatch):
    client, batch, record_cache, invalidate = self._client(
      monkeypatch, "OK", lambda batch_id: [{"license": self.LICENSE, "name": "Ana"}],
    )

    assert client._add_player_to_subida_batch(batch, self.LICENSE, taxa_id=None) == self.LICENSE
    record_cache.assert_called_once_with(self.LICENSE, batch.id)
    invalidate.assert_called_once_with()

  def test_missing_item_after_commit_surfaces_failure_without_caching(self, monkeypatch):
    client, batch, record_cache, invalidate = self._client(
      monkeypatch, "OK", lambda batch_id: [],
    )

    with pytest.raises(SavResponseError, match="not present in batch"):
      client._add_player_to_subida_batch(batch, self.LICENSE, taxa_id=None)

    record_cache.assert_not_called()
    invalidate.assert_not_called()

  def test_item_listing_fatal_makes_commit_outcome_unverified(self, monkeypatch):
    raw_body = "<html>Fatal error: internal_subida_schema</html>"

    def fatal_items(batch_id):
      raise SavServerError(
        "Could not list items: SAV returned a server-side error; " + raw_body
      )

    client, batch, record_cache, invalidate = self._client(
      monkeypatch, "OK", fatal_items,
    )

    with pytest.raises(SavWriteUnverifiedError) as excinfo:
      client._add_player_to_subida_batch(batch, self.LICENSE, taxa_id=None)

    assert raw_body not in str(excinfo.value)
    record_cache.assert_not_called()
    invalidate.assert_not_called()


# ─── 1ª Inscrição (type-1) wizard ───────────────────────────────────────────

class TestPrimeiraInscricao:
  """Type-1 wizard: dispatch + commit body shape.

  The wizard end-to-end isn't exercised against the live server (it would
  commit a real player); instead we stub the wire calls and verify the
  dispatcher routes correctly and assembles the op=27 commit body with the
  type-1 field names (tipo/subida/companhia/seguro/apolice).
  """

  REQUIRED = dict(
    name="João Ferreira Loff", birth_date="2020-09-26", gender_id=1,
    nif="277544319", id_type=1, id_number="12345699", id_expiry="2029-09-26",
    email="x@y.pt", morada="Praceta", cod_postal="1300-536",
    distrito_id=1, concelho_id=5, exam_date=RECENT_EXAM_DATE,
  )

  def _stub_primeira(self, monkeypatch, *, subida_tier=None, minor=False):
    """Wire a logged-in client through every type-1 helper up to op=27,
    capturing the commit body."""
    client = SavClient("https://sav2.fpb.pt", "user", "pass")
    client.session = {"organizacao": "2430"}
    batch = type("BatchStub", (), {
      "id": 629084, "is_open": True, "type_id": 1, "state": "Em construção",
      "tier": "Baby-Basket", "gender": "Masculino", "tier_id": 33, "season": "2025/2026",
    })()
    monkeypatch.setattr(
      client, "list_player_registration_batches", lambda season=None: [batch],
    )

    # Modal-open guards + pre-create checks: every fire-and-forget op.
    monkeypatch.setattr(
      client, "_check_primeira_player_duplicate", lambda **kw: {"existe": 0},
    )
    monkeypatch.setattr(client, "_check_primeira_id_doc", lambda numid: None)
    monkeypatch.setattr(
      client, "_check_primeira_birthdate_fits_tier", lambda batch_obj, bd: None,
    )
    monkeypatch.setattr(
      client, "_primeira_batch_context_refresh", lambda batch_id: None,
    )
    monkeypatch.setattr(client, "_create_primeira_player", lambda **kw: 277534)
    monkeypatch.setattr(
      client, "_save_primeira_step2",
      lambda **kw: {"val": 1, "menor_idade": 1 if minor else 0},
    )
    monkeypatch.setattr(client, "_load_primeira_estatuto", lambda b, u: 6)
    monkeypatch.setattr(
      client, "_resolve_primeira_taxa_id",
      lambda b, u, est, **kw: 1052,
    )
    monkeypatch.setattr(
      client, "_resolve_primeira_insurance_cascade",
      lambda b, u: (4583, 4583, "100.268/1-2-16"),
    )
    monkeypatch.setattr(
      client, "_pick_subida_tier",
      lambda u, prefer_tier_id=None: subida_tier,
    )

    # Modal-open op=15 hits self._http.get — stub the transport entirely.
    monkeypatch.setattr(
      client, "_http",
      type("H", (), {"get": lambda self, *a, **k: type("R", (), {
        "text": "1", "raise_for_status": lambda self: None,
      })()})(),
    )

    captured = {}
    monkeypatch.setattr(
      client, "_primeira_commit",
      lambda body: (captured.update(body=body), {"val": 1, "msg": "", "resultexame": "2026-09-30"})[1],
    )
    return client, captured

  def test_dispatches_type1_and_builds_commit(self, monkeypatch):
    client, captured = self._stub_primeira(monkeypatch)
    userid = client.add_player_to_registration_batch(629084, **self.REQUIRED)

    assert userid == 277534
    body = captured["body"]
    assert body["guiaid"] == 629084
    assert body["userid"] == 277534
    # Type-1 commit uses `tipo` (not Revalidação's `transf`), `subida` (not
    # `sub`), `companhia` (not `comp`), plus new `seguro`/`apolice` keys.
    assert body["tipo"] == 1
    assert body["subida"] == "-1"
    assert body["escalaosubida_txt"] == "- Não selecionado –"
    assert body["seguro"] == "4583"
    assert body["companhia"] == "4583"
    assert body["apolice"] == "100.268/1-2-16"
    assert body["taxa"] == "1052"
    assert body["estatuto"] == "6"
    assert body["dataexame"] == RECENT_EXAM_DATE

  def test_inline_subida_populates_tier(self, monkeypatch):
    client, captured = self._stub_primeira(monkeypatch, subida_tier=(3, "Sub 16"))
    client.add_player_to_registration_batch(
      629084, inline_subida=True, **self.REQUIRED,
    )
    assert captured["body"]["subida"] == "3"
    assert captured["body"]["escalaosubida_txt"] == "Sub 16"

  def test_inline_subida_with_no_option_raises(self, monkeypatch):
    client, captured = self._stub_primeira(monkeypatch, subida_tier=None)
    with pytest.raises(SavConfigError, match="no subida tier"):
      client.add_player_to_registration_batch(
        629084, inline_subida=True, **self.REQUIRED,
      )
    assert "body" not in captured

  def test_minor_without_guardian_fields_raises(self, monkeypatch):
    client, _ = self._stub_primeira(monkeypatch, minor=True)
    with pytest.raises(SavConfigError, match="missing required fields"):
      client.add_player_to_registration_batch(629084, **self.REQUIRED)

  def test_minor_with_guardian_fields_commits(self, monkeypatch):
    client, captured = self._stub_primeira(monkeypatch, minor=True)
    client.add_player_to_registration_batch(
      629084,
      guardian_name="Pai", guardian_relation=1,
      guardian_phone="963000000", guardian_email="p@e.pt",
      **self.REQUIRED,
    )
    body = captured["body"]
    assert body["nomeEncarregado"] == "Pai"
    assert body["tipoRegulacao"] == "1"
    assert body["telefoneEncarregado"] == "963000000"
    assert body["emailEncarregado"] == "p@e.pt"

  def test_missing_required_field_raises_with_field_name(self):
    client = SavClient("https://sav2.fpb.pt", "user", "pass")
    client.session = {"organizacao": "2430"}
    batch = type("BatchStub", (), {
      "id": 1, "is_open": True, "type_id": 1, "state": "Em construção",
      "tier": "x", "gender": "x", "tier_id": 1,
    })()
    client.list_player_registration_batches = lambda season=None: [batch]
    # Missing `nif` (and all other type-1-required fields)
    with pytest.raises(ValueError, match="1ª Inscrição.*requires"):
      client.add_player_to_registration_batch(1, name="x", birth_date="2020-01-01")

  def test_duplicate_player_raises(self, monkeypatch):
    client, _ = self._stub_primeira(monkeypatch)
    monkeypatch.setattr(
      client, "_check_primeira_player_duplicate",
      lambda **kw: {"existe": 1, "id": 99},
    )
    with pytest.raises(SavResponseError, match="already exists in SAV"):
      client.add_player_to_registration_batch(629084, **self.REQUIRED)


# ─── live read-only ─────────────────────────────────────────────────────────

class TestListPlayerRegistrationBatches:
  def test_returns_well_formed_batches(self, client):
    results = client.list_player_registration_batches()

    assert isinstance(results, list)
    for batch in results:
      assert isinstance(batch, PlayerRegistrationBatch)
      assert batch.id > 0
      assert batch.type_id in (1, 2, 3, 4)
      assert batch.gender_id in (1, 2)
      assert batch.state_id >= 1
      assert isinstance(batch.is_open, bool)
      assert batch.is_open == (batch.state_id == 1)


class TestListPlayerRegistrationTiers:
  def test_male_and_female_tiers_returned(self, client):
    male = client.list_player_registration_tiers(gender_id=1)
    female = client.list_player_registration_tiers(gender_id=2)

    assert male and female
    for tier_id, name in male.items():
      assert isinstance(tier_id, int) and tier_id > 0
      assert isinstance(name, str) and name
    # The "Não selecionado" placeholder must be filtered out
    assert 0 not in male
    assert 0 not in female


class TestFindOpenPlayerRegistrationBatch:
  def test_returned_batch_satisfies_predicate(self, client):
    """Whatever find_open returns, it must match (open, type, tier, gender)."""
    for gender_id in (1, 2):
      for tier_id in client.list_player_registration_tiers(gender_id=gender_id):
        match = client.find_open_player_registration_batch(
          type=2, tier_id=tier_id, gender_id=gender_id,
        )
        if match is not None:
          assert match.is_open
          assert match.type_id == 2
          assert match.tier_id == tier_id
          assert match.gender_id == gender_id
          return
    pytest.skip("Account has no open Revalidação batch — predicate cannot be verified")

  def test_returns_none_for_impossible_tier(self, client):
    # 999999 cannot be a real tier — no batch can match
    assert client.find_open_player_registration_batch(
      type=2, tier_id=999999, gender_id=1,
    ) is None


# ─── create / delete (live, with cleanup) ───────────────────────────────────

class TestCreateAndDeletePlayerRegistrationBatch:
  def test_create_appears_in_list_with_expected_shape(self, client, transient_batch):
    batch = next(
      (b for b in client.list_player_registration_batches()
       if b.id == transient_batch["id"]),
      None,
    )
    assert batch is not None
    assert batch.is_open
    assert batch.type_id == 2
    assert batch.tier_id == transient_batch["tier_id"]
    assert batch.gender_id == transient_batch["gender_id"]
    assert batch.item_count == 0

  def test_independent_create_then_delete_cycle(self, client):
    slot = _first_free_slot(client, type_id=2)
    if slot is None:
      pytest.skip("No free (tier, gender) slot to create a delete-test batch")
    tier_id, gender_id = slot

    new_id = client.create_player_registration_batch(
      type=2, tier=tier_id, gender_id=gender_id,
    )
    try:
      assert new_id in {
        b.id for b in client.list_player_registration_batches()
      }
    finally:
      client.delete_player_registration_batch(new_id)

    assert new_id not in {
      b.id for b in client.list_player_registration_batches()
    }


# ─── add: validation paths against a real batch ─────────────────────────────
# Happy path is intentionally not exercised — it would commit a real player.

class TestAddPlayerToRegistrationBatchValidation:
  def test_unknown_batch_raises(self, client):
    with pytest.raises(ValueError, match=r"Batch id=\d+ not found"):
      client.add_player_to_registration_batch(999999999, 301772)

  def test_transferencia_batch_raises(self, client):
    transferencia = next(
      (b for b in client.list_player_registration_batches()
       if b.is_open and b.type_id == 3),
      None,
    )
    if transferencia is None:
      pytest.skip("No open Transferência batch on this account")

    with pytest.raises(NotImplementedError, match="Transferência"):
      client.add_player_to_registration_batch(transferencia.id, 301772)

  def test_closed_batch_raises(self, client):
    closed = next(
      (b for b in client.list_player_registration_batches() if not b.is_open),
      None,
    )
    if closed is None:
      pytest.skip("No closed batch on this account")

    with pytest.raises(ValueError, match="is not open"):
      client.add_player_to_registration_batch(closed.id, 301772)

  def test_licence_not_eligible_raises(self, client, transient_batch):
    with pytest.raises(ValueError, match="not eligible for revalidation"):
      client.add_player_to_registration_batch(transient_batch["id"], 999999999)


# ─── remove ─────────────────────────────────────────────────────────────────

class TestRemovePlayerFromRegistrationBatch:
  LICENSE = 301772
  BATCH_ID = 12

  def _client(self, monkeypatch, body, batches, probe=None):
    """`probe` stands in for the post-removal "is this licence still here?"
    check — `load_existing_registration_record`. Raising
    SavRecordNotFoundError means the player is gone."""
    client = SavClient("https://sav2.fpb.pt", "user", "pass")
    client.session = {"organizacao": "270"}
    response = type("Resp", (), {
      "text": body,
      "raise_for_status": lambda self: None,
    })()
    monkeypatch.setattr(
      client, "_http", type("Http", (), {"get": lambda self, *a, **k: response})(),
    )
    monkeypatch.setattr(client, "list_player_registration_batches", batches)
    if probe is None:
      probe = Mock(side_effect=SavRecordNotFoundError("gone"))
    monkeypatch.setattr(client, "load_existing_registration_record", probe)
    forget_cache = Mock()
    monkeypatch.setattr(client._cache, "forget_license_batch", forget_cache)
    return client, forget_cache

  @staticmethod
  def _batch(item_count):
    return type("BatchStub", (), {
      "id": TestRemovePlayerFromRegistrationBatch.BATCH_ID,
      "type_id": 2,
      "item_count": item_count,
    })()

  def test_one_byte_response_but_player_still_there_raises_without_success_log(
    self, monkeypatch, caplog,
  ):
    """The reported bug: HTTP 200, 1-byte body, nothing actually removed."""
    batches = Mock(return_value=[self._batch(1)])
    still_enrolled = Mock(return_value={"id": 88})
    client, forget_cache = self._client(monkeypatch, "1", batches, probe=still_enrolled)

    with caplog.at_level("INFO", logger="sav_client.sav_client"):
      with pytest.raises(SavResponseError, match=r"licence 301772.*still enrolled"):
        client.remove_player_from_registration_batch(self.BATCH_ID, self.LICENSE)

    assert not any("Removed player license=301772" in r.message for r in caplog.records)
    forget_cache.assert_not_called()

  def test_player_gone_succeeds_and_forgets_cache(self, monkeypatch):
    client, forget_cache = self._client(
      monkeypatch, "OK", Mock(return_value=[self._batch(1)]),
    )

    client.remove_player_from_registration_batch(self.BATCH_ID, self.LICENSE)

    forget_cache.assert_called_once_with(self.LICENSE)

  def test_a_concurrent_removal_cannot_falsely_confirm_this_one(self, monkeypatch):
    """Why the check is per-licence and not a batch count.

    An earlier version compared item_count before and after. Someone else
    removing a *different* player also makes the count fall, which would
    confirm a removal that never happened. Here the count drops 2 -> 1 while
    our licence is still enrolled: that must still fail.
    """
    batches = Mock(side_effect=[[self._batch(2)], [self._batch(1)]])
    still_enrolled = Mock(return_value={"id": 88})
    client, forget_cache = self._client(monkeypatch, "OK", batches, probe=still_enrolled)

    with pytest.raises(SavResponseError, match="still enrolled"):
      client.remove_player_from_registration_batch(self.BATCH_ID, self.LICENSE)
    forget_cache.assert_not_called()

  def test_the_probe_asks_about_this_licence_and_batch(self, monkeypatch):
    probe = Mock(side_effect=SavRecordNotFoundError("gone"))
    client, _ = self._client(
      monkeypatch, "OK", Mock(return_value=[self._batch(1)]), probe=probe,
    )

    client.remove_player_from_registration_batch(self.BATCH_ID, self.LICENSE)

    probe.assert_called_once_with(self.BATCH_ID, self.LICENSE)

  def test_probe_failure_is_unverified_and_forgets_cache(self, monkeypatch):
    broken_probe = Mock(side_effect=SavServerError("op=30 fatal"))
    client, forget_cache = self._client(
      monkeypatch, "OK", Mock(return_value=[self._batch(1)]), probe=broken_probe,
    )

    with pytest.raises(SavWriteUnverifiedError, match="Do not retry"):
      client.remove_player_from_registration_batch(self.BATCH_ID, self.LICENSE)

    forget_cache.assert_called_once_with(self.LICENSE)

  def test_php_fatal_is_rejected_without_leaking_raw_body(self, monkeypatch):
    raw_body = "<html><b>Warning</b>: remove_internal_schema</html>"
    client, forget_cache = self._client(
      monkeypatch, raw_body, Mock(return_value=[self._batch(1)]),
    )

    with pytest.raises(SavServerError) as excinfo:
      client.remove_player_from_registration_batch(self.BATCH_ID, self.LICENSE)

    assert raw_body not in str(excinfo.value)
    forget_cache.assert_not_called()

  def test_explicit_rejection_uses_sav_message(self, monkeypatch):
    client, forget_cache = self._client(
      monkeypatch,
      '{"val": 0, "msg": "Jogador bloqueado"}',
      Mock(return_value=[self._batch(1)]),
    )

    with pytest.raises(SavResponseError, match="Jogador bloqueado"):
      client.remove_player_from_registration_batch(self.BATCH_ID, self.LICENSE)

    forget_cache.assert_not_called()

  def test_unknown_batch_raises(self, client):
    with pytest.raises(ValueError, match=r"Batch id=\d+ not found"):
      client.remove_player_from_registration_batch(999999999, 301772)


class TestStep1PrefillIsNotAWriteAck:
  """`val` is not a universal success flag in SAV.

  op=36 uses val:1 for success; op=33 returns **val:0 on a successful save**,
  alongside the step-2 prefill. Applying the write-ack check to op=33 rejected
  every Revalidação at step 1 — reproduced live on licence 298337 (Alexandre
  Silva Fragoso) on 2026-08-28, where bypassing that one check let the whole
  flow complete and return internal id 252299.

  A PHP fatal is still caught by `_parse_json_response`, and the response shape
  by `_validate_registration_prefill`, so dropping the write-ack check here
  loses no real protection.
  """

  # The exact op=33 body captured from that run.
  REAL_STEP1_RESPONSE = (
    '{"pais":"155","morada":"Rua Professora Carolina Amalia",'
    '"codpostal":"2040-207","concelho":"210","distrito":"14",'
    '"localidade":null,"clube":"Rio Maior Basket",'
    '"nome":"Alexandre Silva Fragoso","val":0,"localidade_txt":"Rio Maior"}'
  )

  def _client(self, monkeypatch, body):
    c = SavClient.__new__(SavClient)
    c.base_url = "https://sav2.example/"
    c.session = {"perfil": 1, "user": "u", "organizacao": 1}
    c._timeout = 10

    class _Http:
      def get(self, *a, **k):
        return type("R", (), {"text": body, "raise_for_status": lambda self: None})()

    c._http = _Http()
    return c

  def test_exact_val_zero_prefill_is_accepted_with_val_stripped(self, monkeypatch):
    c = self._client(monkeypatch, self.REAL_STEP1_RESPONSE)
    prefill = c._save_registration_step1(630304, 252299, "send")
    assert prefill == {
      "pais": "155",
      "morada": "Rua Professora Carolina Amalia",
      "codpostal": "2040-207",
      "concelho": "210",
      "distrito": "14",
      "localidade": None,
      "clube": "Rio Maior Basket",
      "nome": "Alexandre Silva Fragoso",
      "localidade_txt": "Rio Maior",
    }
    # val:0 meant SUCCESS on op=33; it is stripped so nothing reads it as a
    # verdict again. Everything else arrives exactly as SAV sent it.
    assert "val" not in prefill

  def test_prefill_still_reaches_step2_unchanged(self, monkeypatch):
    c = self._client(monkeypatch, self.REAL_STEP1_RESPONSE)
    prefill = c._save_registration_step1(630304, 252299, "send")
    send = SavClient._build_step2_send(
      prefill, morada=None, cod_postal=None, localidade_txt=None,
      distrito_id=None, concelho_id=None,
    )
    # The address must survive; NULLs here are what wiped a live record before.
    assert 'morada="Rua Professora Carolina Amalia"' in send
    assert "distrito=14," in send and "concelho=210," in send

  def test_full_revalidation_flow_reaches_commit_and_returns_internal_id(
    self, monkeypatch,
  ):
    c = self._client(monkeypatch, self.REAL_STEP1_RESPONSE)
    c._cache = Mock()
    batch = type("BatchStub", (), {
      "id": 630304,
      "is_open": True,
      "type_id": 2,
      "state": "Em construção",
      "tier": "Sub 14",
      "gender": "Masculino",
      "tier_id": 7,
    })()
    captured = {}

    monkeypatch.setattr(c, "list_player_registration_batches", lambda season=None: [batch])
    monkeypatch.setattr(c, "_list_revalidable_licenses", lambda batch_obj: {298337})
    monkeypatch.setattr(c, "_load_player_record", lambda batch_id, license: {"id": 252299})
    monkeypatch.setattr(c, "_build_step1_send", lambda *args, **kwargs: "step1")

    def capture_step2(prefill, **kwargs):
      captured["prefill"] = prefill
      return "step2"

    monkeypatch.setattr(c, "_build_step2_send", capture_step2)
    monkeypatch.setattr(
      c, "_save_registration_step2",
      lambda *args, **kwargs: {"menor_idade": 0, "escalao": 7, "estatuto": "A"},
    )
    monkeypatch.setattr(c, "_commit_registration_step3", lambda *args, **kwargs: 252299)

    assert c.add_player_to_registration_batch(630304, 298337) == 252299
    assert "val" not in captured["prefill"]
    assert captured["prefill"]["nome"] == "Alexandre Silva Fragoso"

  def test_a_malformed_shape_is_still_rejected(self, monkeypatch):
    c = self._client(monkeypatch, '{"val":0,"nome":"X"}')  # missing address keys
    with pytest.raises(SavResponseError, match="missing required key"):
      c._save_registration_step1(630304, 252299, "send")

  def test_a_php_fatal_is_still_rejected(self, monkeypatch):
    fatal = "<br /><b>Fatal error</b>:  Uncaught mysqli_sql_exception: boom"
    c = self._client(monkeypatch, fatal)
    with pytest.raises(SavServerError):
      c._save_registration_step1(630304, 252299, "send")
