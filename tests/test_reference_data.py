"""Unit tests for the reference_data() lookup export.

reference_data() is what the sav://lookups MCP resource serves, so these guard
the JSON shape apps build against: id-keyed maps become [{id, name}] lists,
tier windows fold in birth years for a season, and the season key is absent
without one.
"""
import json

from sav_shared.lookups import (
  DISTRITOS,
  DOC_TYPE_CHOICES,
  GENERO,
  GUARDIAN_RELATIONS,
  ID_TYPES,
  PLAYER_REGISTRATION_TIERS,
  REGISTRATION_TYPE_LABELS,
  TIER_AGE_RANGE_IN_SEASON,
  doc_type_to_tipo_doc,
  is_uploadable_doc_type,
  reference_data,
  tier_birth_years_for_season,
)


class TestBundleShape:
  """Every documented section is present and JSON-serializable."""

  def test_all_sections_present(self):
    data = reference_data()
    for key in (
      "genero", "registration_types", "distritos", "id_types",
      "guardian_relations", "doc_types", "player_registration_tiers",
      "tier_ages_in_season",
    ):
      assert key in data, key

  def test_round_trips_through_json(self):
    # No frozensets/tuples/enums leak through — the resource serializes to JSON.
    dumped = json.dumps(reference_data(2025), ensure_ascii=False)
    assert json.loads(dumped)["season_start_year"] == 2025


class TestIdNameLists:
  """Int-keyed maps are emitted as ordered [{id, name}] records, not dicts."""

  def test_genero(self):
    assert reference_data()["genero"] == [
      {"id": 1, "name": "Masculino"},
      {"id": 2, "name": "Feminino"},
    ]

  def test_preserves_int_ids_and_order(self):
    distritos = reference_data()["distritos"]
    assert [d["id"] for d in distritos] == list(DISTRITOS)
    assert all(isinstance(d["id"], int) for d in distritos)

  def test_covers_every_source_entry(self):
    data = reference_data()
    assert len(data["distritos"]) == len(DISTRITOS)
    assert len(data["registration_types"]) == len(REGISTRATION_TYPE_LABELS)
    assert len(data["id_types"]) == len(ID_TYPES)
    assert len(data["guardian_relations"]) == len(GUARDIAN_RELATIONS)
    assert len(data["genero"]) == len(GENERO)


class TestDocTypes:
  def test_lists_every_choice(self):
    values = [d["value"] for d in reference_data()["doc_types"]]
    assert values == list(DOC_TYPE_CHOICES)

  def test_uploadable_entries_carry_tipo_doc(self):
    for entry in reference_data()["doc_types"]:
      assert entry["uploadable"] == is_uploadable_doc_type(entry["value"])
      if entry["uploadable"]:
        assert entry["tipo_doc"] == doc_type_to_tipo_doc(entry["value"])
      else:
        assert "tipo_doc" not in entry


class TestPlayerRegistrationTiers:
  """Tier ids differ per gender, so the table stays nested per gender."""

  def test_nested_per_gender(self):
    tiers = reference_data()["player_registration_tiers"]
    assert {g["gender_id"] for g in tiers} == set(PLAYER_REGISTRATION_TIERS)
    for group in tiers:
      assert group["gender"] == GENERO[group["gender_id"]]
      source = PLAYER_REGISTRATION_TIERS[group["gender_id"]]
      assert {t["tier_id"] for t in group["tiers"]} == set(source)


class TestTierAges:
  def test_static_windows_without_season(self):
    data = reference_data()
    assert "season_start_year" not in data
    by_tier = {t["tier"]: t for t in data["tier_ages_in_season"]}
    assert set(by_tier) == set(TIER_AGE_RANGE_IN_SEASON)
    sub14 = by_tier["Sub 14"]
    assert (sub14["min_age"], sub14["max_age"]) == (13, 14)
    assert "birth_years" not in sub14

  def test_birth_years_match_helper_for_season(self):
    by_tier = {t["tier"]: t for t in reference_data(2025)["tier_ages_in_season"]}
    for tier in TIER_AGE_RANGE_IN_SEASON:
      assert by_tier[tier]["birth_years"] == tier_birth_years_for_season(tier, 2025)

  def test_bounded_tier_birth_year_bounds(self):
    sub14 = next(
      t for t in reference_data(2025)["tier_ages_in_season"] if t["tier"] == "Sub 14"
    )
    assert sub14["birth_years"] == [2013, 2012]
    assert sub14["min_birth_year"] == 2012
    assert sub14["max_birth_year"] == 2013

  def test_open_ended_tier_has_no_enumerable_birth_years(self):
    senior = next(
      t for t in reference_data(2025)["tier_ages_in_season"] if t["tier"] == "Sénior"
    )
    assert senior["max_age"] is None
    assert senior["birth_years"] is None
    assert senior["min_birth_year"] is None
    # Youngest eligible cohort: born 2026-19 = 2007 (or earlier).
    assert senior["max_birth_year"] == 2007
