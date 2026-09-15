# -*- coding: utf-8 -*-
"""Qualifier une page recuperee avant de la confier au modele.

Trois questions distinctes, trois etages :

    PageFetcher   -> ai-je reellement recupere du contenu ?
    ContentGate   -> ce contenu est-il pertinent pour cette mission ?
    SmartScraper  -> quelles preuves puis-je en tirer ?

La porte vit ici, entre les deux autres, parce qu'elle est la seule a connaitre
la mission. `PageFetcher` ignore ce qu'on cherche : lui confier la regle
d'identite ferait rejeter une page fabricant sans reference produit, alors
qu'elle est exactement ce dont `OfficialDomainResolver` a besoin pour confirmer
un domaine.

Mesure qui a motive ce module (fiche Norel XZ07-20-10-11, 2026-08-19) : la page
rend 3733 caracteres, dont pas un seul identifiant attendu — un selecteur de
pays et des fragments d'URL de widgets. Sans porte, ce corpus partait au modele
comme contenu produit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from identite import CORROBORATING_SOURCES


GateMode = Literal[
    "DOMAIN_CONFIRMATION",
    "PRODUCT_DISCOVERY",
    "PRODUCT_EVIDENCE",
]

GateReason = Literal[
    "ACCEPTED",
    "REJECTED_FOR_PRODUCT_EVIDENCE",
    "REJECTED_FOR_DOMAIN_CONFIRMATION",
    "REJECTED_EMPTY_CONTENT",
    # Etat de la mission, pas jugement sur la page : sans identifiant valide a
    # comparer, la porte n'a rien pour decider. Le distinguer d'un rejet evite
    # deux erreurs opposees — laisser passer le bruit faute de critere, ou
    # accuser une page correcte d'etre hors sujet.
    "NOT_READY_FOR_PRODUCT_EVIDENCE",
]

#: Correspondance unique entre l'axe d'analyse existant (`examiner_pages.mode`)
#: et l'exigence de la porte. Deux enumerations paralleles obligeraient a
#: retenir un mapping de tete ; il est donc ecrit une seule fois, ici.
ANALYSIS_MODE_TO_GATE: dict[str, GateMode] = {
    "audit": "PRODUCT_EVIDENCE",
    "discovery": "PRODUCT_DISCOVERY",
    # `targeted` sert aux deux : la mission declare explicitement son intention.
    "targeted": "PRODUCT_EVIDENCE",
}


def gate_mode_for_analysis(
    mode: str,
    *,
    declared: GateMode | None = None,
) -> GateMode:
    """Traduit un mode d'analyse en exigence de porte.

    `declared` n'est honore que pour `targeted`, seul mode dont la spec dit
    qu'il depend de la mission ; ailleurs la correspondance reste figee, sinon
    un appelant pourrait desactiver la porte en la declarant autrement.
    """
    if mode == "targeted" and declared is not None:
        return declared
    return ANALYSIS_MODE_TO_GATE.get(mode, "PRODUCT_EVIDENCE")


@dataclass(frozen=True)
class GateDecision:
    """Verdict de la porte, structure pour le rapport autant que pour le code."""

    accepted: bool
    reason: GateReason
    mode: GateMode
    url: str
    content_length: int
    expected_identifiers: tuple[str, ...] = ()
    matched_identifiers: tuple[str, ...] = ()

    def as_diagnostic(self) -> dict:
        """Projection sure : aucun fragment de page, aucun message d'exception."""
        return {
            "url": self.url,
            "reason": self.reason,
            "mode": self.mode,
            "content_length": self.content_length,
            "expected_identifiers": list(self.expected_identifiers),
            "matched_identifiers": list(self.matched_identifiers),
        }


def _literal_matches(identifiers: tuple[str, ...], content: str) -> tuple[str, ...]:
    """Identifiants presents tels quels dans le contenu.

    La comparaison porte sur la valeur brute, insensible a la casse seulement.
    Normaliser d'abord ferait matcher `XZ07201011` avec `XZ07-20-10-11` et
    rouvrirait ce que l'invariant de preuve litterale ferme.
    """
    corpus = content.casefold()
    return tuple(
        value for value in identifiers
        if value.strip() and value.strip().casefold() in corpus
    )


