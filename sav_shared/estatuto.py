"""Deciding a player's Estatuto FBP — and refusing to guess one.

Estatuto is the FPB classification of how a player relates to Portuguese
basketball formation. It is legally meaningful (it drives eligibility and the
inscription fee), it is written into a federation record, and it is the one
field on the Modelo 1 that this package must never infer generously.

**The mistake this module exists to make impossible: inferring FBP from
citizenship.** ``FBP`` means *Formação Basquetebolística Portuguesa* — the
player was formed in Portuguese basketball. Portuguese nationality says nothing
about that. A Portuguese passport is evidence for *Sem FBP Comunitário* and is
never, on its own, evidence of Portuguese formation. Nationality here can only
ever choose which *Sem FBP* branch applies; the code below is arranged so it
structurally cannot reach 6 or 12.

The priority order (:func:`resolve_estatuto` stops at the first step that
answers):

1. An official FPB status wins, legacy and transitional values included. A
   status FPB reports that this package does not recognise is a *review
   request*, not a reason to fall through to a default.
2. More than one Estatuto box marked on the Modelo 1 → review. A signed form
   with conflicting answers is not resolved by preference order.
3. Apparent local FBP eligibility → review, never promotion. A player who looks
   like they meet the three-seasons-through-Sub-20 rule still needs FPB to
   confirm it.
4. Exactly one marked box is the explicit answer on a signed form, carried with
   its OCR confidence. A low-confidence read is reported in the reason rather
   than discarded.
5. Nationality may choose only between the two *Sem FBP* branches, and only for
   a country recognised exactly.
6. Otherwise: review. There is no fallback value — "not enough information" is
   a correct answer here and a guess is not.

`Equiparado FBP` (12) is accepted from FPB at step 1 and is unreachable from
every other step: it is an official/historical status with no box on the
current Modelo 1, so local inference must never produce it.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from .lookups import (
  ESTATUTO_FBP,
  ESTATUTO_SEM_FBP_COMUNITARIO,
  ESTATUTO_SEM_FBP_NAO_COMUNITARIO,
  ESTATUTOS,
  estatuto_name,
  find_estatuto_id,
)
from .text import normalise_text

if TYPE_CHECKING:
  from sav_parsers import ParsedField


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


# ── The decision ──────────────────────────────────────────────────────────────

# Source vocabulary for EstatutoDecision.source. A review carries the source
# that *raised* it, so a caller can tell "FPB said something we don't know"
# apart from "the form answered nothing".
SOURCE_FPB = "fpb"
SOURCE_MODELO1 = "modelo1"
SOURCE_LOCAL_ELIGIBILITY = "local_eligibility"
SOURCE_NATIONALITY = "nationality"
SOURCE_NONE = "none"
SOURCE_SAV_RULE = "sav_rule"


@dataclass(frozen=True)
class EstatutoDecision:
  """One Estatuto determination, with the evidence that produced it.

  ``value`` is the SAV estatuto id, or ``None`` when no step could answer.
  ``label`` is its Portuguese SAV label (empty when there is no value).
  ``source`` is one of the ``SOURCE_*`` constants. ``reason`` is a full
  sentence for a human: it is rendered verbatim by consumers, so it must say
  what was read and why that led here. ``confidence`` is the OCR confidence of
  the read that decided it, or ``None`` when the step was not an OCR read
  (an official FPB status, or no evidence at all).

  ``conflict`` is a disagreement between the signed form and SAV. A caller
  must resolve it explicitly rather than silently picking one side.

  ``needs_review`` is derived, never stored: a decision needs review when it
  reached no value, or when the read that produced the value was below
  :data:`LOW_CONFIDENCE`, or when it carries a conflict.
  """

  value: int | None
  label: str
  source: str
  reason: str
  confidence: float | None = None
  conflict: str | None = None

  @property
  def needs_review(self) -> bool:
    """True when this decision must not be submitted unconfirmed."""
    if self.value is None:
      return True
    if self.conflict is not None:
      return True
    return self.confidence is not None and self.confidence < LOW_CONFIDENCE

  def to_dict(self) -> dict[str, Any]:
    """JSON-serializable form for the MCP boundary.

    ``needs_review`` is materialised here because it is a property, and the
    consumers on the other side of MCP read plain dict keys.
    """
    return {
      "value": self.value,
      "label": self.label,
      "source": self.source,
      "reason": self.reason,
      "confidence": (
        round(self.confidence, 2) if self.confidence is not None else None
      ),
      "conflict": self.conflict,
      "needs_review": self.needs_review,
    }


def _decision(
  value: int | None, source: str, reason: str, confidence: float | None = None,
) -> EstatutoDecision:
  return EstatutoDecision(
    value=value,
    label=estatuto_name(value),
    source=source,
    reason=reason,
    confidence=confidence,
  )


# OCR entity (from parse_fpb_mod1) → SAV estatuto id, for the three boxes the
# current Modelo 1 prints. `Equiparado FBP` has no box and so appears nowhere
# here — that absence is what keeps local inference from ever producing it.
ESTATUTO_OCR_ENTITY: dict[str, int] = {
  "estatuto_fbp_fbp":                 ESTATUTO_FBP,
  "estatuto_fbp_sem_comunitario":     ESTATUTO_SEM_FBP_COMUNITARIO,
  "estatuto_fbp_sem_nao_comunitario": ESTATUTO_SEM_FBP_NAO_COMUNITARIO,
}

_NATIONALITY_ENTITY = "nacionalidade"


def _marked_boxes(parsed: dict[str, ParsedField]) -> list[tuple[int, float | None]]:
  """The Estatuto boxes marked on a parsed Modelo 1, as (id, confidence)."""
  marked: list[tuple[int, float | None]] = []
  for entity, estatuto_id in ESTATUTO_OCR_ENTITY.items():
    field = parsed.get(entity)
    if field is not None and field.value:
      marked.append((estatuto_id, field.confidence))
  return marked


def _entity_value(parsed: dict[str, ParsedField], entity: str):
  field = parsed.get(entity)
  return (field.value, field.confidence) if field is not None else (None, None)


def resolve_estatuto(
  parsed: dict[str, ParsedField] | None = None,
  *,
  fpb_status: str | int | None = None,
  local_fbp_eligible: bool = False,
) -> EstatutoDecision:
  """Determine a player's Estatuto FBP, or say why it cannot be determined.

  ``parsed`` is a ``parse_fpb_mod1`` fields dict (or a template-filled form's
  equivalent from ``mod1_acroform_to_fields``). ``fpb_status`` is an official
  status from FPB — an estatuto id or its label, including legacy/transitional
  values. ``local_fbp_eligible`` is the hook for a future local check of the
  three-seasons-through-Sub-20 rule; it can only ever *raise a review*, never
  grant FBP, so callers pass ``False`` until such a check exists.

  Returns an :class:`EstatutoDecision` whose ``value`` is ``None`` whenever
  review is needed. There is no default value and no fall-through: the module
  docstring lists the priority order this implements.
  """
  fields = parsed or {}

  # 1 — an official FPB status wins, and an unknown one is a review request.
  if fpb_status not in (None, ""):
    official = _resolve_fpb_status(fpb_status)
    if official is not None:
      return _decision(
        official, SOURCE_FPB,
        f"FPB reports the official status {ESTATUTOS[official]!r}.",
      )
    return _decision(
      None, SOURCE_FPB,
      f"FPB reported the status {str(fpb_status)!r}, which is not one of the "
      f"statuses this package recognises "
      f"({', '.join(ESTATUTOS.values())}); an official status is never guessed.",
    )

  marked = _marked_boxes(fields)

  # 2 — conflicting boxes are never resolved by preference order.
  if len(marked) > 1:
    names = ", ".join(ESTATUTOS[estatuto_id] for estatuto_id, _ in marked)
    return _decision(
      None, SOURCE_MODELO1,
      f"The Modelo 1 has more than one Estatuto box marked ({names}); a signed "
      f"form that answers twice has to be resolved with the player, not by "
      f"preference order.",
    )

  # 3 — apparent local eligibility is a question for FPB, not a promotion.
  if local_fbp_eligible:
    return _decision(
      None, SOURCE_LOCAL_ELIGIBILITY,
      "The player appears to meet the three-seasons-through-Sub-20 rule for "
      "Formação Basquetebolística Portuguesa, but only FPB can confirm local "
      "formation; ask FPB for the official status.",
    )

  # 4 — one marked box is the explicit answer on a signed form.
  if len(marked) == 1:
    estatuto_id, confidence = marked[0]
    label = ESTATUTOS[estatuto_id]
    if confidence is not None and confidence < LOW_CONFIDENCE:
      return _decision(
        estatuto_id, SOURCE_MODELO1,
        f"The Modelo 1 marks {label!r}, but the checkbox read at low OCR "
        f"confidence ({confidence:.2f}); confirm it against the signed form.",
        confidence,
      )
    return _decision(
      estatuto_id, SOURCE_MODELO1,
      f"The Modelo 1 marks {label!r}.",
      confidence,
    )

  # 5 — nationality chooses between the two Sem FBP branches and nothing else.
  nationality, nat_confidence = _entity_value(fields, _NATIONALITY_ENTITY)
  if nationality:
    branch = nationality_branch(nationality)
    if branch == COMMUNITY:
      return _decision(
        ESTATUTO_SEM_FBP_COMUNITARIO, SOURCE_NATIONALITY,
        f"No Estatuto box was marked; nationality {str(nationality)!r} matches "
        f"the FPB community/cooperation list"
        + _confidence_caveat(nat_confidence),
        nat_confidence,
      )
    if branch == NON_COMMUNITY:
      return _decision(
        ESTATUTO_SEM_FBP_NAO_COMUNITARIO, SOURCE_NATIONALITY,
        f"No Estatuto box was marked; nationality {str(nationality)!r} is on "
        f"the known non-community list"
        + _confidence_caveat(nat_confidence),
        nat_confidence,
      )
    return _decision(
      None, SOURCE_NATIONALITY,
      f"No Estatuto box was marked and nationality {str(nationality)!r} "
      f"matches no country on either list; an unrecognised nationality is not "
      f"evidence of non-community status.",
    )

  # 6 — no evidence. "Not enough information" is the answer.
  return _decision(
    None, SOURCE_NONE,
    "No Estatuto box was marked and the Modelo 1 carries no readable "
    "nationality; there is not enough information to determine an Estatuto.",
  )


def apply_sav_batch_rule(
  decision: EstatutoDecision, *, mini: bool,
  batch_number: str, tier: str,
) -> EstatutoDecision:
  """Apply SAV's enforced 1ª Inscrição Estatuto rule to a decision.

  SAV disables the Estatuto select for non-federation profiles and chooses FBP
  for Mini batches or Sem FBP Comunitário for every other tier. This is a
  constraint SAV enforces in its own wizard, not evidence that a caller may
  use to infer FBP from citizenship. A marked form or official FPB answer is
  therefore preserved when it agrees, and surfaced as an explicit conflict
  when it does not; every other local decision is replaced by SAV's stated
  rule so the fee step cannot receive an Estatuto SAV will not price.
  """
  sav_value = ESTATUTO_FBP if mini else ESTATUTO_SEM_FBP_COMUNITARIO
  sav_label = ESTATUTOS[sav_value]

  if (
    decision.source in (SOURCE_MODELO1, SOURCE_FPB)
    and decision.value is not None
  ):
    if decision.value == sav_value:
      return decision
    form_label = ESTATUTOS.get(decision.value, decision.label)
    source_name = (
      "The signed form marks"
      if decision.source == SOURCE_MODELO1
      else "FPB reports"
    )
    conflict = (
      f"{source_name} estatuto {decision.value} ({form_label}), but SAV's "
      f"rule requires estatuto {sav_value} ({sav_label}) for batch "
      f"{batch_number!r} at tier {tier!r}; the caller must resolve this "
      f"conflict explicitly."
    )
    return replace(decision, conflict=conflict)

  return _decision(
    sav_value,
    SOURCE_SAV_RULE,
    f"SAV's op=151 batch rule read mini={mini!r} for batch "
    f"{batch_number!r} at tier {tier!r}, so this 1ª Inscrição must use "
    f"estatuto {sav_value} ({sav_label}).",
  )


def _confidence_caveat(confidence: float | None) -> str:
  """Close the nationality sentence, naming a low-confidence read."""
  if confidence is not None and confidence < LOW_CONFIDENCE:
    return (
      f", but the nationality read at low OCR confidence ({confidence:.2f}); "
      f"confirm it against the signed form."
    )
  return "."


def _resolve_fpb_status(fpb_status: str | int) -> int | None:
  """An FPB-reported status → a SAV estatuto id, or ``None`` if unrecognised.

  Accepts an id (``6``, ``"6"``) or a label. Nothing is fuzzy-matched: FPB
  naming a status this package does not carry is a review request, and a typo
  rounded to the nearest label would be a legally meaningful misfiling.
  """
  if isinstance(fpb_status, bool):
    return None
  if isinstance(fpb_status, int):
    return fpb_status if fpb_status in ESTATUTOS else None
  text = str(fpb_status).strip()
  if text.isdigit():
    return int(text) if int(text) in ESTATUTOS else None
  return find_estatuto_id(text)
