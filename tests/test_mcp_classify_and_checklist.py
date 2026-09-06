"""MCP tests for classify_documents and document_requirements.

Both are read-only: they reach neither SAV nor, in these tests, Document AI —
`sav_parsers.classify` is stubbed. The point of `classify_documents` is that it
names *every* type SAV files, including the supplementary ones
`parse_enrollment_forms` rejects, without the upload that was previously the
only way to learn a document's type.
"""
import base64

import pytest

from sav_parsers.types import DocType

from sav_mcp import server as server_module


def _pdf(marker: bytes = b"x") -> str:
  """A base64 payload with the %PDF- magic ensure_pdf dispatches on."""
  return base64.b64encode(b"%PDF-1.4\n" + marker).decode("ascii")


@pytest.fixture(autouse=True)
def never_trains(monkeypatch):
  """classify_documents has no caller-supplied label, so it must never train."""
  monkeypatch.setattr(
    "sav_parsers.train_classifier",
    lambda *a, **kw: pytest.fail("classify_documents must not train the classifier"),
  )


def _stub_classify(monkeypatch, fn):
  monkeypatch.setattr("sav_parsers.classify", fn)


def test_supplementary_type_classifies(monkeypatch):
  """atestado_residencia is one of the types parse_enrollment_forms rejects."""
  _stub_classify(monkeypatch, lambda path: DocType.ATESTADO_RESIDENCIA)

  result = server_module.classify_documents([{"pdf": _pdf()}])

  assert result == [
    {"index": 0, "doc_type": "atestado_residencia", "uploadable": True},
  ]


def test_outros_is_a_normal_result_not_an_error(monkeypatch):
  """"Not an enrollment document" is a real answer for a folder of mixed files."""
  _stub_classify(monkeypatch, lambda path: DocType.OUTROS)

  (row,) = server_module.classify_documents([{"pdf": _pdf()}])

  assert row["doc_type"] == "outros"
  assert "error" not in row


def test_one_bad_document_does_not_lose_the_others(monkeypatch):
  """A batch is only useful if a single unreadable file cannot sink it."""
  def classify(path):
    with open(path, "rb") as f:
      body = f.read()
    if b"boom" in body:
      raise ValueError("unreadable scan")
    return DocType.FPB_MODELO_1

  _stub_classify(monkeypatch, classify)

  result = server_module.classify_documents(
    [{"pdf": _pdf()}, {"pdf": _pdf(b"boom")}, {"pdf": _pdf()}],
  )

  assert [row["index"] for row in result] == [0, 1, 2]
  assert result[0]["doc_type"] == "fpb_modelo_1"
  assert result[2]["doc_type"] == "fpb_modelo_1"
  assert "unreadable scan" in result[1]["error"]
  assert "doc_type" not in result[1]


def test_unknown_keys_are_rejected(monkeypatch):
  """A misspelled key must never silently classify something the caller hinted."""
  _stub_classify(monkeypatch, lambda path: DocType.FPB_MODELO_1)

  (row,) = server_module.classify_documents([{"pdf": _pdf(), "doc_type": "exame_medico"}])

  assert row == {"index": 0, "error": "Unknown document keys: doc_type"}


def test_missing_pdf_key_is_an_error(monkeypatch):
  _stub_classify(monkeypatch, lambda path: DocType.FPB_MODELO_1)

  (row,) = server_module.classify_documents([{}])

  assert "Missing or invalid required key: pdf" in row["error"]


def test_input_order_is_preserved_across_the_concurrent_path(monkeypatch):
  """More than one entry fans out over a thread pool; rows must still line up."""
  import time

  order = [
    DocType.FPB_MODELO_1, DocType.EXAME_MEDICO, DocType.FPB_MODELO_4,
    DocType.ATESTADO_RESIDENCIA, DocType.CERTIDAO_MATRICULA,
    DocType.DOCUMENTO_IDENTIFICACAO, DocType.OUTROS,
  ]

  def classify(path):
    with open(path, "rb") as f:
      index = int(f.read().split(b"\n")[1])
    # Invert the durations so completion order cannot match input order.
    time.sleep((len(order) - index) * 0.01)
    return order[index]

  _stub_classify(monkeypatch, classify)

  documents = [
    {"pdf": base64.b64encode(b"%PDF-1.4\n" + str(i).encode()).decode("ascii")}
    for i in range(len(order))
  ]
  result = server_module.classify_documents(documents)

  assert [row["index"] for row in result] == list(range(len(order)))
  assert [row["doc_type"] for row in result] == [d.value for d in order]


