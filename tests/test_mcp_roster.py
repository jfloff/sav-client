"""MCP tests for roster_for_escalao — the deterministic escalão-roster tool.

The tool's job: given a tier_id + gender, resolve the right birth_years and
search players with an off-season fallback cascade. The tests below verify
each cascade step in isolation by varying which call returns players.
"""
import dataclasses

import pytest

from sav_client.models import Player
from sav_mcp import server as server_module


def _player(license: str, name: str, tier: str, birth_date: str, active: bool = True) -> Player:
  return Player(
    id=int(license), license=license, name=name,
    association="AB Test", club="Test Club",
    tier=tier, gender="Masculino",
    birth_date=birth_date, nationality="Portuguesa", status="FBP",
    season="2025/2026", active=active,
  )


class _StubClient:
  """Captures every search_players call and returns canned responses by step."""

  def __init__(self, *, epoca_id: int = 100, season_year: int = 2025, club_id: int = 200,
               responses: dict[tuple[int, str], list[Player]] | None = None):
    self.session = {"epoca_id": epoca_id, "organizacao": club_id}
    self._season_year = season_year
    self._responses = responses or {}
    self.calls: list[dict] = []

  def get_current_season_start_year(self) -> int:
    return self._season_year

  def search_players(self, **kwargs):
    self.calls.append(kwargs)
    key = (kwargs.get("club"), kwargs.get("status"))
    return list(self._responses.get(key, []))


class TestNextSeasonSub14Masculinos:
  """The production failure case: 'que jogadores são para o ano Sub-14 masculinos?'"""

  def test_club_active_hit_returns_with_club_source(self, monkeypatch):
    p = _player("301772", "Atleta 2014", "Sub 14", "2014-06-08")
    stub = _StubClient(responses={(200, "active"): [p]})
    monkeypatch.setattr(server_module, "_get_client", lambda: stub)

    result = server_module.roster_for_escalao(tier_id=5, gender_id=1, when="next")

    assert result["tier"] == "Sub 14"
    assert result["season"] == "2026/2027"
    assert result["birth_years"] == [2014, 2013]
    assert result["is_projection"] is True
    assert result["source"] == "projection_by_birth_year"
    assert result["step"] == "club + active"
    assert [pl["license"] for pl in result["players"]] == ["301772"]
    # We project from the *current* season's pool, filtered by next season's
    # birth years — there is no next-season enrollment to query.
    first_call = stub.calls[0]
    assert sorted(first_call["birth_year"]) == [2013, 2014]
    assert first_call["season"] == 100  # current epoca_id, not next
    assert first_call["gender"] == 1

  def test_club_empty_falls_back_to_status_all(self, monkeypatch):
    p = _player("301773", "Atleta 2013", "Sub 14", "2013-03-10", active=False)
    stub = _StubClient(responses={(200, "all"): [p]})
    monkeypatch.setattr(server_module, "_get_client", lambda: stub)

    result = server_module.roster_for_escalao(tier_id=5, gender_id=1, when="next")

    assert result["source"] == "projection_by_birth_year"
    assert result["step"] == "club + all"
    assert len(stub.calls) == 2  # active failed, all succeeded

  def test_club_empty_falls_back_to_federation(self, monkeypatch):
    """Club pool is empty; the wider federation pool has the cohort."""
    p = _player("301774", "Atleta 2014", "Sub 14", "2014-09-01", active=True)
    stub = _StubClient(responses={(0, "all"): [p]})
    monkeypatch.setattr(server_module, "_get_client", lambda: stub)

    result = server_module.roster_for_escalao(tier_id=5, gender_id=1, when="next")

    assert result["source"] == "projection_by_birth_year"
    assert result["step"] == "federation + all"
    assert len(stub.calls) == 3
    # Last call must be the federation-wide one.
    assert stub.calls[-1]["club"] == 0
    assert stub.calls[-1]["status"] == "all"

  def test_all_empty_returns_empty_projection_not_none(self, monkeypatch):
    """No known player projects into the cohort: still a projection, never 'none'."""
    stub = _StubClient(responses={})
    monkeypatch.setattr(server_module, "_get_client", lambda: stub)

    result = server_module.roster_for_escalao(tier_id=5, gender_id=1, when="next")

    assert result["is_projection"] is True
    assert result["source"] == "projection_by_birth_year"
    assert result["players"] == []
    assert result["step"] == "federation + all"  # last step attempted


class TestWhenCurrent:
  def test_uses_current_season_no_offset(self, monkeypatch):
    p = _player("301775", "Atleta 2012", "Sub 14", "2012-01-01")
    stub = _StubClient(responses={(200, "active"): [p]})
    monkeypatch.setattr(server_module, "_get_client", lambda: stub)

    result = server_module.roster_for_escalao(tier_id=5, gender_id=1, when="current")

    assert result["season"] == "2025/2026"
    assert result["birth_years"] == [2013, 2012]
    assert result["is_projection"] is False
    assert result["source"] == "club"  # actual enrollment, not a projection
    assert stub.calls[0]["season"] == 100  # epoca_id unchanged


