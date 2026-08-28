"""MCP server tests for ensure_open_batch, the preview_enrollment resolution
fold-in (license: null), and the minor-guardian needs_review surfacing."""

import pytest

from sav_parsers.types import DocType, ParsedField

from sav_mcp import server as server_module
from sav_shared.fpb_mod1 import player_is_minor


def _batch_stub(**overrides):
  attrs = {
    "id": 12, "number": "2025/12", "club_id": 99, "type_id": 2,
    "type": "Revalidação", "tier": "Sub 14", "gender": "Masculino",
    "item_count": 3,
  }
  attrs.update(overrides)
  return type("BatchStub", (), attrs)()


def _reconcile_stub(**overrides):
  attrs = {
    "kwargs": {"license": 301772},
    "updated": {},
    "kept": {},
    "needs_review": [],
    "retrain_corrections": {},
  }
  attrs.update(overrides)
  return type("ResultStub", (), attrs)()


def _mod1_form(parsed=None, reg_type=2):
  return {
    "parsed": parsed or {},
    "processing_id": None,
    "pdf_bytes": b"%PDF-1.4\n",
    "doc_type": DocType.FPB_MODELO_1,
    "reg_type": reg_type,
    "tier_id": 5,
    "gender_id": 1,
  }


# ── ensure_open_batch ─────────────────────────────────────────────────────────

def test_ensure_open_batch_returns_existing(monkeypatch):
  batch = _batch_stub()

  class StubClient:
    def find_open_player_registration_batch(self, *, type, tier_id, gender_id):
      assert (type, tier_id, gender_id) == (2, 5, 1)
      return batch

  monkeypatch.setattr(server_module, "_get_client", lambda: StubClient())
  monkeypatch.setattr(
    server_module, "create_and_fetch_batch",
    lambda *a, **kw: pytest.fail("create_and_fetch_batch must not run when an open batch exists"),
  )

  result = server_module.ensure_open_batch(reg_type=2, tier_id=5, gender_id=1)
  assert result == {
    "number": "2025/12", "type": "Revalidação", "tier": "Sub 14",
    "gender": "Masculino", "item_count": 3, "created": False,
  }


def test_ensure_open_batch_creates_when_absent(monkeypatch):
  batch = _batch_stub(item_count=0)

  class StubClient:
    def find_open_player_registration_batch(self, *, type, tier_id, gender_id):
      return None

  def fake_create(client, *, batch_type, tier_id, gender_id):
    assert (batch_type, tier_id, gender_id) == (2, 5, 1)
    return 12, batch

  monkeypatch.setattr(server_module, "_get_client", lambda: StubClient())
  monkeypatch.setattr(server_module, "create_and_fetch_batch", fake_create)

  result = server_module.ensure_open_batch(reg_type=2, tier_id=5, gender_id=1)
  assert result["created"] is True
  assert result["number"] == "2025/12"
  assert result["item_count"] == 0


# ── player_is_minor ───────────────────────────────────────────────────────────

def test_player_is_minor_rule():
  assert player_is_minor("2020-01-01") is True
  assert player_is_minor("1990-01-01") is False
  assert player_is_minor("") is None
  assert player_is_minor(None) is None
  # Deterministic with an explicit reference date: 17 on the ref day.
  assert player_is_minor("2008-01-01", "2025-12-31") is True
  assert player_is_minor("2008-01-01", "2026-01-01") is False


# ── preview_enrollment: minor guardian needs_review ───────────────────────────

_GUARDIAN_FIELDS = (
  "guardian_name", "guardian_relation", "guardian_phone", "guardian_email",
)


def _preview_reval(monkeypatch, *, nasc, reconcile_stub, license=301772):
  class StubClient:
    def load_player_profile(self, lic, club_id=None):
      return {"nome": "Player A", "nasc": nasc}

  monkeypatch.setattr(server_module, "_get_client", lambda: StubClient())
  monkeypatch.setattr(
    server_module, "reconcile_fpb_mod1",
    lambda parsed, sav_profile, client=None: reconcile_stub,
  )
  monkeypatch.setattr(server_module, "_forms", {"m1": _mod1_form()})
  return server_module.preview_enrollment(
    batch_number="2025/12", license=license, mod1_id="m1",
  )


def test_preview_appends_guardian_fields_for_minor(monkeypatch):
  stub = _reconcile_stub()
  preview = _preview_reval(monkeypatch, nasc="2015-03-01", reconcile_stub=stub)

  for kwarg in _GUARDIAN_FIELDS:
    assert kwarg in preview["needs_review"]
  rows = {f["kwarg"]: f for f in preview["fields"] if f["kwarg"] in _GUARDIAN_FIELDS}
  assert set(rows) == set(_GUARDIAN_FIELDS)
  for row in rows.values():
    assert row["status"] == "needs_review"
    assert row["final_value"] is None
  # The cached ReconcileResult must stay pristine: submit stages OCR-training
  # corrections from it, and chat-supplied guardian answers must not be
  # labeled onto a form whose guardian block may be blank.
  assert stub.needs_review == []


