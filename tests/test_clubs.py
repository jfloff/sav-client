import pytest

from sav_client import SavClient
from sav_client.exceptions import SavResponseError


class TestListClubs:
  def test_requires_login(self):
    c = SavClient("https://sav2.fpb.pt", "user", "pass")
    with pytest.raises(SavResponseError, match="Must call login"):
      c.list_clubs()

  def test_requires_explicit_association_scope(self):
    c = SavClient("https://sav2.fpb.pt", "user", "pass")
    c.session = {"organizacao": 456}
    with pytest.raises(ValueError, match="association or all_associations=True is required"):
      c.list_clubs()

  def test_lists_live_clubs_for_all_associations(self, client):
    results = client.list_clubs(all_associations=True)

    assert results
    assert all(club.id > 0 for club in results)
    assert all(club.name for club in results)

  def test_lists_live_clubs_for_a_specific_association(self, client):
    associations = client.list_associations()
    assert associations

    results = []
    for association in associations:
      results = client.list_clubs(association=association.id)
      if results:
        break

    if not results:
      pytest.skip("No live associations returned any clubs")

    assert all(club.id > 0 for club in results)
    assert [club.name for club in results] == sorted(club.name for club in results)
