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
  return type("BatchStub", (), {"id": 12, "club_id": 99})()


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
    ["enrollment", "update", "12", "301772", str(pdf_path), "--file-only", "--tipo", "modelo1"],
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
    ["enrollment", "update", "12", "301772", str(pdf_path), "--file-only", "--tipo", "1"],
  )

  assert result.exit_code != 0
  assert "Unknown doc_type '1'" in result.output


def test_enrollment_update_maps_parser_tipo_names_for_file_replace(monkeypatch, tmp_path):
  pdf_path = _write_pdf(tmp_path)
  captured: list[int] = []

  class StubClient:
    def replace_player_registration_document(self, batch_id, license, pdf, *, tipo_doc):
      captured.append(tipo_doc)

  monkeypatch.setattr(cli_module, "_make_client", lambda: StubClient())

  runner = CliRunner()
  result = runner.invoke(
    cli_module.cli,
    [
      "enrollment", "update", "12", "301772", str(pdf_path), "--file-only",
      "--tipo", "exame_medico",
    ],
  )

  assert result.exit_code == 0
  assert captured == [2]
  assert "Replaced exame_medico" in result.output


def test_enrollment_update_classifies_exam_for_file_replace(monkeypatch, tmp_path):
  pdf_path = _write_pdf(tmp_path)
  captured: list[int] = []

  class StubClient:
    def replace_player_registration_document(self, batch_id, license, pdf, *, tipo_doc):
      captured.append(tipo_doc)

  monkeypatch.setattr(cli_module, "_make_client", lambda: StubClient())
  monkeypatch.setattr("sav_parsers.classify", lambda pdf: DocType.EM)

  runner = CliRunner()
  result = runner.invoke(
    cli_module.cli,
    ["enrollment", "update", "12", "301772", str(pdf_path), "--file-only"],
  )

  assert result.exit_code == 0
  assert captured == [2]
  assert "Replaced exame_medico" in result.output


def test_enrollment_update_rejects_unmapped_classified_doc_type(monkeypatch, tmp_path):
  pdf_path = _write_pdf(tmp_path)

  def fail_make_client():
    raise AssertionError("_make_client should not run for unmapped classified doc types")

  monkeypatch.setattr(cli_module, "_make_client", fail_make_client)
  monkeypatch.setattr("sav_parsers.classify", lambda pdf: DocType.OUTROS)

  runner = CliRunner()
  result = runner.invoke(
    cli_module.cli,
    ["enrollment", "update", "12", "301772", str(pdf_path), "--file-only"],
  )

  assert result.exit_code != 0
  assert "has no SAV2 tipo_doc mapping yet" in result.output


