# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import rapport
from compatibilite import evaluate_candidates
from modeles import (
    CandidateAudit,
    CandidateEvaluation,
    CompatibilitySummary,
    CriterionAudit,
    PageAudit,
    Requirement,
    RequirementSet,
    SearchAttempt,
    SourceProof,
)
from recherche_adaptative import ResearchDiagnostics, ResearchOutcome


URL = "https://norelab.example/ref-1"
AUDITED_URL = "https://maker.example/audited"
SENTINEL_FRAGMENT = "LEAD_OCCURRENCE_FRAGMENT_DO_NOT_SERIALIZE"
SENTINEL_RAW_RESPONSE = "RAW_GRAPH_RESPONSE_DO_NOT_SERIALIZE"
SENTINEL_PAGE_CONTENT = "PAGE_CONTENT_DO_NOT_SERIALIZE"
SENTINEL_API_KEY = "sk-task5-do-not-serialize"
SENTINEL_TOKEN = "TOKEN_DO_NOT_SERIALIZE"
SENTINEL_SECRET = "SECRET_DO_NOT_SERIALIZE"


def outcome(status="complete", score=100):
    proof = SourceProof(
        url=URL,
        excerpt="Tension de commande 24 V DC",
        type="web_officiel",
    )
    candidate = CandidateAudit(
        brand="Norel",
        reference="REF-1",
        criteria=[CriterionAudit(
            requirement_id="r1",
            requested_value="24 V DC",
            observed_value="24 V DC",
            status="proven",
            proofs=[proof],
        )],
    )
    evaluation = CandidateEvaluation(
        candidate=candidate,
        summary=CompatibilitySummary(
            score=score,
            brand="Norel",
            reference="REF-1",
            proven_criteria=["Tension"],
        ),
        eligible=True,
        complete=status == "complete",
        official_proof_count=1,
        proof_url_count=1,
    )
    diagnostics = ResearchDiagnostics(
        waves=1,
        logical_queries=4,
        engine_calls=4,
        pages_opened=12,
        queries=[f"requête {i}" for i in range(4)],
        attempts=[SearchAttempt(engine="bing", status="ok", result_count=12)],
        opened_pages=[URL],
    )
    requirements = RequirementSet(
        product="Contacteur source",
        criteria=[Requirement(id="r1", label="Tension", requested_value="24 V DC")],
    )
    return ResearchOutcome(
        status,
        requirements,
        evaluation,
        [PageAudit(page_url=URL, candidates=[candidate])],
        {URL: "Tension de commande 24 V DC"},
        diagnostics,
    )


def test_adaptive_report_contains_score_proofs_and_budgets():
    """Mutation détectée : le nouveau flux est aplati dans l'ancien rapport libre."""
    data = rapport.construire(outcome())

    assert data["status"] == "complete"
    assert data["compatibility"]["score"] == 100
    assert data["compatibility"]["reference"] == "REF-1"
    assert data["diagnostics"]["logical_queries"] == 4
    assert data["diagnostics"]["pages_opened"] == 12
    assert data["sources"] == [{
        "url": URL,
        "excerpt": "Tension de commande 24 V DC",
        "type": "web_officiel",
    }]


def test_adaptive_report_exposes_requirement_coverage_counts_without_raw_text():
    """Rupture visée : le compteur tourne mais son résultat reste invisible."""
    result = outcome()
    result.diagnostics.requirement_specs_detected = 14
    result.diagnostics.requirement_specs_covered = 4
    result.diagnostics.requirement_orphans_detected = 10
    result.diagnostics.requirement_orphan_pages = [1]
    result.diagnostics.requirement_second_pass = True

    data = rapport.construire(result)

    assert data["diagnostics"]["requirement_coverage"] == {
        "detected_specs": 14,
        "covered_specs": 4,
        "orphan_specs": 10,
        "orphan_pages": [1],
        "second_pass_triggered": True,
    }
    assert "LEAD_OCCURRENCE_FRAGMENT_DO_NOT_SERIALIZE" not in json.dumps(data)


