"""The Estatuto FBP group on the Modelo 1 — filling it, reading it back, and
the two things that must not change.

Until 0.106.0 `fpb`, `semfpb_com` and `sem_fpb_naocom` were absent from
MOD1_FILL_MAPPING under a comment claiming they belonged to the club-insurance
branch. They do not — they are the Estatuto group — so every Modelo 1 this
package rendered went out with Estatuto blank.

Two invariants ride along with the fix: the "Seguro FPB" default is untouched
(it is a club insurance policy with no per-player input, unrelated to Estatuto),
and `estatuto` stays optional, so callers that pass none still render.
"""

import pytest

from sav_shared.estatuto import resolve_estatuto
from sav_shared.fpb_mod1 import (
  MOD1_FILL_MAPPING,
  _MOD1_REQUIRED_CORE,
  mod1_acroform_to_fields,
  read_mod1_acroform,
  render_mod1,
  validate_mod1_values,
)
from sav_shared.lookups import (
  ESTATUTO_EQUIPARADO_FBP,
  ESTATUTO_FBP,
  ESTATUTO_SEM_FBP_COMUNITARIO,
  ESTATUTO_SEM_FBP_NAO_COMUNITARIO,
)

_SEASON = "2026/2027"
_ON_FORM = [
  ESTATUTO_FBP, ESTATUTO_SEM_FBP_COMUNITARIO, ESTATUTO_SEM_FBP_NAO_COMUNITARIO,
]


def _render_and_read(values: dict) -> dict:
  pdf = render_mod1(values, season=_SEASON, validate=False)
  return mod1_acroform_to_fields(read_mod1_acroform(pdf))


class TestRoundTrip:
  @pytest.mark.parametrize("estatuto", _ON_FORM)
  def test_fill_then_read_gives_back_the_same_entity(self, estatuto):
    fields = _render_and_read({"estatuto": estatuto})

    assert resolve_estatuto(fields).value == estatuto

  @pytest.mark.parametrize("estatuto", _ON_FORM)
  def test_exactly_one_box_is_ticked(self, estatuto):
    """A second box would make the decision engine call the form conflicting."""
    fields = _render_and_read({"estatuto": estatuto})

    marked = [k for k in fields if k.startswith("estatuto_fbp_")]
    assert len(marked) == 1

  def test_a_label_fills_the_same_box_as_its_id(self):
    by_id = _render_and_read({"estatuto": ESTATUTO_SEM_FBP_NAO_COMUNITARIO})
    by_name = _render_and_read({"estatuto": "Sem FBP Não Comunitário"})

    assert by_id.keys() == by_name.keys()

  def test_no_estatuto_leaves_the_whole_group_blank(self):
    fields = _render_and_read({"nome": "Player A"})

    assert not [k for k in fields if k.startswith("estatuto_fbp_")]


class TestItIsOptional:
  def test_it_is_not_a_required_core_field(self):
    """A signed form may legitimately carry no Estatuto and have it settled
    before submission. Requiring it would break every existing caller."""
    assert "estatuto" not in _MOD1_REQUIRED_CORE

  def test_a_complete_form_without_one_still_validates(self):
    values = {key: "x" for key in _MOD1_REQUIRED_CORE}
    values.update({
      "tipo_inscricao": 1, "genero": 1, "escalao": "Sub 14", "tipo": 1,
      "nif": "277544319", "nasc": "1990-01-01", "dataval": "2030-01-01",
      "codpostal": "1300-536",
      "consent_data": True, "consent_communications": True,
      "consent_marketing": False,
    })

    assert validate_mod1_values(values) == []

  def test_it_is_in_the_fill_mapping(self):
    assert "estatuto" in MOD1_FILL_MAPPING


class TestEquiparadoHasNoBox:
  def test_it_is_refused_with_the_reason_rather_than_dropped(self):
    problems = validate_mod1_values({"estatuto": ESTATUTO_EQUIPARADO_FBP})

    reason = next(p for p in problems if p.startswith("estatuto="))
    assert "no box on the current Modelo 1" in reason
    assert "Equiparado FBP" in reason

  def test_local_inference_can_never_produce_it_either(self):
    """Belt and braces with the engine's own test: the form cannot express 12,
    so nothing that reads a form can conclude it."""
    fields = _render_and_read({"estatuto": ESTATUTO_FBP})

    assert resolve_estatuto(fields).value != ESTATUTO_EQUIPARADO_FBP

  def test_an_unknown_id_still_gets_the_generic_message(self):
    problems = validate_mod1_values({"estatuto": 99})

    assert "estatuto=99 is not a valid option" in problems


class TestInsuranceIsUnrelatedAndUnchanged:
  def test_seguro_fpb_is_still_ticked_on_every_form(self):
    pdf = render_mod1({"nome": "Player A"}, season=_SEASON, validate=False)

    assert read_mod1_acroform(pdf)["Seguro FPB"] == "/On"

  def test_it_stays_ticked_alongside_an_estatuto(self):
    pdf = render_mod1(
      {"estatuto": ESTATUTO_FBP}, season=_SEASON, validate=False,
    )
    raw = read_mod1_acroform(pdf)

    assert raw["Seguro FPB"] == "/On"
    assert raw["Seguro Clube"] != "/On"

  def test_insurance_is_not_a_caller_settable_value(self):
    for key in MOD1_FILL_MAPPING:
      assert "seguro" not in key.lower()
