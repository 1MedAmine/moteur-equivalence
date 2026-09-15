# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from content_gate import (
    ANALYSIS_MODE_TO_GATE,
    ContentGate,
    GateCounters,
    gate_mode_for_analysis,
    qualify_fetched_page,
)
from identite import CORROBORATING_SOURCES, IdentityExtractor
from scraping import PageContent


URL = "https://new.norel.example/products/fr-lu/4KBL137001R1110/xz07-20-10-11"

# Extrait fidele de ce que la vraie page Norel a rendu le 2026-08-19 : du chrome,
# aucun identifiant produit.
CHROME_Norel = (
    "/low-voltage/distributor-inventory/ContactForm "
    "DETAILS-PRODUIT Francais (Luxembourg) Select Country "
    "Argentina Bahamas Barbados Belgium Bolivia Brazil Canada " * 12
)

FICHE = (
    "Contacteur XZ07-20-10-11, code commande 4KBL137001R1110. "
    "Bobine 24 V DC. Courant AC-3 : 9 A."
)


def gate():
    return ContentGate()


def test_product_evidence_rejects_page_without_any_expected_identifier():
    """Mutation détectée : du chrome de navigation part au modèle comme fiche produit."""
    decision = gate().qualify(
        url=URL,
        content=CHROME_Norel,
        mode="PRODUCT_EVIDENCE",
        expected_identifiers=("XZ07-20-10-11", "4KBL137001R1110"),
    )

    assert decision.accepted is False
    assert decision.reason == "REJECTED_FOR_PRODUCT_EVIDENCE"
    assert decision.matched_identifiers == ()
    # Le rejet est un diagnostic, pas un silence.
    diagnostic = decision.as_diagnostic()
    assert diagnostic["url"] == URL
    assert diagnostic["mode"] == "PRODUCT_EVIDENCE"
    assert diagnostic["content_length"] == len(CHROME_Norel)
    assert diagnostic["expected_identifiers"] == ["XZ07-20-10-11", "4KBL137001R1110"]


def test_product_evidence_accepts_on_any_single_raw_identifier():
    """Mutation détectée : la porte exige tous les identifiants au lieu d'un seul."""
    decision = gate().qualify(
        url=URL,
        content="Code commande 4KBL137001R1110 en stock.",
        mode="PRODUCT_EVIDENCE",
        expected_identifiers=("XZ07-20-10-11", "4KBL137001R1110"),
    )

    assert decision.accepted is True
    assert decision.matched_identifiers == ("4KBL137001R1110",)


@pytest.mark.parametrize("variante", ["XZ07201011", "xz07 20 10 11", "XZ07_30_10_11"])
def test_product_evidence_compares_raw_values_never_normalized(variante):
    """Mutation détectée : la normalisation fait matcher une référence absente."""
    decision = gate().qualify(
        url=URL,
        content=f"Reference {variante} disponible.",
        mode="PRODUCT_EVIDENCE",
        expected_identifiers=("XZ07-20-10-11",),
    )

    assert decision.accepted is False
    assert decision.reason == "REJECTED_FOR_PRODUCT_EVIDENCE"


def test_product_evidence_ignores_case_only():
    """La casse seule ne doit pas faire perdre une preuve littérale."""
    assert gate().qualify(
        url=URL,
        content="reference xz07-20-10-11 disponible",
        mode="PRODUCT_EVIDENCE",
        expected_identifiers=("XZ07-20-10-11",),
    ).accepted is True


def test_domain_confirmation_accepts_a_page_without_any_product_reference():
    """Mutation détectée : la page qui confirme le fabricant est rejetée avec les autres."""
    decision = gate().qualify(
        url="https://new.norel.example/low-voltage/fr/produits",
        content="Norel est le fabricant de ces produits. Mentions legales : Norel SA.",
        mode="DOMAIN_CONFIRMATION",
        expected_identifiers=("XZ07-20-10-11",),
        brand_identifiers=("Norel",),
    )

    assert decision.accepted is True
    assert decision.matched_identifiers == ("Norel",)


