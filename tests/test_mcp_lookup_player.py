"""Offline MCP tests for the unified lookup_player tool."""

import pytest

from sav_client.models import Player
from sav_mcp import server as server_module


def _player(**overrides) -> Player:
  fields = dict(
    id=301772, license="301772", name="Roster Name",
    association="AB Test", club="Test Club", tier="Sub 14",
    gender="Masculino", birth_date="2012-06-08",
    nationality="Portuguesa", status="FBP", season="2025/2026",
    active=True, nif="111111111",
  )
  fields.update(overrides)
  return Player(**fields)


class _StubClient:
  def __init__(self):
    self.session = {"epoca_id": 100, "organizacao": 200}
    self.calls: list[dict] = []
    self.nif_calls: list[str] = []
    self.profile_calls: list[tuple[str, int | None]] = []

  def _recent_season_ids(self):
    return [100, 99]

  def find_license_by_nif(self, nif, *, refresh=False):
    self.nif_calls.append(nif)
    return 301772

  def search_players(self, **kwargs):
    self.calls.append(kwargs)
    return [_player()]

  def load_player_profile(self, license, *, club_id=None):
    self.profile_calls.append((license, club_id))
    return {"name": "Profile Name", "nif": "999999999", "email": "x@y.test"}


def test_lookup_player_by_nif(monkeypatch):
  stub = _StubClient()
  monkeypatch.setattr(server_module, "_get_client", lambda: stub)

  result = server_module.lookup_player(nif="123 456 789")

  assert result is not None
  assert result["license"] == "301772"
  assert stub.nif_calls == ["123456789"]
  assert stub.calls[0]["license"] == "301772"


def test_lookup_player_by_license(monkeypatch):
  stub = _StubClient()
  monkeypatch.setattr(server_module, "_get_client", lambda: stub)

  result = server_module.lookup_player(license=301772, status="all")

  assert result is not None
  assert stub.nif_calls == []
  assert stub.calls[0]["license"] == "301772"
  assert stub.calls[0]["status"] == "all"


@pytest.mark.parametrize(
  "kwargs",
  [
    {},
    {"nif": "123456789", "license": 301772},
  ],
)
def test_lookup_player_rejects_both_or_neither(monkeypatch, kwargs):
  stub = _StubClient()
  monkeypatch.setattr(server_module, "_get_client", lambda: stub)

  with pytest.raises(ValueError, match="exactly one"):
    server_module.lookup_player(**kwargs)

  assert stub.calls == []


def test_lookup_player_nests_profile_without_field_collisions(monkeypatch):
  stub = _StubClient()
  monkeypatch.setattr(server_module, "_get_client", lambda: stub)

  result = server_module.lookup_player(
    license=301772, with_profile=True, with_details=True,
  )

  assert result is not None
  assert result["name"] == "Roster Name"
  assert result["nif"] == "111111111"
  assert result["profile"]["name"] == "Profile Name"
  assert result["profile"]["nif"] == "999999999"
  assert stub.profile_calls == [("301772", 200)]


class _ProbeStubClient:
  """Stub that answers search_players conditionally by (club, season).

  ``hits`` maps ``(club, season)`` to the ``club_id`` a returned row should
  carry; a pair absent from ``hits`` is a miss (empty list). Every call is
  recorded in order so tests can assert probe order and which rungs/clubs
  were actually queried.
  """

  def __init__(self, hits: dict[tuple[int, int | None], int], organizacao: int = 200):
    self.session = {"epoca_id": 100, "organizacao": organizacao}
    self._hits = hits
    self.calls: list[tuple[int, int | None]] = []
    self.profile_calls: list[tuple[str, int | None]] = []

  def _recent_season_ids(self):
    return [100, 99]

  def search_players(self, **kwargs):
    club = kwargs["club"]
    season = kwargs["season"]
    self.calls.append((club, season))
    row_club_id = self._hits.get((club, season))
    if row_club_id is None:
      return []
    return [_player(club_id=row_club_id)]

  def load_player_profile(self, license, *, club_id=None):
    self.profile_calls.append((license, club_id))
    return {"name": "Profile Name"}


