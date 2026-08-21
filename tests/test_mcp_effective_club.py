"""Offline tests for the shared _effective_club resolver."""

from sav_mcp import server as server_module


class _StubClient:
  def __init__(self, session):
    self.session = session


def test_explicit_club_id_wins_over_the_session_club():
  client = _StubClient({"organizacao": 200})

  assert server_module._effective_club(client, 314) == 314


def test_explicit_zero_is_kept_as_a_federation_wide_scope():
  client = _StubClient({"organizacao": 200})

  assert server_module._effective_club(client, 0) == 0


def test_omitted_club_id_falls_back_to_the_session_club():
  client = _StubClient({"organizacao": "200"})

  assert server_module._effective_club(client, None) == 200


def test_missing_or_blank_session_club_resolves_to_zero():
  assert server_module._effective_club(_StubClient({}), None) == 0
  assert server_module._effective_club(_StubClient({"organizacao": ""}), None) == 0
  assert server_module._effective_club(_StubClient({"organizacao": None}), None) == 0