def test_domain_confirmation_rejects_a_page_without_the_brand():
    decision = gate().qualify(
        url="https://autre.example/x",
        content="Catalogue generaliste sans mention du fabricant recherche.",
        mode="DOMAIN_CONFIRMATION",
        brand_identifiers=("Norel",),
    )

    assert decision.accepted is False
    assert decision.reason == "REJECTED_FOR_DOMAIN_CONFIRMATION"


def test_product_discovery_accepts_without_identity_but_reports_matches():
    """La découverte sert justement à trouver l'identité : elle ne peut pas l'exiger."""
    sans = gate().qualify(
        url=URL, content=CHROME_Norel, mode="PRODUCT_DISCOVERY",
        expected_identifiers=("XZ07-20-10-11",),
    )
    avec = gate().qualify(
        url=URL, content=FICHE, mode="PRODUCT_DISCOVERY",
        expected_identifiers=("XZ07-20-10-11",),
    )

    assert sans.accepted is True and sans.matched_identifiers == ()
    assert avec.accepted is True and avec.matched_identifiers == ("XZ07-20-10-11",)


def test_list_page_can_discover_but_can_never_prove_a_product():
    url = "https://www.marche-a.example/shop/norel-uk?_nkw=norel+uk"

    evidence = gate().qualify(
        url=url,
        content=FICHE,
        mode="PRODUCT_EVIDENCE",
        expected_identifiers=("XZ07-20-10-11",),
    )
    discovery = gate().qualify(
        url=url,
        content=FICHE,
        mode="PRODUCT_DISCOVERY",
        expected_identifiers=("XZ07-20-10-11",),
    )

    assert evidence.accepted is False
    assert evidence.reason == "REJECTED_FOR_PRODUCT_EVIDENCE"
    assert discovery.accepted is True


def test_product_evidence_without_identifiers_is_not_ready_never_a_free_pass():
    """Mutation détectée : sans critère, la porte laisse passer le bruit par défaut."""
    decision = gate().qualify(
        url=URL, content=CHROME_Norel, mode="PRODUCT_EVIDENCE",
        expected_identifiers=(),
    )

    assert decision.accepted is False
    # Etat de la mission, distinct d'un jugement sur la page.
    assert decision.reason == "NOT_READY_FOR_PRODUCT_EVIDENCE"

    blancs = gate().qualify(
        url=URL, content=FICHE, mode="PRODUCT_EVIDENCE",
        expected_identifiers=("", "   "),
    )
    assert blancs.reason == "NOT_READY_FOR_PRODUCT_EVIDENCE"


def test_discovery_and_domain_modes_still_run_without_any_mpn():
    """Les deux autres modes ne dépendent pas d'une identité produit validée."""
    assert gate().qualify(
        url=URL, content=FICHE, mode="PRODUCT_DISCOVERY", expected_identifiers=(),
    ).accepted is True
    assert gate().qualify(
        url=URL, content="Norel, fabricant.", mode="DOMAIN_CONFIRMATION",
        expected_identifiers=(), brand_identifiers=("Norel",),
    ).accepted is True


def test_empty_content_is_rejected_in_every_mode():
    for mode in ("PRODUCT_EVIDENCE", "PRODUCT_DISCOVERY", "DOMAIN_CONFIRMATION"):
        decision = gate().qualify(url=URL, content="   ", mode=mode)  # type: ignore[arg-type]
        assert decision.accepted is False
        assert decision.reason == "REJECTED_EMPTY_CONTENT"


def test_analysis_mode_maps_once_and_targeted_follows_the_declared_mission():
    """Mutation détectée : une seconde énumération de modes diverge de la première."""
    assert ANALYSIS_MODE_TO_GATE == {
        "audit": "PRODUCT_EVIDENCE",
        "discovery": "PRODUCT_DISCOVERY",
        "targeted": "PRODUCT_EVIDENCE",
    }
    assert gate_mode_for_analysis("audit") == "PRODUCT_EVIDENCE"
    assert gate_mode_for_analysis("discovery") == "PRODUCT_DISCOVERY"
    assert gate_mode_for_analysis("targeted") == "PRODUCT_EVIDENCE"
    assert gate_mode_for_analysis(
        "targeted", declared="DOMAIN_CONFIRMATION"
    ) == "DOMAIN_CONFIRMATION"
    # Un mode non `targeted` ne peut pas desactiver la porte en se declarant.
    assert gate_mode_for_analysis(
        "audit", declared="PRODUCT_DISCOVERY"
    ) == "PRODUCT_EVIDENCE"