def test_requirement_validation_diagnostics_are_allowlisted_and_readable():
    result = outcome()
    result.diagnostics.requirement_validation = [
        {
            "stage": "requirement_visual_supplement",
            "path": "criteria[3].requested_value",
            "issue": "invalid_item",
            "action": "discarded",
            "raw_response": "RAW_MODEL_RESPONSE_SENTINEL",
        },
        {
            "stage": "requirement_initial_extraction",
            "path": "criteria[0].requested_value",
            "issue": "unit_label_disagreement",
            "action": "review",
        },
    ]

    data = rapport.construire(result)
    markdown = rapport.en_markdown(data)

    assert data["diagnostics"]["requirement_validation"] == [
        {
            "stage": "requirement_visual_supplement",
            "path": "criteria[3].requested_value",
            "issue": "invalid_item",
            "action": "discarded",
        },
        {
            "stage": "requirement_initial_extraction",
            "path": "criteria[0].requested_value",
            "issue": "unit_label_disagreement",
            "action": "review",
        },
    ]
    assert "requirement_visual_supplement" in markdown
    assert "criteria[3].requested_value" in markdown
    assert "unit_label_disagreement" not in markdown
    assert "unités incompatibles — à vérifier" in markdown
    assert "RAW_MODEL_RESPONSE_SENTINEL" not in str(data)
    assert "RAW_MODEL_RESPONSE_SENTINEL" not in markdown


def test_page_audit_validation_diagnostics_are_allowlisted_and_readable():
    result = outcome()
    result.diagnostics.page_audit_validation = [
        {
            "stage": "page_audit",
            "path": "candidates[0].criteria[2].status",
            "issue": "invalid_item",
            "action": "discarded",
            "raw_response": "RAW_MODEL_RESPONSE_SENTINEL",
        },
        {
            "stage": "page_audit",
            "path": "candidates",
            "issue": "invalid_structure",
            "action": "aborted",
            "raw_response": "RAW_MODEL_RESPONSE_SENTINEL",
        },
        {
            "stage": "RAW_MODEL_RESPONSE_SENTINEL",
            "path": "RAW_MODEL_RESPONSE_SENTINEL",
            "issue": "RAW_MODEL_RESPONSE_SENTINEL",
            "action": "RAW_MODEL_RESPONSE_SENTINEL",
        },
    ]

    data = rapport.construire(result)
    markdown = rapport.en_markdown(data)

    assert data["diagnostics"]["page_audit_validation"] == [
        {
            "stage": "page_audit",
            "path": "candidates[0].criteria[2].status",
            "issue": "invalid_item",
            "action": "discarded",
        },
        {
            "stage": "page_audit",
            "path": "candidates",
            "issue": "invalid_structure",
            "action": "aborted",
        },
    ]
    assert "candidates[0].criteria[2].status" in markdown
    assert "`candidates`" in markdown
    assert "RAW_MODEL_RESPONSE_SENTINEL" not in str(data)
    assert "RAW_MODEL_RESPONSE_SENTINEL" not in markdown


def test_adaptive_report_projects_each_criterion_with_its_verified_proofs():
    data = rapport.construire(outcome("partial", 25))

    assert data["criteria"] == [{
        "label": "Tension",
        "requested_value": "24 V DC",
        "status": "proven",
        "proofs": [{
            "url": URL,
            "excerpt": "Tension de commande 24 V DC",
            "type": "web_officiel",
        }],
    }]


