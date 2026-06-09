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
    assert result["source"] == "club"
    assert result["step"] == "club + active"
    assert [pl["license"] for pl in result["players"]] == ["301772"]
    # Confirm we asked for both birth years and the next-season epoca_id.
    first_call = stub.calls[0]
    assert sorted(first_call["birth_year"]) == [2013, 2014]
    assert first_call["season"] == 101  # epoca_id + 1
    assert first_call["gender"] == 1

  def test_club_empty_falls_back_to_status_all(self, monkeypatch):
    p = _player("301773", "Atleta 2013", "Sub 14", "2013-03-10", active=False)
    stub = _StubClient(responses={(200, "all"): [p]})
    monkeypatch.setattr(server_module, "_get_client", lambda: stub)

    result = server_module.roster_for_escalao(tier_id=5, gender_id=1, when="next")

    assert result["source"] == "club"
    assert result["step"] == "club + all"
    assert len(stub.calls) == 2  # active failed, all succeeded

  def test_club_empty_falls_back_to_federation(self, monkeypatch):
    """Off-season case: club hasn't built next-season roster; federation has the cohort."""
    p = _player("301774", "Atleta 2014", "Sub 14", "2014-09-01", active=True)
    stub = _StubClient(responses={(0, "all"): [p]})
    monkeypatch.setattr(server_module, "_get_client", lambda: stub)

    result = server_module.roster_for_escalao(tier_id=5, gender_id=1, when="next")

    assert result["source"] == "federation"
    assert result["step"] == "federation + all"
    assert len(stub.calls) == 3
    # Last call must be the federation-wide one.
    assert stub.calls[-1]["club"] == 0
    assert stub.calls[-1]["status"] == "all"

  def test_all_empty_returns_empty_with_none_source(self, monkeypatch):
    stub = _StubClient(responses={})
    monkeypatch.setattr(server_module, "_get_client", lambda: stub)

    result = server_module.roster_for_escalao(tier_id=5, gender_id=1, when="next")

    assert result["source"] == "none"
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
    assert stub.calls[0]["season"] == 100  # epoca_id unchanged


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

    assert result["source"] == "federation"
    assert len(stub.calls) == 1  # only the federation step
    assert stub.calls[0]["club"] == 0

  def test_explicit_other_club_uses_that_club_in_cascade(self, monkeypatch):
    p = _player("301778", "X", "Sub 14", "2014-01-01")
    stub = _StubClient(responses={(999, "active"): [p]})
    monkeypatch.setattr(server_module, "_get_client", lambda: stub)

    result = server_module.roster_for_escalao(
      tier_id=5, gender_id=1, when="next", club_id=999,
    )

    assert result["source"] == "club"
    assert stub.calls[0]["club"] == 999
