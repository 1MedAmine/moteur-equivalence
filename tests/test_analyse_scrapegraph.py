# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
from pathlib import Path
from threading import Lock
from types import SimpleNamespace

import pytest

from langchain_core.exceptions import OutputParserException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analyse import analyser_page, examiner_pages
from candidats import CandidateLead, CandidateOccurrence, DiscoveryLimits, build_discovery_document
from compatibilite import evaluate_candidates
from mission import construire_mission_audit
from modeles import Requirement, RequirementSet
from scraping import PageContent
from indisponibilite import avertissement_est_limitation_llm


URL = "https://norelab.example/ref-1"
CONTENT = "Fiche Norel REF-1. Tension de commande 24 V DC. " * 5


def requirements():
    return RequirementSet(
        product="Contacteur source",
        criteria=[Requirement(
            id="tension",
            label="Tension de commande",
            requested_value="24 V DC",
            critical=True,
        )],
    )


def test_audit_prompt_requires_an_explicitly_different_categorical_state_to_be_incompatible():
    requirements_with_sealing = RequirementSet(
        product="Source bearing",
        criteria=[Requirement(
            id="sealing", label="Sealing", requested_value="with seals on both sides",
        )],
    )

    prompt = construire_mission_audit(
        requirements_with_sealing, None, URL,
    )

    assert "état catégoriel différent" in prompt
    assert "shielded" in prompt
    assert "incompatible" in prompt


def valid_audit(url=URL):
    return {
        "page_url": url,
        "candidates": [{
            "brand": "Norel",
            "reference": "REF-1",
            "criteria": [{
                "requirement_id": "tension",
                "requested_value": "24 V DC",
                "observed_value": "24 V DC",
                "status": "proven",
                "proofs": [{
                    "url": url,
                    "excerpt": "Tension de commande 24 V DC",
                    "type": "web_officiel",
                }],
            }],
        }],
    }


def audit_candidate(brand: str, reference: str, url: str = URL):
    return {
        "brand": brand,
        "reference": reference,
        "criteria": [{
            "requirement_id": "tension",
            "requested_value": "24 V DC",
            "observed_value": "",
            "status": "not_proven",
            "proofs": [],
        }],
    }


def declared_criteria(*candidate_audits):
    return {
        "candidate_criteria": [
            {
                "candidate_index": index,
                "criteria": candidate["criteria"],
            }
            for index, candidate in enumerate(candidate_audits)
        ],
    }


def candidate_lead(
    brand: str,
    reference: str,
    *,
    url: str = URL,
    low_confidence: bool = False,
) -> CandidateLead:
    return CandidateLead(
        brand=brand,
        reference=reference,
        canonical_brand="".join(character for character in brand.casefold() if character.isalnum()),
        canonical_reference="".join(
            character for character in reference.casefold() if character.isalnum()
        ),
        occurrences=(
            CandidateOccurrence(
                url=url,
                field="content",
                fragment=reference,
                rank=1,
            ),
        ),
        low_confidence=low_confidence,
    )


class FakeGraph:
    def __init__(self, captured, result, docs=None):
        self.captured = captured
        self.result = result
        self.docs = docs or []

    def run(self):
        self.captured["run_count"] = self.captured.get("run_count", 0) + 1
        return self.result

    def get_state(self, name):
        assert name == "doc"
        return self.docs


def factory(captured, result, docs=None):
    def build(**kwargs):
        captured.update(kwargs)
        return FakeGraph(captured, result, docs)
    return build


def test_scrapling_content_is_smart_scraper_source():
    """Mutation détectée : ScrapeGraphAI recharge inutilement une page déjà récupérée."""
    captured = {}

    result = analyser_page(
        PageContent(URL, CONTENT, "REF-1", "scrapling"),
        requirements(),
        "Norel",
        {},
        graph_factory=factory(captured, valid_audit()),
    )

    assert captured["source"] == CONTENT
    assert URL in captured["prompt"]
    assert captured["schema"].__name__ == "PageAuditEnvelope"
    assert result.audit.candidates[0].reference == "REF-1"
    assert result.content == CONTENT


