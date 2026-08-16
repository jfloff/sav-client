"""Live regression guard for SavClient.get_current_season().

Nothing in the existing suite calls get_current_season() against the real
SAV2 site (tests/test_current_season.py only exercises it with monkeypatched
responses). This confirms the live label still parses cleanly. Deliberately
season-agnostic — no hardcoded "2026/2027" — so it keeps guarding future
rollovers too.
"""
import re


class TestCurrentSeasonLive:
  def test_resolves_active_season(self, client):
    season = client.get_current_season()

    assert re.fullmatch(r"\d{4}/\d{4}", season.label)
    assert season.start_year == int(season.label[:4])
    assert int(season.label[5:]) == season.start_year + 1
    assert season.is_active
