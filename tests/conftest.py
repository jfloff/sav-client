import pytest

from sav_client import SavClient


@pytest.fixture(scope="session")
def client():
  c = SavClient.from_env()
  c.login()
  return c


@pytest.fixture(scope="session")
def sample_player(client):
  players = client.search_players()
  if not players:
    pytest.skip("Live SAV account has no visible players to use as a sample")
  return players[0]
