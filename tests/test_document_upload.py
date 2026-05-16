"""Regression tests for the op=92 upload contract.

Ground truth comes from a live SAV2 web session — the browser hardcodes
`n=1` in the URL and the multipart field name `file0` for every upload,
regardless of how many documents the player already has (op=92 takes exactly
one file per request). The PHP handler reads `$_FILES["file" . ($n - 1)]`,
so any other combination yields `Undefined array key "fileX"` errors.

These tests pin both invariants so we don't drift back into using the
modal's `num` field (which grows after each successful upload and breaks
the second-and-later uploads to the same player).
"""

from __future__ import annotations

from typing import Any

import pytest

from sav_client.sav_client import SavClient


class _FakeResponse:
  def __init__(self, text: str) -> None:
    self.text = text

  def raise_for_status(self) -> None:
    pass


class _FakeHttp:
  def __init__(self) -> None:
    self.posts: list[dict[str, Any]] = []

  def post(self, url, *, files, timeout, headers):
    captured = {key: name for key, (name, _, _) in files.items()}
    self.posts.append({"url": url, "files": captured})
    return _FakeResponse('{"val": 1}')


def _build_client_for_upload(monkeypatch, tmp_path, *, modal_num: int):
  """Build a SavClient whose op=91 reports `modal_num` as the next-slot.

  Whatever value the modal reports, our SDK must IGNORE it and always send
  `n=1` + `file0` on op=92.
  """
  client = SavClient.__new__(SavClient)
  client.base_url = "https://sav2.example/"
  client.session = {"perfil": 1, "user": "u", "organizacao": 1, "epoca_id": 2026}
  client._timeout = 10
  client._http = _FakeHttp()

  batch = type(
    "BatchStub", (),
    {"id": 12, "number": "2025/12", "type_id": 2, "club_id": 99},
  )()
  monkeypatch.setattr(
    client, "list_player_registration_batches",
    lambda: [batch], raising=False,
  )
  monkeypatch.setattr(
    client, "_fetch_registration_documents",
    lambda batch, license: (modal_num, 778834, []), raising=False,
  )

  pdf = tmp_path / "doc.pdf"
  pdf.write_bytes(b"%PDF-1.4\n")
  return client, pdf


@pytest.mark.parametrize("modal_num", [1, 2, 3, 7])
def test_upload_always_uses_n1_and_file0(monkeypatch, tmp_path, modal_num):
  """Regardless of op=91's reported slot, op=92 sends n=1 + file0."""
  client, pdf = _build_client_for_upload(monkeypatch, tmp_path, modal_num=modal_num)

  client.upload_player_registration_document(12, 301772, pdf, tipo_doc=2)

  assert client._http.posts, "expected one POST to the upload endpoint"
  post = client._http.posts[0]
  assert list(post["files"].keys()) == ["file0"], (
    f"multipart key must be 'file0' (matches browser); got {list(post['files'].keys())!r}"
  )
  assert "&n=1&" in post["url"], (
    f"URL must hardcode n=1 (matches browser); got {post['url']!r}"
  )
  # Sanity: inscricao still comes from op=91.
  assert "inscricao=778834" in post["url"]
