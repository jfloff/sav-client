import base64

from sav_parsers.types import DocType, ParsedField

from sav_mcp import server as server_module


def _pdf_b64() -> str:
  return base64.b64encode(b"%PDF-1.4\n").decode("ascii")


def test_upload_player_document_translates_doc_type(monkeypatch):
  captured = {}

  class StubClient:
    def resolve_batch_id_by_license(self, license):
      return 12

    def upload_player_registration_document(self, batch_id, license, file_path, *, tipo_doc):
      captured["call"] = (batch_id, license, tipo_doc)

  monkeypatch.setattr(server_module, "_get_client", lambda: StubClient())

  result = server_module.upload_player_document(
    license=301772,
    pdf_base64=_pdf_b64(),
    doc_type=DocType.EXAME_MEDICO.value,
  )

  assert result == {"success": True}
  assert captured["call"] == (12, 301772, 2)


def test_upload_player_document_classifies_when_doc_type_omitted(monkeypatch):
  captured = {}

  class StubClient:
    def resolve_batch_id_by_license(self, license):
      return 12

    def upload_player_registration_document(self, batch_id, license, file_path, *, tipo_doc):
      captured["call"] = (batch_id, license, tipo_doc)

  monkeypatch.setattr(server_module, "_get_client", lambda: StubClient())
  monkeypatch.setattr("sav_parsers.classify", lambda pdf: DocType.EXAME_MEDICO)

  result = server_module.upload_player_document(
    license=301772,
    pdf_base64=_pdf_b64(),
  )

  assert result == {"success": True}
  assert captured["call"] == (12, 301772, 2)


def test_replace_player_document_translates_doc_type(monkeypatch):
  captured = {}

  class StubClient:
    def resolve_batch_id_by_license(self, license):
      return 12

    def replace_player_registration_document(self, batch_id, license, file_path, *, tipo_doc):
      captured["call"] = (batch_id, license, tipo_doc)

  monkeypatch.setattr(server_module, "_get_client", lambda: StubClient())

  result = server_module.replace_player_document(
    license=301772,
    pdf_base64=_pdf_b64(),
    doc_type=DocType.FPB_MODELO_4.value,
  )

  assert result == {"success": True}
  assert captured["call"] == (12, 301772, 6)


def test_replace_player_document_classifies_when_doc_type_omitted(monkeypatch):
  captured = {}

  class StubClient:
    def resolve_batch_id_by_license(self, license):
      return 12

    def replace_player_registration_document(self, batch_id, license, file_path, *, tipo_doc):
      captured["call"] = (batch_id, license, tipo_doc)

  monkeypatch.setattr(server_module, "_get_client", lambda: StubClient())
  monkeypatch.setattr("sav_parsers.classify", lambda pdf: DocType.EXAME_MEDICO)

  result = server_module.replace_player_document(
    license=301772,
    pdf_base64=_pdf_b64(),
  )

  assert result == {"success": True}
  assert captured["call"] == (12, 301772, 2)


def test_list_player_documents_returns_parser_doc_types(monkeypatch):
  class StubClient:
    def resolve_batch_id_by_license(self, license):
      return 12

    def list_player_registration_documents(self, batch_id, license):
      return [
        {"doc_id": 1, "tipo_doc": 1},
        {"doc_id": 2, "tipo_doc": 2},
        {"doc_id": 3, "tipo_doc": 6},
        {"doc_id": 4, "tipo_doc": 18},
      ]

  monkeypatch.setattr(server_module, "_get_client", lambda: StubClient())

  result = server_module.list_player_documents(license=301772)

  assert result == [
    {"doc_id": 1, "doc_type": DocType.FPB_MODELO_1.value},
    {"doc_id": 2, "doc_type": DocType.EXAME_MEDICO.value},
    {"doc_id": 3, "doc_type": DocType.FPB_MODELO_4.value},
    {"doc_id": 4, "doc_type": DocType.DOCUMENTO_IDENTIFICACAO.value},
  ]


