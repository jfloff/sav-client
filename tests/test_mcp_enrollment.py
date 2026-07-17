"""MCP server tests for the license-first enrollment surface."""

from sav_mcp import server as server_module


def test_read_enrollment_returns_player_detail(monkeypatch):
  class StubClient:
    def resolve_batch_id_by_license(self, license):
      assert license == 301772
      return 42

    def load_existing_registration_record(self, batch_id, license):
      assert (batch_id, license) == (42, 301772)
      return {"id": 77, "nome": "Player A"}

  monkeypatch.setattr(server_module, "_get_client", lambda: StubClient())

  result = server_module.read_enrollment(license=301772)
  assert result == {"id": 77, "nome": "Player A"}


def test_read_enrollment_license_not_enrolled_returns_structured_error(monkeypatch):
  from sav_client.exceptions import LicenseNotEnrolledError

  class StubClient:
    def resolve_batch_id_by_license(self, license):
      raise LicenseNotEnrolledError(
        license=license,
        open_batches=[{"number": "2025/123", "tier": "Sub 14", "gender": "M"}],
      )

  monkeypatch.setattr(server_module, "_get_client", lambda: StubClient())

  result = server_module.read_enrollment(license=999999999)
  assert result == {
    "error": "license_not_enrolled",
    "license": 999999999,
    "open_batches": [{"number": "2025/123", "tier": "Sub 14", "gender": "M"}],
  }


def test_list_batch_enrollments_lists_players(monkeypatch):
  class StubClient:
    def resolve_batch_id(self, number):
      assert number == "2025/123"
      return 42

    def list_player_registration_batch_items(self, batch_id):
      assert batch_id == 42
      return [{"license": 301772, "name": "Player A"}]

  monkeypatch.setattr(server_module, "_get_client", lambda: StubClient())

  result = server_module.list_batch_enrollments(batch_number="2025/123")
  assert result == [{"license": 301772, "name": "Player A"}]


def test_delete_enrollment_removes_player(monkeypatch):
  captured = {"removed": None}

  class _Cache:
    def get_batch_number(self, batch_id):
      return "2025/123"

  class StubClient:
    _cache = _Cache()

    def resolve_batch_id_by_license(self, license):
      return 42

    def remove_player_from_registration_batch(self, batch_id, license):
      captured["removed"] = (batch_id, license)

  monkeypatch.setattr(server_module, "_get_client", lambda: StubClient())

  result = server_module.delete_enrollment(license=301772)
  assert captured["removed"] == (42, 301772)
  assert result == {"removed": True, "license": 301772, "batch_number": "2025/123"}


def test_delete_enrollment_license_not_enrolled_returns_structured_error(monkeypatch):
  from sav_client.exceptions import LicenseNotEnrolledError

  class StubClient:
    def resolve_batch_id_by_license(self, license):
      raise LicenseNotEnrolledError(license=license, open_batches=[])

    def remove_player_from_registration_batch(self, batch_id, license):
      raise AssertionError("must not be called when resolver fails")

  monkeypatch.setattr(server_module, "_get_client", lambda: StubClient())

  result = server_module.delete_enrollment(license=999999999)
  assert result == {
    "error": "license_not_enrolled",
    "license": 999999999,
    "open_batches": [],
  }


def test_delete_batch_deletes_whole_batch(monkeypatch):
  captured = {"deleted_id": None}

  class StubClient:
    def resolve_batch_id(self, number):
      assert number == "2025/999"
      return 99

    def delete_player_registration_batch(self, batch_id):
      captured["deleted_id"] = batch_id

  monkeypatch.setattr(server_module, "_get_client", lambda: StubClient())

  result = server_module.delete_batch(batch_number="2025/999")
  assert captured["deleted_id"] == 99
  assert result == {"deleted": True, "batch_number": "2025/999"}


def test_update_enrollment_drops_batch_number(monkeypatch):
  captured = {}

  class StubClient:
    def resolve_batch_id_by_license(self, license):
      return 42

    def update_player_in_registration_batch(self, batch_id, license, **kwargs):
      captured["call"] = (batch_id, license, kwargs)
      return 77

  monkeypatch.setattr(server_module, "_get_client", lambda: StubClient())

  result = server_module.update_enrollment(license=301772, fields={"email": "x@y.z"})
  assert captured["call"] == (42, 301772, {"email": "x@y.z"})
  assert result == {"success": True, "player_id": 77}


def test_update_enrollment_returns_structured_error_when_not_enrolled(monkeypatch):
  from sav_client.exceptions import LicenseNotEnrolledError

  class StubClient:
    def resolve_batch_id_by_license(self, license):
      raise LicenseNotEnrolledError(
        license=license,
        open_batches=[{"number": "2025/1", "tier": "Sub 14", "gender": "F"}],
      )

    def update_player_in_registration_batch(self, *a, **kw):
      raise AssertionError("must not be called when resolver fails")

  monkeypatch.setattr(server_module, "_get_client", lambda: StubClient())

  result = server_module.update_enrollment(license=999, fields={"email": "x@y.z"})
  assert result["error"] == "license_not_enrolled"
  assert result["license"] == 999
  assert result["open_batches"] == [{"number": "2025/1", "tier": "Sub 14", "gender": "F"}]