def test_url_mode_lets_scrapegraphai_fetch_and_keeps_loaded_content():
    """Mutation détectée : le dernier recours SGA n'est pas vérifiable par extrait."""
    captured = {}
    loaded = "Page chargée par ScrapeGraphAI. Tension de commande 24 V DC. " * 4

    result = analyser_page(
        PageContent(URL, "", "", "scrapegraph_url"),
        requirements(),
        None,
        {},
        graph_factory=factory(
            captured,
            valid_audit(),
            docs=[SimpleNamespace(page_content=loaded)],
        ),
    )

    assert captured["source"] == URL
    assert result.content == loaded


def test_model_cannot_replace_the_actual_page_url():
    """Mutation détectée : le modèle rattache une preuve à une URL inventée."""
    result = analyser_page(
        PageContent(URL, CONTENT, "", "scrapling"),
        requirements(),
        None,
        {},
        graph_factory=factory({}, valid_audit("https://invented.example/x")),
    )

    assert result.audit.page_url == URL


def test_targeted_mode_keeps_the_audit_contract_and_declares_its_mode():
    result = analyser_page(
        PageContent(URL, CONTENT, "REF-1", "scrapling"),
        requirements(),
        "Norel",
        {},
        mode="targeted",
        authorized_candidates=(candidate_lead("Norel", "REF-1"),),
        graph_factory=factory({}, declared_criteria(valid_audit()["candidates"][0])),
    )

    assert result.mode == "targeted"
    assert result.audit.candidates[0].reference == "REF-1"


def test_targeted_empty_model_audit_declares_candidate_and_reports_anomaly():
    result = analyser_page(
        PageContent(URL, CONTENT, "Norel REF-1", "scrapling"),
        requirements(),
        "Norel",
        {},
        mode="targeted",
        authorized_candidates=(candidate_lead("Norel", "REF-1"),),
        graph_factory=factory({}, {"candidate_criteria": []}),
    )

    assert [(item.brand, item.reference) for item in result.audit.candidates] == [
        ("Norel", "REF-1")
    ]
    assert [
        (item.requirement_id, item.requested_value, item.status, item.proofs)
        for item in result.audit.candidates[0].criteria
    ] == [("tension", "24 V DC", "not_proven", [])]
    assert result.validation_diagnostics == ({
        "stage": "page_audit",
        "path": "candidate_criteria",
        "issue": "empty_declared_audit",
        "action": "defaulted_not_proven",
    },)


def test_targeted_empty_criteria_group_declares_candidate_and_reports_anomaly():
    result = analyser_page(
        PageContent(URL, CONTENT, "Norel REF-1", "scrapling"),
        requirements(),
        "Norel",
        {},
        mode="targeted",
        authorized_candidates=(candidate_lead("Norel", "REF-1"),),
        graph_factory=factory({}, {"candidate_criteria": [{
            "candidate_index": 0,
            "criteria": [],
        }]}),
    )

    assert result.audit.candidates[0].criteria[0].status == "not_proven"
    assert result.validation_diagnostics == ({
        "stage": "page_audit",
        "path": "candidate_criteria[0].criteria",
        "issue": "empty_declared_audit",
        "action": "defaulted_not_proven",
    },)


def test_discovery_with_confirmed_identity_declares_candidate_before_model_audit():
    captured = {}
    result = analyser_page(
        PageContent(URL, CONTENT, "Norel REF-1", "scrapling"),
        requirements(),
        "Norel",
        {},
        mode="discovery",
        authorized_candidates=(candidate_lead("Norel", "REF-1"),),
        graph_factory=factory(
            captured,
            declared_criteria(valid_audit()["candidates"][0]),
        ),
    )

    assert captured["schema"].__name__ == "DeclaredCandidateCriteriaEnvelope"
    assert [(item.brand, item.reference) for item in result.audit.candidates] == [
        ("Norel", "REF-1")
    ]
    assert result.mode == "discovery"


def test_targeted_declared_candidate_keeps_literal_proof_scoring_contract():
    result = analyser_page(
        PageContent(URL, CONTENT, "Norel REF-1", "scrapling"),
        requirements(),
        "Norel",
        {},
        mode="targeted",
        authorized_candidates=(candidate_lead("Norel", "REF-1"),),
        graph_factory=factory({}, declared_criteria(valid_audit()["candidates"][0])),
    )

    evaluations = evaluate_candidates(
        requirements(),
        [result.audit],
        "Norel",
        75,
        {URL: CONTENT},
    )

    assert len(evaluations) == 1
    assert evaluations[0].summary.reference == "REF-1"
    assert evaluations[0].summary.score == 100
    assert evaluations[0].candidate.criteria[0].proofs[0].excerpt == (
        "Tension de commande 24 V DC"
    )


