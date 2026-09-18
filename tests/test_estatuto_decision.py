"""Determining an Estatuto FBP, and the guesses that must stay impossible.

Estatuto is a legally meaningful FPB classification written into a federation
record, so the interesting assertions here are the negative ones: what the
engine refuses to conclude. Above all it must never infer **FBP** — *Formação
Basquetebolística Portuguesa* — from citizenship. A Portuguese passport is
evidence for *Sem FBP Comunitário* and says nothing about where the player was
formed.

The four estatuto ids are pinned against the real op=151 capture that
tests/test_estatuto_resolution.py carries (batch 632884, 2026-09-13), so the
table here and the dropdown SAV actually serves cannot drift apart.
"""

import pytest

from sav_parsers.types import ParsedField

from sav_shared.estatuto import (
  COMMUNITY,
  COMMUNITY_COUNTRIES,
  ESTATUTO_OCR_ENTITY,
  LOW_CONFIDENCE,
  NON_COMMUNITY,
  NON_COMMUNITY_COUNTRIES,
  SOURCE_FPB,
  SOURCE_LOCAL_ELIGIBILITY,
  SOURCE_MODELO1,
  SOURCE_NATIONALITY,
  SOURCE_NONE,
  SOURCE_SAV_RULE,
  EstatutoDecision,
  apply_sav_batch_rule,
  is_portuguese_nationality,
  nationality_branch,
  resolve_country,
  resolve_estatuto,
)
from sav_shared.lookups import (
  ESTATUTO_EQUIPARADO_FBP,
  ESTATUTO_FBP,
  ESTATUTO_SEM_FBP_COMUNITARIO,
  ESTATUTO_SEM_FBP_NAO_COMUNITARIO,
  ESTATUTOS,
  find_estatuto_id,
  reference_data,
)


def _fields(**entities) -> dict:
  """Parsed Modelo 1 entities at high confidence unless a (value, conf) pair."""
  out = {}
  for name, value in entities.items():
    if isinstance(value, tuple):
      value, confidence = value
    else:
      confidence = 0.95
    out[name] = ParsedField(value=value, confidence=confidence)
  return out


def _ticked(estatuto_id: int, confidence: float = 0.95) -> dict:
  entity = next(e for e, i in ESTATUTO_OCR_ENTITY.items() if i == estatuto_id)
  return _fields(**{entity: (True, confidence)})


# ── The four SAV ids ──────────────────────────────────────────────────────────

class TestTheEstatutoTable:
  def test_it_matches_the_op151_dropdown_sav_actually_serves(self):
    """Pinned against the live capture rather than retyped from the UI."""
    from tests.test_estatuto_resolution import OP151_WITH_DEFAULT
    from sav_client.sav_client import SavClient

    assert ESTATUTOS == SavClient._parse_estatuto_options(OP151_WITH_DEFAULT)

  def test_reference_data_publishes_it(self):
    assert reference_data()["estatutos"] == [
      {"id": 6, "name": "FBP"},
      {"id": 10, "name": "Sem FBP Comunitário"},
      {"id": 11, "name": "Sem FBP Não Comunitário"},
      {"id": 12, "name": "Equiparado FBP"},
    ]

  def test_labels_resolve_to_ids_without_fuzzy_matching(self):
    assert find_estatuto_id("sem fbp comunitario") == ESTATUTO_SEM_FBP_COMUNITARIO
    # One character off is not "close enough" for a legal status.
    assert find_estatuto_id("Sem FBP Comunitari") is None
    assert find_estatuto_id("FB") is None


# ── Priority order, one step at a time ────────────────────────────────────────

class TestOfficialStatusWins:
  def test_it_beats_a_ticked_box(self):
    decision = resolve_estatuto(
      _ticked(ESTATUTO_FBP), fpb_status="Sem FBP Não Comunitário",
    )

    assert decision.value == ESTATUTO_SEM_FBP_NAO_COMUNITARIO
    assert decision.source == SOURCE_FPB
    assert decision.needs_review is False

  def test_it_beats_conflicting_boxes_too(self):
    """Step 1 answers before step 2 ever runs — FPB has already decided."""
    parsed = {**_ticked(ESTATUTO_FBP), **_ticked(ESTATUTO_SEM_FBP_COMUNITARIO)}

    assert resolve_estatuto(parsed, fpb_status=6).value == ESTATUTO_FBP

  def test_equiparado_is_accepted_from_fpb(self):
    """12 has no box on the form; FPB is the only place it can come from."""
    decision = resolve_estatuto({}, fpb_status=ESTATUTO_EQUIPARADO_FBP)

    assert decision.value == ESTATUTO_EQUIPARADO_FBP
    assert decision.needs_review is False

  def test_an_id_or_a_label_both_work(self):
    assert resolve_estatuto({}, fpb_status="10").value == 10
    assert resolve_estatuto({}, fpb_status="Sem FBP Comunitário").value == 10

  def test_an_unrecognised_status_is_a_review_not_a_default(self):
    decision = resolve_estatuto(
      _ticked(ESTATUTO_FBP), fpb_status="Formação Nacional (transitório)",
    )

    assert decision.value is None
    assert decision.needs_review is True
    assert decision.source == SOURCE_FPB
    assert "Formação Nacional (transitório)" in decision.reason


