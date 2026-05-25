import pytest
from click.testing import CliRunner
from sav_parsers.types import DocType, ParsedField

from sav_cli import cli as cli_module


def _write_pdf(tmp_path):
  pdf_path = tmp_path / "sample.pdf"
  pdf_path.write_bytes(b"%PDF-1.4\n")
  return pdf_path


@pytest.fixture
def batch_stub():
  return type("BatchStub", (), {"id": 12, "number": "2025/12", "club_id": 99})()


@pytest.fixture
def reconcile_result_stub():
  return type(
    "ResultStub",
    (),
    {
      "kwargs": {"license": 301772},
      "updated": {},
      "kept": {},
      "needs_review": [],
      "retrain_corrections": {},
      "ocr": {},
      "concelhos": {},
    },
  )()


def test_enrollment_update_rejects_legacy_tipo_alias(monkeypatch, tmp_path):
  pdf_path = _write_pdf(tmp_path)

  def fail_make_client():
    raise AssertionError("_make_client should not run for invalid --tipo values")

  monkeypatch.setattr(cli_module, "_make_client", fail_make_client)

  runner = CliRunner()
  result = runner.invoke(
    cli_module.cli,
    ["enrollment", "update", "--license", "301772", str(pdf_path), "--tipo", "modelo1"],
  )

  assert result.exit_code != 0
  assert "Unknown doc_type 'modelo1'" in result.output


def test_enrollment_update_rejects_raw_tipo_integer(monkeypatch, tmp_path):
  pdf_path = _write_pdf(tmp_path)

  def fail_make_client():
    raise AssertionError("_make_client should not run for invalid --tipo values")

  monkeypatch.setattr(cli_module, "_make_client", fail_make_client)

  runner = CliRunner()
  result = runner.invoke(
    cli_module.cli,
    ["enrollment", "update", "--license", "301772", str(pdf_path), "--tipo", "1"],
  )

  assert result.exit_code != 0
  assert "Unknown doc_type '1'" in result.output


def test_enrollment_update_maps_parser_tipo_names_for_file_replace(monkeypatch, tmp_path):
  pdf_path = _write_pdf(tmp_path)
  captured: list[int] = []

  class StubClient:
    def resolve_batch_id_by_license(self, license):
      return 12

    def replace_player_registration_document(self, batch_id, license, pdf, *, tipo_doc):
      captured.append(tipo_doc)

  monkeypatch.setattr(cli_module, "_make_client", lambda: StubClient())

  runner = CliRunner()
  result = runner.invoke(
    cli_module.cli,
    [
      "enrollment", "update", "--license", "301772", str(pdf_path),
      "--tipo", "exame_medico",
    ],
  )

  assert result.exit_code == 0
  assert captured == [2]
  assert "Uploaded exame_medico" in result.output


def test_enrollment_update_classifies_exam_for_file_replace(monkeypatch, tmp_path):
  pdf_path = _write_pdf(tmp_path)
  captured: list[int] = []

  class StubClient:
    def resolve_batch_id_by_license(self, license):
      return 12

    def replace_player_registration_document(self, batch_id, license, pdf, *, tipo_doc):
      captured.append(tipo_doc)

  monkeypatch.setattr(cli_module, "_make_client", lambda: StubClient())
  monkeypatch.setattr("sav_parsers.classify", lambda pdf: DocType.EXAME_MEDICO)
  monkeypatch.setattr("sav_parsers.train_classifier", lambda pdf, dt: None)

  runner = CliRunner()
  result = runner.invoke(
    cli_module.cli,
    ["enrollment", "update", "--license", "301772", str(pdf_path)],
  )

  assert result.exit_code == 0, result.output
  assert captured == [2]
  assert f"Classified {pdf_path.name} as exame_medico" in result.output
  assert "Uploaded exame_medico" in result.output


def test_enrollment_update_uploads_classified_outros_without_reconcile(monkeypatch, tmp_path):
  """A PDF auto-classified as OUTROS (tipo_doc=22) is uploaded directly, no OCR/reconcile."""
  pdf_path = _write_pdf(tmp_path)
  uploaded = []

  class StubClient:
    def resolve_batch_id_by_license(self, license):
      return 12

    def replace_player_registration_document(self, batch_id, license, pdf, *, tipo_doc):
      uploaded.append(tipo_doc)

  monkeypatch.setattr(cli_module, "_make_client", lambda: StubClient())
  monkeypatch.setattr("sav_parsers.classify", lambda pdf: DocType.OUTROS)
  monkeypatch.setattr("sav_parsers.train_classifier", lambda pdf, dt: None)

  runner = CliRunner()
  result = runner.invoke(
    cli_module.cli,
    ["enrollment", "update", "--license", "301772", str(pdf_path)],
  )

  assert result.exit_code == 0, result.output
  assert uploaded, "document should have been uploaded"
  assert 22 in uploaded  # tipo_doc=22 for OUTROS


def test_enrollment_update_non_mod1_uploads_without_reconcile(monkeypatch, tmp_path):
  """Non-fpb_modelo_1 doc types (e.g. exame_medico) upload directly, bypassing OCR/reconcile."""
  pdf_path = _write_pdf(tmp_path)
  parse_called = []
  uploaded = []

  class StubClient:
    def resolve_batch_id_by_license(self, license):
      return 12

    def replace_player_registration_document(self, batch_id, license, pdf, *, tipo_doc):
      uploaded.append(tipo_doc)

  monkeypatch.setattr(cli_module, "_make_client", lambda: StubClient())
  monkeypatch.setattr(
    "sav_parsers.parse_fpb_mod1",
    lambda pdf: parse_called.append(1) or {"fields": {}, "processing_id": "x"},
  )
  monkeypatch.setattr("sav_parsers.train_classifier", lambda pdf, dt: None)

  runner = CliRunner()
  result = runner.invoke(
    cli_module.cli,
    ["enrollment", "update", "--license", "301772", str(pdf_path), "--tipo", "exame_medico"],
  )

  assert result.exit_code == 0, result.output
  assert not parse_called, "parse_fpb_mod1 must not be called for non-mod1 doc types"
  assert 2 in uploaded  # tipo_doc=2 for exame_medico


def test_enrollment_create_rejects_medical_exam_without_pdf(tmp_path):
  exam_path = tmp_path / "exam.pdf"
  exam_path.write_bytes(b"%PDF-1.4\n")

  runner = CliRunner()
  result = runner.invoke(
    cli_module.cli,
    [
      "enrollment", "create",
      "--batch", "12", "--license", "301772",
      "--medical-exam", str(exam_path),
    ],
  )

  assert result.exit_code != 0
  assert "--medical-exam requires a PDF or --mod1" in result.output


