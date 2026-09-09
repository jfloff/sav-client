"""Offline contract tests for the derived enrollment field description.

These tests cover the shape, ordering, validation rules, and lookup references
returned by ``enrollment_fields``. The anti-drift assertions are the important
part: this tool describes the enrollment surface derived from the renderer's
mapping, so the tests must verify that it stays derived rather than copying the
field table a second time and allowing the two descriptions to diverge.
"""
import pytest

from sav_mcp import server as server_module
from sav_shared.fields import FIELDS
from sav_shared.fpb_mod1 import (
  MOD1_FILL_MAPPING,
  _MOD1_CONSENT_KEYS,
  _MOD1_GUARDIAN_KEYS,
  _MOD1_REQUIRED_CORE,
)
from sav_shared.lookups import reference_data


EXPECTED_KEYS = {
  "id", "label", "type", "required_when", "enum_ref", "values_key",
  "field_overrides_key",
}


def _rows_by_id():
  return {row["id"]: row for row in server_module.enrollment_fields()}


def test_every_field_id_is_unique():
  """Duplicate identifiers would make a consumer silently lose a field."""
  rows = server_module.enrollment_fields()

  assert len({row["id"] for row in rows}) == len(rows)


def test_required_rules_follow_mod1_constants():
  """The public rules must keep the renderer's core, revalidation, and minor conditions visible."""
  rows = _rows_by_id()

  assert all(rows[key]["required_when"] == "always" for key in _MOD1_REQUIRED_CORE)
  assert rows["nif"]["required_when"] == "always"
  assert rows["nome"]["required_when"] == "always"
  assert rows["license"]["required_when"] == "revalidacao"
  assert all(rows[key]["required_when"] == "minor" for key in _MOD1_GUARDIAN_KEYS)


def test_data_assinatura_is_the_only_optional_field():
  """The signature date belongs to the hand-completed block, so it is the one
  field no rule makes mandatory. If a second row ever turns up optional, a
  required field has quietly dropped out of the mandatory-fill constants."""
  rows = server_module.enrollment_fields()

  optional = [row["id"] for row in rows if row["required_when"] == "optional"]
  assert optional == ["data_assinatura"]


def test_labels_come_from_field_definitions_and_are_never_invented():
  """`label` is `FieldDef.label` or nothing at all.

  There is no per-field label for the header/identity keys, and inventing one
  here would recreate the hand-maintained table this tool exists to remove — so
  a missing label stays null rather than being filled in.
  """
  rows = _rows_by_id()
  labels_by_key = {f.key: f.label for f in FIELDS}

  for key, row in rows.items():
    assert row["label"] == labels_by_key.get(key)
  assert rows["nif"]["label"] == "NIF"
  assert rows["nome"]["label"] is None


def test_consent_fields_are_always_required_booleans():
  """Consent fields must not be exposed as free text or as optional acknowledgements."""
  rows = _rows_by_id()

  assert all(rows[key]["type"] == "bool" for key in _MOD1_CONSENT_KEYS)
  assert all(rows[key]["required_when"] == "always" for key in _MOD1_CONSENT_KEYS)


def test_date_postal_and_id_type_fields_keep_their_input_contracts():
  """Consumers need the exact primitive type and lookup contract to validate before submission."""
  rows = _rows_by_id()

  assert rows["nasc"]["type"] == "date"
  assert rows["dataval"]["type"] == "date"
  assert rows["codpostal"]["type"] == "postal"
  assert rows["tipo"]["type"] == "enum"
  assert rows["tipo"]["enum_ref"] == "id_types"


def test_enum_references_resolve_through_reference_data():
  """Every emitted lookup reference must resolve through ``sav://lookups``.

  This lets a consumer resolve a field's legal values without the server
  shipping a second copy of the enrollment table. A renamed lookup key must
  fail here rather than silently hand consumers a dangling reference.
  """
  lookup_keys = set(reference_data())

  for row in server_module.enrollment_fields():
    if row["enum_ref"] is not None:
      assert row["enum_ref"] in lookup_keys


def test_field_ids_and_order_are_derived_from_mod1_fill_mapping():
  """A mapping addition must become part of the public field surface immediately."""
  rows = server_module.enrollment_fields()

  assert {row["id"] for row in rows} == set(MOD1_FILL_MAPPING)
  assert [row["id"] for row in rows] == list(MOD1_FILL_MAPPING)


def test_field_schema_uses_closed_type_and_requirement_vocabularies():
  """Unknown vocabulary values would leave consumers without predictable validation semantics."""
  rows = server_module.enrollment_fields()

  assert {row["type"] for row in rows} <= {"text", "date", "bool", "enum", "postal"}
  assert {row["required_when"] for row in rows} <= {
    "always", "revalidacao", "minor", "optional",
  }


def test_every_row_has_exactly_the_documented_keys():
  """Adding or dropping a response key is a contract change for schema consumers."""
  assert all(set(row) == EXPECTED_KEYS for row in server_module.enrollment_fields())


def test_values_key_matches_the_mapping_id():
  """The values payload uses each row's canonical mapping key by construction."""
  assert all(row["values_key"] == row["id"] for row in server_module.enrollment_fields())


def test_distrito_is_text_with_a_distritos_reference():
  """Distrito is deliberately free text on the printed form but submitted as a ``distrito_id`` from the ``distritos`` table.

  Concelhos are distrito-dependent and fetched live from SAV, so
  ``reference_data()`` intentionally has no ``concelhos`` key.
  """
  rows = _rows_by_id()

  assert rows["distrito"]["type"] == "text"
  assert rows["distrito"]["enum_ref"] == "distritos"
  assert rows["concelho"]["enum_ref"] is None


def test_field_override_keys_preserve_submission_names():
  """The submission vocabulary must remain distinct from the form-facing field ids where SAV requires it."""
  rows = _rows_by_id()

  assert rows["tipo"]["field_overrides_key"] == "id_type"
  assert rows["tele"]["field_overrides_key"] == "telemovel"
  assert rows["codpostal"]["field_overrides_key"] == "cod_postal"
  assert rows["distrito"]["field_overrides_key"] == "distrito_id"
  assert rows["nif"]["field_overrides_key"] is None
  assert rows["nasc"]["field_overrides_key"] is None


def test_enrollment_fields_never_gets_a_sav_client(monkeypatch):
  """The schema is server-owned and must work without a session or any SAV call."""
  monkeypatch.setattr(
    server_module, "_get_client",
    lambda: pytest.fail("enrollment_fields must not talk to SAV"),
  )

  rows = server_module.enrollment_fields()

  assert rows