def test_single_document_skips_the_thread_pool(monkeypatch):
  """One entry runs inline — no pool, same shape."""
  from concurrent.futures import ThreadPoolExecutor

  _stub_classify(monkeypatch, lambda path: DocType.EXAME_MEDICO)
  monkeypatch.setattr(
    ThreadPoolExecutor, "__init__",
    lambda *a, **kw: pytest.fail("a single document must not spin up a pool"),
  )

  assert server_module.classify_documents([{"pdf": _pdf()}]) == [
    {"index": 0, "doc_type": "exame_medico", "uploadable": True},
  ]


def test_classify_documents_never_reaches_sav(monkeypatch):
  """It is a read-only tool in the strongest sense: no session at all."""
  _stub_classify(monkeypatch, lambda path: DocType.FPB_MODELO_1)
  monkeypatch.setattr(
    server_module, "_get_client",
    lambda: pytest.fail("classify_documents must not talk to SAV"),
  )

  assert server_module.classify_documents([{"pdf": _pdf()}])[0]["doc_type"] == "fpb_modelo_1"


# ── document_requirements ─────────────────────────────────────────────────────


def test_checklist_counts_documents_the_caller_already_holds():
  """The motivating case: reconcile a pile of files against the FPB rule."""
  without = server_module.document_requirements(reg_type=1, nationality_id=155)
  with_docs = server_module.document_requirements(
    reg_type=1, nationality_id=155,
    available_doc_types=["fpb_modelo_1", "exame_medico"],
  )

  assert with_docs["scenario"] == without["scenario"]
  assert len(with_docs["missing"]) < len(without["missing"])


def test_checklist_is_null_for_transferencia():
  assert server_module.document_requirements(reg_type=3, nationality_id=155) is None


def test_checklist_rejects_an_unknown_doc_type():
  """A silently dropped typo would report a document the caller supplied as missing."""
  with pytest.raises(ValueError):
    server_module.document_requirements(
      reg_type=1, nationality_id=155, available_doc_types=["exame_medic"],
    )


def test_checklist_defaults_to_foreign_born_without_a_nationality():
  """Asking for too many documents is recoverable; declaring someone ready is not."""
  unknown = server_module.document_requirements(reg_type=1)
  portuguese = server_module.document_requirements(reg_type=1, nationality_id=155)

  assert unknown["scenario"] != portuguese["scenario"]
  assert len(unknown["required"]) >= len(portuguese["required"])


def test_portuguese_with_both_documents_is_complete():
  """The portuguese set is just the form and the medical exam."""
  result = server_module.document_requirements(
    reg_type=1, nationality_id=155,
    available_doc_types=["fpb_modelo_1", "exame_medico"],
  )

  assert result["scenario"] == "portuguese"
  assert result["missing"] == []
  assert all(row["satisfied"] for row in result["required"])


def test_foreign_born_needs_two_identity_documents():
  """SAV files both under the same tipo_doc, so the rule counts rather than names."""
  base = ["fpb_modelo_1", "exame_medico", "atestado_residencia", "certidao_matricula"]

  one = server_module.document_requirements(
    reg_type=1, available_doc_types=[*base, "documento_identificacao"],
  )
  two = server_module.document_requirements(
    reg_type=1, available_doc_types=[*base, "documento_identificacao", "documento_identificacao"],
  )

  assert one["missing"] == ["documento_identificacao (need 2, found 1)"]
  assert two["missing"] == []
  row = {r["doc_type"]: r for r in two["required"]}["documento_identificacao"]
  assert (row["min_count"], row["found_count"], row["satisfied"]) == (2, 2, True)


def test_standalone_subida_needs_only_the_mod4():
  result = server_module.document_requirements(reg_type=4)

  assert [row["doc_type"] for row in result["required"]] == ["fpb_modelo_4"]
  assert server_module.document_requirements(
    reg_type=4, available_doc_types=["fpb_modelo_4"],
  )["missing"] == []


def test_a_bare_string_is_rejected_not_iterated():
  """"exame_medico" is 12 characters, not 12 documents."""
  with pytest.raises(ValueError):
    server_module.document_requirements(reg_type=1, available_doc_types="exame_medico")


def test_passing_a_license_is_refused_and_points_at_the_grounded_tool():
  """A licence means SAV can ground the answer; this tool cannot, so it refuses.

  Without this the wrong-tool case answers *plausibly* from a nationality the
  caller guessed — the worst failure mode, because nothing looks broken.
  """
  with pytest.raises(ValueError, match="get_enrollment_status"):
    server_module.document_requirements(reg_type=2, nationality_id=155, license=301772)


def test_checklist_declares_its_nationality_is_caller_supplied():
  """The grounded and ungrounded checklists are otherwise shape-identical."""
  result = server_module.document_requirements(reg_type=1, nationality_id=155)

  assert result["nationality_source"] == "caller"
