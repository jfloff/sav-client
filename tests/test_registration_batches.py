import pytest

from sav_client import SavClient
from sav_client.exceptions import SavResponseError
from sav_client.models import PlayerRegistrationBatch


# ─── helpers ────────────────────────────────────────────────────────────────

def _first_free_slot(client, type_id: int = 2) -> tuple[int, int] | None:
  """
  Return the first (tier_id, gender_id) for which the live account has no
  open batch of `type_id`. Used to create transient test batches without
  colliding with an open batch the user is actively building.
  """
  taken = {
    (b.tier_id, b.gender_id)
    for b in client.list_player_registration_batches()
    if b.is_open and b.type_id == type_id
  }
  for gender_id in (1, 2):
    for tier_id in client.list_player_registration_tiers(gender_id=gender_id):
      if (tier_id, gender_id) not in taken:
        return tier_id, gender_id
  return None


@pytest.fixture(scope="module")
def transient_batch(client):
  """
  Create a brand-new 'Em construção' Revalidação batch and clean it up
  after the module's tests finish. Picks a free (tier, gender) slot.
  """
  slot = _first_free_slot(client, type_id=2)
  if slot is None:
    pytest.skip("No free (tier, gender) slot to create a transient Revalidação batch")
  tier_id, gender_id = slot

  new_id = client.create_player_registration_batch(
    type=2, tier=tier_id, gender_id=gender_id,
  )
  try:
    yield {"id": new_id, "type_id": 2, "tier_id": tier_id, "gender_id": gender_id}
  finally:
    client.delete_player_registration_batch(new_id)


# ─── pre-HTTP guards ────────────────────────────────────────────────────────

class TestPreHttpGuards:
  """Validation paths that fire before any network call."""

  def test_list_requires_login(self):
    c = SavClient("https://sav2.fpb.pt", "user", "pass")
    with pytest.raises(SavResponseError, match="Must call login"):
      c.list_player_registration_batches()

  def test_tiers_requires_login(self):
    c = SavClient("https://sav2.fpb.pt", "user", "pass")
    with pytest.raises(SavResponseError, match="Must call login"):
      c.list_player_registration_tiers(gender_id=1)

  def test_create_requires_login(self):
    c = SavClient("https://sav2.fpb.pt", "user", "pass")
    with pytest.raises(SavResponseError, match="Must call login"):
      c.create_player_registration_batch(type=2, tier=5, gender_id=1)

  def test_delete_requires_login(self):
    c = SavClient("https://sav2.fpb.pt", "user", "pass")
    with pytest.raises(SavResponseError, match="Must call login"):
      c.delete_player_registration_batch(1)

  def test_remove_requires_login(self):
    c = SavClient("https://sav2.fpb.pt", "user", "pass")
    with pytest.raises(SavResponseError, match="Must call login"):
      c.remove_player_from_registration_batch(1, 1)

  def test_add_requires_login(self):
    c = SavClient("https://sav2.fpb.pt", "user", "pass")
    with pytest.raises(SavResponseError, match="Must call login"):
      c.add_player_to_registration_batch(1, 1)

  def test_tiers_rejects_invalid_gender(self, client):
    with pytest.raises(ValueError, match="gender_id must be 1"):
      client.list_player_registration_tiers(gender_id=0)

  def test_add_no_longer_accepts_exam_done_kwarg(self, client):
    """`exam_done` was removed in 0.10.5; passing it must raise TypeError."""
    with pytest.raises(TypeError):
      client.add_player_to_registration_batch(1, 1, exam_done=False)


# ─── live read-only ─────────────────────────────────────────────────────────

class TestListPlayerRegistrationBatches:
  def test_returns_well_formed_batches(self, client):
    results = client.list_player_registration_batches()

    assert isinstance(results, list)
    for batch in results:
      assert isinstance(batch, PlayerRegistrationBatch)
      assert batch.id > 0
      assert batch.type_id in (1, 2, 3, 4)
      assert batch.gender_id in (1, 2)
      assert batch.state_id >= 1
      assert isinstance(batch.is_open, bool)
      assert batch.is_open == (batch.state_id == 1)


class TestListPlayerRegistrationTiers:
  def test_male_and_female_tiers_returned(self, client):
    male = client.list_player_registration_tiers(gender_id=1)
    female = client.list_player_registration_tiers(gender_id=2)

    assert male and female
    for tier_id, name in male.items():
      assert isinstance(tier_id, int) and tier_id > 0
      assert isinstance(name, str) and name
    # The "Não selecionado" placeholder must be filtered out
    assert 0 not in male
    assert 0 not in female


