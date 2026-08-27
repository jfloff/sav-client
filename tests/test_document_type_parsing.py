"""The document type comes from the row label, never from deleteDoc's arguments.

Ground truth captured from production on 2026-08-27 (licence 298352, batch
630304). The record held a Modelo 1 and an Exame Médico, and SAV rendered:

    deleteDoc(2278594,298352,630304,1, 2)   <- Modelo 1
    deleteDoc(2278595,298352,630304,1, 2)   <- Exame Médico

The arguments are (galeria, licenca, guia, agente, tipo_guia) — identical but
for the galeria id. Reading the 4th as `tipo_doc` reported every document as a
Modelo 1, which made `list_player_documents` claim a medical exam was missing
when it was filed correctly, and made `replace_player_registration_document`
delete every document instead of the one type it was asked to replace.
"""

import pytest

from sav_client.sav_client import SavClient


def _row(galeria: int, label: str) -> str:
  return (
    f"<tr id='r{galeria}'>"
    f"<td><button onclick='goToPage(\"x.pdf\")'></button></td>"
    f"<td class='text-left'>\n                {label}</td>"
    f"<td>27-08-2026 13:17:06</td><td>Rio Maior Basket</td>"
    f"<td><button onclick='deleteDoc({galeria},298352,630304,1, 2)'></button></td>"
    f"</tr>"
  )


SELECT = (
  "<select id='tipo3'>"
  "<option value='1'>Modelo 1 - Inscrição jogadores (Primeira ou Revalidação)</option>"
  "<option value='2'>Exame Médico</option>"
  "<option value='6'>Modelo 4 - Subida de Escalão</option>"
  "<option value='99'>Novo Tipo Que Não Conhecemos</option>"
  "</select>"
)

LIVE_BODY = (
  "<table><tbody>"
  "<tr><td><button onclick='checkDoc(1,780605,298352,630304)'></button></td></tr>"
  + _row(2278594, "Modelo 1 - Inscrição jogadores (Primeira ou Revalidação)")
  + _row(2278595, "Exame Médico")
  + "</tbody></table>" + SELECT
)


@pytest.fixture
def client(monkeypatch):
  c = SavClient.__new__(SavClient)
  c.base_url = "https://sav2.example/"
  c.session = {"perfil": 1, "user": "u", "organizacao": 1, "epoca_id": 2026}
  c._timeout = 10
  return c


def _fetch(client, monkeypatch, body):
  import json
  batch = type("BatchStub", (), {"id": 630304, "type_id": 2})()
  monkeypatch.setattr(
    client, "_get",
    lambda *a, **k: type("R", (), {"text": json.dumps({"body": body, "num": 3})})(),
    raising=False,
  )
  return client._fetch_registration_documents(batch, 298352)


class TestDocumentTypeFromLabel:
  def test_distinguishes_types_that_share_deletedoc_arguments(self, client, monkeypatch):
    _, _, docs = _fetch(client, monkeypatch, LIVE_BODY)
    assert docs == [
      {"doc_id": 2278594, "tipo_doc": 1},
      {"doc_id": 2278595, "tipo_doc": 2},
    ]

  def test_inscricao_id_still_parsed(self, client, monkeypatch):
    next_slot, inscricao, _ = _fetch(client, monkeypatch, LIVE_BODY)
    assert (next_slot, inscricao) == (3, 780605)

  def test_select_extends_the_static_label_map(self, client, monkeypatch):
    """A type only SAV knows about resolves via the modal's own <select>."""
    body = (
      "<table><tbody>"
      "<tr><td><button onclick='checkDoc(1,780605,298352,630304)'></button></td></tr>"
      + _row(99, "Novo Tipo Que Não Conhecemos")
      + "</tbody></table>" + SELECT
    )
    _, _, docs = _fetch(client, monkeypatch, body)
    assert docs == [{"doc_id": 99, "tipo_doc": 99}]

  def test_static_map_covers_a_response_without_a_select(self, client, monkeypatch):
    body = (
      "<table><tbody>"
      "<tr><td><button onclick='checkDoc(1,780605,298352,630304)'></button></td></tr>"
      + _row(2278595, "Exame Médico")
      + "</tbody></table>"
    )
    _, _, docs = _fetch(client, monkeypatch, body)
    assert docs == [{"doc_id": 2278595, "tipo_doc": 2}]

  def test_unknown_label_reports_zero_rather_than_guessing(self, client, monkeypatch):
    """tipo_doc=0 matches no real type, so replace_* leaves the row alone."""
    body = (
      "<table><tbody>"
      "<tr><td><button onclick='checkDoc(1,780605,298352,630304)'></button></td></tr>"
      + _row(4242, "Algo Completamente Novo")
      + "</tbody></table>"
    )
    _, _, docs = _fetch(client, monkeypatch, body)
    assert docs == [{"doc_id": 4242, "tipo_doc": 0}]

  def test_empty_document_list(self, client, monkeypatch):
    body = (
      "<table><tbody>"
      "<tr><td><button onclick='checkDoc(1,780605,298352,630304)'></button></td></tr>"
      "</tbody></table>" + SELECT
    )
    _, _, docs = _fetch(client, monkeypatch, body)
    assert docs == []


class TestReplaceOnlyTouchesItsOwnType:
  def test_replacing_a_modelo_1_does_not_delete_the_medical_exam(self, client, monkeypatch):
    """The data-loss case: every doc used to parse as tipo_doc=1."""
    deleted: list[int] = []
    batch = type("BatchStub", (), {"id": 630304, "type_id": 2, "is_open": True})()
    monkeypatch.setattr(
      client, "list_player_registration_batches", lambda: [batch], raising=False,
    )
    monkeypatch.setattr(
      client, "_fetch_registration_documents",
      lambda b, lic: (3, 780605, [
        {"doc_id": 2278594, "tipo_doc": 1},
        {"doc_id": 2278595, "tipo_doc": 2},
      ]),
      raising=False,
    )
    monkeypatch.setattr(
      client, "delete_player_registration_document", deleted.append, raising=False,
    )
    monkeypatch.setattr(
      client, "upload_player_registration_document",
      lambda *a, **k: None, raising=False,
    )

    client.replace_player_registration_document(630304, 298352, "x.pdf", tipo_doc=1)

    assert deleted == [2278594], "replace must not touch the exame_medico"