def test_enroll_uses_medical_exam_date_and_uploads_exam(monkeypatch, tmp_path, batch_stub, reconcile_result_stub):
  form_path = tmp_path / "form.pdf"
  form_path.write_bytes(b"%PDF-1.4\n")
  exam_path = tmp_path / "exam.pdf"
  exam_path.write_bytes(b"%PDF-1.4\n")

  captured = {"kwargs": None, "uploads": [], "closed": [], "trained": []}

  class StubClient:
    def load_player_profile(self, license, club_id=None):
      return {"nome": "Player A"}

    def add_player_to_registration_batch(self, batch_id, license, **kwargs):
      captured["kwargs"] = kwargs
      return 77

    def replace_player_registration_document(self, batch_id, license, pdf, *, tipo_doc):
      captured["uploads"].append(tipo_doc)

  monkeypatch.setattr(cli_module, "_make_client", lambda: StubClient())
  monkeypatch.setattr(cli_module, "_resolve_enroll_batch", lambda client, reg_type, tier_id, gender_id, *, indent="": (12, batch_stub))
  monkeypatch.setattr(cli_module, "_resolve_enroll_player", lambda client, batch_obj, parsed, reg_type=None, *, indent="": (301772, batch_obj))
  monkeypatch.setattr(cli_module, "_confirm_documents_and_submit", lambda *a, **kw: True)
  monkeypatch.setattr(
    "sav_parsers.classify",
    lambda pdf: DocType.EXAME_MEDICO if str(pdf).endswith("exam.pdf") else DocType.FPB_MODELO_1,
  )
  monkeypatch.setattr(
    "sav_parsers.parse_fpb_mod1",
    lambda pdf: {"fields": {"nome": "A"}, "processing_id": "proc-form"},
  )
  monkeypatch.setattr(
    "sav_parsers.parse_em",
    lambda pdf: {
      "fields": {"exam_date": ParsedField(value="2026-05-01", confidence=0.92)},
      "processing_id": "proc-em",
    },
  )
  monkeypatch.setattr(
    "sav_parsers.close_processing",
    lambda processing_id, corrections=None: captured["closed"].append((processing_id, corrections)),
  )
  monkeypatch.setattr(
    "sav_parsers.train_classifier",
    lambda pdf, expected_doc_type: captured["trained"].append((str(pdf), expected_doc_type)),
  )
  monkeypatch.setattr(cli_module, "derive_enrollment_params", lambda parsed, client: (2, 7, 1))
  monkeypatch.setattr(cli_module, "reconcile_fpb_mod1", lambda parsed, sav_profile, client=None: reconcile_result_stub)

  runner = CliRunner()
  result = runner.invoke(
    cli_module.cli,
    ["enrollment", "create", str(form_path), "--medical-exam", str(exam_path)],
  )

  assert result.exit_code == 0, result.output
  assert captured["kwargs"]["exam_date"] == "2026-05-01"
  assert captured["uploads"] == [1, 2]
  assert captured["trained"] == [(str(exam_path), DocType.EXAME_MEDICO)]
  assert captured["closed"] == [
    ("proc-form", None),
    ("proc-em", None),
  ]


def test_enroll_prompts_for_manual_medical_exam_date(monkeypatch, tmp_path, batch_stub, reconcile_result_stub):
  form_path = tmp_path / "form.pdf"
  form_path.write_bytes(b"%PDF-1.4\n")
  exam_path = tmp_path / "exam.pdf"
  exam_path.write_bytes(b"%PDF-1.4\n")

  captured = {"kwargs": None, "closed": [], "trained": []}

  class StubClient:
    def load_player_profile(self, license, club_id=None):
      return {"nome": "Player A"}

    def add_player_to_registration_batch(self, batch_id, license, **kwargs):
      captured["kwargs"] = kwargs
      return 77

    def replace_player_registration_document(self, batch_id, license, pdf, *, tipo_doc):
      return None

  monkeypatch.setattr(cli_module, "_make_client", lambda: StubClient())
  monkeypatch.setattr(cli_module, "_resolve_enroll_batch", lambda client, reg_type, tier_id, gender_id, *, indent="": (12, batch_stub))
  monkeypatch.setattr(cli_module, "_resolve_enroll_player", lambda client, batch_obj, parsed, reg_type=None, *, indent="": (301772, batch_obj))
  monkeypatch.setattr(cli_module, "_confirm_documents_and_submit", lambda *a, **kw: True)
  monkeypatch.setattr(
    "sav_parsers.classify",
    lambda pdf: DocType.EXAME_MEDICO if str(pdf).endswith("exam.pdf") else DocType.FPB_MODELO_1,
  )
  monkeypatch.setattr(
    "sav_parsers.parse_fpb_mod1",
    lambda pdf: {"fields": {"nome": "A"}, "processing_id": "proc-form"},
  )
  monkeypatch.setattr(
    "sav_parsers.parse_em",
    lambda pdf: {
      "fields": {"exam_date": ParsedField(value="13/05/2026", confidence=0.41)},
      "processing_id": "proc-em",
    },
  )
  monkeypatch.setattr(
    "sav_parsers.close_processing",
    lambda processing_id, corrections=None: captured["closed"].append((processing_id, corrections)),
  )
  monkeypatch.setattr(
    "sav_parsers.train_classifier",
    lambda pdf, expected_doc_type: captured["trained"].append((str(pdf), expected_doc_type)),
  )
  monkeypatch.setattr(cli_module, "derive_enrollment_params", lambda parsed, client: (2, 7, 1))
  monkeypatch.setattr(cli_module, "reconcile_fpb_mod1", lambda parsed, sav_profile, client=None: reconcile_result_stub)

  runner = CliRunner()
  result = runner.invoke(
    cli_module.cli,
    ["enrollment", "create", str(form_path), "--medical-exam", str(exam_path)],
    input="2026-05-03\n",
  )

  assert result.exit_code == 0, result.output
  assert captured["kwargs"]["exam_date"] == "2026-05-03"
  assert captured["trained"] == [(str(exam_path), DocType.EXAME_MEDICO)]
  assert captured["closed"] == [
    ("proc-form", None),
    ("proc-em", {"exam_date": "2026-05-03"}),
  ]


