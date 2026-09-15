# -*- coding: utf-8 -*-
"""Décision B2 déterministe à partir des audits ScrapeGraphAI."""

from __future__ import annotations

import sys
import re
import unicodedata
from pathlib import Path
from time import perf_counter

import pytest
from hypothesis import given, strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from compatibilite import (
    CompatibilityContractError,
    EvidenceContractError,
    _brand_matches,
    cached_reaudit_downgraded_criteria,
    _official_page_confirms_identity,
    _range_contains_requested,
    evaluate_candidates,
    reaudit_cached_product_evidence,
    select_best_candidate,
)
from modeles import (
    CandidateAudit,
    CriterionAudit,
    PageAudit,
    Requirement,
    RequirementSet,
    SourceProof,
)
from mission import construire_mission_audit, construire_mission_criteres


URL = "https://new.norel.example/product/ref-1"
CONTENT = "Référence REF-1. Tension de commande 24 V DC. Trois pôles. Courant 9 A."


def _requirements(count: int = 4, critical: set[str] | None = None) -> RequirementSet:
    critical = critical or set()
    return RequirementSet(
        product="Contacteur source",
        criteria=[
            Requirement(
                id=f"r{index}",
                label=f"Critère {index}",
                requested_value=f"valeur {index}",
                critical=f"r{index}" in critical,
            )
            for index in range(1, count + 1)
        ],
    )


def _candidate(statuses: list[str], *, reference: str = "REF-1",
               brand: str = "Norel", proof: SourceProof | None = None) -> CandidateAudit:
    evidence = proof or SourceProof(
        url=URL,
        excerpt="Tension de commande 24 V DC",
        type="web_officiel",
    )
    return CandidateAudit(
        brand=brand,
        reference=reference,
        criteria=[
            CriterionAudit(
                requirement_id=f"r{index}",
                requested_value=f"valeur {index}",
                observed_value=f"valeur {index}",
                status=status,
                proofs=[evidence] if status != "not_proven" else [],
            )
            for index, status in enumerate(statuses, start=1)
        ],
    )


def _evaluate(requirements, candidate, *, threshold=75, pages=None):
    return evaluate_candidates(
        requirements,
        [PageAudit(page_url=URL, candidates=[candidate])],
        target_brand="Norel",
        threshold=threshold,
        visited_pages=pages or {URL: CONTENT},
    )[0]


def test_score_is_computed_from_immutable_criteria():
    """Mutation détectée : le score libre du modèle remplace le rapport 3/4."""
    evaluation = _evaluate(
        _requirements(),
        _candidate(["proven", "proven", "proven", "not_proven"]),
    )

    assert evaluation.summary.score == 75
    assert evaluation.eligible
    assert not evaluation.complete


def test_critical_incompatibility_blocks_even_at_threshold():
    """Mutation détectée : un calibre/tension incompatible passe grâce au score."""
    evaluation = _evaluate(
        _requirements(4, critical={"r4"}),
        _candidate(["proven", "proven", "proven", "incompatible"]),
    )

    assert evaluation.summary.score == 75
    assert not evaluation.eligible
    assert evaluation.summary.critical_blockers == ["Critère 4"]


def test_complete_requires_every_criterion_proven():
    """Mutation détectée : `complete` est accordé avec un point à confirmer."""
    evaluation = _evaluate(_requirements(2), _candidate(["proven", "proven"]))

    assert evaluation.summary.score == 100
    assert evaluation.complete
    assert evaluation.eligible


def test_secondary_only_evidence_keeps_the_score_but_cannot_select_a_candidate():
    secondary_url = "https://distributor.example/products/ref-1"
    candidate = _candidate(["proven"] * 4)
    for criterion in candidate.criteria:
        criterion.proofs[0].type = "web_secondaire"
        criterion.proofs[0].url = secondary_url

    evaluation = _evaluate(
        _requirements(4), candidate, pages={secondary_url: CONTENT},
    )

    assert evaluation.summary.score == 100
    assert evaluation.official_proof_count == 0
    assert not evaluation.eligible
    assert not evaluation.complete


def test_one_verified_proof_below_threshold_is_not_eligible():
    """Une preuve isolée sous 75 % ne doit jamais devenir une proposition."""
    candidate = _candidate(["proven", "not_proven", "not_proven", "not_proven"])

    evaluation = _evaluate(_requirements(4), candidate)

    assert evaluation.summary.score == 25
    assert not evaluation.eligible


def test_single_secondary_product_source_at_100_is_eligible_but_not_complete():
    """Régression Distributeur A : 6/6 propose le produit sans simuler une corroboration."""
    secondary_url = "https://www.distributeur-a.example/products/ref-1"
    candidate = _candidate(["proven"] * 4)
    for criterion in candidate.criteria:
        criterion.proofs[0].type = "web_secondaire"
        criterion.proofs[0].url = secondary_url

    evaluation = _evaluate(
        _requirements(4),
        candidate,
        pages={secondary_url: f"Norel REF-1. {CONTENT}"},
    )

    assert evaluation.summary.score == 100
    assert evaluation.proof_url_count == 1
    assert evaluation.official_proof_count == 0
    assert evaluation.eligible
    assert not evaluation.complete


def test_two_secondary_product_sources_at_100_are_not_enough_for_complete():
    first_url = "https://distributor-one.com/products/ref-1"
    second_url = "https://distributor-two.com/products/ref-1"
    candidate = _candidate(["proven"] * 4)
    for index, criterion in enumerate(candidate.criteria):
        criterion.proofs = [criterion.proofs[0].model_copy(update={
            "type": "web_secondaire",
            "url": first_url if index < 2 else second_url,
        })]

    evaluation = _evaluate(
        _requirements(4),
        candidate,
        pages={
            first_url: f"Norel REF-1. {CONTENT}",
            second_url: f"Norel REF-1. {CONTENT}",
        },
    )

    assert evaluation.summary.score == 100
    assert evaluation.proof_url_count == 2
    assert evaluation.official_proof_count == 0
    assert evaluation.eligible
    assert not evaluation.complete


def test_three_independent_secondary_domains_at_100_allow_complete():
    urls = [
        "https://distributor-one.com/products/ref-1",
        "https://distributor-two.com/products/ref-1",
        "https://distributor-three.com/products/ref-1",
    ]
    candidate = _candidate(["proven"] * 4)
    for index, criterion in enumerate(candidate.criteria):
        criterion.proofs = [criterion.proofs[0].model_copy(update={
            "type": "web_secondaire",
            "url": urls[min(index, 2)],
        })]

    evaluation = _evaluate(
        _requirements(4),
        candidate,
        pages={url: f"Norel REF-1. {CONTENT}" for url in urls},
    )

    assert evaluation.summary.score == 100
    assert evaluation.proof_url_count == 3
    assert evaluation.official_proof_count == 0
    assert evaluation.eligible
    assert evaluation.complete


def test_three_subdomains_of_one_secondary_domain_do_not_allow_complete():
    urls = [
        "https://www.distributor.com/products/ref-1",
        "https://shop.distributor.com/products/ref-1",
        "https://api.distributor.com/products/ref-1",
    ]
    candidate = _candidate(["proven"] * 4)
    for index, criterion in enumerate(candidate.criteria):
        criterion.proofs = [criterion.proofs[0].model_copy(update={
            "type": "web_secondaire",
            "url": urls[min(index, 2)],
        })]

    evaluation = _evaluate(
        _requirements(4),
        candidate,
        pages={url: f"Norel REF-1. {CONTENT}" for url in urls},
    )

    assert evaluation.proof_url_count == 3
    assert evaluation.eligible
    assert not evaluation.complete


def test_compact_identity_led_value_listing_cannot_prove_many_criteria():
    page_url = "https://catalogue.example.com/products/kc1c310atd"
    listing = (
        "LS KC1C310ATD Contactor IEC Mini 3-Pole 24VDC Coil 9A "
        "1NO Aux. DIN Rail M Series"
    )
    requirements = RequirementSet(
        product="Contacteur source",
        criteria=[
            Requirement(id="current", label="Courant nominal", requested_value="9 A"),
            Requirement(id="coil", label="Tension de bobine", requested_value="24 V DC"),
            Requirement(id="poles", label="Nombre de poles", requested_value="3P"),
            Requirement(id="aux", label="Contact auxiliaire", requested_value="1NO"),
        ],
    )
    proof = SourceProof(
        url=page_url,
        excerpt=listing,
        type="web_secondaire",
    )
    candidate = CandidateAudit(
        brand="LS",
        reference="KC1C310ATD",
        criteria=[
            CriterionAudit(
                requirement_id=requirement.id,
                requested_value=requirement.requested_value,
                observed_value=requirement.requested_value,
                status="proven",
                proofs=[proof],
            )
            for requirement in requirements.criteria
        ],
    )

    evaluation = evaluate_candidates(
        requirements,
        [PageAudit(page_url=page_url, candidates=[candidate])],
        target_brand="LS",
        threshold=75,
        visited_pages={page_url: listing},
        strict_evidence=False,
    )[0]

    assert evaluation.summary.score == 0
    assert not evaluation.eligible
    assert set(evaluation.summary.rejected_proof_criteria) == {
        "Courant nominal",
        "Tension de bobine",
        "Nombre de poles",
        "Contact auxiliaire",
    }


def test_identity_led_sentence_with_technical_context_remains_admissible():
    page_url = "https://catalogue.example.com/products/kc1c310atd"
    sentence = (
        "LS KC1C310ATD has rated operational current 9 A and "
        "a control coil voltage of 24 V DC."
    )
    requirements = RequirementSet(
        product="Contacteur source",
        criteria=[
            Requirement(id="current", label="Courant nominal", requested_value="9 A"),
            Requirement(id="coil", label="Tension de bobine", requested_value="24 V DC"),
        ],
    )
    proof = SourceProof(url=page_url, excerpt=sentence, type="web_secondaire")
    candidate = CandidateAudit(
        brand="LS",
        reference="KC1C310ATD",
        criteria=[
            CriterionAudit(
                requirement_id=requirement.id,
                requested_value=requirement.requested_value,
                observed_value=requirement.requested_value,
                status="proven",
                proofs=[proof],
            )
            for requirement in requirements.criteria
        ],
    )

    evaluation = evaluate_candidates(
        requirements,
        [PageAudit(page_url=page_url, candidates=[candidate])],
        target_brand="LS",
        threshold=75,
        visited_pages={page_url: sentence},
        strict_evidence=False,
    )[0]

    assert evaluation.summary.score == 100
    assert evaluation.eligible
    assert not evaluation.complete


