"""One shape per identifier, tolerant when reading and strict when writing.

Licences used to be `str` on `Player`/`player_to_dict` and `int` everywhere
else, which sav_mcp/AGENTS.md already warned would break authorization
comparisons — a wrapper storing "301772" from a search then testing it against
{301772} denies a parent access to their own child. NIF was validated as nine
digits by the MCP lookup tools but only checked for presence by the Modelo 1
validator, so `nif="abc"` reached SAV's create-player call verbatim.
"""

import pytest

from sav_shared.identifiers import (
  NO_LICENSE, normalise_nif, require_license, require_nif, to_license,
)


class TestToLicense:
  """Read path — a row we can't read must not break the listing."""

  @pytest.mark.parametrize(
    "value,expected",
    [("301772", 301772), (301772, 301772), (" 301772 ", 301772), ("301-772", 301772)],
  )
  def test_parses_licences(self, value, expected):
    assert to_license(value) == expected

  @pytest.mark.parametrize(
    "value",
    # "Sub 14" and "lic 301772" matter: scraping digits out of prose would
    # have invented licences 14 and 301772 out of a tier name and a label.
    ["", None, "n/a", 0, -5, True, False, "Sub 14", "lic 301772"],
  )
  def test_unusable_values_become_the_sentinel(self, value):
    assert to_license(value) == NO_LICENSE

  def test_never_raises(self):
    assert to_license(object()) == NO_LICENSE


class TestRequireLicense:
  def test_accepts_a_real_licence(self):
    assert require_license("301772") == 301772

  @pytest.mark.parametrize("value", ["", None, 0, "abc", -1])
  def test_rejects_everything_else(self, value):
    with pytest.raises(ValueError, match="must be a positive licence number"):
      require_license(value)

  def test_blank_does_not_become_licence_zero(self):
    # The failure this guards: "" silently addressing licence 0.
    with pytest.raises(ValueError):
      require_license("")


class TestNif:
  @pytest.mark.parametrize(
    "value", ["289463491", "289 463 491", "289.463.491", "289-463-491"],
  )
  def test_separators_are_stripped(self, value):
    assert normalise_nif(value) == "289463491"

  @pytest.mark.parametrize(
    "value",
    ["abc", "12345", "1234567890", "", None, True, "abc123456789", "NIF 289463491"],
  )
  def test_non_nifs_read_as_none(self, value):
    assert normalise_nif(value) is None

  def test_require_accepts_a_real_nif(self):
    assert require_nif("289 463 491") == "289463491"

  @pytest.mark.parametrize("value", ["abc", "12345", "", None])
  def test_require_rejects_non_nifs(self, value):
    with pytest.raises(ValueError, match="must be 9 digits"):
      require_nif(value)

  def test_error_names_the_field(self):
    with pytest.raises(ValueError, match="guardian_nif must be 9 digits"):
      require_nif("abc", field="guardian_nif")


class TestModelo1NifRule:
  """The Modelo 1 validator now applies the same rule as the lookup tools."""

  def test_rejects_a_non_numeric_nif(self):
    from sav_shared.fpb_mod1 import validate_mod1_values
    problems = validate_mod1_values({"nif": "abc"})
    assert any("nif" in p and "9 digits" in p for p in problems)

  def test_accepts_a_real_nif(self):
    from sav_shared.fpb_mod1 import validate_mod1_values
    problems = validate_mod1_values({"nif": "289463491"})
    assert not any("9 digits" in p for p in problems)
