"""SAV's boolean flags are decoded explicitly, never by Python truthiness.

`bool('0')` is `True`. SAV encodes these inconsistently — op=31 returns
`menor_idade` as the int 1 while its consent flags come back as the strings
'1'/'0'/None — so reading any of them with plain truthiness inverts a false
value.

For `menor_idade` the inversion has a direction that matters. Decoded as an
adult, a minor walks past the guardian check that exists to stop an
unaccompanied child being filed with the federation; decoded as a minor, an
adult is blocked until someone invents guardian details for them.
"""

import pytest

from sav_client.exceptions import SavResponseError
from sav_client.sav_client import _decode_sav_flag


class TestDecodeSavFlag:
  @pytest.mark.parametrize(
    "value,expected",
    [
      (1, True), (0, False),
      ("1", True), ("0", False),
      (True, True), (False, False),
      ("true", True), ("false", False),
      ("sim", True), ("nao", False), ("Não", False),
      ("  1  ", True), ("", False),
    ],
  )
  def test_documented_encodings(self, value, expected):
    assert _decode_sav_flag(value, field="menor_idade") is expected

  def test_string_zero_is_false(self):
    # The whole reason this exists: bool('0') is True.
    assert _decode_sav_flag("0", field="menor_idade") is False

  @pytest.mark.parametrize("value", ["maybe", 2, -1, [], {}, 1.5])
  def test_unrecognised_encoding_raises(self, value):
    with pytest.raises(SavResponseError, match="menor_idade"):
      _decode_sav_flag(value, field="menor_idade")

  def test_absent_raises_by_default(self):
    """Fail closed: "SAV didn't say" must not silently disable the guard."""
    with pytest.raises(SavResponseError, match="did not return"):
      _decode_sav_flag(None, field="menor_idade")

  def test_absent_is_allows_an_explicit_default(self):
    assert _decode_sav_flag(None, field="optional", absent_is=False) is False
    assert _decode_sav_flag(None, field="optional", absent_is=True) is True

  def test_error_never_leaks_the_raw_value(self):
    # SAV bodies carry its internal schema; the field name is enough.
    with pytest.raises(SavResponseError) as excinfo:
      _decode_sav_flag("SECRET_INTERNAL_TOKEN", field="menor_idade")
    assert "SECRET_INTERNAL_TOKEN" not in str(excinfo.value)


class TestMinorGateUsesIt:
  """Both guardian gates decode the flag rather than trusting truthiness."""

  def test_both_gates_call_the_decoder(self):
    import inspect
    from sav_client.sav_client import SavClient

    for method in (SavClient._commit_registration_step3,
                   SavClient._add_player_to_primeira_batch):
      src = inspect.getsource(method)
      assert "_decode_sav_flag" in src, f"{method.__name__} decodes menor_idade"
      assert 'bool(step' not in src.replace("_decode_sav_flag", ""), (
        f"{method.__name__} still uses truthiness on a SAV flag"
      )