class TestConflictingBoxesGoToReview:
  def test_two_boxes_are_never_resolved_by_preference(self):
    parsed = {
      **_ticked(ESTATUTO_FBP),
      **_ticked(ESTATUTO_SEM_FBP_NAO_COMUNITARIO),
    }

    decision = resolve_estatuto(parsed)

    assert decision.value is None
    assert decision.needs_review is True
    assert decision.source == SOURCE_MODELO1
    assert "FBP" in decision.reason and "Sem FBP Não Comunitário" in decision.reason

  def test_a_nationality_does_not_break_the_tie(self):
    parsed = {
      **_ticked(ESTATUTO_FBP),
      **_ticked(ESTATUTO_SEM_FBP_COMUNITARIO),
      **_fields(nacionalidade="Portuguesa"),
    }

    assert resolve_estatuto(parsed).value is None


class TestLocalEligibilityNeverPromotes:
  def test_it_asks_for_review_instead_of_granting_fbp(self):
    decision = resolve_estatuto({}, local_fbp_eligible=True)

    assert decision.value is None
    assert decision.needs_review is True
    assert decision.source == SOURCE_LOCAL_ELIGIBILITY

  def test_it_outranks_a_ticked_box(self):
    """Apparent eligibility contradicts the form; FPB settles it, not us."""
    decision = resolve_estatuto(
      _ticked(ESTATUTO_SEM_FBP_COMUNITARIO), local_fbp_eligible=True,
    )

    assert decision.value is None
    assert decision.source == SOURCE_LOCAL_ELIGIBILITY

  def test_an_official_status_still_outranks_it(self):
    decision = resolve_estatuto({}, fpb_status=6, local_fbp_eligible=True)

    assert decision.value == ESTATUTO_FBP


class TestOneTickedBox:
  @pytest.mark.parametrize(
    "estatuto_id",
    [ESTATUTO_FBP, ESTATUTO_SEM_FBP_COMUNITARIO, ESTATUTO_SEM_FBP_NAO_COMUNITARIO],
  )
  def test_it_is_the_signed_forms_answer(self, estatuto_id):
    decision = resolve_estatuto(_ticked(estatuto_id))

    assert decision.value == estatuto_id
    assert decision.label == ESTATUTOS[estatuto_id]
    assert decision.source == SOURCE_MODELO1
    assert decision.needs_review is False

  def test_it_outranks_the_nationality_branch(self):
    parsed = {
      **_ticked(ESTATUTO_SEM_FBP_NAO_COMUNITARIO),
      **_fields(nacionalidade="Portuguesa"),
    }

    assert resolve_estatuto(parsed).value == ESTATUTO_SEM_FBP_NAO_COMUNITARIO

  def test_a_low_confidence_read_is_kept_and_said_out_loud(self):
    """Dropping it would lose the form's own answer; trusting it silently
    would file a status nobody read."""
    decision = resolve_estatuto(_ticked(ESTATUTO_FBP, confidence=0.42))

    assert decision.value == ESTATUTO_FBP
    assert decision.confidence == 0.42
    assert decision.needs_review is True
    assert "0.42" in decision.reason


