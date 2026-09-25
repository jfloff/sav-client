import pytest

from sav_client import SavClient
from sav_client.models import NifLicenses, Player


@pytest.fixture
def client(monkeypatch, tmp_path):
  monkeypatch.setattr("sav_client.cache._CACHE_DIR", tmp_path)
  result = SavClient("https://example.invalid", "user", "pass")
  result.session = {"organizacao": 200, "epoca_id": 20}
  return result


def _player(
  license: int,
  name: str,
  birth_date: str = "2010-01-01",
  season: str = "2025/2026",
) -> Player:
  return Player(
    id=license,
    license=license,
    name=name,
    association="AB X",
    club="Club X",
    tier="Sub 14",
    gender="Masculino",
    birth_date=birth_date,
    nationality="Portuguesa",
    status="FBP",
    season=season,
  )


def _nif_result(monkeypatch, client, licenses, *, complete=True):
  monkeypatch.setattr(
    client,
    "find_licenses_by_nif",
    lambda nif: NifLicenses(licenses=list(licenses), complete=complete),
  )


def test_same_person_on_two_licenses_returns_newest_and_other_license(
  monkeypatch, client,
):
  players = {
    101: _player(101, "Ána Silva", season="2023/2024"),
    102: _player(102, "Ana Silva", season="2025/2026"),
  }
  _nif_result(monkeypatch, client, [101, 102])
  calls = []

  def search_players(**kwargs):
    calls.append(kwargs)
    return [players[int(kwargs["license"])]]

  monkeypatch.setattr(client, "search_players", search_players)

  result = client.resolve_player_identity(nif="123456789", club=0)

  assert result.status == "found"
  assert result.player == players[102]
  assert result.other_licenses == [players[101]]
  assert result.matched_by == ["nif"]
  assert calls == [
    {"license": "101", "club": 200, "season": 0, "status": "all"},
    {"license": "102", "club": 200, "season": 0, "status": "all"},
  ]


def test_siblings_sharing_nif_are_ambiguous_and_birth_date_resolves_them(
  monkeypatch, client,
):
  sibling_a = _player(101, "Ana Silva", "2010-01-01")
  sibling_b = _player(102, "Rita Silva", "2012-02-03")
  _nif_result(monkeypatch, client, [101, 102])

  def search_players(**kwargs):
    if "license" in kwargs:
      return [sibling_a if kwargs["license"] == "101" else sibling_b]
    if kwargs.get("birth_date"):
      return [sibling_b]
    raise AssertionError(f"unexpected search: {kwargs}")

  monkeypatch.setattr(client, "search_players", search_players)

  ambiguous = client.resolve_player_identity(nif="123456789")
  resolved = client.resolve_player_identity(
    nif="123456789", birth_date="2012-02-03",
  )

  assert ambiguous.status == "ambiguous"
  assert ambiguous.player is None
  assert ambiguous.candidates == [sibling_b, sibling_a]
  assert resolved.status == "found"
  assert resolved.player == sibling_b
  assert resolved.matched_by == ["nif", "birth_date"]


def test_same_birth_date_candidates_can_be_resolved_by_name(monkeypatch, client):
  anna = _player(101, "Anna Silva")
  rita = _player(102, "Rita Silva")
  monkeypatch.setattr(
    client, "search_players", lambda **kwargs: [anna, rita],
  )

  result = client.resolve_player_identity(
    name="rita silva", birth_date="2010-01-01",
  )

  assert result.status == "found"
  assert result.player == rita
  assert result.matched_by == ["name", "birth_date"]


def test_placeholder_only_is_ignored_as_an_identifier(monkeypatch, client):
  monkeypatch.setattr(
    client, "search_players",
    lambda **kwargs: pytest.fail("placeholder NIF must not be searched"),
  )
  monkeypatch.setattr(
    client, "find_licenses_by_nif",
    lambda *args, **kwargs: pytest.fail("placeholder NIF must not be scanned"),
  )

  result = client.resolve_player_identity(nif="999 999 990")

  assert result.status == "not_found"
  assert result.placeholder_nif is True
  assert result.matched_by == []


def test_id_number_search_can_find_a_player(monkeypatch, client):
  player = _player(123, "Ana Silva")
  calls = []

  def search_players(**kwargs):
    calls.append(kwargs)
    return [player]

  monkeypatch.setattr(client, "search_players", search_players)

  result = client.resolve_player_identity(id_number="AB12345")

  assert result.status == "found"
  assert result.player == player
  assert calls == [{
    "number": "AB12345", "club": 200, "season": 0, "status": "all",
  }]


def test_name_without_birth_date_raises(client):
  with pytest.raises(ValueError, match="birth_date is required"):
    client.resolve_player_identity(name="Ana Silva")


def test_incomplete_nif_scan_and_no_hit_is_unknown(monkeypatch, client):
  _nif_result(monkeypatch, client, [], complete=False)

  result = client.resolve_player_identity(nif="123456789")

  assert result.status == "unknown"
  assert result.player is None


def test_unread_newer_licence_joins_the_confirmed_person(monkeypatch, client):
  # The person's newer licence 102 is in the birth-date results, but its
  # profile could not be read (NIF unknown), so the NIF set only holds the
  # older 101. 102 must not be lost: it joins 101's person, and the newest
  # licence wins — never the stale 101.
  old = _player(101, "Ana Silva", season="2023/2024")
  new = _player(102, "Ana Silva", season="2025/2026")
  _nif_result(monkeypatch, client, [101], complete=False)
  monkeypatch.setattr(client, "search_players", lambda **kwargs: [old, new])

  result = client.resolve_player_identity(
    nif="123456789", birth_date="2010-01-01", name="Ana Silva",
  )

  assert result.status == "found"
  assert result.player == new
  assert result.other_licenses == [old]
  assert result.nif_on_file == "match"