def test_enroll_prompts_for_exam_date_without_medical_exam(monkeypatch, tmp_path, batch_stub, reconcile_result_stub):
  form_path = tmp_path / "form.pdf"
  form_path.write_bytes(b"%PDF-1.4\n")

  captured = {"kwargs": None, "closed": []}

  class StubClient:
    def load_player_profile(self, license, club_id=None):
      return {"nome": "Player A"}

    def add_player_to_registration_batch(self, batch_id, license, **kwargs):
      captured["kwargs"] = kwargs
      return 77

    def replace_player_registration_document(self, batch_id, license, pdf, *, tipo_doc):
      return None

  monkeypatch.setattr(cli_module, "_make_client", lambda: StubClient())
  monkeypatch.setattr(cli_module, "_resolve_enroll_batch", lambda client, reg_type, tier_id, gender_id, *, indent="": (12, batch_stub))
  monkeypatch.setattr(cli_module, "_resolve_enroll_player", lambda client, batch_obj, parsed, reg_type=None, *, indent="": (301772, batch_obj))
  monkeypatch.setattr(cli_module, "_confirm_documents_and_submit", lambda *a, **kw: True)
  monkeypatch.setattr("sav_parsers.classify", lambda pdf: DocType.FPB_MODELO_1)
  monkeypatch.setattr(
    "sav_parsers.parse_fpb_mod1",
    lambda pdf: {"fields": {"nome": "A"}, "processing_id": "proc-form"},
  )
  monkeypatch.setattr(
    "sav_parsers.close_processing",
    lambda processing_id, corrections=None: captured["closed"].append((processing_id, corrections)),
  )
  monkeypatch.setattr("sav_parsers.train_classifier", lambda pdf, expected_doc_type: None)
  monkeypatch.setattr(cli_module, "derive_enrollment_params", lambda parsed, client: (2, 7, 1))
  monkeypatch.setattr(cli_module, "reconcile_fpb_mod1", lambda parsed, sav_profile, client=None: reconcile_result_stub)

  runner = CliRunner()
  result = runner.invoke(
    cli_module.cli,
    ["enrollment", "create", str(form_path)],
    input="2026-05-03\n",
  )

  assert result.exit_code == 0, result.output
  assert captured["kwargs"]["exam_date"] == "2026-05-03"
  assert captured["closed"] == [
    ("proc-form", None),
  ]


def test_enroll_skips_when_parse_em_raises(monkeypatch, tmp_path, batch_stub, reconcile_result_stub):
  form_path = tmp_path / "form.pdf"
  form_path.write_bytes(b"%PDF-1.4\n")
  exam_path = tmp_path / "exam.pdf"
  exam_path.write_bytes(b"%PDF-1.4\n")

  captured = {"add_called": False, "closed": []}

  class StubClient:
    def load_player_profile(self, license, club_id=None):
      return {"nome": "Player A"}

    def add_player_to_registration_batch(self, batch_id, license, **kwargs):
      captured["add_called"] = True
      return 77

    def replace_player_registration_document(self, batch_id, license, pdf, *, tipo_doc):
      return None

  def _raise_parse_em(pdf):
    raise RuntimeError("OCR engine failure")

  monkeypatch.setattr(cli_module, "_make_client", lambda: StubClient())
  monkeypatch.setattr(cli_module, "_resolve_enroll_batch", lambda client, reg_type, tier_id, gender_id, *, indent="": (12, batch_stub))
  monkeypatch.setattr(cli_module, "_resolve_enroll_player", lambda client, batch_obj, parsed, reg_type=None, *, indent="": (301772, batch_obj))
  monkeypatch.setattr(
    "sav_parsers.classify",
    lambda pdf: DocType.EXAME_MEDICO if str(pdf).endswith("exam.pdf") else DocType.FPB_MODELO_1,
  )
  monkeypatch.setattr(
    "sav_parsers.parse_fpb_mod1",
    lambda pdf: {"fields": {"nome": "A"}, "processing_id": "proc-form"},
  )
  monkeypatch.setattr("sav_parsers.parse_em", _raise_parse_em)
  monkeypatch.setattr(
    "sav_parsers.close_processing",
    lambda processing_id, corrections=None: captured["closed"].append((processing_id, corrections)),
  )
  monkeypatch.setattr("sav_parsers.train_classifier", lambda pdf, expected_doc_type: None)
  monkeypatch.setattr(cli_module, "derive_enrollment_params", lambda parsed, client: (2, 7, 1))
  monkeypatch.setattr(cli_module, "reconcile_fpb_mod1", lambda parsed, sav_profile, client=None: reconcile_result_stub)

  runner = CliRunner()
  result = runner.invoke(
    cli_module.cli,
    ["enrollment", "create", str(form_path), "--medical-exam", str(exam_path)],
  )

  assert result.exit_code != 0
  assert not captured["add_called"]
  assert "Medical exam parse error" in result.output
  assert ("proc-form", None) in captured["closed"]


def test_enroll_skips_when_exam_date_not_entered(monkeypatch, tmp_path, batch_stub, reconcile_result_stub):
  form_path = tmp_path / "form.pdf"
  form_path.write_bytes(b"%PDF-1.4\n")
  exam_path = tmp_path / "exam.pdf"
  exam_path.write_bytes(b"%PDF-1.4\n")

  captured = {"add_called": False, "closed": []}

  class StubClient:
    def load_player_profile(self, license, club_id=None):
      return {"nome": "Player A"}

    def add_player_to_registration_batch(self, batch_id, license, **kwargs):
      captured["add_called"] = True
      return 77

    def replace_player_registration_document(self, batch_id, license, pdf, *, tipo_doc):
      return None

  monkeypatch.setattr(cli_module, "_make_client", lambda: StubClient())
  monkeypatch.setattr(cli_module, "_resolve_enroll_batch", lambda client, reg_type, tier_id, gender_id, *, indent="": (12, batch_stub))
  monkeypatch.setattr(cli_module, "_resolve_enroll_player", lambda client, batch_obj, parsed, reg_type=None, *, indent="": (301772, batch_obj))
  monkeypatch.setattr(
    "sav_parsers.classify",
    lambda pdf: DocType.EXAME_MEDICO if str(pdf).endswith("exam.pdf") else DocType.FPB_MODELO_1,
  )
  monkeypatch.setattr(
    "sav_parsers.parse_fpb_mod1",
    lambda pdf: {"fields": {"nome": "A"}, "processing_id": "proc-form"},
  )
  monkeypatch.setattr(
    "sav_parsers.parse_em",
    lambda pdf: {
      "fields": {"exam_date": ParsedField(value="13/05/2026", confidence=0.25)},
      "processing_id": "proc-em",
    },
  )
  monkeypatch.setattr(
    "sav_parsers.close_processing",
    lambda processing_id, corrections=None: captured["closed"].append((processing_id, corrections)),
  )
  monkeypatch.setattr("sav_parsers.train_classifier", lambda pdf, expected_doc_type: None)
  monkeypatch.setattr(cli_module, "derive_enrollment_params", lambda parsed, client: (2, 7, 1))
  monkeypatch.setattr(cli_module, "reconcile_fpb_mod1", lambda parsed, sav_profile, client=None: reconcile_result_stub)

  runner = CliRunner()
  result = runner.invoke(
    cli_module.cli,
    ["enrollment", "create", str(form_path), "--medical-exam", str(exam_path)],
    input="\n",
  )

  assert result.exit_code != 0
  assert not captured["add_called"]
  assert "Medical exam date required" in result.output
  assert ("proc-form", None) in captured["closed"]
  assert ("proc-em", None) in captured["closed"]


