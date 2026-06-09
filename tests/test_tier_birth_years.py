"""Unit tests for tier_birth_years_for_season.

Sources verified against:
  - FPB Comunicado 057 (Competições Nacionais Escalões de Formação 2025-2026)
  - ABP Regulamento de Provas e Calendarização 2025/26 §3 (Escalões Etários)
"""
import pytest

from sav_shared.lookups import (
  TIER_AGES_IN_SEASON,
  TIER_MIN_AGE_IN_SEASON,
  tier_birth_years_for_season,
)


class TestTierBirthYears2025_26:
  """Anchor case — directly from the published ABP/FPB table."""

  @pytest.mark.parametrize("tier,expected", [
    ("Baby-Basket", [2022, 2021, 2020]),
    ("Mini 8",      [2019, 2018]),
    ("Mini 10",     [2017, 2016]),
    ("Mini 12",     [2015, 2014]),
    ("Sub 14",      [2013, 2012]),
    ("Sub 16",      [2011, 2010]),
    ("Sub 18",      [2009, 2008]),
  ])
  def test_matches_published_table(self, tier, expected):
    assert tier_birth_years_for_season(tier, 2025) == expected


class TestTierBirthYearsShiftsWithSeason:
  """The same formula one season later — Sub-14 should advance by one year."""

  def test_sub14_advances_one_year(self):
    assert tier_birth_years_for_season("Sub 14", 2026) == [2014, 2013]

  def test_mini12_advances_one_year(self):
    assert tier_birth_years_for_season("Mini 12", 2026) == [2016, 2015]

  def test_baby_basket_keeps_three_year_span(self):
    assert tier_birth_years_for_season("Baby-Basket", 2026) == [2023, 2022, 2021]


class TestOpenEndedAndUnknown:
  def test_senior_is_open_ended_below(self):
    """Sénior has a lower-bound rule (Sub 18+) — not enumerable as a fixed list."""
    assert tier_birth_years_for_season("Sénior", 2025) is None
    assert "Sénior" in TIER_MIN_AGE_IN_SEASON

  def test_masters_returns_none_until_modelled(self):
    assert tier_birth_years_for_season("Masters / Veteranos", 2025) is None

  def test_bcr_returns_none_until_modelled(self):
    assert tier_birth_years_for_season("BCR", 2025) is None

  def test_unknown_tier_returns_none(self):
    assert tier_birth_years_for_season("Sub 99", 2025) is None


class TestFormativeTiersAlwaysReturnTwoYears:
  """Every Sub-X / Mini-X tier (excluding Baby-Basket) spans exactly two years."""

  @pytest.mark.parametrize("tier", [
    "Mini 8", "Mini 10", "Mini 12",
    "Sub 14", "Sub 16", "Sub 18",
  ])
  def test_returns_exactly_two_consecutive_years(self, tier):
    years = tier_birth_years_for_season(tier, 2025)
    assert years is not None
    assert len(years) == 2
    assert years[0] - years[1] == 1

  def test_baby_basket_spans_three_years(self):
    years = tier_birth_years_for_season("Baby-Basket", 2025)
    assert years is not None
    assert len(years) == 3
    assert years == [2022, 2021, 2020]


class TestAgeTableContract:
  """Guard against accidental mutation of the published windows."""

  def test_ages_in_season_are_frozensets(self):
    for tier, ages in TIER_AGES_IN_SEASON.items():
      assert isinstance(ages, frozenset), tier

  def test_sub_x_ages_are_x_and_x_minus_1(self):
    """Sub-X / Mini-X eligibility = ages X-1 and X reached during Y+1."""
    for tier, ages in TIER_AGES_IN_SEASON.items():
      if tier == "Baby-Basket":
        continue
      # Parse X from the tier name ("Sub 14" -> 14, "Mini 8" -> 8).
      x = int(tier.split()[-1])
      assert ages == frozenset({x - 1, x}), tier
