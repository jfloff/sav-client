"""Unit tests for tier_birth_years_for_season and its inverse, tier_for_birth_date.

Sources verified against:
  - FPB Comunicado 057 (Competições Nacionais Escalões de Formação 2025-2026)
  - ABP Regulamento de Provas e Calendarização 2025/26 §3 (Escalões Etários)
"""
import pytest

from datetime import date

from sav_shared.lookups import (
  TIER_AGE_RANGE_IN_SEASON,
  tier_birth_years_for_season,
  tier_for_birth_date,
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
    # Modelled, but open-ended above: present in the table with no max age.
    assert TIER_AGE_RANGE_IN_SEASON["Sénior"] == (19, None)

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

  def test_ranges_are_int_pairs(self):
    for tier, age_range in TIER_AGE_RANGE_IN_SEASON.items():
      assert isinstance(age_range, tuple) and len(age_range) == 2, tier
      min_age, max_age = age_range
      assert isinstance(min_age, int), tier
      assert max_age is None or isinstance(max_age, int), tier

  def test_sub_x_range_is_x_minus_1_to_x(self):
    """Sub-X / Mini-X eligibility = ages X-1..X reached during Y+1."""
    for tier, age_range in TIER_AGE_RANGE_IN_SEASON.items():
      if tier in ("Baby-Basket", "Sénior"):
        continue
      # Parse X from the tier name ("Sub 14" -> 14, "Mini 8" -> 8).
      x = int(tier.split()[-1])
      assert age_range == (x - 1, x), tier


class TestTierForBirthDate:
  """The inverse: birth date → escalão for a season."""

  @pytest.mark.parametrize("birth_year,expected", [
    (2020, "Baby-Basket"),
    (2018, "Mini 8"),
    (2016, "Mini 10"),
    (2014, "Mini 12"),
    (2012, "Sub 14"),
    (2010, "Sub 16"),
    (2008, "Sub 18"),
    (2006, "Sénior"),
  ])
  def test_matches_published_table(self, birth_year, expected):
    assert tier_for_birth_date(f"{birth_year}-06-15", 2025) == expected

  def test_is_the_exact_inverse_of_tier_birth_years(self):
    """Every year the forward function enumerates must map back to its tier.

    The two functions read the same table from opposite ends; if they ever
    disagree, one of them is placing real players in the wrong escalão.
    """
    for tier in TIER_AGE_RANGE_IN_SEASON:
      years = tier_birth_years_for_season(tier, 2025)
      if years is None:
        continue
      for year in years:
        assert tier_for_birth_date(f"{year}-01-01", 2025) == tier

  def test_only_the_birth_year_matters(self):
    """Escalões are birth-year cohorts, so the day and month are ignored."""
    assert (tier_for_birth_date("2012-01-01", 2025)
            == tier_for_birth_date("2012-12-31", 2025) == "Sub 14")

  def test_new_year_can_cross_a_cohort_boundary(self):
    """A month apart across New Year is a different tier when the band ends there."""
    assert tier_for_birth_date("2013-12-31", 2025) == "Sub 14"
    assert tier_for_birth_date("2014-01-01", 2025) == "Mini 12"

  def test_accepts_date_objects_and_european_strings(self):
    """Same input tolerance as the rest of the read path."""
    assert tier_for_birth_date(date(2012, 6, 15), 2025) == "Sub 14"
    assert tier_for_birth_date("15-06-2012", 2025) == "Sub 14"

  @pytest.mark.parametrize("value", [None, "", "garbage", "12-2012"])
  def test_unusable_birth_dates_return_none(self, value):
    """Refuse to guess: a wrong escalão is a wrong federation record."""
    assert tier_for_birth_date(value, 2025) is None

  def test_too_young_for_any_modelled_tier_is_none(self):
    """Below Baby-Basket there is no escalão to offer."""
    assert tier_for_birth_date("2024-01-01", 2025) is None

  def test_sub_20_ages_come_back_as_senior(self):
    """Documented limitation, asserted so it stays a known one.

    `Sub 20` is deliberately absent from TIER_AGE_RANGE_IN_SEASON because its
    window would overlap Sénior's, so ages 19-20 answer "Sénior" even where a
    club would register the player as Sub 20. The caller reviews the default.
    """
    assert tier_for_birth_date("2007-01-01", 2025) == "Sénior"
    assert tier_for_birth_date("2006-01-01", 2025) == "Sénior"

  def test_season_shifts_the_answer(self):
    """The same player moves up as the season advances."""
    assert tier_for_birth_date("2012-06-15", 2025) == "Sub 14"
    assert tier_for_birth_date("2012-06-15", 2027) == "Sub 16"
