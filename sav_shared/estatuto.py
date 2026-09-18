"""SAV nationality country lists and classification helpers.

The lists mirror FPB's published agreement and non-community country
vocabulary. They are kept here as dated, reviewable policy data because a
changed list can alter whether a ``nationality_id`` needs operator review.
The helpers deliberately accept only exact country names and known
nationality aliases: an unrecognised spelling is safer as a review than as a
silent country classification. This is separate from SAV's type-1 Estatuto
choice, which SAV determines itself in its wizard.
"""
from __future__ import annotations

from .text import normalise_text


# The OCR-confidence floor shared with the Modelo 1 reconcile loop
# (`fpb_mod1._CONFIDENCE_LOW`). Below it a read is reported but not trusted.
LOW_CONFIDENCE = 0.60


# ── Country lists ─────────────────────────────────────────────────────────────
#
# Source: FPB, Comunicado da Direção nº 091, 26/04/2024 — "Atualização da lista
# de países com acordos com Portugal e a União Europeia", effective from season
# 2024/2025.
# https://www.fpb.pt/wp-content/uploads/2024/04/Com091-Atualizacao-Paises-com-Acordos-com-Portugal-e-UE.pdf
#
# A player holding the nationality of one of these countries who does not meet
# the requirements for "Com Formação Basquetebolística Portuguesa" is
# "Sem Formação Basquetebolística Portuguesa Comunitário" (art. 12º do
# Regulamento de Inscrições e Transferências).
#
# Hardcoded rather than served as reference data, deliberately: the list is a
# published, dated legal instrument, it changes only when FPB republishes it,
# and keeping it here means a republication arrives as a reviewable diff against
# a cited source instead of as a silent data change nobody can date afterwards.
#
# **Portugal is not in the Comunicado** — the table lists countries *with
# agreements with* Portugal, so it omits Portugal itself. It is added
# explicitly below; without it every Portuguese player falls through to review.

COMMUNITY_COUNTRIES: frozenset[str] = frozenset({
  # Added by us — see above. Not an entry in the Comunicado.
  "Portugal",
  # A–D
  "Albânia", "Alemanha", "Angola", "Antígua e Barbuda",
  "Argélia", "Arménia", "Áustria", "Azerbaijão",
  "Bahamas", "Barbados", "Bélgica", "Belize",
  "Benim", "Bielorrússia", "Bósnia Herzegovina", "Botswana",
  "Brasil", "Bulgária", "Burkina Faso", "Burundi",
  # C–D
  "Cabo Verde", "Camarões", "Cazaquistão", "Chade",
  "Chéquia", "Chile", "Chipre", "Comores",
  "Congo", "Costa do Marfim", "Costa Rica", "Croácia",
  "Cuba", "Dinamarca", "Djibouti", "Dominica",
  # E–F
  "Egito", "El Salvador", "Eritreia", "Eslováquia",
  "Eslovénia", "Espanha", "Essuatíni", "Estónia",
  "Etiópia", "Fiji", "Filipinas", "Finlândia",
  "França",
  # G–H
  "Gabão", "Gâmbia", "Gana", "Geórgia",
  "Grécia", "Grenada", "Guatemala", "Guiana",
  "Guiné", "Guiné-Bissau", "Guiné-Equatorial", "Haiti",
  "Honduras", "Hungria",
  # I–L
  "Ilhas Cook", "Ilhas Marshall", "Ilhas Salomão", "Indonésia",
  "Iraque", "Irlanda", "Islândia", "Israel",
  "Itália", "Jamaica", "Jordânia", "Lesoto",
  "Letónia", "Líbano", "Libéria", "Lichtenstein",
  "Lituânia", "Luxemburgo",
  # M
  "Macedónia", "Madagáscar", "Malawi", "Maldivas",
  "Mali", "Malta", "Marrocos", "Maurícias",
  "Mauritânia", "Micronésia", "Moçambique", "Moldávia",
  "Mongólia", "Montenegro",
  # N–Q
  "Namíbia", "Nauru", "Nicarágua", "Níger",
  "Nigéria", "Niue", "Noruega", "Países Baixos",
  "Palau", "Panamá", "Papua-Nova Guiné", "Polónia",
  "Quénia", "Quirguizistão", "Quiribati",
  # R–S
  "República Centro-Africana", "República Democrática do Congo",
  "República Dominicana", "Roménia",
  "Ruanda", "Rússia", "Samoa", "Santa Lúcia",
  "São Cristóvão e Neves", "São Tomé e Príncipe",
  "São Vicente e Granadinas", "Seicheles",
  "Senegal", "Serra Leoa", "Sérvia", "Singapura",
  "Somália", "Suécia", "Suriname",
  # T–Z
  "Tajiquistão", "Tanzânia", "Timor-Leste", "Togo",
  "Tonga", "Trindade e Tobago", "Tunísia", "Turquemenistão",
  "Turquia", "Tuvalu", "Ucrânia", "Uganda",
  "Uzbequistão", "Vanuatu", "Vietname", "Zâmbia",
  "Zimbabué",
})

