from sav_shared.enrollment import classify_primeira_duplicate


LICENSED_DUPLICATE = {
  "proximopassoexiste": 0,
  "inscricaovalida": 1,
  "existe": 1,
  "id": "226686",
  "tipo": "1",
  "atleta": 1,
  "nacional": "155",
}

ORPHAN = {
  "proximopassoexiste": 0,
  "inscricaovalida": 0,
  "existe": 1,
  "id": "278342",
  "tipo": "1",
  "atleta": 1,
  "nacional": "155",
  "naturalidade": None,
  "profissao": None,
}


def test_licensed_duplicate_blocks():
  result = classify_primeira_duplicate(LICENSED_DUPLICATE)

  assert result.existing_id == 226686
  assert result.blocking is True
  assert result.reusable is False


def test_orphan_is_reusable():
  result = classify_primeira_duplicate(ORPHAN)

  assert result.existing_id == 278342
  assert result.blocking is False
  assert result.reusable is True


def test_no_person_is_neither_blocking_nor_reusable():
  result = classify_primeira_duplicate({"existe": 0, "inscricaovalida": 0})

  assert result.existing_id is None
  assert result.blocking is False
  assert result.reusable is False


def test_missing_enrolment_flag_blocks_fail_closed():
  payload = dict(LICENSED_DUPLICATE)
  del payload["inscricaovalida"]

  result = classify_primeira_duplicate(payload)

  assert result.blocking is True
  assert result.reusable is False


def test_string_existe_flag_is_decoded():
  payload = dict(LICENSED_DUPLICATE, existe="1")

  result = classify_primeira_duplicate(payload)

  assert result.blocking is True


def test_none_existe_flag_is_not_a_duplicate():
  result = classify_primeira_duplicate({"existe": None, "inscricaovalida": 1})

  assert result.existing_id is None
  assert result.blocking is False
  assert result.reusable is False


def test_atleta_is_not_used_as_a_reuse_id():
  payload = dict(ORPHAN)
  del payload["id"]

  result = classify_primeira_duplicate(payload)

  assert result.reusable is True
  assert result.existing_id is None
