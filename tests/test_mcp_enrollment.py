"""MCP server tests for the license-first enrollment surface."""

from sav_mcp import server as server_module


def test_read_enrollment_returns_player_detail(monkeypatch):
  class StubClient:
    def resolve_batch_id_by_license(self, license):
      assert license == 301772
      return 42

    def load_existing_registration_record(self, batch_id, license):
      assert (batch_id, license) == (42, 301772)
      return {"id": 77, "nome": "Player A"}

  monkeypatch.setattr(server_module, "_get_client", lambda: StubClient())

  result = server_module.read_enrollment(license=301772)
  assert result == {"id": 77, "nome": "Player A"}


def test_read_enrollment_license_not_enrolled_returns_structured_error(monkeypatch):
  from sav_client.exceptions import LicenseNotEnrolledError

  class StubClient:
    def resolve_batch_id_by_license(self, license):
      raise LicenseNotEnrolledError(
        license=license,
        open_batches=[{"number": "2025/123", "tier": "Sub 14", "gender": "M"}],
      )

  monkeypatch.setattr(server_module, "_get_client", lambda: StubClient())

  result = server_module.read_enrollment(license=999999999)
  assert result == {
    "error": "license_not_enrolled",
    "license": 999999999,
    "open_batches": [{"number": "2025/123", "tier": "Sub 14", "gender": "M"}],
  }


def test_list_batch_enrollments_lists_players(monkeypatch):
  class StubClient:
    def resolve_batch_id(self, number):
      assert number == "2025/123"
      return 42

    def list_player_registration_batch_items(self, batch_id):
      assert batch_id == 42
      return [{"license": 301772, "name": "Player A"}]

  monkeypatch.setattr(server_module, "_get_client", lambda: StubClient())

  result = server_module.list_batch_enrollments(batch_number="2025/123")
  assert result == [{"license": 301772, "name": "Player A"}]


def test_delete_enrollment_removes_player(monkeypatch):
  captured = {"removed": None}

  class _Cache:
    def get_batch_number(self, batch_id):
      return "2025/123"

  class StubClient:
    _cache = _Cache()

    def resolve_batch_id_by_license(self, license):
      return 42

    def remove_player_from_registration_batch(self, batch_id, license):
      captured["removed"] = (batch_id, license)

  monkeypatch.setattr(server_module, "_get_client", lambda: StubClient())

  result = server_module.delete_enrollment(license=301772)
  assert captured["removed"] == (42, 301772)
  assert result == {"removed": True, "license": 301772, "batch_number": "2025/123"}


def test_delete_enrollment_license_not_enrolled_returns_structured_error(monkeypatch):
  from sav_client.exceptions import LicenseNotEnrolledError

  class StubClient:
    def resolve_batch_id_by_license(self, license):
      raise LicenseNotEnrolledError(license=license, open_batches=[])

    def remove_player_from_registration_batch(self, batch_id, license):
      raise AssertionError("must not be called when resolver fails")

  monkeypatch.setattr(server_module, "_get_client", lambda: StubClient())

  result = server_module.delete_enrollment(license=999999999)
  assert result == {
    "error": "license_not_enrolled",
    "license": 999999999,
    "open_batches": [],
  }


def test_delete_batch_deletes_whole_batch(monkeypatch):
  captured = {"deleted_id": None}

  class StubClient:
    def resolve_batch_id(self, number):
      assert number == "2025/999"
      return 99

    def delete_player_registration_batch(self, batch_id):
      captured["deleted_id"] = batch_id

  monkeypatch.setattr(server_module, "_get_client", lambda: StubClient())

  result = server_module.delete_batch(batch_number="2025/999")
  assert captured["deleted_id"] == 99
  assert result == {"deleted": True, "batch_number": "2025/999"}


def test_update_enrollment_drops_batch_number(monkeypatch):
  captured = {}

  class StubClient:
    def resolve_batch_id_by_license(self, license):
      return 42

    def update_player_in_registration_batch(self, batch_id, license, **kwargs):
      captured["call"] = (batch_id, license, kwargs)
      return 77

  monkeypatch.setattr(server_module, "_get_client", lambda: StubClient())

  result = server_module.update_enrollment(license=301772, fields={"email": "x@y.z"})
  assert captured["call"] == (42, 301772, {"email": "x@y.z"})
  assert result == {"success": True, "player_id": 77}


def test_update_enrollment_returns_structured_error_when_not_enrolled(monkeypatch):
  from sav_client.exceptions import LicenseNotEnrolledError

  class StubClient:
    def resolve_batch_id_by_license(self, license):
      raise LicenseNotEnrolledError(
        license=license,
        open_batches=[{"number": "2025/1", "tier": "Sub 14", "gender": "F"}],
      )

    def update_player_in_registration_batch(self, *a, **kw):
      raise AssertionError("must not be called when resolver fails")

  monkeypatch.setattr(server_module, "_get_client", lambda: StubClient())

  result = server_module.update_enrollment(license=999, fields={"email": "x@y.z"})
  assert result["error"] == "license_not_enrolled"
  assert result["license"] == 999
  assert result["open_batches"] == [{"number": "2025/1", "tier": "Sub 14", "gender": "F"}]


def test_mcp_tool_signatures_dropped_batch_number():
  """Existing-enrollment MCP tools must no longer accept batch_number."""
  import inspect

  for tool_name in (
    "update_enrollment",
    "update_enrollment_with_document",
    "read_enrollment",
    "delete_enrollment",
    "list_player_documents",
    "upload_player_document",
    "replace_player_document",
  ):
    fn = getattr(server_module, tool_name)
    sig = inspect.signature(fn)
    assert "batch_number" not in sig.parameters, (
      f"{tool_name} still accepts batch_number; "
      f"parameters: {list(sig.parameters)}"
    )