def test_rejected_pages_count_as_fetched_never_as_analyzed():
    """Mutation détectée : une page rejetée consomme le budget d'analyse."""
    counters = GateCounters()
    rejet = gate().qualify(
        url=URL, content=CHROME_Norel, mode="PRODUCT_EVIDENCE",
        expected_identifiers=("XZ07-20-10-11",),
    )
    accepte = gate().qualify(
        url=URL, content=FICHE, mode="PRODUCT_EVIDENCE",
        expected_identifiers=("XZ07-20-10-11",),
    )

    assert counters.record(rejet) is False
    assert counters.record(accepte) is True

    diagnostic = counters.as_diagnostic()
    assert diagnostic["pages_fetched"] == 2
    assert diagnostic["pages_rejected_by_gate"] == 1
    assert diagnostic["pages_analyzed"] == 1
    assert diagnostic["gate_rejections"][0]["reason"] == "REJECTED_FOR_PRODUCT_EVIDENCE"


def _page(content, url=URL):
    return PageContent(url=url, content=content, title="", mode="scrapling")


def test_a_candidate_lead_never_becomes_a_gate_criterion_on_its_own():
    """Mutation détectée : une piste plausible qualifie une page sans être validée."""
    # La piste dit « XZ07-20-10-11 ». L'URL Norel la porte dans son chemin, mais
    # le contenu récupéré n'a aucun fait : l'identité est observée sans être
    # corroborée, donc elle ne devient pas un critère de qualification.
    decision, identity = qualify_fetched_page(
        page=_page(CHROME_Norel),
        analysis_mode="audit",
        identity_extractor=IdentityExtractor(),
        structured_values=[{"kind": "mpn", "value": "XZ07-20-10-11"}],
    )

    assert {item.source for item in identity.values[0].occurrences} == {"url"}
    # L'URL ne corrobore rien : la mission n'a aucun critère, donc NOT_READY —
    # à distinguer du cas où le titre corrobore et où la page est jugée.
    assert identity.raw_identifiers(sources=CORROBORATING_SOURCES) == ()
    assert decision.accepted is False
    assert decision.reason == "NOT_READY_FOR_PRODUCT_EVIDENCE"


def test_norel_titled_page_without_facts_is_rejected_not_merely_not_ready():
    """Le cas Norel complet : titre porteur, contenu vide de faits.

        title   = XZ07-20-10-11 | Norel   -> identité corroborée
        content = aucun identifiant     -> qualification refusée
    """
    page = PageContent(
        url=URL, content=CHROME_Norel, title="XZ07-20-10-11 | Norel", mode="scrapling"
    )
    decision, identity = qualify_fetched_page(
        page=page,
        analysis_mode="audit",
        identity_extractor=IdentityExtractor(),
        structured_values=[{"kind": "mpn", "value": "XZ07-20-10-11"}],
    )

    # L'identité existe, corroborée par le titre, et reste traçable.
    assert identity.values[0].raw_value == "XZ07-20-10-11"
    assert identity.raw_identifiers(sources=CORROBORATING_SOURCES) == ("XZ07-20-10-11",)
    # La mission avait donc un critère : c'est bien la page qui est jugée,
    # parce que son contenu ne porte pas le produit. Aucun appel LLM.
    assert decision.accepted is False
    assert decision.reason == "REJECTED_FOR_PRODUCT_EVIDENCE"
    assert decision.expected_identifiers == ("XZ07-20-10-11",)
    assert decision.matched_identifiers == ()


