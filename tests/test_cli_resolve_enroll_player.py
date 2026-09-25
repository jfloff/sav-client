"""Offline coverage for identity fallbacks in `_resolve_enroll_player`.

Both paths matter and neither was covered: a wrong answer here either enrols
the player into the wrong batch or drops through to a manual licence prompt in
the middle of an otherwise automatic run.
"""

from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from sav_cli import cli as cli_module
from sav_shared import enrollment as enrollment_module

_REAL_RESOLVE_PLAYER_CANDIDATES = cli_module.resolve_player_candidates


@pytest.fixture
def batch():
  return type(
    "BatchStub", (), {"id": 12, "number": "2025/12", "club_id": 99},
  )()


@pytest.fixture(autouse=True)
def _no_candidate_match(monkeypatch):
  """Force the eligible-list match to miss, which is what arms both fallbacks."""
  monkeypatch.setattr(
    cli_module, "resolve_player_candidates",
    lambda parsed, eligible, client, club_id: (None, [], "", None),
  )


class _Client:
  def _list_revalidable_licenses(self, batch):
    return []


def _identity(status, license=None, candidates=None):
  player = SimpleNamespace(license=license) if license is not None else None
  return SimpleNamespace(
    status=status, player=player, candidates=candidates or [],
  )


def test_found_identity_recovers_the_licence_and_redirects_to_the_open_batch(
  monkeypatch, batch,
):
  """A NIF-resolved licence already enrolled elsewhere returns that batch.

  SAV2 refuses to add a player to a second open batch, so returning the input
  batch here would make the enrolment fail downstream.
  """
  other = type("BatchStub", (), {"id": 77, "number": "2025/77", "club_id": 99})()
  monkeypatch.setattr(
    cli_module, "resolve_player_from_form",
    lambda parsed, client: _identity("found", license=301772),
  )
  monkeypatch.setattr(
    cli_module, "_find_enrolled_in_matching_batches",
    lambda client, batch_obj, license: other if license == 301772 else None,
  )

  assert cli_module._resolve_enroll_player(
    _Client(), batch, {}, reg_type=2,
  ) == (301772, other)


def test_revalidacao_offers_found_identity_and_accepts_it(monkeypatch, batch):
  """With no OCR licence, a confirmed identity short-circuits manual entry."""
  monkeypatch.setattr(
    cli_module, "resolve_player_from_form",
    lambda parsed, client: _identity("found", license=301772),
  )
  monkeypatch.setattr(
    cli_module, "_find_enrolled_in_matching_batches",
    lambda client, batch_obj, license: None,
  )
  monkeypatch.setattr(cli_module.click, "confirm", lambda *a, **kw: True)

  assert cli_module._resolve_enroll_player(
    _Client(), batch, {}, reg_type=2,
  ) == (301772, batch)


def test_declining_the_identity_licence_falls_through_to_manual_entry(
  monkeypatch, batch,
):
  """Declining must not silently enrol the suggested licence anyway."""
  monkeypatch.setattr(
    cli_module, "resolve_player_from_form",
    lambda parsed, client: _identity("found", license=301772),
  )
  monkeypatch.setattr(
    cli_module, "_find_enrolled_in_matching_batches",
    lambda client, batch_obj, license: None,
  )
  monkeypatch.setattr(cli_module.click, "confirm", lambda *a, **kw: False)
  monkeypatch.setattr(cli_module.click, "prompt", lambda *a, **kw: "")

  assert cli_module._resolve_enroll_player(
    _Client(), batch, {}, reg_type=2,
  ) is None


def test_primeira_inscricao_does_not_offer_an_identity_licence(monkeypatch, batch):
  """The offer is gated on reg_type=2; a 1ª Inscrição must reach the prompt."""
  monkeypatch.setattr(
    cli_module, "resolve_player_from_form",
    lambda parsed, client: _identity("found", license=301772),
  )
  monkeypatch.setattr(
    cli_module, "_find_enrolled_in_matching_batches",
    lambda client, batch_obj, license: None,
  )
  monkeypatch.setattr(
    cli_module.click, "confirm",
    lambda *a, **kw: pytest.fail("1ª Inscrição must not offer a NIF licence"),
  )
  monkeypatch.setattr(cli_module.click, "prompt", lambda *a, **kw: "")

  assert cli_module._resolve_enroll_player(
    _Client(), batch, {}, reg_type=1,
  ) is None


def test_unresolvable_identity_reaches_manual_entry_without_offering(
  monkeypatch, batch,
):
  """A missing identity result must not be offered as a licence."""
  monkeypatch.setattr(
    cli_module, "resolve_player_from_form",
    lambda parsed, client: None,
  )
  monkeypatch.setattr(
    cli_module, "_find_enrolled_in_matching_batches",
    lambda client, batch_obj, license: pytest.fail(
      "no licence to check for an existing enrolment"
    ),
  )
  monkeypatch.setattr(
    cli_module.click, "confirm",
    lambda *a, **kw: pytest.fail("nothing to confirm without a licence"),
  )
  monkeypatch.setattr(cli_module.click, "prompt", lambda *a, **kw: "")

  assert cli_module._resolve_enroll_player(
    _Client(), batch, {}, reg_type=2,
  ) is None


def test_ambiguous_identity_lists_candidates_without_offering_a_licence(
  monkeypatch, batch,
):
  candidate = SimpleNamespace(
    name="Ana Silva", license=301772, season="2025/2026",
  )
  identity = _identity("ambiguous", candidates=[candidate])
  monkeypatch.setattr(
    cli_module, "resolve_player_from_form",
    lambda parsed, client: identity,
  )
  monkeypatch.setattr(
    enrollment_module, "resolve_player_from_form", lambda parsed, client: identity,
  )
  monkeypatch.setattr(
    cli_module, "resolve_player_candidates", _REAL_RESOLVE_PLAYER_CANDIDATES,
  )
  monkeypatch.setattr(
    _Client, "_list_revalidable_licenses", lambda self, batch: [301772],
  )
  printed = []

  class ConsoleStub:
    def status(self, *args, **kwargs):
      return nullcontext()

    def print(self, message):
      printed.append(str(message))

  monkeypatch.setattr(cli_module, "_console", lambda: ConsoleStub())
  monkeypatch.setattr(cli_module.click, "prompt", lambda *a, **kw: 1)
  monkeypatch.setattr(
    cli_module.click, "confirm",
    lambda *a, **kw: pytest.fail("an ambiguous identity must not offer a licence"),
  )

  assert cli_module._resolve_enroll_player(
    _Client(), batch, {}, reg_type=2,
  ) == (301772, batch)
  rendered = "\n".join(printed)
  assert "Ana Silva" in rendered
  assert "301772" in rendered
  assert "2025/2026" in rendered


def test_unknown_identity_warns_and_falls_through_to_manual_entry(
  monkeypatch, batch,
):
  monkeypatch.setattr(
    cli_module, "resolve_player_from_form",
    lambda parsed, client: _identity("unknown"),
  )
  printed = []

  class ConsoleStub:
    def status(self, *args, **kwargs):
      return nullcontext()

    def print(self, message):
      printed.append(str(message))

  monkeypatch.setattr(cli_module, "_console", lambda: ConsoleStub())
  monkeypatch.setattr(cli_module.click, "prompt", lambda *a, **kw: "")
  monkeypatch.setattr(
    cli_module.click, "confirm",
    lambda *a, **kw: pytest.fail("unknown identity must not offer a licence"),
  )

  assert cli_module._resolve_enroll_player(
    _Client(), batch, {}, reg_type=2,
  ) is None
  assert any("could not verify" in message for message in printed)