# The negative inference needs its own positive list.
#
# "Not on the Comunicado" is *not* the same as "non-community": an OCR misread,
# a spelling the Comunicado does not use, or a country FPB simply has not listed
# all look identical to a country that is genuinely outside the agreements. So
# `Sem FBP Não Comunitário` is only ever concluded for a country named here, and
# everything else goes to review.
#
# Membership criterion: the country is absent from the Comunicado *and* its
# absence is unsurprising — it is outside the EU/EEA, outside the ACP group,
# and outside the Euro-Mediterranean, Eastern Partnership and Central Asia
# agreement families that account for the Comunicado's non-EU entries.
#
# Deliberately **not** listed, and therefore sent to review: Reino Unido,
# Suíça, México, África do Sul, Sudão, Líbia, Síria. Each is absent from the
# Comunicado while holding an EU agreement of exactly the kind the list is
# drawn from (the Withdrawal Agreement/TCA, the free-movement agreement, the
# Global Agreement, the TDCA, ACP membership, Euro-Mediterranean association).
# Their absence could as easily be an omission as a decision, and a wrong
# answer here misclassifies a real player. Review is the correct outcome.
NON_COMMUNITY_COUNTRIES: frozenset[str] = frozenset({
  # Américas
  "Argentina", "Bolívia", "Canadá", "Colômbia", "Equador",
  "Estados Unidos da América", "Paraguai", "Peru", "Uruguai", "Venezuela",
  # Ásia
  "Afeganistão", "Arábia Saudita", "Bangladesh", "Barém", "Brunei", "Butão",
  "Camboja", "Catar", "China", "Coreia do Norte", "Coreia do Sul",
  "Emirados Árabes Unidos", "Iémen", "Índia", "Irão", "Japão", "Kuwait",
  "Laos", "Malásia", "Myanmar", "Nepal", "Omã", "Paquistão", "Sri Lanka",
  "Tailândia", "Taiwan",
  # Oceania
  "Austrália", "Nova Zelândia",
})

