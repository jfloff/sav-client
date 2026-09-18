"""The Estatuto FBP surface across the MCP boundary.

`preview_enrollment` must return the `estatuto_decision` shape drive-to-sav
renders, and `add_enrollment` must refuse to commit a 1ª Inscrição whose
Estatuto was never determined — because SAV will happily supply a default, and
a wrong legal classification filed silently is exactly the failure this whole
path exists to prevent.

The nationality trap is tested here too, and it is a separate field: SAV's
type-1 wizard defaults nationality to Portugal, so an unconfirmed nationality
does not leave a gap in the record, it files the player as Portuguese.
"""

import pytest

from sav_client.exceptions import SavError
from sav_parsers.types import DocType, ParsedField

from sav_mcp import server as server_module
from sav_shared.estatuto import SOURCE_MODELO1, SOURCE_NATIONALITY, SOURCE_NONE
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

  def primeira_estatuto_mini_flag(self, batch):
    if self.mini_error is not None:
      raise self.mini_error
    return self.mini


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
  def test_it_carries_every_key_drive_to_sav_reads(self, monkeypatch):
    preview, _ = _preview(monkeypatch, _fields(nacionalidade="Portuguesa"))

    decision = preview["estatuto_decision"]
    assert set(decision) == {
      "value", "label", "source", "reason", "confidence", "conflict",
      "needs_review",
    }
    assert decision["value"] == ESTATUTO_SEM_FBP_COMUNITARIO
    assert decision["label"] == "Sem FBP Comunitário"
    assert decision["source"] == SOURCE_NATIONALITY
    assert decision["needs_review"] is False
    assert decision["reason"]

  def test_a_ticked_box_is_reported_as_the_forms_answer(self, monkeypatch):
    preview, _ = _preview(monkeypatch, _fields(estatuto_fbp_fbp=True))

    assert preview["estatuto_decision"]["value"] == ESTATUTO_FBP
    assert preview["estatuto_decision"]["source"] == SOURCE_MODELO1

  def test_an_undetermined_estatuto_reaches_needs_review(self, monkeypatch):
    preview, _ = _preview(monkeypatch, {})

    assert preview["estatuto_decision"]["source"] == SOURCE_NONE
    assert preview["estatuto_decision"]["needs_review"] is True
    assert "estatuto" in preview["needs_review"]
    row = next(f for f in preview["fields"] if f["kwarg"] == "estatuto")
    assert row["status"] == "needs_review"
    assert row["final_value"] is None

  def test_a_determined_estatuto_is_not_asked_about(self, monkeypatch):
    preview, _ = _preview(monkeypatch, _fields(estatuto_fbp_sem_comunitario=True))

    assert "estatuto" not in preview["needs_review"]
    row = next(f for f in preview["fields"] if f["kwarg"] == "estatuto")
    assert row["status"] == "ocr"
    assert row["final_value"] == ESTATUTO_SEM_FBP_COMUNITARIO

  def test_it_is_cached_for_add_enrollment(self, monkeypatch):
    """Committed from the cache, so what is filed is what was shown."""
    preview, form = _preview(monkeypatch, _fields(estatuto_fbp_fbp=True))

    assert form["estatuto_decision"].value == ESTATUTO_FBP
    assert form["estatuto_decision"].to_dict() == preview["estatuto_decision"]


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

  def test_a_sem_fbp_comunitario_decision_does_not_set_nationality(
    self, monkeypatch,
  ):
    """SAV's batch rule deliberately overrides the engine's answer."""
    preview, form = _preview(
      monkeypatch, _fields(nacionalidade="Brasileira"), mini=None,
    )

    assert preview["estatuto_decision"]["value"] == ESTATUTO_SEM_FBP_COMUNITARIO
    assert preview["estatuto_decision"]["source"] == SOURCE_NATIONALITY
    assert "nationality_id" not in form["primeira_kwargs"]

    preview, form = _preview(
      monkeypatch, _fields(nacionalidade="Brasileira"), mini=True,
    )

    assert preview["estatuto_decision"]["value"] == ESTATUTO_FBP
    assert preview["estatuto_decision"]["source"] == "sav_rule"
    assert preview["estatuto_decision"]["needs_review"] is False
    assert "nationality_id" not in form["primeira_kwargs"]

  def test_a_mini_batch_preview_uses_savs_fbp_rule(self, monkeypatch):
    preview, _ = _preview(
      monkeypatch, _fields(nacionalidade="Brasileira"), mini=True,
    )

    decision = preview["estatuto_decision"]
    assert decision["source"] == "sav_rule"
    assert decision["value"] == ESTATUTO_FBP
    assert decision["needs_review"] is False

  def test_a_failure_reading_the_mini_flag_keeps_the_engine_decision(
    self, monkeypatch,
  ):
    preview, _ = _preview(
      monkeypatch,
      _fields(nacionalidade="Brasileira"),
      mini_error=SavError("flag unavailable"),
    )

    decision = preview["estatuto_decision"]
    assert decision["value"] == ESTATUTO_SEM_FBP_COMUNITARIO
    assert decision["source"] == SOURCE_NATIONALITY


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


