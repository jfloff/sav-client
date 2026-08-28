"""Editing an enrolment's exam date re-fires the op=36 step-3 commit.

op=31 returns the committed step-3 selections. These tests pin that an
exam-date edit preserves that prefill unless an explicit value, including
false or an empty string, asks to overwrite it.
"""

from datetime import date, timedelta

import pytest

from sav_client.exceptions import SavConfigError
from sav_client.sav_client import SavClient

from sav_mcp import server as server_module

# SAV rejects an exam date outside its validity window (exam date + 12 months),
# and `_coerce_exam_date` now enforces both bounds. A literal date would quietly
# age out of that window and start failing on a date unrelated to any code
# change, so tests reaching the real commit path anchor to today instead.
RECENT_EXAM_DATE = (date.today() - timedelta(days=30)).isoformat()


class _Batch:
  id = 42
  tier_id = 7
  type_id = 2  # Revalidação
  is_open = True
  state = "Em construção"
  tier = "Sub-14"
  gender = "M"
  number = 5
  type = "Revalidação"


def _bare_client() -> SavClient:
  import threading

  client = SavClient.__new__(SavClient)
  client.session = {"user": "u"}
  client._timeout = 5
  client._base_url = "https://example.invalid"
  # The step-3 commit invalidates the batch-listing memo; a bare (no-__init__)
  # client needs the memo attributes present for that.
  client._batch_memo = {}
  client._batch_memo_lock = threading.Lock()
  return client


def _stub_step3_commit(monkeypatch, client, prefill):
  captured: dict = {}

  monkeypatch.setattr(
    client, "_resolve_insurance_cascade",
    lambda internal_id, batch, escalao: (1, 99),
  )
  monkeypatch.setattr(client, "_resolve_taxa_id", lambda batch, internal_id, estatuto: 55)
  monkeypatch.setattr(client, "_registration_precommit", lambda guia, uid: None)
  def _capture_commit(body):
    captured["body"] = body
    return {"val": 1, "resultfunction": "ok"}

  monkeypatch.setattr(client, "_registration_commit", _capture_commit)

  class _Cache:
    def record_license_batch(self, license, batch_id):
      pass

  client._cache = _Cache()
  return captured


def _commit_step3(client, prefill, **overrides):
  kwargs = {
    "exam_date": RECENT_EXAM_DATE,
    "taxa_id": None,
    "promote_to_tier_id": None,
    "inline_subida": None,
    "guardian_name": None,
    "guardian_relation": None,
    "guardian_phone": None,
    "guardian_email": None,
    "consent_data": None,
    "consent_communications": None,
    "consent_marketing": None,
  }
  kwargs.update(overrides)
  return client._commit_registration_step3(_Batch(), 1234, 301772, prefill, **kwargs)


def test_commit_step3_sends_exam_date(monkeypatch):
  client = _bare_client()
  captured: dict = {}

  monkeypatch.setattr(client, "_resolve_insurance_cascade", lambda internal_id, batch, escalao: (1, 99))
  monkeypatch.setattr(client, "_resolve_taxa_id", lambda batch, internal_id, estatuto: 55)
  monkeypatch.setattr(client, "_registration_precommit", lambda guia, uid: None)

  def _fake_commit(body):
    captured["body"] = body
    return {"val": 1, "resultfunction": "ok"}

  monkeypatch.setattr(client, "_registration_commit", _fake_commit)

  class _Cache:
    def record_license_batch(self, license, batch_id):
      pass

  client._cache = _Cache()

  uid = client._commit_registration_step3(
    _Batch(), 1234, 301772, {"estatuto": "S", "escalao": 7, "menor_idade": 0},
    exam_date=RECENT_EXAM_DATE, taxa_id=None,
    promote_to_tier_id=None, inline_subida=False,
    guardian_name=None, guardian_relation=None, guardian_phone=None, guardian_email=None,
    consent_data=True, consent_communications=True, consent_marketing=False,
  )

  assert uid == 1234
  body = captured["body"]
  assert body["dataexame"] == RECENT_EXAM_DATE
  assert body["guiaid"] == 42
  assert body["userid"] == 1234
  assert body["taxa"] == "55"
  assert body["sub"] == "-1"  # no subida re-supplied