# Modelo 1's "Nacionalidade" box asks for a nationality, and people write the
# demonym ("Brasileira"), not the country ("Brasil"). Both spoken genders are
# accepted because both are written on real forms.
#
# Explicit, never fuzzy: a nationality that resolves to nothing is a review,
# and that is much better than "Nigeriana" being rounded onto "Nicarágua".
#
# A demonym shared by several countries is mapped to one of them only when
# every country sharing it falls in the same branch, so the branch is right
# whichever was meant — "guineense" (Guiné, Guiné-Bissau) and "coreana"
# (both Koreas) are the cases in play. Decision reasons therefore quote the
# nationality as written on the form and never the country resolved from it.
#
# Not exhaustive: it covers the EU/EEA, the CPLP, and the nationalities that
# actually reach Portuguese basketball. Add rows as forms arrive — an absent
# demonym costs a review, which is the safe failure.
NATIONALITY_ALIASES: dict[str, str] = {
  # ── Portugal ───────────────────────────────────────────────────────────────
  "portuguesa": "Portugal", "portugues": "Portugal",
  # ── EU / EEA ───────────────────────────────────────────────────────────────
  "alema": "Alemanha", "alemao": "Alemanha",
  "austriaca": "Áustria", "austriaco": "Áustria",
  "belga": "Bélgica",
  "bulgara": "Bulgária", "bulgaro": "Bulgária",
  "checa": "Chéquia", "checo": "Chéquia",
  "cipriota": "Chipre",
  "croata": "Croácia",
  "dinamarquesa": "Dinamarca", "dinamarques": "Dinamarca",
  "eslovaca": "Eslováquia", "eslovaco": "Eslováquia",
  "eslovena": "Eslovénia", "esloveno": "Eslovénia",
  "espanhola": "Espanha", "espanhol": "Espanha",
  "estoniana": "Estónia", "estoniano": "Estónia",
  "finlandesa": "Finlândia", "finlandes": "Finlândia",
  "francesa": "França", "frances": "França",
  "grega": "Grécia", "grego": "Grécia",
  "hungara": "Hungria", "hungaro": "Hungria",
  "irlandesa": "Irlanda", "irlandes": "Irlanda",
  "islandesa": "Islândia", "islandes": "Islândia",
  "italiana": "Itália", "italiano": "Itália",
  "leta": "Letónia", "letao": "Letónia", "letoniana": "Letónia",
  "lituana": "Lituânia", "lituano": "Lituânia",
  "luxemburguesa": "Luxemburgo", "luxemburgues": "Luxemburgo",
  "maltesa": "Malta", "maltes": "Malta",
  "neerlandesa": "Países Baixos", "neerlandes": "Países Baixos",
  "holandesa": "Países Baixos", "holandes": "Países Baixos",
  "norueguesa": "Noruega", "norgues": "Noruega", "noruegues": "Noruega",
  "polaca": "Polónia", "polaco": "Polónia",
  "polonesa": "Polónia", "polones": "Polónia",
  "romena": "Roménia", "romeno": "Roménia",
  "sueca": "Suécia", "sueco": "Suécia",
  # ── CPLP ───────────────────────────────────────────────────────────────────
  "angolana": "Angola", "angolano": "Angola",
  "brasileira": "Brasil", "brasileiro": "Brasil",
  "cabo verdiana": "Cabo Verde", "cabo verdiano": "Cabo Verde",
  "caboverdiana": "Cabo Verde", "caboverdiano": "Cabo Verde",
  # Shared by Guiné and Guiné-Bissau; both are community, so the branch holds.
  "guineense": "Guiné-Bissau",
  "equatoguineense": "Guiné-Equatorial",
  "mocambicana": "Moçambique", "mocambicano": "Moçambique",
  "santomense": "São Tomé e Príncipe",
  "sao tomense": "São Tomé e Príncipe",
  "timorense": "Timor-Leste",
  # ── Other countries on the Comunicado ──────────────────────────────────────
  "albanesa": "Albânia", "albanes": "Albânia",
  "argelina": "Argélia", "argelino": "Argélia",
  "bielorrussa": "Bielorrússia", "bielorrusso": "Bielorrússia",
  "bosnia": "Bósnia Herzegovina", "bosniaca": "Bósnia Herzegovina",
  "camaronesa": "Camarões", "camarones": "Camarões",
  "chilena": "Chile", "chileno": "Chile",
  "cubana": "Cuba", "cubano": "Cuba",
  "egipcia": "Egito", "egipcio": "Egito",
  "filipina": "Filipinas", "filipino": "Filipinas",
  "georgiana": "Geórgia", "georgiano": "Geórgia",
  "ganesa": "Gana", "ganes": "Gana",
  "israelita": "Israel",
  "jamaicana": "Jamaica", "jamaicano": "Jamaica",
  "macedonia": "Macedónia", "macedonio": "Macedónia",
  "marfinense": "Costa do Marfim",
  "marroquina": "Marrocos", "marroquino": "Marrocos",
  "moldava": "Moldávia", "moldavo": "Moldávia",
  "montenegrina": "Montenegro", "montenegrino": "Montenegro",
  "nigeriana": "Nigéria", "nigeriano": "Nigéria",
  "queniana": "Quénia", "queniano": "Quénia",
  "russa": "Rússia", "russo": "Rússia",
  "senegalesa": "Senegal", "senegales": "Senegal",
  "servia": "Sérvia", "servio": "Sérvia",
  "tunisina": "Tunísia", "tunisino": "Tunísia",
  "turca": "Turquia", "turco": "Turquia",
  "ucraniana": "Ucrânia", "ucraniano": "Ucrânia",
  # ── Known non-community ────────────────────────────────────────────────────
  "americana": "Estados Unidos da América",
  "americano": "Estados Unidos da América",
  "norte americana": "Estados Unidos da América",
  "norte americano": "Estados Unidos da América",
  "estado unidense": "Estados Unidos da América",
  "argentina": "Argentina", "argentino": "Argentina",
  "australiana": "Austrália", "australiano": "Austrália",
  "boliviana": "Bolívia", "boliviano": "Bolívia",
  "canadiana": "Canadá", "canadiano": "Canadá",
  "canadense": "Canadá",
  "chinesa": "China", "chines": "China",
  "colombiana": "Colômbia", "colombiano": "Colômbia",
  "equatoriana": "Equador", "equatoriano": "Equador",
  "indiana": "Índia", "indiano": "Índia",
  "iraniana": "Irão", "iraniano": "Irão",
  "japonesa": "Japão", "japones": "Japão",
  # Shared by both Koreas; both are non-community, so the branch holds.
  "coreana": "Coreia do Sul", "coreano": "Coreia do Sul",
  "sul coreana": "Coreia do Sul", "sul coreano": "Coreia do Sul",
  "neozelandesa": "Nova Zelândia", "neozelandes": "Nova Zelândia",
  "paquistanesa": "Paquistão", "paquistanes": "Paquistão",
  "paraguaia": "Paraguai", "paraguaio": "Paraguai",
  "peruana": "Peru", "peruano": "Peru",
  "uruguaia": "Uruguai", "uruguaio": "Uruguai",
  "venezuelana": "Venezuela", "venezuelano": "Venezuela",
}