def test_parse_enrollment_forms_reads_template_and_skips_ocr(monkeypatch):
  """A Modelo 1 filled from the template is read from its AcroForm — no
  classify, no Document AI OCR — and still yields the enrollment params."""
  import base64

  import sav_parsers
  from sav_shared.fpb_mod1 import render_mod1

  values = {
    "tipo_inscricao": 1, "clube": "CT", "associacao": "AB Lisboa",
    "genero": "Masculino", "escalao": "Senior", "nome": "Joao Adulto",
    "nacionalidade": "Portuguesa", "pais_nascimento": "Portugal",
    "nif": "234567890", "nasc": "1990-01-01", "tipo": 1, "numi": "22334455",
    "dataval": "2030-01-01", "email": "j@x.pt", "tele": "911111111",
    "morada": "Rua A", "localidade_txt": "Lisboa", "codpostal": "1000-001",
    "distrito": "Lisboa", "concelho": "Lisboa",
    "consent_data": True, "consent_communications": True, "consent_marketing": False,
  }
  pdf_b64 = base64.b64encode(render_mod1(values)).decode()

  class StubClient:
    def list_player_registration_tiers(self, gender_id):
      assert gender_id == 1
      return {9: "Sénior"}

    def find_license_by_nif(self, nif, club_id=None):
      raise AssertionError("1ª Inscrição box checked → no NIF lookup")

  monkeypatch.setattr(server_module, "_get_client", lambda: StubClient())

  def _boom(*a, **k):
    raise AssertionError("Document AI must not run for a template-filled mod1")

  monkeypatch.setattr(sav_parsers, "classify", _boom)
  monkeypatch.setattr(sav_parsers, "parse_fpb_mod1", _boom)

  results = server_module.parse_enrollment_forms([pdf_b64])
  assert len(results) == 1
  entry = results[0]
  assert entry["doc_type"] == "fpb_modelo_1"
  assert entry["reg_type"] == 1          # 1ª Inscrição
  assert entry["gender_id"] == 1         # Masculino
  assert entry["tier_name"] == "Sénior"
  # A form-read doc has no Document AI session to close later.
  assert server_module._forms[entry["mod1_id"]]["processing_id"] is None


def test_mcp_tool_signatures_dropped_batch_number():
  """Existing-enrollment MCP tools must no longer accept batch_number."""
  import inspect

  for tool_name in (
    "update_enrollment",
    "update_enrollment_with_document",
    "read_enrollment",
    "delete_enrollment",
    "list_player_documents",
    "upload_player_document",
    "replace_player_document",
  ):
    fn = getattr(server_module, tool_name)
    sig = inspect.signature(fn)
    assert "batch_number" not in sig.parameters, (
      f"{tool_name} still accepts batch_number; "
      f"parameters: {list(sig.parameters)}"
    )


def test_parse_enrollment_forms_parallel_order_and_error_isolation(monkeypatch):
  """Multiple PDFs fan out to worker threads: results keep input order, each
  PDF fails independently, and artifacts land in _forms off the main thread."""
  import base64
  import threading

  import sav_parsers
  from sav_parsers.types import DocType, ParsedField

  classify_threads: list[str] = []

  def fake_classify(tmp_path):
    classify_threads.append(threading.current_thread().name)
    with open(tmp_path, "rb") as f:
      return DocType.EXAME_MEDICO if b"EM" in f.read() else DocType.OUTROS

  def fake_parse_em(tmp_path):
    return {
      "fields": {"exam_date": ParsedField(value="2026-01-01", confidence=0.9)},
      "processing_id": "proc-em",
    }

  monkeypatch.setattr(sav_parsers, "classify", fake_classify)
  monkeypatch.setattr(sav_parsers, "parse_em", fake_parse_em)
  monkeypatch.setattr(sav_parsers, "train_classifier", lambda *a, **k: None)
  # Junk bytes must not trip the mod1 AcroForm probe.
  monkeypatch.setattr(server_module, "_read_mod1_template_fields", lambda b: None)
  monkeypatch.setattr(server_module, "_get_client", lambda: object())
  forms: dict = {}
  monkeypatch.setattr(server_module, "_forms", forms)

  pdfs = [
    base64.b64encode(b"%PDF-1.4\nEM").decode(),   # → exame_medico
    "!!!not-base64!!!",                            # → per-entry error
    base64.b64encode(b"%PDF-1.4\nXX").decode(),   # → unsupported type error
  ]
  results = server_module.parse_enrollment_forms(pdfs)

  assert [r["index"] for r in results] == [0, 1, 2]
  assert results[0]["doc_type"] == "exame_medico"
  assert results[0]["exam_date"] == "2026-01-01"
  assert "error" in results[1] and "base64" in results[1]["error"].lower()
  assert "Unsupported document type" in results[2]["error"]
  # The one successful parse is registered in _forms, sans internal id key.
  artifact = forms[results[0]["medical_exam_id"]]
  assert artifact["pdf_bytes"] == b"%PDF-1.4\nEM"
  assert "artifact_id" not in artifact
  # >1 PDF → the pool path ran: classification happened off the main thread.
  assert classify_threads and all(t != "MainThread" for t in classify_threads)
