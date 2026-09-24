"""Subida de escalão status parsed from the player page's "Inscrições" tab.

The HTML mirrors the two layouts SAV renders for ``jogadoresdb.php?op=2``,
captured live on 2026-09-24: the full one (a leading icon column, the lote's
doc buttons and "Data Aprovação") and the reduced one SAV shows when another
club holds the player's last approved registration.
"""

import json

import pytest
from bs4 import BeautifulSoup

from sav_client import SavClient, SubidaStatus
from sav_client.exceptions import SavConnectionError
from sav_client.models import Player, Season
from sav_client.sav_client import _parse_subida_status
from sav_shared.serializers import player_to_dict

SEASON = "2026/2027"


def _full(*rows):
  """Full layout: rows are (lote, época, escalão, tipo, data aprovação)."""
  body = "".join(
    f"<tr><td><button onclick='seeDocsGuia(1,2)'></button></td>"
    f"<td>{lote}</td><td>{epoca}</td><td>AB Santarém</td><td>Rio Maior Basket</td>"
    f"<td>{escalao}</td><td>{tipo}</td><td>FBP</td><td>{data}</td></tr>"
    for lote, epoca, escalao, tipo, data in rows
  )
  return (
    "<div id='inscricao'><table><thead><tr><th></th><th>Lote</th><th>Época</th>"
    "<th>Associação</th><th>Clube</th><th>Escalão</th><th>Tipo</th>"
    f"<th>Estatuto</th><th>Data Aprovação</th></tr></thead><tbody>{body}</tbody>"
    "</table></div>"
  )


def _reduced(*rows):
  """Reduced layout: rows are (lote, época, escalão, tipo); no date column."""
  body = "".join(
    f"<tr><td>{lote}</td><td>{epoca}</td><td>AB Santarém</td>"
    f"<td>Rio Maior Basket</td><td>{escalao}</td><td>{tipo}</td><td>FBP</td></tr>"
    for lote, epoca, escalao, tipo in rows
  )
  return (
    "<div id='inscricao'><table><thead><tr><th>Lote</th><th>Época</th>"
    "<th>Associação</th><th>Clube</th><th>Escalão</th><th>Tipo</th>"
    f"<th>Estatuto</th></tr></thead><tbody>{body}</tbody></table></div>"
  )


def _parse(html, season=SEASON):
  return _parse_subida_status(BeautifulSoup(html, "html.parser"), season)


