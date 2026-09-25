"""Offline MCP tests for the unified lookup_player tool."""

import json

import pytest

from sav_client import SavClient
from sav_client.models import IdentityMatch, Player
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
    self.identity_calls: list[dict] = []
    self.profile_calls: list[tuple[str, int | None]] = []
    self.identity_match: IdentityMatch | None = None

  def _recent_season_ids(self):
    return [100, 99]

  def resolve_player_identity(self, **kwargs):
    self.identity_calls.append(kwargs)
    return self.identity_match or IdentityMatch(
      status="found", player=_player(), other_licenses=[], candidates=[],
      matched_by=["nif"], placeholder_nif=False,
    )

  def search_players(self, **kwargs):
    self.calls.append(kwargs)
    return [_player()]

  def load_player_profile(self, license, *, club_id=None):
    self.profile_calls.append((license, club_id))
    return {"name": "Profile Name", "nif": "999999999", "email": "x@y.test"}

  # One op=2 page carries both the detail fields and the profile.
  _DETAIL = dict(photo_url="photo.jpg", mobile_phone="912000000", nif="111111111")

  def get_player_detail(self, player_id, *, with_details=False):
    self.detail_calls = getattr(self, "detail_calls", []) + [player_id]
    return _player(id=player_id, **self._DETAIL)

  def get_player_detail_and_profile(self, player_id):
    self.combined_calls = getattr(self, "combined_calls", []) + [player_id]
    return (
      _player(id=player_id, **self._DETAIL),
      {"name": "Profile Name", "nif": "999999999", "email": "x@y.test"},
    )


def test_identify_player_by_nif(monkeypatch):
  stub = _StubClient()
  monkeypatch.setattr(server_module, "_get_client", lambda: stub)

  result = server_module.identify_player(nif="123 456 789")

  assert result is not None
  assert result["license"] == "301772"
  assert result["matched_by"] == ["nif"]
  assert stub.identity_calls == [{
    "nif": "123456789", "id_number": None, "birth_date": None,
    "name": None, "club": None, "status": "active",
  }]
  assert stub.calls == []


def test_lookup_player_by_license(monkeypatch):
  stub = _StubClient()
  monkeypatch.setattr(server_module, "_get_client", lambda: stub)

  result = server_module.lookup_player(license=301772, status="all")

  assert result is not None
  assert stub.identity_calls == []
  assert stub.calls[0]["license"] == "301772"
  assert stub.calls[0]["status"] == "all"


def test_lookup_player_is_licence_only(monkeypatch):
  """Identity keys moved to identify_player: lookup_player takes a licence."""
  stub = _StubClient()
  monkeypatch.setattr(server_module, "_get_client", lambda: stub)

  with pytest.raises(TypeError):
    server_module.lookup_player()
  with pytest.raises(TypeError):
    server_module.lookup_player(nif="123456789")
  with pytest.raises(TypeError):
    server_module.lookup_player(license=301772, birth_date="2012-06-08")

  assert stub.calls == []
  assert stub.identity_calls == []


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
  # Both flags read the op=2 page once, by the row's internal id — no
  # separate profile load, and the search itself asked for no details.
  assert stub.combined_calls == [301772]
  assert stub.profile_calls == []
  assert all(not call.get("with_details") for call in stub.calls)


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
  # Details and profile come from ONE op=2 page (they used to fetch it twice).
  assert [params for _, _, params in calls].count({"op": "2"}) == 1
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


def test_identify_player_nif_with_club_zero_scopes_only_the_searches(monkeypatch):
  """The NIF is always resolved in the session club; club_id=0 only widens
  the doc-number / birth-date searches (a player registered elsewhere)."""
  stub = _StubClient()
  monkeypatch.setattr(server_module, "_get_client", lambda: stub)

  server_module.identify_player(nif="123456789", club_id=0)

  assert stub.identity_calls[0]["nif"] == "123456789"
  assert stub.identity_calls[0]["club"] == 0


def test_identify_player_nif_with_another_club_scopes_only_the_searches(monkeypatch):
  stub = _StubClient()
  monkeypatch.setattr(server_module, "_get_client", lambda: stub)

  server_module.identify_player(nif="123456789", club_id=300)

  assert stub.identity_calls[0]["club"] == 300


def test_identify_player_nif_with_own_club_id_is_accepted(monkeypatch):
  """club_id=<session club> is just an explicit spelling of "my club"."""
  stub = _StubClient()
  monkeypatch.setattr(server_module, "_get_client", lambda: stub)

  result = server_module.identify_player(nif="123456789", club_id=200)

  assert result is not None
  assert result["license"] == "301772"
  assert stub.identity_calls[0]["nif"] == "123456789"


def test_identify_player_nif_without_session_club_raises(monkeypatch):
  stub = _StubClient()
  stub.session = {"epoca_id": 100}  # no "organizacao"
  monkeypatch.setattr(server_module, "_get_client", lambda: stub)

  with pytest.raises(ValueError, match="scoped to your own club"):
    server_module.identify_player(nif="123456789")

  assert stub.identity_calls == []


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