def test_page_audit_keeps_valid_criterion_when_its_sibling_is_invalid():
    audit = valid_audit()
    audit["candidates"][0]["criteria"].append({
        "requirement_id": "courant",
        "requested_value": "9 A",
        "observed_value": "9 A",
        "status": "invalid-status",
        "proofs": [],
    })

    result = analyser_page(
        PageContent(URL, CONTENT, "REF-1", "scrapling"),
        requirements(),
        "Norel",
        {},
        graph_factory=factory({}, audit),
    )

    assert [item.requirement_id for item in result.audit.candidates[0].criteria] == [
        "tension"
    ]
    assert result.validation_diagnostics == ({
        "stage": "page_audit",
        "path": "candidates[0].criteria[1].status",
        "issue": "invalid_item",
        "action": "discarded",
    },)


def test_page_audit_keeps_valid_items_when_graph_validates_the_envelope():
    """Un élément invalide ne doit pas faire rejeter toute la page par Pydantic."""
    audit = valid_audit()
    audit["candidates"][0]["criteria"].append({
        "requirement_id": "courant",
        "requested_value": "9 A",
        "observed_value": "9 A",
        "status": "invalid-status",
        "proofs": [],
    })
    captured = {}

    def strict_factory(**kwargs):
        captured.update(kwargs)

        class Graph:
            def run(self):
                return kwargs["schema"].model_validate(audit)

            def get_state(self, name):
                return []

        return Graph()

    result = analyser_page(
        PageContent(URL, CONTENT, "REF-1", "scrapling"),
        requirements(),
        "Norel",
        {},
        graph_factory=strict_factory,
    )

    assert captured["schema"].__name__ == "PageAuditEnvelope"
    assert [item.requirement_id for item in result.audit.candidates[0].criteria] == [
        "tension"
    ]
    assert result.validation_diagnostics == ({
        "stage": "page_audit",
        "path": "candidates[0].criteria[1].status",
        "issue": "invalid_item",
        "action": "discarded",
    },)


def test_page_audit_global_schema_error_returns_only_a_safe_diagnostic():
    """Une structure entière invalide reste visible sans exposer la réponse."""
    calls = []

    def strict_factory(**kwargs):
        calls.append(kwargs)

        class Graph:
            def run(self):
                return kwargs["schema"].model_validate({
                    "candidates": "RAW_MODEL_RESPONSE_SENTINEL",
                })

            def get_state(self, name):
                return []

        return Graph()

    result = analyser_page(
        PageContent(URL, CONTENT, "REF-1", "scrapling"),
        requirements(),
        "Norel",
        {},
        graph_factory=strict_factory,
    )

    assert len(calls) == 1
    assert result.audit.candidates == []
    assert result.validation_diagnostics == ({
        "stage": "page_audit",
        "path": "candidates",
        "issue": "invalid_structure",
        "action": "aborted",
    },)
    assert "RAW_MODEL_RESPONSE_SENTINEL" not in str(result.validation_diagnostics)


def test_page_audit_discards_invalid_identity_without_losing_valid_candidate():
    invalid = audit_candidate("", "BROKEN")
    audit = valid_audit()
    audit["candidates"].insert(0, invalid)

    result = analyser_page(
        PageContent(URL, CONTENT, "REF-1", "scrapling"),
        requirements(),
        "Norel",
        {},
        graph_factory=factory({}, audit),
    )

    assert [item.reference for item in result.audit.candidates] == ["REF-1"]
    assert result.validation_diagnostics == ({
        "stage": "page_audit",
        "path": "candidates[0].brand",
        "issue": "invalid_item",
        "action": "discarded",
    },)