def test_parse_enrollment_forms_returns_doc_type(monkeypatch):
  class StubClient:
    def list_player_registration_tiers(self, gender_id):
      return {7: "Sub 14"}

  monkeypatch.setattr(server_module, "_get_client", lambda: StubClient())
  monkeypatch.setattr("sav_parsers.classify", lambda pdf: DocType.FPB_MODELO_1)
  monkeypatch.setattr(
    "sav_parsers.parse_fpb_mod1",
    lambda pdf: {"fields": {"nome": "A"}, "processing_id": "proc-1"},
  )
  monkeypatch.setattr(server_module, "derive_enrollment_params", lambda parsed, client: (2, 7, 1))
  monkeypatch.setattr(server_module, "_forms", {})

  result = server_module.parse_enrollment_forms([_pdf_b64()])

  assert len(result) == 1
  assert result[0]["artifact_id"] == result[0]["mod1_id"]
  assert result[0]["doc_type"] == DocType.FPB_MODELO_1.value
  assert server_module._forms[result[0]["mod1_id"]]["doc_type"] == DocType.FPB_MODELO_1


def test_parse_enrollment_forms_returns_medical_exam_payload(monkeypatch):
  monkeypatch.setattr(server_module, "_get_client", lambda: object())
  monkeypatch.setattr("sav_parsers.classify", lambda pdf: DocType.EXAME_MEDICO)
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
      "doc_type": DocType.EXAME_MEDICO.value,
      "exam_date": "2026-05-01",
      "raw_exam_date": None,
      "exam_date_confidence": 0.91,
      "doctor_validation_present": True,
      "needs_review": False,
    }
  ]
  assert server_module._forms[result[0]["artifact_id"]]["doc_type"] == DocType.EXAME_MEDICO


def test_parse_enrollment_forms_returns_raw_medical_exam_date_when_unusable(monkeypatch):
  monkeypatch.setattr(server_module, "_get_client", lambda: object())
  monkeypatch.setattr("sav_parsers.classify", lambda pdf: DocType.EXAME_MEDICO)
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
    def resolve_batch_id(self, number):
      return int(number)

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
        "doc_type": DocType.FPB_MODELO_1,
      },
      "exam-1": {
        "parsed": {
          "exam_date": ParsedField(value="2026-05-01", confidence=0.93),
          "doctor_validation_present": ParsedField(value=True, confidence=0.8),
        },
        "processing_id": "proc-em",
        "doc_type": DocType.EXAME_MEDICO,
      },
    },
  )

  result = server_module.preview_enrollment(
    batch_number="12", license=301772, mod1_id="form-1", medical_exam_id="exam-1",
  )

  assert result["medical_exam"] == {
    "artifact_id": "exam-1",
    "medical_exam_id": "exam-1",
    "doc_type": DocType.EXAME_MEDICO.value,
    "exam_date": "2026-05-01",
    "raw_exam_date": None,
    "exam_date_confidence": 0.93,
    "doctor_validation_present": True,
    "needs_review": False,
  }


def test_submit_enrollment_returns_source_document_upload_payload(monkeypatch):
  replace_calls: list[int] = []

  class StubClient:
    def resolve_batch_id(self, number):
      return int(number)

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
        "doc_type": DocType.FPB_MODELO_1,
        "sav_profile": {"nome": "Player A"},
      }
    },
  )

  result = server_module.submit_enrollment(
    batch_number="12",
    license=301772,
    mod1_id="form-1",
    field_overrides={"exam_date": "2026-05-01"},
  )

  assert result["success"] is True
  assert replace_calls == [1]
  assert result["source_document_upload"] == {
    "doc_type": DocType.FPB_MODELO_1.value,
    "status": "ok",
    "error": None,
    "has_club_stamp": None,
    "stamp_warning": None,
    "has_inscricao_mark": None,
    "inscricao_warning": None,
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
        "doc_type": DocType.FPB_MODELO_1,
        "sav_profile": {"nome": "Player A"},
      }
    },
  )

  with pytest.raises(ValueError, match="Enrollment requires exam_date"):
    server_module.submit_enrollment(batch_number="12", license=301772, mod1_id="form-1")


def test_submit_enrollment_uses_medical_exam_date_and_uploads_exam(monkeypatch):
  captured = {"kwargs": None, "close": []}
  replace_calls: list[int] = []

  class StubClient:
    def resolve_batch_id(self, number):
      return int(number)

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
        "doc_type": DocType.FPB_MODELO_1,
        "sav_profile": {"nome": "Player A"},
      },
      "exam-1": {
        "parsed": {"exam_date": ParsedField(value="2026-05-01", confidence=0.91)},
        "processing_id": "proc-em",
        "pdf_bytes": b"%PDF-1.4\n",
        "doc_type": DocType.EXAME_MEDICO,
      },
    },
  )

  result = server_module.submit_enrollment(
    batch_number="12", license=301772, mod1_id="form-1", medical_exam_id="exam-1",
  )

  assert captured["kwargs"]["exam_date"] == "2026-05-01"
  assert replace_calls == [1, 2]
  assert result["medical_exam_upload"] == {
    "doc_type": DocType.EXAME_MEDICO.value,
    "status": "ok",
    "error": None,
    "has_club_stamp": None,
    "stamp_warning": None,
    "has_inscricao_mark": None,
    "inscricao_warning": None,
  }
  assert captured["close"] == [
    ("proc-form", None),
    ("proc-em", None),
  ]


