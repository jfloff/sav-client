"""MCP tests for get_session_info — focused on the best-effort season label.

SAV2 stores the season only as the opaque ``epoca_id``; the ``"2025/2026"``
label has to be read back off a server object. get_session_info resolves it
best-effort, so a club with no batches still gets valid (label-less) info.
"""
import pytest

from sav_client.exceptions import SavResponseError
from sav_mcp import server as server_module


class _StubClient:
  def __init__(self, *, epoca_id: int = 2026, club_id: int = 200,
               season_year: int | None = 2025, raises: Exception | None = None):
    self.session = {
      "user": "bot",
      "perfil": "coach",
      "organizacao": club_id,
      "epoca_id": epoca_id,
    }
    self._season_year = season_year
    self._raises = raises

  def get_current_season_start_year(self) -> int:
    if self._raises is not None:
      raise self._raises
    return self._season_year


def test_includes_resolved_season_label(monkeypatch):
  stub = _StubClient(epoca_id=2026, club_id=200, season_year=2025)
  monkeypatch.setattr(server_module, "_get_client", lambda: stub)

  result = server_module.get_session_info()

  assert result["club_id"] == 200
  assert result["season_id"] == 2026
  assert result["season"] == "2025/2026"
  assert result["season_start_year"] == 2025


def test_label_is_best_effort_when_no_batches(monkeypatch):
  stub = _StubClient(raises=SavResponseError("no batches in current epoch"))
  monkeypatch.setattr(server_module, "_get_client", lambda: stub)

  result = server_module.get_session_info()

  # Core fields still present; only the label degrades to None.
  assert result["season_id"] == 2026
  assert result["season"] is None
  assert result["season_start_year"] is None


def test_raises_when_session_uninitialized(monkeypatch):
  class _NoSession:
    session = None

  monkeypatch.setattr(server_module, "_get_client", lambda: _NoSession())

  with pytest.raises(ValueError, match="Session not initialized"):
    server_module.get_session_info()