def test_commit_step3_minor_requires_guardian():
  client = _bare_client()
  # Guardian validation runs before any HTTP, so no stubs are needed.
  with pytest.raises(SavConfigError, match="minor"):
    client._commit_registration_step3(
      _Batch(), 1, 2, {"menor_idade": 1},
      exam_date=RECENT_EXAM_DATE, taxa_id=55,
      promote_to_tier_id=None, inline_subida=False,
      guardian_name=None, guardian_relation=None, guardian_phone=None, guardian_email=None,
      consent_data=True, consent_communications=True, consent_marketing=False,
    )


def test_exam_date_only_edit_preserves_step3_consents(monkeypatch):
  client = _bare_client()
  prefill = {
    "estatuto": "S", "escalao": 7, "menor_idade": 0, "taxa": "1090",
    "subida": "-1", "escalaosubida": None,
    "concordo_tratamento_dados": "1",
    "receber_comunicacoes": "1",
    "autoriza_utilizacao_dados": None,
  }
  captured = _stub_step3_commit(monkeypatch, client, prefill)
  _commit_step3(client, prefill)

  assert captured["body"]["taxa"] == "1090"
  assert captured["body"]["consentimentoDados"] == 1
  assert captured["body"]["comunicacoes"] == 1
  assert captured["body"]["marketing"] == 0


def test_stored_zero_consent_stays_off_on_wire(monkeypatch):
  client = _bare_client()
  prefill = {
    "estatuto": "S", "escalao": 7, "menor_idade": 0, "taxa": "1090",
    "concordo_tratamento_dados": "0",
    "receber_comunicacoes": "0",
    "autoriza_utilizacao_dados": "0",
  }
  captured = _stub_step3_commit(monkeypatch, client, prefill)
  _commit_step3(client, prefill)

  assert captured["body"]["consentimentoDados"] == 0
  assert captured["body"]["comunicacoes"] == 0
  assert captured["body"]["marketing"] == 0


def test_explicit_marketing_false_overwrites_stored_one(monkeypatch):
  client = _bare_client()
  prefill = {
    "estatuto": "S", "escalao": 7, "menor_idade": 0, "taxa": "1090",
    "autoriza_utilizacao_dados": "1",
  }
  captured = _stub_step3_commit(monkeypatch, client, prefill)
  _commit_step3(client, prefill, consent_marketing=False)

  assert captured["body"]["marketing"] == 0


def test_explicit_empty_guardian_phone_and_taxa_overwrite_stored_values(monkeypatch):
  client = _bare_client()
  prefill = {
    "estatuto": "S", "escalao": 7, "menor_idade": 0, "taxa": "1090",
    "telefone_menor": "+351912345678",
  }
  captured = _stub_step3_commit(monkeypatch, client, prefill)
  _commit_step3(client, prefill, guardian_phone="", taxa_id="")

  assert captured["body"]["telefoneEncarregado"] == ""
  assert captured["body"]["taxa"] == ""


def test_subida_is_preserved_unless_explicitly_cleared(monkeypatch):
  client = _bare_client()
  prefill = {
    "estatuto": "S", "escalao": 7, "menor_idade": 0, "taxa": "1090",
    "subida": "6", "escalaosubida": "Sub 14",
  }
  captured = _stub_step3_commit(monkeypatch, client, prefill)
  _commit_step3(client, prefill)
  assert captured["body"]["sub"] == "6"
  assert captured["body"]["escalaosubida_txt"] == "Sub 14"

  captured = _stub_step3_commit(monkeypatch, client, prefill)
  _commit_step3(client, prefill, inline_subida=False)
  assert captured["body"]["sub"] == "-1"


def test_minor_raises_when_preserved_guardian_block_is_empty():
  client = _bare_client()
  with pytest.raises(SavConfigError, match="minor"):
    _commit_step3(
      client,
      {"menor_idade": 1, "estatuto": "S", "escalao": 7, "taxa": "1090"},
    )


def test_minor_raises_when_explicit_guardian_phone_is_cleared():
  client = _bare_client()
  prefill = {
    "menor_idade": 1, "estatuto": "S", "escalao": 7, "taxa": "1090",
    "nome_encarregado_menor": "Marlene Figueiredo",
    "tipo_regulacao_menor": "2",
    "telefone_menor": "+351912345678",
    "email_menor": "marlene.teste@gmail.com",
  }
  with pytest.raises(SavConfigError, match="guardian_phone"):
    _commit_step3(client, prefill, guardian_phone="")


