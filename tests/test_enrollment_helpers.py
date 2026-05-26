"""Unit tests for sav_shared.enrollment helpers."""

import pytest

from sav_shared.enrollment import (
  REGISTRATION_TYPE_SUBIDA,
  validate_subida_combo,
)


@pytest.mark.parametrize("reg_type", [1, 2])
def test_validate_subida_combo_allows_inline_on_type_1_and_2(reg_type):
  # Inline subida rides on a 1ª Inscrição (1) or Revalidação (2).
  validate_subida_combo(reg_type, inline_subida=True)


@pytest.mark.parametrize("reg_type", [1, 2, REGISTRATION_TYPE_SUBIDA])
def test_validate_subida_combo_allows_no_rider(reg_type):
  # Without an inline rider, every batch type is fine (incl. standalone subida).
  validate_subida_combo(reg_type, inline_subida=False)


def test_validate_subida_combo_rejects_inline_on_standalone_subida():
  # A standalone Subida batch IS a subida — an inline rider on top is contradictory.
  with pytest.raises(ValueError, match="inline_subida is only valid"):
    validate_subida_combo(REGISTRATION_TYPE_SUBIDA, inline_subida=True)
