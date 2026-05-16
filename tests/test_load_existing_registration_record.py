"""Tests for `load_existing_registration_record` response classification.

The probe used by `resolve_batch_id_by_license` must be able to tell apart:
  - a clean "record not in this batch" response (SavRecordNotFoundError),
  - a server-side error / unexpected shape (SavResponseError),
  - a transport / parse failure (SavConnectionError / SavResponseError),
so the resolver doesn't mask real failures as stale-cache misses.
"""

import pytest

from sav_client.exceptions import (
  SavConnectionError,
  SavRecordNotFoundError,
  SavResponseError,
)
from sav_client.sav_client import SavClient


class _StubResponse:
  def __init__(self, text: str, status: int = 200) -> None:
    self.text = text
    self._status = status

  def raise_for_status(self) -> None:
    if self._status >= 400:
      import requests
      raise requests.exceptions.HTTPError(f"{self._status}")


class _StubHttp:
  def __init__(self, response: _StubResponse) -> None:
    self._response = response

  def get(self, *args, **kwargs):
    return self._response


def _client_with_response(response: _StubResponse) -> SavClient:
  client = SavClient.__new__(SavClient)
  client.session = {"user": "u"}
  client._http = _StubHttp(response)
  client._timeout = 5
  client._base_url = "https://example.invalid"
  return client


def test_returns_record_when_id_present(monkeypatch):
  client = _client_with_response(_StubResponse('{"id": 77, "nome": "A", "existe": 1}'))
  monkeypatch.setattr(client, "_url", lambda path: "https://example.invalid" + path, raising=False)

  out = client.load_existing_registration_record(batch_id=42, license=301772)
  assert out["id"] == 77


def test_raises_not_found_on_empty_payload(monkeypatch):
  client = _client_with_response(_StubResponse("{}"))
  monkeypatch.setattr(client, "_url", lambda path: "https://example.invalid" + path, raising=False)

  with pytest.raises(SavRecordNotFoundError):
    client.load_existing_registration_record(batch_id=42, license=301772)


def test_raises_not_found_on_existe_zero(monkeypatch):
  client = _client_with_response(_StubResponse('{"existe": 0}'))
  monkeypatch.setattr(client, "_url", lambda path: "https://example.invalid" + path, raising=False)

  with pytest.raises(SavRecordNotFoundError):
    client.load_existing_registration_record(batch_id=42, license=301772)


def test_raises_response_error_on_server_error_payload(monkeypatch):
  """A well-formed JSON error payload must NOT be classified as not-found.

  This is the case Codex flagged: previously every id-less payload became
  `SavRecordNotFoundError`, so an `{"error": "permission denied"}` from the
  server got silently swallowed by the resolver as a stale-cache miss.
  """
  client = _client_with_response(_StubResponse('{"error": "permission denied"}'))
  monkeypatch.setattr(client, "_url", lambda path: "https://example.invalid" + path, raising=False)

  with pytest.raises(SavResponseError) as excinfo:
    client.load_existing_registration_record(batch_id=42, license=301772)
  # Specifically not the narrower SavRecordNotFoundError.
  assert not isinstance(excinfo.value, SavRecordNotFoundError)


def test_raises_response_error_on_unexpected_shape(monkeypatch):
  """An id-less payload with neither a not-found signal nor an obvious error key."""
  client = _client_with_response(_StubResponse('{"some_unrelated_field": 7}'))
  monkeypatch.setattr(client, "_url", lambda path: "https://example.invalid" + path, raising=False)

  with pytest.raises(SavResponseError) as excinfo:
    client.load_existing_registration_record(batch_id=42, license=301772)
  assert not isinstance(excinfo.value, SavRecordNotFoundError)


def test_raises_response_error_on_invalid_json(monkeypatch):
  client = _client_with_response(_StubResponse("<html>500</html>"))
  monkeypatch.setattr(client, "_url", lambda path: "https://example.invalid" + path, raising=False)

  with pytest.raises(SavResponseError) as excinfo:
    client.load_existing_registration_record(batch_id=42, license=301772)
  assert not isinstance(excinfo.value, SavRecordNotFoundError)


@pytest.mark.parametrize(
  "payload",
  ["null", "[]", '"a string"', "42", "true"],
  ids=["null", "list", "string", "number", "boolean"],
)
def test_raises_response_error_on_non_dict_json(monkeypatch, payload):
  """Valid JSON but the wrong top-level shape must surface as `SavResponseError`,
  not crash with AttributeError on the downstream `.get(...)` calls."""
  client = _client_with_response(_StubResponse(payload))
  monkeypatch.setattr(client, "_url", lambda path: "https://example.invalid" + path, raising=False)

  with pytest.raises(SavResponseError) as excinfo:
    client.load_existing_registration_record(batch_id=42, license=301772)
  assert not isinstance(excinfo.value, SavRecordNotFoundError)
  assert "Unexpected existing-record payload type" in str(excinfo.value)


def test_raises_connection_error_on_request_exception(monkeypatch):
  import requests

  class _BrokenHttp:
    def get(self, *args, **kwargs):
      raise requests.exceptions.ConnectionError("network down")

  client = SavClient.__new__(SavClient)
  client.session = {"user": "u"}
  client._http = _BrokenHttp()
  client._timeout = 5
  monkeypatch.setattr(client, "_url", lambda path: "https://example.invalid" + path, raising=False)

  with pytest.raises(SavConnectionError, match="network down"):
    client.load_existing_registration_record(batch_id=42, license=301772)