@given(
    source_type=st.sampled_from(["web_officiel", "web_secondaire"]),
    threshold=st.integers(min_value=1, max_value=100),
)
def test_verified_incompatibility_is_never_eligible_for_any_source_or_threshold(
    source_type,
    threshold,
):
    """PBT-03 : le score et la provenance ne peuvent masquer un écart prouvé."""
    proof_url = URL if source_type == "web_officiel" else (
        "https://distributor.example/products/ref-1"
    )
    candidate = _candidate(
        ["proven", "proven", "proven", "incompatible"],
        proof=SourceProof(
            url=proof_url,
            excerpt="Tension de commande 24 V DC",
            type=source_type,
        ),
    )

    evaluation = evaluate_candidates(
        _requirements(4),
        [PageAudit(page_url=proof_url, candidates=[candidate])],
        target_brand="Norel",
        threshold=threshold,
        visited_pages={proof_url: f"Norel REF-1. {CONTENT}"},
    )[0]

    assert evaluation.summary.incompatible_criteria == ["Critère 4"]
    assert not evaluation.eligible
    assert not evaluation.complete


def test_any_verified_incompatibility_excludes_candidate_even_when_not_critical():
    """Un pourcentage élevé ne doit jamais masquer un écart technique établi."""
    evaluation = _evaluate(
        _requirements(4),
        _candidate(["proven", "proven", "proven", "incompatible"]),
    )

    assert evaluation.summary.score == 75
    assert evaluation.summary.incompatible_criteria == ["Critère 4"]
    assert not evaluation.eligible


def test_official_proofs_without_the_candidate_reference_do_not_confirm_identity():
    candidate = _candidate(["proven"] * 4, reference="REF-NOT-IN-PAGE")

    evaluation = _evaluate(_requirements(4), candidate)

    assert evaluation.summary.score == 100
    assert evaluation.official_proof_count == 4
    assert not evaluation.eligible
    assert not evaluation.complete


@pytest.mark.parametrize("lookalike_url", [
    "https://notnorel.example/product/ref-1",
    "https://norel.evil.example/product/ref-1",
])
def test_brand_lookalike_domains_cannot_confirm_official_identity(lookalike_url):
    candidate = _candidate(["proven"] * 4)
    for criterion in candidate.criteria:
        criterion.proofs[0].url = lookalike_url

    evaluation = evaluate_candidates(
        _requirements(4),
        [PageAudit(page_url=lookalike_url, candidates=[candidate])],
        target_brand="Norel",
        threshold=75,
        visited_pages={lookalike_url: CONTENT},
    )[0]

    assert evaluation.summary.score == 100
    assert not evaluation.eligible
    assert not evaluation.complete


@pytest.mark.parametrize("official_url", [
    "https://maker.co.uk/product/ref-1",
    "https://maker.com.au/product/ref-1",
    "https://maker.co.jp/product/ref-1",
    "https://maker.com.vn/product/ref-1",
    "https://maker.co.id/product/ref-1",
    "https://maker.co.th/product/ref-1",
    "https://maker.co.il/product/ref-1",
])
def test_generic_manufacturer_domains_support_common_multilabel_suffixes(
    official_url,
):
    candidate = _candidate(["proven"] * 4, brand="Maker")
    for criterion in candidate.criteria:
        criterion.proofs[0].url = official_url

    evaluation = evaluate_candidates(
        _requirements(4),
        [PageAudit(page_url=official_url, candidates=[candidate])],
        target_brand="Maker",
        threshold=75,
        visited_pages={official_url: CONTENT},
    )[0]

    assert evaluation.summary.score == 100
    assert evaluation.eligible
    assert evaluation.complete


def test_list_page_audit_never_produces_a_candidate_evaluation():
    list_url = "https://www.marche-a.example/shop/norel-uk?_nkw=norel+uk"
    candidate = _candidate(
        ["proven"],
        proof=SourceProof(
            url=list_url,
            excerpt="Tension de commande 24 V DC",
            type="web_secondaire",
        ),
    )

    evaluations = evaluate_candidates(
        _requirements(1),
        [PageAudit(page_url=list_url, candidates=[candidate])],
        target_brand="Norel",
        threshold=75,
        visited_pages={list_url: CONTENT},
    )

    assert evaluations == []


def test_list_page_proof_is_rejected_even_when_the_audit_url_looks_like_a_product():
    list_url = "https://catalog.example/search?q=ref-1"
    candidate = _candidate(
        ["proven"],
        proof=SourceProof(
            url=list_url,
            excerpt="Tension de commande 24 V DC",
            type="web_secondaire",
        ),
    )

    with pytest.raises(EvidenceContractError, match="page de liste"):
        evaluate_candidates(
            _requirements(1),
            [PageAudit(page_url=URL, candidates=[candidate])],
            target_brand="Norel",
            threshold=75,
            visited_pages={list_url: CONTENT},
        )


def test_mixed_audit_counts_only_the_product_page_and_ignores_list_proof():
    list_url = "https://catalog.example/search?q=ref-1"
    candidate = _candidate(["proven"])
    candidate.criteria[0].proofs.insert(0, SourceProof(
        url=list_url,
        excerpt="Tension de commande 24 V DC",
        type="web_secondaire",
    ))

    evaluation = evaluate_candidates(
        _requirements(1),
        [PageAudit(page_url=list_url, candidates=[candidate])],
        target_brand="Norel",
        threshold=75,
        visited_pages={list_url: CONTENT, URL: CONTENT},
    )[0]

    assert evaluation.summary.score == 100
    assert evaluation.proof_url_count == 1
    assert [proof.url for proof in evaluation.candidate.criteria[0].proofs] == [URL]


def test_non_strict_evaluation_removes_an_invented_proof_but_keeps_a_valid_one():
    candidate = _candidate(["proven"])
    candidate.criteria[0].proofs.insert(0, SourceProof(
        url=URL,
        excerpt="fragment invente absent",
        type="web_officiel",
    ))

    evaluation = evaluate_candidates(
        _requirements(1),
        [PageAudit(page_url=URL, candidates=[candidate])],
        target_brand="Norel",
        threshold=75,
        visited_pages={URL: CONTENT},
        strict_evidence=False,
    )[0]

    assert evaluation.summary.score == 100
    assert [
        proof.excerpt for proof in evaluation.candidate.criteria[0].proofs
    ] == ["Tension de commande 24 V DC"]


def test_unvisited_url_is_rejected():
    """Mutation détectée : une URL inventée est acceptée comme preuve."""
    candidate = _candidate(
        ["proven"],
        proof=SourceProof(
            url="https://invented.example/product",
            excerpt="Tension de commande 24 V DC",
            type="web_secondaire",
        ),
    )

    with pytest.raises(EvidenceContractError, match="non visitée"):
        _evaluate(_requirements(1), candidate)


def test_excerpt_must_exist_in_recovered_page():
    """Mutation détectée : un extrait halluciné est accepté car il est non vide."""
    candidate = _candidate(
        ["proven"],
        proof=SourceProof(
            url=URL,
            excerpt="Bobine universelle 12 à 240 V",
            type="web_secondaire",
        ),
    )

    with pytest.raises(EvidenceContractError, match="absent"):
        _evaluate(_requirements(1), candidate)


def test_typographic_apostrophe_does_not_break_the_excerpt_match():
    """CAP-3 : une apostrophe typographique sur la page n'invalide pas l'extrait."""
    candidate = _candidate(
        ["proven"],
        proof=SourceProof(url=URL, excerpt="L'appareil se règle", type="web_officiel"),
    )

    _evaluate(_requirements(1), candidate, pages={URL: "Notice : L’appareil se règle en usine."})


def test_dash_variant_does_not_break_the_excerpt_match():
    """CAP-3 : un tiret cadratin sur la page n'invalide pas l'extrait."""
    candidate = _candidate(
        ["proven"],
        proof=SourceProof(url=URL, excerpt="Courant 9-16 A", type="web_officiel"),
    )

    _evaluate(_requirements(1), candidate, pages={URL: "Plage : Courant 9–16 A en sortie."})


def test_soft_hyphen_does_not_break_the_excerpt_match():
    """CAP-3 : un trait d'union conditionnel invisible n'invalide pas l'extrait."""
    candidate = _candidate(
        ["proven"],
        proof=SourceProof(url=URL, excerpt="Disjoncteur triphasé", type="web_officiel"),
    )

    _evaluate(_requirements(1), candidate, pages={URL: "Dis­joncteur triphasé pour tableau."})


def test_all_four_unicode_variants_combined_still_match():
    """CAP-3 : apostrophe, tiret, trait d'union conditionnel et accents combinés."""
    candidate = _candidate(
        ["proven"],
        proof=SourceProof(
            url=URL, excerpt="L'appareil - 9-16 A - reference", type="web_officiel"
        ),
    )

    _evaluate(
        _requirements(1),
        candidate,
        pages={URL: "Notice : L’appareil – 9–16 A – ré­férence certifiée."},
    )


def test_audit_must_cover_exact_immutable_ids_and_values():
    """Mutation détectée : ScrapeGraphAI supprime ou réécrit un besoin demandé."""
    candidate = _candidate(["proven", "proven"])
    candidate.criteria[1].requested_value = "valeur changée"

    with pytest.raises(CompatibilityContractError, match="valeur demandée"):
        _evaluate(_requirements(2), candidate)


def test_missing_audit_ids_are_visible_as_unverified_in_soft_mode():
    candidate = _candidate(["proven"])

    evaluation = _evaluate_souple(_requirements(2), candidate)[0]

    assert evaluation.summary.score == 50
    assert evaluation.summary.proven_criteria == ["Critère 1"]
    assert evaluation.summary.not_proven_criteria == []
    assert evaluation.summary.incompatible_criteria == []
    assert evaluation.summary.unverified_criteria == ["Critère 2"]
    assert evaluation.summary.missing_audit_criteria == ["Critère 2"]
    assert evaluation.summary.rejected_proof_criteria == []


def test_duplicate_audit_ids_remain_a_contract_error():
    candidate = _candidate(["proven", "proven"])
    candidate.criteria[1].requirement_id = "r1"

    with pytest.raises(CompatibilityContractError, match="identifiants"):
        _evaluate_souple(_requirements(2), candidate)