class TestFindOpenPlayerRegistrationBatch:
  def test_returned_batch_satisfies_predicate(self, client):
    """Whatever find_open returns, it must match (open, type, tier, gender)."""
    for gender_id in (1, 2):
      for tier_id in client.list_player_registration_tiers(gender_id=gender_id):
        match = client.find_open_player_registration_batch(
          type=2, tier_id=tier_id, gender_id=gender_id,
        )
        if match is not None:
          assert match.is_open
          assert match.type_id == 2
          assert match.tier_id == tier_id
          assert match.gender_id == gender_id
          return
    pytest.skip("Account has no open Revalidação batch — predicate cannot be verified")

  def test_returns_none_for_impossible_tier(self, client):
    # 999999 cannot be a real tier — no batch can match
    assert client.find_open_player_registration_batch(
      type=2, tier_id=999999, gender_id=1,
    ) is None


# ─── create / delete (live, with cleanup) ───────────────────────────────────

class TestCreateAndDeletePlayerRegistrationBatch:
  def test_create_appears_in_list_with_expected_shape(self, client, transient_batch):
    batch = next(
      (b for b in client.list_player_registration_batches()
       if b.id == transient_batch["id"]),
      None,
    )
    assert batch is not None
    assert batch.is_open
    assert batch.type_id == 2
    assert batch.tier_id == transient_batch["tier_id"]
    assert batch.gender_id == transient_batch["gender_id"]
    assert batch.item_count == 0

  def test_independent_create_then_delete_cycle(self, client):
    slot = _first_free_slot(client, type_id=2)
    if slot is None:
      pytest.skip("No free (tier, gender) slot to create a delete-test batch")
    tier_id, gender_id = slot

    new_id = client.create_player_registration_batch(
      type=2, tier=tier_id, gender_id=gender_id,
    )
    try:
      assert new_id in {
        b.id for b in client.list_player_registration_batches()
      }
    finally:
      client.delete_player_registration_batch(new_id)

    assert new_id not in {
      b.id for b in client.list_player_registration_batches()
    }


# ─── add: validation paths against a real batch ─────────────────────────────
# Happy path is intentionally not exercised — it would commit a real player.

class TestAddPlayerToRegistrationBatchValidation:
  def test_unknown_batch_raises(self, client):
    with pytest.raises(ValueError, match=r"Batch id=\d+ not found"):
      client.add_player_to_registration_batch(999999999, 301772)

  def test_non_revalidacao_batch_raises(self, client):
    other = next(
      (b for b in client.list_player_registration_batches()
       if b.is_open and b.type_id != 2),
      None,
    )
    if other is None:
      pytest.skip("No open non-Revalidação batch on this account")

    with pytest.raises(NotImplementedError, match="Only Revalidação"):
      client.add_player_to_registration_batch(other.id, 301772)

  def test_closed_batch_raises(self, client):
    closed = next(
      (b for b in client.list_player_registration_batches() if not b.is_open),
      None,
    )
    if closed is None:
      pytest.skip("No closed batch on this account")

    with pytest.raises(ValueError, match="is not open"):
      client.add_player_to_registration_batch(closed.id, 301772)

  def test_licence_not_eligible_raises(self, client, transient_batch):
    with pytest.raises(ValueError, match="not eligible for revalidation"):
      client.add_player_to_registration_batch(transient_batch["id"], 999999999)


# ─── remove: live ───────────────────────────────────────────────────────────

class TestRemovePlayerFromRegistrationBatch:
  def test_unknown_batch_raises(self, client):
    with pytest.raises(ValueError, match=r"Batch id=\d+ not found"):
      client.remove_player_from_registration_batch(999999999, 301772)

  def test_licence_not_in_batch_raises(self, client, transient_batch):
    with pytest.raises(SavResponseError, match=r"Licence \d+ is not in batch"):
      client.remove_player_from_registration_batch(
        transient_batch["id"], 999999999,
      )

  def test_load_batch_items_parses_real_rows(self, client):
    """
    Find a batch with items on the live account and exercise the op=10
    parser end-to-end. Each row must include the per-row `item_id` parsed
    from `eliJogador(item_id, ...)` — that's the value the op=29 fix in
    0.10.4 depends on, so this is the source-of-truth check for it.
    """
    populated = next(
      (b for b in client.list_player_registration_batches() if b.item_count > 0),
      None,
    )
    if populated is None:
      pytest.skip("No batch with items on this account to exercise op=10 parser")

    items = client._load_batch_items(populated)

    assert items, (
      f"Batch {populated.id} reports {populated.item_count} items but "
      f"the op=10 parser found none — parser regression"
    )
    for it in items:
      assert isinstance(it["item_id"], int) and it["item_id"] > 0
      assert isinstance(it["license"], int) and it["license"] > 0
      assert isinstance(it["name"], str) and it["name"]