def test_lookup_player_club_zero_probes_session_club_first(monkeypatch):
  stub = _ProbeStubClient({(200, None): 200})
  monkeypatch.setattr(server_module, "_get_client", lambda: stub)

  result = server_module.lookup_player(license=301772, club_id=0)

  assert result is not None
  assert stub.calls[0] == (200, None)
  assert all(club != 0 for club, _ in stub.calls)


def test_lookup_player_club_zero_probe_hit_skips_federation_call(monkeypatch):
  stub = _ProbeStubClient({(200, None): 200})
  monkeypatch.setattr(server_module, "_get_client", lambda: stub)

  result = server_module.lookup_player(license=301772, club_id=0)

  assert result is not None
  assert stub.calls == [(200, None)]
  assert (0, None) not in stub.calls


def test_lookup_player_club_zero_probe_miss_falls_back_to_federation(monkeypatch):
  stub = _ProbeStubClient({(0, None): 999})
  monkeypatch.setattr(server_module, "_get_client", lambda: stub)

  result = server_module.lookup_player(license=301772, club_id=0)

  assert result is not None
  assert stub.calls == [(200, None), (0, None)]
  assert result["club_id"] == 999


def test_lookup_player_club_zero_all_seasons_rung_skips_own_club_probe(monkeypatch):
  # Own club (200) would answer at season=0 with a stale row if probed there
  # (a naive "probe every rung" implementation would return it), but the
  # all-seasons rung must skip the own-club probe and go straight to the
  # federation-wide search, which finds the player's current row at another
  # club (300).
  stub = _ProbeStubClient({
    (200, 0): 200,  # stale own-club row; must never be reached
    (0, 0): 300,    # correct current row at the other club
  })
  monkeypatch.setattr(server_module, "_get_client", lambda: stub)

  result = server_module.lookup_player(license=301772, club_id=0)

  assert result is not None
  assert (200, 0) not in stub.calls
  assert (0, 0) in stub.calls
  assert result["club_id"] == 300


def test_lookup_player_nif_with_club_zero_raises(monkeypatch):
  stub = _StubClient()
  monkeypatch.setattr(server_module, "_get_client", lambda: stub)

  with pytest.raises(ValueError, match="scoped to your own club"):
    server_module.lookup_player(nif="123456789", club_id=0)


def test_lookup_player_nif_with_another_club_raises(monkeypatch):
  """SAV2 only exposes a player's NIF to their own club, so another club's
  id would silently resolve to null — raise instead."""
  stub = _StubClient()
  monkeypatch.setattr(server_module, "_get_client", lambda: stub)

  with pytest.raises(ValueError, match="scoped to your own club"):
    server_module.lookup_player(nif="123456789", club_id=300)

  assert stub.nif_calls == []


def test_lookup_player_nif_with_own_club_id_is_accepted(monkeypatch):
  """club_id=<session club> is just an explicit spelling of "my club"."""
  stub = _StubClient()
  monkeypatch.setattr(server_module, "_get_client", lambda: stub)

  result = server_module.lookup_player(nif="123456789", club_id=200)

  assert result is not None
  assert result["license"] == "301772"
  assert stub.nif_calls == ["123456789"]


def test_lookup_player_nif_without_session_club_raises(monkeypatch):
  stub = _StubClient()
  stub.session = {"epoca_id": 100}  # no "organizacao"
  monkeypatch.setattr(server_module, "_get_client", lambda: stub)

  with pytest.raises(ValueError, match="scoped to your own club"):
    server_module.lookup_player(nif="123456789")

  assert stub.nif_calls == []


def test_lookup_player_license_without_session_club_raises(monkeypatch):
  stub = _StubClient()
  stub.session = {"epoca_id": 100}  # no "organizacao"
  monkeypatch.setattr(server_module, "_get_client", lambda: stub)

  with pytest.raises(ValueError, match="club_id=0"):
    server_module.lookup_player(license=301772, club_id=None)


def test_lookup_player_with_profile_uses_resolved_club_id(monkeypatch):
  stub = _ProbeStubClient({(0, None): 500})
  monkeypatch.setattr(server_module, "_get_client", lambda: stub)

  result = server_module.lookup_player(
    license=301772, club_id=0, with_profile=True,
  )

  assert result is not None
  assert stub.profile_calls == [("301772", 500)]