def test_unexpected_audit_ids_remain_a_contract_error():
    candidate = _candidate(["proven", "proven", "proven"])

    with pytest.raises(CompatibilityContractError, match="identifiants"):
        _evaluate_souple(_requirements(2), candidate)


def test_requested_value_uses_unicode_proof_normalization():
    requirements = _requirements(1)
    requirements.criteria[0].requested_value = "Reference XZ07-20-10-11 d'Norel"
    candidate = _candidate(["proven"])
    candidate.criteria[0].requested_value = (
        "Re\u0301fe\u0301rence XZ07\u201320\u201310\u201311 d\u2019Norel"
    )

    evaluation = _evaluate(requirements, candidate)

    assert evaluation.summary.score == 100


def test_target_brand_is_mandatory_when_requested():
    """Mutation détectée : une recherche Norel retient un candidat Kerion."""
    evaluation = _evaluate(_requirements(1), _candidate(["proven"], brand="Kerion"))

    assert not evaluation.eligible


def _evaluate_with_brand(requirements, candidate, target_brand, *, threshold=75):
    normalized_brand = unicodedata.normalize("NFKD", candidate.brand).casefold()
    normalized_brand = "".join(
        character for character in normalized_brand
        if not unicodedata.combining(character)
    )
    tokens = re.findall(r"[a-z0-9]+", normalized_brand)
    if "kerion" in tokens or tokens == ["se"]:
        hostname = "kr.example"
    elif "norel" in tokens:
        hostname = "norel.example"
    else:
        hostname = f"{'-'.join(tokens)}.example"
    official_url = f"https://{hostname}/product/{candidate.reference}"
    for criterion in candidate.criteria:
        for proof in criterion.proofs:
            proof.url = official_url
    return evaluate_candidates(
        requirements,
        [PageAudit(page_url=official_url, candidates=[candidate])],
        target_brand=target_brand,
        threshold=threshold,
        visited_pages={official_url: f"{CONTENT} {candidate.reference}"},
    )[0]


def test_target_brand_matches_a_longer_manufacturer_name():
    """CAP-2 : une cible « Kerion » retient un fabricant « Kerion Electric »."""
    evaluation = _evaluate_with_brand(
        _requirements(1), _candidate(["proven"], brand="Kerion Electric"), "Kerion"
    )

    assert evaluation.eligible


def test_known_short_alias_matches_a_longer_manufacturer_name():
    """CAP-2 : l'alias documenté « SE » retient « Kerion Electric »."""
    evaluation = _evaluate_with_brand(
        _requirements(1), _candidate(["proven"], brand="Kerion Electric"), "SE"
    )

    assert evaluation.eligible


def test_unknown_short_abbreviation_is_never_expanded():
    """CAP-2 : une abréviation courte non répertoriée n'élargit jamais la cible."""
    evaluation = _evaluate_with_brand(
        _requirements(1), _candidate(["proven"], brand="A Electric"), "A"
    )

    assert not evaluation.eligible


def test_two_character_unknown_abbreviation_is_never_expanded():
    """CAP-2 : pin précis de la borne du garde-fou (<= 2 caractères, pas 1 seul)."""
    evaluation = _evaluate_with_brand(
        _requirements(1), _candidate(["proven"], brand="AB Electric"), "AB"
    )

    assert not evaluation.eligible


def test_three_character_unknown_brand_uses_subset_match_not_the_guard():
    """CAP-2 : au-delà de 2 caractères, le garde-fou ne s'applique plus."""
    evaluation = _evaluate_with_brand(
        _requirements(1), _candidate(["proven"], brand="Norel France"), "Norel"
    )

    assert evaluation.eligible


def test_manufacturer_side_alias_is_also_resolved():
    """CAP-2 : l'alias joue aussi côté fabricant, pas seulement côté cible."""
    evaluation = _evaluate_with_brand(
        _requirements(1), _candidate(["proven"], brand="SE"), "Kerion"
    )

    assert evaluation.eligible


def test_diacritics_are_folded_before_comparison():
    """CAP-2 : la tokenisation NFKD rend la comparaison insensible aux accents."""
    assert _brand_matches("Télémarque", "Telemarque")


def test_punctuation_only_target_matches_nothing():
    """CAP-2 : une cible sans aucun token exploitable ne matche jamais par défaut."""
    evaluation = _evaluate_with_brand(
        _requirements(1), _candidate(["proven"], brand="Kerion Electric"), "!!!"
    )

    assert not evaluation.eligible


def test_short_alias_does_not_capture_an_unrelated_manufacturer():
    """CAP-2 : « SE » ne doit jamais attraper DOKRAN par sous-chaîne."""
    evaluation = _evaluate_with_brand(
        _requirements(1), _candidate(["proven"], brand="DOKRAN"), "SE"
    )

    assert not evaluation.eligible


def test_target_brand_matches_despite_case_spacing_and_legal_suffix():
    """CAP-2 : casse, espaces et forme juridique en surplus n'empêchent pas l'inclusion."""
    evaluation = _evaluate_with_brand(
        _requirements(1),
        _candidate(["proven"], brand="KERION ELECTRIC SA"),
        "  kerion  ",
    )

    assert evaluation.eligible


def test_no_target_brand_matches_any_candidate():
    """Comportement inchangé : aucune marque imposée matche tout candidat."""
    evaluation = _evaluate_with_brand(
        _requirements(1), _candidate(["proven"], brand="Kerion Electric"), None
    )

    assert evaluation.eligible


def test_whitespace_only_target_brand_matches_any_candidate():
    """Comportement préservé : une cible réduite à des espaces équivaut à aucune cible."""
    evaluation = _evaluate_with_brand(
        _requirements(1), _candidate(["proven"], brand="Kerion Electric"), "   "
    )

    assert evaluation.eligible


def test_best_candidate_prefers_score_then_official_proof():
    """Mutation détectée : le classement préfère un revendeur moins complet."""
    requirements = _requirements(4)
    official = _candidate(["proven"] * 4, reference="OFFICIAL")
    secondary = _candidate(["proven"] * 3 + ["not_proven"], reference="SECONDARY")
    secondary.criteria[0].proofs[0].type = "web_secondaire"
    evaluations = evaluate_candidates(
        requirements,
        [PageAudit(page_url=URL, candidates=[secondary, official])],
        target_brand="Norel",
        threshold=75,
        visited_pages={URL: f"{CONTENT} OFFICIAL SECONDARY"},
    )

    assert select_best_candidate(evaluations).summary.reference == "OFFICIAL"


def _evaluate_souple(requirements, candidate, *, threshold=75):
    """Évalue en mode CAP-4 : preuve invérifiable rétrogradée, pas fatale."""
    return evaluate_candidates(
        requirements,
        [PageAudit(page_url=URL, candidates=[candidate])],
        target_brand="Norel",
        threshold=threshold,
        visited_pages={URL: CONTENT},
        strict_evidence=False,
    )


def _preuve_invalide():
    return SourceProof(
        url=URL,
        excerpt="Phrase absente de la page récupérée",
        type="web_officiel",
    )


def test_le_mode_strict_reste_le_defaut():
    """CAP-4 est opt-in : sans argument, le contrat historique s'applique."""
    candidate = _candidate(["proven"], proof=_preuve_invalide())

    with pytest.raises(EvidenceContractError, match="absent"):
        _evaluate(_requirements(1), candidate)


def test_une_preuve_invalide_ne_fait_plus_perdre_le_candidat():
    """CAP-4 : le candidat reste visible au lieu d'être supprimé en silence."""
    candidate = _candidate(["proven"], proof=_preuve_invalide())

    evaluations = _evaluate_souple(_requirements(1), candidate)

    assert len(evaluations) == 1
    assert evaluations[0].summary.reference == "REF-1"


def test_un_critere_inverifiable_est_nomme_dans_le_resume():
    """CAP-4 : la lacune est énoncée, pas devinée par l'absence de résultat."""
    candidate = _candidate(["proven"], proof=_preuve_invalide())

    evaluation = _evaluate_souple(_requirements(1), candidate)[0]

    assert evaluation.summary.unverified_criteria == ["Critère 1"]
    assert evaluation.summary.missing_audit_criteria == []
    assert evaluation.summary.rejected_proof_criteria == ["Critère 1"]
    assert evaluation.summary.proven_criteria == []


def test_un_critere_inverifiable_ne_compte_pas_comme_prouve():
    """CAP-4 ne doit pas gonfler le score : rétrograder n'est pas absoudre."""
    candidate = _candidate(["proven", "proven"], proof=_preuve_invalide())

    evaluation = _evaluate_souple(_requirements(2), candidate)[0]

    assert evaluation.summary.score == 0
    assert not evaluation.eligible


def test_une_incompatibilite_inverifiable_ne_bloque_plus():
    """Une incompatibilité que la page ne confirme pas n'est pas un verdict."""
    candidate = _candidate(["proven", "incompatible"], proof=_preuve_invalide())

    evaluation = _evaluate_souple(_requirements(2, critical={"r2"}), candidate)[0]

    assert evaluation.summary.critical_blockers == []
    assert evaluation.summary.incompatible_criteria == []
    assert evaluation.summary.unverified_criteria == ["Critère 1", "Critère 2"]


def test_une_preuve_verifiable_reste_prouvee_en_mode_souple():
    """Le mode souple ne dégrade pas ce qui se vérifie normalement."""
    evaluation = _evaluate_souple(_requirements(1), _candidate(["proven"]))[0]

    assert evaluation.summary.score == 100
    assert evaluation.summary.unverified_criteria == []
    assert evaluation.eligible


def test_un_audit_malforme_leve_meme_en_mode_souple():
    """CAP-4 assouplit la preuve, jamais le contrat : l'audit doit répondre à la question."""
    candidate = _candidate(["proven"])
    candidate.criteria[0].requested_value = "valeur réécrite par le modèle"

    with pytest.raises(CompatibilityContractError, match="valeur demandée"):
        _evaluate_souple(_requirements(1), candidate)


def test_new_scrapegraph_missions_request_facts_not_a_free_score():
    """Mutation détectée : ScrapeGraphAI recommence à inventer un pourcentage."""
    requirements = _requirements(1)

    criteria_prompt = construire_mission_criteres("Contacteur 9 A bobine 24 V DC")
    audit_prompt = construire_mission_audit(
        requirements,
        "Norel",
        URL,
    )

    assert "critères immuables" in criteria_prompt
    assert "proven" in audit_prompt
    assert "not_proven" in audit_prompt
    assert "incompatible" in audit_prompt
    assert URL in audit_prompt
    assert "pourcentage" not in audit_prompt.casefold()
    assert "pertinence" not in audit_prompt.casefold()


