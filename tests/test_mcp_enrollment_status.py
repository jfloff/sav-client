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
  license = 301772  # int on every surface — see sav_shared.identifiers
  name = "Jogador Teste"
  club = "Rio Maior Basket"
  club_id = 7
  association = "AB Santarém"
  tier = "Sub 14"
  tier_id = 5
  gender = "Masculino"
  gender_id = 1
  birth_date = "2012-01-01"
  nationality = "Brasil"
  status = "FBP"
  season = "2025/2026"
  active = True


def test_not_enrolled_returns_projected_foreign_born_checklist(monkeypatch):
  class StubClient:
    session = {"organizacao": 7}

    def resolve_batch_id_by_license(self, license, *, include_submitted=False):
      assert include_submitted, "status must scan every pending state, not only open batches"
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

    def resolve_batch_id_by_license(self, license, *, include_submitted=False):
      assert include_submitted, "status must scan every pending state, not only open batches"
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

    def resolve_batch_id_by_license(self, license, *, include_submitted=False):
      assert include_submitted, "status must scan every pending state, not only open batches"
      raise LicenseNotEnrolledError(license, open_batches=[])

    def search_players(self, license, club, status):
      return [_Player()]  # active in roster

    def load_player_profile(self, license, club_id=None):
      return {"nacional": "200"}

  monkeypatch.setattr(server_module, "_get_client", lambda: StubClient())

  result = server_module.get_enrollment_status(license=301772)

  assert result["status"] == "enrolled"
  assert result["player"]["license"] == 301772
  assert "id" not in result["player"]
  assert result["checklist"]["scenario"] == "foreign_born"


def test_profile_failure_defaults_to_foreign_born(monkeypatch):
  class StubClient:
    session = {"organizacao": 7}

    def resolve_batch_id_by_license(self, license, *, include_submitted=False):
      assert include_submitted, "status must scan every pending state, not only open batches"
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

    def resolve_batch_id_by_license(self, license, *, include_submitted=False):
      assert include_submitted, "status must scan every pending state, not only open batches"
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


# ── available_doc_types ───────────────────────────────────────────────────────
# Documents the caller holds OUTSIDE SAV. The design point is that omitting the
# parameter changes nothing, so the first test pins that for all three statuses.


class _NotEnrolledClient:
  session = {"organizacao": 7}

  def resolve_batch_id_by_license(self, license, *, include_submitted=False):
    raise LicenseNotEnrolledError(license, open_batches=[{"number": "B1"}])

  def search_players(self, license, club, status):
    return []

  def load_player_profile(self, license, club_id=None):
    return {"nacional": "155"}  # Portugal


class _Batch:
  id = 12
  number = "B1"
  type_id = 2
  type = "Revalidação"
  state = "Aberta"


class _PendingClient:
  """A live batch that already holds one uploaded document (exame_medico)."""

  session = {"organizacao": 7}

  def resolve_batch_id_by_license(self, license, *, include_submitted=False):
    return 12

  def list_player_registration_batches(self):
    return [_Batch()]

  def load_existing_registration_record(self, batch_id, license):
    return {"nacional": "155"}

  def list_player_registration_documents(self, batch_id, license):
    # Derived, not hard-coded: the doc-type <-> tipo_doc map is SAV's, and a
    # literal here would silently test the wrong document if it ever moved.
    from sav_shared.lookups import doc_type_to_tipo_doc
    return [{"tipo_doc": doc_type_to_tipo_doc("exame_medico")}]

  def batch_item_subida(self, batch_id, license):
    return {
      "status": "pending", "tier_from": "Sub 16", "tier_to": "Sub 18",
      "approved_on": None,
    }


def _doc_type_for_tipo(tipo_doc):
  from sav_shared.lookups import tipo_doc_to_doc_type
  return tipo_doc_to_doc_type(tipo_doc).value


def test_omitting_available_doc_types_changes_nothing(monkeypatch):
  """Hard requirement: the response is byte-for-byte what it was without the param."""
  for client_cls in (_NotEnrolledClient, _PendingClient):
    monkeypatch.setattr(server_module, "_get_client", lambda c=client_cls: c())
    baseline = server_module.get_enrollment_status(license=301772)
    explicit_none = server_module.get_enrollment_status(
      license=301772, available_doc_types=None,
    )
    empty_list = server_module.get_enrollment_status(
      license=301772, available_doc_types=[],
    )
    assert baseline == explicit_none == empty_list
    assert "available_doc_types" not in baseline
    assert "counts_include_available" not in (baseline["checklist"] or {})


