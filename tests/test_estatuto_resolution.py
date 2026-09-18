"""How the step-3 commit settles `estatuto`, and why it never sends an empty one.

Three things went wrong together in production on 2026-09-12:

* `add_player_to_registration_batch(estatuto=...)` reached only the type-1 path.
  For a Revalidação the commit read op=31's prefill and ignored the parameter,
  so the documented way to supply a missing estatuto did nothing.
* An empty estatuto was forwarded to op=36 anyway, and SAV answered HTTP 200
  with a PHP fatal — the empty value leaves a hole in its INSERT
  (`... near '4704,23,0,1139  , '1 ', '1 ')'`).
* Nothing fell back the way SAV's own UI does. `guiasjog.js` POSTs op=151 for
  *every* batch type and selects `res['id']`, overriding it only when op=31's
  estatuto is non-empty (`if (res['estatuto'] != "")`).

The op=151 payloads below are the real shape, captured live on 2026-09-13 from
batch 632884 (type 2): a per-player `id` — 6 for two athletes, 10 for a third —
alongside the same four-option dropdown for everyone.
"""

import threading

import pytest

from sav_client.exceptions import SavConfigError
from sav_client.sav_client import SavClient

ESTATUTO_OPTIONS = (
  "<option value=0></option>"
  "<option value='6'>FBP</option>"
  "<option value='10'>Sem FBP Comunitário</option>"
  "<option value='11'>Sem FBP Não Comunitário</option>"
  "<option value='12'>Equiparado FBP</option>"
)
# op=151 as SAV actually answers it for a Revalidação item.
OP151_WITH_DEFAULT = {
  "estatutos": ESTATUTO_OPTIONS, "id": "6", "mini": 0,
  "nacional": 1, "val": 1, "valsegclube": 1,
}
# The same call when SAV commits to no default — the state that must be refused
# rather than committed blank.
OP151_NO_DEFAULT = {**OP151_WITH_DEFAULT, "id": "0"}


class _Batch:
  id = 42
  tier_id = 7
  type_id = 2  # Revalidação
  is_open = True
  state = "Em construção"
  tier = "Sub-14"
  gender = "M"
  number = 5
  type = "Revalidação"


@pytest.fixture
def client():
  c = SavClient.__new__(SavClient)
  c.session = {"user": "u"}
  c._timeout = 5
  c.base_url = "https://sav2.example/"
  c._batch_memo = {}
  c._batch_memo_lock = threading.Lock()

  class _Cache:
    def record_license_batch(self, license, batch_id):
      pass

  c._cache = _Cache()
  return c


@pytest.fixture
def commit(client, monkeypatch):
  """Drive _commit_registration_step3, capturing the op=36 body and the
  estatuto op=26 was asked for."""
  seen: dict = {"op151_calls": 0}

  monkeypatch.setattr(
    client, "_resolve_insurance_cascade",
    lambda internal_id, batch, escalao: (1, 99),
  )
  monkeypatch.setattr(client, "_registration_precommit", lambda guia, uid: None)

  def _taxa(batch, internal_id, estatuto):
    seen["taxa_estatuto"] = estatuto
    return 55

  monkeypatch.setattr(client, "_resolve_taxa_id", _taxa)

  def _commit_body(body):
    seen["body"] = body
    return {"val": 1, "resultfunction": "ok"}

  monkeypatch.setattr(client, "_registration_commit", _commit_body)

  def _run(prefill, *, op151=OP151_WITH_DEFAULT, **overrides):
    def _options(batch, userid, tipo):
      seen["op151_calls"] += 1
      seen["op151_tipo"] = tipo
      return op151

    monkeypatch.setattr(client, "_load_estatuto_options", _options)
    kwargs = {
      "exam_date": "2026-08-14", "taxa_id": None, "estatuto": None,
      "promote_to_tier_id": None, "inline_subida": None,
      "guardian_name": None, "guardian_relation": None,
      "guardian_phone": None, "guardian_email": None,
      "consent_data": None, "consent_communications": None,
      "consent_marketing": None,
    }
    kwargs.update(overrides)
    client._commit_registration_step3(_Batch(), 1234, 301772, prefill, **kwargs)
    return seen

  return _run


