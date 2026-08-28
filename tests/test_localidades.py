"""The locality is free text; its numeric id is not ours to set.

SAV2's own form has no locality dropdown — it is a free-text box that accepts a
town which does not exist. Passing `localidade_id=1454` on a live Revalidação
(licence 298337, 2026-08-28) was accepted and silently ignored:
`load_player_profile` still reported `localidade_id: None` with
`localidade_txt: "Rio Maior"`, and the enrolment committed and listed cleanly
with `missing: []`. So the id is not the system of record and the client no
longer offers a way to set one.

What it must still do is carry forward whatever SAV already stored. A probe that
sent `localidade=NULL` blanked a live minor's address on 2026-08-27, and the
`localidade` id was the one field that could not be restored — the Modelo 1
carries only the text.
"""

import pytest

from sav_client.sav_client import SavClient


BASE = dict(
  morada=None, cod_postal=None, localidade_txt=None,
  distrito_id=None, concelho_id=None,
)


class TestLocalidadeIsCarriedNotSet:
  def test_stored_id_survives_an_address_edit(self):
    """The regression that blanked a real record."""
    send = SavClient._build_step2_send(
      {"localidade": "1455", "morada": "Rua X", "codpostal": "2040-483"},
      **{**BASE, "morada": "Rua Nova"},
    )
    assert "localidade=1455," in send
    assert 'morada="Rua Nova"' in send

  def test_absent_stored_id_serialises_as_null(self):
    # Honest: we have nothing to send, and we must not invent one.
    assert "localidade=NULL," in SavClient._build_step2_send({}, **BASE)

  def test_there_is_no_way_to_set_the_id(self):
    """Pinned deliberately — SAV ignores it, so offering the knob misleads."""
    with pytest.raises(TypeError):
      SavClient._build_step2_send({}, **BASE, localidade_id=1455)

  def test_free_text_is_the_field_that_carries(self):
    send = SavClient._build_step2_send(
      {"localidade": "1455"}, **{**BASE, "localidade_txt": "Asseiceira"},
    )
    assert 'localidade_txt="Asseiceira"' in send
    assert "localidade=1455," in send  # id untouched alongside it


def test_no_localidade_lookup_is_exposed():
  """`list_localidades` was removed with the setter it existed to serve."""
  assert not hasattr(SavClient, "list_localidades")
