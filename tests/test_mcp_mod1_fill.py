"""MCP test for fill_mod1 — returns a base64-encoded, field-filled Modelo 1 PDF."""
import base64
import inspect
import io

import pikepdf
import pytest

from sav_client.models import Season
from sav_mcp import server as server_module

# A complete, valid adult enrollment (guardian block omitted, born 1990). The
# tool validates by default, so partial dicts would be rejected.
VALUES = {
  "tipo_inscricao": 1, "clube": "Clube X", "associacao": "AB Lisboa",
  "genero": "Masculino", "escalao": "Sénior", "nome": "João Silva",
  "nacionalidade": "Portuguesa", "pais_nascimento": "Portugal",
  "nif": "123456789", "nasc": "1990-01-01", "tipo": 1, "numi": "12345678",
  "dataval": "2030-12-31", "email": "joao@example.pt", "tele": "912345678",
  "morada": "Rua X, 1", "localidade_txt": "Lisboa",
  "codpostal": "1500-123", "distrito": "Lisboa", "concelho": "Lisboa",
  "consent_data": True, "consent_communications": False, "consent_marketing": True,
  "data_assinatura": "2026-07-08",
}


class _StubClient:
  def get_current_season(self):
    return Season(id=65, label="2026/2027", start_year=2026, is_active=True)


@pytest.fixture(autouse=True)
def current_sav_season(monkeypatch):
  monkeypatch.setattr(server_module, "_get_client", lambda: _StubClient())


def _xobject_count(pdf_bytes):
  with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
    return len(pdf.pages[0].get("/Resources", {}).get("/XObject", {}))


def _png_b64():
  from PIL import Image
  buf = io.BytesIO()
  Image.new("RGBA", (60, 24), (0, 0, 180, 255)).save(buf, "PNG")
  return base64.b64encode(buf.getvalue()).decode("ascii")


def test_fill_mod1_returns_filled_pdf():
  result = server_module.fill_mod1(values=VALUES)

  assert result["filename"] == "modelo1.pdf"
  pdf_bytes = base64.b64decode(result["pdf_b64"])
  assert pdf_bytes[:5] == b"%PDF-"
  assert result["size_bytes"] == len(pdf_bytes)

  pdf = pikepdf.open(io.BytesIO(pdf_bytes))
  fields = {str(f.T): f for f in pdf.Root.AcroForm.Fields if "/T" in f}
  assert str(fields["Morada"].get("/V")) == "Rua X, 1"
  assert str(fields["Cartão Cidadão"].get("/V")) == "/On"
  assert str(fields["SIM"].get("/V")) == "/On"
  assert str(fields["epoca2"].get("/V")) == "2026"
  assert str(fields["epoca1"].get("/V")) == "2027"
  pdf.close()


def test_fill_mod1_has_no_season_parameter():
  assert "season" not in inspect.signature(server_module.fill_mod1).parameters


def test_fill_mod1_overlays_base64_signatures():
  base = base64.b64decode(server_module.fill_mod1(values=VALUES)["pdf_b64"])
  sig = _png_b64()
  # An adult has no guardian signature line to fill, so overlay the two that apply.
  signed = base64.b64decode(server_module.fill_mod1(
    values=VALUES,
    player_signature_b64=sig,
    club_stamp_b64=sig,
  )["pdf_b64"])
  assert _xobject_count(signed) == _xobject_count(base) + 2


def test_fill_mod1_rejects_invalid_values():
  with pytest.raises(ValueError, match="required"):
    server_module.fill_mod1(values={"morada": "Rua X, 1"})


def test_fill_mod1_rejects_season_in_values():
  with pytest.raises(ValueError, match="supplied separately"):
    server_module.fill_mod1(values={**VALUES, "season": "2025/2026"})