def test_submit_enrollment_manual_exam_override_wins(monkeypatch):
  captured = {"kwargs": None, "close": []}

  class StubClient:
    def resolve_batch_id(self, number):
      return int(number)

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
        "doc_type": DocType.FPB_MODELO_1,
        "sav_profile": {"nome": "Player A"},
      },
      "exam-1": {
        "parsed": {"exam_date": ParsedField(value="2026-05-01", confidence=0.91)},
        "processing_id": "proc-em",
        "pdf_bytes": b"%PDF-1.4\n",
        "doc_type": DocType.EXAME_MEDICO,
      },
    },
  )

  server_module.submit_enrollment(
    batch_number="12",
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
        "doc_type": DocType.FPB_MODELO_1,
        "sav_profile": {"nome": "Player A"},
      },
      "exam-1": {
        "parsed": {"exam_date": ParsedField(value="13/05/2026", confidence=0.30)},
        "processing_id": "proc-em",
        "pdf_bytes": b"%PDF-1.4\n",
        "doc_type": DocType.EXAME_MEDICO,
      },
    },
  )

  with pytest.raises(ValueError, match="exam_date"):
    server_module.submit_enrollment(
      batch_number="12", license=301772, mod1_id="form-1", medical_exam_id="exam-1",
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
        "doc_type": DocType.EXAME_MEDICO,
      },
    },
  )

  with pytest.raises(ValueError, match="not an fpb_modelo_1"):
    server_module.resolve_player(batch_number="12", mod1_id="exam-1")


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
        "doc_type": DocType.EXAME_MEDICO,
      },
    },
  )

  with pytest.raises(ValueError, match="not an fpb_modelo_1"):
    server_module.preview_enrollment(batch_number="12", license=301772, mod1_id="exam-1")


# ─── subida de escalão (mod4) ────────────────────────────────────────────────

def test_parse_enrollment_forms_returns_mod4_id(monkeypatch):
  from sav_parsers.types import ParsedField
  monkeypatch.setattr(server_module, "_get_client", lambda: object())
  monkeypatch.setattr("sav_parsers.classify", lambda pdf: DocType.FPB_MODELO_4)
  monkeypatch.setattr(
    "sav_parsers.parse_fpb_mod4",
    lambda pdf: {
      "fields": {
        "licenca_nr": ParsedField(value="301772", confidence=0.95),
        "nome_jogador": ParsedField(value="Player A", confidence=0.92),
        "escalao_subida": ParsedField(value="Sub 14", confidence=0.95),
        "escalao_actual": ParsedField(value="Mini 12", confidence=0.90),
      },
      "processing_id": "proc-mod4",
    },
  )
  monkeypatch.setattr(server_module, "_forms", {})

  result = server_module.parse_enrollment_forms([_pdf_b64()])

  assert result == [
    {
      "index": 0,
      "artifact_id": result[0]["artifact_id"],
      "mod4_id": result[0]["artifact_id"],
      "doc_type": DocType.FPB_MODELO_4.value,
      "nome_jogador": "Player A",
      "licenca_nr": "301772",
      "escalao_actual": "Mini 12",
      "escalao_subida": "Sub 14",
    }
  ]
  artifact = server_module._forms[result[0]["mod4_id"]]
  assert artifact["doc_type"] == DocType.FPB_MODELO_4
  assert artifact["pdf_bytes"] == b"%PDF-1.4\n"
  assert artifact["parsed"]["nome_jogador"].value == "Player A"
  assert artifact["processing_id"] == "proc-mod4"


def _submit_stub_forms(result_obj, *, with_mod4: bool) -> dict:
  forms = {
    "form-1": {
      "reconcile_result": result_obj,
      "processing_id": "proc-1",
      "pdf_bytes": b"%PDF-1.4\n",
      "doc_type": DocType.FPB_MODELO_1,
      "sav_profile": {"nome": "Player A"},
      "previewed": True,
    },
  }
  if with_mod4:
    forms["mod4-1"] = {
      "parsed": {},
      "processing_id": None,
      "pdf_bytes": b"%PDF-1.4\n",
      "doc_type": DocType.FPB_MODELO_4,
    }
  return forms


