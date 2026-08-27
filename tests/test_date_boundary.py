"""Caller-supplied values are checked at the tool boundary, not by the federation.

Dates, consent flags, and the override allowlist all follow the same rule: a
value the tool cannot honour is rejected here, where the caller can still be
told which field is wrong.

SAV2 rejects a bad date long after the call that supplied it, and usually
without saying why — the live 2026-08-27 run cost four attempts to a commit
that answered `{"val":0,"msg":""}`. So the CLI and MCP surfaces reject the
value where the caller can still be told which field is wrong. A DD-MM-YYYY
date is rejected rather than converted: guessing the caller's convention is
how a wrong-but-plausible date gets filed with the federation.
"""

import base64

import click
import pytest

from sav_cli import cli as cli_module
from sav_mcp import server as server_module


def _pdf_b64() -> str:
  return base64.b64encode(b"%PDF-1.4\n").decode("ascii")


class TestParseEnrollmentFormsExamDate:
  """`exam_date` is validated at entry, not five calls later."""

  def test_rejects_european_exam_date(self, monkeypatch):
    monkeypatch.setattr(server_module, "_get_client", lambda: object())
    monkeypatch.setattr(server_module, "_forms", {})

    result = server_module.parse_enrollment_forms(
      [{"pdf": _pdf_b64(), "doc_type": "exame_medico", "exam_date": "13/05/2026"}]
    )

    assert result[0]["index"] == 0
    assert "exam_date must be YYYY-MM-DD" in result[0]["error"]
    # Nothing was cached — a rejected entry must not leave an artifact behind.
    assert server_module._forms == {}

  def test_rejects_unpadded_exam_date(self, monkeypatch):
    monkeypatch.setattr(server_module, "_get_client", lambda: object())
    monkeypatch.setattr(server_module, "_forms", {})

    result = server_module.parse_enrollment_forms(
      [{"pdf": _pdf_b64(), "doc_type": "exame_medico", "exam_date": "2026-5-1"}]
    )
    assert "exam_date must be YYYY-MM-DD" in result[0]["error"]

  def test_error_does_not_blame_ocr(self, monkeypatch):
    """The old failure mode: a hand-typed date surfaced as an OCR complaint."""
    monkeypatch.setattr(server_module, "_get_client", lambda: object())
    monkeypatch.setattr(server_module, "_forms", {})

    result = server_module.parse_enrollment_forms(
      [{"pdf": _pdf_b64(), "doc_type": "exame_medico", "exam_date": "13/05/2026"}]
    )
    assert "OCR" not in result[0]["error"]

  def test_iso_exam_date_still_accepted(self, monkeypatch):
    monkeypatch.setattr(server_module, "_get_client", lambda: object())
    monkeypatch.setattr(server_module, "_forms", {})

    result = server_module.parse_enrollment_forms(
      [{"pdf": _pdf_b64(), "doc_type": "exame_medico", "exam_date": "2026-05-13"}]
    )
    assert result[0].get("error") is None
    assert result[0]["exam_date"] == "2026-05-13"


class TestValidateOverrides:
  """The shared override guard used by submit_enrollment / update_with_document."""

  def test_passes_iso_through(self):
    out = server_module._validate_overrides(
      {"exam_date": "2026-05-13", "id_expiry": "2030-01-01", "email": "a@b.pt"}
    )
    assert out == {"exam_date": "2026-05-13", "id_expiry": "2030-01-01", "email": "a@b.pt"}

  @pytest.mark.parametrize("key", ["exam_date", "id_expiry", "birth_date"])
  def test_rejects_european_on_every_date_kwarg(self, key):
    with pytest.raises(ValueError, match=f"{key} must be YYYY-MM-DD"):
      server_module._validate_overrides({key: "13/05/2026"})

  def test_ignores_absent_and_blank_dates(self):
    # Blank means "not supplied" here; only a present value is validated.
    assert server_module._validate_overrides({"email": "a@b.pt"}) == {"email": "a@b.pt"}
    assert server_module._validate_overrides({"exam_date": ""}) == {"exam_date": ""}

  def test_does_not_mutate_the_caller_dict(self):
    original = {"exam_date": "2026-05-13"}
    server_module._validate_overrides(original)
    assert original == {"exam_date": "2026-05-13"}