def test_origin_identity_is_non_applicable_but_family_and_usage_are_scored():
    requirements = RequirementSet(
        product="Kerion Electric NV1T05BD",
        origin_brand="Kerion Electric",
        criteria=[
            Requirement(
                id="manufacturer", label="Fabricant",
                requested_value="Kerion Electric", critical=True,
            ),
            Requirement(
                id="reference", label="Référence exacte",
                requested_value="NV1T05BD", critical=True,
            ),
            Requirement(
                id="family", label="Famille",
                requested_value="Contacteur", critical=True,
            ),
            Requirement(
                id="usage", label="Usage visé",
                requested_value="Commande moteur", critical=True,
            ),
        ],
    )
    proof = SourceProof(
        url=URL, excerpt="Tension de commande 24 V DC", type="web_officiel"
    )
    candidate = CandidateAudit(
        brand="Norel",
        reference="XZ07-20-10-11",
        criteria=[
            CriterionAudit(
                requirement_id="manufacturer",
                requested_value="Kerion Electric",
                observed_value="Norel",
                status="incompatible",
                proofs=[proof],
            ),
            CriterionAudit(
                requirement_id="reference",
                requested_value="NV1T05BD",
                observed_value="XZ07-20-10-11",
                status="incompatible",
                proofs=[proof],
            ),
            CriterionAudit(
                requirement_id="family",
                requested_value="Contacteur",
                observed_value="Contacteur",
                status="proven",
                proofs=[proof],
            ),
            CriterionAudit(
                requirement_id="usage",
                requested_value="Commande moteur",
                observed_value="Commande moteur",
                status="proven",
                proofs=[proof],
            ),
        ],
    )

    evaluation = _evaluate(
        requirements,
        candidate,
        pages={URL: f"{CONTENT} XZ07-20-10-11"},
    )

    assert evaluation.summary.score == 100
    assert evaluation.summary.non_applicable_criteria == [
        "Fabricant", "Référence exacte"
    ]
    assert evaluation.summary.proven_criteria == ["Famille", "Usage visé"]
    assert evaluation.summary.incompatible_criteria == []
    assert evaluation.summary.critical_blockers == []
    assert evaluation.complete


def test_origin_identity_value_is_non_applicable_regardless_of_label():
    """Une identité portée par la valeur ne dépend pas du libellé choisi par le LLM."""
    requirements = RequirementSet(
        product="Contacteur de puissance tripolaire NV1T05BD",
        origin_brand="Kerion Electric Tersa D",
        criteria=[
            Requirement(id="a", label="Origine A", requested_value="Kerion Electric"),
            Requirement(id="b", label="Origine B", requested_value="NV1T05BD"),
            Requirement(id="c", label="Origine C", requested_value="Tersa D"),
            Requirement(
                id="function",
                label="Fonction",
                requested_value="Contacteur de puissance tripolaire",
            ),
            Requirement(id="current", label="Courant", requested_value="9 A"),
            Requirement(id="coil", label="Bobine", requested_value="24 V DC"),
            Requirement(id="usage", label="Usage", requested_value="Commande moteur"),
        ],
    )
    proof = SourceProof(
        url=URL, excerpt="Tension de commande 24 V DC", type="web_officiel"
    )
    candidate = CandidateAudit(
        brand="Norel",
        reference="XZ07-20-10-11",
        criteria=[
            CriterionAudit(
                requirement_id=item.id,
                requested_value=item.requested_value,
                observed_value=(
                    "identité Norel" if item.id in {"a", "b", "c"} else item.requested_value
                ),
                status="incompatible" if item.id in {"a", "b", "c"} else "proven",
                proofs=[proof],
            )
            for item in requirements.criteria
        ],
    )

    evaluation = _evaluate(requirements, candidate)

    assert evaluation.summary.non_applicable_criteria == [
        "Origine A", "Origine B", "Origine C"
    ]
    assert evaluation.summary.proven_criteria == [
        "Fonction", "Courant", "Bobine", "Usage"
    ]
    assert evaluation.summary.score == 100


def test_more_than_half_origin_identity_values_reject_the_requirement_set():
    """Une majorité d'identité indique une extraction inutilisable, pas un bon score."""
    requirements = RequirementSet(
        product="Contacteur SRC-100",
        origin_brand="SourceCo AlphaLine",
        criteria=[
            Requirement(id="a", label="Origine A", requested_value="SourceCo"),
            Requirement(id="b", label="Origine B", requested_value="SRC-100"),
            Requirement(id="function", label="Fonction", requested_value="Contacteur"),
        ],
    )
    candidate = CandidateAudit(
        brand="Norel",
        reference="ALT-100",
        criteria=[
            CriterionAudit(
                requirement_id=item.id,
                requested_value=item.requested_value,
                status="not_proven",
            )
            for item in requirements.criteria
        ],
    )

    with pytest.raises(CompatibilityContractError, match="moitié"):
        _evaluate_souple(requirements, candidate)


def test_requirements_with_only_origin_identity_are_rejected():
    requirements = RequirementSet(
        product="Kerion Electric NV1T05BD",
        criteria=[
            Requirement(
                id="manufacturer", label="Fabricant",
                requested_value="Kerion Electric",
            ),
            Requirement(
                id="reference", label="Référence exacte",
                requested_value="NV1T05BD",
            ),
        ],
    )
    candidate = CandidateAudit(
        brand="Norel",
        reference="XZ07-20-10-11",
        criteria=[
            CriterionAudit(
                requirement_id="manufacturer",
                requested_value="Kerion Electric",
                status="not_proven",
            ),
            CriterionAudit(
                requirement_id="reference",
                requested_value="NV1T05BD",
                status="not_proven",
            ),
        ],
    )

    with pytest.raises(CompatibilityContractError, match="notable"):
        _evaluate_souple(requirements, candidate)


def _range_evaluation(
    observed_value: str,
    *,
    proofs: list[SourceProof] | None = None,
    requested_value: str = "24 V DC",
    status: str = "incompatible",
):
    requirements = RequirementSet(
        product="Contacteur",
        criteria=[Requirement(
            id="coil", label="Tension de bobine",
            requested_value=requested_value, critical=True,
        )],
    )
    candidate = CandidateAudit(
        brand="Norel",
        reference="XZ07-20-10-11",
        criteria=[CriterionAudit(
            requirement_id="coil",
            requested_value=requested_value,
            observed_value=observed_value,
            status=status,
            proofs=(
                proofs
                if proofs is not None
                else [SourceProof(url=URL, excerpt=observed_value, type="web_officiel")]
            ),
        )],
    )
    return evaluate_candidates(
        requirements,
        [PageAudit(page_url=URL, candidates=[candidate])],
        target_brand="Norel",
        threshold=75,
        visited_pages={
            URL: f"Norel XZ07-20-10-11. Bobine publiée : {observed_value}."
        },
    )[0]


def test_quantity_comparator_skips_an_unrelated_voltage_before_the_rated_current():
    evaluation = _range_evaluation(
        "Provozni proud AC-3 400V: 9 A",
        requested_value="9 A (AC-3)",
        status="proven",
    )

    assert evaluation.summary.score == 100
    assert evaluation.summary.proven_criteria == ["Tension de bobine"]
    assert evaluation.summary.incompatible_criteria == []


def test_quantity_comparator_accepts_dc_mode_written_before_the_value():
    evaluation = _range_evaluation(
        "Ridici napajeci napeti DC: 24 V",
        status="proven",
    )

    assert evaluation.summary.score == 100
    assert evaluation.summary.proven_criteria == ["Tension de bobine"]
    assert evaluation.summary.not_proven_criteria == []


@pytest.mark.parametrize(("requested", "observed"), [
    ("24 V AC", "Control voltage AC/DC: 24 V"),
    ("24 V DC", "Control voltage DC/AC: 24 V"),
])
def test_quantity_comparator_preserves_a_dual_mode_written_before_the_value(
    requested,
    observed,
):
    evaluation = _range_evaluation(
        observed,
        requested_value=requested,
        status="proven",
    )

    assert evaluation.summary.score == 100
    assert evaluation.summary.proven_criteria == ["Tension de bobine"]
    assert evaluation.summary.incompatible_criteria == []


@pytest.mark.parametrize(("requested", "observed"), [
    ("9 A", "9 apples available"),
    ("24 V", "24 versatile products"),
])
def test_quantity_comparator_does_not_read_a_word_prefix_as_a_unit(
    requested,
    observed,
):
    evaluation = _range_evaluation(
        observed,
        requested_value=requested,
        status="proven",
    )

    assert evaluation.summary.score == 0
    assert evaluation.summary.proven_criteria == []
    assert evaluation.summary.not_proven_criteria == ["Tension de bobine"]


def test_quantity_comparator_scans_many_ranges_and_points_in_linear_time():
    observed = " ".join(["1-2 V"] * 4_000 + ["3 V"] * 4_000)

    started = perf_counter()
    result = _range_contains_requested("99999 V", observed)
    elapsed = perf_counter() - started

    assert result is False
    assert elapsed < 1.0


def test_requested_unit_value_inside_published_range_becomes_proven():
    evaluation = _range_evaluation("20 ... 60 V DC")

    assert evaluation.summary.score == 100
    assert evaluation.summary.proven_criteria == ["Tension de bobine"]
    assert evaluation.summary.incompatible_criteria == []
    assert evaluation.summary.not_proven_criteria == []
    assert evaluation.complete


def test_published_range_with_different_mode_remains_incompatible():
    evaluation = _range_evaluation("24-60 V AC")

    assert evaluation.summary.score == 0
    assert evaluation.summary.incompatible_criteria == ["Tension de bobine"]
    assert evaluation.summary.critical_blockers == ["Tension de bobine"]


def test_unparseable_unit_range_is_downgraded_to_not_proven():
    evaluation = _range_evaluation("plage étendue non chiffrée")

    assert evaluation.summary.score == 0
    assert evaluation.summary.not_proven_criteria == ["Tension de bobine"]
    assert evaluation.summary.incompatible_criteria == []
    assert evaluation.summary.critical_blockers == []


def test_incompatible_without_observed_value_survives_as_not_proven():
    evaluation = _range_evaluation("", proofs=[])

    assert evaluation.summary.not_proven_criteria == ["Tension de bobine"]
    assert evaluation.summary.incompatible_criteria == []