def test_submit_enrollment_with_mod4_marks_subida(monkeypatch):
  captured: dict = {"uploads": []}

  class StubClient:
    def resolve_batch_id(self, number):
      return int(number)

    def add_player_to_registration_batch(self, batch_id, license, **kwargs):
      captured["kwargs"] = kwargs
      return 77

    def replace_player_registration_document(self, batch_id, license, file_path, *, tipo_doc):
      captured["uploads"].append(tipo_doc)

  result_obj = type("ResultStub", (), {
    "kwargs": {"license": 301772, "nome": "A"},
    "needs_review": [],
    "retrain_corrections": {},
  })()

  monkeypatch.setattr(server_module, "_get_client", lambda: StubClient())
  monkeypatch.setattr("sav_parsers.close_processing", lambda processing_id, corrections=None: None)
  monkeypatch.setattr(server_module, "_forms", _submit_stub_forms(result_obj, with_mod4=True))

  result = server_module.submit_enrollment(
    batch_number="12",
    license=301772,
    mod1_id="form-1",
    mod4_id="mod4-1",
    field_overrides={"exam_date": "2026-05-01"},
  )

  assert result["success"] is True
  assert captured["kwargs"].get("inline_subida") is True
  assert result["inline_subida"] is True
  # mod1 (tipo_doc=1) and mod4 (tipo_doc=6) both uploaded.
  assert 6 in captured["uploads"]
  assert result["subida_document_upload"]["status"] == "ok"


def test_submit_enrollment_without_mod4_is_not_subida(monkeypatch):
  captured: dict = {}

  class StubClient:
    def resolve_batch_id(self, number):
      return int(number)

    def add_player_to_registration_batch(self, batch_id, license, **kwargs):
      captured["kwargs"] = kwargs
      return 77

    def replace_player_registration_document(self, batch_id, license, file_path, *, tipo_doc):
      pass

  result_obj = type("ResultStub", (), {
    "kwargs": {"license": 301772, "nome": "A"},
    "needs_review": [],
    "retrain_corrections": {},
  })()

  monkeypatch.setattr(server_module, "_get_client", lambda: StubClient())
  monkeypatch.setattr("sav_parsers.close_processing", lambda processing_id, corrections=None: None)
  monkeypatch.setattr(server_module, "_forms", _submit_stub_forms(result_obj, with_mod4=False))

  result = server_module.submit_enrollment(
    batch_number="12",
    license=301772,
    mod1_id="form-1",
    field_overrides={"exam_date": "2026-05-01"},
  )

  assert captured["kwargs"].get("inline_subida") is False
  assert result["inline_subida"] is False
  assert result["subida_document_upload"] is None


def test_submit_enrollment_rejects_non_mod4_artifact(monkeypatch):
  import pytest

  result_obj = type("ResultStub", (), {
    "kwargs": {"license": 301772},
    "needs_review": [],
    "retrain_corrections": {},
  })()

  monkeypatch.setattr(server_module, "_get_client", lambda: object())
  forms = _submit_stub_forms(result_obj, with_mod4=False)
  # Point mod4_id at the exam/mod1 artifact to trigger the type guard.
  monkeypatch.setattr(server_module, "_forms", forms)

  with pytest.raises(ValueError, match="not an fpb_modelo_4"):
    server_module.submit_enrollment(
      batch_number="12",
      license=301772,
      mod1_id="form-1",
      mod4_id="form-1",
      field_overrides={"exam_date": "2026-05-01"},
    )


def test_submit_enrollment_rejects_inline_subida_on_type4(monkeypatch):
  """The XOR guardrail: a mod1 form already routed to reg_type 4 (standalone
  Subida) can't also carry an inline mod4 rider."""
  import pytest

  result_obj = type("ResultStub", (), {
    "kwargs": {"license": 301772},
    "needs_review": [],
    "retrain_corrections": {},
  })()

  monkeypatch.setattr(server_module, "_get_client", lambda: object())
  forms = _submit_stub_forms(result_obj, with_mod4=True)
  forms["form-1"]["reg_type"] = 4
  monkeypatch.setattr(server_module, "_forms", forms)

  with pytest.raises(ValueError, match="inline_subida is only valid"):
    server_module.submit_enrollment(
      batch_number="12",
      license=301772,
      mod1_id="form-1",
      mod4_id="mod4-1",
      field_overrides={"exam_date": "2026-05-01"},
    )