def test_adaptive_report_keys_criterion_statuses_by_requirement_id_not_label():
    result = outcome("rejected", 50)
    evaluation = result.evaluation
    assert evaluation is not None
    incompatible_proof = SourceProof(
        url=URL,
        excerpt="Courant observé 19 A",
        type="web_officiel",
    )
    evaluation.candidate.criteria = [
        evaluation.candidate.criteria[0],
        CriterionAudit(
            requirement_id="r2",
            requested_value="9 A",
            observed_value="19 A",
            status="incompatible",
            proofs=[incompatible_proof],
        ),
    ]
    evaluation.summary.proven_criteria = ["Valeur"]
    evaluation.summary.incompatible_criteria = ["Valeur"]
    result.requirements.criteria = [
        Requirement(id="r1", label="Valeur", requested_value="24 V DC"),
        Requirement(id="r2", label="Valeur", requested_value="9 A"),
    ]

    details = rapport._details_criteres(evaluation, result.requirements)

    assert [(item["requested_value"], item["status"]) for item in details] == [
        ("24 V DC", "proven"),
        ("9 A", "incompatible"),
    ]


def test_eligible_candidate_with_noncritical_incompatibility_is_proposed_not_discarded():
    result = outcome("partial", 75)
    evaluation = result.evaluation
    assert evaluation is not None
    proof = SourceProof(
        url=URL,
        excerpt="Stock No. concurrent 9999",
        type="web_officiel",
    )
    evaluation.candidate.criteria.append(CriterionAudit(
        requirement_id="stock_no",
        requested_value="7822",
        observed_value="9999",
        status="incompatible",
        proofs=[proof],
    ))
    evaluation.summary.incompatible_criteria = ["Stock No."]
    evaluation.summary.critical_blockers = []
    evaluation.eligible = True
    evaluation.complete = False
    result.requirements.criteria.append(Requirement(
        id="stock_no",
        label="Stock No.",
        requested_value="7822",
        critical=False,
    ))
    result.visited_pages[URL] += " Stock No. concurrent 9999."

    data = rapport.construire(result)

    assert [item["reference"] for item in data["proposed_candidates"]] == ["REF-1"]
    assert data["proposed_candidates"][0]["incompatible_criteria"] == ["Stock No."]
    assert data["discarded_candidates"] == []


def test_adaptive_report_never_serializes_a_rejected_proof_from_a_mixed_list():
    result = outcome("partial", 100)
    candidate = result.audits[0].candidates[0]
    candidate.criteria[0].proofs.insert(0, SourceProof(
        url=URL,
        excerpt="fragment inventé absent",
        type="web_officiel",
    ))
    result.evaluation = evaluate_candidates(
        result.requirements,
        result.audits,
        target_brand="Norel",
        threshold=75,
        visited_pages=result.visited_pages,
        strict_evidence=False,
    )[0]

    data = rapport.construire(result)

    assert [proof["excerpt"] for proof in data["sources"]] == [
        "Tension de commande 24 V DC",
    ]
    assert [proof["excerpt"] for proof in data["criteria"][0]["proofs"]] == [
        "Tension de commande 24 V DC",
    ]


def test_partial_report_marks_a_single_secondary_source_as_not_corroborated():
    result = outcome("partial", 100)
    secondary_url = "https://www.distributeur-a.example/products/ref-1"
    evaluation = result.evaluation
    assert evaluation is not None
    evaluation.official_proof_count = 0
    evaluation.proof_url_count = 1
    evaluation.complete = False
    evaluation.candidate.criteria[0].proofs = [SourceProof(
        url=secondary_url,
        excerpt="Tension de commande 24 V DC",
        type="web_secondaire",
    )]

    data = rapport.construire(result)

    assert data["status"] == "partial"
    assert data["compatibility"]["evidence_note"] == (
        "Source unique, non corroborée."
    )
    assert "source unique, non corroborée" in data["alternative_proposee"].casefold()


def test_partial_report_marks_two_secondary_sources_as_insufficiently_corroborated():
    result = outcome("partial", 100)
    evaluation = result.evaluation
    assert evaluation is not None
    evaluation.official_proof_count = 0
    evaluation.proof_url_count = 2
    evaluation.complete = False

    data = rapport.construire(result)

    assert data["compatibility"]["evidence_note"] == (
        "Deux sources secondaires, corroboration insuffisante."
    )