class _RosterProbeStubClient(_ProbeStubClient):
  """_ProbeStubClient that also answers the own-roster emptiness check.

  ``_resolve_rows`` asks whether the session club has any current-season roster
  with a filterless ``search_players(club=own_club)`` — no ``license`` and no
  ``season``. Those calls are recorded separately as ``roster_calls`` so tests
  can assert both that they happen (memoized: exactly once) and that they never
  happen for a single-club lookup.
  """

  def __init__(self, hits, *, roster_empty: bool = True, roster_error: bool = False,
               organizacao: int = 200):
    super().__init__(hits, organizacao=organizacao)
    self._roster_empty = roster_empty
    self._roster_error = roster_error
    self.roster_calls: list[int] = []

  def search_players(self, **kwargs):
    if "license" not in kwargs:
      self.roster_calls.append(kwargs["club"])
      if self._roster_error:
        raise RuntimeError("roster check exploded")
      return [] if self._roster_empty else [_player()]
    return super().search_players(**kwargs)


@pytest.fixture(autouse=True)
def _clear_season_empty_cache():
  server_module._SEASON_EMPTY_CACHE.clear()
  yield
  server_module._SEASON_EMPTY_CACHE.clear()


def test_lookup_player_club_zero_skips_current_rung_when_own_season_empty(monkeypatch):
  stub = _RosterProbeStubClient({(200, 99): 200}, roster_empty=True)
  monkeypatch.setattr(server_module, "_get_client", lambda: stub)

  result = server_module.lookup_player(license=301772, club_id=0)

  assert result is not None
  assert stub.roster_calls == [200]
  # Neither the rung-1 probe nor the rung-1 federation sweep is issued.
  assert (0, None) not in stub.calls
  assert (200, None) not in stub.calls
  assert stub.calls[0] == (200, 99)


def test_lookup_player_club_zero_runs_current_rung_when_own_season_populated(monkeypatch):
  stub = _RosterProbeStubClient({(0, None): 999}, roster_empty=False)
  monkeypatch.setattr(server_module, "_get_client", lambda: stub)

  result = server_module.lookup_player(license=301772, club_id=0)

  assert result is not None
  assert stub.roster_calls == [200]
  assert stub.calls == [(200, None), (0, None)]
  assert result["club_id"] == 999


def test_lookup_player_own_season_empty_check_is_memoized(monkeypatch):
  stub = _RosterProbeStubClient({(200, 99): 200}, roster_empty=True)
  monkeypatch.setattr(server_module, "_get_client", lambda: stub)

  assert server_module.lookup_player(license=301772, club_id=0) is not None
  assert server_module.lookup_player(license=301772, club_id=0) is not None

  assert stub.roster_calls == [200]


def test_lookup_player_single_club_never_checks_own_roster(monkeypatch):
  stub = _RosterProbeStubClient({(300, None): 300}, roster_empty=True)
  monkeypatch.setattr(server_module, "_get_client", lambda: stub)

  result = server_module.lookup_player(license=301772, club_id=300)

  assert result is not None
  assert stub.roster_calls == []
  assert stub.calls == [(300, None)]


def test_lookup_player_roster_check_failure_runs_current_rung(monkeypatch):
  """Fail open: a broken roster check must not skip rung 1."""
  stub = _RosterProbeStubClient({(0, None): 999}, roster_error=True)
  monkeypatch.setattr(server_module, "_get_client", lambda: stub)

  result = server_module.lookup_player(license=301772, club_id=0)

  assert result is not None
  assert stub.roster_calls == [200]
  assert stub.calls == [(200, None), (0, None)]


def test_lookup_player_club_zero_accepted_stale_row_when_own_season_empty(monkeypatch):
  """Documents the accepted regression, not a bug.

  See the "ACCEPTED REGRESSION" comment on the rung-1 skip in
  ``_resolve_rows``: with the own current season empty, a player who left this
  club last season and enrolled elsewhere this season resolves to their stale
  own-club previous-season row, because the rung-1 federation sweep that would
  have found the new club's current row is exactly what we skipped.
  """
  stub = _RosterProbeStubClient(
    {
      (200, 99): 200,   # stale own-club row from last season
      (0, None): 300,   # the player's real current row at another club
    },
    roster_empty=True,
  )
  monkeypatch.setattr(server_module, "_get_client", lambda: stub)

  result = server_module.lookup_player(license=301772, club_id=0)

  assert result is not None
  assert result["club_id"] == 200
  assert (0, None) not in stub.calls