def test_page_audit_retries_once_after_broken_json_without_exposing_it():
    calls = []

    def graph_factory(**kwargs):
        calls.append(kwargs)

        class Graph:
            def run(self):
                if len(calls) == 1:
                    raise OutputParserException(
                        "provider included a secret response",
                        llm_output='{"candidates": [SECRET',
                    )
                return valid_audit()

            def get_state(self, name):
                return []

        return Graph()

    result = analyser_page(
        PageContent(URL, CONTENT, "REF-1", "scrapling"),
        requirements(),
        "Norel",
        {},
        graph_factory=graph_factory,
    )

    assert len(calls) == 2
    assert result.audit.candidates[0].reference == "REF-1"
    assert result.validation_diagnostics == ({
        "stage": "page_audit",
        "path": "$",
        "issue": "json_parse_error",
        "action": "retried",
    },)


def test_page_audit_reports_stage_and_path_after_the_single_retry_fails():
    calls = []

    def graph_factory(**kwargs):
        calls.append(kwargs)

        class Graph:
            def run(self):
                raise OutputParserException(
                    "provider included a secret response",
                    llm_output='{"candidates": [SECRET',
                )

        return Graph()

    results, warnings = examiner_pages(
        [PageContent(URL, CONTENT, "REF-1", "scrapling")],
        requirements(),
        "Norel",
        {},
        graph_factory=graph_factory,
    )

    assert results == []
    assert len(calls) == 2
    assert len(warnings) == 1
    assert "stage=page_audit" in warnings[0]
    assert "path=$" in warnings[0]
    assert "SECRET" not in warnings[0]
    assert "provider included" not in warnings[0]


def test_targeted_mode_constructs_only_the_authorized_exact_page_identity():
    captured = {}
    audit = declared_criteria(audit_candidate("Maker", "ZX-41-7"))
    audit["candidate_criteria"].append({
        "candidate_index": 1,
        "criteria": audit_candidate("Maker", "ZX-41-8")["criteria"],
    })

    result = analyser_page(
        PageContent(URL, "Maker ZX-41-7 technical data", "Maker ZX-41-7", "scrapling"),
        requirements(),
        "Maker",
        {},
        mode="targeted",
        authorized_candidates=(candidate_lead("Maker", "ZX-41-7"),),
        graph_factory=factory(captured, audit),
    )

    assert [(item.brand, item.reference) for item in result.audit.candidates] == [
        ("Maker", "ZX-41-7")
    ]
    assert captured["run_count"] == 1


def test_targeted_low_confidence_lead_still_requires_the_brand_on_the_page():
    result = analyser_page(
        PageContent(URL, "Catalog entry ZX-41-7 technical data", "ZX-41-7", "scrapling"),
        requirements(),
        "TargetCo",
        {},
        mode="targeted",
        authorized_candidates=(
            candidate_lead("TargetCo", "ZX-41-7", low_confidence=True),
        ),
        graph_factory=factory({}, declared_criteria(
            audit_candidate("TargetCo", "ZX-41-7")
        )),
    )

    assert result.audit.candidates == []


def test_targeted_mode_does_not_combine_brand_and_reference_from_neighbours():
    content = (
        "FAG 6205-C-2Z-C3 bearing. "
        "KINEX 6205-2ZR C3 bearing."
    )

    result = analyser_page(
        PageContent(URL, content, "SKF 6205-2Z/C3", "scrapling"),
        requirements(),
        None,
        {},
        mode="targeted",
        authorized_candidates=(candidate_lead("FAG", "6205 2ZR.C3"),),
        graph_factory=factory({}, declared_criteria(
            audit_candidate("FAG", "6205 2ZR.C3")
        )),
    )

    assert result.audit.candidates == []


def test_targeted_mode_keeps_two_authorized_identities_from_one_graph_call():
    captured = {}
    result = analyser_page(
        PageContent(
            URL,
            "Maker ZX-41-7 technical data. Maker QK-900 technical data.",
            "Maker ZX-41-7 and QK-900",
            "scrapling",
        ),
        requirements(),
        "Maker",
        {},
        mode="targeted",
        authorized_candidates=(
            candidate_lead("Maker", "ZX-41-7"),
            candidate_lead("Maker", "QK-900"),
        ),
        graph_factory=factory(captured, declared_criteria(
            audit_candidate("Maker", "ZX-41-7"),
            audit_candidate("Maker", "QK-900"),
        )),
    )

    assert [item.reference for item in result.audit.candidates] == ["ZX-41-7", "QK-900"]
    assert captured["run_count"] == 1