class TestNationalityChoosesOnlyTheSemFbpBranch:
  def test_portuguese_nationality_yields_sem_fbp_comunitario(self):
    decision = resolve_estatuto(_fields(nacionalidade="Portuguesa"))

    assert decision.value == ESTATUTO_SEM_FBP_COMUNITARIO
    assert decision.source == SOURCE_NATIONALITY
    assert decision.needs_review is False

  def test_portuguese_nationality_never_yields_fbp(self):
    """The one inference that must be impossible: a Portuguese passport is not
    evidence of Portuguese basketball formation."""
    for text in ("Portuguesa", "Português", "Portugal"):
      assert resolve_estatuto(_fields(nacionalidade=text)).value != ESTATUTO_FBP

  def test_no_nationality_can_reach_fbp_or_equiparado(self):
    """Structural, not per-country: the branch has only two outcomes."""
    reachable = set()
    for country in COMMUNITY_COUNTRIES | NON_COMMUNITY_COUNTRIES:
      reachable.add(resolve_estatuto(_fields(nacionalidade=country)).value)

    assert reachable == {
      ESTATUTO_SEM_FBP_COMUNITARIO, ESTATUTO_SEM_FBP_NAO_COMUNITARIO,
    }

  def test_a_known_non_community_country_yields_11(self):
    decision = resolve_estatuto(_fields(nacionalidade="Americana"))

    assert decision.value == ESTATUTO_SEM_FBP_NAO_COMUNITARIO
    assert decision.needs_review is False

  def test_an_unrecognised_nationality_goes_to_review_not_to_11(self):
    """The negative inference by elimination is the trap: an OCR error and an
    unlisted spelling look exactly like a non-community country."""
    decision = resolve_estatuto(_fields(nacionalidade="Brasilerra"))

    assert decision.value is None
    assert decision.needs_review is True
    assert decision.source == SOURCE_NATIONALITY
    assert "Brasilerra" in decision.reason

  @pytest.mark.parametrize("country", ["Reino Unido", "Suíça", "México"])
  def test_a_country_on_neither_list_goes_to_review(self, country):
    """Absent from the Comunicado but holding an EU agreement of the kind it is
    drawn from — the absence could be an omission, so we do not decide."""
    assert resolve_estatuto(_fields(nacionalidade=country)).value is None

  def test_a_low_confidence_nationality_is_flagged(self):
    decision = resolve_estatuto(_fields(nacionalidade=("Portuguesa", 0.31)))

    assert decision.value == ESTATUTO_SEM_FBP_COMUNITARIO
    assert decision.needs_review is True
    assert "0.31" in decision.reason


class TestNoEvidence:
  def test_an_empty_form_has_no_fallback_value(self):
    decision = resolve_estatuto({})

    assert decision.value is None
    assert decision.label == ""
    assert decision.source == SOURCE_NONE
    assert decision.needs_review is True

  def test_none_is_accepted_as_an_absent_form(self):
    assert resolve_estatuto(None).value is None


# ── The decision object ───────────────────────────────────────────────────────

class TestEstatutoDecision:
  def test_needs_review_is_derived_from_a_null_value(self):
    assert EstatutoDecision(None, "", SOURCE_NONE, "x").needs_review is True

  def test_needs_review_is_derived_from_low_confidence(self):
    just_under = EstatutoDecision(6, "FBP", SOURCE_MODELO1, "x", LOW_CONFIDENCE - 0.01)
    at_floor = EstatutoDecision(6, "FBP", SOURCE_MODELO1, "x", LOW_CONFIDENCE)

    assert just_under.needs_review is True
    assert at_floor.needs_review is False

  def test_an_absent_confidence_is_not_a_low_one(self):
    """An official FPB status is not an OCR read and must not read as doubtful."""
    assert EstatutoDecision(6, "FBP", SOURCE_FPB, "x").needs_review is False

  def test_it_is_frozen(self):
    with pytest.raises(Exception):
      EstatutoDecision(6, "FBP", SOURCE_FPB, "x").value = 10

  def test_to_dict_carries_the_shape_consumers_read(self):
    payload = resolve_estatuto(_fields(nacionalidade="Portuguesa")).to_dict()

    assert payload == {
      "value": 10,
      "label": "Sem FBP Comunitário",
      "source": SOURCE_NATIONALITY,
      "reason": (
        "No Estatuto box was marked; nationality 'Portuguesa' matches the FPB "
        "community/cooperation list."
      ),
      "confidence": 0.95,
      "conflict": None,
      "needs_review": False,
    }

  def test_every_reason_is_a_sentence_a_human_can_act_on(self):
    for decision in (
      resolve_estatuto({}),
      resolve_estatuto({}, fpb_status="???"),
      resolve_estatuto({}, local_fbp_eligible=True),
      resolve_estatuto(_ticked(ESTATUTO_FBP)),
      resolve_estatuto(_fields(nacionalidade="Portuguesa")),
      resolve_estatuto(_fields(nacionalidade="Marciana")),
    ):
      assert decision.reason.endswith(".")
      assert len(decision.reason) > 20


