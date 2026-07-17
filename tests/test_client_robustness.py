"""
Unit tests for SavClient robustness features (no live SAV2 server):

  * Session-expiry re-login + replay on _post / _post_form / _get.
  * Single transient-network retry on idempotent GETs.
  * Short-TTL in-process memo for list_player_registration_batches, plus its
    invalidation on create / delete / add / remove / login / invalidate_cache.

These mock the HTTP transport (``client._http``) and, where a real DB write
would otherwise happen, the SQLite ``client._cache`` — following the
monkeypatch conventions in test_registration_batches.py.
"""

import json

import pytest
import requests

from sav_client import SavClient
from sav_client.exceptions import SavConnectionError, SavResponseError


# ─── helpers ─────────────────────────────────────────────────────────────────

_LOGIN_PAGE = (
  "<!DOCTYPE html><html><head><title>SAV2</title></head><body>"
  "<form action='php/logindb.php'>"
  "<input type='password' name='pass'></form></body></html>"
)


def _make_client(monkeypatch):
  """A logged-in-looking client with the SQLite cache stubbed to no-ops."""
  c = SavClient("https://sav2.fpb.pt", "user", "pass")
  c.session = {"organizacao": "270", "epoca_id": "20", "perfil": 1, "user": "u"}
  # Keep the real cache DB untouched.
  monkeypatch.setattr(c._cache, "record_batches", lambda pairs: None)
  monkeypatch.setattr(c._cache, "record_license_batch", lambda lic, bid: None)
  monkeypatch.setattr(c._cache, "forget_license_batch", lambda lic: None)
  monkeypatch.setattr(c._cache, "forget_licenses_in_batch", lambda bid: None)
  monkeypatch.setattr(c._cache, "invalidate", lambda: None)
  return c


def _batches_payload(*ids):
  rows = [
    {
      "guia_id": i, "numero_guia": f"2025/{i:05d}", "idtipo_guia": 2,
      "idgenero": 1, "idescalao": 7, "idestado": 1, "num": 0,
    }
    for i in ids
  ]
  return json.dumps({"data": rows})


class _Resp:
  def __init__(self, text):
    self.text = text

  def raise_for_status(self):
    return None

  def json(self):
    return json.loads(self.text)


# ─── memo: hit within TTL means no second HTTP call ──────────────────────────

class TestBatchListingMemo:
  def test_memo_hit_within_ttl_skips_second_http_call(self, monkeypatch):
    c = _make_client(monkeypatch)
    calls = {"n": 0}

    def fake_post_form(path, payload, *, params=None):
      calls["n"] += 1
      return _batches_payload(101)

    monkeypatch.setattr(c, "_post_form", fake_post_form)

    first = c.list_player_registration_batches()
    second = c.list_player_registration_batches()

    assert calls["n"] == 1
    assert [b.id for b in first] == [101]
    assert [b.id for b in second] == [101]

  def test_memo_returns_defensive_copy(self, monkeypatch):
    c = _make_client(monkeypatch)
    monkeypatch.setattr(
      c, "_post_form", lambda p, pl, *, params=None: _batches_payload(101, 102)
    )
    first = c.list_player_registration_batches()
    first.clear()  # mutating the returned list must not corrupt the memo
    second = c.list_player_registration_batches()
    assert [b.id for b in second] == [101, 102]

  def test_memo_keyed_by_season(self, monkeypatch):
    c = _make_client(monkeypatch)
    calls = {"n": 0}

    def fake_post_form(path, payload, *, params=None):
      calls["n"] += 1
      return _batches_payload(101)

    monkeypatch.setattr(c, "_post_form", fake_post_form)
    c.list_player_registration_batches(season=20)
    c.list_player_registration_batches(season=21)
    assert calls["n"] == 2  # different seasons never share a memo entry

  def test_expired_ttl_refetches(self, monkeypatch):
    c = _make_client(monkeypatch)
    calls = {"n": 0}

    def fake_post_form(path, payload, *, params=None):
      calls["n"] += 1
      return _batches_payload(101)

    monkeypatch.setattr(c, "_post_form", fake_post_form)
    c.list_player_registration_batches()
    # Age the memo entry past the TTL by rewriting its timestamp.
    (ts, batches), = c._batch_memo.values()
    c._batch_memo[20] = (ts - 999.0, batches)
    c.list_player_registration_batches()
    assert calls["n"] == 2