def test_discovery_audit_keeps_only_identities_literal_in_the_visited_page():
    document = build_discovery_document(
        url=URL,
        title="Maker ZX-41-7",
        snippets=[],
        content="Maker ZX-41-7 technical data",
        rank=1,
    )
    result = analyser_page(
        PageContent(URL, document.content, document.title, "scrapling"),
        requirements(),
        None,
        {},
        mode="discovery",
        document=document,
        graph_factory=factory({}, {
            "leads": [],
            "audit": {
                "page_url": URL,
                "candidates": [
                    audit_candidate("Maker", "ZX-41-7"),
                    audit_candidate("Maker", "ZX-41-8"),
                    audit_candidate("InventedCo", "ZX-41-7"),
                ],
            },
        }),
    )

    assert [(item.brand, item.reference) for item in result.audit.candidates] == [
        ("Maker", "ZX-41-7")
    ]


def test_discovery_envelope_keeps_the_lead_when_one_audit_item_is_invalid():
    """La découverte ne perd pas une identité à cause d'un critère voisin."""
    audit = valid_audit()
    audit["candidates"][0]["criteria"].append({
        "requirement_id": "courant",
        "requested_value": "9 A",
        "observed_value": "9 A",
        "status": "invalid-status",
        "proofs": [],
    })
    document = build_discovery_document(
        url=URL,
        title="Norel REF-1",
        snippets=[],
        content=CONTENT,
        rank=1,
    )

    def strict_factory(**kwargs):
        class Graph:
            def run(self):
                return kwargs["schema"].model_validate({
                    "leads": [{"brand": "Norel", "reference": "REF-1"}],
                    "audit": audit,
                })

            def get_state(self, name):
                return []

        return Graph()

    result = analyser_page(
        PageContent(URL, CONTENT, "Norel REF-1", "scrapling"),
        requirements(),
        "Norel",
        {},
        mode="discovery",
        document=document,
        graph_factory=strict_factory,
    )

    assert [proposal.reference for proposal in result.proposals] == ["REF-1"]
    assert [item.requirement_id for item in result.audit.candidates[0].criteria] == [
        "tension"
    ]
    assert result.validation_diagnostics == ({
        "stage": "page_audit",
        "path": "candidates[0].criteria[1].status",
        "issue": "invalid_item",
        "action": "discarded",
    },)


def test_targeted_audit_prompt_names_allowed_identity_and_forbids_substitutions():
    prompt = construire_mission_audit(
        requirements(),
        "Maker",
        URL,
        authorized_candidates=[candidate_lead("Maker", "ZX-41-7")],
    )

    assert "Maker ZX-41-7" in prompt
    assert "déjà déclarés" in prompt
    assert "ne décides pas" in prompt
    assert "candidate_index" in prompt
    assert "variantes" in prompt.casefold()
    assert "accessoires" in prompt.casefold()
    assert "gammes voisines" in prompt.casefold()
    assert "substitutions" in prompt.casefold()


def test_discovery_mode_keeps_a_literal_lead_when_audit_is_empty():
    captured = {}
    document = build_discovery_document(
        url=URL,
        title="Maker ZX-41-7",
        snippets=["Product ZX-41-7"],
        content="Maker model ZX-41-7 without complete specifications.",
        rank=1,
    )

    result = analyser_page(
        PageContent(URL, document.content, document.title, "scrapling"),
        requirements(),
        "Maker",
        {},
        mode="discovery",
        document=document,
        graph_factory=factory(captured, {
            "leads": [{"brand": "Maker", "reference": "ZX-41-7"}],
            "audit": None,
        }),
    )

    assert result.proposals[0].reference == "ZX-41-7"
    assert result.audit.candidates == []
    assert captured["source"] == document.prompt_source
    assert captured["schema"].__name__ == "DiscoveryEnvelope"
    assert "piste" in captured["prompt"].casefold()
    assert "preuve" in captured["prompt"].casefold()
    assert "liste `leads` vide" in captured["prompt"]
    assert "ne calcule aucun score" in captured["prompt"].casefold()