def test_complete_report_names_three_corroborated_secondary_sources():
    result = outcome("complete", 100)
    evaluation = result.evaluation
    assert evaluation is not None
    evaluation.official_proof_count = 0
    evaluation.proof_url_count = 3
    evaluation.complete = True

    data = rapport.construire(result)

    assert data["compatibility"]["evidence_note"] == (
        "Sources multiples corroborées."
    )


def test_adaptive_report_lists_an_incompatible_candidate_as_discarded_not_proposed():
    proof = SourceProof(
        url=URL,
        excerpt="Bobine 100-250 V AC/DC",
        type="web_officiel",
    )
    candidate = CandidateAudit(
        brand="Norel",
        reference="XZ07-20-10-13",
        criteria=[CriterionAudit(
            requirement_id="coil",
            requested_value="24 V DC",
            observed_value="100-250 V AC/DC",
            status="incompatible",
            proofs=[proof],
        )],
    )
    requirements = RequirementSet(
        product="Contacteur source",
        criteria=[Requirement(id="coil", label="Bobine", requested_value="24 V DC")],
    )
    rejected_evaluation = CandidateEvaluation(
        candidate=candidate,
        summary=CompatibilitySummary(
            score=0,
            brand="Norel",
            reference="XZ07-20-10-13",
            incompatible_criteria=["Bobine"],
        ),
        eligible=False,
        complete=False,
        official_proof_count=1,
        proof_url_count=1,
    )
    rejected = ResearchOutcome(
        "rejected",
        requirements,
        None,
        [PageAudit(page_url=URL, candidates=[candidate])],
        {URL: "XZ07-20-10-13. Bobine 100-250 V AC/DC."},
        ResearchDiagnostics(waves=1),
        evaluations=[rejected_evaluation],
    )

    data = rapport.construire(rejected)
    text = rapport.en_markdown(data)

    assert data["status"] == "rejected"
    assert data["alternative_proposee"] == ""
    assert data["discarded_candidates"] == [{
        "brand": "Norel",
        "reference": "XZ07-20-10-13",
        "reason": "Bobine incompatible",
        "criteria": [{
            "label": "Bobine",
            "requested_value": "24 V DC",
            "status": "incompatible",
            "proofs": [{
                "url": URL,
                "excerpt": "Bobine 100-250 V AC/DC",
                "type": "web_officiel",
            }],
        }],
    }]
    assert "Écarté : Norel XZ07-20-10-13 — Bobine incompatible" in text


def test_markdown_exposes_criteria_and_search_diagnostics():
    """Mutation détectée : l'humain ne voit ni les manques ni les moteurs essayés."""
    text = rapport.en_markdown(rapport.construire(outcome("partial", 75)))

    assert "**Statut** : partial" in text
    assert "**Compatibilité** : 75 %" in text
    assert "## Critères prouvés" in text
    assert "## Diagnostic de recherche" in text
    assert "Requêtes logiques : 4" in text


def test_not_resolved_adaptive_report_never_invents_a_reference():
    requirements = RequirementSet(
        product="Produit source",
        criteria=[Requirement(id="r1", label="Tension", requested_value="24 V DC")],
    )
    unresolved = ResearchOutcome(
        "not_resolved", requirements, None, [], {}, ResearchDiagnostics(waves=3)
    )

    data = rapport.construire(unresolved)

    assert data["status"] == "not_resolved"
    assert data["sources"] == []
    assert data["compatibility"] is None
    assert "aucune" in data["alternative_proposee"].casefold()


