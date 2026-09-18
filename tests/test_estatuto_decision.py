"""The surviving Estatuto reference table and nationality helpers."""

import pytest

from sav_shared.estatuto import (
  COMMUNITY,
  COMMUNITY_COUNTRIES,
  NON_COMMUNITY,
  NON_COMMUNITY_COUNTRIES,
  is_portuguese_nationality,
  nationality_branch,
  resolve_country,
)
from sav_shared.lookups import ESTATUTOS, find_estatuto_id, reference_data


# ── The four SAV ids ──────────────────────────────────────────────────────────

class TestTheEstatutoTable:
  def test_it_matches_the_op151_dropdown_sav_actually_serves(self):
    """Pinned against the live capture rather than retyped from the UI."""
    from test_estatuto_resolution import OP151_WITH_DEFAULT
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
    assert find_estatuto_id("sem fbp comunitario") == 10
    # One character off is not "close enough" for a legal status.
    assert find_estatuto_id("Sem FBP Comunitari") is None
    assert find_estatuto_id("FB") is None


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
