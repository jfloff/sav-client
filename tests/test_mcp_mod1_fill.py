"""MCP test for fill_mod1 — returns a base64-encoded, field-filled Modelo 1 PDF."""
import base64
import io

import pikepdf

from sav_mcp import server as server_module


def test_fill_mod1_returns_filled_pdf():
  result = server_module.fill_mod1(values={
    "morada": "Rua X, 1",
    "tipo": 2,               # Passaporte
    "consent_data": True,
  })

  assert result["filename"] == "modelo1.pdf"
  pdf_bytes = base64.b64decode(result["pdf_b64"])
  assert pdf_bytes[:5] == b"%PDF-"
  assert result["size_bytes"] == len(pdf_bytes)

  pdf = pikepdf.open(io.BytesIO(pdf_bytes))
  fields = {str(f.T): f for f in pdf.Root.AcroForm.Fields if "/T" in f}
  assert str(fields["Morada"].get("/V")) == "Rua X, 1"
  assert str(fields["Passaporte"].get("/V")) == "/On"
  assert str(fields["SIM"].get("/V")) == "/On"
  pdf.close()
