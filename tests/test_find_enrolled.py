"""_find_enrolled_in_matching_batches reads each candidate batch's items to
locate where a licence already sits. Those reads now fan out concurrently;
these tests pin that it still returns the right batch, preserves order, keeps
swallowing per-candidate errors, and actually runs off the main thread."""

import threading
from types import SimpleNamespace

from sav_client.exceptions import SavConnectionError
from sav_cli.cli import _find_enrolled_in_matching_batches


def _b(id, *, is_open=True, type_id=2, tier_id=5, gender_id=1):
  return SimpleNamespace(
    id=id, number=f"2025/{id}", is_open=is_open,
    type_id=type_id, tier_id=tier_id, gender_id=gender_id,
  )


class _Client:
  def __init__(self, batches, items_map, *, errors=()):
    self._batches = batches
    self._items_map = items_map
    self._errors = set(errors)
    self.threads: list[str] = []

  def list_player_registration_batches(self):
    return self._batches

  def list_player_registration_batch_items(self, batch_id):
    self.threads.append(threading.current_thread().name)
    if batch_id in self._errors:
      raise SavConnectionError("boom")
    return self._items_map.get(batch_id, [])


def test_returns_matching_batch_where_licence_enrolled():
  ref = _b(0)
  cand_a = _b(12)
  cand_b = _b(13)
  wrong_tier = _b(14, tier_id=99)  # not a candidate — filtered out
  client = _Client([cand_a, cand_b, wrong_tier], {13: [{"license": 301772}]})

  assert _find_enrolled_in_matching_batches(client, ref, 301772) is cand_b


def test_runs_off_the_main_thread_for_multiple_candidates():
  ref = _b(0)
  client = _Client([_b(12), _b(13)], {13: [{"license": 301772}]})

  _find_enrolled_in_matching_batches(client, ref, 301772)

  assert client.threads and all(t != "MainThread" for t in client.threads)


def test_swallows_per_candidate_read_errors():
  ref = _b(0)
  cand_a = _b(12)  # its item read raises — must be skipped, not fatal
  cand_b = _b(13)  # holds the player
  client = _Client([cand_a, cand_b], {13: [{"license": 301772}]}, errors={12})

  assert _find_enrolled_in_matching_batches(client, ref, 301772) is cand_b


def test_returns_none_when_licence_absent_everywhere():
  ref = _b(0)
  client = _Client([_b(12), _b(13)], {})

  assert _find_enrolled_in_matching_batches(client, ref, 301772) is None


def test_returns_none_when_no_matching_candidates():
  ref = _b(0, tier_id=5)
  # Only batch has a different tier → no candidates, no item reads at all.
  client = _Client([_b(12, tier_id=99)], {12: [{"license": 301772}]})

  assert _find_enrolled_in_matching_batches(client, ref, 301772) is None
  assert client.threads == []  # short-circuited before any item read
