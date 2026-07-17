"""Editing an enrolment's exam date re-fires the op=36 step-3 commit.

SAV2 has no read-back of a committed item's step-3 selections, so the edit
path rebuilds the commit body: these tests pin that `dataexame` is written,
that a missing address doesn't stop the re-commit, and that omitting
`exam_date` leaves the old personal/address-only behaviour untouched.
"""

import pytest

from sav_client.exceptions import SavConfigError
from sav_client.sav_client import SavClient

from sav_mcp import server as server_module


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
    exam_date="2026-05-01", taxa_id=None,
    promote_to_tier_id=None, inline_subida=False,
    guardian_name=None, guardian_relation=None, guardian_phone=None, guardian_email=None,
    consent_data=True, consent_communications=True, consent_marketing=False,
  )

  assert uid == 1234
  body = captured["body"]
  assert body["dataexame"] == "2026-05-01"
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
      exam_date="2026-05-01", taxa_id=55,
      promote_to_tier_id=None, inline_subida=False,
      guardian_name=None, guardian_relation=None, guardian_phone=None, guardian_email=None,
      consent_data=True, consent_communications=True, consent_marketing=False,
    )


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
    exam_date="2026-05-01",
  )

  assert uid == 1234
  assert "commit" in calls  # op=36 re-fired even with no address change
  assert calls["commit"][1]["exam_date"] == "2026-05-01"


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
    fields={"exam_date": "2026-05-01", "guardian_relation": "3", "consent_marketing": True},
  )

  assert out == {"success": True, "player_id": 999}
  assert captured["exam_date"] == "2026-05-01"
  assert captured["guardian_relation"] == 3  # coerced to int
  assert captured["consent_marketing"] is True