def test_range_cannot_become_proven_without_literal_evidence():
    proof = SourceProof(
        url=URL,
        excerpt="20 ... 60 V DC",
        type="web_officiel",
    )
    requirements = RequirementSet(
        product="Contacteur",
        criteria=[Requirement(
            id="coil", label="Tension de bobine",
            requested_value="24 V DC", critical=True,
        )],
    )
    candidate = CandidateAudit(
        brand="Norel",
        reference="XZ07-20-10-11",
        criteria=[CriterionAudit(
            requirement_id="coil",
            requested_value="24 V DC",
            observed_value="20 ... 60 V DC",
            status="incompatible",
            proofs=[proof],
        )],
    )

    with pytest.raises(EvidenceContractError, match="absent"):
        evaluate_candidates(
            requirements,
            [PageAudit(page_url=URL, candidates=[candidate])],
            target_brand="Norel",
            threshold=75,
            visited_pages={URL: "Page sans la plage annoncée."},
        )


def test_audit_prompt_defines_range_semantics_and_requires_observed_value():
    prompt = construire_mission_audit(_requirements(1), "Norel", URL)

    assert "comprise dans une plage" in prompt
    assert "exclut la valeur demandée" in prompt
    assert "`observed_value` non vide" in prompt


def _windowed_evaluation(
    excerpt: str,
    content: str,
    *,
    observed_value: str = "24...60 V DC",
    status: str = "proven",
):
    requirements = RequirementSet(
        product="Contacteur",
        criteria=[Requirement(
            id="coil",
            label="Tension de bobine",
            requested_value="24 V DC",
            critical=True,
        )],
    )
    candidate = CandidateAudit(
        brand="Norel",
        reference="XZ07-20-10-11",
        criteria=[CriterionAudit(
            requirement_id="coil",
            requested_value="24 V DC",
            observed_value=observed_value,
            status=status,
            proofs=[SourceProof(url=URL, excerpt=excerpt, type="web_officiel")],
        )],
    )
    return evaluate_candidates(
        requirements,
        [PageAudit(page_url=URL, candidates=[candidate])],
        target_brand="Norel",
        threshold=75,
        visited_pages={URL: content},
        strict_evidence=False,
    )[0]


def test_non_contiguous_tokens_inside_window_are_proven_and_graded_windowed():
    """Une ligne de tableau aplatie reste une preuve bornée, jamais une citation exacte."""
    evaluation = _windowed_evaluation(
        "XZ07-20-10-11 24...60 20...60 3 0 1 0",
        "Table Norel : XZ07-20-10-11 | 24...60 | 20...60 | 3 | 0 | 1 | 0",
    )

    assert evaluation.summary.score == 100
    assert evaluation.summary.proven_criteria == ["Tension de bobine"]
    assert evaluation.summary.rejected_proof_criteria == []
    assert evaluation.summary.proof_grades == {"Tension de bobine": "windowed"}
    assert evaluation.complete


def test_contiguous_excerpt_keeps_the_stronger_grade():
    evaluation = _evaluate_souple(_requirements(1), _candidate(["proven"]))[0]

    assert evaluation.summary.proof_grades == {"Critère 1": "contiguous"}


def test_windowed_excerpt_requires_a_discriminating_token():
    evaluation = _windowed_evaluation("ac dc v", "ac / dc / v")

    assert evaluation.summary.score == 0
    assert evaluation.summary.rejected_proof_criteria == ["Tension de bobine"]
    assert evaluation.summary.proof_grades == {}


def test_windowed_excerpt_rejects_tokens_spread_beyond_300_characters():
    evaluation = _windowed_evaluation(
        "XZ07-20-10-11 24",
        "XZ07-20-10-11 " + ("x " * 151) + "24",
    )

    assert evaluation.summary.score == 0
    assert evaluation.summary.rejected_proof_criteria == ["Tension de bobine"]


def test_windowed_excerpt_rejects_a_single_token_larger_than_the_window():
    long_token = "a" * 301
    evaluation = _windowed_evaluation(
        f"{long_token} 24",
        f"{long_token} | valeur absente",
    )

    assert evaluation.summary.score == 0
    assert evaluation.summary.rejected_proof_criteria == ["Tension de bobine"]


def test_windowed_excerpt_requires_repeated_tokens_to_repeat_in_content():
    evaluation = _windowed_evaluation(
        "XZ07-20-10-11 24 24",
        "XZ07-20-10-11 | 24",
    )

    assert evaluation.summary.score == 0
    assert evaluation.summary.rejected_proof_criteria == ["Tension de bobine"]


def test_windowed_proof_does_not_override_ac_dc_mismatch():
    evaluation = _windowed_evaluation(
        "XZ07-20-10-11 24 60",
        "XZ07-20-10-11 | 24 | 60",
        observed_value="24...60 V AC",
        status="incompatible",
    )

    assert evaluation.summary.score == 0
    assert evaluation.summary.incompatible_criteria == ["Tension de bobine"]
    assert evaluation.summary.critical_blockers == ["Tension de bobine"]
    assert evaluation.summary.proof_grades == {"Tension de bobine": "windowed"}


def test_model_proven_status_cannot_override_ac_dc_mismatch():
    evaluation = _windowed_evaluation(
        "XZ07-20-10-11 24 60",
        "XZ07-20-10-11 | 24 | 60",
        observed_value="24...60 V AC",
        status="proven",
    )

    assert evaluation.summary.score == 0
    assert evaluation.summary.incompatible_criteria == ["Tension de bobine"]
    assert evaluation.summary.critical_blockers == ["Tension de bobine"]
    assert not evaluation.eligible


def test_cached_reaudit_requires_the_candidate_identity_on_an_official_page():
    """Une preuve officielle generique ne confirme pas REF-900."""
    official_url = "https://norelab.example/products/ref-900"
    secondary_url = "https://distributor.example/products/ref-900"
    requirements = RequirementSet(
        product="Source SRC-1",
        origin_brand="SourceCo",
        criteria=[
            Requirement(
                id="manufacturer",
                label="Fabricant",
                requested_value="SourceCo",
            ),
            Requirement(
                id="coil",
                label="Tension de bobine",
                requested_value="24 V DC",
            ),
        ],
    )
    candidate = CandidateAudit(
        brand="Norel",
        reference="REF-900",
        criteria=[
            CriterionAudit(
                requirement_id="manufacturer",
                requested_value="SourceCo",
                observed_value="Norel",
                status="incompatible",
                proofs=[SourceProof(
                    url=official_url,
                    excerpt="Norel technical documentation",
                    type="web_officiel",
                )],
            ),
            CriterionAudit(
                requirement_id="coil",
                requested_value="24 V DC",
                status="not_proven",
            ),
        ],
    )
    audit = PageAudit(page_url=official_url, candidates=[candidate])

    enriched = reaudit_cached_product_evidence(
        requirements,
        audit,
        {
            official_url: "Norel technical documentation without product identity",
            secondary_url: "Norel REF-900 coil 24VDC",
        },
    )

    assert next(
        item for item in enriched.candidates[0].criteria
        if item.requirement_id == "coil"
    ).status == "not_proven"
    assert enriched.candidates[0].criteria[0].proofs[0].type == "web_secondaire"


def test_cached_reaudit_never_treats_a_technical_value_as_product_identity():
    """`24 V DC` commun a deux produits ne relie jamais leurs preuves."""
    official_url = "https://new.norel.example/products/technical-data"
    unrelated_url = "https://distributor.example/products/unrelated-device"
    requirements = RequirementSet(
        product="Source SRC-1",
        origin_brand="SourceCo",
        criteria=[
            Requirement(
                id="coil", label="Tension de bobine", requested_value="24 V DC",
            ),
            Requirement(
                id="current", label="Courant nominal", requested_value="9 A",
            ),
        ],
    )
    candidate = CandidateAudit(
        brand="Norel",
        reference="REF-123",
        criteria=[
            CriterionAudit(
                requirement_id="coil",
                requested_value="24 V DC",
                observed_value="24 V DC",
                status="proven",
                proofs=[SourceProof(
                    url=official_url,
                    excerpt="Norel technical data 24 V DC",
                    type="web_officiel",
                )],
            ),
            CriterionAudit(
                requirement_id="current",
                requested_value="9 A",
                status="not_proven",
            ),
        ],
    )
    audit = PageAudit(page_url=official_url, candidates=[candidate])

    enriched = reaudit_cached_product_evidence(
        requirements,
        audit,
        {
            official_url: "Norel technical data 24 V DC",
            unrelated_url: "Unrelated device rated 24 V DC and 9 A",
        },
    )

    criteria = {item.requirement_id: item for item in enriched.candidates[0].criteria}
    assert criteria["current"].status == "not_proven"
    assert criteria["coil"].proofs[0].type == "web_secondaire"


def test_cached_reaudit_never_treats_an_operating_range_as_a_product_family():
    official_url = "https://new.norel.example/products/ref-123"
    secondary_url = "https://distributor.example/products/unrelated"
    requirements = RequirementSet(
        product="Source SRC-1",
        criteria=[
            Requirement(
                id="operating_range",
                label="Operating range",
                requested_value="24 V DC",
            ),
            Requirement(id="current", label="Courant nominal", requested_value="9 A"),
        ],
    )
    candidate = CandidateAudit(
        brand="Norel",
        reference="REF-123",
        criteria=[
            CriterionAudit(
                requirement_id="operating_range",
                requested_value="24 V DC",
                observed_value="24 V DC",
                status="proven",
                proofs=[SourceProof(
                    url=official_url,
                    excerpt="Norel REF-123 operating range 24 V DC",
                    type="web_officiel",
                )],
            ),
            CriterionAudit(
                requirement_id="current", requested_value="9 A", status="not_proven",
            ),
        ],
    )
    audit = PageAudit(page_url=official_url, candidates=[candidate])

    enriched = reaudit_cached_product_evidence(
        requirements,
        audit,
        {
            official_url: "Norel REF-123 operating range 24 V DC",
            secondary_url: "Unrelated device 24 V DC rated 9 A",
        },
    )

    assert enriched == audit