PORTUGAL = "Portugal"

COMMUNITY = "community"
NON_COMMUNITY = "non_community"

_COMMUNITY_BY_KEY: dict[str, str] = {
  normalise_text(name): name for name in COMMUNITY_COUNTRIES
}
_NON_COMMUNITY_BY_KEY: dict[str, str] = {
  normalise_text(name): name for name in NON_COMMUNITY_COUNTRIES
}


def resolve_country(nationality: str | None) -> str | None:
  """Modelo 1 ``Nacionalidade`` text → a country on one of the two lists.

  Accepts either a country name ("Brasil") or a demonym ("Brasileira"),
  accent- and case-insensitively; everything else returns ``None``. Exact
  only — see :data:`NATIONALITY_ALIASES` for why nothing here is fuzzy.
  """
  if not nationality:
    return None
  key = normalise_text(str(nationality))
  if not key:
    return None
  if key in _COMMUNITY_BY_KEY:
    return _COMMUNITY_BY_KEY[key]
  if key in _NON_COMMUNITY_BY_KEY:
    return _NON_COMMUNITY_BY_KEY[key]
  country = NATIONALITY_ALIASES.get(key)
  if country in COMMUNITY_COUNTRIES or country in NON_COMMUNITY_COUNTRIES:
    return country
  return None


def nationality_branch(nationality: str | None) -> str | None:
  """``COMMUNITY`` / ``NON_COMMUNITY`` for a recognised country, else ``None``.

  ``None`` means "not recognised", which is *not* the same as non-community —
  the caller must send it to review rather than take the negative branch.
  """
  country = resolve_country(nationality)
  if country is None:
    return None
  return COMMUNITY if country in COMMUNITY_COUNTRIES else NON_COMMUNITY


def is_portuguese_nationality(nationality: str | None) -> bool:
  """True only when the text resolves exactly to Portugal.

  A *Sem FBP Comunitário* estatuto is not an answer to this question: many
  countries share that branch. This is the nationality field, which SAV's
  type-1 wizard defaults to Portugal — so it must be answered on its own
  evidence or left for review.
  """
  return resolve_country(nationality) == PORTUGAL