def _nifs_on_file(monkeypatch, client, nifs):
  monkeypatch.setattr(
    client._cache, "get_nifs_for_licenses",
    lambda licenses: {lic: nifs[lic] for lic in licenses if lic in nifs},
  )


@pytest.mark.parametrize("on_file, reason", [
  ("999999990", "different"), ("", "none"), (None, "unknown"),
])
def test_real_nif_finds_a_player_sav_holds_without_it(
  monkeypatch, client, on_file, reason,
):
  # The club registered the child with the placeholder (or no NIF, or the
  # licence was never scanned). Next season the parent looks them up by the
  # real NIF: SAV holds no licence for it, but it cannot contradict either,
  # so the birth date + name still find the child — flagged unconfirmed.
  child = _player(201, "Rita Costa", birth_date="2014-05-06")
  _nif_result(monkeypatch, client, [])
  _nifs_on_file(monkeypatch, client, {} if on_file is None else {201: on_file})
  monkeypatch.setattr(client, "search_players", lambda **kwargs: [child])

  result = client.resolve_player_identity(
    nif="123456789", birth_date="2014-05-06", name="Rita Costa",
  )

  assert result.status == "found"
  assert result.player == child
  assert result.nif_on_file == reason


def test_a_wrong_nif_in_sav_does_not_hide_a_name_and_birth_date_match(
  monkeypatch, client,
):
  # Verified live: SAV can hold someone else's NIF for a player. Looked up by
  # their real NIF + name + birth date, they must still be found — flagged
  # "different" — or the caller reads "not found" as "new player".
  child = _player(201, "Rita Costa", birth_date="2014-05-06")
  _nif_result(monkeypatch, client, [])
  _nifs_on_file(monkeypatch, client, {201: "111111111"})
  monkeypatch.setattr(client, "search_players", lambda **kwargs: [child])

  result = client.resolve_player_identity(
    nif="123456789", birth_date="2014-05-06", name="Rita Costa",
  )

  assert result.status == "found"
  assert result.player == child
  assert result.nif_on_file == "different"


def test_a_different_nif_rules_out_a_birth_date_only_candidate(monkeypatch, client):
  # A same-day birth date alone is too weak to override a contradicting NIF:
  # it would return an unrelated child.
  child = _player(201, "Rita Costa", birth_date="2014-05-06")
  _nif_result(monkeypatch, client, [])
  _nifs_on_file(monkeypatch, client, {201: "111111111"})
  monkeypatch.setattr(client, "search_players", lambda **kwargs: [child])

  result = client.resolve_player_identity(nif="123456789", birth_date="2014-05-06")

  assert result.status == "not_found"
  assert result.nif_on_file is None


def test_a_nif_match_outranks_nif_unknown_people(monkeypatch, client):
  # Same birth date, no name: the NIF-carrying child is the answer; an
  # unrelated placeholder-NIF child born the same day must not make it
  # ambiguous.
  ours = _player(301, "Rui Lopes", birth_date="2012-01-01")
  other = _player(302, "Tiago Reis", birth_date="2012-01-01")
  _nif_result(monkeypatch, client, [301])
  _nifs_on_file(monkeypatch, client, {301: "123456789", 302: "999999990"})
  monkeypatch.setattr(client, "search_players", lambda **kwargs: [ours, other])

  result = client.resolve_player_identity(nif="123456789", birth_date="2012-01-01")

  assert result.status == "found"
  assert result.player == ours
  assert result.nif_on_file == "match"


def test_forty_eight_federation_wide_birth_rows_make_single_person_unknown(
  monkeypatch, client,
):
  rows = [
    _player(license, "Same Person", season="2025/2026")
    for license in range(100, 148)
  ]
  calls = []

  def search_players(**kwargs):
    calls.append(kwargs)
    return rows

  monkeypatch.setattr(client, "search_players", search_players)

  result = client.resolve_player_identity(
    name="Same Person", birth_date="2010-01-01", club=0,
  )

  assert result.status == "unknown"
  assert result.player is None
  assert len(calls) == 1
  assert calls[0]["club"] == 0


def test_status_filters_the_chosen_licence_not_the_candidates(monkeypatch, client):
  # The newest licence is inactive. Filtering the searches by status first
  # would drop it and "find" the older active licence; status must instead
  # judge the licence the resolver chose.
  old = _player(101, "Ana Silva", season="2023/2024")
  new = _player(102, "Ana Silva", season="2025/2026")
  old = old.__class__(**{**old.__dict__, "active": True})
  searched_statuses = []

  def search_players(**kwargs):
    searched_statuses.append(kwargs["status"])
    return [old, new]

  monkeypatch.setattr(client, "search_players", search_players)

  active = client.resolve_player_identity(
    birth_date="2010-01-01", name="Ana Silva", status="active",
  )
  anyone = client.resolve_player_identity(
    birth_date="2010-01-01", name="Ana Silva", status="all",
  )

  assert set(searched_statuses) == {"all"}
  assert active.status == "not_found"
  assert anyone.status == "found" and anyone.player.license == 102
