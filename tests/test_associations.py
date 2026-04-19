import pytest

from sav_client import SavClient
from sav_client.exceptions import SavResponseError


class TestListAssociations:
  def test_requires_login(self):
    c = SavClient("https://sav2.fpb.pt", "user", "pass")
    with pytest.raises(SavResponseError, match="Must call login"):
      c.list_associations()

  def test_extracts_and_sorts_live_associations(self, client):
    results = client.list_associations()

    assert results
    assert all(association.id > 0 for association in results)
    assert all(association.name for association in results)
    assert [association.name for association in results] == sorted(
      association.name for association in results
    )
