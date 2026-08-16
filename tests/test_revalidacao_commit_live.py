"""Live regression guard for the Revalidação add-player commit path.

Nothing in the existing suite exercises a real, successful
add_player_to_registration_batch() commit (op=33 -> op=31 -> op=36) against
production SAV2 -- every prior live test only reaches guard-clause/error
paths (invalid batch, invalid licence, etc). This drives the real commit for
a minor tier (guardian fields required) and an adult tier, then immediately
reverses it (remove + delete) so nothing survives past the "Em construção"
draft state SAV2 requires a human to actually submit.

Fabricated guardian info is safe here: batches stay in "Em construção" until
a human submits them on the SAV2 site, so a create -> add -> remove -> delete
cycle has no lasting effect even when the add step momentarily writes
made-up guardian contact info. Step1/step2 overrides are left None ("keep
stored value") so no real stored data is ever overwritten.
"""
import pytest

from sav_client.exceptions import SavConfigError, SavResponseError

_FAKE_GUARDIAN = dict(
  guardian_name="Encarregado Teste",
  guardian_relation=1,
  guardian_phone="900000000",
  guardian_email="teste@example.com",
)


def _try_commit(client, batch, license):
  """Attempt one candidate. Retries once with fabricated guardian info if
  SAV2 reports the player is a minor. Returns the new userid, or None if
  this player has no applicable taxa (registration fee) option -- that's
  normal per-player variation (their competition division/estatuto may have
  none configured), not a bug.
  """
  attempts = [{}, _FAKE_GUARDIAN]
  for guardian_kwargs in attempts:
    try:
      return client.add_player_to_registration_batch(
        batch.id, license=license, exam_date="2026-08-01", **guardian_kwargs,
      )
    except SavConfigError:
      continue  # minor missing guardian fields -- retry with placeholders
    except SavResponseError as exc:
      if "No taxa options" in str(exc):
        return None
      raise
  return None


def _commit_first_workable_license(client, batch, *, max_attempts=5):
  eligible = sorted(client._list_revalidable_licenses(batch))
  assert eligible, (
    f"No revalidable licences in batch {batch.id} ({batch.tier} {batch.gender})"
  )

  for license in eligible[:max_attempts]:
    userid = _try_commit(client, batch, license)
    if userid:
      return license, userid

  pytest.skip(
    f"No candidate among the first {max_attempts} eligible licences had a "
    f"usable taxa option in batch {batch.id} ({batch.tier} {batch.gender})"
  )


class TestRevalidacaoCommitLive:
  @pytest.mark.parametrize("tier_id,gender_id,label", [
    (1, 2, "Sub 16 Feminino"),    # minor tier -- exercises the guardian path
    (18, 1, "Sénior Masculino"),  # adult tier -- exercises taxa variability
  ])
  def test_add_then_undo_real_player(self, client, tier_id, gender_id, label):
    batch_id = client.create_player_registration_batch(
      type=2, tier=tier_id, gender_id=gender_id,
    )
    try:
      batch = next(
        b for b in client.list_player_registration_batches() if b.id == batch_id
      )
      license, userid = _commit_first_workable_license(client, batch)
      assert userid > 0

      items = client.list_player_registration_batch_items(batch_id)
      assert any(item["license"] == license for item in items)

      client.remove_player_from_registration_batch(batch_id, license)
      items_after = client.list_player_registration_batch_items(batch_id)
      assert not any(item["license"] == license for item in items_after)
    finally:
      client.delete_player_registration_batch(batch_id)
