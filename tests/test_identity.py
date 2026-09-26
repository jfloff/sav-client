import pytest
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


# Cartão de Cidadão: the civil number the club holds vs the full card number
# SAV holds on older records — every spelling observed live on 2026-09-26.
SAV_CC_SPELLINGS = [
  ("15932997", "15932997 3ZW6"),
  ("30110167", "30110167 1ZX6"),
  ("30543973", "305439731 ZW4"),
  ("30927726", "30927726 4 ZX1"),
  ("30561190", "30561190 9ZX4"),
  ("31028623", "31028623 9 ZX0"),
  ("31045528", "31045528 6 ZX6"),
]


@pytest.mark.parametrize("given, on_file", SAV_CC_SPELLINGS)
def test_cc_civil_number_reads_every_sav_spelling(given, on_file):
  from sav_shared.identity import cc_civil_number
  assert cc_civil_number(on_file) == given
  assert cc_civil_number(given) == given


@pytest.mark.parametrize("given, on_file", SAV_CC_SPELLINGS)
def test_a_cc_civil_number_matches_the_full_card_number(given, on_file):
  from sav_shared.identity import id_numbers_match
  assert id_numbers_match(given, on_file, doc_type=1)
  # A renewed card (new check digit / version) is the same person.
  assert id_numbers_match(on_file, given + " 0ZZ9", doc_type=1)


@pytest.mark.parametrize("given, on_file", [
  ("E2203397", "N1F62X3D0"), ("860ww7029", "GF112738"), ("23D553W08", "EJG252806"),
])
def test_different_documents_never_match(given, on_file):
  from sav_shared.identity import id_numbers_match
  for doc_type in (1, 2, 3, None):
    assert not id_numbers_match(given, on_file, doc_type=doc_type)


@pytest.mark.parametrize("given, on_file", [
  ("ejg252806", "EJG252806"), ("EJG 252 806", "EJG252806"),
  ("EJG-252.806", "ejg252806"), (" AB/1234567 ", "ab1234567"),
])
@pytest.mark.parametrize("doc_type", [2, 3, 5, None])
def test_other_documents_ignore_case_and_separators(given, on_file, doc_type):
  from sav_shared.identity import id_numbers_match
  assert id_numbers_match(given, on_file, doc_type=doc_type)


def test_other_types_and_an_unknown_type_compare_every_character():
  from sav_shared.identity import cc_civil_number, id_numbers_match
  assert id_numbers_match(" EJG252806 ", "EJG252806", doc_type=2)
  assert not id_numbers_match("EJG252806", "EJG25280", doc_type=2)
  assert not id_numbers_match("EJG252806", "EJG252807", doc_type=2)
  # Unknown type: conservative — a CC base against a full number is reported.
  assert not id_numbers_match("15932997", "15932997 3ZW6", doc_type=None)
  # A passport of 8 digits is not silently truncated either.
  assert not id_numbers_match("12345678", "123456789", doc_type=2)
  assert cc_civil_number("1593") is None
  assert cc_civil_number("159329973ZW") is None
  assert cc_civil_number("15932997-3zw6") == "15932997"
