# -*- coding: utf-8 -*-
"""Identites produit verifiables, jamais des preuves.

Une identite sert a chercher et a qualifier une page ; elle ne demontre aucune
compatibilite. La regle qui tient tout le module :

    presence litterale d'abord, normalisation ensuite.

Inverser les deux suffirait a rendre une valeur inventee acceptable — il suffit
qu'elle se normalise comme une valeur reelle. C'est pourquoi `raw_value` est
cherche tel quel dans le corpus, et `normalized_value` n'est calcule qu'apres,
pour la deduplication et les requetes.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal, Sequence
from urllib.parse import unquote


IdentityKind = Literal["brand", "manufacturer", "model", "mpn", "ean"]

IDENTITY_KINDS: frozenset[str] = frozenset(
    {"brand", "manufacturer", "model", "mpn", "ean"}
)

#: Types qui designent le produit lui-même. Seuls ceux-ci peuvent servir
#: d'`expected_identifiers` a la porte en mode PRODUCT_EVIDENCE : une marque
#: presente sur toutes les pages d'un site ne qualifie pas une fiche produit.
PRODUCT_KINDS: tuple[str, ...] = ("mpn", "model", "ean")

BRAND_KINDS: tuple[str, ...] = ("brand", "manufacturer")

OccurrenceSource = Literal["url", "title", "snippet", "content"]

#: Trois niveaux, du plus faible au plus fort :
#:
#:     url / snippet -> decouverte seulement
#:     title         -> corroboration d'identite
#:     content       -> qualification PRODUCT_EVIDENCE
#:
#: `url` et `snippet` sont choisis hors de la page. `title` vient de la page
#: mais reste une annonce : la fiche Norel s'intitule `XZ07-20-10-11 | Norel` alors
#: que son contenu rendu ne porte aucun fait technique. Une page censee fournir
#: des preuves produit doit porter le produit dans le contenu reellement
#: recuperé — c'est la meme exigence que celle qui gouverne les extraits.
CORROBORATING_SOURCES: tuple[str, ...] = ("title", "content")

QUALIFYING_SOURCES: tuple[str, ...] = ("content",)

#: Un MPN plausible : au moins un chiffre et une lettre, assez long pour ne pas
#: attraper un mot courant. Sert uniquement au repli deterministe.
MPN_PATTERN = re.compile(r"\b(?=[A-Z0-9-]*[A-Z])(?=[A-Z0-9-]*\d)[A-Z0-9]{2,}(?:-[A-Z0-9]+)*\b")

MIN_FALLBACK_LENGTH = 5

#: Plafond du repli deterministe. Ce chemin n'est qu'un filet quand le modele
#: n'a rien rendu : ses pistes sortent `low_confidence` et ne prouvent rien.
#: Sans plafond, une page de listing hors sujet en produit des milliers.
#: Mesure du 2026-08-20 : 1,2 Mo de HTML `marchand-a.example/shop/listings` ont fige une
#: mission entiere, 1 606 s de CPU sur `literal_occurrences`.
MAX_FALLBACK_IDENTITIES = 40


@dataclass(frozen=True)
class IdentityOccurrence:
    source: OccurrenceSource
    value: str


@dataclass(frozen=True)
class IdentityValue:
    kind: IdentityKind
    raw_value: str
    normalized_value: str
    occurrences: tuple[IdentityOccurrence, ...]
    rank: int = 0
    low_confidence: bool = False

    @property
    def key(self) -> tuple[str, str]:
        return self.kind, self.normalized_value


@dataclass(frozen=True)
class ProductIdentity:
    values: tuple[IdentityValue, ...] = ()

    def of_kinds(self, kinds: Sequence[str]) -> tuple[IdentityValue, ...]:
        return tuple(value for value in self.values if value.kind in kinds)

    def raw_identifiers(
        self,
        kinds: Sequence[str] = PRODUCT_KINDS,
        *,
        include_low_confidence: bool = False,
        sources: Sequence[str] = QUALIFYING_SOURCES,
    ) -> tuple[str, ...]:
        """Valeurs brutes utilisables comme `expected_identifiers`.

        Deux filtres, pour deux facons d'ouvrir la porte a tort :

        - les pistes `low_confidence` sont exclues par defaut — le repli
          deterministe produit une piste de recherche, pas un critere ;
        - seule une occurrence dans le contenu recupere qualifie. L'URL
          `.../4KBL137001R1110/xz07-20-10-11` et le titre `XZ07-20-10-11 | Norel`
          portent tous deux la reference alors que la page rendue n'a aucun
          fait technique : les admettre ferait qualifier exactement les pages
          que la porte doit arreter.

        `CORROBORATING_SOURCES` reste disponible pour la tracabilite et
        `SourceDiscovery`, qui ont besoin de savoir que le titre l'atteste.
        """
        seen: list[str] = []
        autorisees = frozenset(sources)
        for value in self.of_kinds(kinds):
            if value.low_confidence and not include_low_confidence:
                continue
            if not any(item.source in autorisees for item in value.occurrences):
                continue
            if value.raw_value not in seen:
                seen.append(value.raw_value)
        return tuple(seen)


def normalize_identity(value: str) -> str:
    """Unifie Unicode, casse et separateurs — apres la validation litterale."""
    unified = unicodedata.normalize("NFKC", unquote(value)).casefold()
    return " ".join(re.findall(r"[^\W_]+", unified, flags=re.UNICODE))


def is_valid_ean(value: str) -> bool:
    """Checksum EAN-8, UPC-A, EAN-13 ou GTIN-14."""
    digits = value.strip()
    if not digits.isdigit() or len(digits) not in {8, 12, 13, 14}:
        return False
    chiffres = [int(character) for character in digits]
    controle = chiffres.pop()
    # Le poids 3 s'applique en partant du dernier chiffre du corps, quelle que
    # soit la longueur : c'est ce qui rend la regle commune aux quatre formats.
    total = sum(
        chiffre * (3 if index % 2 == 0 else 1)
        for index, chiffre in enumerate(reversed(chiffres))
    )
    return (10 - total % 10) % 10 == controle


def folded_identity_fields(document) -> tuple[tuple[OccurrenceSource, str], ...]:
    """Champs du document, casse repliee une seule fois.

    `literal_occurrences` est appele une fois par valeur candidate. Replier le
    corpus a chaque appel refait le meme travail sur des megaoctets : c'est ce
    qui a fige la mission du 2026-08-20.
    """
    snippets = getattr(document, "snippets", ()) or ()
    champs: tuple[tuple[OccurrenceSource, str], ...] = (
        ("url", unquote(getattr(document, "url", "") or "")),
        ("title", getattr(document, "title", "") or ""),
        ("snippet", "\n".join(snippets)),
        ("content", getattr(document, "content", "") or ""),
    )
    return tuple((source, corpus.casefold()) for source, corpus in champs)


def literal_occurrences(
    raw_value: str,
    document,
    *,
    folded_fields: tuple[tuple[OccurrenceSource, str], ...] | None = None,
) -> tuple[IdentityOccurrence, ...]:
    """Champs du document ou la valeur brute apparait telle quelle.

    `folded_fields` evite de replier le corpus a chaque appel quand plusieurs
    valeurs sont testees contre le meme document.
    """
    value = (raw_value or "").strip()
    if not value:
        return ()
    champs = (
        folded_fields if folded_fields is not None
        else folded_identity_fields(document)
    )
    cible = value.casefold()
    return tuple(
        IdentityOccurrence(source=source, value=value)
        for source, corpus in champs
        if cible in corpus
    )


def deduplicate_identity_values(
    values: Sequence[IdentityValue],
) -> tuple[IdentityValue, ...]:
    """Une valeur par (type, forme normalisee) ; la premiere gagne."""
    retenues: dict[tuple[str, str], IdentityValue] = {}
    for value in values:
        retenues.setdefault(value.key, value)
    return tuple(retenues.values())


def deterministic_identity_leads(document) -> tuple[IdentityValue, ...]:
    """Repli borne quand ScrapeGraphAI n'a rien rendu d'exploitable.

    Ne devine jamais une marque : une marque mal devinee oriente toute la
    mission vers le mauvais fabricant. Seul un MPN/modele alphanumerique
    litteral est releve, et il sort marque `low_confidence` — piste de
    recherche, jamais critere de qualification ni preuve.
    """
    contenu = getattr(document, "content", "") or ""
    titre = getattr(document, "title", "") or ""
    champs = folded_identity_fields(document)
    trouvees: list[IdentityValue] = []
    deja_vues: set[str] = set()
    for brut in MPN_PATTERN.findall(f"{titre}\n{contenu}"):
        # Un catalogue repete la meme reference des centaines de fois : elle ne
        # vaut qu'une observation, et la rescanner ne peut rien apprendre.
        if len(brut) < MIN_FALLBACK_LENGTH or brut in deja_vues:
            continue
        deja_vues.add(brut)
        occurrences = literal_occurrences(brut, document, folded_fields=champs)
        if not occurrences:
            continue
        trouvees.append(IdentityValue(
            kind="mpn",
            raw_value=brut,
            normalized_value=normalize_identity(brut),
            occurrences=occurrences,
            low_confidence=True,
        ))
        if len(trouvees) >= MAX_FALLBACK_IDENTITIES:
            break
    return deduplicate_identity_values(trouvees)


class IdentityExtractor:
    """Filtre les valeurs proposees par le modele contre le corpus reel."""

    def extract(
        self,
        document,
        structured_values: Sequence[dict] | None,
        *,
        excluded_raw_values: Sequence[str] = (),
    ) -> ProductIdentity:
        exclues = {
            value.strip().casefold() for value in excluded_raw_values if value.strip()
        }
        acceptees: list[IdentityValue] = []

        for item in structured_values or ():
            if not isinstance(item, dict):
                continue
            brut = str(item.get("value", "") or "").strip()
            kind = str(item.get("kind", "") or "").strip().casefold()
            if not brut or kind not in IDENTITY_KINDS:
                continue
            # L'identite du produit d'origine ne doit jamais orienter la
            # recherche d'une alternative.
            if brut.casefold() in exclues:
                continue
            if kind == "ean" and not is_valid_ean(brut):
                continue
            occurrences = literal_occurrences(brut, document)
            if not occurrences:
                continue
            acceptees.append(IdentityValue(
                kind=kind,  # type: ignore[arg-type]
                raw_value=brut,
                normalized_value=normalize_identity(brut),
                occurrences=occurrences,
                rank=int(item.get("rank", 0) or 0),
            ))

        return ProductIdentity(values=deduplicate_identity_values(acceptees))