def test_cached_reaudit_ignores_an_unvisited_official_proof_url_alias():
    """Une URL fabricant hallucinee ne peut pas semer une fausse famille."""
    official_url = "https://new.norel.example/products/ref-123"
    unvisited_proof_url = "https://new.norel.example/products/unrelated-999"
    secondary_url = "https://distributor.example/products/unrelated-999"
    requirements = RequirementSet(
        product="Source SRC-1",
        origin_brand="SourceCo",
        criteria=[
            Requirement(id="function", label="Fonction", requested_value="contactor"),
            Requirement(id="current", label="Courant nominal", requested_value="9 A"),
        ],
    )
    candidate = CandidateAudit(
        brand="Norel",
        reference="REF-123",
        criteria=[
            CriterionAudit(
                requirement_id="function",
                requested_value="contactor",
                observed_value="contactor",
                status="proven",
                proofs=[
                    SourceProof(
                        url=official_url,
                        excerpt="Norel REF-123 contactor",
                        type="web_officiel",
                    ),
                    SourceProof(
                        url=unvisited_proof_url,
                        excerpt="invented proof",
                        type="web_officiel",
                    ),
                ],
            ),
            CriterionAudit(
                requirement_id="current",
                requested_value="9 A",
                status="not_proven",
            ),
        ],
    )
    audit = PageAudit(page_url=official_url, candidates=[candidate])

    enriched = reaudit_cached_product_evidence(
        requirements,
        audit,
        {
            official_url: "Norel REF-123 contactor technical data",
            secondary_url: "UNRELATED-999 product rated 9 A",
        },
    )

    enriched_candidate = enriched.candidates[0]
    assert next(
        item for item in enriched_candidate.criteria
        if item.requirement_id == "current"
    ).status == "not_proven"
    assert [proof.type for proof in enriched_candidate.criteria[0].proofs] == [
        "web_officiel", "web_secondaire",
    ]


def test_cached_reaudit_accepts_a_known_order_code_from_the_visited_official_page():
    """Un code commande officiel peut rattacher une page secondaire Norel."""
    official_url = "https://new.norel.example/products/ref-123/bsl07-20-10-81"
    secondary_url = "https://distributor.example/products/bsl07"
    requirements = RequirementSet(
        product="Source SRC-1",
        origin_brand="SourceCo",
        criteria=[
            Requirement(id="family", label="Famille", requested_value="SourceFamily"),
            Requirement(id="current", label="Courant nominal", requested_value="9 A"),
        ],
    )
    candidate = CandidateAudit(
        brand="Norel",
        reference="REF-123",
        criteria=[
            CriterionAudit(
                requirement_id="family",
                requested_value="SourceFamily",
                observed_value="BSL07",
                status="incompatible",
                proofs=[SourceProof(
                    url=official_url,
                    excerpt="Norel REF-123 family BSL07-20-10-81",
                    type="web_officiel",
                )],
            ),
            CriterionAudit(
                requirement_id="current",
                requested_value="9 A",
                status="not_proven",
            ),
        ],
    )
    audit = PageAudit(page_url=official_url, candidates=[candidate])

    enriched = reaudit_cached_product_evidence(
        requirements,
        audit,
        {
            official_url: "Norel REF-123 family BSL07-20-10-81",
            secondary_url: "Norel BSL07-20-10-81 product rated 9 A",
        },
    )

    current = next(
        item for item in enriched.candidates[0].criteria
        if item.requirement_id == "current"
    )
    assert current.status == "proven"
    assert [proof.url for proof in current.proofs] == [secondary_url]


def test_cached_reaudit_scans_a_later_identity_occurrence_on_the_same_page():
    official_url = "https://new.norel.example/products/ref-123"
    secondary_url = "https://distributor.example/products/ref-123"
    requirements = RequirementSet(
        product="Source SRC-1",
        criteria=[Requirement(
            id="current", label="Courant nominal", requested_value="9 A",
        )],
    )
    candidate = CandidateAudit(
        brand="Norel",
        reference="REF-123",
        criteria=[CriterionAudit(
            requirement_id="current",
            requested_value="9 A",
            status="not_proven",
            proofs=[SourceProof(
                url=official_url,
                excerpt="Norel REF-123",
                type="web_officiel",
            )],
        )],
    )
    audit = PageAudit(page_url=official_url, candidates=[candidate])

    enriched = reaudit_cached_product_evidence(
        requirements,
        audit,
        {
            official_url: "Norel REF-123",
            secondary_url: "Norel REF-123 " + ("padding " * 150) + "REF-123 rated 9 A",
        },
    )

    assert enriched.candidates[0].criteria[0].status == "proven"


def test_cached_reaudit_uses_the_minimum_useful_secondary_source_set():
    """B+C couvrent les six criteres : la page A, pourtant plus riche, est inutile."""
    official_url = "https://new.norel.example/products/ref-123"
    page_a = "https://distributor.example/a"
    page_b = "https://distributor.example/b"
    page_c = "https://distributor.example/c"
    requirements = RequirementSet(
        product="Source SRC-1",
        origin_brand="SourceCo",
        criteria=[
            Requirement(id=f"x{index}", label=f"X{index}", requested_value=f"VALUE-X{index}")
            for index in range(1, 7)
        ],
    )
    candidate = CandidateAudit(
        brand="Norel",
        reference="REF-123",
        criteria=[
            CriterionAudit(
                requirement_id=f"x{index}",
                requested_value=f"VALUE-X{index}",
                status="not_proven",
            )
            for index in range(1, 7)
        ],
    )
    # Une preuve officielle quelconque porte l'identite et permet de confirmer
    # que la page Norel correspond bien au candidat avant le re-audit du cache.
    candidate.criteria[0] = CriterionAudit(
        requirement_id="x1",
        requested_value="VALUE-X1",
        observed_value="VALUE-X1",
        status="proven",
        proofs=[SourceProof(
            url=official_url,
            excerpt="Norel REF-123 VALUE-X1",
            type="web_officiel",
        )],
    )
    audit = PageAudit(page_url=official_url, candidates=[candidate])

    enriched = reaudit_cached_product_evidence(
        requirements,
        audit,
        {
            official_url: "Norel REF-123 VALUE-X1",
            page_a: "Norel REF-123 VALUE-X1 VALUE-X2 VALUE-X3 VALUE-X4",
            page_b: "Norel REF-123 VALUE-X1 VALUE-X2 VALUE-X5",
            page_c: "Norel REF-123 VALUE-X3 VALUE-X4 VALUE-X6",
        },
    )

    secondary_urls = {
        proof.url
        for criterion in enriched.candidates[0].criteria
        for proof in criterion.proofs
        if proof.type == "web_secondaire"
    }
    assert secondary_urls == {page_b, page_c}


def test_cached_official_page_confirms_and_enriches_a_secondary_candidate_without_prior_official_proof():
    """Une fiche Norel déjà en cache brise la circularité des preuves officielles."""
    official_url = "https://empower.norel.example/ecatalog/ec/FR_CA/p/4KBL136001R3001"
    secondary_url = "https://www.revendeur-b.example/xz07z-20-01-30-24-v-dc"
    requirements = RequirementSet(
        product="Contacteur source",
        origin_brand="Kerion Electric",
        criteria=[
            Requirement(id="current", label="Courant nominal", requested_value="9 A (AC-3)"),
            Requirement(id="coil", label="Tension de bobine", requested_value="24 V DC"),
        ],
    )
    candidate = CandidateAudit(
        brand="Norel",
        reference="4KBL136001R3001",
        criteria=[
            CriterionAudit(
                requirement_id="current",
                requested_value="9 A (AC-3)",
                observed_value="12 A (AC-3)",
                status="incompatible",
                proofs=[SourceProof(
                    url=secondary_url,
                    excerpt="4KBL136001R3001 12 A (AC-3)",
                    # Le modèle peut mal étiqueter un distributeur : la
                    # hiérarchie doit être recalculée depuis l'URL visitée.
                    type="web_officiel",
                )],
            ),
            CriterionAudit(
                requirement_id="coil",
                requested_value="24 V DC",
                status="not_proven",
            ),
        ],
    )
    audit = PageAudit(page_url=secondary_url, candidates=[candidate])
    pages = {
        secondary_url: "Norel 4KBL136001R3001 12 A (AC-3)",
        official_url: (
            "Norel 4KBL136001R3001 XZ07Z-20-01-30. "
            "Rated Operational Power AC-3 2.2 kW. "
            + ("technical details " * 80)
            +
            "Rated Operational Current AC-3 (220 / 230 / 240 V) 60 C 9 A. "
            "Tension de bobine 24 V DC."
        ),
    }

    assert _official_page_confirms_identity(candidate, pages)

    downgraded = reaudit_cached_product_evidence(requirements, audit, pages)
    criteria_after_downgrade = {
        item.requirement_id: item
        for item in downgraded.candidates[0].criteria
    }

    assert cached_reaudit_downgraded_criteria(
        requirements, audit, downgraded,
    ) == ("Courant nominal",)
    # Une incompatibilité secondaire n'est jamais promue directement : le
    # passage officiel la rend seulement ré-auditable.
    assert criteria_after_downgrade["current"].status == "not_proven"

    enriched = reaudit_cached_product_evidence(requirements, downgraded, pages)
    criteria = {item.requirement_id: item for item in enriched.candidates[0].criteria}
    from rejeu import candidate_evaluation_diagnostic
    evaluation = evaluate_candidates(
        requirements,
        [enriched],
        "Norel",
        75,
        pages,
        strict_evidence=False,
    )[0]
    assert candidate_evaluation_diagnostic(
        evaluation,
        wave=1,
        url=secondary_url,
        downgraded_by_official_source=("Courant nominal",),
    )["downgraded_by_official_source"] == ["Courant nominal"]
    assert criteria["current"].status == "proven"
    assert criteria["coil"].status == "proven"
    for criterion in (criteria["current"], criteria["coil"]):
        assert any(
            proof.url == official_url and proof.type == "web_officiel"
            for proof in criterion.proofs
        )


def test_cached_official_reaudit_keeps_a_secondary_incompatibility_without_the_official_value():
    """Une simple fiche fabricant ne suffit jamais à effacer un conflit prouvé."""
    official_url = "https://new.norel.example/products/ref-123"
    secondary_url = "https://distributor.example/ref-123"
    requirements = RequirementSet(
        product="Contacteur source",
        criteria=[Requirement(
            id="coil", label="Tension de bobine", requested_value="24 V DC",
        )],
    )
    candidate = CandidateAudit(
        brand="Norel",
        reference="REF-123",
        criteria=[CriterionAudit(
            requirement_id="coil",
            requested_value="24 V DC",
            observed_value="230 V AC",
            status="incompatible",
            proofs=[SourceProof(
                url=secondary_url,
                excerpt="Norel REF-123 coil 230 V AC",
                type="web_secondaire",
            )],
        )],
    )
    audit = PageAudit(page_url=secondary_url, candidates=[candidate])
    pages = {
        official_url: "Norel REF-123 product overview.",
        secondary_url: "Norel REF-123 coil 230 V AC.",
    }

    enriched = reaudit_cached_product_evidence(requirements, audit, pages)

    assert enriched == audit
    assert cached_reaudit_downgraded_criteria(requirements, audit, enriched) == ()