def test_strict_iso_date_rejects_non_dash_format():
  from sav_shared.medical_exam import _strict_iso_date
  assert _strict_iso_date("20260513") is None    # compact form accepted by Python 3.11+ fromisoformat
  assert _strict_iso_date("2026-5-1") is None    # missing zero-padding
  assert _strict_iso_date("2026-99-99") is None  # impossible date
  assert _strict_iso_date("13/05/2026") is None  # European format
  assert _strict_iso_date("2026-05-13") == "2026-05-13"
  assert _strict_iso_date(None) is None
  assert _strict_iso_date("") is None


def test_prompt_field_accepts_choice_mapping(monkeypatch):
  printed: list[str] = []

  class StubConsole:
    def print(self, message):
      printed.append(message)

  monkeypatch.setattr(cli_module, "_console", lambda err=False: StubConsole())
  monkeypatch.setattr(cli_module.click, "prompt", lambda text, **kwargs: "2")

  assert cli_module._prompt_field("guardian_relation", field_type=cli_module.GUARDIAN_RELATIONS) == 2
  assert any("1=Pai" in line for line in printed)


def test_prompt_field_retries_date_type(monkeypatch):
  printed: list[str] = []
  entered = iter(["13/05/2026", "2026-05-03"])

  class StubConsole:
    def print(self, message):
      printed.append(message)

  monkeypatch.setattr(cli_module, "_console", lambda err=False: StubConsole())
  monkeypatch.setattr(cli_module.click, "prompt", lambda text, **kwargs: next(entered))

  assert cli_module._prompt_field("exam_date", field_type="date") == "2026-05-03"
  assert any("YYYY-MM-DD" in line for line in printed)


def test_enrollment_create_rejects_no_input(tmp_path):
  runner = CliRunner()
  result = runner.invoke(cli_module.cli, ["enrollment", "create"])
  assert result.exit_code != 0
  assert "Pass one or more PDFs" in result.output


def test_enrollment_create_mod1_skips_classify(monkeypatch, tmp_path, batch_stub, reconcile_result_stub):
  """--mod1 skips classify() and calls train_classifier instead."""
  mod1_path = tmp_path / "form.pdf"
  mod1_path.write_bytes(b"%PDF-1.4\n")

  captured: dict = {"classify_called": False, "trained": []}

  def fake_classify(path):
    captured["classify_called"] = True
    return DocType.FPB_MODELO_1

  monkeypatch.setattr("sav_parsers.classify", fake_classify)
  monkeypatch.setattr("sav_parsers.train_classifier", lambda path, dt: captured["trained"].append(dt))
  monkeypatch.setattr(
    "sav_parsers.parse_fpb_mod1",
    lambda path: {"fields": {}, "processing_id": "proc-mod1"},
  )
  monkeypatch.setattr("sav_parsers.close_processing", lambda pid, corrections=None: None)
  monkeypatch.setattr(cli_module, "_make_client", lambda: type("C", (), {
    "load_player_profile": lambda self, lic, club_id=None: {},
    "add_player_to_registration_batch": lambda self, *a, **kw: None,
    "replace_player_registration_document": lambda self, *a, **kw: None,
  })())
  monkeypatch.setattr(cli_module, "_resolve_enroll_batch", lambda client, reg_type, tier_id, gender_id, *, indent="": (12, batch_stub))
  monkeypatch.setattr(cli_module, "_resolve_enroll_player", lambda client, batch, parsed, reg_type=None, *, indent="": (301772, batch_stub))
  monkeypatch.setattr(cli_module, "_confirm_documents_and_submit", lambda *a, **kw: True)
  monkeypatch.setattr(cli_module, "reconcile_fpb_mod1", lambda parsed, sav_profile, client=None: reconcile_result_stub)
  monkeypatch.setattr(cli_module, "derive_enrollment_params", lambda parsed, client: (2, 7, 1))

  runner = CliRunner()
  result = runner.invoke(
    cli_module.cli, ["enrollment", "create", "--mod1", str(mod1_path)],
    input="2026-01-01\n",
  )

  assert not captured["classify_called"], "classify() should be skipped for --mod1"
  assert DocType.FPB_MODELO_1 in captured["trained"]


def test_enrollment_create_manual_mode(monkeypatch, tmp_path):
  """--batch + --license + --field enrolls player without a PDF."""
  captured: dict = {"add_kwargs": None}

  monkeypatch.setattr(cli_module, "_make_client", lambda: type("C", (), {
    "resolve_batch_id": lambda self, number: int(number),
    "load_player_profile": lambda self, lic: {"nome": "Test Player"},
    "add_player_to_registration_batch": lambda self, batch_id, lic, **kw: captured.__setitem__("add_kwargs", kw),
  })())

  runner = CliRunner()
  result = runner.invoke(
    cli_module.cli,
    ["enrollment", "create", "--batch", "42", "--license", "301772", "--field", "email=foo@bar.com"],
    input="y\n",
  )

  assert result.exit_code == 0, result.output
  assert captured["add_kwargs"] == {"email": "foo@bar.com"}


def test_enrollment_create_pdf_mode_applies_field_overrides(monkeypatch, tmp_path, batch_stub, reconcile_result_stub):
  """--field values are merged into kwargs after reconcile in PDF mode."""
  form_path = tmp_path / "form.pdf"
  form_path.write_bytes(b"%PDF-1.4\n")

  captured: dict = {"add_kwargs": None}

  monkeypatch.setattr("sav_parsers.classify", lambda path: DocType.FPB_MODELO_1)
  monkeypatch.setattr(
    "sav_parsers.parse_fpb_mod1",
    lambda path: {"fields": {}, "processing_id": "proc-form"},
  )
  monkeypatch.setattr("sav_parsers.close_processing", lambda pid, corrections=None: None)
  monkeypatch.setattr(cli_module, "_make_client", lambda: type("C", (), {
    "load_player_profile": lambda self, lic, club_id=None: {},
    "add_player_to_registration_batch": lambda self, batch_id, lic, **kw: captured.__setitem__("add_kwargs", kw),
    "replace_player_registration_document": lambda self, *a, **kw: None,
  })())
  monkeypatch.setattr(cli_module, "_resolve_enroll_batch", lambda client, reg_type, tier_id, gender_id, *, indent="": (12, batch_stub))
  monkeypatch.setattr(cli_module, "_resolve_enroll_player", lambda client, batch, parsed, reg_type=None, *, indent="": (301772, batch_stub))
  # _confirm_documents_and_submit auto-approves; --field email= should win over any OCR email
  monkeypatch.setattr(cli_module, "_confirm_documents_and_submit", lambda *a, **kw: True)
  monkeypatch.setattr(cli_module, "reconcile_fpb_mod1", lambda parsed, sav_profile, client=None: reconcile_result_stub)
  monkeypatch.setattr(cli_module, "derive_enrollment_params", lambda parsed, client: (2, 7, 1))

  runner = CliRunner()
  result = runner.invoke(
    cli_module.cli,
    ["enrollment", "create", str(form_path), "--field", "email=manual@example.com"],
    input="2026-01-01\n",
  )

  assert result.exit_code == 0, result.output
  assert captured["add_kwargs"]["email"] == "manual@example.com"


