"""Offline tests for SAV2 season listing and recent-season resolution."""
import json

import pytest

from sav_client import SavClient
from sav_client.exceptions import SavResponseError


def _client(monkeypatch, entries):
  client = SavClient("https://example.invalid", "user", "pass")
  client.session = {"user": "u", "perfil": 1, "organizacao": 2}
  response = json.dumps({"arrayEpoca": entries})
  monkeypatch.setattr(client, "_post_form", lambda *args, **kwargs: response)
  return client


def test_list_seasons_parses_every_entry_and_marks_active(monkeypatch):
  client = _client(monkeypatch, [
    {"id": "65", "descricao": "2026/2027", "activa": "0"},
    {"id": "64", "descricao": "2025/2026", "activa": "1"},
    {"id": "63", "descricao": "2024/2025", "activa": "0"},
  ])

  seasons = client.list_seasons()

  assert [(season.id, season.label, season.start_year) for season in seasons] == [
    (65, "2026/2027", 2026),
    (64, "2025/2026", 2025),
    (63, "2024/2025", 2024),
  ]
  assert [season.id for season in seasons if season.is_active] == [64]
  assert seasons[1].raw == {
    "id": "64", "descricao": "2025/2026", "activa": "1",
  }


def test_list_seasons_skips_malformed_entry(monkeypatch):
  client = _client(monkeypatch, [
    {"id": "64", "descricao": "2025/2026", "activa": "1"},
    {"id": "broken", "descricao": "2024/2025", "activa": "0"},
    {"id": "63", "descricao": "2024/not-a-year", "activa": "0"},
    {"id": "61", "descricao": "2021/2023", "activa": "0"},
    {"id": "62", "descricao": "2023/2024", "activa": "0"},
  ])

  assert [season.id for season in client.list_seasons()] == [64, 62]


def test_list_seasons_raises_when_no_entry_parses(monkeypatch):
  client = _client(monkeypatch, [
    {"id": "broken", "descricao": "not-a-season", "activa": "0"},
    {"descricao": "2024/2025", "activa": "0"},
  ])

  with pytest.raises(SavResponseError, match="no season entries could be parsed"):
    client.list_seasons()


def test_get_current_season_returns_active(monkeypatch):
  client = _client(monkeypatch, [
    {"id": "65", "descricao": "2026/2027", "activa": "0"},
    {"id": "64", "descricao": "2025/2026", "activa": "1"},
  ])

  season = client.get_current_season()

  assert season.id == 64
  assert season.is_active is True
  assert season.end_year == 2026


def test_get_current_season_raises_when_none_is_active(monkeypatch):
  client = _client(monkeypatch, [
    {"id": "64", "descricao": "2025/2026", "activa": "0"},
  ])

  with pytest.raises(SavResponseError, match="no active época"):
    client.get_current_season()


def test_recent_season_ids_use_start_year_not_opaque_id(monkeypatch):
  client = _client(monkeypatch, [
    {"id": "71", "descricao": "2019/2020", "activa": "0"},
    {"id": "3", "descricao": "2024/2025", "activa": "0"},
    {"id": "64", "descricao": "2025/2026", "activa": "1"},
    {"id": "90", "descricao": "2022/2023", "activa": "0"},
  ])

  assert client._recent_season_ids() == [64, 3]


def test_list_seasons_is_memoised(monkeypatch):
  client = SavClient("https://example.invalid", "user", "pass")
  client.session = {"user": "u", "perfil": 1, "organizacao": 2}
  response = json.dumps({"arrayEpoca": [
    {"id": "64", "descricao": "2025/2026", "activa": "1"},
  ]})
  calls = 0

  def fake_post_form(*args, **kwargs):
    nonlocal calls
    calls += 1
    return response

  monkeypatch.setattr(client, "_post_form", fake_post_form)

  first = client.list_seasons()
  second = client.list_seasons()

  assert calls == 1
  assert second is first
