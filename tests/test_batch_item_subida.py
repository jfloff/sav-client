"""Subida status read from a lote's op=10 rows, and merged across lotes.

The op=10 HTML mirrors lote 226 as captured live on 2026-09-24: a leading icon
column and a trailing "Subida" column holding the destination escalão of an
inline subida (blank otherwise).
"""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from sav_client.models import PlayerRegistrationBatch
from sav_client.sav_client import SavClient, _merge_subida

HEADER = (
  "<tr><th></th><th>Nº Licença</th><th>Nome</th><th>Data Nasc.</th>"
  "<th>Nacionalidade</th><th>Estatuto</th><th>Taxa</th>"
  "<th>Seguro desportivo</th><th>Exame médico</th><th>Subida</th></tr>"
)


def _row(license, subida):
  return (
    f"<tr><td><button onclick='editJogador({license}, 12,2)'></button></td>"
    f"<td>{license}</td><td>Player {license}</td><td>2011-03-30</td>"
    f"<td>Portugal</td><td>FBP</td><td>10 €</td><td>Seguro Jogador</td>"
    f"<td>2026-09-04</td><td>{subida}</td></tr>"
  )


def _batch(id=12, *, type_id=2, tier="Sub 16"):
  return PlayerRegistrationBatch(
    id=id, number=str(id), type_id=type_id, type="Revalidação",
    association_id=0, association="", club_id=0, club="",
    tier_id=3, tier=tier, gender_id=1, gender="Masculino",
    state_id=8, state="Em Pagamento", state_date="2026-09-18",
    item_count=1, season_id=65, season="2026/2027",
  )


def _items(monkeypatch, batch, html):
  client = SavClient("https://example.invalid", "user", "pass")
  client.session = {"user": "u", "perfil": 1, "organizacao": 2}
  monkeypatch.setattr(client, "_require_batch", lambda batch_id: batch)
  monkeypatch.setattr(
    client, "_get",
    lambda *a, **k: SimpleNamespace(text=json.dumps({"msg": html})),
  )
  return client.list_player_registration_batch_items(batch.id)


def _st(status, tier_from=None, tier_to=None):
  return {
    "status": status, "tier_from": tier_from, "tier_to": tier_to,
    "approved_on": None,
  }


class TestBatchItemSubida:
  def test_inline_subida_runs_from_the_lote_tier(self, monkeypatch):
    items = _items(
      monkeypatch, _batch(), f"<table>{HEADER}{_row(266798, 'Sub 18')}</table>",
    )
    assert items == [{
      "license": 266798, "name": "Player 266798",
      "subida": _st("pending", "Sub 16", "Sub 18"),
    }]

  @pytest.mark.parametrize("cell", ["", "- Não selecionado –"])
  def test_blank_cell_is_none(self, monkeypatch, cell):
    items = _items(monkeypatch, _batch(), f"<table>{HEADER}{_row(1, cell)}</table>")
    assert items[0]["subida"] == _st("none")

  def test_subida_lote_is_pending_even_when_blank(self, monkeypatch):
    # Sitting in a type-4 lote *is* a filed standalone subida.
    items = _items(
      monkeypatch, _batch(type_id=4), f"<table>{HEADER}{_row(1, '')}</table>",
    )
    assert items[0]["subida"] == _st("pending")

  def test_missing_column_is_unknown(self, monkeypatch):
    header = HEADER.replace("<th>Subida</th>", "")
    row = _row(1, "Sub 18").replace("<td>Sub 18</td>", "")
    items = _items(monkeypatch, _batch(), f"<table>{header}{row}</table>")
    assert items[0]["subida"] == _st("unknown")

  def test_misaligned_row_is_unknown(self, monkeypatch):
    row = _row(1, "Sub 18").replace("<td>FBP</td>", "")
    items = _items(monkeypatch, _batch(), f"<table>{HEADER}{row}</table>")
    assert items[0]["subida"] == _st("unknown")


class TestMergeSubida:
  def test_pending_beats_unknown_beats_none(self):
    assert _merge_subida([_st("none"), _st("pending", "Sub 14", "Sub 16")]) == (
      _st("pending", "Sub 14", "Sub 16")
    )
    assert _merge_subida([_st("none"), _st("unknown")]) == _st("unknown")

  def test_no_lote_is_none(self):
    assert _merge_subida([]) == _st("none")


def _classifying_client(monkeypatch, batches, items):
  client = SavClient.__new__(SavClient)
  client._cache = SimpleNamespace(record_license_batch=lambda *a: None)
  client.session = {"organizacao": 7}
  monkeypatch.setattr(
    client, "list_player_registration_batches",
    lambda season=None: batches, raising=False,
  )
  monkeypatch.setattr(
    client, "list_player_registration_batch_items",
    lambda batch_id: items.get(batch_id, []), raising=False,
  )
  monkeypatch.setattr(client, "search_players", lambda **kw: [], raising=False)
  return client


class TestAcrossLotes:
  """A licence in a blank Revalidação *and* a Subida lote must read the
  Subida, whichever lote the listing puts first."""

  BATCHES = [_batch(12), replace(_batch(13, type_id=4), type="Subida de Escalão")]
  ITEMS = {
    12: [{"license": 1, "name": "P", "subida": _st("none")}],
    13: [{"license": 1, "name": "P", "subida": _st("pending", None, "Sub 18")}],
  }

  def test_bulk_merges_every_lote(self, monkeypatch):
    client = _classifying_client(monkeypatch, self.BATCHES, self.ITEMS)
    out = client.classify_enrollment_status([1])
    assert out[1]["batch"]["number"] == "12"          # first lote still reported
    assert out[1]["subida"] == _st("pending", None, "Sub 18")

  def test_single_licence_merges_every_lote(self, monkeypatch):
    client = _classifying_client(monkeypatch, self.BATCHES, self.ITEMS)
    assert client.pending_subida_status(1) == _st("pending", None, "Sub 18")

  def test_item_without_subida_reads_unknown(self, monkeypatch):
    client = _classifying_client(
      monkeypatch, [_batch(12)], {12: [{"license": 1, "name": "P"}]},
    )
    assert client.classify_enrollment_status([1])[1]["subida"] == _st("unknown")