class TestUpdateEnrollmentDates:
  """`update_enrollment` validates before it ever builds a client."""

  @pytest.mark.parametrize("key", ["exam_date", "id_expiry"])
  def test_rejects_european_date(self, key, monkeypatch):
    def _no_client():
      raise AssertionError("date validation must run before the client is built")

    monkeypatch.setattr(server_module, "_get_client", _no_client)
    with pytest.raises(ValueError, match=f"{key} must be YYYY-MM-DD"):
      server_module.update_enrollment(license=301772, fields={key: "13/05/2026"})


class TestCliUpdateFields:
  """`--field id_expiry=...` fails at the CLI, not in the federation."""

  def test_rejects_european_date(self):
    with pytest.raises(click.UsageError, match="id_expiry must be YYYY-MM-DD"):
      cli_module._parse_update_fields(("id_expiry=13/05/2026",))

  def test_rejects_european_exam_date(self):
    with pytest.raises(click.UsageError, match="exam_date must be YYYY-MM-DD"):
      cli_module._parse_update_fields(("exam_date=13/05/2026",))

  def test_accepts_iso(self):
    assert cli_module._parse_update_fields(("id_expiry=2030-01-01",)) == {
      "id_expiry": "2030-01-01"
    }

  def test_non_date_fields_are_untouched(self):
    assert cli_module._parse_update_fields(("email=a@b.pt",)) == {"email": "a@b.pt"}


class TestConsentDecoding:
  """Consents are recorded against a real person; truthiness is not good enough."""

  @pytest.mark.parametrize(
    "value,expected",
    [
      (True, True), (False, False),
      (1, True), (0, False),
      ("true", True), ("false", False),
      ("TRUE", True), ("False", False),
      ("1", True), ("0", False),
      ("sim", True), ("não", False),
    ],
  )
  def test_decodes_the_documented_vocabulary(self, value, expected):
    assert server_module._require_bool(value, field="consent_data") is expected

  def test_string_false_is_not_true(self):
    # bool("false") is True — the whole reason this helper exists. Left to
    # Python truthiness, a caller asking to withhold consent recorded consent.
    assert server_module._require_bool("false", field="consent_marketing") is False

  @pytest.mark.parametrize("value", ["maybe", 2, -1, [], {}, "  "])
  def test_rejects_anything_ambiguous(self, value):
    with pytest.raises(ValueError, match="expects a boolean"):
      server_module._require_bool(value, field="consent_data")

  def test_update_enrollment_rejects_ambiguous_consent(self, monkeypatch):
    def _no_client():
      raise AssertionError("validation must run before the client is built")

    monkeypatch.setattr(server_module, "_get_client", _no_client)
    with pytest.raises(ValueError, match="expects a boolean"):
      server_module.update_enrollment(license=301772, fields={"consent_data": "maybe"})

  def test_overrides_decode_consents(self):
    out = server_module._validate_overrides({"consent_marketing": "false"})
    assert out["consent_marketing"] is False


class TestUpdateWithDocumentOverrides:
  """Keys this path can't apply are rejected, not silently dropped."""

  @pytest.mark.parametrize(
    "key", ["exam_date", "guardian_name", "consent_data", "consent_marketing"],
  )
  def test_rejects_step3_keys(self, key):
    # These used to be filtered out while the response still said
    # fields_updated=True, so the caller believed the patch had landed.
    with pytest.raises(ValueError, match="cannot apply"):
      server_module.update_enrollment_with_document(
        license=301772, pdf="", field_overrides={key: "x"},
      )

  def test_error_points_at_the_right_tool(self):
    with pytest.raises(ValueError, match="Use update_enrollment"):
      server_module.update_enrollment_with_document(
        license=301772, pdf="", field_overrides={"exam_date": "2026-05-01"},
      )

  def test_rejects_unknown_keys_too(self):
    with pytest.raises(ValueError, match="cannot apply"):
      server_module.update_enrollment_with_document(
        license=301772, pdf="", field_overrides={"nonsense": "x"},
      )