class TestSavBatchRule:
  def test_mini_batch_replaces_a_nationality_decision(self):
    decision = resolve_estatuto(_fields(nacionalidade="Brasileira"))

    result = apply_sav_batch_rule(
      decision, mini=True, batch_number="2025/12", tier="Mini 10",
    )

    assert result.value == ESTATUTO_FBP
    assert result.source == SOURCE_SAV_RULE
    assert result.needs_review is False
    assert "2025/12" in result.reason
    assert "Mini 10" in result.reason
    assert "mini=True" in result.reason

  def test_non_mini_batch_replaces_a_nationality_decision(self):
    decision = resolve_estatuto(_fields(nacionalidade="Brasileira"))

    result = apply_sav_batch_rule(
      decision, mini=False, batch_number="2025/13", tier="Sub 14",
    )

    assert result.value == ESTATUTO_SEM_FBP_COMUNITARIO
    assert result.source == SOURCE_SAV_RULE
    assert result.needs_review is False
    assert "2025/13" in result.reason
    assert "Sub 14" in result.reason
    assert "mini=False" in result.reason

  @pytest.mark.parametrize(
    "source, value",
    [(SOURCE_MODELO1, ESTATUTO_FBP), (SOURCE_FPB, ESTATUTO_FBP)],
  )
  def test_an_official_answer_that_agrees_is_returned_unchanged(
    self, source, value,
  ):
    decision = EstatutoDecision(value, ESTATUTOS[value], source, "official")

    result = apply_sav_batch_rule(
      decision, mini=True, batch_number="2025/12", tier="Mini 10",
    )

    assert result is decision
    assert result.conflict is None

  @pytest.mark.parametrize(
    "source, value, label",
    [
      (SOURCE_MODELO1, ESTATUTO_SEM_FBP_COMUNITARIO, "signed form"),
      (SOURCE_FPB, ESTATUTO_SEM_FBP_COMUNITARIO, "FPB"),
    ],
  )
  def test_an_official_answer_that_disagrees_stays_but_needs_review(
    self, source, value, label,
  ):
    decision = EstatutoDecision(value, ESTATUTOS[value], source, "official")

    result = apply_sav_batch_rule(
      decision, mini=True, batch_number="2025/12", tier="Mini 10",
    )

    assert result.value == value
    assert result.source == source
    assert result.needs_review is True
    assert result.conflict is not None
    assert str(value) in result.conflict
    assert "6 (FBP)" in result.conflict
    assert "2025/12" in result.conflict
    assert label in result.conflict

  def test_no_decision_is_replaced_by_the_rule(self):
    decision = resolve_estatuto({})

    result = apply_sav_batch_rule(
      decision, mini=False, batch_number="2025/13", tier="Sub 14",
    )

    assert result.value == ESTATUTO_SEM_FBP_COMUNITARIO
    assert result.source == SOURCE_SAV_RULE
    assert result.conflict is None


# ── The country lists ─────────────────────────────────────────────────────────

class TestCountryLists:
  def test_portugal_is_added_explicitly(self):
    """The Comunicado lists countries *with agreements with* Portugal, so it
    omits Portugal; without this every Portuguese player falls to review."""
    assert "Portugal" in COMMUNITY_COUNTRIES
    assert nationality_branch("Portugal") == COMMUNITY

  def test_the_two_lists_are_disjoint(self):
    assert not (COMMUNITY_COUNTRIES & NON_COMMUNITY_COUNTRIES)

  @pytest.mark.parametrize(
    "text,country",
    [
      ("Portuguesa", "Portugal"),
      ("Brasileiro", "Brasil"),
      ("angolana", "Angola"),
      ("Cabo-verdiana", "Cabo Verde"),
      ("ucraniano", "Ucrânia"),
      ("Espanhola", "Espanha"),
      ("Brasil", "Brasil"),
    ],
  )
  def test_demonyms_and_country_names_both_resolve(self, text, country):
    assert resolve_country(text) == country

  def test_an_unknown_demonym_resolves_to_nothing(self):
    assert resolve_country("Marciana") is None
    assert nationality_branch("Marciana") is None

  def test_nothing_is_fuzzy_matched(self):
    """'Nigeriana' must not be rounded onto 'Nicarágua'."""
    assert resolve_country("Nigeriana") == "Nigéria"
    assert resolve_country("Nigerian") is None
    assert resolve_country("Portugesa") is None


class TestIsPortugueseNationality:
  def test_only_portugal_counts(self):
    assert is_portuguese_nationality("Portuguesa") is True
    assert is_portuguese_nationality("Português") is True
    assert is_portuguese_nationality("Portugal") is True

  def test_another_community_country_is_not_portugal(self):
    """A Sem FBP Comunitário estatuto is shared by the whole list; it is not an
    answer to 'is this player Portuguese?'."""
    assert is_portuguese_nationality("Brasileira") is False
    assert nationality_branch("Brasileira") == COMMUNITY

  def test_blank_and_unknown_are_not_portugal(self):
    assert is_portuguese_nationality(None) is False
    assert is_portuguese_nationality("") is False
    assert is_portuguese_nationality("Marciana") is False


class TestNationalityBranch:
  def test_it_reports_the_two_branches(self):
    assert nationality_branch("Cabo Verde") == COMMUNITY
    assert nationality_branch("Canadá") == NON_COMMUNITY