def test_enrollment_update_mod1_skips_classify(monkeypatch, tmp_path):
  """--mod1 in update skips classify() and calls train_classifier."""
  mod1_path = tmp_path / "form.pdf"
  mod1_path.write_bytes(b"%PDF-1.4\n")

  captured: dict = {"classify_called": False, "trained": []}

  def fake_classify(path):
    captured["classify_called"] = True
    return DocType.FPB_MODELO_1

  monkeypatch.setattr("sav_parsers.classify", fake_classify)
  monkeypatch.setattr("sav_parsers.train_classifier", lambda path, dt: captured["trained"].append(dt))
  monkeypatch.setattr(
    "sav_parsers.parse_fpb_mod1",
    lambda path: {"fields": {}, "processing_id": "proc-mod1"},
  )
  monkeypatch.setattr("sav_parsers.close_processing", lambda pid, corrections=None: None)
  monkeypatch.setattr(cli_module, "_make_client", lambda: type("C", (), {
    "resolve_batch_id_by_license": lambda self, license: 12,
    "load_player_profile": lambda self, lic: {},
    "update_player_in_registration_batch": lambda self, *a, **kw: None,
    "replace_player_registration_document": lambda self, *a, **kw: None,
  })())
  monkeypatch.setattr(cli_module, "_confirm_documents_and_submit", lambda *a, **kw: True)
  monkeypatch.setattr(cli_module, "reconcile_fpb_mod1", lambda parsed, sav_profile, client=None: type("R", (), {
    "kwargs": {}, "needs_review": [], "retrain_corrections": {},
    "updated": {}, "kept": {}, "ocr": {}, "concelhos": {},
  })())

  runner = CliRunner()
  result = runner.invoke(
    cli_module.cli,
    ["enrollment", "update", "--license", "301772", "--mod1", str(mod1_path)],
  )

  assert not captured["classify_called"], "classify() should be skipped for --mod1"
  assert DocType.FPB_MODELO_1 in captured["trained"]


def test_enrollment_update_medical_exam_uploads_exam(monkeypatch, tmp_path):
  """--medical-exam in update uploads the exam document."""
  exam_path = tmp_path / "exam.pdf"
  exam_path.write_bytes(b"%PDF-1.4\n")

  uploaded: list = []
  monkeypatch.setattr("sav_parsers.train_classifier", lambda path, dt: None)
  monkeypatch.setattr(cli_module, "_make_client", lambda: type("C", (), {
    "resolve_batch_id_by_license": lambda self, license: 12,
    "replace_player_registration_document": lambda self, batch_id, lic, path, tipo_doc: uploaded.append(path),
  })())

  runner = CliRunner()
  result = runner.invoke(
    cli_module.cli,
    ["enrollment", "update", "--license", "301772", "--medical-exam", str(exam_path)],
  )

  assert str(exam_path) in uploaded


def test_enrollment_update_rejects_unknown_batch_flag(monkeypatch, tmp_path):
  """The new `update` interface drops --batch entirely — click rejects it."""
  pdf_path = _write_pdf(tmp_path)

  def fail_make_client():
    raise AssertionError("_make_client should not run for unknown flags")

  monkeypatch.setattr(cli_module, "_make_client", fail_make_client)

  runner = CliRunner()
  result = runner.invoke(
    cli_module.cli,
    ["enrollment", "update", "--batch", "12", "--license", "301772", str(pdf_path), "--file-only"],
  )

  assert result.exit_code != 0


def test_enrollment_read_lists_batch_items(monkeypatch):
  """`enrollment read --batch BATCH` lists all players in the batch."""

  class StubClient:
    def resolve_batch_id(self, number):
      return int(number)

    def list_player_registration_batch_items(self, batch_id):
      assert batch_id == 42
      return [
        {"license": 301772, "name": "Player A"},
        {"license": 301773, "name": "Player B"},
      ]

  monkeypatch.setattr(cli_module, "_make_client", lambda: StubClient())

  runner = CliRunner()
  result = runner.invoke(cli_module.cli, ["enrollment", "read", "--batch", "42"])

  assert result.exit_code == 0, result.output
  assert "301772" in result.output
  assert "Player A" in result.output
  assert "2 player(s) enrolled" in result.output


def test_enrollment_read_empty_batch(monkeypatch):
  """`enrollment read --batch BATCH` prints a friendly message when empty."""

  class StubClient:
    def resolve_batch_id(self, number):
      return int(number)

    def list_player_registration_batch_items(self, batch_id):
      return []

  monkeypatch.setattr(cli_module, "_make_client", lambda: StubClient())

  runner = CliRunner()
  result = runner.invoke(cli_module.cli, ["enrollment", "read", "--batch", "42"])

  assert result.exit_code == 0, result.output
  assert "No players enrolled" in result.output


def test_enrollment_read_detail(monkeypatch):
  """`enrollment read --license LICENSE` resolves the batch and shows detail."""
  captured: dict = {}

  class StubClient:
    def resolve_batch_id_by_license(self, license):
      captured["resolver_arg"] = license
      return 42

    def load_existing_registration_record(self, batch_id, license):
      captured["args"] = (batch_id, license)
      return {"id": 77, "nome": "Player A", "nif": "123456789", "email": ""}

  monkeypatch.setattr(cli_module, "_make_client", lambda: StubClient())

  runner = CliRunner()
  result = runner.invoke(cli_module.cli, ["enrollment", "read", "--license", "301772"])

  assert result.exit_code == 0, result.output
  assert captured["resolver_arg"] == 301772
  assert captured["args"] == (42, 301772)
  assert "Player A" in result.output
  assert "123456789" in result.output
  # Empty values must be filtered out of the table.
  assert "email" not in result.output


def test_enrollment_read_detail_json(monkeypatch):
  """`--output json enrollment read --license LICENSE` emits valid JSON."""
  import json as _json

  class StubClient:
    def resolve_batch_id_by_license(self, license):
      return 42

    def load_existing_registration_record(self, batch_id, license):
      return {"id": 77, "nome": "Player A"}

  monkeypatch.setattr(cli_module, "_make_client", lambda: StubClient())

  runner = CliRunner()
  result = runner.invoke(
    cli_module.cli, ["--output", "json", "enrollment", "read", "--license", "301772"]
  )

  assert result.exit_code == 0, result.output
  payload = _json.loads(result.output)
  assert payload == {"id": 77, "nome": "Player A"}


def test_enrollment_read_requires_exactly_one_flag(monkeypatch):
  """`enrollment read` with neither / both flags fails with a usage error."""
  monkeypatch.setattr(cli_module, "_make_client", lambda: type("C", (), {})())

  runner = CliRunner()
  no_flags = runner.invoke(cli_module.cli, ["enrollment", "read"])
  assert no_flags.exit_code != 0
  assert "exactly one" in no_flags.output.lower()

  both = runner.invoke(
    cli_module.cli, ["enrollment", "read", "--license", "301772", "--batch", "42"]
  )
  assert both.exit_code != 0
  assert "exactly one" in both.output.lower()