def test_cached_reaudit_never_trusts_a_model_official_label_for_a_secondary_url():
    """Le domaine réel, pas le champ LLM, détermine le type de preuve."""
    official_url = "https://new.norel.example/products/ref-123"
    secondary_url = "https://distributor.example/ref-123"
    requirements = RequirementSet(
        product="Contacteur source",
        criteria=[Requirement(
            id="current", label="Courant nominal", requested_value="9 A",
        )],
    )
    candidate = CandidateAudit(
        brand="Norel",
        reference="REF-123",
        criteria=[CriterionAudit(
            requirement_id="current",
            requested_value="9 A",
            observed_value="9 A",
            status="proven",
            proofs=[SourceProof(
                url=secondary_url,
                excerpt="Norel REF-123 9 A",
                type="web_officiel",
            )],
        )],
    )
    audit = PageAudit(page_url=secondary_url, candidates=[candidate])

    enriched = reaudit_cached_product_evidence(
        requirements,
        audit,
        {
            official_url: "Norel REF-123 product overview.",
            secondary_url: "Norel REF-123 9 A.",
        },
    )

    proof = enriched.candidates[0].criteria[0].proofs[0]
    assert proof.type == "web_secondaire"


def test_cached_reaudit_reclassifies_a_mislabeled_secondary_proof_without_any_official_page():
    """Même sans enrichissement possible, le diagnostic ne doit pas mentir."""
    secondary_url = "https://distributor.example/ref-123"
    requirements = RequirementSet(
        product="Contacteur source",
        criteria=[Requirement(
            id="current", label="Courant nominal", requested_value="9 A",
        )],
    )
    candidate = CandidateAudit(
        brand="Norel",
        reference="REF-123",
        criteria=[CriterionAudit(
            requirement_id="current",
            requested_value="9 A",
            observed_value="9 A",
            status="proven",
            proofs=[SourceProof(
                url=secondary_url,
                excerpt="Norel REF-123 9 A",
                type="web_officiel",
            )],
        )],
    )
    audit = PageAudit(page_url=secondary_url, candidates=[candidate])

    enriched = reaudit_cached_product_evidence(
        requirements,
        audit,
        {secondary_url: "Norel REF-123 9 A."},
    )

    assert enriched.candidates[0].criteria[0].proofs[0].type == "web_secondaire"


def test_cached_official_reaudit_never_uses_a_prefix_from_another_product():
    """La fenêtre Norel commence autour de la référence, jamais au début du catalogue."""
    official_url = "https://new.norel.example/products/ref-123"
    requirements = RequirementSet(
        product="Contacteur source",
        criteria=[Requirement(
            id="coil", label="Tension de bobine", requested_value="24 V DC",
        )],
    )
    candidate = CandidateAudit(
        brand="Norel",
        reference="REF-123",
        criteria=[CriterionAudit(
            requirement_id="coil",
            requested_value="24 V DC",
            status="not_proven",
        )],
    )
    audit = PageAudit(page_url=official_url, candidates=[candidate])
    pages = {
        official_url: (
            "Norel other model coil 24 V DC. "
            + ("catalogue navigation " * 700)
            + "Norel REF-123 technical overview without coil data."
        ),
    }

    enriched = reaudit_cached_product_evidence(requirements, audit, pages)

    assert enriched == audit


def test_cached_reaudit_does_not_borrow_a_coil_value_from_the_next_product():
    """Une variante suivante ne peut pas prouver la bobine du candidat courant."""
    official_url = "https://new.norel.example/products/ref-a"
    requirements = RequirementSet(
        product="Contacteur source",
        criteria=[Requirement(
            id="coil", label="Tension de bobine", requested_value="24 V DC",
        )],
    )
    candidate = CandidateAudit(
        brand="Norel",
        reference="REF-A",
        criteria=[CriterionAudit(
            requirement_id="coil",
            requested_value="24 V DC",
            status="not_proven",
        )],
    )
    audit = PageAudit(page_url=official_url, candidates=[candidate])
    pages = {
        official_url: (
            "Norel REF-A coil 230 V AC. "
            "Related product Norel REF-B coil 24 V DC."
        ),
    }

    assert reaudit_cached_product_evidence(requirements, audit, pages) == audit


def test_cached_reaudit_does_not_borrow_a_coil_value_from_an_unlabelled_next_reference():
    """Une ligne de tableau Norel qui change de référence borne aussi la preuve."""
    official_url = "https://new.norel.example/products/ref-a"
    requirements = RequirementSet(
        product="Contacteur source",
        criteria=[Requirement(
            id="coil", label="Tension de bobine", requested_value="24 V DC",
        )],
    )
    candidate = CandidateAudit(
        brand="Norel",
        reference="REF-A",
        criteria=[CriterionAudit(
            requirement_id="coil",
            requested_value="24 V DC",
            status="not_proven",
        )],
    )
    audit = PageAudit(page_url=official_url, candidates=[candidate])
    pages = {
        official_url: "Norel REF-A coil 230 V AC. Norel REF-B coil 24 V DC.",
    }

    assert reaudit_cached_product_evidence(requirements, audit, pages) == audit


def test_cached_reaudit_stops_at_a_branded_next_reference_on_a_new_table_line():
    official_url = "https://new.norel.example/products/ref-a"
    requirement = Requirement(
        id="coil", label="Tension de bobine", requested_value="24 V DC",
    )
    candidate = CandidateAudit(
        brand="Norel",
        reference="REF-A",
        criteria=[CriterionAudit(
            requirement_id="coil", requested_value="24 V DC", status="not_proven",
        )],
    )
    audit = PageAudit(page_url=official_url, candidates=[candidate])

    assert reaudit_cached_product_evidence(
        RequirementSet(product="Contacteur source", criteria=[requirement]),
        audit,
        {official_url: "Norel REF-A coil 230 V AC\nNorel REF-B coil 24 V DC."},
    ) == audit


@pytest.mark.parametrize("standard", ["IEC60947", "UL508", "VDE0660", "GB14048", "CSA22"])
def test_cached_reaudit_keeps_specs_after_a_technical_standard(standard):
    """Une norme n'est pas une référence produit et ne borne pas sa fiche."""
    official_url = "https://new.norel.example/products/ref-a"
    requirement = Requirement(
        id="coil", label="Tension de bobine", requested_value="24 V DC",
    )
    candidate = CandidateAudit(
        brand="Norel",
        reference="REF-A",
        criteria=[CriterionAudit(
            requirement_id="coil", requested_value="24 V DC", status="not_proven",
        )],
    )
    audit = PageAudit(page_url=official_url, candidates=[candidate])

    enriched = reaudit_cached_product_evidence(
        RequirementSet(product="Contacteur source", criteria=[requirement]),
        audit,
        {official_url: f"Norel REF-A. Norel {standard} compliant. Coil 24 V DC."},
    )

    assert enriched.candidates[0].criteria[0].status == "proven"


@pytest.mark.parametrize("other_reference", ["BSL07", "XZ07", "KB6"])
def test_cached_reaudit_stops_at_a_branded_short_norel_reference(other_reference):
    """Une famille Norel courte après une seconde marque est une autre fiche."""
    official_url = "https://new.norel.example/products/ref-a"
    requirement = Requirement(
        id="coil", label="Tension de bobine", requested_value="24 V DC",
    )
    candidate = CandidateAudit(
        brand="Norel",
        reference="REF-A",
        criteria=[CriterionAudit(
            requirement_id="coil", requested_value="24 V DC", status="not_proven",
        )],
    )
    audit = PageAudit(page_url=official_url, candidates=[candidate])

    assert reaudit_cached_product_evidence(
        RequirementSet(product="Contacteur source", criteria=[requirement]),
        audit,
        {official_url: (
            f"Norel REF-A coil 230 V AC. Norel {other_reference} coil 24 V DC."
        )},
    ) == audit


def test_cached_reaudit_does_not_reopen_a_next_variant_through_a_family_alias():
    """Une famille partagée n'ouvre jamais une fenêtre autonome d'une autre référence."""
    official_url = "https://new.norel.example/products/ref-a"
    requirements = RequirementSet(
        product="Contacteur source",
        criteria=[
            Requirement(id="family", label="Famille", requested_value="Source family"),
            Requirement(id="coil", label="Tension de bobine", requested_value="24 V DC"),
        ],
    )
    candidate = CandidateAudit(
        brand="Norel",
        reference="REF-A",
        criteria=[
            CriterionAudit(
                requirement_id="family",
                requested_value="Source family",
                observed_value="BSL07",
                status="incompatible",
                proofs=[SourceProof(
                    url=official_url,
                    excerpt="Norel REF-A family BSL07",
                    type="web_officiel",
                )],
            ),
            CriterionAudit(
                requirement_id="coil",
                requested_value="24 V DC",
                status="not_proven",
            ),
        ],
    )
    audit = PageAudit(page_url=official_url, candidates=[candidate])
    pages = {
        official_url: (
            "Norel REF-A family BSL07 coil 230 V AC. "
            "Norel REF-B family BSL07 coil 24 V DC."
        ),
    }

    assert reaudit_cached_product_evidence(requirements, audit, pages) == audit


def test_cached_reaudit_rejects_a_competitor_page_that_only_mentions_the_candidate_as_alternative():
    """Une citation comparative Norel ne transforme pas le produit concurrent en preuve."""
    official_url = "https://new.norel.example/products/ref-123"
    competitor_url = "https://competitor.example/products/other-9a"
    requirements = RequirementSet(
        product="Contacteur source",
        criteria=[Requirement(
            id="current", label="Courant nominal", requested_value="9 A (AC-3)",
        )],
    )
    candidate = CandidateAudit(
        brand="Norel",
        reference="REF-123",
        criteria=[CriterionAudit(
            requirement_id="current",
            requested_value="9 A (AC-3)",
            status="not_proven",
        )],
    )
    audit = PageAudit(page_url=official_url, candidates=[candidate])
    pages = {
        official_url: "Norel REF-123 product overview.",
        competitor_url: (
            "Competitor product, alternative to Norel REF-123. "
            "Rated Operational Current AC-3 9 A."
        ),
    }

    assert reaudit_cached_product_evidence(requirements, audit, pages) == audit