def test_available_docs_satisfy_a_not_enrolled_checklist(monkeypatch):
  """A player SAV holds nothing for can still be shown as document-complete."""
  monkeypatch.setattr(server_module, "_get_client", lambda: _NotEnrolledClient())

  before = server_module.get_enrollment_status(license=301772)
  required = [row["doc_type"] for row in before["checklist"]["required"]
              for _ in range(row["min_count"])]

  after = server_module.get_enrollment_status(
    license=301772, available_doc_types=required,
  )

  assert after["status"] == "not_enrolled"
  assert after["checklist"]["missing"] == []
  assert after["checklist"]["counts_include_available"] is True
  # SAV still holds no batch uploads, so the counts remain a projection.
  assert after["checklist"]["projected"] is True
  assert after["available_doc_types"] == required


def test_pending_checklist_unions_batch_uploads_with_supplied_docs(monkeypatch):
  """The answer becomes "what is still missing overall", not "what has SAV got"."""
  monkeypatch.setattr(server_module, "_get_client", lambda: _PendingClient())
  from sav_shared.lookups import doc_type_to_tipo_doc
  uploaded = _doc_type_for_tipo(doc_type_to_tipo_doc("exame_medico"))

  before = server_module.get_enrollment_status(license=301772)
  still_needed = [row["doc_type"] for row in before["checklist"]["required"]
                  for _ in range(row["min_count"] - row["found_count"])]
  assert uploaded not in still_needed, "the batch already supplies this one"

  after = server_module.get_enrollment_status(
    license=301772, available_doc_types=still_needed,
  )

  assert after["status"] == "pending"
  assert after["checklist"]["missing"] == []
  assert after["checklist"]["counts_include_available"] is True
  # The uploaded doc is still counted — the supplied set is a union, not a replacement.
  found = {row["doc_type"]: row["found_count"] for row in after["checklist"]["required"]}
  assert found.get(uploaded, 0) >= 1


def test_duplicates_are_significant(monkeypatch):
  """foreign_born needs TWO documento_identificacao, both filed under tipo_doc=18."""
  class _ForeignClient(_NotEnrolledClient):
    def load_player_profile(self, license, club_id=None):
      return {"nacional": "200"}  # Brazil

  monkeypatch.setattr(server_module, "_get_client", lambda: _ForeignClient())

  one = server_module.get_enrollment_status(
    license=301772, available_doc_types=["documento_identificacao"],
  )
  two = server_module.get_enrollment_status(
    license=301772, available_doc_types=["documento_identificacao"] * 2,
  )

  counts = {row["doc_type"]: row for row in one["checklist"]["required"]}
  assert counts["documento_identificacao"]["min_count"] == 2
  assert counts["documento_identificacao"]["satisfied"] is False
  counts2 = {row["doc_type"]: row for row in two["checklist"]["required"]}
  assert counts2["documento_identificacao"]["satisfied"] is True


def test_unknown_doc_type_raises(monkeypatch):
  """A silently dropped typo would report a document the caller supplied as missing."""
  import pytest

  monkeypatch.setattr(server_module, "_get_client", lambda: _NotEnrolledClient())

  with pytest.raises(ValueError):
    server_module.get_enrollment_status(
      license=301772, available_doc_types=["exame_medic"],
    )


def test_a_bare_string_is_rejected_not_iterated(monkeypatch):
  """"exame_medico" is 12 characters, not 12 documents."""
  import pytest

  monkeypatch.setattr(server_module, "_get_client", lambda: _NotEnrolledClient())

  with pytest.raises(ValueError):
    server_module.get_enrollment_status(
      license=301772, available_doc_types="exame_medico",
    )


def test_grounded_checklist_declares_its_nationality_came_from_sav(monkeypatch):
  """The pair is shape-identical, so provenance is what tells them apart.

  A consumer holding a checklist can refuse to act on a caller-asserted
  nationality for a player SAV could have grounded.
  """
  for client_cls in (_NotEnrolledClient, _PendingClient):
    monkeypatch.setattr(server_module, "_get_client", lambda c=client_cls: c())
    checklist = server_module.get_enrollment_status(license=301772)["checklist"]
    assert checklist["nationality_source"] == "sav_record"


def test_pending_status_carries_the_lote_subida(monkeypatch):
  monkeypatch.setattr(server_module, "_get_client", lambda: _PendingClient())
  result = server_module.get_enrollment_status(license=301772)
  assert result["status"] == "pending"
  assert result["subida"] == {
    "status": "pending", "tier_from": "Sub 16", "tier_to": "Sub 18",
    "approved_on": None,
  }
