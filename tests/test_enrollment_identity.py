"""Identity-based enrollment decisions and form parsing."""

from types import SimpleNamespace

import pytest

from sav_shared.enrollment import (
  derive_enrollment_params,
  resolve_player_candidates,
  resolve_player_from_form,
)


def _field(value):
  return SimpleNamespace(value=value)


def _parsed(**values):
  return {key: _field(value) for key, value in values.items()}


class _IdentityClient:
  def __init__(self, identity):
    self.identity = identity
    self.identity_calls = []
    self.search_calls = []

  def resolve_player_identity(self, **kwargs):
    self.identity_calls.append(kwargs)
    return self.identity

  def list_player_registration_tiers(self, *, gender_id):
    assert gender_id == 1
    return {7: "Sub 14"}

  def search_players(self, **kwargs):
    self.search_calls.append(kwargs)
    return []


def _match(status, *, player=None, candidates=None):
  return SimpleNamespace(
    status=status, player=player, candidates=candidates or [],
  )


def test_resolve_player_from_form_normalises_ocr_birth_date():
  client = _IdentityClient(_match("not_found"))
  parsed = _parsed(
    nif=" 123456789 ", nome_completo="Ana Silva",
    data_nascimento="07/03/2010", num_doc_identificacao="AB123456",
  )

  result = resolve_player_from_form(parsed, client)

  assert result.status == "not_found"
  assert client.identity_calls == [{
    "nif": "123456789",
    "id_number": "AB123456",
    "birth_date": "2010-03-07",
    "name": "Ana Silva",
    "club": None,
    "status": "all",
  }]


def test_resolve_player_from_form_drops_unparseable_date_and_name():
  client = _IdentityClient(_match("not_found"))
  parsed = _parsed(
    nif="123456789", nome_completo="Ana Silva",
    data_nascimento="not a date", num_doc_identificacao="",
  )

  resolve_player_from_form(parsed, client)

  assert client.identity_calls == [{
    "nif": "123456789", "id_number": None, "birth_date": None,
    "name": None, "club": None, "status": "all",
  }]


def test_resolve_player_from_form_without_any_usable_key_returns_none():
  client = _IdentityClient(_match("unknown"))

  assert resolve_player_from_form(_parsed(
    nome_completo="Ana Silva", data_nascimento="not a date",
  ), client) is None
  assert client.identity_calls == []


def _registration_form():
  return _parsed(
    tipo_inscricao_revalidacao=False,
    tipo_inscricao_primeira=False,
    genero_feminino=False,
    escalao_sub14=True,
    nif="999999999",
  )


def test_shared_nif_ambiguity_requires_explicit_registration_type():
  client = _IdentityClient(_match("ambiguous"))

  with pytest.raises(ValueError, match="several players.*Tick.*Revalidação / 1ª Inscrição"):
    derive_enrollment_params(_registration_form(), client)


def test_unverified_identity_requires_explicit_registration_type():
  client = _IdentityClient(_match("unknown"))

  with pytest.raises(ValueError, match="could not verify.*Tick.*Revalidação / 1ª Inscrição"):
    derive_enrollment_params(_registration_form(), client)


def test_found_identity_infers_revalidacao():
  client = _IdentityClient(_match("found", player=SimpleNamespace(license=42)))

  assert derive_enrollment_params(_registration_form(), client) == (2, 7, 1)


def test_not_found_identity_infers_primeira():
  client = _IdentityClient(_match("not_found"))

  assert derive_enrollment_params(_registration_form(), client) == (1, 7, 1)


def test_ambiguous_identity_does_not_auto_pick_one_eligible_candidate():
  eligible_player = SimpleNamespace(license=42, name="Ana Silva", season="2025/2026")
  other_player = SimpleNamespace(license=43, name="Ana Silva", season="2024/2025")
  client = _IdentityClient(_match(
    "ambiguous", candidates=[eligible_player, other_player],
  ))
  parsed = _parsed(
    nif="123456789", nome_completo="Ana Silva",
    data_nascimento="2010-03-07",
  )

  license, candidates, name, ocr_license = resolve_player_candidates(
    parsed, {42}, client, 200,
  )

  assert license is None
  assert candidates == [eligible_player]
  assert name == "Ana Silva"
  assert ocr_license is None
  assert client.search_calls == []


def test_found_but_ineligible_identity_falls_through_to_name_search():
  eligible_player = SimpleNamespace(license=99, name="Ana Silva")
  client = _IdentityClient(_match(
    "found", player=SimpleNamespace(license=42),
  ))
  client.search_players = lambda **kwargs: (
    client.search_calls.append(kwargs) or [eligible_player]
  )
  parsed = _parsed(nif="123456789", nome_completo="Ana Silva")

  result = resolve_player_candidates(parsed, {99}, client, 200)

  assert result == (99, [], "Ana Silva", None)
  assert client.search_calls == [
    {"name": "Ana Silva", "club": 200, "season": 0, "status": "all"},
  ]


def test_malformed_ocr_nif_is_not_a_key():
  # "12345" is an OCR misread, not a NIF. Passing it on would make the
  # resolver raise "requires a usable nif"; the form simply carries no NIF.
  client = _IdentityClient(_match("found"))

  assert resolve_player_from_form(_parsed(nif="12345"), client) is None
  assert client.identity_calls == []
