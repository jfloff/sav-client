"""Standalone Subida add (op=50) and reading a Subida lote back (op=10).

Recorded live on 2026-09-24 (lote 335, a Sub 14 Masculino Subida lote). op=50
answered ``{"val":1,"msg":""}`` and the lote's listing counted the player, but
the client reported failure: SAV renders a Subida lote's item with
``editSub(...)`` instead of ``editJogador(...)``, and emits its cells straight
into ``<tbody>`` with no ``<tr>``. The markup below keeps that shape; the
licences and names are synthetic.
"""

import json
from types import SimpleNamespace

import pytest

from sav_client.exceptions import SavResponseError, SavWriteUnverifiedError
from sav_client.models import PlayerRegistrationBatch
from sav_client.sav_client import SavClient

LIC = 900001
BATCH = PlayerRegistrationBatch(
  id=637540, number="335", type_id=4, type="Subida de Escalão",
  association_id=7, association="AB Santarém", club_id=2430, club="Club",
  tier_id=5, tier="Sub 14", gender_id=1, gender="Masculino",
  state_id=1, state="Em construção", state_date="2026-09-24",
  item_count=0, season_id=65, season="2026/2027",
)

HEADER = (
  "<table><thead><tr><th></th><th>Nº Licença</th><th>Nome</th>"
  "<th>Data Nasc.</th><th>Nacionalidade</th><th>Estatuto</th><th>Taxa</th>"
  "<th>Seguro desportivo</th><th>Exame médico</th><th>Subida</th></tr></thead>"
)
FOOTER = "<tfoot><tr><td></td><td>0 €</td><td></td></tr></tfoot></table>"


def _subida_item(license, name):
  """One Subida-lote item as SAV renders it: cells with no enclosing <tr>."""
  return (
    f"<td class='text-center'><div><button onclick='editSub({license}, 637540,4)'>"
    f"</button><button onclick='eliJogador({license}, 637540,4)'></button>"
    f"<button onclick='seeJogador({license}, 637540,4)'></button></div></td>"
    f"<td>{license}</td><td> {name}</td><td>2015-01-01</td><td>Portugal</td>"
    f"<td>FBP</td><td class='text-right'>0 €</td>"
    f"<td>Seguro Jogador (Sub 14 M)</td><td>2026-09-04</td><td>Sub 14</td>"
  )


def _op10(*items):
  return json.dumps({"msg": f"{HEADER}<tbody>{''.join(items)}</tbody>{FOOTER}"})


OP48 = json.dumps({
  "body": (
    f"<select id='atleta'><option value='0'></option>"
    f"<option value='{LIC}'>{LIC} - Test Player</option></select>"
    "<select id='novataxa'><option value='0'></option>"
    "<option value='1096'>Isento Sub14 Masc FBP</option>"
    "<option value='1124'>Sub14 Masc FBP c/quota adicional</option></select>"
  ),
  "footer": "",
})


class _Http:
  """Routes SAV calls by op, recording each request. ``op10`` is a list of
  bodies served in turn to the lote read (the last one repeats)."""

  def __init__(self, *, op50, op10):
    self.op50, self.op10 = op50, list(op10)
    self.calls = []

  def _reply(self, op, data):
    self.calls.append((op, data))
    text = {
      "48": OP48,
      "49": json.dumps({"esco": "Mini 12", "estatuto": "6"}),
      "128": json.dumps({"msg": "<option value='0'></option><option value='1' >Seguro Jogador (Sub 14)</option>", "val": "1"}),
      "126": json.dumps({"msg": "<option value='4694' >Real Vida Seguros </option>"}),
      "24": " 94/000065",
      "134": json.dumps({"taxas": "<option value='0' ></option><option value='1096' >Isento Sub14 Masc FBP </option>"}),
      "50": self.op50,
    }.get(op)
    if op == "10":
      text = self.op10.pop(0) if len(self.op10) > 1 else self.op10[0]
    if text is None:
      raise AssertionError(f"unexpected op={op}")
    return SimpleNamespace(text=text, status_code=200, raise_for_status=lambda: None)

  def get(self, url, params=None, **kwargs):
    return self._reply(str((params or {}).get("op")), None)

  def post(self, url, params=None, data=None, **kwargs):
    return self._reply(str((params or {}).get("op")), data)


