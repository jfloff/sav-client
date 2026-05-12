import base64

from sav_parsers.types import DocType

from sav_mcp import server as server_module


def _pdf_b64() -> str:
  return base64.b64encode(b"%PDF-1.4\n").decode("ascii")


def test_upload_player_document_translates_doc_type(monkeypatch):
  captured = {}

  class StubClient:
    def upload_player_registration_document(self, batch_id, license, file_path, *, tipo_doc):
      captured["call"] = (batch_id, license, tipo_doc)

  monkeypatch.setattr(server_module, "_get_client", lambda: StubClient())

  result = server_module.upload_player_document(
    batch_id=12,
    license=301772,
    pdf_base64=_pdf_b64(),
    doc_type=DocType.EM.value,
  )

  assert result == {"success": True}
  assert captured["call"] == (12, 301772, 2)


def test_replace_player_document_translates_doc_type(monkeypatch):
  captured = {}

  class StubClient:
    def replace_player_registration_document(self, batch_id, license, file_path, *, tipo_doc):
      captured["call"] = (batch_id, license, tipo_doc)

  monkeypatch.setattr(server_module, "_get_client", lambda: StubClient())

  result = server_module.replace_player_document(
    batch_id=12,
    license=301772,
    pdf_base64=_pdf_b64(),
    doc_type=DocType.FPB_MOD4.value,
  )

  assert result == {"success": True}
  assert captured["call"] == (12, 301772, 6)


def test_list_player_documents_returns_parser_doc_types(monkeypatch):
  class StubClient:
    def list_player_registration_documents(self, batch_id, license):
      return [
        {"doc_id": 1, "tipo_doc": 1},
        {"doc_id": 2, "tipo_doc": 2},
        {"doc_id": 3, "tipo_doc": 6},
        {"doc_id": 4, "tipo_doc": 18},
      ]

  monkeypatch.setattr(server_module, "_get_client", lambda: StubClient())

  result = server_module.list_player_documents(batch_id=12, license=301772)

  assert result == [
    {"doc_id": 1, "doc_type": DocType.FPB_MOD1.value},
    {"doc_id": 2, "doc_type": DocType.EM.value},
    {"doc_id": 3, "doc_type": DocType.FPB_MOD4.value},
    {"doc_id": 4, "doc_type": None},
  ]


def test_parse_enrollment_forms_returns_doc_type(monkeypatch):
  class StubClient:
    def list_player_registration_tiers(self, gender_id):
      return {7: "Sub 14"}

  monkeypatch.setattr(server_module, "_get_client", lambda: StubClient())
  monkeypatch.setattr("sav_parsers.classify", lambda pdf: DocType.FPB_MOD1)
  monkeypatch.setattr(
    "sav_parsers.parse_fpb_mod1",
    lambda pdf: {"fields": {"nome": "A"}, "processing_id": "proc-1"},
  )
  monkeypatch.setattr(server_module, "derive_enrollment_params", lambda parsed, client: (2, 7, 1))
  monkeypatch.setattr(server_module, "_forms", {})

  result = server_module.parse_enrollment_forms([_pdf_b64()])

  assert len(result) == 1
  assert result[0]["doc_type"] == DocType.FPB_MOD1.value
  assert server_module._forms[result[0]["form_id"]]["doc_type"] == DocType.FPB_MOD1


def test_submit_enrollment_returns_source_document_upload_payload(monkeypatch):
  class StubClient:
    def add_player_to_registration_batch(self, batch_id, license, **kwargs):
      return 77

    def replace_player_registration_document(self, batch_id, license, file_path, *, tipo_doc):
      assert tipo_doc == 1

  result_obj = type(
    "ResultStub",
    (),
    {
      "kwargs": {"license": 301772, "nome": "A"},
      "needs_review": [],
      "retrain_corrections": {},
    },
  )()

  monkeypatch.setattr(server_module, "_get_client", lambda: StubClient())
  monkeypatch.setattr("sav_parsers.close_processing", lambda processing_id, corrections=None: None)
  monkeypatch.setattr(
    server_module,
    "_forms",
    {
      "form-1": {
        "reconcile_result": result_obj,
        "processing_id": "proc-1",
        "pdf_bytes": b"%PDF-1.4\n",
        "doc_type": DocType.FPB_MOD1,
        "sav_profile": {"nome": "Player A"},
      }
    },
  )

  result = server_module.submit_enrollment(batch_id=12, license=301772, form_id="form-1")

  assert result["success"] is True
  assert result["source_document_upload"] == {
    "doc_type": DocType.FPB_MOD1.value,
    "status": "ok",
    "error": None,
  }
