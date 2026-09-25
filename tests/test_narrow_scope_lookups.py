"""Lookups whose scope was narrower than their flow — and facts re-fetched
from SAV that the caller already held.

The key case is a Revalidação athlete: by definition they have no row in the
current season (observed live 2026-09-25, licence 315784, last row 2025/2026),
so any current-season search for them comes back empty.
"""

from types import SimpleNamespace

import pytest

from sav_client.models import Player
from sav_client.sav_client import SavClient, record_subida_picks
from sav_mcp import server as server_module
from sav_shared.enrollment import resolve_subida_player
from sav_shared.players import gender_id_of, resolve_license_row


def _row(license=315784, gender="Feminino", gender_id=2, season="2025/2026"):
  return Player(
    id=900, license=license, name="Test Player", association="", club="",
    tier="Mini 10", gender=gender, birth_date="2016-01-01", nationality="",
    status="FBP", season=season, gender_id=gender_id,
  )


class _SeasonAwareClient:
  """A Revalidação athlete: no row unless the search spans every season."""

  def __init__(self, row):
    self.row = row
    self.calls = []

  def search_players(self, **kwargs):
    self.calls.append(kwargs)
    return [self.row] if kwargs.get("season") == 0 else []


class TestGenderFromTheRowInHand:
  """The standalone Subida flow reads the gender from the SAV row the player
  was matched on — no second lookup (it used to ask SAV again, current season
  only, and default an unknown label to Masculino)."""

  def test_resolve_subida_player_returns_the_row_it_matched(self):
    row = _row(season="2026/2027")
    client = SimpleNamespace(search_players=lambda **kwargs: [row])
    parsed = {"licenca_nr": SimpleNamespace(value="315784")}

    license, candidates, _name, _ocr, player = resolve_subida_player(
      parsed, client, club_id=2430,
    )

    assert (license, candidates, player) == (315784, [], row)

  def test_gender_comes_from_the_row(self):
    assert gender_id_of(_row(gender="Feminino", gender_id=2)) == 2

  def test_a_bare_row_is_resolved_from_its_label(self):
    assert gender_id_of(_row(gender="Masculino", gender_id=0)) == 1

  def test_an_unrecognised_gender_raises_instead_of_defaulting(self):
    with pytest.raises(ValueError, match="Unrecognised gender"):
      gender_id_of(_row(gender="?", gender_id=0))

  def test_the_gender_helper_is_gone(self):
    import sav_shared.enrollment as enrollment
    assert not hasattr(enrollment, "gender_id_for_license")


class TestTheOneLicenceLookup:
  """A licence with no current-season row (a Revalidação athlete) is found by
  the shared ladder that lookup_player, get_player and the CLI all use."""

  def test_walks_to_all_seasons(self):
    client = _SeasonAwareClient(_row())
    client._recent_season_ids = lambda: [65, 64]

    row = resolve_license_row(client, license=315784)

    assert row is not None and row.license == 315784
    assert [c["season"] for c in client.calls] == [None, 64, 0]

  def test_mcp_uses_the_shared_ladder(self):
    assert server_module._resolve_rows is resolve_license_row


class TestInlineSubidaGender:
  """add_enrollment's inline subida must not ask SAV for the gender: the
  Modelo 1 artifact and the gender-keyed lote already hold it."""

  def _client(self, monkeypatch, batch_gender):
    batch = SimpleNamespace(number="345", gender_id=batch_gender)
    monkeypatch.setattr(
      server_module, "_find_batch_by_number", lambda client, number: batch,
    )
    return object()

  def test_the_lote_gender_is_used(self, monkeypatch):
    client = self._client(monkeypatch, 2)
    assert server_module._enrollment_gender_id(client, {"gender_id": 2}, {}, "345") == 2

  def test_a_form_that_disagrees_with_the_lote_raises(self, monkeypatch):
    client = self._client(monkeypatch, 2)
    with pytest.raises(ValueError, match="gender_id=1 but lote 345 is gender_id=2"):
      server_module._enrollment_gender_id(client, {"gender_id": 1}, {}, "345")

  def test_the_form_gender_covers_a_lote_without_one(self, monkeypatch):
    client = self._client(monkeypatch, 0)
    assert server_module._enrollment_gender_id(client, {}, {"gender_id": 1}, "345") == 1

  def test_add_enrollment_no_longer_looks_the_gender_up(self):
    import inspect
    # The lookup that searched the current season only, and failed every
    # Revalidação with an inline subida.
    assert "gender_id_for_license" not in inspect.getsource(server_module.add_enrollment)