def test_enrollment_read_license_not_enrolled_lists_open_batches(monkeypatch):
  """A `read --license` miss surfaces the structured error from the client."""
  from sav_client.exceptions import LicenseNotEnrolledError

  class StubClient:
    def resolve_batch_id_by_license(self, license):
      raise LicenseNotEnrolledError(
        license=license,
        open_batches=[{"number": "2025/123", "tier": "Sub 14", "gender": "M"}],
      )

  monkeypatch.setattr(cli_module, "_make_client", lambda: StubClient())

  runner = CliRunner()
  result = runner.invoke(cli_module.cli, ["enrollment", "read", "--license", "301772"])

  assert result.exit_code != 0
  assert "not enrolled" in result.output.lower()
  assert "2025/123" in result.output


def test_enrollment_delete_license_confirms_and_removes(monkeypatch):
  """`enrollment delete --license` prompts before removing one player."""
  captured: dict = {"removed": None}

  class StubClient:
    def resolve_batch_id_by_license(self, license):
      return 42

    def remove_player_from_registration_batch(self, batch_id, license):
      captured["removed"] = (batch_id, license)

  monkeypatch.setattr(cli_module, "_make_client", lambda: StubClient())

  runner = CliRunner()
  result = runner.invoke(
    cli_module.cli, ["enrollment", "delete", "--license", "301772"], input="y\n",
  )

  assert result.exit_code == 0, result.output
  assert captured["removed"] == (42, 301772)
  assert "removed from batch" in result.output


def test_enrollment_delete_license_aborts_on_no(monkeypatch):
  """Answering 'n' to the player-delete prompt aborts without removal."""

  class StubClient:
    def resolve_batch_id_by_license(self, license):
      return 42

    def remove_player_from_registration_batch(self, batch_id, license):
      raise AssertionError("remove should not be called when user declines")

  monkeypatch.setattr(cli_module, "_make_client", lambda: StubClient())

  runner = CliRunner()
  result = runner.invoke(
    cli_module.cli, ["enrollment", "delete", "--license", "301772"], input="n\n",
  )

  assert result.exit_code != 0
  assert "removed from batch" not in result.output


def test_enrollment_delete_batch_deletes_whole_batch(monkeypatch):
  """`enrollment delete --batch BATCH` confirms and removes the entire batch."""
  captured: dict = {"deleted_id": None}

  class StubClient:
    def resolve_batch_id(self, number):
      return int(number)

    def delete_player_registration_batch(self, batch_id):
      captured["deleted_id"] = batch_id

  monkeypatch.setattr(cli_module, "_make_client", lambda: StubClient())

  runner = CliRunner()
  result = runner.invoke(
    cli_module.cli, ["enrollment", "delete", "--batch", "42"], input="y\n",
  )

  assert result.exit_code == 0, result.output
  assert captured["deleted_id"] == 42
  assert "deleted" in result.output.lower()


def test_enrollment_delete_requires_exactly_one_flag(monkeypatch):
  """`enrollment delete` with neither / both flags fails with a usage error."""
  monkeypatch.setattr(cli_module, "_make_client", lambda: type("C", (), {})())

  runner = CliRunner()
  no_flags = runner.invoke(cli_module.cli, ["enrollment", "delete"])
  assert no_flags.exit_code != 0
  assert "exactly one" in no_flags.output.lower()

  both = runner.invoke(
    cli_module.cli, ["enrollment", "delete", "--license", "301772", "--batch", "42"]
  )
  assert both.exit_code != 0
  assert "exactly one" in both.output.lower()


def test_enrollment_create_auto_classifies_two_positionals_into_form_and_exam(
  monkeypatch, tmp_path, batch_stub, reconcile_result_stub,
):
  """Two positional PDFs auto-classify into one mod1 + one exam for ONE player."""
  form_path = tmp_path / "form.pdf"
  form_path.write_bytes(b"%PDF-1.4\n")
  exam_path = tmp_path / "exam.pdf"
  exam_path.write_bytes(b"%PDF-1.4\n")

  captured: dict = {"add_calls": 0, "uploads": [], "trained": []}

  class StubClient:
    def load_player_profile(self, license, club_id=None):
      return {"nome": "Player A"}

    def add_player_to_registration_batch(self, batch_id, license, **kwargs):
      captured["add_calls"] += 1
      captured["kwargs"] = kwargs
      return 77

    def replace_player_registration_document(self, batch_id, license, pdf, *, tipo_doc):
      captured["uploads"].append((str(pdf), tipo_doc))

  monkeypatch.setattr(cli_module, "_make_client", lambda: StubClient())
  monkeypatch.setattr(cli_module, "_resolve_enroll_batch", lambda client, reg_type, tier_id, gender_id, *, indent="": (12, batch_stub))
  monkeypatch.setattr(cli_module, "_resolve_enroll_player", lambda client, batch, parsed, reg_type=None, *, indent="": (301772, batch))
  monkeypatch.setattr(cli_module, "_confirm_documents_and_submit", lambda *a, **kw: True)
  monkeypatch.setattr(
    "sav_parsers.classify",
    lambda pdf: DocType.EXAME_MEDICO if str(pdf).endswith("exam.pdf") else DocType.FPB_MODELO_1,
  )
  monkeypatch.setattr(
    "sav_parsers.parse_fpb_mod1",
    lambda pdf: {"fields": {}, "processing_id": "proc-form"},
  )
  monkeypatch.setattr(
    "sav_parsers.parse_em",
    lambda pdf: {
      "fields": {"exam_date": ParsedField(value="2026-05-01", confidence=0.92)},
      "processing_id": "proc-em",
    },
  )
  monkeypatch.setattr("sav_parsers.close_processing", lambda pid, corrections=None: None)
  monkeypatch.setattr(
    "sav_parsers.train_classifier",
    lambda pdf, dt: captured["trained"].append((str(pdf), dt)),
  )
  monkeypatch.setattr(cli_module, "derive_enrollment_params", lambda parsed, client: (2, 7, 1))
  monkeypatch.setattr(cli_module, "reconcile_fpb_mod1", lambda parsed, sav_profile, client=None: reconcile_result_stub)

  runner = CliRunner()
  result = runner.invoke(
    cli_module.cli, ["enrollment", "create", str(form_path), str(exam_path)],
  )

  assert result.exit_code == 0, result.output
  # Exactly one enrollment, not one per PDF.
  assert captured["add_calls"] == 1
  # Both documents uploaded, each with the correct tipo_doc.
  assert (str(form_path), 1) in captured["uploads"]
  assert (str(exam_path), 2) in captured["uploads"]
  # Auto-classified positionals should NOT trigger classifier training —
  # only explicit --mod1 / --medical-exam pinning does. Both PDFs here were
  # auto-classified, so no training calls should fire.
  assert captured["trained"] == []
  assert f"{form_path.name} (fpb_modelo_1)" in result.output
  assert f"{exam_path.name} (exame_medico)" in result.output


