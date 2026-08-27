"""Delete and remove operations must not report success on an HTTP-200 rejection.

SAV answers these endpoints with an undocumented body — sometimes empty — so the
client used to log it and assume the write landed. That turned a refusal into
`removed`/`deleted`/`success: true` at the MCP boundary while the row was still
in the batch, and the local cache had already forgotten it. We can't demand a
positive acknowledgement without inventing a contract, but we can refuse the two
failure shapes SAV actually produces.
"""

import pytest

from sav_client.exceptions import SavResponseError, SavServerError
from sav_client.sav_client import SavClient


PHP_FATAL = (
  "<br /><b>Fatal error</b>:  Uncaught mysqli_sql_exception: Cannot delete or "
  "update a parent row: a foreign key constraint fails (`sav2`.`guias`, "
  "CONSTRAINT `fk_guia_item`) in /var/www/html/php/regdb.php:412"
)


@pytest.fixture
def client():
  c = SavClient.__new__(SavClient)
  c.base_url = "https://sav2.example/"
  c.session = {"perfil": 1, "user": "u", "organizacao": 1, "epoca_id": 2026}
  c._timeout = 10
  return c


class TestCheckWriteResponse:
  def test_empty_body_is_still_success(self, client):
    # The common case: SAV says nothing at all. Unchanged behaviour.
    client._check_write_response("", "Could not delete batch 12")

  def test_non_json_body_is_still_success(self, client):
    client._check_write_response("OK", "Could not delete batch 12")

  def test_val_1_is_success(self, client):
    client._check_write_response('{"val": 1}', "Could not delete batch 12")

  def test_val_zero_is_a_rejection(self, client):
    with pytest.raises(SavResponseError, match="Could not delete batch 12"):
      client._check_write_response('{"val": 0, "msg": "Guia bloqueada"}', "Could not delete batch 12")

  def test_rejection_surfaces_savs_own_message(self, client):
    with pytest.raises(SavResponseError, match="Guia bloqueada"):
      client._check_write_response('{"val": 0, "msg": "Guia bloqueada"}', "Could not delete batch 12")

  def test_val_zero_as_string_is_also_a_rejection(self, client):
    # SAV is inconsistent about numeric vs string encodings across endpoints.
    with pytest.raises(SavResponseError):
      client._check_write_response('{"val": "0"}', "Could not delete batch 12")

  def test_php_fatal_raises_server_error(self, client):
    with pytest.raises(SavServerError):
      client._check_write_response(PHP_FATAL, "Could not delete document 9")

  def test_php_fatal_does_not_leak_the_body(self, client):
    with pytest.raises(SavServerError) as excinfo:
      client._check_write_response(PHP_FATAL, "Could not delete document 9")
    message = str(excinfo.value)
    for leak in ("mysqli_sql_exception", "Fatal error", "fk_guia_item", "regdb.php"):
      assert leak not in message

  def test_body_without_val_is_left_alone(self, client):
    # No `val` key means no verdict — we don't invent one.
    client._check_write_response('{"rows": 1}', "Could not delete batch 12")


class TestCacheIsNotUpdatedOnRejection:
  """A refused delete must not leave the client believing it succeeded."""

  def test_delete_batch_rejection_does_not_forget_licences(self, client, monkeypatch):
    forgotten: list[int] = []

    class _Cache:
      def forget_licenses_in_batch(self, batch_id):
        forgotten.append(batch_id)

    class _Resp:
      text = '{"val": 0, "msg": "Guia bloqueada"}'

      def raise_for_status(self):
        pass

    class _Http:
      def get(self, *a, **k):
        return _Resp()

    client._cache = _Cache()
    client._http = _Http()
    monkeypatch.setattr(client, "_invalidate_batch_memo", lambda: None, raising=False)

    with pytest.raises(SavResponseError):
      client.delete_player_registration_batch(12)

    assert forgotten == [], "cache was cleared for a delete SAV refused"