FULL_PREFILL = {"estatuto": "6", "escalao": 7, "menor_idade": 0, "taxa": "-1"}
EMPTY_PREFILL = {"estatuto": "", "escalao": 7, "menor_idade": 0, "taxa": "-1"}


class TestExplicitEstatutoWins:
  def test_it_overrides_a_stored_selection(self, commit):
    seen = commit(FULL_PREFILL, estatuto=12)

    assert seen["body"]["estatuto"] == "12", "the parameter must reach the commit"

  def test_it_also_drives_the_fee_lookup(self, commit):
    """The reported bug in full: the fee was priced off the prefill, so even a
    caller who passed an estatuto got the stored one's fee list."""
    seen = commit(FULL_PREFILL, estatuto=12)

    assert seen["taxa_estatuto"] == 12

  def test_without_one_the_stored_selection_is_used(self, commit):
    seen = commit(FULL_PREFILL)

    assert seen["body"]["estatuto"] == "6"
    assert seen["op151_calls"] == 0, "no need to ask SAV when op=31 answered"


class TestEmptyEstatutoIsNeverCommitted:
  def test_an_empty_prefill_falls_back_to_savs_own_default(self, commit):
    """What the browser does, and what this client did not."""
    seen = commit(EMPTY_PREFILL)

    assert seen["body"]["estatuto"] == "6"
    assert seen["op151_calls"] == 1
    assert seen["op151_tipo"] == 2, "op=151 is asked about *this* batch's type"

  def test_the_fallback_prices_the_fee_too(self, commit):
    seen = commit(EMPTY_PREFILL)

    assert seen["taxa_estatuto"] == 6

  def test_zero_is_savs_blank_row_not_a_choice(self, commit):
    """`<option value=0></option>` is the empty row; committing it is the bug."""
    seen = commit({**FULL_PREFILL, "estatuto": "0"})

    assert seen["body"]["estatuto"] == "6", "resolved, not sent as 0"

  def test_no_default_anywhere_refuses_before_the_commit(self, commit):
    with pytest.raises(SavConfigError, match="No estatuto for license=301772"):
      commit(EMPTY_PREFILL, op151=OP151_NO_DEFAULT)

  def test_the_refusal_names_the_options_and_never_commits(self, commit, client):
    with pytest.raises(SavConfigError) as excinfo:
      commit(EMPTY_PREFILL, op151=OP151_NO_DEFAULT)

    message = str(excinfo.value)
    assert "6='FBP'" in message, "tell the caller what they may pass"
    assert "estatuto=" in message

  def test_an_explicit_value_rescues_a_batch_sav_cannot_place(self, commit):
    """The recovery path: SAV offers no default, the caller supplies one."""
    seen = commit(EMPTY_PREFILL, op151=OP151_NO_DEFAULT, estatuto=10)

    assert seen["body"]["estatuto"] == "10"


class TestOp151Parsing:
  def test_the_blank_option_is_not_a_choice(self, client):
    options = client._parse_estatuto_options(OP151_WITH_DEFAULT)

    assert options == {
      6: "FBP", 10: "Sem FBP Comunitário",
      11: "Sem FBP Não Comunitário", 12: "Equiparado FBP",
    }

  def test_a_lone_real_option_is_the_default_when_sav_names_none(
    self, client, monkeypatch,
  ):
    """Type-1's rule, reused only as a last resort for other types."""
    monkeypatch.setattr(
      client, "_load_estatuto_options",
      lambda batch, userid, tipo: {
        "estatutos": "<option value=0></option><option value='6'>FBP</option>",
        "id": "0",
      },
    )

    data = client._load_estatuto_options(_Batch(), 1234, _Batch.type_id)
    options = client._parse_estatuto_options(data)

    assert client._estatuto_default_from(data, options) == 6

  def test_four_options_and_no_id_means_no_default(self, client, monkeypatch):
    """Why _load_primeira_estatuto cannot simply be reused for Revalidação:
    op=151 returns all four options for a type-2 too, so the sole-option rule
    never fires and `id` is the only real answer."""
    monkeypatch.setattr(
      client, "_load_estatuto_options",
      lambda batch, userid, tipo: OP151_NO_DEFAULT,
    )

    data = client._load_estatuto_options(_Batch(), 1234, _Batch.type_id)
    options = client._parse_estatuto_options(data)

    assert client._estatuto_default_from(data, options) is None
