import pytest

from sav_client import SavClient
from sav_client.exceptions import SavConfigError, SavResponseError
from sav_client.models import PlayerRegistrationBatch


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


# ─── subida de escalão (op=21 + commit) ──────────────────────────────────────

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
      1, 301772, exam_date="2026-05-25", inline_subida=True,
    )
    assert captured["body"]["sub"] == "6"
    assert captured["body"]["escalaosubida_txt"] == "Sub 14"

  def test_no_subida_sends_minus_one(self, monkeypatch):
    client, captured = self._stub_enroll(monkeypatch, subida_tier=None)
    client.add_player_to_registration_batch(
      1, 301772, exam_date="2026-05-25", inline_subida=False,
    )
    assert captured["body"]["sub"] == "-1"
    assert captured["body"]["escalaosubida_txt"] == "- Não selecionado –"

  def test_subida_with_no_option_raises(self, monkeypatch):
    client, captured = self._stub_enroll(monkeypatch, subida_tier=None)
    with pytest.raises(SavConfigError, match="no subida tier"):
      client.add_player_to_registration_batch(
        1, 301772, exam_date="2026-05-25", inline_subida=True,
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
    distrito_id=1, concelho_id=5, exam_date="2025-09-26",
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
    assert body["dataexame"] == "2025-09-26"

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


# ─── remove: live ───────────────────────────────────────────────────────────

class TestRemovePlayerFromRegistrationBatch:
  def test_unknown_batch_raises(self, client):
    with pytest.raises(ValueError, match=r"Batch id=\d+ not found"):
      client.remove_player_from_registration_batch(999999999, 301772)