# ─── memo invalidation on mutations ──────────────────────────────────────────

class TestBatchMemoInvalidation:
  def _prime_and_count(self, monkeypatch, c):
    calls = {"n": 0}

    def fake_post_form(path, payload, *, params=None):
      calls["n"] += 1
      return _batches_payload(101)

    monkeypatch.setattr(c, "_post_form", fake_post_form)
    c.list_player_registration_batches()  # populate memo (call 1)
    assert calls["n"] == 1
    return calls

  def test_create_invalidates(self, monkeypatch):
    c = _make_client(monkeypatch)
    calls = self._prime_and_count(monkeypatch, c)

    monkeypatch.setattr(
      c, "_resolve_tier_id", lambda tier, gender_id: 7,
    )
    monkeypatch.setattr(
      c, "_resolve_club_association_id", lambda club_id: 5,
    )
    monkeypatch.setattr(
      c, "_http",
      type("H", (), {
        "get": lambda self, *a, **k: _Resp('{"id": 202}'),
      })(),
    )
    c.create_player_registration_batch(type=2, tier=7, gender_id=1)
    c.list_player_registration_batches()  # must re-fetch (call 2)
    assert calls["n"] == 2

  def test_delete_invalidates(self, monkeypatch):
    c = _make_client(monkeypatch)
    calls = self._prime_and_count(monkeypatch, c)
    monkeypatch.setattr(
      c, "_http",
      type("H", (), {"get": lambda self, *a, **k: _Resp("ok")})(),
    )
    c.delete_player_registration_batch(101)
    c.list_player_registration_batches()
    assert calls["n"] == 2

  def test_remove_invalidates(self, monkeypatch):
    c = _make_client(monkeypatch)
    calls = self._prime_and_count(monkeypatch, c)
    batch = type("B", (), {"id": 101, "type_id": 2})()
    # remove re-lists once internally to find the batch (call 2), then the
    # explicit list after invalidation must re-fetch (call 3).
    monkeypatch.setattr(
      c, "_http",
      type("H", (), {"get": lambda self, *a, **k: _Resp("ok")})(),
    )
    monkeypatch.setattr(
      c, "list_player_registration_batches",
      lambda season=None: [batch],
    )
    c.remove_player_from_registration_batch(101, 301772)
    # After the real invalidation call, assert the memo is empty.
    assert c._batch_memo == {}

  def test_add_finalizer_invalidates(self, monkeypatch):
    # Exercise the invalidation hook directly: the subida/revalidação/primeira
    # finalizers all call _invalidate_batch_memo before returning.
    c = _make_client(monkeypatch)
    self._prime_and_count(monkeypatch, c)
    assert c._batch_memo != {}
    c._invalidate_batch_memo()
    assert c._batch_memo == {}

  def test_login_invalidates(self, monkeypatch):
    c = _make_client(monkeypatch)
    self._prime_and_count(monkeypatch, c)
    monkeypatch.setattr(
      c, "_post",
      lambda path, payload, *, params=None: {
        "val": 1, "msg": "", "sessao": {"epoca_id": "20"},
      },
    )
    c.login()
    assert c._batch_memo == {}

  def test_invalidate_cache_invalidates_memo(self, monkeypatch):
    c = _make_client(monkeypatch)
    self._prime_and_count(monkeypatch, c)
    c.invalidate_cache()
    assert c._batch_memo == {}


# ─── session-expiry re-login + replay ────────────────────────────────────────