def test_submit_enrollment_subida_no_tier_error_propagates(monkeypatch):
  """A non-guardian SavConfigError (e.g. no subida tier) is re-raised, not
  swallowed as a missing-guardian retry."""
  import pytest

  from sav_client.exceptions import SavConfigError

  class StubClient:
    def resolve_batch_id(self, number):
      return int(number)

    def add_player_to_registration_batch(self, batch_id, license, **kwargs):
      raise SavConfigError("Subida requested but SAV offers no subida tier")

  result_obj = type("ResultStub", (), {
    "kwargs": {"license": 301772},
    "needs_review": [],
    "retrain_corrections": {},
  })()

  monkeypatch.setattr(server_module, "_get_client", lambda: StubClient())
  monkeypatch.setattr(server_module, "_forms", _submit_stub_forms(result_obj, with_mod4=True))

  with pytest.raises(SavConfigError, match="no subida tier"):
    server_module.submit_enrollment(
      batch_number="12",
      license=301772,
      mod1_id="form-1",
      mod4_id="mod4-1",
      field_overrides={"exam_date": "2026-05-01"},
    )


# ─── 1ª Inscrição (type-1) MCP paths ────────────────────────────────────────

def _type1_batch_stub():
  """Live-account-shape stub of a type-1 batch returned by
  list_player_registration_batches."""
  return type("BatchStub", (), {
    "id": 629084, "number": "726", "type_id": 1, "is_open": True,
    "tier": "Baby-Basket", "gender": "Masculino",
    "tier_id": 33, "gender_id": 1, "season": "2025/2026",
    "club_id": 2430,
  })()


def test_resolve_player_type1_returns_new_player_when_no_duplicate(monkeypatch):
  """For a type-1 batch, resolve_player skips the eligibility list and the
  duplicate check passes — it should return resolved=true with license=null."""
  from sav_parsers.types import ParsedField

  batch = _type1_batch_stub()

  class StubClient:
    def list_player_registration_batches(self, season=None):
      return [batch]

    def _check_primeira_player_duplicate(self, *, gender_id, birth_date, id_number):
      return {"existe": 0}

  monkeypatch.setattr(server_module, "_get_client", lambda: StubClient())
  monkeypatch.setattr(server_module, "_forms", {
    "form-1": {
      "doc_type": DocType.FPB_MODELO_1,
      "parsed": {
        "nome_completo": ParsedField(value="João Loff", confidence=0.95),
        "data_nascimento": ParsedField(value="2020-09-26", confidence=0.95),
        "num_doc_identificacao": ParsedField(value="12345699", confidence=0.95),
        "genero_masculino": ParsedField(value=True, confidence=0.95),
      },
      "reg_type": 1,
    },
  })

  result = server_module.resolve_player(batch_number="726", mod1_id="form-1")
  assert result["resolved"] is True
  assert result["license"] is None
  assert result["reg_type"] == 1
  assert result["ocr_name"] == "João Loff"
  assert result["ocr_birth_date"] == "2020-09-26"
  assert result["ocr_gender_id"] == 1


def test_resolve_player_type1_short_circuits_on_duplicate(monkeypatch):
  """When op=11 says the player already exists, resolve_player must reject
  the 1ª Inscrição path with a structured error rather than proceeding."""
  from sav_parsers.types import ParsedField

  batch = _type1_batch_stub()

  class StubClient:
    def list_player_registration_batches(self, season=None):
      return [batch]

    def _check_primeira_player_duplicate(self, *, gender_id, birth_date, id_number):
      return {"existe": 1, "id": 99}

  monkeypatch.setattr(server_module, "_get_client", lambda: StubClient())
  monkeypatch.setattr(server_module, "_forms", {
    "form-1": {
      "doc_type": DocType.FPB_MODELO_1,
      "parsed": {
        "nome_completo": ParsedField(value="João Loff", confidence=0.95),
        "data_nascimento": ParsedField(value="2020-09-26", confidence=0.95),
        "num_doc_identificacao": ParsedField(value="12345699", confidence=0.95),
      },
      "reg_type": 1,
    },
  })

  result = server_module.resolve_player(batch_number="726", mod1_id="form-1")
  assert result["resolved"] is False
  assert result["error"] == "player_already_in_sav"
  assert result["existing_sav_id"] == 99


