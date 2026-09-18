"""The Estatuto FBP surface across the MCP boundary.

`preview_enrollment` reports SAV's type-1 Estatuto rule when op=151 states it,
and `add_enrollment` still accepts an explicit caller Estatuto while refusing
the FPB-only status that has no box on the form.

The nationality trap is tested here too, and it is a separate field: SAV's
type-1 wizard defaults nationality to Portugal, so an unconfirmed nationality
does not leave a gap in the record, it files the player as Portuguese.
"""

import pytest

from sav_client.exceptions import SavError
from sav_parsers.types import DocType, ParsedField

from sav_mcp import server as server_module
from sav_shared.lookups import (
  ESTATUTO_EQUIPARADO_FBP,
  ESTATUTO_FBP,
  ESTATUTO_SEM_FBP_COMUNITARIO,
  ESTATUTO_SEM_FBP_NAO_COMUNITARIO,
)


def _fields(**entities) -> dict:
  out = {}
  for name, value in entities.items():
    if isinstance(value, tuple):
      value, confidence = value
    else:
      confidence = 0.95
    out[name] = ParsedField(value=value, confidence=confidence)
  return out


def _primeira_form(parsed=None):
  return {
    "parsed": parsed or {},
    "processing_id": None,
    "pdf_bytes": b"%PDF-1.4\n",
    "doc_type": DocType.FPB_MODELO_1,
    "reg_type": 1,
    "tier_id": 5,
    "gender_id": 1,
  }


class _PreviewClient:
  def __init__(self, mini=None, mini_error=None):
    self.mini = mini
    self.mini_error = mini_error

  def list_concelhos(self, distrito_id):
    return {}

  def list_player_registration_batches(self):
    return [type("Batch", (), {"number": "2025/12", "tier": "Mini 10"})()]

  def primeira_estatuto_for_batch(self, batch):
    if self.mini_error is not None:
      raise self.mini_error
    if self.mini is None:
      return None
    return ESTATUTO_FBP if self.mini else ESTATUTO_SEM_FBP_COMUNITARIO


def _preview(monkeypatch, parsed, *, mini=None, mini_error=None):
  forms = {"m1": _primeira_form(parsed)}
  monkeypatch.setattr(
    server_module, "_get_client",
    lambda: _PreviewClient(mini=mini, mini_error=mini_error),
  )
  monkeypatch.setattr(server_module, "_forms", forms)
  monkeypatch.setattr(
    server_module, "_resolve_primeira_player",
    lambda client, form: {"resolved": True},
  )
  preview = server_module.preview_enrollment(
    batch_number="2025/12", license=None, mod1_id="m1",
  )
  return preview, forms["m1"]


# ── preview_enrollment ────────────────────────────────────────────────────────

class TestPreviewReturnsTheDecision:
  def test_mini_batch_reports_only_savs_rule_shape(self, monkeypatch):
    preview, _ = _preview(
      monkeypatch, _fields(nacionalidade="Portuguesa"), mini=True,
    )

    reason = (
      "SAV's op=151 rule for batch '2025/12' at tier 'Mini 10' selects "
      "estatuto 6 (FBP) for this 1ª Inscrição. SAV enforces this itself — "
      "the estatuto select is read-only for a club profile."
    )
    assert preview["estatuto_decision"] == {
      "value": ESTATUTO_FBP,
      "label": "FBP",
      "source": "sav_rule",
      "reason": reason,
    }
    assert "estatuto" not in preview["needs_review"]

  def test_absent_rule_omits_decision_and_review(self, monkeypatch):
    preview, _ = _preview(monkeypatch, {})

    assert "estatuto_decision" not in preview
    assert "estatuto" not in preview["needs_review"]

  def test_failure_reading_rule_leaves_preview_working_without_it(
    self, monkeypatch,
  ):
    preview, _ = _preview(
      monkeypatch, _fields(nacionalidade="Portuguesa"),
      mini_error=SavError("flag unavailable"),
    )

    assert "player" in preview
    assert "fields" in preview
    assert "estatuto_decision" not in preview


class TestPreviewAndTheNationalityTrap:
  def test_a_confirmed_portuguese_nationality_is_set(self, monkeypatch):
    preview, form = _preview(monkeypatch, _fields(nacionalidade="Portuguesa"))

    assert form["primeira_kwargs"]["nationality_id"] == 155
    assert "nationality_id" not in preview["needs_review"]

  def test_a_foreign_nationality_is_never_silently_filed_as_portuguese(
    self, monkeypatch,
  ):
    preview, form = _preview(monkeypatch, _fields(nacionalidade="Brasileira"))

    assert "nationality_id" not in form["primeira_kwargs"]
    assert "nationality_id" in preview["needs_review"]

  def test_an_unread_nationality_goes_to_review(self, monkeypatch):
    preview, form = _preview(monkeypatch, {})

    assert "nationality_id" not in form["primeira_kwargs"]
    assert "nationality_id" in preview["needs_review"]

  def test_a_low_confidence_portuguese_read_goes_to_review(self, monkeypatch):
    preview, form = _preview(
      monkeypatch, _fields(nacionalidade=("Portuguesa", 0.32)),
    )

    assert "nationality_id" not in form["primeira_kwargs"]
    assert "nationality_id" in preview["needs_review"]

  def test_savs_estatuto_rule_does_not_set_nationality(
    self, monkeypatch,
  ):
    """Estatuto and nationality remain separate fields."""
    preview, form = _preview(
      monkeypatch, _fields(nacionalidade="Brasileira"), mini=True,
    )

    assert preview["estatuto_decision"]["value"] == ESTATUTO_FBP
    assert preview["estatuto_decision"]["source"] == "sav_rule"
    assert "estatuto" not in preview["needs_review"]
    assert "nationality_id" not in form["primeira_kwargs"]

  def test_a_mini_batch_preview_uses_savs_fbp_rule(self, monkeypatch):
    preview, _ = _preview(
      monkeypatch, _fields(nacionalidade="Brasileira"), mini=True,
    )

    decision = preview["estatuto_decision"]
    assert decision["source"] == "sav_rule"
    assert decision["value"] == ESTATUTO_FBP
    assert set(decision) == {"value", "label", "source", "reason"}

  def test_a_failure_reading_the_sav_rule_keeps_preview_working(
    self, monkeypatch,
  ):
    preview, _ = _preview(
      monkeypatch,
      _fields(nacionalidade="Brasileira"),
      mini_error=SavError("flag unavailable"),
    )

    assert "estatuto_decision" not in preview