def test_discovery_mode_forces_a_nested_audit_back_to_the_visited_url():
    document = build_discovery_document(
        url=URL,
        title="Maker ZX-41-7",
        snippets=[],
        content=CONTENT,
        rank=1,
    )

    result = analyser_page(
        PageContent(URL, CONTENT, "Maker ZX-41-7", "scrapling"),
        requirements(),
        "Norel",
        {},
        mode="discovery",
        document=document,
        graph_factory=factory({}, {"leads": [], "audit": valid_audit("https://invented.example/x")}),
    )

    assert result.audit.page_url == URL


def test_discovery_prompt_is_truncated_without_losing_full_page_content():
    captured = {}
    full_content = "Maker ZX-41-7 technical text. " * 20
    document = build_discovery_document(
        url=URL,
        title="Maker ZX-41-7",
        snippets=[],
        content=full_content,
        rank=1,
        limits=DiscoveryLimits(content_chars=12, total_chars=80),
    )

    result = analyser_page(
        PageContent(URL, full_content, "Maker ZX-41-7", "scrapling"),
        requirements(),
        "Maker",
        {},
        mode="discovery",
        document=document,
        graph_factory=factory(captured, {"leads": [], "audit": None}),
    )

    assert captured["source"] == document.prompt_source
    assert captured["source"] != full_content
    assert result.content == full_content
    assert "content" in document.truncated_fields


def test_metadata_only_discovery_document_is_usable_without_fetched_content():
    captured = {}
    document = build_discovery_document(
        url=URL,
        title="Maker ZX-41-7",
        snippets=["Product ZX-41-7"],
        content="",
        rank=1,
    )

    result = analyser_page(
        PageContent(URL, "", document.title, "scrapling"),
        requirements(),
        "Maker",
        {},
        mode="discovery",
        document=document,
        graph_factory=factory(captured, {
            "leads": [{"brand": "Maker", "reference": "ZX-41-7"}],
            "audit": None,
        }),
    )

    assert captured["source"] == document.prompt_source
    assert [item.reference for item in result.proposals] == ["ZX-41-7"]
    assert result.content == ""


def test_discovery_metadata_composite_never_becomes_auditable_page_content():
    """Le corpus de pistes ne doit jamais devenir une preuve technique visitee."""
    document = build_discovery_document(
        url=URL,
        title="Maker ZX-41-7",
        snippets=["Maker ZX-41-7, tension 24 V DC"],
        content="",
        rank=1,
    )

    result = analyser_page(
        PageContent(URL, "", document.title, "scrapling"),
        requirements(),
        "Maker",
        {},
        mode="discovery",
        document=document,
        graph_factory=factory(
            {},
            {
                "leads": [{"brand": "Maker", "reference": "ZX-41-7"}],
                "audit": {
                    "page_url": URL,
                    "candidates": [audit_candidate("Maker", "ZX-41-7")],
                },
            },
            docs=[SimpleNamespace(page_content=document.prompt_source)],
        ),
    )

    assert [item.reference for item in result.proposals] == ["ZX-41-7"]
    assert result.content == ""
    assert result.audit.candidates == []


def test_one_malformed_discovery_response_does_not_discard_another_page_lead():
    good_url = "https://maker.example/good"
    bad_url = "https://maker.example/bad"
    pages = [
        PageContent(good_url, "Maker ZX-41-7", "Maker ZX-41-7", "scrapling"),
        PageContent(bad_url, "Maker ZX-99-1", "Maker ZX-99-1", "scrapling"),
    ]
    documents = {
        page.url: build_discovery_document(
            url=page.url,
            title=page.title,
            snippets=[],
            content=page.content,
            rank=index + 1,
        )
        for index, page in enumerate(pages)
    }

    def graph_factory(**kwargs):
        if kwargs["source"] == documents[bad_url].prompt_source:
            return FakeGraph({}, {"leads": "not-a-list", "audit": None})
        return FakeGraph({}, {
            "leads": [{"brand": "Maker", "reference": "ZX-41-7"}],
            "audit": None,
        })

    results, warnings = examiner_pages(
        pages,
        requirements(),
        "Maker",
        {},
        mode="discovery",
        documents_by_url=documents,
        graph_factory=graph_factory,
    )

    assert [item.page_url for item in results] == [good_url]
    assert [item.reference for item in results[0].proposals] == ["ZX-41-7"]
    assert len(warnings) == 1