class TestExplicitSeasonYear:
  """season_year names an absolute season and overrides `when`."""

  def test_past_season_is_actual_enrollment(self, monkeypatch):
    """A past season reflects real enrollment, queried at that season's own epoch."""
    p = _player("301779", "Atleta 2009", "Sub 14", "2009-05-05")
    stub = _StubClient(responses={(200, "active"): [p]})
    monkeypatch.setattr(server_module, "_get_client", lambda: stub)

    # current_year=2025, epoca_id=100 → 2020/2021 is 5 seasons back.
    result = server_module.roster_for_escalao(
      tier_id=5, gender_id=1, season_year=2020,
    )

    assert result["season"] == "2020/2021"
    # Sub 14 in 2020/2021 → born 2021−14 and 2022−14 = 2007, 2008.
    assert sorted(result["birth_years"]) == [2007, 2008]
    assert result["is_projection"] is False
    assert result["source"] == "club"  # real enrollment, not a projection
    # Queried that season's own epoch: 100 - (2025 - 2020) = 95.
    assert stub.calls[0]["season"] == 95
    assert sorted(stub.calls[0]["birth_year"]) == [2007, 2008]

  def test_season_year_overrides_when(self, monkeypatch):
    """season_year wins even if `when` says otherwise."""
    p = _player("301780", "X", "Sub 14", "2007-01-01")
    stub = _StubClient(responses={(200, "active"): [p]})
    monkeypatch.setattr(server_module, "_get_client", lambda: stub)

    result = server_module.roster_for_escalao(
      tier_id=5, gender_id=1, when="next", season_year=2020,
    )

    assert result["season"] == "2020/2021"
    assert result["is_projection"] is False
    assert stub.calls[0]["season"] == 95

  def test_future_season_year_projects_like_next(self, monkeypatch):
    """A season_year ahead of today is a projection over the current pool."""
    p = _player("301781", "Atleta 2012", "Sub 14", "2012-02-02")
    stub = _StubClient(responses={(200, "active"): [p]})
    monkeypatch.setattr(server_module, "_get_client", lambda: stub)

    # 2027/2028 is two seasons ahead; Sub 14 then → born 2028−14, 2029−14 = 2014, 2015.
    result = server_module.roster_for_escalao(
      tier_id=5, gender_id=1, season_year=2027,
    )

    assert result["season"] == "2027/2028"
    assert sorted(result["birth_years"]) == [2014, 2015]
    assert result["is_projection"] is True
    assert result["source"] == "projection_by_birth_year"
    # Projection queries the *current* pool (epoca_id 100), not the future epoch.
    assert stub.calls[0]["season"] == 100


class TestSenior:
  """Open-ended tier: filter by tier name, not birth_year."""

  def test_senior_filters_by_tier_not_birth_year(self, monkeypatch):
    p = _player("301776", "Atleta Sénior", "Sénior", "1995-04-04")
    stub = _StubClient(responses={(200, "active"): [p]})
    monkeypatch.setattr(server_module, "_get_client", lambda: stub)

    result = server_module.roster_for_escalao(tier_id=18, gender_id=1, when="current")

    assert result["birth_years"] is None
    assert "birth_year" not in stub.calls[0]
    assert stub.calls[0]["tier"] == "Sénior"


class TestUnknownTier:
  def test_bcr_raises_with_actionable_message(self, monkeypatch):
    stub = _StubClient()
    monkeypatch.setattr(server_module, "_get_client", lambda: stub)
    with pytest.raises(ValueError, match="BCR"):
      server_module.roster_for_escalao(tier_id=31, gender_id=1)

  def test_masters_raises_with_actionable_message(self, monkeypatch):
    stub = _StubClient()
    monkeypatch.setattr(server_module, "_get_client", lambda: stub)
    with pytest.raises(ValueError, match="Masters"):
      server_module.roster_for_escalao(tier_id=29, gender_id=1)


class TestInputValidation:
  def test_invalid_tier_id_for_gender(self, monkeypatch):
    stub = _StubClient()
    monkeypatch.setattr(server_module, "_get_client", lambda: stub)
    # tier_id=5 is Sub 14 *masculino*; gender_id=2 (feminino) renumbers it to 6.
    with pytest.raises(ValueError, match="tier_id=5 not valid for gender_id=2"):
      server_module.roster_for_escalao(tier_id=5, gender_id=2)

  def test_invalid_when(self, monkeypatch):
    stub = _StubClient()
    monkeypatch.setattr(server_module, "_get_client", lambda: stub)
    with pytest.raises(ValueError, match="when must be"):
      server_module.roster_for_escalao(tier_id=5, gender_id=1, when="last")


class TestExplicitClubId:
  def test_zero_club_skips_club_cascade(self, monkeypatch):
    p = _player("301777", "X", "Sub 14", "2014-01-01")
    stub = _StubClient(responses={(0, "all"): [p]})
    monkeypatch.setattr(server_module, "_get_client", lambda: stub)

    result = server_module.roster_for_escalao(
      tier_id=5, gender_id=1, when="next", club_id=0,
    )

    assert result["source"] == "projection_by_birth_year"
    assert result["step"] == "federation + all"
    assert len(stub.calls) == 1  # only the federation step
    assert stub.calls[0]["club"] == 0

  def test_explicit_other_club_uses_that_club_in_cascade(self, monkeypatch):
    p = _player("301778", "X", "Sub 14", "2014-01-01")
    stub = _StubClient(responses={(999, "active"): [p]})
    monkeypatch.setattr(server_module, "_get_client", lambda: stub)

    result = server_module.roster_for_escalao(
      tier_id=5, gender_id=1, when="next", club_id=999,
    )

    assert result["source"] == "projection_by_birth_year"
    assert result["step"] == "club + active"
    assert stub.calls[0]["club"] == 999