def test_enrollment_create_mod4_marks_subida(
  monkeypatch, tmp_path, batch_stub, reconcile_result_stub,
):
  """A classified fpb_modelo_4 sets is_subida=True, uploads as tipo_doc=6, and
  shows the subida row as yes."""
  form_path = tmp_path / "form.pdf"
  form_path.write_bytes(b"%PDF-1.4\n")
  exam_path = tmp_path / "exam.pdf"
  exam_path.write_bytes(b"%PDF-1.4\n")
  subida_path = tmp_path / "subida.pdf"
  subida_path.write_bytes(b"%PDF-1.4\n")

  captured: dict = {"add_calls": 0, "uploads": [], "trained": []}

  class StubClient:
    def load_player_profile(self, license, club_id=None):
      return {"nome": "Player A"}

    def add_player_to_registration_batch(self, batch_id, license, **kwargs):
      captured["add_calls"] += 1
      captured["kwargs"] = kwargs
      return 77

    def replace_player_registration_document(self, batch_id, license, pdf, *, tipo_doc):
      captured["uploads"].append((str(pdf), tipo_doc))

  monkeypatch.setattr(cli_module, "_make_client", lambda: StubClient())
  monkeypatch.setattr(cli_module, "_resolve_enroll_batch", lambda client, reg_type, tier_id, gender_id, *, indent="": (12, batch_stub))
  monkeypatch.setattr(cli_module, "_resolve_enroll_player", lambda client, batch, parsed, reg_type=None, *, indent="": (301772, batch))
  monkeypatch.setattr(cli_module, "_confirm_documents_and_submit", lambda *a, **kw: True)

  def classify(pdf):
    if str(pdf).endswith("exam.pdf"):
      return DocType.EXAME_MEDICO
    if str(pdf).endswith("subida.pdf"):
      return DocType.FPB_MODELO_4
    return DocType.FPB_MODELO_1

  monkeypatch.setattr("sav_parsers.classify", classify)
  monkeypatch.setattr("sav_parsers.parse_fpb_mod1", lambda pdf: {"fields": {}, "processing_id": "proc-form"})
  monkeypatch.setattr(
    "sav_parsers.parse_em",
    lambda pdf: {
      "fields": {"exam_date": ParsedField(value="2026-05-01", confidence=0.92)},
      "processing_id": "proc-em",
    },
  )
  monkeypatch.setattr("sav_parsers.close_processing", lambda pid, corrections=None: None)
  monkeypatch.setattr("sav_parsers.train_classifier", lambda pdf, dt: captured["trained"].append((str(pdf), dt)))
  monkeypatch.setattr(cli_module, "derive_enrollment_params", lambda parsed, client: (2, 7, 1))
  monkeypatch.setattr(cli_module, "reconcile_fpb_mod1", lambda parsed, sav_profile, client=None: reconcile_result_stub)

  runner = CliRunner()
  result = runner.invoke(
    cli_module.cli, ["enrollment", "create", str(form_path), str(exam_path), str(subida_path)],
  )

  assert result.exit_code == 0, result.output
  assert captured["add_calls"] == 1
  assert captured["kwargs"].get("is_subida") is True
  # mod4 uploaded as tipo_doc=6.
  assert (str(subida_path), 6) in captured["uploads"]
  # Auto-classified positional → no classifier training.
  assert captured["trained"] == []
  assert "Subida de Escalão" in result.output
  assert "fpb_modelo_4" in result.output


def test_enrollment_create_without_mod4_is_not_subida(
  monkeypatch, tmp_path, batch_stub, reconcile_result_stub,
):
  """No mod4 anywhere → is_subida=False (the default, unchanged behavior)."""
  form_path = tmp_path / "form.pdf"
  form_path.write_bytes(b"%PDF-1.4\n")
  exam_path = tmp_path / "exam.pdf"
  exam_path.write_bytes(b"%PDF-1.4\n")

  captured: dict = {"uploads": []}

  class StubClient:
    def load_player_profile(self, license, club_id=None):
      return {"nome": "Player A"}

    def add_player_to_registration_batch(self, batch_id, license, **kwargs):
      captured["kwargs"] = kwargs
      return 77

    def replace_player_registration_document(self, batch_id, license, pdf, *, tipo_doc):
      captured["uploads"].append((str(pdf), tipo_doc))

  monkeypatch.setattr(cli_module, "_make_client", lambda: StubClient())
  monkeypatch.setattr(cli_module, "_resolve_enroll_batch", lambda client, reg_type, tier_id, gender_id, *, indent="": (12, batch_stub))
  monkeypatch.setattr(cli_module, "_resolve_enroll_player", lambda client, batch, parsed, reg_type=None, *, indent="": (301772, batch))
  monkeypatch.setattr(cli_module, "_confirm_documents_and_submit", lambda *a, **kw: True)
  monkeypatch.setattr(
    "sav_parsers.classify",
    lambda pdf: DocType.EXAME_MEDICO if str(pdf).endswith("exam.pdf") else DocType.FPB_MODELO_1,
  )
  monkeypatch.setattr("sav_parsers.parse_fpb_mod1", lambda pdf: {"fields": {}, "processing_id": "proc-form"})
  monkeypatch.setattr(
    "sav_parsers.parse_em",
    lambda pdf: {
      "fields": {"exam_date": ParsedField(value="2026-05-01", confidence=0.92)},
      "processing_id": "proc-em",
    },
  )
  monkeypatch.setattr("sav_parsers.close_processing", lambda pid, corrections=None: None)
  monkeypatch.setattr("sav_parsers.train_classifier", lambda pdf, dt: None)
  monkeypatch.setattr(cli_module, "derive_enrollment_params", lambda parsed, client: (2, 7, 1))
  monkeypatch.setattr(cli_module, "reconcile_fpb_mod1", lambda parsed, sav_profile, client=None: reconcile_result_stub)

  runner = CliRunner()
  result = runner.invoke(
    cli_module.cli, ["enrollment", "create", str(form_path), str(exam_path)],
  )

  assert result.exit_code == 0, result.output
  assert captured["kwargs"].get("is_subida") is False


def test_enrollment_create_rejects_two_mod1_pdfs(monkeypatch, tmp_path):
  """Two positional PDFs both classifying as mod1 is rejected as ambiguous."""
  a = tmp_path / "a.pdf"
  a.write_bytes(b"%PDF-1.4\n")
  b = tmp_path / "b.pdf"
  b.write_bytes(b"%PDF-1.4\n")

  monkeypatch.setattr(cli_module, "_make_client", lambda: type("C", (), {})())
  monkeypatch.setattr("sav_parsers.classify", lambda pdf: DocType.FPB_MODELO_1)

  runner = CliRunner()
  result = runner.invoke(cli_module.cli, ["enrollment", "create", str(a), str(b)])

  assert result.exit_code != 0
  assert "one enrollment per invocation" in result.output
  assert "2 fpb_modelo_1 forms" in result.output


