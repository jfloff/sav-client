"""Authorization and catalog checks for identity lookup tools."""

from sav_mcp import server as server_module


def test_identity_tool_catalog_and_authz_policy():
  names = {
    tool.name for tool in server_module.server._tool_manager.list_tools()
  }
  assert "lookup_player" in names
  assert "identify_player" in names
  assert "find_player_by_nif" not in names

  lookup_policy = server_module._AUTHZ_POLICY["lookup_player"]
  assert lookup_policy.subject_license == ("license",)
  assert lookup_policy.subject_nif == ()

  # Parents/players identify their own dependant by NIF; the other identity
  # keys ride along unverified by design (accepted trade-off, see authz.toml).
  identify_policy = server_module._AUTHZ_POLICY["identify_player"]
  assert identify_policy.capability == "read"
  assert identify_policy.roles == ("coach",)
  assert identify_policy.self_scope == ("parent", "player")
  assert identify_policy.subject_license == ()
  assert identify_policy.subject_nif == ("nif",)


def test_lookup_and_identify_tool_schemas_expose_identity_inputs():
  lookup = server_module.server._tool_manager.get_tool("lookup_player")
  identify = server_module.server._tool_manager.get_tool("identify_player")

  assert lookup is not None
  assert identify is not None
  assert set(lookup.parameters["properties"]) == {
    "license", "club_id", "status", "with_profile", "with_details",
  }
  assert {
    "nif", "id_number", "name", "birth_date", "club_id", "with_profile",
  } <= set(identify.parameters["properties"])
  assert identify.parameters["properties"]["nif"].get("x-sav-subject") == "nif"