def test_enrollment_update_reconcile_accepts_only_fpb_modelo_1(monkeypatch, tmp_path):
  pdf_path = _write_pdf(tmp_path)

  def fail_make_client():
    raise AssertionError("_make_client should not run for unsupported reconcile doc types")

  monkeypatch.setattr(cli_module, "_make_client", fail_make_client)

  runner = CliRunner()
  result = runner.invoke(
    cli_module.cli,
    ["enrollment", "update", "12", "301772", str(pdf_path), "--tipo", "exame_medico"],
  )

  assert result.exit_code != 0
  assert "only fpb_modelo_1 forms are reconciled" in result.output


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
  monkeypatch.setattr(cli_module, "_resolve_enroll_batch", lambda client, reg_type, tier_id, gender_id: (12, batch_stub))
  monkeypatch.setattr(cli_module, "_resolve_enroll_player", lambda client, batch_obj, parsed: (301772, batch_obj))
  monkeypatch.setattr(cli_module, "_confirm_enroll", lambda result, sav_profile, license: {})
  monkeypatch.setattr(
    "sav_parsers.classify",
    lambda pdf: DocType.EM if str(pdf).endswith("exam.pdf") else DocType.FPB_MOD1,
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
  monkeypatch.setattr("sav_shared.fpb_mod1.reconcile_fpb_mod1", lambda parsed, sav_profile, client=None: reconcile_result_stub)

  runner = CliRunner()
  result = runner.invoke(
    cli_module.cli,
    ["enrollment", "create", str(form_path), "--medical-exam", str(exam_path)],
  )

  assert result.exit_code == 0
  assert captured["kwargs"]["exam_date"] == "2026-05-01"
  assert captured["uploads"] == [1, 2]
  assert captured["trained"] == [(str(exam_path), DocType.EM)]
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
  monkeypatch.setattr(cli_module, "_resolve_enroll_batch", lambda client, reg_type, tier_id, gender_id: (12, batch_stub))
  monkeypatch.setattr(cli_module, "_resolve_enroll_player", lambda client, batch_obj, parsed: (301772, batch_obj))
  monkeypatch.setattr(cli_module, "_confirm_enroll", lambda result, sav_profile, license: {})
  monkeypatch.setattr(
    "sav_parsers.classify",
    lambda pdf: DocType.EM if str(pdf).endswith("exam.pdf") else DocType.FPB_MOD1,
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
  monkeypatch.setattr("sav_shared.fpb_mod1.reconcile_fpb_mod1", lambda parsed, sav_profile, client=None: reconcile_result_stub)

  runner = CliRunner()
  result = runner.invoke(
    cli_module.cli,
    ["enrollment", "create", str(form_path), "--medical-exam", str(exam_path)],
    input="2026-05-03\n",
  )

  assert result.exit_code == 0
  assert captured["kwargs"]["exam_date"] == "2026-05-03"
  assert captured["trained"] == [(str(exam_path), DocType.EM)]
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
  monkeypatch.setattr(cli_module, "_resolve_enroll_batch", lambda client, reg_type, tier_id, gender_id: (12, batch_stub))
  monkeypatch.setattr(cli_module, "_resolve_enroll_player", lambda client, batch_obj, parsed: (301772, batch_obj))
  monkeypatch.setattr(cli_module, "_confirm_enroll", lambda result, sav_profile, license: {})
  monkeypatch.setattr("sav_parsers.classify", lambda pdf: DocType.FPB_MOD1)
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
  monkeypatch.setattr("sav_shared.fpb_mod1.reconcile_fpb_mod1", lambda parsed, sav_profile, client=None: reconcile_result_stub)

  runner = CliRunner()
  result = runner.invoke(
    cli_module.cli,
    ["enrollment", "create", str(form_path)],
    input="2026-05-03\n",
  )

  assert result.exit_code == 0
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
  monkeypatch.setattr(cli_module, "_resolve_enroll_batch", lambda client, reg_type, tier_id, gender_id: (12, batch_stub))
  monkeypatch.setattr(cli_module, "_resolve_enroll_player", lambda client, batch_obj, parsed: (301772, batch_obj))
  monkeypatch.setattr(cli_module, "_confirm_enroll", lambda result, sav_profile, license: {})
  monkeypatch.setattr(
    "sav_parsers.classify",
    lambda pdf: DocType.EM if str(pdf).endswith("exam.pdf") else DocType.FPB_MOD1,
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
  monkeypatch.setattr("sav_shared.fpb_mod1.reconcile_fpb_mod1", lambda parsed, sav_profile, client=None: reconcile_result_stub)

  runner = CliRunner()
  result = runner.invoke(
    cli_module.cli,
    ["enrollment", "create", str(form_path), "--medical-exam", str(exam_path)],
  )

  assert result.exit_code == 0
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
  monkeypatch.setattr(cli_module, "_resolve_enroll_batch", lambda client, reg_type, tier_id, gender_id: (12, batch_stub))
  monkeypatch.setattr(cli_module, "_resolve_enroll_player", lambda client, batch_obj, parsed: (301772, batch_obj))
  monkeypatch.setattr(cli_module, "_confirm_enroll", lambda result, sav_profile, license: {})
  monkeypatch.setattr(
    "sav_parsers.classify",
    lambda pdf: DocType.EM if str(pdf).endswith("exam.pdf") else DocType.FPB_MOD1,
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
  monkeypatch.setattr("sav_shared.fpb_mod1.reconcile_fpb_mod1", lambda parsed, sav_profile, client=None: reconcile_result_stub)

  runner = CliRunner()
  result = runner.invoke(
    cli_module.cli,
    ["enrollment", "create", str(form_path), "--medical-exam", str(exam_path)],
    input="\n",
  )

  assert result.exit_code == 0
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

  captured: dict = {"classify_called": False, "trained": [], "add_called": False}

  def fake_classify(path):
    captured["classify_called"] = True
    return DocType.FPB_MOD1

  monkeypatch.setattr("sav_parsers.classify", fake_classify)
  monkeypatch.setattr("sav_parsers.train_classifier", lambda path, dt: captured["trained"].append(dt))
  monkeypatch.setattr(
    "sav_parsers.parse_fpb_mod1",
    lambda path: {"fields": {}, "processing_id": "proc-mod1"},
  )
  monkeypatch.setattr("sav_parsers.close_processing", lambda pid, corrections=None: None)
  monkeypatch.setattr(cli_module, "_make_client", lambda: type("C", (), {
    "load_player_profile": lambda self, lic, club_id=None: {},
    "add_player_to_registration_batch": lambda self, *a, **kw: captured.__setitem__("add_called", True),
    "replace_player_registration_document": lambda self, *a, **kw: None,
  })())
  monkeypatch.setattr(cli_module, "_resolve_enroll_batch", lambda client, reg_type, tier_id, gender_id: (12, batch_stub))
  monkeypatch.setattr(cli_module, "_resolve_enroll_player", lambda client, batch, parsed: (301772, batch_stub))
  monkeypatch.setattr(cli_module, "_confirm_enroll", lambda result, sav_profile, license: {"exam_date": "2026-01-01"})
  monkeypatch.setattr("sav_shared.fpb_mod1.reconcile_fpb_mod1", lambda parsed, sav_profile, client=None: reconcile_result_stub)
  monkeypatch.setattr(cli_module, "derive_enrollment_params", lambda parsed, client: (2, 7, 1))

  runner = CliRunner()
  result = runner.invoke(cli_module.cli, ["enrollment", "create", "--mod1", str(mod1_path)])

  assert not captured["classify_called"], "classify() should be skipped for --mod1"
  assert DocType.FPB_MOD1 in captured["trained"]


def test_enrollment_create_manual_mode(monkeypatch, tmp_path):
  """--batch + --license + --field enrolls player without a PDF."""
  captured: dict = {"add_kwargs": None}

  monkeypatch.setattr(cli_module, "_make_client", lambda: type("C", (), {
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

  monkeypatch.setattr("sav_parsers.classify", lambda path: DocType.FPB_MOD1)
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
  monkeypatch.setattr(cli_module, "_resolve_enroll_batch", lambda client, reg_type, tier_id, gender_id: (12, batch_stub))
  monkeypatch.setattr(cli_module, "_resolve_enroll_player", lambda client, batch, parsed: (301772, batch_stub))
  # reconcile returns email from OCR; --field should override it
  monkeypatch.setattr(cli_module, "_confirm_enroll", lambda result, sav_profile, license: {"exam_date": "2026-01-01", "email": "ocr@example.com"})
  monkeypatch.setattr("sav_shared.fpb_mod1.reconcile_fpb_mod1", lambda parsed, sav_profile, client=None: reconcile_result_stub)
  monkeypatch.setattr(cli_module, "derive_enrollment_params", lambda parsed, client: (2, 7, 1))

  runner = CliRunner()
  result = runner.invoke(
    cli_module.cli,
    ["enrollment", "create", str(form_path), "--field", "email=manual@example.com"],
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
    return DocType.FPB_MOD1

  monkeypatch.setattr("sav_parsers.classify", fake_classify)
  monkeypatch.setattr("sav_parsers.train_classifier", lambda path, dt: captured["trained"].append(dt))
  monkeypatch.setattr(
    "sav_parsers.parse_fpb_mod1",
    lambda path: {"fields": {}, "processing_id": "proc-mod1"},
  )
  monkeypatch.setattr("sav_parsers.close_processing", lambda pid, corrections=None: None)
  monkeypatch.setattr(cli_module, "_make_client", lambda: type("C", (), {
    "load_player_profile": lambda self, lic: {},
    "update_player_in_registration_batch": lambda self, *a, **kw: None,
    "replace_player_registration_document": lambda self, *a, **kw: None,
  })())
  monkeypatch.setattr(cli_module, "_confirm_enroll", lambda result, sav_profile, license: {})
  monkeypatch.setattr("sav_shared.fpb_mod1.reconcile_fpb_mod1", lambda parsed, sav_profile, client=None: type("R", (), {
    "needs_review": [], "retrain_corrections": {},
  })())

  runner = CliRunner()
  result = runner.invoke(
    cli_module.cli,
    ["enrollment", "update", "12", "301772", "--mod1", str(mod1_path)],
  )

  assert not captured["classify_called"], "classify() should be skipped for --mod1"
  assert DocType.FPB_MOD1 in captured["trained"]


def test_enrollment_update_medical_exam_uploads_exam(monkeypatch, tmp_path):
  """--medical-exam in update uploads the exam document."""
  exam_path = tmp_path / "exam.pdf"
  exam_path.write_bytes(b"%PDF-1.4\n")

  uploaded: list = []
  monkeypatch.setattr("sav_parsers.train_classifier", lambda path, dt: None)
  monkeypatch.setattr(cli_module, "_make_client", lambda: type("C", (), {
    "replace_player_registration_document": lambda self, batch_id, lic, path, tipo_doc: uploaded.append(path),
  })())

  runner = CliRunner()
  result = runner.invoke(
    cli_module.cli,
    ["enrollment", "update", "12", "301772", "--medical-exam", str(exam_path)],
  )

  assert str(exam_path) in uploaded
