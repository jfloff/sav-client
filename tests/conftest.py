import pytest

from sav_client import SavClient


@pytest.fixture(scope="session")
def client():
  c = SavClient.from_env()
  c.login()
  return c


@pytest.fixture(scope="session")
def sample_player(client):
  club_id = int(client.session.get("organizacao") or 0)
  players = client.search_players(club=club_id)
  if not players:
    pytest.skip("Live SAV account has no visible players to use as a sample")
  return players[0]