class TestReauthReplay:
  def test_looks_like_login_page_signature(self):
    assert SavClient._looks_like_login_page(_LOGIN_PAGE) is True
    # A JSON business rejection is NOT treated as expiry.
    assert SavClient._looks_like_login_page('{"val": 0, "msg": "no"}') is False
    # Ordinary wizard HTML fragment (no login marker) is not a login page.
    assert SavClient._looks_like_login_page("<html><body>ok</body></html>") is False
    assert SavClient._looks_like_login_page("") is False

  def test_post_form_reauth_replays_once(self, monkeypatch):
    c = _make_client(monkeypatch)
    logins = {"n": 0}

    def fake_login():
      logins["n"] += 1
      return None

    monkeypatch.setattr(c, "login", fake_login)

    responses = [_Resp(_LOGIN_PAGE), _Resp(_batches_payload(101))]

    class H:
      def post(self, *a, **k):
        return responses.pop(0)

    monkeypatch.setattr(c, "_http", H())

    result = c.list_player_registration_batches()
    assert logins["n"] == 1                 # re-login fired exactly once
    assert [b.id for b in result] == [101]  # replayed request's payload returned
    assert responses == []                  # both queued responses consumed

  def test_post_json_reauth_replays_once(self, monkeypatch):
    c = _make_client(monkeypatch)
    logins = {"n": 0}
    monkeypatch.setattr(c, "login", lambda: logins.__setitem__("n", logins["n"] + 1))

    responses = [_Resp(_LOGIN_PAGE), _Resp('{"val": 1, "ok": true}')]

    class H:
      def post(self, *a, **k):
        return responses.pop(0)

    monkeypatch.setattr(c, "_http", H())
    data = c._post("php/some.php", {"x": 1}, params={"op": "9"})
    assert logins["n"] == 1
    assert data == {"val": 1, "ok": True}

  def test_get_reauth_replays_once(self, monkeypatch):
    c = _make_client(monkeypatch)
    logins = {"n": 0}
    monkeypatch.setattr(c, "login", lambda: logins.__setitem__("n", logins["n"] + 1))

    responses = [_Resp(_LOGIN_PAGE), _Resp("real body")]

    class H:
      def get(self, *a, **k):
        return responses.pop(0)

    monkeypatch.setattr(c, "_http", H())
    resp = c._get(c._url("php/x.php"), params={"op": "1"})
    assert logins["n"] == 1
    assert resp.text == "real body"

  def test_no_infinite_loop_when_reauth_also_expired(self, monkeypatch):
    # login() "succeeds" but the server keeps returning the login page. The
    # re-entrancy guard must stop after one re-login; the second login-page
    # body then surfaces as a non-JSON SavResponseError rather than looping.
    c = _make_client(monkeypatch)
    logins = {"n": 0}
    monkeypatch.setattr(c, "login", lambda: logins.__setitem__("n", logins["n"] + 1))

    class H:
      def post(self, *a, **k):
        return _Resp(_LOGIN_PAGE)

    monkeypatch.setattr(c, "_http", H())
    with pytest.raises(SavResponseError, match="non-JSON"):
      c._post("php/some.php", {"x": 1}, params={"op": "9"})
    assert logins["n"] == 1  # re-login fired exactly once, no loop

  def test_get_no_infinite_loop_when_reauth_also_expired(self, monkeypatch):
    c = _make_client(monkeypatch)
    logins = {"n": 0}
    monkeypatch.setattr(c, "login", lambda: logins.__setitem__("n", logins["n"] + 1))

    class H:
      def get(self, *a, **k):
        return _Resp(_LOGIN_PAGE)

    monkeypatch.setattr(c, "_http", H())
    # Second detection is suppressed by the guard, so _get returns the (still
    # login-page) response rather than recursing forever.
    resp = c._get(c._url("php/x.php"), params={"op": "1"})
    assert logins["n"] == 1
    assert resp.text == _LOGIN_PAGE


# ─── transient-network retry on GET ──────────────────────────────────────────

class TestTransientGetRetry:
  def test_single_retry_on_connection_error_then_success(self, monkeypatch):
    c = _make_client(monkeypatch)
    monkeypatch.setattr("sav_client.sav_client.time.sleep", lambda s: None)
    calls = {"n": 0}

    class H:
      def get(self, *a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
          raise requests.exceptions.ConnectionError("boom")
        return _Resp("recovered")

    monkeypatch.setattr(c, "_http", H())
    resp = c._get(c._url("php/x.php"), params={"op": "1"})
    assert calls["n"] == 2          # exactly one retry
    assert resp.text == "recovered"

  def test_retry_exhausted_raises_connection_error(self, monkeypatch):
    c = _make_client(monkeypatch)
    monkeypatch.setattr("sav_client.sav_client.time.sleep", lambda s: None)
    calls = {"n": 0}

    class H:
      def get(self, *a, **k):
        calls["n"] += 1
        raise requests.exceptions.Timeout("slow")

    monkeypatch.setattr(c, "_http", H())
    with pytest.raises(SavConnectionError):
      c._get(c._url("php/x.php"), params={"op": "1"})
    assert calls["n"] == 2  # original + one retry, then give up