class TestAddEnrollmentRefusesAnUndeterminedEstatuto:
  def test_it_refuses_and_gives_the_reason(self, monkeypatch):
    with pytest.raises(ValueError) as excinfo:
      _add(monkeypatch, {})

    message = str(excinfo.value)
    assert "no determined Estatuto FBP" in message
    assert "not enough information to determine an Estatuto" in message

  def test_the_refusal_names_the_ids_a_caller_may_pass(self, monkeypatch):
    with pytest.raises(ValueError) as excinfo:
      _add(monkeypatch, _fields(nacionalidade="Marciana"))

    message = str(excinfo.value)
    assert "field_overrides" in message
    assert "6='FBP'" in message
    assert "12=" not in message

  def test_nothing_is_committed(self, monkeypatch):
    class _Explode:
      def resolve_batch_id(self, number):
        pytest.fail("the batch must not be touched before the refusal")

    forms = {"m1": _primeira_form({})}
    monkeypatch.setattr(server_module, "_get_client", lambda: _PreviewClient())
    monkeypatch.setattr(server_module, "_forms", forms)
    monkeypatch.setattr(
      server_module, "_resolve_primeira_player",
      lambda client, form: {"resolved": True},
    )
    server_module.preview_enrollment(
      batch_number="2025/12", license=None, mod1_id="m1",
    )
    monkeypatch.setattr(server_module, "_get_client", lambda: _Explode())

    with pytest.raises(ValueError):
      server_module.add_enrollment(
        batch_number="12", license=None, mod1_id="m1",
        field_overrides={"exam_date": "2026-05-01"},
      )

  def test_a_low_confidence_box_also_needs_the_override(self, monkeypatch):
    """The form answered, but not legibly enough to file unconfirmed."""
    with pytest.raises(ValueError, match="no determined Estatuto FBP"):
      _add(monkeypatch, _fields(estatuto_fbp_fbp=(True, 0.41)))

  def test_a_conflict_reaches_the_commit_refusal(self, monkeypatch):
    with pytest.raises(ValueError) as excinfo:
      _add(
        monkeypatch,
        _fields(estatuto_fbp_sem_comunitario=True),
        mini=True,
      )

    message = str(excinfo.value)
    assert "The signed form marks estatuto 10" in message
    assert "SAV's rule requires estatuto 6" in message


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


class TestAddEnrollmentCommitsTheDeterminedDecision:
  def test_a_ticked_box_is_committed_without_an_override(self, monkeypatch):
    _, seen = _add(monkeypatch, _fields(estatuto_fbp_sem_nao_comunitario=True))

    assert seen["estatuto"] == ESTATUTO_SEM_FBP_NAO_COMUNITARIO

  def test_a_nationality_decision_is_committed_too(self, monkeypatch):
    _, seen = _add(monkeypatch, _fields(nacionalidade="Portuguesa"))

    assert seen["estatuto"] == ESTATUTO_SEM_FBP_COMUNITARIO


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