def test_nominal_current_evidence_does_not_cross_into_the_next_product_section():
    from compatibilite import _nominal_current_evidence

    requirement = Requirement(
        id="current", label="Courant nominal", requested_value="9 A (AC-3)",
    )
    window = (
        "Rated Operational Current AC-3 for REF-A: 12 A. "
        "Related model REF-B. Rated Operational Current AC-3 for REF-B: 9 A."
    )

    assert _nominal_current_evidence(
        requirement, window, candidate_reference="REF-A",
    ) == ""


def test_main_no_contact_evidence_never_borrows_no_from_an_auxiliary_contact():
    from compatibilite import _contact_no_evidence

    requirement = Requirement(
        id="contacts", label="Contacts principaux", requested_value="3 NO",
    )

    assert _contact_no_evidence(
        requirement,
        "REF-123. 1 contact normally open auxiliary. contacts main: 3.",
    ) == ""


def test_main_no_contact_evidence_accepts_the_explicit_norel_wording():
    from compatibilite import _contact_no_evidence

    requirement = Requirement(
        id="contacts", label="Contacts principaux", requested_value="3 NO",
    )
    wording = (
        "Nombre de contacts a fermeture en tant que contacts principaux: 3."
    )

    assert _contact_no_evidence(requirement, wording)


def test_main_no_contact_evidence_skips_auxiliary_clause_before_norel_wording():
    from compatibilite import _contact_no_evidence

    requirement = Requirement(
        id="contacts", label="Contacts principaux", requested_value="3 NO",
    )
    wording = (
        "nombre de contacts auxiliaires a fermeture: 0\n"
        "nombre de contacts a fermeture en tant que contacts principaux: 3"
    )

    evidence = _contact_no_evidence(requirement, wording)
    assert evidence
    assert "auxiliaires" not in evidence


def _page_de_rayon(lignes: int = 30) -> str:
    """Une page de categorie : beaucoup d'identites, aucune fiche."""
    voisins = " ".join(
        f"Ministykac 200-K{index:02d}ZL01M 24V DC" for index in range(1, lignes)
    )
    return f"Stykace {voisins} Mini contacteur HPL1211001R0101 Norel"


def test_a_listing_row_cannot_prove_a_criterion_for_the_neighbouring_product():
    """Rupture visée : un 24 V AC vendu comme 24 V DC par la ligne d'à côté.

    Mesure du 2026-08-27 sur un corpus de rejeu : l'extrait
    `Ministykac 200-K09ZL01M 24V DC`, lu sur une page de catégorie, prouvait
    « Tension de bobine = 24 V DC » pour `HPL1211001R0101` — un contacteur dont
    la bobine est en 24 V AC. L'extrait décrivait le produit voisin.
    """
    url = "https://distributeur.example/stykace-c663/"
    contenu = _page_de_rayon()
    requirements = RequirementSet(
        product="Kerion NV1T05BD",
        criteria=[Requirement(
            id="coil",
            label="Tension de bobine",
            requested_value="24 V DC",
        )],
    )
    audit = PageAudit(page_url=url, candidates=[CandidateAudit(
        brand="Norel",
        reference="HPL1211001R0101",
        criteria=[CriterionAudit(
            requirement_id="coil",
            requested_value="24 V DC",
            observed_value="24 V DC",
            status="proven",
            proofs=[SourceProof(
                url=url,
                excerpt="Ministykac 200-K09ZL01M 24V DC",
                type="web_secondaire",
            )],
        )],
    )])

    evaluations = evaluate_candidates(
        requirements, [audit], "Norel", 75, {url: contenu}, strict_evidence=False,
    )

    assert [item.summary.proven_criteria for item in evaluations] == [[]]
    assert evaluations[0].summary.score == 0


def test_a_product_page_still_proves_a_criterion_without_repeating_its_reference():
    """La garde de rayon ne doit pas condamner une fiche produit ordinaire.

    Sur une page qui décrit un seul produit, c'est la page qui porte
    l'identité : un extrait contextuel reste une preuve valide, et le doit.
    """
    url = "https://distributeur.example/p/hpl1211001r0101"
    contenu = (
        "Mini contacteur HPL1211001R0101 Norel. "
        "Ridici napaj. napeti DC: 24 V. Trois poles."
    )
    requirements = RequirementSet(
        product="Kerion NV1T05BD",
        criteria=[Requirement(
            id="coil",
            label="Tension de bobine",
            requested_value="24 V DC",
        )],
    )
    audit = PageAudit(page_url=url, candidates=[CandidateAudit(
        brand="Norel",
        reference="HPL1211001R0101",
        criteria=[CriterionAudit(
            requirement_id="coil",
            requested_value="24 V DC",
            observed_value="24 V DC",
            status="proven",
            proofs=[SourceProof(
                url=url,
                excerpt="Ridici napaj. napeti DC: 24 V",
                type="web_secondaire",
            )],
        )],
    )])

    evaluations = evaluate_candidates(
        requirements, [audit], "Norel", 75, {url: contenu}, strict_evidence=False,
    )

    assert evaluations[0].summary.proven_criteria == ["Tension de bobine"]


def test_the_audit_prompt_drops_the_criteria_that_can_never_apply():
    """Rupture visée : demander à un Norel de prouver qu'il est un Kerion.

    Le fabricant, la référence exacte et la gamme d'origine sont classés
    `non_applicable` par `evaluate_candidates` et exclus du score. Les envoyer
    quand même au modèle noie les critères techniques sous des consignes
    contradictoires. Mesure du 2026-08-27 sur trois runs réels : les audits
    revenaient avec des critères techniques entièrement absents.
    """
    requirements = RequirementSet(
        product="Contacteur de puissance tripolaire NV1T05BD",
        origin_brand="Kerion Electric Tersa D",
        criteria=[
            Requirement(id="a", label="Fabricant", requested_value="Kerion Electric"),
            Requirement(id="b", label="Reference exacte", requested_value="NV1T05BD"),
            Requirement(id="c", label="Famille", requested_value="Tersa D"),
            Requirement(
                id="d",
                label="Fonction",
                requested_value="Contacteur de puissance tripolaire",
            ),
            Requirement(id="e", label="Courant nominal", requested_value="9 A (AC-3)"),
            Requirement(id="f", label="Tension de bobine", requested_value="24 V DC"),
        ],
    )

    prompt = construire_mission_audit(requirements, "Norel", "https://norelab.example/p")

    assert "Fabricant" not in prompt
    assert "Reference exacte" not in prompt
    assert "Famille" not in prompt
    assert "Fonction" in prompt
    assert "Courant nominal" in prompt
    assert "Tension de bobine" in prompt


def test_a_purely_technical_requirement_set_reaches_the_audit_untouched():
    """Sans critère d'origine, le jeu passe intégralement au modèle."""
    requirements = _requirements(3)

    prompt = construire_mission_audit(requirements, "Norel", "https://norelab.example/p")

    for requirement in requirements.criteria:
        assert requirement.label in prompt


def test_a_product_name_alone_proves_nothing_even_on_an_official_page():
    """Rupture visée : 100 % « complete » sur une page sans donnée technique.

    Mesure du 2026-08-27 sur un run réel : la page officielle
    `new.norel.example/products/fr/HPL1213001R0101/kb6-20-10-01` ne rendait que sa
    navigation — menus de pays, de langues, « Loading documents ». Le modèle a
    cité `KB6-20-10-01` comme preuve des six critères, et la mission a conclu
    `complete` à 100 % sur une page ne contenant aucune caractéristique.

    Le contrôle littéral ne pouvait rien voir : la désignation figure bien dans
    la page. Ce qui manque, c'est la valeur.
    """
    url = "https://new.norelab.example/products/fr/HPL1213001R0101/kb6-20-10-01"
    contenu = "KB6-20-10-01 | Norel Select Country Select Language Loading documents"
    requirements = RequirementSet(
        product="Kerion NV1T05BD",
        criteria=[
            Requirement(id="current", label="Courant nominal", requested_value="9 A"),
            Requirement(id="coil", label="Tension de bobine", requested_value="24 V DC"),
        ],
    )
    audit = PageAudit(page_url=url, candidates=[CandidateAudit(
        brand="Norel",
        reference="HPL1213001R0101",
        criteria=[
            CriterionAudit(
                requirement_id=identifiant,
                requested_value=valeur,
                observed_value=valeur,
                status="proven",
                proofs=[SourceProof(
                    url=url, excerpt="KB6-20-10-01", type="web_officiel",
                )],
            )
            for identifiant, valeur in (("current", "9 A"), ("coil", "24 V DC"))
        ],
    )])

    evaluations = evaluate_candidates(
        requirements, [audit], "Norel", 75, {url: contenu}, strict_evidence=False,
    )

    assert evaluations[0].summary.proven_criteria == []
    assert evaluations[0].summary.score == 0
    assert evaluations[0].complete is False


def test_a_value_bearing_excerpt_is_still_a_proof():
    """La garde ne doit pas confondre une valeur avec une désignation."""
    url = "https://distributeur.example/p/4kbl137001r1110"
    contenu = (
        "Norel 4KBL137001R1110 XZ07-20-10-11. Courant de commutation AC3 9A. "
        "24-60V50/60HZ 20-60VDC Contactor."
    )
    requirements = RequirementSet(
        product="Kerion NV1T05BD",
        criteria=[
            Requirement(id="current", label="Courant nominal", requested_value="9 A"),
        ],
    )
    audit = PageAudit(page_url=url, candidates=[CandidateAudit(
        brand="Norel",
        reference="4KBL137001R1110",
        criteria=[CriterionAudit(
            requirement_id="current",
            requested_value="9 A",
            observed_value="9 A",
            status="proven",
            proofs=[SourceProof(
                url=url,
                excerpt="Courant de commutation AC3 9A",
                type="web_secondaire",
            )],
        )],
    )])

    evaluations = evaluate_candidates(
        requirements, [audit], "Norel", 75, {url: contenu}, strict_evidence=False,
    )

    assert evaluations[0].summary.proven_criteria == ["Courant nominal"]