def test_identify_player_nif_passes_active_status_without_season_ladder(monkeypatch):
  """Port of the old active-default season-rung case.

  Identity resolution gets the requested status directly and searches all
  identity evidence; lookup_player no longer walks a one-licence season ladder.
  """
  stub = _StubClient()
  stub.identity_match = IdentityMatch(
    status="found", player=_player(), other_licenses=[], candidates=[],
    matched_by=["nif"], placeholder_nif=False,
  )
  monkeypatch.setattr(server_module, "_get_client", lambda: stub)

  result = server_module.identify_player(nif="123456789")

  assert result is not None
  assert result["license"] == "301772"
  assert result["tier"] == "Sub 14"
  assert stub.identity_calls[0]["status"] == "active"
  assert stub.calls == []


def test_identify_player_nif_status_all_is_passed_to_identity_resolver(monkeypatch):
  pending = _player(
    license="301772", tier="Sub 16", season="2024/2025", active=False,
  )
  stub = _StubClient()
  stub.identity_match = IdentityMatch(
    status="found", player=pending, other_licenses=[], candidates=[],
    matched_by=["nif"], placeholder_nif=False,
  )
  monkeypatch.setattr(server_module, "_get_client", lambda: stub)

  result = server_module.identify_player(nif="123456789", status="all")

  assert result is not None
  assert result["tier"] == "Sub 16"
  assert result["season"] == "2024/2025"
  assert stub.identity_calls[0]["status"] == "all"
  assert stub.calls == []


def test_identify_player_nif_can_return_a_lapsed_identity_match(monkeypatch):
  lapsed = _player(
    license="301772", tier="Sub 18", season="2022/2023", active=True,
  )
  stub = _StubClient()
  stub.identity_match = IdentityMatch(
    status="found", player=lapsed, other_licenses=[], candidates=[],
    matched_by=["nif"], placeholder_nif=False,
  )
  monkeypatch.setattr(server_module, "_get_client", lambda: stub)

  result = server_module.identify_player(nif="123456789", status="active")

  assert result is not None
  assert result["season"] == "2022/2023"
  assert result["tier"] == "Sub 18"
  assert stub.identity_calls[0]["status"] == "active"
  assert stub.calls == []


def test_identify_player_nif_not_found_returns_none(monkeypatch):
  stub = _StubClient()
  stub.identity_match = IdentityMatch(
    status="not_found", player=None, other_licenses=[], candidates=[],
    matched_by=["nif"], placeholder_nif=False,
  )
  monkeypatch.setattr(server_module, "_get_client", lambda: stub)

  assert server_module.identify_player(nif="123456789", status="all") is None
  assert stub.identity_calls[0]["status"] == "all"
  assert stub.calls == []


def test_identify_player_malformed_nif_returns_none_before_client_lookup(monkeypatch):
  monkeypatch.setattr(
    server_module, "_get_client",
    lambda: pytest.fail("malformed NIF must short-circuit before client lookup"),
  )

  assert server_module.identify_player(nif="123") is None


def test_identify_player_nif_shared_by_same_person_returns_other_licenses(monkeypatch):
  older = _player(license="201001", season="2023/2024", active=False)
  newest = _player(license="301772", season="2025/2026", active=True)
  stub = _StubClient()
  stub.identity_match = IdentityMatch(
    status="found", player=newest, other_licenses=[older], candidates=[],
    matched_by=["nif"], placeholder_nif=False,
  )
  monkeypatch.setattr(server_module, "_get_client", lambda: stub)

  result = server_module.identify_player(nif="123456789")

  assert result is not None
  assert result["license"] == "301772"
  assert result["other_licenses"] == [
    {"license": "201001", "season": "2023/2024", "active": False},
  ]
  assert result["matched_by"] == ["nif"]
  assert "placeholder_nif" not in result  # nif_on_file is the only NIF field


def test_identify_player_nif_siblings_are_ambiguous_then_birth_date_narrows(
  monkeypatch,
):
  older_sibling = _player(
    license="301772", name="Ana Silva", birth_date="2012-06-08",
  )
  younger_sibling = _player(
    license="301773", name="Rita Silva", birth_date="2014-06-08",
  )
  stub = _StubClient()

  def resolve(**kwargs):
    stub.identity_calls.append(kwargs)
    if kwargs["birth_date"] == "2014-06-08":
      return IdentityMatch(
        status="found", player=younger_sibling, other_licenses=[],
        candidates=[], matched_by=["nif", "birth_date"],
        placeholder_nif=False,
      )
    return IdentityMatch(
      status="ambiguous", player=None, other_licenses=[],
      candidates=[younger_sibling, older_sibling], matched_by=["nif"],
      placeholder_nif=False,
    )

  stub.resolve_player_identity = resolve
  monkeypatch.setattr(server_module, "_get_client", lambda: stub)

  ambiguous = server_module.identify_player(nif="123456789")
  resolved = server_module.identify_player(
    nif="123456789", birth_date="2014-06-08",
  )

  assert ambiguous == {
    "ambiguous": True,
    "candidates": [
      server_module.player_to_dict(younger_sibling),
      server_module.player_to_dict(older_sibling),
    ],
    "matched_by": ["nif"],
  }
  assert resolved is not None
  assert resolved["license"] == "301773"
  assert resolved["matched_by"] == ["nif", "birth_date"]
  assert stub.identity_calls[1]["birth_date"] == "2014-06-08"