def test_enrollment_create_rejects_two_exam_pdfs(monkeypatch, tmp_path):
  """A form plus two exams (one positional + one --medical-exam) is rejected."""
  form = tmp_path / "form.pdf"
  form.write_bytes(b"%PDF-1.4\n")
  e1 = tmp_path / "e1.pdf"
  e1.write_bytes(b"%PDF-1.4\n")
  e2 = tmp_path / "e2.pdf"
  e2.write_bytes(b"%PDF-1.4\n")

  monkeypatch.setattr(cli_module, "_make_client", lambda: type("C", (), {})())
  monkeypatch.setattr(
    "sav_parsers.classify",
    lambda pdf: DocType.FPB_MODELO_1 if str(pdf).endswith("form.pdf") else DocType.EXAME_MEDICO,
  )

  runner = CliRunner()
  result = runner.invoke(
    cli_module.cli,
    ["enrollment", "create", str(form), str(e1), "--medical-exam", str(e2)],
  )

  assert result.exit_code != 0
  assert "at most one exame_medico" in result.output


def test_enrollment_create_pinned_supplementary_docs(
  monkeypatch, tmp_path, batch_stub, reconcile_result_stub,
):
  """--atestado/--id-doc skip classify, upload with the right tipo_doc, and train
  (except --outros). Two --id-doc files coexist: first replaces, second appends."""
  for name in ("form.pdf", "ate.pdf", "id1.pdf", "id2.pdf", "misc.pdf"):
    (tmp_path / name).write_bytes(b"%PDF-1.4\n")

  captured: dict = {"replaced": [], "appended": [], "trained": [], "classify_called": False}

  class StubClient:
    def load_player_profile(self, license, club_id=None):
      return {"nome": "Player A"}

    def add_player_to_registration_batch(self, batch_id, license, **kwargs):
      return 77

    def replace_player_registration_document(self, batch_id, license, pdf, *, tipo_doc):
      captured["replaced"].append((str(pdf), tipo_doc))

    def upload_player_registration_document(self, batch_id, license, pdf, *, tipo_doc):
      captured["appended"].append((str(pdf), tipo_doc))

  def fake_classify(pdf):
    captured["classify_called"] = True
    return DocType.FPB_MODELO_1

  monkeypatch.setattr(cli_module, "_make_client", lambda: StubClient())
  monkeypatch.setattr(cli_module, "_resolve_enroll_batch", lambda client, reg_type, tier_id, gender_id, *, indent="": (12, batch_stub))
  monkeypatch.setattr(cli_module, "_resolve_enroll_player", lambda client, batch, parsed, reg_type=None, *, indent="": (301772, batch_stub))
  monkeypatch.setattr(cli_module, "_confirm_documents_and_submit", lambda *a, **kw: True)
  monkeypatch.setattr("sav_parsers.classify", fake_classify)
  monkeypatch.setattr("sav_parsers.parse_fpb_mod1", lambda pdf: {"fields": {}, "processing_id": "proc-form"})
  monkeypatch.setattr("sav_parsers.close_processing", lambda pid, corrections=None: None)
  monkeypatch.setattr("sav_parsers.train_classifier", lambda pdf, dt: captured["trained"].append(dt))
  monkeypatch.setattr(cli_module, "derive_enrollment_params", lambda parsed, client: (2, 7, 1))
  monkeypatch.setattr(cli_module, "reconcile_fpb_mod1", lambda parsed, sav_profile, client=None: reconcile_result_stub)

  runner = CliRunner()
  result = runner.invoke(
    cli_module.cli,
    [
      "enrollment", "create",
      "--mod1", str(tmp_path / "form.pdf"),
      "--atestado", str(tmp_path / "ate.pdf"),
      "--id-doc", str(tmp_path / "id1.pdf"),
      "--id-doc", str(tmp_path / "id2.pdf"),
      "--outros", str(tmp_path / "misc.pdf"),
    ],
    input="2026-01-01\n",
  )

  assert result.exit_code == 0, result.output
  # Nothing was auto-classified: every doc was pinned via a flag.
  assert not captured["classify_called"]
  # mod1 (1), atestado (15), first id-doc (18), outros (22) all replace.
  assert (str(tmp_path / "ate.pdf"), 15) in captured["replaced"]
  assert (str(tmp_path / "id1.pdf"), 18) in captured["replaced"]
  assert (str(tmp_path / "misc.pdf"), 22) in captured["replaced"]
  # Second id-doc shares tipo_doc=18, so it appends instead of clobbering id1.
  assert (str(tmp_path / "id2.pdf"), 18) in captured["appended"]
  # Pinned identifiable docs train the classifier; the outros catch-all never does.
  assert DocType.ATESTADO_RESIDENCIA in captured["trained"]
  assert captured["trained"].count(DocType.DOCUMENTO_IDENTIFICACAO) == 2
  assert DocType.OUTROS not in captured["trained"]


def test_enrollment_create_rejects_two_atestado(monkeypatch, tmp_path):
  """A positional atestado plus --atestado exceeds the per-player limit of one."""
  form = tmp_path / "form.pdf"
  form.write_bytes(b"%PDF-1.4\n")
  a1 = tmp_path / "a1.pdf"
  a1.write_bytes(b"%PDF-1.4\n")
  a2 = tmp_path / "a2.pdf"
  a2.write_bytes(b"%PDF-1.4\n")

  monkeypatch.setattr(cli_module, "_make_client", lambda: type("C", (), {})())
  monkeypatch.setattr(
    "sav_parsers.classify",
    lambda pdf: DocType.FPB_MODELO_1 if str(pdf).endswith("form.pdf") else DocType.ATESTADO_RESIDENCIA,
  )

  runner = CliRunner()
  result = runner.invoke(
    cli_module.cli,
    ["enrollment", "create", str(form), str(a1), "--atestado", str(a2)],
  )

  assert result.exit_code != 0
  assert "at most one atestado_residencia" in result.output


def test_enrollment_create_rejects_pdf_input_without_mod1(monkeypatch, tmp_path):
  """If a positional PDF doesn't classify as mod1 or em, the command fails."""
  random_pdf = tmp_path / "random.pdf"
  random_pdf.write_bytes(b"%PDF-1.4\n")

  monkeypatch.setattr(cli_module, "_make_client", lambda: type("C", (), {})())
  monkeypatch.setattr("sav_parsers.classify", lambda pdf: DocType.OUTROS)

  runner = CliRunner()
  result = runner.invoke(cli_module.cli, ["enrollment", "create", str(random_pdf)])

  assert result.exit_code != 0
  assert "No fpb_modelo_1 form provided" in result.output