def test_submit_enrollment_type1_dispatches_via_primeira_kwargs(monkeypatch):
  """Type-1 submit must pass the OCR-derived demographics (name, birth_date,
  gender_id, nif, …) instead of a reconcile result. It must also look up the
  newly-assigned licence in the batch listing so the source PDF upload
  targets the right player."""
  captured: dict = {}

  class StubClient:
    def resolve_batch_id(self, number):
      return 629084

    def add_player_to_registration_batch(self, batch_id, license, **kwargs):
      captured["kwargs"] = kwargs
      captured["license_arg"] = license
      return 277534  # the SAV userid

    def list_player_registration_batch_items(self, batch_id):
      return [{"license": 321160, "name": "João Loff"}]

    def replace_player_registration_document(self, batch_id, license, file_path, *, tipo_doc):
      captured.setdefault("uploads", []).append((license, tipo_doc))

  primeira_kwargs = {
    "name": "João Loff", "birth_date": "2020-09-26", "gender_id": 1,
    "nif": "277544319", "id_type": 1, "id_number": "12345699",
    "id_expiry": "2029-09-26", "email": "x@y.pt",
    "morada": "Praceta", "cod_postal": "1300-536",
    "distrito_id": 1, "concelho_id": 5,
  }

  monkeypatch.setattr(server_module, "_get_client", lambda: StubClient())
  monkeypatch.setattr("sav_parsers.close_processing", lambda processing_id, corrections=None: None)
  monkeypatch.setattr(server_module, "_forms", {
    "form-1": {
      "doc_type": DocType.FPB_MODELO_1,
      "reg_type": 1,
      "parsed": {},
      "primeira_kwargs": primeira_kwargs,
      "primeira_concelhos": {5: "Aveiro"},
      "previewed": True,
      "processing_id": "proc-1",
      "pdf_bytes": b"%PDF-1.4\n",
    },
  })

  result = server_module.submit_enrollment(
    batch_number="726",
    license=None,
    mod1_id="form-1",
    field_overrides={"exam_date": "2025-09-26"},
  )

  assert result["success"] is True
  assert result["license"] == 321160  # looked up from the batch listing
  assert result["name"] == "João Loff"
  # The wizard got the licence kwarg as 0 (sentinel for "no licence yet").
  assert captured["license_arg"] == 0
  # Demographics flowed through.
  assert captured["kwargs"]["name"] == "João Loff"
  assert captured["kwargs"]["nif"] == "277544319"
  assert captured["kwargs"]["gender_id"] == 1
  # Upload targeted the new licence (not 0).
  assert captured["uploads"] == [(321160, 1)]


def test_submit_enrollment_type1_skips_upload_when_licence_lookup_fails(monkeypatch):
  """If the post-commit batch listing doesn't contain a row matching the
  supplied name, the upload is skipped with a clear status rather than
  silently uploading against licence=0."""

  class StubClient:
    def resolve_batch_id(self, number):
      return 629084

    def add_player_to_registration_batch(self, batch_id, license, **kwargs):
      return 277534

    def list_player_registration_batch_items(self, batch_id):
      return [{"license": 999, "name": "Someone Else"}]

    def replace_player_registration_document(self, *a, **k):
      raise AssertionError("upload must not be attempted when licence lookup fails")

  monkeypatch.setattr(server_module, "_get_client", lambda: StubClient())
  monkeypatch.setattr("sav_parsers.close_processing", lambda processing_id, corrections=None: None)
  monkeypatch.setattr(server_module, "_forms", {
    "form-1": {
      "doc_type": DocType.FPB_MODELO_1,
      "reg_type": 1,
      "parsed": {},
      "primeira_kwargs": {
        "name": "João Loff", "birth_date": "2020-09-26", "gender_id": 1,
        "nif": "277544319", "id_type": 1, "id_number": "12345699",
        "id_expiry": "2029-09-26", "email": "x@y.pt",
        "morada": "x", "cod_postal": "1000-000",
        "distrito_id": 1, "concelho_id": 5,
      },
      "previewed": True,
      "processing_id": "proc-1",
      "pdf_bytes": b"%PDF-1.4\n",
    },
  })

  result = server_module.submit_enrollment(
    batch_number="726", license=None, mod1_id="form-1",
    field_overrides={"exam_date": "2025-09-26"},
  )

  assert result["success"] is True
  assert result["license"] is None
  assert result["source_document_upload"]["status"] == "skipped"
  assert "type-1 commit" in result["source_document_upload"]["error"]