def unresolved_outcome(
    final_state="candidates_discovered_but_not_audited",
):
    requirements = RequirementSet(
        product="Produit source",
        criteria=[Requirement(id="r1", label="Tension", requested_value="24 V DC")],
    )
    diagnostics = ResearchDiagnostics(
        waves=2,
        logical_queries=8,
        engine_calls=8,
        pages_opened=3,
        wave_modes=[
            {
                "wave": 1,
                "mode": "discovery",
                "secret": {"token": SENTINEL_TOKEN, "secret": SENTINEL_SECRET},
            },
            {"wave": 2, "mode": "targeted", "raw_graph_response": SENTINEL_RAW_RESPONSE},
        ],
        candidate_leads=[
            {
                "brand": "Maker",
                "reference": "ZX-41-7",
                "rank": 1,
                "source_count": 2,
                "fields": ["title", "content", SENTINEL_FRAGMENT],
                "fragment": SENTINEL_FRAGMENT,
                "secret": {"api_key": SENTINEL_API_KEY},
            },
            {
                "brand": "Other",
                "reference": "QK-900",
                "rank": 2,
                "source_count": 1,
                "fields": ["url"],
                "raw_graph_response": SENTINEL_RAW_RESPONSE,
            },
        ],
        rejected_candidates=[
            {
                "brand": "Maker",
                "reference": "INVENTED-99",
                "reason": "reference_not_literal",
                "page_content": SENTINEL_PAGE_CONTENT,
                "token": SENTINEL_TOKEN,
            },
        ],
        targeted_queries=[
            {
                "wave": 2,
                "query": "Maker ZX-41-7 fiche produit",
                "brand": "Maker",
                "reference": "ZX-41-7",
                "api_key": SENTINEL_API_KEY,
                "secret": {"raw_graph_response": SENTINEL_RAW_RESPONSE},
            },
        ],
        page_states=[
            {
                "wave": 1,
                "mode": "discovery",
                "url": "https://maker.example/first",
                "selected": True,
                "fetched": True,
                "analysis_status": "discovered",
                "audit_candidate_count": 0,
                "truncated_fields": ["content", SENTINEL_PAGE_CONTENT],
                "page_content": SENTINEL_PAGE_CONTENT,
                "secret": {"token": SENTINEL_TOKEN},
            },
            {
                "wave": 2,
                "mode": "targeted",
                "url": "https://maker.example/second",
                "selected": True,
                "fetched": True,
                "analysis_status": "audited",
                "audit_candidate_count": 1,
                "truncated_fields": [],
                "raw_graph_response": SENTINEL_RAW_RESPONSE,
            },
            {
                "wave": 2,
                "mode": "targeted",
                "url": "https://maker.example/third",
                "selected": True,
                "fetched": False,
                "analysis_status": "no_candidate",
                "audit_candidate_count": 0,
                "truncated_fields": [],
                "api_key": SENTINEL_API_KEY,
            },
        ],
        final_state=final_state,
    )
    diagnostics.raw_graph_response = SENTINEL_RAW_RESPONSE
    diagnostics.api_key = SENTINEL_API_KEY
    return ResearchOutcome(
        "not_resolved",
        requirements,
        None,
        [],
        {URL: SENTINEL_PAGE_CONTENT},
        diagnostics,
    )


def test_adaptive_report_serializes_safe_candidate_progress_without_sources():
    data = rapport.construire(unresolved_outcome())

    assert data["diagnostics"]["final_state"] == (
        "candidates_discovered_but_not_audited"
    )
    assert data["diagnostics"]["candidate_leads"] == [
        {
            "brand": "Maker",
            "reference": "ZX-41-7",
            "source_count": 2,
            "fields": ["title", "content"],
        },
        {
            "brand": "Other",
            "reference": "QK-900",
            "source_count": 1,
            "fields": ["url"],
        },
    ]
    assert data["diagnostics"]["rejected_candidates"] == [
        {
            "brand": "Maker",
            "reference": "INVENTED-99",
            "reason": "reference_not_literal",
        },
    ]
    assert data["diagnostics"]["targeted_queries"] == [
        {
            "wave": 2,
            "query": "Maker ZX-41-7 fiche produit",
            "brand": "Maker",
            "reference": "ZX-41-7",
        },
    ]
    assert data["diagnostics"]["page_states"][1]["analysis_status"] == "audited"
    assert data["diagnostics"]["page_states"] == [
        {
            "wave": 1,
            "mode": "discovery",
            "url": "https://maker.example/first",
            "selected": True,
            "fetched": True,
            "analysis_status": "discovered",
            "audit_candidate_count": 0,
            "truncated_fields": ["content"],
        },
        {
            "wave": 2,
            "mode": "targeted",
            "url": "https://maker.example/second",
            "selected": True,
            "fetched": True,
            "analysis_status": "audited",
            "audit_candidate_count": 1,
            "truncated_fields": [],
        },
        {
            "wave": 2,
            "mode": "targeted",
            "url": "https://maker.example/third",
            "selected": True,
            "fetched": False,
            "analysis_status": "no_candidate",
            "audit_candidate_count": 0,
            "truncated_fields": [],
        },
    ]
    assert data["diagnostics"]["wave_modes"] == [
        {"wave": 1, "mode": "discovery"},
        {"wave": 2, "mode": "targeted"},
    ]
    assert data["sources"] == []

    serialized = json.dumps(data, sort_keys=True)
    for forbidden in (
        SENTINEL_RAW_RESPONSE,
        SENTINEL_API_KEY,
        SENTINEL_PAGE_CONTENT,
        SENTINEL_FRAGMENT,
        SENTINEL_TOKEN,
        SENTINEL_SECRET,
    ):
        assert forbidden not in serialized