def test_identity_validated_against_the_page_unlocks_product_evidence():
    """Le seul chemin : la piste confirmée par le contenu devient critère."""
    decision, identity = qualify_fetched_page(
        page=_page(FICHE),
        analysis_mode="audit",
        identity_extractor=IdentityExtractor(),
        structured_values=[{"kind": "mpn", "value": "XZ07-20-10-11"}],
    )

    assert identity.raw_identifiers() == ("XZ07-20-10-11",)
    assert decision.accepted is True
    assert decision.matched_identifiers == ("XZ07-20-10-11",)


def test_brand_alone_never_unlocks_product_evidence():
    """Mutation détectée : « Norel » qualifie n'importe quelle page du site."""
    decision, identity = qualify_fetched_page(
        page=_page("Norel, fabricant de produits industriels. " + CHROME_Norel),
        analysis_mode="audit",
        identity_extractor=IdentityExtractor(),
        structured_values=[{"kind": "brand", "value": "Norel"}],
    )

    assert identity.raw_identifiers(("brand",)) == ("Norel",)
    assert decision.reason == "NOT_READY_FOR_PRODUCT_EVIDENCE"


def test_discovery_mode_still_runs_on_the_same_page():
    """La même page reste exploitable pour découvrir, pas pour prouver."""
    decision, _ = qualify_fetched_page(
        page=_page(CHROME_Norel),
        analysis_mode="discovery",
        identity_extractor=IdentityExtractor(),
        structured_values=[{"kind": "mpn", "value": "XZ07-20-10-11"}],
    )

    assert decision.accepted is True
    assert decision.mode == "PRODUCT_DISCOVERY"


def test_counters_separate_not_ready_from_rejected_and_stay_consistent():
    """Mutation détectée : « mission pas prête » se lit « pages hors sujet »."""
    counters = GateCounters()
    extractor = IdentityExtractor()

    # Sans identité validée : la mission n'est pas prête.
    for _ in range(3):
        decision, _ = qualify_fetched_page(
            page=_page(CHROME_Norel), analysis_mode="audit",
            identity_extractor=extractor,
            structured_values=[{"kind": "mpn", "value": "XZ07-20-10-11"}],
        )
        counters.record(decision)

    # Identité validée ailleurs, mais cette page-ci ne la porte pas : rejet.
    counters.record(ContentGate().qualify(
        url=URL, content=CHROME_Norel, mode="PRODUCT_EVIDENCE",
        expected_identifiers=("XZ07-20-10-11",),
    ))
    # Page qui la porte : analysée.
    counters.record(ContentGate().qualify(
        url=URL, content=FICHE, mode="PRODUCT_EVIDENCE",
        expected_identifiers=("XZ07-20-10-11",),
    ))

    diagnostic = counters.as_diagnostic()
    assert diagnostic["pages_not_ready_for_evidence"] == 3
    assert diagnostic["pages_rejected_by_gate"] == 1
    assert diagnostic["pages_analyzed"] == 1
    assert diagnostic["pages_fetched"] == 5
    # Les trois colonnes partitionnent exactement les pages récupérées.
    assert diagnostic["pages_fetched"] == (
        diagnostic["pages_analyzed"]
        + diagnostic["pages_rejected_by_gate"]
        + diagnostic["pages_not_ready_for_evidence"]
    )


def test_full_norel_mission_reports_zero_analyzed_without_looking_like_a_network_failure():
    """Le cas réel : 28 pages récupérées, 28 rejetées, 0 analysée — et ça se lit."""
    counters = GateCounters()
    for index in range(28):
        counters.record(gate().qualify(
            url=f"{URL}?p={index}",
            content=CHROME_Norel,
            mode="PRODUCT_EVIDENCE",
            expected_identifiers=("XZ07-20-10-11", "4KBL137001R1110"),
        ))

    diagnostic = counters.as_diagnostic()
    assert diagnostic["pages_fetched"] == 28
    assert diagnostic["pages_rejected_by_gate"] == 28
    assert diagnostic["pages_analyzed"] == 0
    # Chaque rejet nomme sa cause : aucune ambiguïté avec une panne réseau.
    assert {item["reason"] for item in diagnostic["gate_rejections"]} == {
        "REJECTED_FOR_PRODUCT_EVIDENCE"
    }