def test_discovery_creates_and_runs_exactly_one_graph_per_page():
    pages = [
        PageContent(
            f"https://maker.example/product-{index}",
            f"Maker ZX-{index} technical data",
            f"Maker ZX-{index}",
            "scrapling",
        )
        for index in (10, 20)
    ]
    documents = {
        page.url: build_discovery_document(
            url=page.url,
            title=page.title,
            snippets=[],
            content=page.content,
            rank=index,
        )
        for index, page in enumerate(pages, start=1)
    }
    lock = Lock()
    factory_counts: dict[str, int] = {}
    run_counts: dict[str, int] = {}

    class CountingGraph:
        def __init__(self, source: str) -> None:
            self.source = source

        def run(self):
            with lock:
                run_counts[self.source] = run_counts.get(self.source, 0) + 1
            return {"leads": [], "audit": None}

    def graph_factory(**kwargs):
        source = kwargs["source"]
        with lock:
            factory_counts[source] = factory_counts.get(source, 0) + 1
        return CountingGraph(source)

    results, warnings = examiner_pages(
        pages,
        requirements(),
        "Maker",
        {},
        mode="discovery",
        documents_by_url=documents,
        graph_factory=graph_factory,
    )

    assert len(results) == len(pages) == 2
    assert warnings == []
    expected = {document.prompt_source: 1 for document in documents.values()}
    assert factory_counts == expected
    assert run_counts == expected


def test_discovery_retains_a_valid_lead_and_rejects_its_malformed_sibling():
    document = build_discovery_document(
        url=URL,
        title="Maker ZX-41-7",
        snippets=[],
        content="Maker ZX-41-7",
        rank=1,
    )

    result = analyser_page(
        PageContent(URL, document.content, document.title, "scrapling"),
        requirements(),
        "Maker",
        {},
        mode="discovery",
        document=document,
        graph_factory=factory({}, {
            "leads": [
                {"brand": "Maker", "reference": "ZX-41-7"},
                {"brand": "", "reference": 17},
            ],
            "audit": None,
        }),
    )

    assert [item.reference for item in result.proposals] == ["ZX-41-7"]
    assert [item.reason for item in result.proposal_rejections] == ["malformed_proposal"]


def test_one_page_failure_does_not_discard_other_pages():
    """Mutation détectée : une exception annule toute la vague."""
    pages = [
        PageContent(URL, CONTENT, "", "scrapling"),
        PageContent("https://broken.example/x", CONTENT, "", "scrapling"),
    ]

    def graph_factory(**kwargs):
        if "broken.example" in kwargs["prompt"]:
            raise RuntimeError("boom")
        return FakeGraph({}, valid_audit())

    results, warnings = examiner_pages(
        pages,
        requirements(),
        "Norel",
        {},
        workers=2,
        graph_factory=graph_factory,
    )

    assert [item.page_url for item in results] == [URL]
    assert len(warnings) == 1
    assert "broken.example/x" in warnings[0]
    assert "boom" not in warnings[0]


def test_page_warning_carries_a_structured_429_flag_from_a_generic_wrapper():
    class ProviderFailure(RuntimeError):
        status_code = 429

    def graph_factory(**kwargs):
        raise ProviderFailure("secret provider payload")

    _, warnings = examiner_pages(
        [PageContent(URL, CONTENT, "", "scrapling")],
        requirements(),
        "Norel",
        {},
        graph_factory=graph_factory,
    )

    assert len(warnings) == 1
    assert avertissement_est_limitation_llm(warnings[0])
    assert "secret provider payload" not in warnings[0]