def test_candidate_evaluations_are_projected_by_allowlist_and_safe_labels():
    result = unresolved_outcome()
    result.diagnostics.candidate_evaluations = [
        {
            "wave": 2,
            "url": "https://maker.example/product",
            "reference": "ZX-41-7",
            "brand": "Maker",
            "score": 75,
            "eligible": True,
            "complete": False,
            "proof_url_count": 2,
            "official_proof_count": 1,
            "proven_criteria": ["Tension", SENTINEL_PAGE_CONTENT],
            "not_proven_criteria": ["Tension", SENTINEL_FRAGMENT],
            "incompatible_criteria": [SENTINEL_RAW_RESPONSE],
            "unverified_criteria": ["Tension", SENTINEL_SECRET],
            "missing_audit_criteria": ["Tension", SENTINEL_PAGE_CONTENT],
            "rejected_proof_criteria": ["Tension", SENTINEL_RAW_RESPONSE],
            "proof_grades": {
                "Tension": "windowed",
                SENTINEL_SECRET: "contiguous",
            },
            "non_applicable_criteria": ["Tension", SENTINEL_TOKEN],
            "downgraded_by_official_source": ["Tension", SENTINEL_PAGE_CONTENT],
            "observed_value": SENTINEL_TOKEN,
            "raw_graph_response": SENTINEL_RAW_RESPONSE,
        },
        {
            "wave": 3,
            "url": "https://maker.example/bad",
            "reference": "BAD-1",
            "contract_error": "CompatibilityContractError",
            "exception_message": SENTINEL_API_KEY,
        },
    ]

    data = rapport.construire(result)

    assert data["diagnostics"]["candidate_evaluations"] == [
        {
            "wave": 2,
            "url": "https://maker.example/product",
            "reference": "ZX-41-7",
            "brand": "Maker",
            "score": 75,
            "eligible": True,
            "complete": False,
            "proof_url_count": 2,
            "official_proof_count": 1,
            "proven_criteria": ["Tension"],
            "not_proven_criteria": ["Tension"],
            "incompatible_criteria": [],
            "unverified_criteria": ["Tension"],
            "missing_audit_criteria": ["Tension"],
            "rejected_proof_criteria": ["Tension"],
            "proof_grades": {"Tension": "windowed"},
            "non_applicable_criteria": ["Tension"],
            "downgraded_by_official_source": ["Tension"],
        },
        {
            "wave": 3,
            "url": "https://maker.example/bad",
            "reference": "BAD-1",
            "contract_error": "CompatibilityContractError",
        },
    ]
    serialized = json.dumps(data, sort_keys=True)
    for forbidden in (
        SENTINEL_PAGE_CONTENT,
        SENTINEL_FRAGMENT,
        SENTINEL_RAW_RESPONSE,
        SENTINEL_SECRET,
        SENTINEL_TOKEN,
        SENTINEL_API_KEY,
    ):
        assert forbidden not in serialized