class TestProfileLicenceResolution:
  def test_a_cold_cache_finds_a_revalidacao_athlete(self, monkeypatch, tmp_path):
    monkeypatch.setattr("sav_client.cache._CACHE_DIR", tmp_path)
    client = SavClient("https://example.invalid", "user", "pass")
    client.session = {"user": "u", "perfil": 1, "organizacao": 2}
    seasonal = _SeasonAwareClient(_row())
    monkeypatch.setattr(client, "search_players", seasonal.search_players)
    monkeypatch.setattr(
      client, "_post_form",
      lambda *a, **k: '{"msg": "<input id=\\"nome\\" value=\\"Test Player\\">"}',
    )

    profile = client.load_player_profile(315784)

    assert profile.get("nome") == "Test Player"
    assert seasonal.calls == [
      {"license": "315784", "club": 0, "season": 0, "status": "all"},
    ]


class TestSubidaTierObservability:
  def _client(self, monkeypatch, options):
    client = SavClient("https://example.invalid", "user", "pass")
    monkeypatch.setattr(client, "_list_subida_tier_options", lambda internal_id: options)
    return client

  def test_a_committed_pick_is_recorded(self, monkeypatch):
    client = self._client(monkeypatch, [(3, "Sub 16"), (10, "Sub 18")])
    with record_subida_picks() as picks:
      assert client._pick_subida_tier(42, prefer_tier_id=3) == (3, "Sub 16")
    assert picks == [{
      "offered": [{"tier_id": 3, "name": "Sub 16"}, {"tier_id": 10, "name": "Sub 18"}],
      "requested_tier_id": 3,
      "committed": {"tier_id": 3, "name": "Sub 16"},
    }]

  def test_a_refused_pick_is_recorded_too(self, monkeypatch):
    from sav_client.exceptions import SavConfigError

    client = self._client(monkeypatch, [(3, "Sub 16")])
    with record_subida_picks() as picks:
      with pytest.raises(SavConfigError):
        client._pick_subida_tier(42, prefer_tier_id=10)
    assert picks[0]["committed"] is None
    assert picks[0]["offered"] == [{"tier_id": 3, "name": "Sub 16"}]

  def test_nothing_is_recorded_outside_the_block(self, monkeypatch):
    client = self._client(monkeypatch, [(3, "Sub 16")])
    client._pick_subida_tier(42)
    with record_subida_picks() as picks:
      pass
    assert picks == []


class TestTheLoteRowIsNotReadTwice:
  def _client(self, monkeypatch):
    from sav_client.models import PlayerRegistrationBatch
    client = SavClient.__new__(SavClient)
    client._cache = SimpleNamespace(
      get_batch_id_by_license=lambda lic: None,
      record_license_batch=lambda *a: None,
    )
    batch = PlayerRegistrationBatch(
      id=12, number="12", type_id=2, type="Revalidação",
      association_id=0, association="", club_id=0, club="",
      tier_id=3, tier="Sub 16", gender_id=1, gender="Masculino",
      state_id=8, state="Em Pagamento", state_date="2026-09-18",
      item_count=1, season_id=65, season="2026/2027",
    )
    item = {"license": 1, "name": "P", "subida": {
      "status": "pending", "tier_from": "Sub 16", "tier_to": "Sub 18", "approved_on": None,
    }}
    reads = []
    monkeypatch.setattr(client, "list_player_registration_batches", lambda season=None: [batch], raising=False)

    def items(batch_id):
      reads.append(batch_id)
      return [item]

    monkeypatch.setattr(client, "list_player_registration_batch_items", items, raising=False)
    return client, reads, item

  def test_status_reuses_the_row_the_resolver_found(self, monkeypatch):
    client, reads, item = self._client(monkeypatch)
    batch_id = client.resolve_batch_id_by_license(1, include_submitted=True)
    assert client.batch_item_subida(batch_id, 1) == item["subida"]
    assert reads == [12]  # one op=10, not two

  def test_a_write_invalidates_the_remembered_row(self, monkeypatch):
    client, reads, _item = self._client(monkeypatch)
    client._batch_memo, client._batch_memo_lock = {}, __import__("threading").Lock()
    batch_id = client.resolve_batch_id_by_license(1, include_submitted=True)
    client._invalidate_batch_memo()
    client.batch_item_subida(batch_id, 1)
    assert reads == [12, 12]