def test_identify_player_placeholder_nif_alone_is_a_plain_miss(monkeypatch):
  """999999990 identifies no one: alone it answers null, like any NIF miss."""
  stub = _StubClient()
  stub.identity_match = IdentityMatch(
    status="not_found", player=None, other_licenses=[], candidates=[],
    matched_by=[], placeholder_nif=True,
  )
  monkeypatch.setattr(server_module, "_get_client", lambda: stub)

  assert server_module.identify_player(nif="999999990") is None


def test_identify_player_surfaces_unknown_results(monkeypatch):
  stub = _StubClient()
  stub.identity_match = IdentityMatch(
    status="unknown", player=None, other_licenses=[], candidates=[],
    matched_by=["nif"], placeholder_nif=False,
  )
  monkeypatch.setattr(server_module, "_get_client", lambda: stub)

  result = server_module.identify_player(nif="123456789")

  assert result["error"] == "identity_unverifiable"
  assert result["matched_by"] == ["nif"]
  assert "neither found nor not-found" in result["detail"]


def test_identify_player_nif_with_details_hydrates_found_license(monkeypatch):
  found = _player(license="301772", photo_url="")
  stub = _StubClient()
  stub.identity_match = IdentityMatch(
    status="found", player=found, other_licenses=[], candidates=[],
    matched_by=["nif"], placeholder_nif=False,
  )
  monkeypatch.setattr(server_module, "_get_client", lambda: stub)

  result = server_module.identify_player(nif="123456789", with_details=True)

  assert result is not None
  assert result["photo_url"] == "photo.jpg"
  # The resolver's row already names the player: the detail page is read by
  # its internal id, with no second search for the licence.
  assert stub.detail_calls == [301772]
  assert stub.calls == []


def test_identify_player_by_id_number_returns_found_match(monkeypatch):
  stub = _StubClient()
  stub.identity_match = IdentityMatch(
    status="found", player=_player(), other_licenses=[], candidates=[],
    matched_by=["id_number"], placeholder_nif=False,
  )
  monkeypatch.setattr(server_module, "_get_client", lambda: stub)

  result = server_module.identify_player(id_number="12345678")

  assert result is not None
  assert result["license"] == "301772"
  assert result["matched_by"] == ["id_number"]
  assert stub.identity_calls == [{
    "nif": None, "id_number": "12345678", "birth_date": None,
    "name": None, "club": None, "status": "active",
  }]


def test_identify_player_requires_id_number_or_name_and_birth_date(monkeypatch):
  stub = _StubClient()
  monkeypatch.setattr(server_module, "_get_client", lambda: stub)

  with pytest.raises(ValueError, match="name needs birth_date"):
    server_module.identify_player(name="Ana Silva")
  with pytest.raises(ValueError, match="needs nif, id_number, or name and birth_date"):
    server_module.identify_player()

  assert stub.identity_calls == []


def test_identify_player_club_zero_passes_federation_scope(monkeypatch):
  stub = _StubClient()
  stub.identity_match = IdentityMatch(
    status="found", player=_player(), other_licenses=[], candidates=[],
    matched_by=["name", "birth_date"], placeholder_nif=False,
  )
  monkeypatch.setattr(server_module, "_get_client", lambda: stub)

  result = server_module.identify_player(
    name="Roster Name", birth_date="2012-06-08", club_id=0,
  )

  assert result is not None
  assert stub.identity_calls[0]["club"] == 0


def test_identify_player_reports_conflicts_on_a_nif_match(monkeypatch):
  stub = _StubClient()
  stub.identity_match = IdentityMatch(
    status="found", player=_player(), other_licenses=[], candidates=[],
    matched_by=["nif", "name"], placeholder_nif=False, nif_on_file="match",
    conflicts=[{"key": "birth_date", "given": "2009-09-28", "on_file": "2009-08-29"}],
  )
  monkeypatch.setattr(server_module, "_get_client", lambda: stub)

  result = server_module.identify_player(
    nif="123456789", name="Test", birth_date="2009-09-28",
  )

  assert result["license"] == "301772"
  assert result["conflicts"] == [
    {"key": "birth_date", "given": "2009-09-28", "on_file": "2009-08-29"},
  ]


def test_identify_player_omits_conflicts_when_every_key_agrees(monkeypatch):
  stub = _StubClient()
  monkeypatch.setattr(server_module, "_get_client", lambda: stub)

  result = server_module.identify_player(nif="123456789")

  assert "conflicts" not in result
