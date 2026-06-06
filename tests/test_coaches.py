import json

import pytest

from sav_client import SavClient, Coach
from sav_client.exceptions import SavResponseError


# Minimal HTML mirroring the live SAV2 coach-detail form: mixed single- and
# double-quoted attributes, both styles must be parsed.
_SAMPLE_MSG = (
  "<div class='overflow-x-auto'>"
  "<input class='form-control' name='edit' id='nif' value='223688177' "
  "data-parsley-nif='' disabled>"
  '<input type="text" class="form-control" id="nrtptd" value="166614" disabled>'
  '<input type="text" class="form-control" id="validadetptd" value="15-09-2028" disabled>'
  "</div>"
)


def _new_authed_client() -> SavClient:
  c = SavClient("https://sav2.fpb.pt", "u", "p")
  c.session = {"user": "2430", "organizacao": 2430}
  return c


class TestGetCoachDetail:
  def test_requires_login(self):
    c = SavClient("https://sav2.fpb.pt", "u", "p")
    with pytest.raises(SavResponseError, match="Must call login"):
      c.get_coach_detail(24868)

  def test_parses_nif_tptd_and_expiry_from_msg(self, monkeypatch):
    client = _new_authed_client()
    payload = json.dumps({"nome": "Test Coach", "roles": 2, "msg": _SAMPLE_MSG})
    monkeypatch.setattr(client, "_post_form", lambda *a, **k: payload)

    result = client.get_coach_detail(24868)

    assert result.id == 24868
    assert result.name == "Test Coach"
    assert result.nif == "223688177"
    assert result.tptd == "166614"
    assert result.tptd_expiry == "15-09-2028"

  def test_tolerates_raw_newlines_in_msg(self, monkeypatch):
    """SAV2 sometimes emits raw CR/LF inside string values; strict=False handles it."""
    client = _new_authed_client()
    # Start from a valid JSON encoding, then inject a raw CR+LF *inside* the
    # msg value to mimic the malformed bytes SAV2 actually sends.
    valid = json.dumps({"nome": "N", "roles": 2, "msg": "\\r\\n" + _SAMPLE_MSG})
    broken = valid.replace('"msg":"', '"msg":"\r\n', 1)
    monkeypatch.setattr(client, "_post_form", lambda *a, **k: broken)

    result = client.get_coach_detail(24868)

    assert result.nif == "223688177"
    assert result.tptd == "166614"

  def test_missing_fields_become_empty_strings(self, monkeypatch):
    client = _new_authed_client()
    payload = json.dumps({"nome": "X", "roles": 2, "msg": "<div>no inputs here</div>"})
    monkeypatch.setattr(client, "_post_form", lambda *a, **k: payload)

    result = client.get_coach_detail(24868)

    assert result.nif == ""
    assert result.tptd == ""
    assert result.tptd_expiry == ""

  def test_raises_when_msg_missing(self, monkeypatch):
    client = _new_authed_client()
    monkeypatch.setattr(client, "_post_form", lambda *a, **k: json.dumps({"nome": "X"}))

    with pytest.raises(SavResponseError, match="missing 'msg'"):
      client.get_coach_detail(24868)


class TestListCoachesWithDetails:
  def _listing_coach(self) -> Coach:
    return Coach(
      id=42, carreira_id=99, wallet="22174", name="Listed",
      association="A", club="C", gender="M", season="2025/2026",
      grade="Grau 3", birth_date="1980-01-01", active=True,
    )

  def test_with_details_false_skips_detail_calls(self, monkeypatch):
    client = _new_authed_client()
    listed = self._listing_coach()
    calls = {"detail": 0}

    monkeypatch.setattr(client, "_post_form", lambda *a, **k: "<html/>")
    monkeypatch.setattr(client, "_parse_coaches_response", lambda html: [listed])

    def _detail(_id):
      calls["detail"] += 1
      raise AssertionError("get_coach_detail should not be called when with_details=False")

    monkeypatch.setattr(client, "get_coach_detail", _detail)

    out = client.list_coaches(club=1, status="all")

    assert calls["detail"] == 0
    assert out == [listed]
    assert out[0].nif == ""
    assert out[0].tptd == ""

  def test_with_details_true_merges_nif_and_tptd(self, monkeypatch):
    client = _new_authed_client()
    listed = self._listing_coach()
    detail = Coach(
      id=42, carreira_id=0, wallet="", name="Listed",
      association="", club="", gender="", season="", grade="", birth_date="",
      active=False, nif="500123456", tptd="166614", tptd_expiry="15-09-2028",
    )

    monkeypatch.setattr(client, "_post_form", lambda *a, **k: "<html/>")
    monkeypatch.setattr(client, "_parse_coaches_response", lambda html: [listed])
    monkeypatch.setattr(client, "get_coach_detail", lambda cid: detail)

    out = client.list_coaches(club=1, status="all", with_details=True)

    assert len(out) == 1
    merged = out[0]
    # Listing fields preserved
    assert merged.wallet == "22174"
    assert merged.club == "C"
    assert merged.active is True
    # Detail fields merged in
    assert merged.nif == "500123456"
    assert merged.tptd == "166614"
    assert merged.tptd_expiry == "15-09-2028"
