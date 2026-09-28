"""Checked-in SAV nationality lookup and MCP export contracts."""

from types import SimpleNamespace

import pytest

from sav_mcp import server as server_module
from sav_shared.lookups import NATIONALITIES, reference_data


def test_nationality_snapshot_has_the_verified_sav_shape():
  assert len(NATIONALITIES) == 222
  assert NATIONALITIES[155] == "Portugal"
  assert NATIONALITIES[26] == "Brasil"
  assert NATIONALITIES[222] == "Sudão do Sul"
  assert all(isinstance(key, int) for key in NATIONALITIES)
  assert len(set(NATIONALITIES.values())) == len(NATIONALITIES)


def test_reference_data_exports_the_complete_snapshot_with_int_ids():
  exported = reference_data()["nationalities"]

  assert exported == [
    {"id": nationality_id, "name": name}
    for nationality_id, name in NATIONALITIES.items()
  ]
  assert all(isinstance(row["id"], int) for row in exported)


class _LookupClient:
  def get_current_season(self):
    return SimpleNamespace(start_year=2026)


def test_current_mcp_lookup_resource_exports_nationality_ids(monkeypatch):
  monkeypatch.setattr(server_module, "_get_client", lambda: _LookupClient())

  result = server_module.lookups_resource()

  assert result["nationalities"][0] == {"id": 155, "name": "Portugal"}
  assert len(result["nationalities"]) == 222
  assert result["season_start_year"] == 2026


def test_season_lookup_resource_remains_static(monkeypatch):
  monkeypatch.setattr(
    server_module,
    "_get_client",
    lambda: pytest.fail("explicit-season lookups must not get a SAV client"),
  )

  result = server_module.lookups_for_season_resource("2025")

  assert result["nationalities"][0] == {"id": 155, "name": "Portugal"}
  assert len(result["nationalities"]) == 222
  assert result["season_start_year"] == 2025