def test_report_filters_unrecognized_reason_and_final_state_values():
    result = unresolved_outcome()
    result.diagnostics.rejected_candidates.append({
        "brand": "Maker",
        "reference": "ZX-41-7",
        "reason": SENTINEL_RAW_RESPONSE,
    })
    result.diagnostics.final_state = SENTINEL_SECRET

    data = rapport.construire(result)
    serialized = json.dumps(data, sort_keys=True)

    assert data["diagnostics"]["rejected_candidates"] == [{
        "brand": "Maker",
        "reference": "INVENTED-99",
        "reason": "reference_not_literal",
    }]
    assert data["diagnostics"]["final_state"] == "no_candidate_discovered"
    assert SENTINEL_RAW_RESPONSE not in serialized
    assert SENTINEL_SECRET not in serialized


def terminal_outcome(final_state):
    if final_state == "candidate_selected":
        result = outcome()
        result.diagnostics.final_state = final_state
        result.diagnostics.wave_modes = [{"wave": 1, "mode": "discovery"}]
        result.diagnostics.page_states = [{
            "wave": 1,
            "mode": "discovery",
            "url": URL,
            "selected": True,
            "fetched": True,
            "analysis_status": "audited",
            "audit_candidate_count": 1,
            "truncated_fields": [],
        }]
        return result
    result = unresolved_outcome(final_state)
    if final_state == "no_candidate_discovered":
        result.diagnostics.candidate_leads = []
        result.diagnostics.rejected_candidates = []
        result.diagnostics.targeted_queries = []
        result.diagnostics.wave_modes = [{"wave": 1, "mode": "discovery"}]
        result.diagnostics.page_states = [{
            "wave": 1,
            "mode": "discovery",
            "url": "https://maker.example/empty",
            "selected": True,
            "fetched": True,
            "analysis_status": "no_candidate",
            "audit_candidate_count": 0,
            "truncated_fields": [],
        }]
    elif final_state in {"rejected_by_evidence", "audited_but_non_verifiable"}:
        result.diagnostics.candidate_leads = []
        result.diagnostics.targeted_queries = []
        if final_state == "rejected_by_evidence":
            candidate = CandidateAudit(
                brand="Maker",
                reference="ZX-41-7",
                criteria=[CriterionAudit(
                    requirement_id="r1",
                    requested_value="24 V DC",
                    observed_value="24 V DC",
                    status="proven",
                    proofs=[SourceProof(
                        url=AUDITED_URL,
                        excerpt="Preuve absente du contenu visité",
                        type="web_officiel",
                    )],
                )],
            )
            result.visited_pages = {AUDITED_URL: "Tension 24 V DC sans cet extrait."}
        else:
            candidate = CandidateAudit(
                brand="Maker",
                reference="ZX-41-7",
                criteria=[CriterionAudit(
                    requirement_id="r1",
                    requested_value="24 V DC",
                    observed_value="",
                    status="not_proven",
                )],
            )
            result.visited_pages = {AUDITED_URL: "Fiche Maker ZX-41-7."}
        result.audits = [PageAudit(page_url=AUDITED_URL, candidates=[candidate])]
        result.diagnostics.page_states = [{
            "wave": 1,
            "mode": "discovery",
            "url": AUDITED_URL,
            "selected": True,
            "fetched": True,
            "analysis_status": "audited",
            "audit_candidate_count": 1,
            "truncated_fields": [],
        }]
    elif final_state == "candidates_discovered_but_not_audited":
        result.diagnostics.page_states = [
            {
                "wave": 1,
                "mode": "discovery",
                "url": "https://maker.example/first",
                "selected": True,
                "fetched": True,
                "analysis_status": "discovered",
                "audit_candidate_count": 0,
                "truncated_fields": ["content"],
            },
            {
                "wave": 2,
                "mode": "targeted",
                "url": "https://maker.example/third",
                "selected": True,
                "fetched": False,
                "analysis_status": "no_candidate",
                "audit_candidate_count": 0,
                "truncated_fields": [],
            },
        ]
    return result


