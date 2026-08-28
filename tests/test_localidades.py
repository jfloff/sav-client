"""The localidade id must be settable, not just carried forward.

`_build_step2_send` used to take `localidade` from the prefill with no override,
so a Revalidação could only ever keep whatever SAV already stored. When that
stored value was lost there was no route to put one back — the 1ª Inscrição path
has accepted a `localidade_id` all along, the update path did not.

Ground truth from production 2026-08-27: op=19 with `conc=210` (Rio Maior)
returns 10 localidades, among them Asseiceira = 1455.
"""

import pytest

from sav_client.exceptions import SavConnectionError
from sav_client.sav_client import SavClient


OPTIONS_HTML = (
  "<option value='0'></option>"
  "<option value='1451'>Alcobertas </option>"
  "<option value='1452'>Arrouquelas </option>"
  "<option value='1455'>Asseiceira</option>"
)


@pytest.fixture
def client():
  c = SavClient.__new__(SavClient)
  c.base_url = "https://sav2.example/"
  c.session = {"perfil": 1, "user": "u", "organizacao": 1}
  c._timeout = 10
  return c


class TestListLocalidades:
  def test_parses_the_cascade_fragment(self, client, monkeypatch):
    captured = {}

    class _Http:
      def get(self, url, params=None, timeout=None):
        captured["params"] = params
        return type("R", (), {"text": OPTIONS_HTML, "raise_for_status": lambda self: None})()

    client._http = _Http()
    out = client.list_localidades(210)

    assert out == {1451: "Alcobertas", 1452: "Arrouquelas", 1455: "Asseiceira"}
    assert captured["params"] == {"op": "19", "conc": 210}

  def test_placeholder_option_is_dropped(self, client, monkeypatch):
    class _Http:
      def get(self, *a, **k):
        return type("R", (), {"text": OPTIONS_HTML, "raise_for_status": lambda self: None})()

    client._http = _Http()
    assert 0 not in client.list_localidades(210)

  def test_no_concelho_means_no_request(self, client):
    class _Http:
      def get(self, *a, **k):
        raise AssertionError("must not call SAV for concelho_id=0")

    client._http = _Http()
    assert client.list_localidades(0) == {}

  def test_transport_failure_surfaces(self, client):
    import requests

    class _Http:
      def get(self, *a, **k):
        raise requests.exceptions.ConnectionError("boom")

    client._http = _Http()
    with pytest.raises(SavConnectionError, match="Could not load localidades"):
      client.list_localidades(210)


class TestStep2SendCarriesLocalidade:
  PREFILL = {
    "distrito": "14", "concelho": "210", "localidade": "1455",
    "morada": "Rua X", "codpostal": "2040-483", "localidade_txt": "Asseiceira",
  }

  def _send(self, **overrides):
    kwargs = dict(
      morada=None, cod_postal=None, localidade_txt=None,
      distrito_id=None, concelho_id=None,
    )
    kwargs.update(overrides)
    return SavClient._build_step2_send(self.PREFILL, **kwargs)

  def test_stored_localidade_is_kept_when_not_overridden(self):
    assert "localidade=1455," in self._send()

  def test_override_replaces_the_stored_id(self):
    assert "localidade=1452," in self._send(localidade_id=1452)

  def test_missing_stored_value_serialises_as_null(self):
    # The shape that blanked a live record: no stored id and no override.
    send = SavClient._build_step2_send(
      {}, morada=None, cod_postal=None, localidade_txt=None,
      distrito_id=None, concelho_id=None,
    )
    assert "localidade=NULL," in send

  def test_override_recovers_from_a_missing_stored_value(self):
    send = SavClient._build_step2_send(
      {}, morada=None, cod_postal=None, localidade_txt=None,
      distrito_id=None, concelho_id=None, localidade_id=1455,
    )
    assert "localidade=1455," in send