def test_update_existing_player_routes_exam_date_to_commit(monkeypatch):
  client = _bare_client()
  calls: dict = {}

  monkeypatch.setattr(client, "load_existing_registration_record", lambda batch_id, license: {"id": 1234})
  monkeypatch.setattr(client, "_build_step1_send", lambda record, **k: {})
  monkeypatch.setattr(client, "_save_registration_step1", lambda guia, uid, send: {"prefill": 1})
  monkeypatch.setattr(client, "_build_step2_send", lambda prefill, **k: {})
  monkeypatch.setattr(
    client, "_save_registration_step2",
    lambda tp, guia, uid, lic, send: {"estatuto": "S", "escalao": 7, "menor_idade": 0},
  )
  def _fake_step3(*a, **k):
    calls["commit"] = (a, k)
    return 1234

  monkeypatch.setattr(client, "_commit_registration_step3", _fake_step3)

  uid = client._update_existing_player_in_batch(
    _Batch(), 301772,
    id_type=None, id_number=None, id_expiry=None,
    telemovel=None, telefone=None, email=None,
    nome_pai=None, nome_mae=None,
    morada=None, cod_postal=None, localidade_txt=None,
    distrito_id=None, concelho_id=None,
    exam_date=RECENT_EXAM_DATE,
  )

  assert uid == 1234
  assert "commit" in calls  # op=36 re-fired even with no address change
  assert calls["commit"][1]["exam_date"] == RECENT_EXAM_DATE
  assert calls["commit"][1]["inline_subida"] is None
  assert calls["commit"][1]["consent_data"] is None


def test_update_existing_player_without_exam_date_skips_commit(monkeypatch):
  client = _bare_client()
  committed = {"called": False}

  monkeypatch.setattr(client, "load_existing_registration_record", lambda batch_id, license: {"id": 1234})
  monkeypatch.setattr(client, "_build_step1_send", lambda record, **k: {})
  monkeypatch.setattr(client, "_save_registration_step1", lambda guia, uid, send: {"prefill": 1})

  def _boom_step2(*a, **k):
    raise AssertionError("step-2 must not be saved with no address and no exam edit")

  monkeypatch.setattr(client, "_save_registration_step2", _boom_step2)
  monkeypatch.setattr(
    client, "_commit_registration_step3",
    lambda *a, **k: committed.__setitem__("called", True) or 1,
  )

  uid = client._update_existing_player_in_batch(
    _Batch(), 301772,
    id_type=None, id_number=None, id_expiry=None,
    telemovel=None, telefone=None, email=None,
    nome_pai=None, nome_mae=None,
    morada=None, cod_postal=None, localidade_txt=None,
    distrito_id=None, concelho_id=None,
  )

  assert uid == 1234
  assert committed["called"] is False


def test_update_enrollment_tool_accepts_exam_date(monkeypatch):
  captured: dict = {}

  class StubClient:
    def update_player_in_registration_batch(self, batch_id, license, **kwargs):
      captured["batch_id"] = batch_id
      captured["license"] = license
      captured.update(kwargs)
      return 999

  monkeypatch.setattr(server_module, "_get_client", lambda: StubClient())
  monkeypatch.setattr(server_module, "_resolve_license_batch", lambda client, license: 12)

  out = server_module.update_enrollment(
    license=301772,
    fields={"exam_date": RECENT_EXAM_DATE, "guardian_relation": "3", "consent_marketing": True},
  )

  assert out == {"success": True, "license": 301772}
  assert captured["exam_date"] == RECENT_EXAM_DATE
  assert captured["guardian_relation"] == 3  # coerced to int
  assert captured["consent_marketing"] is True


def test_update_enrollment_keeps_explicit_false_and_empty_values(monkeypatch):
  captured: dict = {}

  class StubClient:
    def update_player_in_registration_batch(self, batch_id, license, **kwargs):
      captured.update(kwargs)
      return 999

  monkeypatch.setattr(server_module, "_get_client", lambda: StubClient())
  monkeypatch.setattr(server_module, "_resolve_license_batch", lambda client, license: 12)

  server_module.update_enrollment(
    license=301772,
    fields={
      "inline_subida": False, "consent_marketing": False,
      "guardian_phone": "", "guardian_relation": "", "taxa_id": "",
    },
  )

  assert captured["inline_subida"] is False
  assert captured["consent_marketing"] is False
  assert captured["guardian_phone"] == ""
  assert captured["guardian_relation"] == ""
  assert captured["taxa_id"] == ""