def test_preview_no_guardian_fields_for_adult(monkeypatch):
  stub = _reconcile_stub()
  preview = _preview_reval(monkeypatch, nasc="1990-01-01", reconcile_stub=stub)

  for kwarg in _GUARDIAN_FIELDS:
    assert kwarg not in preview["needs_review"]
  assert preview["needs_review"] == []


def test_preview_guardian_field_with_value_not_duplicated(monkeypatch):
  stub = _reconcile_stub(
    kwargs={"license": 301772, "guardian_name": "Maria Silva"},
  )
  preview = _preview_reval(monkeypatch, nasc="2015-03-01", reconcile_stub=stub)

  # guardian_name already carries an OCR value → one "ocr" row, no review row.
  name_rows = [f for f in preview["fields"] if f["kwarg"] == "guardian_name"]
  assert len(name_rows) == 1
  assert name_rows[0]["status"] == "ocr"
  assert "guardian_name" not in preview["needs_review"]
  for kwarg in ("guardian_relation", "guardian_phone", "guardian_email"):
    assert kwarg in preview["needs_review"]


# ── preview_enrollment: license null auto-resolution ──────────────────────────

def test_preview_license_null_auto_resolves(monkeypatch):
  batch = _batch_stub()

  class StubClient:
    def list_player_registration_batches(self):
      return [batch]

    def _list_revalidable_licenses(self, b):
      assert b is batch
      return {301772}

    def load_player_profile(self, lic, club_id=None):
      assert lic == 301772
      return {"nome": "Player A", "nasc": "1990-01-01"}

  monkeypatch.setattr(server_module, "_get_client", lambda: StubClient())
  monkeypatch.setattr(
    server_module, "reconcile_fpb_mod1",
    lambda parsed, sav_profile, client=None: _reconcile_stub(),
  )
  parsed = {"licenca_fpb": ParsedField(value="301772", confidence=0.99)}
  monkeypatch.setattr(server_module, "_forms", {"m1": _mod1_form(parsed=parsed)})

  preview = server_module.preview_enrollment(
    batch_number="2025/12", license=None, mod1_id="m1",
  )

  assert preview["resolved"] is True
  assert preview["player"]["license"] == 301772
  assert preview["player"]["name"] == "Player A"


def test_preview_license_null_unresolved_returns_candidates(monkeypatch):
  batch = _batch_stub()

  class StubClient:
    def list_player_registration_batches(self):
      return [batch]

    def _list_revalidable_licenses(self, b):
      return set()  # OCR licence not eligible → no resolution

    def load_player_profile(self, lic, club_id=None):
      pytest.fail("preview must short-circuit before loading a profile")

  monkeypatch.setattr(server_module, "_get_client", lambda: StubClient())
  parsed = {"licenca_fpb": ParsedField(value="301772", confidence=0.99)}
  monkeypatch.setattr(server_module, "_forms", {"m1": _mod1_form(parsed=parsed)})

  result = server_module.preview_enrollment(
    batch_number="2025/12", license=None, mod1_id="m1",
  )

  assert result["resolved"] is False
  assert result["candidates"] == []
  assert result["ocr_license"] == 301772
  # No preview payload on an unresolved call.
  assert "fields" not in result


def test_preview_license_null_primeira_duplicate_guard(monkeypatch):
  class StubClient:
    def _check_primeira_player_duplicate(self, *, gender_id, birth_date, id_number):
      assert (gender_id, birth_date, id_number) == (1, "2015-03-01", "12345678")
      return {"existe": 1, "id": 555}

    def find_license_by_nif(self, nif):
      assert nif == "277544319"
      return None

  monkeypatch.setattr(server_module, "_get_client", lambda: StubClient())
  parsed = {
    "nome_completo": ParsedField(value="Player B", confidence=0.99),
    "data_nascimento": ParsedField(value="2015-03-01", confidence=0.99),
    "num_doc_identificacao": ParsedField(value="12345678", confidence=0.99),
    "nif": ParsedField(value="277544319", confidence=0.99),
  }
  monkeypatch.setattr(
    server_module, "_forms", {"m1": _mod1_form(parsed=parsed, reg_type=1)},
  )

  result = server_module.preview_enrollment(
    batch_number="2025/12", license=None, mod1_id="m1",
  )

  assert result["resolved"] is False
  assert result["error"] == "player_already_in_sav"
  assert result["existing_license"] is None
  assert "existing_sav_id" not in result
  assert "name or NIF search" in result["reason"]


def test_preview_explicit_license_skips_resolution(monkeypatch):
  """With an explicit licence the resolution machinery must never run — the
  stub client deliberately lacks the listing/eligibility methods."""

  class StubClient:
    def load_player_profile(self, lic, club_id=None):
      return {"nome": "Player A", "nasc": "1990-01-01"}

  monkeypatch.setattr(server_module, "_get_client", lambda: StubClient())
  monkeypatch.setattr(
    server_module, "reconcile_fpb_mod1",
    lambda parsed, sav_profile, client=None: _reconcile_stub(),
  )
  monkeypatch.setattr(server_module, "_forms", {"m1": _mod1_form()})

  preview = server_module.preview_enrollment(
    batch_number="2025/12", license=301772, mod1_id="m1",
  )

  assert "resolved" not in preview
  assert preview["player"]["license"] == 301772
