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

from sav_client.exceptions import SavResponseError
from sav_client.models import PlayerRegistrationBatch
from sav_client.sav_client import SavClient


def _row(galeria: int, label: str, file_path: str | None) -> str:
  view_button = (
    f"<button onclick='goToPage(\"{file_path}\")'></button>"
    if file_path is not None else ""
  )
  return (
    f"<tr id='r{galeria}'>"
    f"<td>{view_button}</td>"
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

# The view paths follow the shape every live capture shows —
# `uploads/galeria_docs/jogadores/<licenca>_<inscricao>_<epoch>.<ext>` — keyed
# to this fixture's own licence (298352). The 2026-08-27 capture predates any
# interest in the path, so its real filenames were not recorded; the *paths
# verbatim from production* live in SUBMITTED_BODY below.
LIVE_BODY = (
  "<table><tbody>"
  "<tr><td><button onclick='checkDoc(1,780605,298352,630304)'></button></td></tr>"
  + _row(
    2278594,
    "Modelo 1 - Inscrição jogadores (Primeira ou Revalidação)",
    "uploads/galeria_docs/jogadores/298352_780605_1756300626.pdf",
  )
  + _row(
    2278595,
    "Exame Médico",
    "uploads/galeria_docs/jogadores/298352_780605_1756300627.pdf",
  )
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
      {
        "doc_id": 2278594,
        "tipo_doc": 1,
        "file_path": "uploads/galeria_docs/jogadores/298352_780605_1756300626.pdf",
      },
      {
        "doc_id": 2278595,
        "tipo_doc": 2,
        "file_path": "uploads/galeria_docs/jogadores/298352_780605_1756300627.pdf",
      },
    ]

  def test_inscricao_id_still_parsed(self, client, monkeypatch):
    next_slot, inscricao, _ = _fetch(client, monkeypatch, LIVE_BODY)
    assert (next_slot, inscricao) == (3, 780605)

  def test_select_extends_the_static_label_map(self, client, monkeypatch):
    """A type only SAV knows about resolves via the modal's own <select>."""
    body = (
      "<table><tbody>"
      "<tr><td><button onclick='checkDoc(1,780605,298352,630304)'></button></td></tr>"
      + _row(99, "Novo Tipo Que Não Conhecemos", "uploads/unknown.pdf")
      + "</tbody></table>" + SELECT
    )
    _, _, docs = _fetch(client, monkeypatch, body)
    assert docs == [{"doc_id": 99, "tipo_doc": 99, "file_path": "uploads/unknown.pdf"}]

  def test_static_map_covers_a_response_without_a_select(self, client, monkeypatch):
    body = (
      "<table><tbody>"
      "<tr><td><button onclick='checkDoc(1,780605,298352,630304)'></button></td></tr>"
      + _row(2278595, "Exame Médico", "uploads/exame.pdf")
      + "</tbody></table>"
    )
    _, _, docs = _fetch(client, monkeypatch, body)
    assert docs == [{
      "doc_id": 2278595, "tipo_doc": 2, "file_path": "uploads/exame.pdf",
    }]

  def test_unknown_label_reports_zero_rather_than_guessing(self, client, monkeypatch):
    """tipo_doc=0 matches no real type, so replace_* leaves the row alone."""
    body = (
      "<table><tbody>"
      "<tr><td><button onclick='checkDoc(1,780605,298352,630304)'></button></td></tr>"
      + _row(4242, "Algo Completamente Novo", "uploads/unknown-label.pdf")
      + "</tbody></table>"
    )
    _, _, docs = _fetch(client, monkeypatch, body)
    assert docs == [{
      "doc_id": 4242, "tipo_doc": 0, "file_path": "uploads/unknown-label.pdf",
    }]

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


# ─── submitted batches render read-only ──────────────────────────────────────

# Captured verbatim from production on 2026-09-11: licence 257901 in batch
# 632478, state "Em Validação". Both documents are present and listed, but SAV
# has dropped every `checkDoc` and `deleteDoc` handler and the type <select>,
# because none of those actions are available on a submitted batch. This body
# is recorded, not hand-written — the bug was that the real shape did not match
# what the parser assumed, so an invented fixture could not have caught it.
SUBMITTED_BODY = """\
<div class='panel-body contactosdiv' >
                     
                      <h4>Tiago Sacramento Bernardino</h4>
                        <div class='table-responsive'><table class='table table-bordered'>
                    <thead>
                        <tr>
                            <th style='width:2%;' class='text-left'></th>
                            <th style='width:40%;' class='text-left'>Tipo de Documento</th>
                            <th>Data/Hora</th>
                            <th>Utilizador</th> </tr>
                    </thead>
                    <tbody><tr id='r1'>
                <td><span class='tooltip-r' data-toggle='tooltip' data-placement='bottom' title='Ver documento'><button type='button' onclick='goToPage("uploads/galeria_docs/jogadores/257901_783358_1789078995.pdf")' class='btn btn-default' ><i class='fa fa-file-pdf' style='color:#660d0d'></i></button></span></td>
                <td class='text-left'>
                Modelo 1 - Inscrição jogadores (Primeira ou Revalidação)</td>
                <td>10-09-2026 23:23:18</td>
                <td>Rio Maior Basket</td><tr id='r2'>
                <td><span class='tooltip-r' data-toggle='tooltip' data-placement='bottom' title='Ver documento'><button type='button' onclick='goToPage("uploads/galeria_docs/jogadores/257901_783358_1789078999.pdf")' class='btn btn-default' ><i class='fa fa-file-pdf' style='color:#660d0d'></i></button></span></td>
                <td class='text-left'>
                Exame Médico</td>
                <td>10-09-2026 23:23:19</td>
                <td>Rio Maior Basket</td></tbody></table></div>"""


def _fetch_for(client, monkeypatch, body, *, state_id, state):
  """Run the op=91 parse against `body` for a batch in the given state."""
  import json
  batch = PlayerRegistrationBatch(
    id=632478, number="99", type_id=2, type="Revalidação",
    association_id=7, association="AB Santarém",
    club_id=2430, club="Rio Maior Basket",
    tier_id=10, tier="Sub 18", gender_id=1, gender="Masculino",
    state_id=state_id, state=state, state_date="2026-09-11",
    item_count=1, season_id=65, season="2026/2027",
  )
  monkeypatch.setattr(
    client, "_get",
    lambda *a, **k: type("R", (), {"text": json.dumps({"body": body, "num": 3})})(),
    raising=False,
  )
  return batch, client._fetch_registration_documents(batch, 257901)


class TestSubmittedBatchIsStillReadable:
  """A submitted batch must report its documents, not crash and not lie."""

  def test_documents_are_listed_with_their_types(self, client, monkeypatch):
    _, (slot, inscricao, docs) = _fetch_for(
      client, monkeypatch, SUBMITTED_BODY, state_id=9, state="Em Validação",
    )
    # The types survive: they come from the row label via _DOC_TYPE_LABELS,
    # which is what makes a checklist readable for a submitted enrolment.
    assert [d["tipo_doc"] for d in docs] == [1, 2]
    assert [d["file_path"] for d in docs] == [
      "uploads/galeria_docs/jogadores/257901_783358_1789078995.pdf",
      "uploads/galeria_docs/jogadores/257901_783358_1789078999.pdf",
    ]
    assert slot == 3
    # The write-only ids do not. None here means "SAV withheld it", and must
    # never be read as "there are no documents".
    assert inscricao is None
    assert [d["doc_id"] for d in docs] == [None, None]

  def test_empty_document_list_is_not_the_answer(self, client, monkeypatch):
    """The silent-failure guard: anchoring on deleteDoc returned []."""
    _, (_, _, docs) = _fetch_for(
      client, monkeypatch, SUBMITTED_BODY, state_id=9, state="Em Validação",
    )
    assert len(docs) == 2, "a submitted batch's documents must still enumerate"

  def test_open_batch_missing_checkdoc_still_raises(self, client, monkeypatch):
    """Absence is expected only when submitted. On an *open* batch the same
    markup means SAV changed and we want to hear about it."""
    with pytest.raises(SavResponseError, match="inscricao"):
      _fetch_for(
        client, monkeypatch, SUBMITTED_BODY, state_id=1, state="Em construção",
      )

  def test_upload_refuses_a_submitted_batch_by_state(self, client, monkeypatch, tmp_path):
    """The old refusal was accidental (the inscricao parse raised). Now that
    reads tolerate it, the write path needs its own explicit guard."""
    batch, _ = _fetch_for(
      client, monkeypatch, SUBMITTED_BODY, state_id=9, state="Em Validação",
    )
    monkeypatch.setattr(
      client, "list_player_registration_batches", lambda: [batch], raising=False,
    )
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
    with pytest.raises(ValueError, match=r"Em Validação.*Em constru"):
      client.upload_player_registration_document(
        632478, 257901, str(pdf), tipo_doc=2,
      )


class TestDownloadRegistrationDocuments:
  def test_filters_by_type_and_returns_all_in_server_order(self, client, monkeypatch):
    batch = type("BatchStub", (), {"id": 632478, "is_open": False})()
    docs = [
      {
        "doc_id": None,
        "tipo_doc": 1,
        "file_path": "uploads/modelo.pdf",
      },
      {
        "doc_id": None,
        "tipo_doc": 2,
        "file_path": "uploads/exame.jpg",
      },
    ]
    monkeypatch.setattr(
      client, "list_player_registration_batches", lambda: [batch], raising=False,
    )
    monkeypatch.setattr(
      client, "_fetch_registration_documents",
      lambda b, lic: (3, None, docs),
      raising=False,
    )
    monkeypatch.setattr(
      client, "_download_registration_document_file",
      lambda path: {"uploads/modelo.pdf": b"%PDF-1.6", "uploads/exame.jpg": b"\xff\xd8\xff"}[path],
      raising=False,
    )

    filtered = client.download_player_registration_documents(632478, 257901, tipo_doc=2)
    all_docs = client.download_player_registration_documents(632478, 257901)

    assert filtered == [{
      "tipo_doc": 2,
      "file_path": "uploads/exame.jpg",
      "filename": "exame.jpg",
      "content": b"\xff\xd8\xff",
    }]
    assert [doc["filename"] for doc in all_docs] == ["modelo.pdf", "exame.jpg"]

  def test_does_not_refuse_a_submitted_batch(self, client, monkeypatch):
    batch, _ = _fetch_for(
      client, monkeypatch, SUBMITTED_BODY, state_id=9, state="Em Validação",
    )
    monkeypatch.setattr(
      client, "list_player_registration_batches", lambda: [batch], raising=False,
    )
    monkeypatch.setattr(
      client, "_download_registration_document_file",
      lambda path: b"%PDF-1.6\n%%EOF\n",
      raising=False,
    )

    docs = client.download_player_registration_documents(632478, 257901)

    assert [doc["filename"] for doc in docs] == [
      "257901_783358_1789078995.pdf",
      "257901_783358_1789078999.pdf",
    ]

  def test_skips_a_row_without_a_view_button(self, client, monkeypatch):
    body = (
      "<table><tbody>"
      "<tr><td><button onclick='checkDoc(1,780605,298352,630304)'></button></td></tr>"
      + _row(2278594, "Modelo 1 - Inscrição jogadores (Primeira ou Revalidação)", None)
      + "</tbody></table>" + SELECT
    )
    _, _, docs = _fetch(client, monkeypatch, body)
    assert docs == [{"doc_id": 2278594, "tipo_doc": 1, "file_path": None}]

    batch = type("BatchStub", (), {"id": 630304, "is_open": True})()
    monkeypatch.setattr(
      client, "list_player_registration_batches", lambda: [batch], raising=False,
    )
    monkeypatch.setattr(
      client, "_fetch_registration_documents",
      lambda b, lic: (3, 780605, docs),
      raising=False,
    )
    monkeypatch.setattr(
      client, "_download_registration_document_file",
      lambda path: pytest.fail("a row without a view path must not be downloaded"),
      raising=False,
    )
    assert client.download_player_registration_documents(630304, 298352) == []

  def test_download_helper_rejects_html_and_returns_pdf(self, client, monkeypatch):
    class Response:
      def __init__(self, content):
        self.content = content

      def raise_for_status(self):
        return None

    response = Response(b"<html><body>login</body></html>")
    client._http = type("Http", (), {"get": lambda self, *a, **k: response})()
    with pytest.raises(SavResponseError, match="neither a PDF"):
      client._download_registration_document_file("uploads/document.pdf")

    response.content = b"%PDF-1.6\n%%EOF\n"
    assert client._download_registration_document_file("uploads/document.pdf") == response.content