class TestParseSubidaStatus:
  def test_inline_subida_approved(self):
    html = _full(
      ("142", SEASON, "Sub 14 >> Sub 16", "Revalidação", "16-09-2026"),
      ("117", "2025/2026", "Sub 14", "Revalidação", "23-09-2025"),
    )
    assert _parse(html) == SubidaStatus(
      status="approved", tier_from="Sub 14", tier_to="Sub 16",
      approved_on="2026-09-16",
    )

  def test_blank_approval_date_is_pending(self):
    html = _full(("226", SEASON, "Sub 16 >> Sub 18", "Revalidação", ""))
    assert _parse(html) == SubidaStatus(
      status="pending", tier_from="Sub 16", tier_to="Sub 18", approved_on=None,
    )

  def test_standalone_lote_matches_on_sav_spelling(self):
    # SAV spells the type "Súbida"; the match must survive the accent.
    html = _full(
      ("357", SEASON, "Sub 14 >> Sub 16", "Súbida de Escalão", "15-10-2026"),
    )
    assert _parse(html).status == "approved"

  def test_standalone_row_without_separator_keeps_base_tier(self):
    html = _full(("357", SEASON, "Sub 14", "Súbida de Escalão", ""))
    assert _parse(html) == SubidaStatus(
      status="pending", tier_from="Sub 14", tier_to=None,
    )

  def test_current_season_without_subida_is_none(self):
    html = _full(("216", SEASON, "Sub 18", "Revalidação", ""))
    assert _parse(html) == SubidaStatus(status="none")

  def test_previous_season_subida_does_not_count(self):
    html = _full(
      ("357", "2025/2026", "Sub 14 >> Sub 16", "Súbida de Escalão", "15-10-2025"),
    )
    assert _parse(html) == SubidaStatus(status="none")

  def test_approved_outranks_pending(self):
    html = _full(
      ("300", SEASON, "Sub 14 >> Sub 16", "Súbida de Escalão", ""),
      ("142", SEASON, "Sub 14 >> Sub 16", "Revalidação", "16-09-2026"),
    )
    assert _parse(html).status == "approved"

  def test_reduced_layout_current_season_is_unknown(self):
    # No date column, and whether SAV marks an inline subida here is
    # unverified: the row cannot be read as "none".
    html = _reduced(("284", SEASON, "Mini 10", "Revalidação"))
    assert _parse(html) == SubidaStatus(status="unknown")

  def test_reduced_layout_without_current_season_is_none(self):
    html = _reduced(("11", "2024/2025", "Mini 8", "1º Inscrição"))
    assert _parse(html) == SubidaStatus(status="none")

  @pytest.mark.parametrize("html", [
    "<div id='dados_base'></div>",
    "<div id='inscricao'>Sem inscrições</div>",
    "<div id='inscricao'><table><tr><th>Lote</th><th>Clube</th></tr></table></div>",
  ])
  def test_unreadable_tab_is_unknown(self, html):
    assert _parse(html) == SubidaStatus(status="unknown")

  def test_misaligned_row_is_unknown(self):
    html = _full(("142", SEASON, "Sub 14", "Revalidação", "")).replace(
      "<td>FBP</td>", "", 1,
    )
    assert _parse(html) == SubidaStatus(status="unknown")

  def test_unresolved_season_is_unknown(self):
    html = _full(("142", SEASON, "Sub 14 >> Sub 16", "Revalidação", "16-09-2026"))
    assert _parse(html, season=None) == SubidaStatus(status="unknown")


class TestGetPlayerDetailSubida:
  def _client(self, monkeypatch, html):
    client = SavClient("https://example.invalid", "user", "pass")
    client.session = {"user": "u", "perfil": 1, "organizacao": 2}
    monkeypatch.setattr(
      client, "_post_form", lambda *args, **kwargs: json.dumps({"msg": html}),
    )
    return client

  def test_uses_current_season(self, monkeypatch):
    html = _full(("142", SEASON, "Sub 14 >> Sub 16", "Revalidação", "16-09-2026"))
    client = self._client(monkeypatch, html)
    monkeypatch.setattr(
      client, "get_current_season",
      lambda: Season(id=65, label=SEASON, start_year=2026, is_active=True),
    )
    detail = client.get_player_detail(9, with_details=True)
    assert detail.subida.status == "approved"
    assert detail.subida.tier_to == "Sub 16"

  def test_season_lookup_failure_is_unknown(self, monkeypatch):
    html = _full(("142", SEASON, "Sub 14 >> Sub 16", "Revalidação", "16-09-2026"))
    client = self._client(monkeypatch, html)

    def _fail():
      raise SavConnectionError("down")

    monkeypatch.setattr(client, "get_current_season", _fail)
    assert client.get_player_detail(9, with_details=True).subida == SubidaStatus(
      status="unknown",
    )


class TestPlayerToDictSubida:
  def _player(self, subida):
    return Player(
      id=1, license=270157, name="X", association="", club="", tier="Sub 14",
      gender="", birth_date="", nationality="", status="", subida=subida,
    )

  def test_emitted_with_details(self):
    out = player_to_dict(
      self._player(SubidaStatus("pending", "Sub 16", "Sub 18")), with_details=True,
    )
    assert out["subida"] == {
      "status": "pending", "tier_from": "Sub 16", "tier_to": "Sub 18",
      "approved_on": None,
    }

  def test_absent_without_details(self):
    out = player_to_dict(self._player(SubidaStatus("none")), with_details=False)
    assert "subida" not in out

  def test_null_when_detail_not_parsed(self):
    assert player_to_dict(self._player(None), with_details=True)["subida"] is None