class ContentGate:
    """Decide si une page recuperee merite d'etre envoyee au modele."""

    def qualify(
        self,
        *,
        url: str,
        content: str,
        mode: GateMode,
        expected_identifiers: tuple[str, ...] = (),
        brand_identifiers: tuple[str, ...] = (),
    ) -> GateDecision:
        length = len(content)

        def decision(accepted: bool, reason: GateReason, matched=()) -> GateDecision:
            return GateDecision(
                accepted=accepted,
                reason=reason,
                mode=mode,
                url=url,
                content_length=length,
                expected_identifiers=tuple(
                    value for value in expected_identifiers if value.strip()
                ),
                matched_identifiers=tuple(matched),
            )

        if not content.strip():
            return decision(False, "REJECTED_EMPTY_CONTENT")

        if mode == "PRODUCT_EVIDENCE":
            # Import local : `recherche -> mission -> candidats ->
            # compatibilite`; le charger au niveau module creerait un cycle.
            from recherche import est_page_de_liste

            # Une liste peut faire emerger une reference en decouverte, mais
            # elle juxtapose plusieurs produits : aucune valeur n'y est une
            # preuve attribuable de facon sure a un candidat unique.
            if est_page_de_liste(url):
                return decision(False, "REJECTED_FOR_PRODUCT_EVIDENCE")
            if not any(value.strip() for value in expected_identifiers):
                # Jamais de laissez-passer par defaut : sans critere, la porte
                # laisserait entrer exactement le bruit qu'elle doit arreter.
                return decision(False, "NOT_READY_FOR_PRODUCT_EVIDENCE")
            matched = _literal_matches(expected_identifiers, content)
            if not matched:
                return decision(False, "REJECTED_FOR_PRODUCT_EVIDENCE")
            return decision(True, "ACCEPTED", matched)

        if mode == "DOMAIN_CONFIRMATION":
            matched = _literal_matches(brand_identifiers, content)
            if not matched:
                return decision(False, "REJECTED_FOR_DOMAIN_CONFIRMATION")
            return decision(True, "ACCEPTED", matched)

        # PRODUCT_DISCOVERY : l'identite est souhaitee, jamais exigee — c'est
        # l'etape qui sert justement a la decouvrir.
        return decision(True, "ACCEPTED", _literal_matches(expected_identifiers, content))


def qualify_fetched_page(
    *,
    page,
    analysis_mode: str,
    identity_extractor,
    structured_values=None,
    declared: GateMode | None = None,
    brand_identifiers: tuple[str, ...] = (),
    excluded_raw_values: tuple[str, ...] = (),
    gate: "ContentGate | None" = None,
):
    """Enchaine identite puis porte, dans l'ordre fige par la mission.

        CandidateLead -> contenu recupere -> IdentityExtractor
        -> ProductIdentity validee -> raw_identifiers() -> ContentGate

    Une piste ne traverse jamais cette fonction sans passer par
    `IdentityExtractor` : c'est le seul point ou une hypothese de decouverte
    devient un critere de qualification, et c'est ce qui empeche un
    `CandidateLead` plausible de devenir silencieusement une verite systeme.

    Rend `(GateDecision, ProductIdentity)`.
    """
    identity = identity_extractor.extract(
        page,
        structured_values or (),
        excluded_raw_values=excluded_raw_values,
    )
    mode = gate_mode_for_analysis(analysis_mode, declared=declared)
    decision = (gate or ContentGate()).qualify(
        url=getattr(page, "url", ""),
        content=getattr(page, "content", "") or "",
        mode=mode,
        # Identifiants *corrobores* — attestes par le titre ou le contenu de la
        # page. La regle « seul le contenu qualifie » n'est pas appliquee ici
        # mais dans la porte, qui cherche ces valeurs dans le contenu : c'est
        # ce qui permet de distinguer « la mission n'a aucune identite »
        # (NOT_READY) de « cette page ne porte pas le produit » (REJECTED).
        # Filtrer des l'amont rendrait les deux etats indiscernables.
        # Types produit seulement, et jamais une piste `low_confidence` : une
        # marque qualifierait toutes les pages du site, une regex n'importe
        # quelle page portant un code.
        expected_identifiers=identity.raw_identifiers(
            sources=CORROBORATING_SOURCES,
        ),
        brand_identifiers=brand_identifiers,
    )
    return decision, identity


@dataclass
class GateCounters:
    """Compteurs separes : une page rejetee a ete telechargee, pas analysee.

    `pages_rejected_by_gate` ne compte que les verdicts qui *jugent* la page.
    `NOT_READY_FOR_PRODUCT_EVIDENCE` a sa propre colonne parce qu'il dit
    l'inverse : la mission n'avait pas d'identite validee a comparer, et la
    meme page redeviendrait qualifiable des qu'elle en aurait une. Les
    confondre ferait lire « 28 pages hors sujet » la ou il faut lire « aucune
    identite validee, 28 pages restees sans verdict ».

    Invariant : fetched == analyzed + rejected + not_ready.
    """

    pages_fetched: int = 0
    pages_rejected_by_gate: int = 0
    pages_not_ready_for_evidence: int = 0
    pages_analyzed: int = 0
    rejections: list[dict] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.rejections is None:
            self.rejections = []

    def record(self, decision: GateDecision) -> bool:
        """Enregistre un verdict et dit si la page part a l'analyse."""
        self.pages_fetched += 1
        if decision.accepted:
            self.pages_analyzed += 1
            return True
        if decision.reason == "NOT_READY_FOR_PRODUCT_EVIDENCE":
            self.pages_not_ready_for_evidence += 1
        else:
            self.pages_rejected_by_gate += 1
        self.rejections.append(decision.as_diagnostic())
        return False

    def as_diagnostic(self) -> dict:
        return {
            "pages_fetched": self.pages_fetched,
            "pages_rejected_by_gate": self.pages_rejected_by_gate,
            "pages_not_ready_for_evidence": self.pages_not_ready_for_evidence,
            "pages_analyzed": self.pages_analyzed,
            "gate_rejections": list(self.rejections),
        }