def test_failure_warning_separates_url_navigation_from_content_analysis():
    """Mutation détectée : deux pannes de causes opposées rendent le même diagnostic."""
    pages = [
        PageContent(URL, CONTENT, "", "scrapling"),
        PageContent("https://vide.example/x", "", "", "scrapegraph_url"),
    ]

    def graph_factory(**kwargs):
        raise RuntimeError("boom")

    _, warnings = examiner_pages(
        pages,
        requirements(),
        "Norel",
        {},
        workers=2,
        graph_factory=graph_factory,
    )

    par_page = {
        ("vide" if "vide.example" in warning else "remplie"): warning
        for warning in warnings
    }

    # Page vide : ScrapeGraphAI a navigué lui-même, la panne est côté Chromium.
    assert "source=url" in par_page["vide"]
    assert "0 caractères" in par_page["vide"]
    # Page déjà récupérée : aucune navigation, la panne est côté modèle/schéma.
    assert "source=contenu" in par_page["remplie"]

    assert all("RuntimeError" in warning for warning in warnings)
    assert all("boom" not in warning for warning in warnings)


def _audit_avec_type_de_preuve(valeur):
    audit = valid_audit()
    audit["candidates"][0]["criteria"][0]["proofs"][0]["type"] = valeur
    return audit


@pytest.mark.parametrize(
    ("rendu", "attendu"),
    [
        ("official", "web_officiel"),
        ("Fabricant", "web_officiel"),
        ("distributor", "web_secondaire"),
        ("", "web_secondaire"),
        (None, "web_secondaire"),
    ],
)
def test_an_unexpected_proof_type_is_normalised_instead_of_losing_the_criterion(
    rendu,
    attendu,
):
    """Rupture visée : un `type` hors contrat emportait tout le critère.

    Mesure du 2026-08-27 sur quatre runs réels : le modèle rendait `official`
    ou une variante française, Pydantic rejetait la preuve, et le critère
    entier — extrait littéral et URL compris — disparaissait de l'audit. Les
    missions finissaient `not_resolved` avec des critères dits « absents ».

    Le repli va toujours vers `web_secondaire` : le crédit officiel reste
    accordé par le domaine du fabricant, jamais par la déclaration du modèle.
    """
    result = analyser_page(
        PageContent(URL, CONTENT, "REF-1", "scrapling"),
        requirements(),
        "Norel",
        {},
        graph_factory=factory({}, _audit_avec_type_de_preuve(rendu)),
    )

    criterion = result.audit.candidates[0].criteria[0]
    assert criterion.status == "proven"
    assert criterion.proofs[0].excerpt == "Tension de commande 24 V DC"
    assert criterion.proofs[0].type == attendu


def test_a_contract_conform_proof_type_is_left_untouched():
    """La normalisation ne doit pas toucher une réponse déjà valide."""
    result = analyser_page(
        PageContent(URL, CONTENT, "REF-1", "scrapling"),
        requirements(),
        "Norel",
        {},
        graph_factory=factory({}, valid_audit()),
    )

    assert result.audit.candidates[0].criteria[0].proofs[0].type == "web_officiel"
    assert result.validation_diagnostics == ()


def test_a_proof_returned_as_a_bare_string_is_rebuilt_on_the_audited_page():
    """Rupture visée : l'extrait seul, sans objet autour, perdait le critère.

    Mesure du 2026-08-27 sur quatre runs réels : c'est la forme la plus
    fréquemment rejetée — 69 preuves sur 108. L'URL rattachée n'est pas devinée,
    c'est la page en cours d'audit, la seule que le graphe ait lue.
    """
    audit = valid_audit()
    audit["candidates"][0]["criteria"][0]["proofs"] = [
        "Tension de commande 24 V DC"
    ]

    result = analyser_page(
        PageContent(URL, CONTENT, "REF-1", "scrapling"),
        requirements(),
        "Norel",
        {},
        graph_factory=factory({}, audit),
    )

    criterion = result.audit.candidates[0].criteria[0]
    assert criterion.status == "proven"
    assert criterion.proofs[0].url == URL
    assert criterion.proofs[0].excerpt == "Tension de commande 24 V DC"
    # Jamais officiel sur la seule forme : le domaine du fabricant en décide.
    assert criterion.proofs[0].type == "web_secondaire"


def test_an_empty_bare_string_proof_is_still_refused():
    """Réparer la forme ne doit pas fabriquer une preuve vide."""
    audit = valid_audit()
    audit["candidates"][0]["criteria"][0]["proofs"] = ["   "]

    result = analyser_page(
        PageContent(URL, CONTENT, "REF-1", "scrapling"),
        requirements(),
        "Norel",
        {},
        graph_factory=factory({}, audit),
    )

    assert result.audit.candidates == []
