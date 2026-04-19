"""
Integration tests for SavClient.login() — hits the real SAV2 website.

Requires a valid .env file with SAV_BASE_URL, SAV_USERNAME, SAV_PASSWORD.
"""

import pytest

from sav_client import SavClient
from sav_client.exceptions import SavAuthError, SavConfigError, SavConnectionError


@pytest.fixture(scope="module")
def client():
  return SavClient.from_env()


# ---------------------------------------------------------------------------
# Successful login
# ---------------------------------------------------------------------------

class TestLoginSuccess:
  def test_returns_success(self, client):
    result = client.login()
    assert result.success is True

  def test_session_is_populated(self, client):
    client.login()
    assert client.session is not None
    assert bool(client.session)

  def test_redirect_is_set(self, client):
    result = client.login()
    assert result.redirect is not None

  def test_raw_response_has_val(self, client):
    result = client.login()
    assert result.raw.get("val") == 1


# ---------------------------------------------------------------------------
# Auth failure — wrong password
# ---------------------------------------------------------------------------

class TestLoginAuthFailure:
  def test_wrong_password_raises_sav_auth_error(self):
    c = SavClient.from_env()
    c._password = "wrongpassword"
    with pytest.raises(SavAuthError):
      c.login()

  def test_session_remains_none_on_failure(self):
    c = SavClient.from_env()
    c._password = "wrongpassword"
    with pytest.raises(SavAuthError):
      c.login()
    assert c.session is None


# ---------------------------------------------------------------------------
# Connection error — bad URL
# ---------------------------------------------------------------------------

class TestLoginConnectionError:
  def test_bad_url_raises_sav_connection_error(self):
    c = SavClient("https://doesnotexist.fpb.invalid", "user", "pass", timeout=5)
    with pytest.raises(SavConnectionError):
      c.login()


# ---------------------------------------------------------------------------
# Config errors — missing credentials
# ---------------------------------------------------------------------------

class TestConfigErrors:
  def test_empty_base_url_raises(self):
    with pytest.raises(SavConfigError, match="base_url"):
      SavClient("", "user", "pass")

  def test_empty_username_raises(self):
    with pytest.raises(SavConfigError, match="username"):
      SavClient("https://sav2.fpb.pt", "", "pass")

  def test_empty_password_raises(self):
    with pytest.raises(SavConfigError, match="password"):
      SavClient("https://sav2.fpb.pt", "user", "")
