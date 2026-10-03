"""Batch state history (guiasdb op=10) and the Devolvida return reason.

The op=10 HTML mirrors lote 351 as captured live on 2026-10-01, names replaced:
returned once for a missing club stamp, then resubmitted and moved on to
Em Pagamento. SAV's markup is malformed (a stray ``</div>``); kept as-is.
"""

import json

import pytest

from sav_client.exceptions import SavResponseError
from sav_client.models import BatchStateChange, PlayerRegistrationBatch
from sav_client.sav_client import SavClient
from sav_mcp import server as server_module


def _state(state, user, when):
  return (
    f"<tr>\r\n                <td>{state}</td><td>{user}</td><td>{when}</td>"
    "\r\n        \r\n      </tr>"
  )


def _motivo(reason):
  return (
    "<tr><td colspan='3' class='text-wrap'><strong>Motivo: </strong>"
    f"{reason}</td></tr>"
  )


def _history_html(*rows):
  return (
    "<div class='table-responsive'>\r\n    <table class='table table-hover'>"
    "<thead><tr><th>Estado</th><th>Utilizador</th><th>Data/Hora</th></tr>"
    f"</thead><tbody>{''.join(rows)}</tbody></table></div></div>"
  )


LOTE_351 = _history_html(
  _state("Em construção", "Club", "27-09-2026 12:43:10"),
  _state("Em Validação", "Club", "28-09-2026 22:13:20"),
  _state("Devolvido", "Officer", "29-09-2026 18:29:05"),
  _motivo("Modelo 1 - Falta assinatura/carimbo clube."),
  _state("Em Validação", "Club", "30-09-2026 22:53:03"),
  _state("Em Pagamento", "Officer", "01-10-2026 12:13:19"),
)


def _client(monkeypatch, body):
  client = SavClient("https://example.invalid", "user", "pass")
  client.session = {"user": "u", "perfil": 1, "organizacao": 2}
  sent = []

  def _post_form(path, payload, *, params=None):
    sent.append((path, payload, params))
    return body if isinstance(body, str) else json.dumps(body)

  monkeypatch.setattr(client, "_post_form", _post_form)
  return client, sent


def _batch(state, id=638204):
  return PlayerRegistrationBatch(
    id=id, number="351", type_id=1, type="1º Inscrição",
    association_id=0, association="", club_id=0, club="",
    tier_id=3, tier="Sub 16", gender_id=1, gender="Masculino",
    state_id=0, state=state, state_date="2026-09-29",
    item_count=1, season_id=65, season="2026/2027",
  )


class TestStateHistory:
  def test_reads_every_state_and_attaches_the_reason_to_its_return(self, monkeypatch):
    client, sent = _client(monkeypatch, {"msg": LOTE_351, "guia": "351"})

    history = client.get_batch_state_history(638204)

    assert sent == [("php/guiasdb.php", {"guiaid": 638204}, {"op": "10"})]
    assert history == [
      BatchStateChange("Em construção", "Club", "2026-09-27T12:43:10"),
      BatchStateChange("Em Validação", "Club", "2026-09-28T22:13:20"),
      BatchStateChange(
        "Devolvido", "Officer", "2026-09-29T18:29:05",
        reason="Modelo 1 - Falta assinatura/carimbo clube.",
      ),
      BatchStateChange("Em Validação", "Club", "2026-09-30T22:53:03"),
      BatchStateChange("Em Pagamento", "Officer", "2026-10-01T12:13:19"),
    ]

  def test_unparseable_timestamp_is_passed_through(self, monkeypatch):
    client, _ = _client(
      monkeypatch, {"msg": _history_html(_state("Em construção", "Club", "ontem"))},
    )
    assert client.get_batch_state_history(1)[0].changed_at == "ontem"

  def test_missing_msg_raises_rather_than_reading_as_no_history(self, monkeypatch):
    client, _ = _client(monkeypatch, {"guia": "351"})
    with pytest.raises(SavResponseError):
      client.get_batch_state_history(1)


class TestReturnReason:
  def test_latest_return_wins(self, monkeypatch):
    client, _ = _client(monkeypatch, {"msg": _history_html(
      _state("Devolvido", "Officer", "29-09-2026 18:29:05"),
      _motivo("Modelo 1 - Falta assinatura/carimbo clube."),
      _state("Em Validação", "Club", "30-09-2026 22:53:03"),
      _state("Devolvido", "Officer", "01-10-2026 09:00:00"),
      _motivo("Exame médico ilegível."),
    )})
    assert client.get_batch_return_reason(1) == "Exame médico ilegível."

  def test_return_without_motivo_is_none(self, monkeypatch):
    client, _ = _client(monkeypatch, {"msg": _history_html(
      _state("Devolvido", "Officer", "29-09-2026 18:29:05"),
    )})
    assert client.get_batch_return_reason(1) is None

  def test_never_returned_is_none(self, monkeypatch):
    client, _ = _client(monkeypatch, {"msg": _history_html(
      _state("Em construção", "Club", "27-09-2026 12:43:10"),
    )})
    assert client.get_batch_return_reason(1) is None


@pytest.mark.parametrize("state, returned", [
  ("Devolvida", True), ("Em construção", False),
  ("Em Validação", False), ("Em Pagamento", False),
])
def test_is_returned(state, returned):
  assert _batch(state).is_returned is returned


class _ListingClient:
  def __init__(self, batches):
    self.batches = batches
    self.reason_reads = []

  def list_player_registration_batches(self, season=None):
    return self.batches

  def get_batch_return_reason(self, batch_id):
    self.reason_reads.append(batch_id)
    return "Modelo 1 - Falta assinatura/carimbo clube."


def test_list_batches_reads_the_reason_only_for_returned_batches(monkeypatch):
  client = _ListingClient([_batch("Devolvida", id=1), _batch("Em Validação", id=2)])
  monkeypatch.setattr(server_module, "_get_client", lambda: client)

  rows = server_module.list_batches()

  assert client.reason_reads == [1]
  assert [r["return_reason"] for r in rows] == [
    "Modelo 1 - Falta assinatura/carimbo clube.", None,
  ]


def test_get_batch_carries_the_reason(monkeypatch):
  client = _ListingClient([_batch("Devolvida")])
  monkeypatch.setattr(server_module, "_get_client", lambda: client)

  row = server_module.get_batch("351")

  assert row["return_reason"] == "Modelo 1 - Falta assinatura/carimbo clube."