def _client(monkeypatch, http):
  client = SavClient("https://example.invalid", "user", "pass")
  client.session = {"user": 2430, "perfil": 4, "organizacao": 2430}
  client._http = http
  monkeypatch.setattr(client, "list_player_registration_batches", lambda season=None: [BATCH])
  monkeypatch.setattr(client._cache, "record_license_batch", lambda *a: None)
  return client


class TestSubidaLoteRead:
  def test_subida_item_without_tr_is_read(self, monkeypatch):
    client = _client(monkeypatch, _Http(op50="", op10=[_op10(_subida_item(LIC, "Test Player"))]))
    assert client.list_player_registration_batch_items(BATCH.id) == [{
      "license": LIC, "name": "Test Player",
      "subida": {
        "status": "pending", "tier_from": None, "tier_to": "Sub 14",
        "approved_on": None,
      },
    }]

  def test_several_subida_items_keep_their_own_cells(self, monkeypatch):
    client = _client(monkeypatch, _Http(op50="", op10=[_op10(
      _subida_item(LIC, "First Player"), _subida_item(LIC + 1, "Second Player"),
    )]))
    items = client.list_player_registration_batch_items(BATCH.id)
    assert [(i["license"], i["name"]) for i in items] == [
      (LIC, "First Player"), (LIC + 1, "Second Player"),
    ]

  def test_eligible_list_ignores_the_fee_select(self, monkeypatch):
    # 1096/1124 are novataxa fee ids from the same op=48 body, not licences.
    client = _client(monkeypatch, _Http(op50="", op10=[_op10()]))
    assert client._list_subida_licenses(BATCH) == {LIC}


class TestSubidaCommit:
  def test_recorded_exchange_succeeds(self, monkeypatch):
    http = _Http(
      op50='{"val":1,"msg":""}',
      op10=[_op10(_subida_item(LIC, "Test Player"))],  # the post-commit read
    )
    client = _client(monkeypatch, http)

    assert client._add_player_to_subida_batch(BATCH, LIC, taxa_id=None) == LIC
    [op50] = [data for op, data in http.calls if op == "50"]
    # Field for field what SAV's own saveSub() posts.
    assert op50 == {
      "atleta": LIC, "guia": 637540, "taxa": 1096, "companhia": 4694,
      "epoca": "2026/2027",
    }

  def test_acknowledged_but_unseen_is_unverified(self, monkeypatch):
    http = _Http(op50='{"val":1,"msg":""}', op10=[_op10()])
    client = _client(monkeypatch, http)
    with pytest.raises(SavWriteUnverifiedError, match="acknowledged it .val=1"):
      client._add_player_to_subida_batch(BATCH, LIC, taxa_id=None)

  def test_unacknowledged_body_is_described_not_quoted(self, monkeypatch):
    body = "<html>unexpected table guia_item</html>"
    http = _Http(op50=body, op10=[_op10()])
    client = _client(monkeypatch, http)
    with pytest.raises(SavResponseError) as excinfo:
      client._add_player_to_subida_batch(BATCH, LIC, taxa_id=None)
    assert not isinstance(excinfo.value, SavWriteUnverifiedError)
    assert f"a non-JSON body of {len(body)} bytes" in str(excinfo.value)
    assert "guia_item" not in str(excinfo.value)


class TestSubidaLoteCacheProbe:
  def test_cached_subida_lote_is_validated_by_rows_not_op30(self, monkeypatch):
    client = _client(monkeypatch, _Http(op50="", op10=[_op10(_subida_item(LIC, "P"))]))
    monkeypatch.setattr(client._cache, "get_batch_id_by_license", lambda lic: BATCH.id)

    def _op30(*a, **k):
      raise AssertionError("op=30 fatals on a Subida lote and must not be called")

    monkeypatch.setattr(client, "load_existing_registration_record", _op30)
    assert client.resolve_batch_by_license(LIC, include_submitted=True) is BATCH
