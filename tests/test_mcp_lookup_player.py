"""Offline MCP tests for the unified lookup_player tool."""

import json

import pytest

from sav_client import SavClient
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


def test_lookup_player_federation_wide_profile_reuses_exact_search_cache(
  monkeypatch, tmp_path,
):
  client = SavClient("https://sav2.fpb.pt", "user", "pass", cache_dir=tmp_path)
  client.session = {"epoca_id": 100, "organizacao": 200, "perfil": 1, "user": "t"}
  html = (
    "<table><tbody><tr>"
    '<td><button onclick="seeJogador(1949, 1)"></button></td>'
    "<td><i class='fa-color-activo'></i></td>"
    "<td>194998</td><td>Roster Name</td><td>AB Test</td>"
    "<td>Other Club</td><td>Sub 14</td><td>Masculino</td>"
    "<td>2025/2026</td><td>FBP</td><td>2012-06-08</td><td>Portuguesa</td>"
    "</tr></tbody></table>"
  )
  calls = []

  def fake_post_form(path, payload, params=None):
    calls.append((path, payload, params))
    if params == {"op": "1"}:
      return html
    if params == {"op": "2"}:
      assert payload["user_id"] == 1949
      return json.dumps({
        "msg": (
          '<img src="uploads/1949.jpg">'
          '<input id="nome" value="Profile Name">'
          '<input id="telem" value="912345678">'
          '<input id="nif" value="">'
        ),
      })
    if params == {"op": "168"}:
      # The season table, read once so the detail can scope `subida`.
      return json.dumps({
        "arrayEpoca": [{"id": "100", "descricao": "2026/2027", "activa": "1"}],
      })
    raise AssertionError(f"unexpected request: path={path!r} params={params!r}")

  monkeypatch.setattr(client, "_post_form", fake_post_form)

  def fail(*args, **kwargs):
    raise AssertionError("exact licence profile lookup must not scan federation clubs")

  monkeypatch.setattr(client, "_resolve_club_id_by_name", fail)
  monkeypatch.setattr(client, "list_clubs", fail)
  monkeypatch.setattr(server_module, "_get_client", lambda: client)

  result = server_module.lookup_player(
    license=194998, club_id=0, status="all",
    with_details=True, with_profile=True,
  )

  assert result is not None
  assert result["club_id"] == 0
  assert result["photo_url"] == "uploads/1949.jpg"
  assert result["mobile_phone"] == "912345678"
  assert result["nif"] == ""
  assert result["profile"] == {
    "nome": "Profile Name", "tele": "912345678",
  }
  assert client._cache.get_player_id(194998) == 1949
  assert [params for _, _, params in calls].count({"op": "1"}) == 1
  assert [params for _, _, params in calls].count({"op": "2"}) == 2
  assert [params for _, _, params in calls].count({"op": "168"}) == 1
  # The stub page has no "Inscrições" tab, so SAV's answer is unreadable.
  assert result["subida"] == {
    "status": "unknown", "tier_from": None, "tier_to": None, "approved_on": None,
  }


class _ProbeStubClient:
  """Stub that answers search_players conditionally by (club, season).

  ``hits`` maps ``(club, season)`` to the ``club_id`` a returned row should
  carry; a pair absent from ``hits`` is a miss (empty list). Every call is
  recorded in order so tests can assert which rungs and clubs were actually
  queried, and in what order.
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


def test_lookup_player_club_zero_searches_federation_wide_once_per_rung(monkeypatch):
  """club_id=0 goes straight to the federation-wide search.

  SAV2 matches a licence across every club natively, so there is nothing to
  gain from probing the session club first — the ladder issues exactly one
  club=0 search per rung.
  """
  stub = _ProbeStubClient({(0, None): 999})
  monkeypatch.setattr(server_module, "_get_client", lambda: stub)

  result = server_module.lookup_player(license=301772, club_id=0)

  assert result is not None
  assert stub.calls == [(0, None)]
  assert result["club_id"] == 999


def test_lookup_player_club_zero_never_queries_the_session_club(monkeypatch):
  """Every rung is federation-wide, including the all-seasons one.

  Own club (200) holds a stale row a probe would have returned; the player's
  current row lives at another club (300) and must win.
  """
  stub = _ProbeStubClient({
    (200, 0): 200,  # stale own-club row; must never be reached
    (0, 0): 300,    # correct current row at the other club
  })
  monkeypatch.setattr(server_module, "_get_client", lambda: stub)

  result = server_module.lookup_player(license=301772, club_id=0)

  assert result is not None
  assert all(club == 0 for club, _ in stub.calls)
  assert stub.calls == [(0, None), (0, 99), (0, 0)]
  assert result["club_id"] == 300


def test_lookup_player_explicit_club_id_is_still_scoped(monkeypatch):
  stub = _ProbeStubClient({(300, None): 300})
  monkeypatch.setattr(server_module, "_get_client", lambda: stub)

  result = server_module.lookup_player(license=301772, club_id=300)

  assert result is not None
  assert stub.calls == [(300, None)]


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
