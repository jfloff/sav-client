from sav_client.models import Player
from sav_shared.identity import (
  PLACEHOLDER_NIFS,
  group_same_person,
  is_placeholder_nif,
  names_match,
)


def _player(license: int, name: str, birth_date: str) -> Player:
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
  )


def test_placeholder_nif_is_normalised_before_matching():
  assert PLACEHOLDER_NIFS == frozenset({"999999990"})
  assert is_placeholder_nif("999 999 990") is True
  assert is_placeholder_nif("123456789") is False
  assert is_placeholder_nif(None) is False


def test_names_match_ignores_accents_case_and_word_order():
  assert names_match("José Álvares Silva", "silva alvares jose") is True


def test_names_match_does_not_use_a_single_shared_token_as_a_match():
  assert names_match("Hu", "Yuting Hu") is False
  assert names_match("Ana Silva", "Rita Silva") is False


def test_names_match_blank_inputs_never_match():
  assert names_match("", "Ana Silva") is False
  assert names_match("Ana Silva", "   ") is False


def test_group_same_person_uses_normalised_name_and_exact_birth_date_stably():
  first = _player(101, "José Silva", "2010-01-01")
  same_person = _player(102, "jose silva", "2010-01-01")
  different_birth_date = _player(103, "JOSE SILVA", "2010-01-02")
  other_person = _player(104, "Ana Silva", "2010-01-01")

  groups = group_same_person(
    [first, different_birth_date, same_person, other_person]
  )

  assert groups == [[first, same_person], [different_birth_date], [other_person]]