# ── add_enrollment ────────────────────────────────────────────────────────────

class _CommitClient:
  def __init__(self, seen):
    self._seen = seen

  def resolve_batch_id(self, number):
    return int(number)

  def list_player_registration_batch_items(self, batch_id):
    return []

  def add_player_to_registration_batch(self, batch_id, license, **kwargs):
    self._seen.update(kwargs)
    return 77


def _add(monkeypatch, decision_parsed, **call_kwargs):
  seen: dict = {}
  forms = {"m1": _primeira_form(decision_parsed)}
  mini = call_kwargs.pop("mini", None)
  mini_error = call_kwargs.pop("mini_error", None)
  monkeypatch.setattr(
    server_module, "_get_client",
    lambda: _PreviewClient(mini=mini, mini_error=mini_error),
  )
  monkeypatch.setattr(server_module, "_forms", forms)
  monkeypatch.setattr(
    server_module, "_resolve_primeira_player",
    lambda client, form: {"resolved": True},
  )
  server_module.preview_enrollment(
    batch_number="2025/12", license=None, mod1_id="m1",
  )
  monkeypatch.setattr(server_module, "_get_client", lambda: _CommitClient(seen))
  overrides = {"exam_date": "2026-05-01"}
  overrides.update(call_kwargs.pop("field_overrides", {}))
  result = server_module.add_enrollment(
    batch_number="12", license=None, mod1_id="m1",
    field_overrides=overrides, **call_kwargs,
  )
  return result, seen


class TestAddEnrollmentAcceptsAnOverride:
  @pytest.mark.parametrize(
    "estatuto",
    [ESTATUTO_FBP, ESTATUTO_SEM_FBP_COMUNITARIO, ESTATUTO_SEM_FBP_NAO_COMUNITARIO],
  )
  def test_the_override_rescues_the_submission(self, monkeypatch, estatuto):
    result, seen = _add(
      monkeypatch, {}, field_overrides={"estatuto": estatuto},
    )

    assert result["success"] is True
    assert seen["estatuto"] == estatuto

  def test_it_beats_the_estatuto_argument(self, monkeypatch):
    """field_overrides is the channel preview's needs_review points at."""
    _, seen = _add(
      monkeypatch, {},
      field_overrides={"estatuto": ESTATUTO_SEM_FBP_COMUNITARIO},
      estatuto=ESTATUTO_FBP,
    )

    assert seen["estatuto"] == ESTATUTO_SEM_FBP_COMUNITARIO

  def test_the_estatuto_argument_works_on_its_own(self, monkeypatch):
    _, seen = _add(monkeypatch, {}, estatuto=ESTATUTO_FBP)

    assert seen["estatuto"] == ESTATUTO_FBP

  def test_an_override_never_reaches_the_client_twice(self, monkeypatch):
    """Two channels for one parameter would be a TypeError at the call."""
    result, seen = _add(
      monkeypatch, {}, field_overrides={"estatuto": ESTATUTO_FBP},
    )

    assert result["success"] is True
    assert seen["estatuto"] == ESTATUTO_FBP

  @pytest.mark.parametrize("channel", ["argument", "override"])
  def test_equiparado_is_refused_from_a_caller(self, monkeypatch, channel):
    call = (
      {"estatuto": ESTATUTO_EQUIPARADO_FBP} if channel == "argument"
      else {"field_overrides": {"estatuto": ESTATUTO_EQUIPARADO_FBP}}
    )

    with pytest.raises(ValueError) as excinfo:
      _add(monkeypatch, {}, **call)

    message = str(excinfo.value)
    assert "Equiparado" in message
    assert "must come from FPB" in message

  def test_a_nonsense_id_is_refused(self, monkeypatch):
    with pytest.raises(ValueError, match="not a SAV estatuto id"):
      _add(monkeypatch, {}, estatuto=7)


class TestRevalidacaoIsUntouched:
  def test_preview_returns_no_estatuto_decision(self, monkeypatch):
    """Type-2 has a stored SAV selection and op=151's default behind it (see
    tests/test_estatuto_resolution.py); this path decides nothing for it."""
    class _Client:
      def load_player_profile(self, license, club_id=None):
        return {"nome": "Player A", "nasc": "1990-01-01"}

    form = _primeira_form(_fields(nacionalidade="Portuguesa"))
    form["reg_type"] = 2
    monkeypatch.setattr(server_module, "_get_client", lambda: _Client())
    monkeypatch.setattr(server_module, "_forms", {"m1": form})
    monkeypatch.setattr(
      server_module, "reconcile_fpb_mod1",
      lambda parsed, sav_profile, client=None: type("R", (), {
        "kwargs": {"license": 301772}, "needs_review": [],
        "retrain_corrections": {}, "updated": {}, "kept": {},
      })(),
    )

    preview = server_module.preview_enrollment(
      batch_number="2025/12", license=301772, mod1_id="m1",
    )

    assert "estatuto_decision" not in preview
    assert "estatuto" not in preview["needs_review"]
