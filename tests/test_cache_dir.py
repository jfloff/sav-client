from pathlib import Path

import pytest

from sav_client.cache import Cache, resolve_cache_dir


@pytest.fixture
def missing_home(monkeypatch, tmp_path):
  """Point ~/.sav at a path that does not exist, so the XDG rung is reachable."""
  home = tmp_path / "home" / ".sav"
  monkeypatch.setattr("sav_client.cache._CACHE_DIR", home)
  return home


def test_explicit_directory_beats_every_env_source(monkeypatch, tmp_path):
  monkeypatch.setenv("SAV_CACHE_DIR", str(tmp_path / "env"))
  monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
  assert resolve_cache_dir(tmp_path / "explicit") == tmp_path / "explicit"


def test_sav_cache_dir_beats_xdg_and_home(monkeypatch, tmp_path):
  monkeypatch.setenv("SAV_CACHE_DIR", str(tmp_path / "env"))
  monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
  assert resolve_cache_dir() == tmp_path / "env"


def test_existing_legacy_dir_beats_xdg(monkeypatch, tmp_path):
  legacy = tmp_path / "legacy"
  legacy.mkdir()
  monkeypatch.setattr("sav_client.cache._CACHE_DIR", legacy)
  monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
  assert resolve_cache_dir() == legacy


def test_xdg_used_when_legacy_dir_absent(monkeypatch, tmp_path, missing_home):
  monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
  assert resolve_cache_dir() == tmp_path / "xdg" / "sav"


def test_falls_back_to_legacy_dir_when_nothing_set(missing_home):
  assert resolve_cache_dir() == missing_home


def test_tilde_is_expanded(monkeypatch):
  monkeypatch.setenv("SAV_CACHE_DIR", "~/somewhere/sav")
  assert resolve_cache_dir() == Path.home() / "somewhere" / "sav"


def test_cache_writes_to_the_env_directory(monkeypatch, tmp_path):
  monkeypatch.setenv("SAV_CACHE_DIR", str(tmp_path / "vol"))
  cache = Cache()
  assert cache.path == tmp_path / "vol" / "cache.db"
  cache.record_player_ids([(12345, 999)])
  assert cache.path.exists()
  assert cache.get_player_id(12345) == 999


def test_cache_accepts_an_explicit_directory(monkeypatch, tmp_path):
  monkeypatch.setenv("SAV_CACHE_DIR", str(tmp_path / "env"))
  cache = Cache(tmp_path / "arg")
  cache.record_player_ids([(1, 2)])
  assert cache.path == tmp_path / "arg" / "cache.db"
  assert not (tmp_path / "env").exists()
