"""The one date contract: tolerant when reading, strict when writing.

SAV2 sends ISO from its wizard/profile endpoints and DD-MM-YYYY from its
listing endpoints, so `to_iso` has to take either. `require_iso` guards the
other direction — anything a caller supplies that ends up in a SAV commit
body — where being permissive is what lets a wrong date reach the federation
and fail there with no legible reason.
"""

from datetime import date

import pytest

from sav_shared.dates import require_iso, split_date_parts, to_date, to_iso
from sav_shared.games import game_sort_key
from sav_shared.text import iso_date


class TestToIso:
  """Read path — accept what SAV or OCR actually sends."""

  @pytest.mark.parametrize("value", ["2026-08-27", "27-08-2026", "27/08/2026", "27.08.2026"])
  def test_normalises_every_accepted_spelling(self, value):
    assert to_iso(value) == "2026-08-27"

  def test_iso_input_is_not_swapped(self):
    # The regression: iso_date used to return "27-08-2026" here — a plausible
    # date string, so nothing downstream noticed.
    assert to_iso("2026-08-27") == "2026-08-27"
    assert iso_date("2026-08-27") == "2026-08-27"

  def test_iso_date_alias_still_converts_european(self):
    assert iso_date("27-08-2026") == "2026-08-27"

  def test_unparseable_passes_through_untouched(self):
    assert to_iso("adiado") == "adiado"

  def test_blank_and_none(self):
    assert to_iso("") == ""
    assert to_iso(None) == ""

  def test_two_digit_year_is_not_guessed(self):
    assert to_iso("27-08-26") == "27-08-26"


class TestToDate:
  def test_parses_both_orders(self):
    assert to_date("2026-08-27") == date(2026, 8, 27)
    assert to_date("27-08-2026") == date(2026, 8, 27)

  def test_date_passes_through(self):
    assert to_date(date(2026, 8, 27)) == date(2026, 8, 27)

  @pytest.mark.parametrize("value", ["", None, "nonsense", "2026-99-99", "27-08-26"])
  def test_unusable_values_return_none(self, value):
    assert to_date(value) is None

  def test_split_parts_refuses_ambiguous_input(self):
    assert split_date_parts("27-08-26") is None
    assert split_date_parts("2026-08") is None


class TestRequireIso:
  """Write path — one shape, enforced at the boundary."""

  def test_accepts_iso(self):
    assert require_iso("2026-08-27", field="exam_date") == "2026-08-27"

  def test_strips_surrounding_whitespace(self):
    assert require_iso("  2026-08-27 ", field="exam_date") == "2026-08-27"

  @pytest.mark.parametrize(
    "value",
    [
      "27-08-2026",   # European — rejected, NOT converted
      "27/08/2026",
      "20260827",     # compact form fromisoformat() accepts from 3.11
      "2026-8-1",     # unpadded
      "2026-99-99",
      "",
      None,
      "adiado",
    ],
  )
  def test_rejects_everything_else(self, value):
    with pytest.raises(ValueError, match="must be YYYY-MM-DD"):
      require_iso(value, field="exam_date")

  def test_error_names_the_field(self):
    with pytest.raises(ValueError, match="id_expiry must be YYYY-MM-DD"):
      require_iso("27-08-2026", field="id_expiry")


class _Game:
  def __init__(self, d, t="10:00"):
    self.date = d
    self.time = t


class TestGameSortKey:
  def test_sorts_iso_dates_chronologically(self):
    games = [_Game("2026-08-27"), _Game("2026-01-05"), _Game("2025-12-31")]
    ordered = [g.date for g in sorted(games, key=game_sort_key)]
    assert ordered == ["2025-12-31", "2026-01-05", "2026-08-27"]

  def test_sorts_european_dates_chronologically(self):
    games = [_Game("27-08-2026"), _Game("05-01-2026"), _Game("31-12-2025")]
    ordered = [g.date for g in sorted(games, key=game_sort_key)]
    assert ordered == ["31-12-2025", "05-01-2026", "27-08-2026"]

  def test_time_breaks_ties(self):
    games = [_Game("2026-08-27", "18:30"), _Game("2026-08-27", "09:15")]
    assert [g.time for g in sorted(games, key=game_sort_key)] == ["09:15", "18:30"]

  def test_unparseable_date_sorts_last(self):
    games = [_Game(""), _Game("2026-08-27")]
    assert [g.date for g in sorted(games, key=game_sort_key)] == ["2026-08-27", ""]
