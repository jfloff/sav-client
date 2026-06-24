"""Projected document checklist for not-enrolled / enrolled players.

The pending path is exercised via compute_enrollment_checklist's own unit
tests; here we cover the branches get_enrollment_status added on top: a
checklist grounded in the player's stored nationality even when there is no
open batch to read it from.
"""

from sav_client.exceptions import LicenseNotEnrolledError, SavResponseError

from sav_mcp import server as server_module


class _Player:
  """Minimal Player stand-in for player_to_dict."""

  id = 1
  license = "301772"
  name = "Jogador Teste"
  club = "Rio Maior Basket"
  association = "AB Santarém"
  tier = "Sub 14"
  gender = "Masculino"
  birth_date = "2012-01-01"
  nationality = "Brasil"
  status = "FBP"
  season = "2025/2026"
  active = True


def test_not_enrolled_returns_projected_foreign_born_checklist(monkeypatch):
  class StubClient:
    session = {"organizacao": 7}

    def resolve_batch_id_by_license(self, license):
      raise LicenseNotEnrolledError(license, open_batches=[{"number": "B1"}])

    def search_players(self, license, club, status):
      return []  # not in the active roster

    def load_player_profile(self, license, club_id=None):
      return {"nacional": "200"}  # Brazil → foreign_born

  monkeypatch.setattr(server_module, "_get_client", lambda: StubClient())

  result = server_module.get_enrollment_status(license=301772)

  assert result["status"] == "not_enrolled"
  assert result["open_batches"] == [{"number": "B1"}]
  checklist = result["checklist"]
  assert checklist["projected"] is True
  assert checklist["scenario"] == "foreign_born"
  # No batch yet → every required doc unsatisfied.
  assert all(not row["satisfied"] for row in checklist["required"])
  assert "atestado_residencia" in checklist["missing"]


def test_not_enrolled_portuguese_checklist(monkeypatch):
  class StubClient:
    session = {"organizacao": 7}

    def resolve_batch_id_by_license(self, license):
      raise LicenseNotEnrolledError(license, open_batches=[])

    def search_players(self, license, club, status):
      return []

    def load_player_profile(self, license, club_id=None):
      return {"nacional": "155"}  # Portugal

  monkeypatch.setattr(server_module, "_get_client", lambda: StubClient())

  result = server_module.get_enrollment_status(license=301772)

  assert result["checklist"]["scenario"] == "portuguese"
  assert result["checklist"]["projected"] is True


def test_enrolled_player_also_gets_projected_checklist(monkeypatch):
  class StubClient:
    session = {"organizacao": 7}

    def resolve_batch_id_by_license(self, license):
      raise LicenseNotEnrolledError(license, open_batches=[])

    def search_players(self, license, club, status):
      return [_Player()]  # active in roster

    def load_player_profile(self, license, club_id=None):
      return {"nacional": "200"}

  monkeypatch.setattr(server_module, "_get_client", lambda: StubClient())

  result = server_module.get_enrollment_status(license=301772)

  assert result["status"] == "enrolled"
  assert result["player"]["license"] == "301772"
  assert result["checklist"]["scenario"] == "foreign_born"


def test_profile_failure_defaults_to_foreign_born(monkeypatch):
  class StubClient:
    session = {"organizacao": 7}

    def resolve_batch_id_by_license(self, license):
      raise LicenseNotEnrolledError(license, open_batches=[])

    def search_players(self, license, club, status):
      return []

    def load_player_profile(self, license, club_id=None):
      raise SavResponseError("no player")

  monkeypatch.setattr(server_module, "_get_client", lambda: StubClient())

  result = server_module.get_enrollment_status(license=301772)

  # Defensive: unknown nationality asks for the larger document set.
  assert result["checklist"]["scenario"] == "foreign_born"


def test_reg_type_transferencia_yields_null_checklist(monkeypatch):
  class StubClient:
    session = {"organizacao": 7}

    def resolve_batch_id_by_license(self, license):
      raise LicenseNotEnrolledError(license, open_batches=[])

    def search_players(self, license, club, status):
      return []

    def load_player_profile(self, license, club_id=None):
      return {"nacional": "155"}

  monkeypatch.setattr(server_module, "_get_client", lambda: StubClient())

  result = server_module.get_enrollment_status(license=301772, reg_type=3)

  assert result["checklist"] is None


# ── enrollment_status_bulk ───────────────────────────────────────────────────
# The classification logic lives in (and is tested at) the client layer; here
# we cover the MCP wrapper: it preserves input order and stamps each row with
# its licence.

def test_bulk_preserves_order_and_stamps_license(monkeypatch):
  class StubClient:
    def classify_enrollment_status(self, licenses):
      return {
        301772: {"status": "pending", "name": "A"},
        301773: {"status": "enrolled", "name": "B"},
        999: {"status": "not_enrolled", "open_batches": []},
      }

  monkeypatch.setattr(server_module, "_get_client", lambda: StubClient())

  rows = server_module.enrollment_status_bulk([301773, 999, 301772])

  assert [r["license"] for r in rows] == [301773, 999, 301772]
  assert rows[0] == {"license": 301773, "status": "enrolled", "name": "B"}
  assert rows[1]["status"] == "not_enrolled"
  assert rows[2]["status"] == "pending"


def test_bulk_empty_input(monkeypatch):
  class StubClient:
    def classify_enrollment_status(self, licenses):
      return {}

  monkeypatch.setattr(server_module, "_get_client", lambda: StubClient())

  assert server_module.enrollment_status_bulk([]) == []
