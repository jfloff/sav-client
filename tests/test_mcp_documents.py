import base64

from sav_parsers.types import DocType, ParsedField

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


def test_upload_player_document_classifies_when_doc_type_omitted(monkeypatch):
  captured = {}

  class StubClient:
    def upload_player_registration_document(self, batch_id, license, file_path, *, tipo_doc):
      captured["call"] = (batch_id, license, tipo_doc)

  monkeypatch.setattr(server_module, "_get_client", lambda: StubClient())
  monkeypatch.setattr("sav_parsers.classify", lambda pdf: DocType.EM)

  result = server_module.upload_player_document(
    batch_id=12,
    license=301772,
    pdf_base64=_pdf_b64(),
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


def test_replace_player_document_classifies_when_doc_type_omitted(monkeypatch):
  captured = {}

  class StubClient:
    def replace_player_registration_document(self, batch_id, license, file_path, *, tipo_doc):
      captured["call"] = (batch_id, license, tipo_doc)

  monkeypatch.setattr(server_module, "_get_client", lambda: StubClient())
  monkeypatch.setattr("sav_parsers.classify", lambda pdf: DocType.EM)

  result = server_module.replace_player_document(
    batch_id=12,
    license=301772,
    pdf_base64=_pdf_b64(),
  )

  assert result == {"success": True}
  assert captured["call"] == (12, 301772, 2)


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
  assert result[0]["artifact_id"] == result[0]["mod1_id"]
  assert result[0]["doc_type"] == DocType.FPB_MOD1.value
  assert server_module._forms[result[0]["mod1_id"]]["doc_type"] == DocType.FPB_MOD1


def test_parse_enrollment_forms_returns_medical_exam_payload(monkeypatch):
  monkeypatch.setattr(server_module, "_get_client", lambda: object())
  monkeypatch.setattr("sav_parsers.classify", lambda pdf: DocType.EM)
  monkeypatch.setattr(
    "sav_parsers.parse_em",
    lambda pdf: {
      "fields": {
        "exam_date": ParsedField(value="2026-05-01", confidence=0.91),
        "doctor_validation_present": ParsedField(value=True, confidence=0.87),
      },
      "processing_id": "proc-em-1",
    },
  )
  monkeypatch.setattr(server_module, "_forms", {})

  result = server_module.parse_enrollment_forms([_pdf_b64()])

  assert result == [
    {
      "index": 0,
      "artifact_id": result[0]["artifact_id"],
      "medical_exam_id": result[0]["artifact_id"],
      "doc_type": DocType.EM.value,
      "exam_date": "2026-05-01",
      "raw_exam_date": None,
      "exam_date_confidence": 0.91,
      "doctor_validation_present": True,
      "needs_review": False,
    }
  ]
  assert server_module._forms[result[0]["artifact_id"]]["doc_type"] == DocType.EM


def test_parse_enrollment_forms_returns_raw_medical_exam_date_when_unusable(monkeypatch):
  monkeypatch.setattr(server_module, "_get_client", lambda: object())
  monkeypatch.setattr("sav_parsers.classify", lambda pdf: DocType.EM)
  monkeypatch.setattr(
    "sav_parsers.parse_em",
    lambda pdf: {
      "fields": {
        "exam_date": ParsedField(value="13/05/2026", confidence=0.42),
      },
      "processing_id": "proc-em-2",
    },
  )
  monkeypatch.setattr(server_module, "_forms", {})

  result = server_module.parse_enrollment_forms([_pdf_b64()])

  assert result[0]["exam_date"] is None
  assert result[0]["raw_exam_date"] == "13/05/2026"
  assert result[0]["needs_review"] is True


def test_preview_enrollment_includes_medical_exam_payload(monkeypatch):
  result_obj = type(
    "ResultStub",
    (),
    {
      "updated": {},
      "kept": {},
      "needs_review": [],
      "kwargs": {"license": 301772},
    },
  )()

  class StubClient:
    def load_player_profile(self, license):
      return {"nome": "Player A", "nasc": "2000-01-01"}

  monkeypatch.setattr(server_module, "_get_client", lambda: StubClient())
  monkeypatch.setattr("sav_shared.fpb_mod1.reconcile_fpb_mod1", lambda parsed, sav_profile, client=None: result_obj)
  monkeypatch.setattr(
    server_module,
    "_forms",
    {
      "form-1": {
        "parsed": {"nome": "A"},
        "processing_id": "proc-form",
        "doc_type": DocType.FPB_MOD1,
      },
      "exam-1": {
        "parsed": {
          "exam_date": ParsedField(value="2026-05-01", confidence=0.93),
          "doctor_validation_present": ParsedField(value=True, confidence=0.8),
        },
        "processing_id": "proc-em",
        "doc_type": DocType.EM,
      },
    },
  )

  result = server_module.preview_enrollment(
    batch_id=12, license=301772, mod1_id="form-1", medical_exam_id="exam-1",
  )

  assert result["medical_exam"] == {
    "artifact_id": "exam-1",
    "medical_exam_id": "exam-1",
    "doc_type": DocType.EM.value,
    "exam_date": "2026-05-01",
    "raw_exam_date": None,
    "exam_date_confidence": 0.93,
    "doctor_validation_present": True,
    "needs_review": False,
  }


def test_submit_enrollment_returns_source_document_upload_payload(monkeypatch):
  replace_calls: list[int] = []

  class StubClient:
    def add_player_to_registration_batch(self, batch_id, license, **kwargs):
      return 77

    def replace_player_registration_document(self, batch_id, license, file_path, *, tipo_doc):
      replace_calls.append(tipo_doc)

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

  result = server_module.submit_enrollment(
    batch_id=12,
    license=301772,
    mod1_id="form-1",
    field_overrides={"exam_date": "2026-05-01"},
  )

  assert result["success"] is True
  assert replace_calls == [1]
  assert result["source_document_upload"] == {
    "doc_type": DocType.FPB_MOD1.value,
    "status": "ok",
    "error": None,
  }
  assert result["medical_exam_upload"] is None


def test_submit_enrollment_raises_when_exam_date_missing_without_medical_exam(monkeypatch):
  import pytest

  result_obj = type(
    "ResultStub",
    (),
    {
      "kwargs": {"license": 301772, "nome": "A"},
      "needs_review": [],
      "retrain_corrections": {},
    },
  )()

  monkeypatch.setattr(server_module, "_get_client", lambda: object())
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

  with pytest.raises(ValueError, match="Enrollment requires exam_date"):
    server_module.submit_enrollment(batch_id=12, license=301772, mod1_id="form-1")


def test_submit_enrollment_uses_medical_exam_date_and_uploads_exam(monkeypatch):
  captured = {"kwargs": None, "close": []}
  replace_calls: list[int] = []

  class StubClient:
    def add_player_to_registration_batch(self, batch_id, license, **kwargs):
      captured["kwargs"] = kwargs
      return 77

    def replace_player_registration_document(self, batch_id, license, file_path, *, tipo_doc):
      replace_calls.append(tipo_doc)

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
  monkeypatch.setattr(
    "sav_parsers.close_processing",
    lambda processing_id, corrections=None: captured["close"].append((processing_id, corrections)),
  )
  monkeypatch.setattr(
    server_module,
    "_forms",
    {
      "form-1": {
        "reconcile_result": result_obj,
        "processing_id": "proc-form",
        "pdf_bytes": b"%PDF-1.4\n",
        "doc_type": DocType.FPB_MOD1,
        "sav_profile": {"nome": "Player A"},
      },
      "exam-1": {
        "parsed": {"exam_date": ParsedField(value="2026-05-01", confidence=0.91)},
        "processing_id": "proc-em",
        "pdf_bytes": b"%PDF-1.4\n",
        "doc_type": DocType.EM,
      },
    },
  )

  result = server_module.submit_enrollment(
    batch_id=12, license=301772, mod1_id="form-1", medical_exam_id="exam-1",
  )

  assert captured["kwargs"]["exam_date"] == "2026-05-01"
  assert replace_calls == [1, 2]
  assert result["medical_exam_upload"] == {
    "doc_type": DocType.EM.value,
    "status": "ok",
    "error": None,
  }
  assert captured["close"] == [
    ("proc-form", None),
    ("proc-em", None),
  ]


def test_submit_enrollment_manual_exam_override_wins(monkeypatch):
  captured = {"kwargs": None, "close": []}

  class StubClient:
    def add_player_to_registration_batch(self, batch_id, license, **kwargs):
      captured["kwargs"] = kwargs
      return 77

    def replace_player_registration_document(self, batch_id, license, file_path, *, tipo_doc):
      return None

  result_obj = type(
    "ResultStub",
    (),
    {
      "kwargs": {"license": 301772},
      "needs_review": [],
      "retrain_corrections": {},
    },
  )()

  monkeypatch.setattr(server_module, "_get_client", lambda: StubClient())
  monkeypatch.setattr(
    "sav_parsers.close_processing",
    lambda processing_id, corrections=None: captured["close"].append((processing_id, corrections)),
  )
  monkeypatch.setattr(
    server_module,
    "_forms",
    {
      "form-1": {
        "reconcile_result": result_obj,
        "processing_id": "proc-form",
        "pdf_bytes": b"%PDF-1.4\n",
        "doc_type": DocType.FPB_MOD1,
        "sav_profile": {"nome": "Player A"},
      },
      "exam-1": {
        "parsed": {"exam_date": ParsedField(value="2026-05-01", confidence=0.91)},
        "processing_id": "proc-em",
        "pdf_bytes": b"%PDF-1.4\n",
        "doc_type": DocType.EM,
      },
    },
  )

  server_module.submit_enrollment(
    batch_id=12,
    license=301772,
    mod1_id="form-1",
    medical_exam_id="exam-1",
    field_overrides={"exam_date": "2026-05-02"},
  )

  assert captured["kwargs"]["exam_date"] == "2026-05-02"
  assert captured["close"][-1] == ("proc-em", {"exam_date": "2026-05-02"})


def test_submit_enrollment_raises_when_exam_date_missing(monkeypatch):
  import pytest

  result_obj = type(
    "ResultStub",
    (),
    {
      "kwargs": {"license": 301772},
      "needs_review": [],
      "retrain_corrections": {},
    },
  )()

  monkeypatch.setattr(server_module, "_get_client", lambda: object())
  monkeypatch.setattr(
    server_module,
    "_forms",
    {
      "form-1": {
        "reconcile_result": result_obj,
        "processing_id": "proc-form",
        "pdf_bytes": b"%PDF-1.4\n",
        "doc_type": DocType.FPB_MOD1,
        "sav_profile": {"nome": "Player A"},
      },
      "exam-1": {
        "parsed": {"exam_date": ParsedField(value="13/05/2026", confidence=0.30)},
        "processing_id": "proc-em",
        "pdf_bytes": b"%PDF-1.4\n",
        "doc_type": DocType.EM,
      },
    },
  )

  with pytest.raises(ValueError, match="exam_date"):
    server_module.submit_enrollment(
      batch_id=12, license=301772, mod1_id="form-1", medical_exam_id="exam-1",
    )


def test_resolve_player_rejects_non_fpb_mod1_artifact(monkeypatch):
  import pytest

  monkeypatch.setattr(server_module, "_get_client", lambda: object())
  monkeypatch.setattr(
    server_module,
    "_forms",
    {
      "exam-1": {
        "parsed": {},
        "processing_id": "proc-em",
        "doc_type": DocType.EM,
      },
    },
  )

  with pytest.raises(ValueError, match="not an fpb_modelo_1"):
    server_module.resolve_player(batch_id=12, mod1_id="exam-1")


def test_preview_enrollment_rejects_non_fpb_mod1_artifact(monkeypatch):
  import pytest

  monkeypatch.setattr(server_module, "_get_client", lambda: object())
  monkeypatch.setattr(
    server_module,
    "_forms",
    {
      "exam-1": {
        "parsed": {},
        "processing_id": "proc-em",
        "doc_type": DocType.EM,
      },
    },
  )

  with pytest.raises(ValueError, match="not an fpb_modelo_1"):
    server_module.preview_enrollment(batch_id=12, license=301772, mod1_id="exam-1")