@pytest.mark.parametrize(
    ("final_state", "expected_status", "expected_source_count"),
    [
        ("candidate_selected", "complete", 1),
        ("rejected_by_evidence", "not_resolved", 0),
        ("audited_but_non_verifiable", "not_resolved", 0),
        ("candidates_discovered_but_not_audited", "not_resolved", 0),
        ("no_candidate_discovered", "not_resolved", 0),
    ],
)
def test_adaptive_report_renders_coherent_terminal_outcomes(
    final_state,
    expected_status,
    expected_source_count,
):
    result = terminal_outcome(final_state)

    data = rapport.construire(result)
    text = rapport.en_markdown(data)

    assert data["status"] == expected_status
    assert len(data["sources"]) == expected_source_count
    assert result.diagnostics.final_state == final_state
    assert data["diagnostics"]["final_state"] == final_state
    assert f"**Statut** : {expected_status}" in text
    assert f"- Etat final : {final_state}" in text

    page_states = result.diagnostics.page_states
    if final_state == "candidate_selected":
        assert result.evaluation is not None
        assert len(result.audits) == 1
        assert len(result.audits[0].candidates) == 1
        assert page_states == [{
            "wave": 1,
            "mode": "discovery",
            "url": URL,
            "selected": True,
            "fetched": True,
            "analysis_status": "audited",
            "audit_candidate_count": 1,
            "truncated_fields": [],
        }]
        assert "- Etats des pages : audited: 1" in text
    elif final_state in {"rejected_by_evidence", "audited_but_non_verifiable"}:
        assert result.evaluation is None
        assert len(result.audits) == 1
        assert len(result.audits[0].candidates) == 1
        criterion = result.audits[0].candidates[0].criteria[0]
        if final_state == "rejected_by_evidence":
            assert criterion.status == "proven"
            assert criterion.proofs
            assert criterion.proofs[0].excerpt not in result.visited_pages[AUDITED_URL]
        else:
            assert criterion.status == "not_proven"
            assert criterion.proofs == []
        assert page_states == [{
            "wave": 1,
            "mode": "discovery",
            "url": AUDITED_URL,
            "selected": True,
            "fetched": True,
            "analysis_status": "audited",
            "audit_candidate_count": 1,
            "truncated_fields": [],
        }]
        assert "- Etats des pages : audited: 1" in text
    elif final_state == "candidates_discovered_but_not_audited":
        assert result.evaluation is None
        assert result.audits == []
        assert result.diagnostics.candidate_leads
        assert all(item["analysis_status"] != "audited" for item in page_states)
        assert all(item["audit_candidate_count"] == 0 for item in page_states)
        assert "- Etats des pages : discovered: 1; no_candidate: 1" in text
    else:
        assert result.evaluation is None
        assert result.audits == []
        assert result.diagnostics.candidate_leads == []
        assert all(item["analysis_status"] != "audited" for item in page_states)
        assert all(item["audit_candidate_count"] == 0 for item in page_states)
        assert "- Etats des pages : no_candidate: 1" in text


def test_markdown_exposes_safe_candidate_progress_without_page_content():
    text = rapport.en_markdown(rapport.construire(unresolved_outcome()))

    assert "- Etat final : candidates_discovered_but_not_audited" in text
    assert "- Modes de vague : 1: discovery; 2: targeted" in text
    assert "- References decouvertes : Maker ZX-41-7; Other QK-900" in text
    assert "- Etats des pages : audited: 1; discovered: 1; no_candidate: 1" in text
    for forbidden in (
        SENTINEL_PAGE_CONTENT,
        SENTINEL_RAW_RESPONSE,
        SENTINEL_FRAGMENT,
        SENTINEL_API_KEY,
        SENTINEL_TOKEN,
        SENTINEL_SECRET,
    ):
        assert forbidden not in text
