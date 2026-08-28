import pytest
from click.testing import Result
from click.utils import strip_ansi

from sav_client import SavClient


@pytest.fixture(autouse=True)
def _strip_ansi_from_cli_output(monkeypatch):
  """Make plain-text CLI assertions independent of terminal colour support.

  Keep the original bytes on ``Result.output_bytes`` so CLI tests still run
  through the real Rich rendering path; only the text view used by assertions
  is normalized.
  """
  output = Result.output.fget
  assert output is not None
  monkeypatch.setattr(Result, "output", property(lambda result: strip_ansi(output(result))))


@pytest.fixture(autouse=True)
def _unset_club_stamp_path(monkeypatch):
  """Tests don't exercise the stamp overlay; isolate from the user's env."""
  monkeypatch.delenv("CLUB_STAMP_PATH", raising=False)


@pytest.fixture(autouse=True)
def _unset_cache_dir_env(monkeypatch):
  """Keep cache-dir resolution off the developer's env in every test."""
  monkeypatch.delenv("SAV_CACHE_DIR", raising=False)
  monkeypatch.delenv("XDG_CACHE_HOME", raising=False)


@pytest.fixture(scope="session")
def client():
  c = SavClient.from_env()
  c.login()
  return c


@pytest.fixture(scope="session")
def sample_player(client):
  club_id = int(client.session.get("organizacao") or 0)
  # All seasons: a freshly rolled-over season has no roster yet, and skipping
  # every sample_player test each August hides real regressions.
  players = client.search_players(club=club_id, season=0)
  if not players:
    pytest.skip("Live SAV account has no visible players to use as a sample")
  return players[0]
