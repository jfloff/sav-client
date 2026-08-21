import json

import pytest

from sav_client import SavClient
from sav_client.exceptions import SavResponseError


def _load_profile(monkeypatch, html):
  client = SavClient("https://example.invalid", "user", "pass")
  client.session = {"user": "u", "perfil": 1, "organizacao": 2}
  monkeypatch.setattr(client._cache, "get_player_id", lambda license: 99)
  monkeypatch.setattr(
    client, "_post_form", lambda *args, **kwargs: json.dumps({"msg": html})
  )
  return client.load_player_profile(123456)


class TestGetPlayerDetail:
  def test_requires_login(self):
    c = SavClient("https://sav2.fpb.pt", "user", "pass")
    with pytest.raises(SavResponseError, match="Must call login"):
      c.get_player_detail(9, with_details=True)

  def test_with_details_false_returns_minimal_player(self, client, sample_player):
    result = client.get_player_detail(sample_player.id, with_details=False)

    assert result.id == sample_player.id
    assert result.photo_url == ""
    assert result.mobile_phone == ""
    assert result.name == ""

  def test_with_details_true_fetches_live_detail(self, client, sample_player):
    result = client.get_player_detail(sample_player.id, with_details=True)

    assert result.id == sample_player.id
    assert isinstance(result.photo_url, str)
    assert isinstance(result.mobile_phone, str)


class TestLoadPlayerProfile:
  def test_selected_options_include_values_and_labels(self, monkeypatch):
    html = """
      <select id="tipoi"><option value="2" selected> Passaporte </option></select>
      <select id="nacionalidade"><option value="155" selected> Portugal </option></select>
      <select id="paisNascimento"><option value="24" selected> Brasil </option></select>
      <select id="distrito"><option value="14" selected> Santarém </option></select>
      <select id="concelho"><option value="7" selected> Rio Maior </option></select>
    """

    profile = _load_profile(monkeypatch, html)

    assert profile == {
      "tipo": "2",
      "tipo_label": "Passaporte",
      "nacional": "155",
      "nacional_label": "Portugal",
      "naturalidade": "24",
      "naturalidade_label": "Brasil",
      "distrito": "14",
      "distrito_label": "Santarém",
      "concelho": "7",
      "concelho_label": "Rio Maior",
    }

  def test_select_without_selected_option_emits_neither_key(self, monkeypatch):
    html = '<select id="tipoi"><option value="2">Passaporte</option></select>'

    profile = _load_profile(monkeypatch, html)

    assert "tipo" not in profile
    assert "tipo_label" not in profile

  def test_absent_select_emits_neither_key(self, monkeypatch):
    profile = _load_profile(monkeypatch, "<input id=\"nome\" value=\"Player\">")

    assert "tipo" not in profile
    assert "tipo_label" not in profile
