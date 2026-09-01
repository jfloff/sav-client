"""Unit tests for SavClient.get_current_season.

The current season is read straight from SAV2's season table (op=168 on
``incricoesdb.php``), whose ``arrayEpoca`` marks the active época with
``activa == "1"``. The whole point of the endpoint is that it does NOT depend
on any registration batch existing, so the season resolves off-season too.
"""
import json

import pytest

from sav_client import SavClient
from sav_client.exceptions import SavResponseError
from sav_client.models import Season
from sav_client.sav_client import _REGISTRATIONS_PATH, _REGISTRATIONS_SEASONS_OP


# A trimmed op=168 response: only arrayEpoca matters here. The active season is
# the one with activa == "1"; its id is an opaque epoca_id, not the year.
_SEASONS_RESPONSE = json.dumps({
  "msg": "",
  "val": 1,
  "arrayEpoca": [
    {"id": "65", "descricao": "2026/2027", "activa": "0"},
    {"id": "64", "descricao": "2025/2026", "activa": "1"},
    {"id": "63", "descricao": "2024/2025", "activa": "0"},
  ],
})


def _client(monkeypatch, response, *, forbid_batches=True):
  c = SavClient("https://sav2.fpb.pt", "user", "pass")
  c.session = {"user": "2430", "organizacao": 2430, "perfil": 4, "epoca_id": 64}
  monkeypatch.setattr(c, "_post_form", lambda *a, **k: response)
  if forbid_batches:
    # The season lookup must never fall back to scraping registration batches.
    def _no_batches(*a, **k):
      raise AssertionError("get_current_season must not read registration batches")
    monkeypatch.setattr(c, "list_player_registration_batches", _no_batches)
  return c


class TestPreHttpGuards:
  def test_get_current_season_requires_login(self):
    c = SavClient("https://sav2.fpb.pt", "user", "pass")
    with pytest.raises(SavResponseError, match="Must call login"):
      c.get_current_season()


class TestResolvesActiveSeason:
  def test_resolves_without_any_batches(self, monkeypatch):
    c = _client(monkeypatch, _SEASONS_RESPONSE)

    season = c.get_current_season()

    assert isinstance(season, Season)
    assert season.id == 64
    assert season.label == "2025/2026"
    assert season.start_year == 2025
    assert season.end_year == 2026
    assert season.is_active is True

  def test_sends_op_168_with_session_identity(self, monkeypatch):
    c = SavClient("https://sav2.fpb.pt", "user", "pass")
    c.session = {"user": "2430", "organizacao": 2430, "perfil": 4, "epoca_id": 64}
    captured = {}

    def fake_post_form(path, payload, params=None):
      captured.update(path=path, payload=payload, params=params)
      return _SEASONS_RESPONSE

    monkeypatch.setattr(c, "_post_form", fake_post_form)
    c.get_current_season()

    assert captured["path"] == _REGISTRATIONS_PATH
    assert captured["params"] == {"op": _REGISTRATIONS_SEASONS_OP}
    assert captured["payload"] == {"perfil": 4, "user": "2430", "organizacao": 2430}


class TestErrorHandling:
  def test_no_active_season_raises(self, monkeypatch):
    resp = json.dumps({"arrayEpoca": [
      {"id": "64", "descricao": "2025/2026", "activa": "0"},
    ]})
    c = _client(monkeypatch, resp)
    with pytest.raises(SavResponseError, match="no active"):
      c.get_current_season()

  def test_missing_array_raises(self, monkeypatch):
    c = _client(monkeypatch, json.dumps({"val": 1}))
    with pytest.raises(SavResponseError, match="no active"):
      c.get_current_season()

  def test_malformed_label_raises(self, monkeypatch):
    resp = json.dumps({"arrayEpoca": [
      {"id": "64", "descricao": "not-a-season", "activa": "1"},
    ]})
    c = _client(monkeypatch, resp)
    with pytest.raises(SavResponseError, match="Could not parse active"):
      c.get_current_season()

  def test_non_json_response_raises(self, monkeypatch):
    c = _client(monkeypatch, "<html>oops</html>")
    with pytest.raises(SavResponseError, match="op=168"):
      c.get_current_season()