class TestBatchItemsDoesNotSwallowFatals:
  """A server error must not read back as an empty batch.

  Reproduced against production on 2026-08-27: op=169 for a batch holding one
  player answered HTTP 200 with a MariaDB syntax fatal. The HTML parser found
  no `editJogador(` rows in it and returned [], so the batch looked empty —
  indistinguishable from "no players yet", and the caller had no way to know
  SAV was broken. submit_enrollment's type-1 licence resolution reads this
  listing, so a silent [] there is not a cosmetic problem.
  """

  SQL_FATAL = (
    "<br />\n<b>Fatal error</b>:  Uncaught mysqli_sql_exception: You have an "
    "error in your SQL syntax; check the manual that corresponds to your "
    "MariaDB server version for the right syntax to use near "
    "'and inscricao_jogador.n_licenca=298352 order by guia.data_criacao desc "
    "limit 1' at line 6 in /usr/local/www/apache24/SADEV/php/incricoesdb.php:1964"
  )

  def _client(self, monkeypatch, body):
    c = SavClient.__new__(SavClient)
    c.base_url = "https://sav2.example/"
    c.session = {"perfil": 1, "user": "u", "organizacao": 1, "epoca_id": 2026}
    c._timeout = 10
    batch = type("BatchStub", (), {"id": 630304, "type_id": 2})()
    monkeypatch.setattr(
      c, "list_player_registration_batches", lambda: [batch], raising=False,
    )
    monkeypatch.setattr(
      c, "_get", lambda *a, **k: type("R", (), {"text": body})(), raising=False,
    )
    return c

  def test_fatal_raises_instead_of_returning_empty(self, monkeypatch):
    c = self._client(monkeypatch, self.SQL_FATAL)
    with pytest.raises(SavServerError):
      c.list_player_registration_batch_items(630304)

  def test_fatal_body_is_not_leaked(self, monkeypatch):
    c = self._client(monkeypatch, self.SQL_FATAL)
    with pytest.raises(SavServerError) as excinfo:
      c.list_player_registration_batch_items(630304)
    message = str(excinfo.value)
    for leak in ("mysqli_sql_exception", "incricoesdb.php", "n_licenca", "MariaDB"):
      assert leak not in message

  def test_a_genuinely_empty_batch_still_returns_empty(self, monkeypatch):
    c = self._client(monkeypatch, '{"msg": "<table><tbody></tbody></table>"}')
    assert c.list_player_registration_batch_items(630304) == []


class TestIdDocCheckStaysAdvisory:
  """op=163 must surface SAV's errors without ever blocking creation.

  Probed against production 2026-08-27: four of five real Cartão de Cidadão
  numbers already on file returned an unhandled PHP fatal
  (`mysqli_query(): Argument #2 ($query) cannot be empty`), and `"1"` cannot
  mean "already in use" — an invented 99999999 returns it while every random
  8-digit number returns an empty body. A hard gate here would reject most
  legitimate enrollments, so the duplicate defence stays on op=11.
  """

  FATAL = (
    "<br />\n<b>Fatal error</b>:  Uncaught ValueError: mysqli_query(): "
    "Argument #2 ($query) cannot be empty in "
    "/usr/local/www/apache24/SADEV/php/incricoesdb.php:28003"
  )

  def _client(self, monkeypatch, body):
    c = SavClient.__new__(SavClient)
    c.base_url = "https://sav2.example/"
    c.session = {"perfil": 1, "user": "u", "organizacao": 1}
    c._timeout = 10

    class _Http:
      def post(self, *a, **k):
        return type("R", (), {"text": body})()

    c._http = _Http()
    return c

  def test_fatal_does_not_raise(self, monkeypatch):
    c = self._client(monkeypatch, self.FATAL)
    c._check_primeira_id_doc("11546873")  # must not raise

  def test_fatal_is_logged_as_a_warning(self, monkeypatch, caplog):
    import logging
    c = self._client(monkeypatch, self.FATAL)
    with caplog.at_level(logging.WARNING, logger="sav_client.sav_client"):
      c._check_primeira_id_doc("11546873")
    assert any(r.levelno == logging.WARNING for r in caplog.records), (
      "a broken SAV endpoint must be visible, not swallowed"
    )

  def test_warning_does_not_leak_the_body(self, monkeypatch, caplog):
    import logging
    c = self._client(monkeypatch, self.FATAL)
    with caplog.at_level(logging.WARNING, logger="sav_client.sav_client"):
      c._check_primeira_id_doc("11546873")
    warnings = " ".join(
      r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
    )
    for leak in ("mysqli_query", "incricoesdb.php", "ValueError"):
      assert leak not in warnings

  def test_ordinary_replies_are_silent(self, monkeypatch, caplog):
    import logging
    for body in ("", "1"):
      c = self._client(monkeypatch, body)
      with caplog.at_level(logging.WARNING, logger="sav_client.sav_client"):
        c._check_primeira_id_doc("31864739")
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]
