# -*- coding: utf-8 -*-
"""Contrats purs de découverte et de registre des candidats B2."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import candidats
from candidats import (
    CandidateProposal,
    CandidateRegistry,
    CandidateLead,
    CandidateOccurrence,
    DiscoveryEnvelope,
    DiscoveryLimits,
    audit_proposals,
    build_discovery_document,
    parse_candidate_proposals,
    rank_leads,
)
from modeles import CandidateAudit, CriterionAudit, PageAudit, Requirement, RequirementSet


def requirements(product: str = "Neutral breaker", **values: str) -> RequirementSet:
    requested = values or {"current": "10 A"}
    return RequirementSet(
        product=product,
        criteria=[
            Requirement(id=key, label=key, requested_value=value)
            for key, value in requested.items()
        ],
    )


def document_with(text: str, *, url: str = "https://maker.example/p/ZX-41-7", rank: int = 1):
    return build_discovery_document(
        url=url,
        title=text,
        snippets=[],
        content=text,
        rank=rank,
    )


def test_reference_variants_merge_but_an_invented_reference_is_rejected():
    document = build_discovery_document(
        url="https://maker.example/p/ZX%2D41%2D7",
        title="Maker ZX-41-7 product data",
        snippets=["Technical page for ZX 41 7"],
        content="Maker data sheet for model ZX-41-7.",
        rank=1,
    )
    registry = CandidateRegistry()

    accepted = registry.ingest(
        [CandidateProposal(brand="Maker", reference="zx 41 7")],
        document,
        requirements(),
        target_brand="Maker",
    )
    rejected = registry.ingest(
        [CandidateProposal(brand="Maker", reference="INVENTED-99")],
        document,
        requirements(),
        target_brand="Maker",
    )

    # La piste retient l'orthographe la plus observee dans le document, pas
    # celle de la proposition : `ZX-41-7` est ecrit dans le titre, l'URL et le
    # contenu, alors que `zx 41 7` ne vient que du modele.
    assert [lead.reference for lead in accepted.accepted] == ["ZX-41-7"]
    assert registry.active()[0].canonical_reference == "zx417"
    assert rejected.rejected[0].reason == "reference_not_literal"


def test_reference_must_be_visible_in_every_supported_document_field():
    cases = [
        ("title", "Maker ZX-41-7"),
        ("snippet", "Maker ZX 41 7"),
        ("url", "https://maker.example/p/ZX%2D41%2D7"),
        ("content", "Maker data for ZX-41-7"),
    ]
    for field, value in cases:
        parts = {"url": "https://maker.example/p/other", "title": "Maker catalogue", "snippets": [], "content": "Maker catalogue"}
        if field == "snippet":
            parts["snippets"] = [value]
        else:
            parts[field] = value
        result = CandidateRegistry().ingest(
            [CandidateProposal(brand="Maker", reference="ZX 41 7")],
            build_discovery_document(rank=1, **parts),
            requirements(),
            target_brand="Maker",
        )
        assert len(result.accepted) == 1, field
        assert result.accepted[0].occurrences[0].field == field


def test_separator_normalized_reference_is_literal_but_not_an_embedded_substring():
    compact = CandidateRegistry().ingest(
        [CandidateProposal(brand="Maker", reference="ZX-41-7")],
        document_with("Maker ZX417", url="https://maker.example/p/other"),
        requirements(),
        target_brand="Maker",
        allow_deterministic_fallback=False,
    )
    embedded = CandidateRegistry().ingest(
        [CandidateProposal(brand="Maker", reference="ZX-41-7")],
        document_with("Maker AZX417B", url="https://maker.example/p/other"),
        requirements(),
        target_brand="Maker",
        allow_deterministic_fallback=False,
    )

    assert [lead.reference for lead in compact.accepted] == ["ZX-41-7"]
    assert embedded.rejected[0].reason == "reference_not_literal"


def test_identity_key_keeps_brands_separate_and_folds_unicode_variants():
    registry = CandidateRegistry()
    document = document_with("Maker One ZX–41 7; Maker Two zx 41 7")
    registry.ingest(
        [
            CandidateProposal(brand="Maker One", reference="ZX-41-7"),
            CandidateProposal(brand="Maker Two", reference="zx 41 7"),
        ],
        document,
        requirements(),
        target_brand=None,
    )

    assert [(lead.canonical_brand, lead.canonical_reference) for lead in registry.active()] == [
        ("makerone", "zx417"),
        ("makertwo", "zx417"),
    ]


def test_registry_excludes_origin_and_isolated_requirement_values():
    registry = CandidateRegistry()
    result = registry.ingest(
        [
            CandidateProposal(brand="SourceCo", reference="SRC-100"),
            CandidateProposal(brand="TargetCo", reference="24VDC"),
        ],
        document_with("SourceCo SRC-100 and TargetCo 24VDC"),
        requirements(product="SourceCo SRC-100", voltage="24 V DC"),
        target_brand="TargetCo",
        allow_deterministic_fallback=False,
    )

    assert result.accepted == ()
    assert {item.reason for item in result.rejected} == {
        "origin_identity",
        "isolated_requirement_value",
    }


def test_registry_decodes_structured_origin_brand_range_and_reference():
    source = RequirementSet(
        product="Motor starter 10 A",
        origin_brand=(
            "fabricant: SourceCo; gamme: AlphaLine; reference: SRC-100"
        ),
        criteria=[Requirement(id="current", label="Current", requested_value="10 A")],
    )
    document = document_with(
        "SourceCo SRC-100 and SourceCo AlphaLine",
        url="https://source.example/products/SRC-100",
    )

    result = CandidateRegistry().ingest(
        [
            CandidateProposal(brand="SourceCo", reference="SRC-100"),
            CandidateProposal(brand="SourceCo", reference="AlphaLine"),
        ],
        document,
        source,
        target_brand=None,
        allow_deterministic_fallback=False,
    )

    assert result.accepted == ()
    assert [item.reason for item in result.rejected] == [
        "origin_identity",
        "origin_identity",
    ]


def test_registry_excludes_origin_reference_across_unicode_separator_variants():
    variants = (
        ("Origin module ZX-41-7", "ZX 41 7"),
        ("Origin module ZX 41 7", "ZX–41–7"),
        ("Origin module ZX—41—7", "ZX-41-7"),
    )

    for product, candidate_reference in variants:
        source = RequirementSet(
            product=product,
            origin_brand="SourceCo",
            criteria=[Requirement(id="current", label="Current", requested_value="10 A")],
        )
        result = CandidateRegistry().ingest(
            [CandidateProposal(brand="DifferentMaker", reference=candidate_reference)],
            document_with(f"DifferentMaker {candidate_reference}"),
            source,
            target_brand=None,
            allow_deterministic_fallback=False,
        )

        assert result.accepted == (), (product, candidate_reference)
        assert result.rejected[0].reason == "origin_identity"


def test_compact_electrical_unit_requested_as_a_criterion_is_not_a_candidate_identity():
    result = CandidateRegistry().ingest(
        [CandidateProposal(brand="TargetCo", reference="24VDC")],
        document_with("TargetCo 24VDC"),
        requirements(voltage="24VDC"),
        target_brand="TargetCo",
        allow_deterministic_fallback=False,
    )

    assert result.rejected[0].reason == "isolated_requirement_value"


def test_registry_rejects_a_candidate_from_another_target_brand():
    result = CandidateRegistry().ingest(
        [CandidateProposal(brand="OtherCo", reference="OT-77")],
        document_with("OtherCo OT-77"),
        requirements(),
        target_brand="TargetCo",
    )

    assert result.rejected[0].reason == "target_brand_mismatch"


def test_target_brand_requires_an_exact_canonical_brand_identity():
    result = CandidateRegistry().ingest(
        [CandidateProposal(brand="Maker One", reference="MO-17")],
        document_with("Maker One MO-17"),
        requirements(),
        target_brand="Maker",
        allow_deterministic_fallback=False,
    )

    assert result.rejected[0].reason == "target_brand_mismatch"


def test_graph_target_brand_without_page_brand_is_retained_as_low_confidence():
    result = CandidateRegistry().ingest(
        [CandidateProposal(brand="TargetCo", reference="ZX-41-7")],
        document_with("Catalog entry ZX-41-7"),
        requirements(),
        target_brand="TargetCo",
    )

    assert result.accepted[0].low_confidence is True


def test_ambiguous_reference_is_retained_only_as_low_confidence():
    result = CandidateRegistry().ingest(
        [CandidateProposal(brand="Maker", reference="P-7")],
        document_with("Maker P-7 accessory"),
        requirements(),
        target_brand="Maker",
    )

    assert result.accepted[0].low_confidence is True


def test_occurrences_merge_across_document_fields_and_urls():
    registry = CandidateRegistry()
    registry.ingest(
        [CandidateProposal(brand="Maker", reference="ZX-41-7")],
        build_discovery_document(
            url="https://maker.example/a",
            title="Maker ZX-41-7",
            snippets=[],
            content="Maker catalogue",
            rank=2,
        ),
        requirements(),
        target_brand="Maker",
    )
    registry.ingest(
        [CandidateProposal(brand="Maker", reference="ZX 41 7")],
        build_discovery_document(
            url="https://maker.example/b",
            title="Maker catalogue",
            snippets=["Maker ZX 41 7"],
            content="Maker ZX-41-7 details",
            rank=1,
        ),
        requirements(),
        target_brand="Maker",
    )

    lead = registry.active()[0]
    assert {occurrence.url for occurrence in lead.occurrences} == {
        "https://maker.example/a",
        "https://maker.example/b",
    }
    assert {occurrence.field for occurrence in lead.occurrences} == {"title", "snippet", "content"}


def test_nested_references_merge_under_the_shorter_searchable_identity():
    registry = CandidateRegistry()
    document = document_with(
        "Norel 4KBL141001R8110 - X9-20-10 24V; code 4KBL141001R8110",
        url="https://maker.example/product/4KBL141001R8110",
    )

    result = registry.ingest(
        [
            CandidateProposal(
                brand="Norel",
                reference="4KBL141001R8110 - X9-20-10 24V",
            ),
            CandidateProposal(brand="Norel", reference="4KBL141001R8110"),
        ],
        document,
        requirements(),
        target_brand="Norel",
        allow_deterministic_fallback=False,
    )

    assert [lead.reference for lead in registry.active()] == ["4KBL141001R8110"]
    assert [lead.reference for lead in result.accepted] == ["4KBL141001R8110"]
    assert len(registry.active()[0].occurrences) >= 2


def test_nested_merge_requires_same_brand_and_an_eight_character_short_identity():
    registry = CandidateRegistry()
    document = document_with(
        "Maker 10Pcs and Maker 1P; Other 4KBL141001R8110 extended",
    )
    registry.ingest(
        [
            CandidateProposal(brand="Maker", reference="10Pcs"),
            CandidateProposal(brand="Maker", reference="1P"),
            CandidateProposal(brand="Other", reference="4KBL141001R8110"),
        ],
        document,
        requirements(),
        target_brand=None,
        allow_deterministic_fallback=False,
    )

    assert {(lead.brand, lead.reference) for lead in registry.active()} == {
        ("Maker", "10Pcs"),
        ("Maker", "1P"),
        ("Other", "4KBL141001R8110"),
    }


def test_nested_reference_merge_is_order_independent_across_pages():
    registry = CandidateRegistry()
    registry.ingest(
        [CandidateProposal(brand="Norel", reference="4KBL141001R8110")],
        document_with(
            "Norel code 4KBL141001R8110",
            url="https://maker.example/short",
        ),
        requirements(),
        target_brand="Norel",
        allow_deterministic_fallback=False,
    )
    registry.ingest(
        [CandidateProposal(
            brand="Norel",
            reference="4KBL141001R8110 - X9-20-10 24V",
        )],
        document_with(
            "Norel 4KBL141001R8110 - X9-20-10 24V",
            url="https://maker.example/long",
        ),
        requirements(),
        target_brand="Norel",
        allow_deterministic_fallback=False,
    )

    assert [lead.reference for lead in registry.active()] == ["4KBL141001R8110"]
    assert {item.url for item in registry.active()[0].occurrences} == {
        "https://maker.example/short",
        "https://maker.example/long",
    }


def test_registry_retains_four_best_leads_in_documented_order():
    registry = CandidateRegistry(DiscoveryLimits(max_active=4))
    document = document_with(
        "TargetCo T-10; TargetCo T-20; TargetCo T-30; TargetCo T-40; TargetCo T-50",
        rank=4,
    )
    registry.ingest(
        [CandidateProposal(brand="TargetCo", reference=reference) for reference in ("T-50", "T-40", "T-30", "T-20", "T-10")],
        document,
        requirements(),
        target_brand="TargetCo",
    )

    assert [lead.reference for lead in registry.active()] == ["T-10", "T-20", "T-30", "T-40"]
    assert [lead.rank for lead in registry.active()] == [1, 2, 3, 4]


def test_confirmed_graph_lead_outranks_four_repeated_low_confidence_url_tokens():
    registry = CandidateRegistry(DiscoveryLimits(max_active=4))
    weak_references = ("AA-1001", "BB-2002", "CC-3003", "DD-4004")
    for index, suffix in enumerate("abcdefghijkl", start=1):
        registry.ingest(
            (),
            build_discovery_document(
                url=(
                    "https://maker.example/products/"
                    + "/".join(weak_references)
                    + f"/page-{suffix}"
                ),
                title="Maker product catalogue",
                snippets=[],
                content="",
                rank=index,
            ),
            requirements(),
            target_brand="Maker",
        )

    confirmed = registry.ingest(
        [CandidateProposal(brand="Maker", reference="CONF-900")],
        build_discovery_document(
            url="https://maker.example/products/confirmed",
            title="Maker CONF-900 product data",
            snippets=[],
            content="Maker CONF-900 technical specification",
            rank=12,
        ),
        requirements(),
        target_brand="Maker",
        allow_deterministic_fallback=False,
    )

    assert [item.reference for item in confirmed.accepted] == ["CONF-900"]
    assert registry.active()[0].reference == "CONF-900"
    assert registry.active()[0].low_confidence is False
    assert len(registry.active()) == 4


def test_rank_is_recomputed_after_a_merge_improves_provenance():
    registry = CandidateRegistry()
    registry.ingest(
        [CandidateProposal(brand="Maker", reference="AA-1"), CandidateProposal(brand="Maker", reference="BB-2")],
        build_discovery_document(
            url="https://maker.example/one",
            title="Maker AA-1; Maker BB-2",
            snippets=[],
            content="Maker catalogue",
            rank=2,
        ),
        requirements(),
        target_brand="Maker",
    )
    assert [lead.reference for lead in registry.active()] == ["AA-1", "BB-2"]
    registry.ingest(
        [CandidateProposal(brand="Maker", reference="BB-2")],
        build_discovery_document(
            url="https://maker.example/two",
            title="Maker BB-2",
            snippets=[],
            content="Maker details",
            rank=1,
        ),
        requirements(),
        target_brand="Maker",
    )

    assert [(lead.reference, lead.rank) for lead in registry.active()] == [("BB-2", 1), ("AA-1", 2)]


def test_ranking_uses_target_urls_field_quality_result_rank_then_lexical_key():
    def lead(brand: str, reference: str, occurrences: tuple[CandidateOccurrence, ...]) -> CandidateLead:
        return CandidateLead(
            brand=brand,
            reference=reference,
            canonical_brand=brand.casefold(),
            canonical_reference=reference.casefold(),
            occurrences=occurrences,
        )

    def occurrence(url: str, field: str, rank: int) -> CandidateOccurrence:
        return CandidateOccurrence(url=url, field=field, fragment="id", rank=rank)  # type: ignore[arg-type]

    ranked = rank_leads([
        lead("Maker", "D", (occurrence("https://x/d", "title", 2),)),
        lead("Maker", "C", (occurrence("https://x/c", "title", 1),)),
        lead("Maker", "B", (occurrence("https://x/b", "content", 8),)),
        lead("Maker", "A", (
            occurrence("https://x/a1", "snippet", 10),
            occurrence("https://x/a2", "snippet", 10),
            occurrence("https://x/a3", "snippet", 10),
        )),
        lead("TargetCo", "Z", (occurrence("https://x/z", "url", 12),)),
        lead("Maker", "F", (occurrence("https://x/f", "url", 4),)),
        lead("Maker", "E", (occurrence("https://x/e", "url", 4),)),
    ], "TargetCo")

    assert [item.reference for item in ranked] == ["Z", "A", "C", "D", "B", "E", "F"]
    assert [item.rank for item in ranked] == [1, 2, 3, 4, 5, 6, 7]


def test_one_malformed_graph_lead_does_not_discard_its_valid_sibling():
    parsed = parse_candidate_proposals([
        {"brand": "Maker", "reference": "ZX-41-7"},
        {"brand": "", "reference": 17},
    ])
    assert [item.reference for item in parsed.proposals] == ["ZX-41-7"]
    assert [item.reason for item in parsed.rejected] == ["malformed_proposal"]


def test_discovery_envelope_allows_leads_without_a_technical_audit():
    envelope = DiscoveryEnvelope.model_validate({
        "leads": [{"brand": "Maker", "reference": "ZX-41-7"}],
        "audit": None,
    })

    assert envelope.audit is None
    assert envelope.leads == [{"brand": "Maker", "reference": "ZX-41-7"}]


def test_deterministic_fallback_returns_only_a_low_confidence_target_brand_identifier():
    result = CandidateRegistry().ingest(
        [],
        document_with("TargetCo catalogue : modèle ZX-41-7"),
        requirements(),
        target_brand="TargetCo",
    )

    assert [(lead.brand, lead.reference, lead.low_confidence) for lead in result.accepted] == [
        ("TargetCo", "ZX-41-7", True),
    ]


@pytest.mark.parametrize("noise", ["10Pcs", "1P", "1000stel"])
def test_deterministic_fallback_rejects_quantifiers_and_prose_tokens(noise):
    result = CandidateRegistry().ingest(
        [],
        document_with(
            f"Norel catalogue contacteur : {noise}",
            url="https://maker.example/catalogue",
        ),
        requirements(product="Contacteur industriel"),
        target_brand="Norel",
    )

    assert result.accepted == ()


def test_deterministic_fallback_does_not_assign_a_nearby_competitor_reference_to_target_brand():
    result = CandidateRegistry().ingest(
        [],
        document_with(
            "Kessner-Roy contactor 200-C30GK10. Norel product catalogue.",
            url="https://maker.example/comparison",
        ),
        requirements(product="Contacteur industriel"),
        target_brand="Norel",
    )

    assert result.accepted == ()


def test_deterministic_fallback_rejects_competitor_reference_in_same_comparison_clause():
    result = CandidateRegistry().ingest(
        [],
        document_with(
            "Compare Norel contactor with Kessner-Roy 200-C30GK10",
            url="https://maker.example/comparison",
        ),
        requirements(product="Contacteur industriel"),
        target_brand="Norel",
    )

    assert result.accepted == ()


def test_deterministic_fallback_rejects_competitor_reference_after_vs():
    result = CandidateRegistry().ingest(
        [],
        document_with(
            "Norel contactor vs Kessner-Roy 200-C30GK10",
            url="https://maker.example/comparison",
        ),
        requirements(product="Contacteur industriel"),
        target_brand="Norel",
    )

    assert result.accepted == ()


def test_deterministic_fallback_keeps_only_target_side_of_comparison():
    result = CandidateRegistry().ingest(
        [],
        document_with(
            "Norel XZ07-20-10-11 vs Kessner-Roy 200-C30GK10",
            url="https://maker.example/comparison",
        ),
        requirements(product="Contacteur industriel"),
        target_brand="Norel",
    )

    assert [lead.reference for lead in result.accepted] == ["XZ07-20-10-11"]


def test_deterministic_fallback_rejects_competitor_reference_found_only_in_comparison_url():
    document = build_discovery_document(
        url="https://maker.example/comparison/200-C30GK10",
        title="Compare Norel contactor with Kessner-Roy",
        snippets=[],
        content="",
        rank=1,
    )

    result = CandidateRegistry().ingest(
        [],
        document,
        requirements(product="Contacteur industriel"),
        target_brand="Norel",
    )

    assert result.accepted == ()


def test_url_fallback_prefers_explicit_competitor_snippet_over_generic_target_title():
    document = build_discovery_document(
        url="https://maker.example/comparison/200-C30GK10",
        title="Norel product catalogue",
        snippets=["Kessner-Roy contactor 200-C30GK10"],
        content="",
        rank=1,
    )

    result = CandidateRegistry().ingest(
        [],
        document,
        requirements(product="Contacteur industriel"),
        target_brand="Norel",
    )

    assert result.accepted == ()


def test_url_fallback_attributes_the_exact_repeated_reference_occurrence():
    document = build_discovery_document(
        url="https://maker.example/products/ZX-41-7",
        title="Other ZX-41-7 vs Maker contactor ZX-41-7",
        snippets=[],
        content="",
        rank=1,
    )

    result = CandidateRegistry().ingest(
        [],
        document,
        requirements(product="Contactor"),
        target_brand="Maker",
    )

    assert [lead.reference for lead in result.accepted] == ["ZX-41-7"]


def test_content_fallback_attributes_the_exact_repeated_reference_occurrence():
    result = CandidateRegistry().ingest(
        [],
        document_with(
            "Other ZX-41-7 vs Maker contactor ZX-41-7",
            url="https://maker.example/comparison",
        ),
        requirements(product="Contactor"),
        target_brand="Maker",
    )

    assert [lead.reference for lead in result.accepted] == ["ZX-41-7"]


def test_comparison_chart_word_alone_does_not_hide_target_reference():
    result = CandidateRegistry().ingest(
        [],
        document_with(
            "Norel XZ07-20-10-11 comparison chart",
            url="https://maker.example/comparison",
        ),
        requirements(product="Contacteur industriel"),
        target_brand="Norel",
    )

    assert [lead.reference for lead in result.accepted] == ["XZ07-20-10-11"]


@pytest.mark.parametrize("description", [
    "Norel replacement contactor model XZ07-20-10-11",
    "Norel contacteur equivalent XZ07-20-10-11",
])
def test_deterministic_fallback_keeps_target_references_in_equivalence_wording(description):
    result = CandidateRegistry().ingest(
        [],
        document_with(description, url="https://maker.example/product"),
        requirements(product="Contacteur industriel"),
        target_brand="Norel",
    )

    assert [lead.reference for lead in result.accepted] == ["XZ07-20-10-11"]


@pytest.mark.parametrize("noise", [
    "100-250VAC",
    "norel-contacteur-serie-industrielle-tres-longue-reference-de-catalogue-24v",
    "e9e16115249c6c27c12578610033fd77",
])
def test_deterministic_fallback_rejects_ranges_slugs_and_hashes(noise):
    result = CandidateRegistry().ingest(
        [],
        document_with(
            f"Norel catalogue contacteur : {noise}",
            url="https://maker.example/catalogue",
        ),
        requirements(product="Contacteur industriel"),
        target_brand="Norel",
    )

    assert result.accepted == ()


def test_deterministic_fallback_can_read_reference_from_url_with_brand_in_metadata():
    document = build_discovery_document(
        url="https://maker.example/products/ZX-41-7",
        title="Maker product catalogue",
        snippets=["Official Maker documentation"],
        content="",
        rank=1,
    )

    result = CandidateRegistry().ingest(
        (),
        document,
        requirements(),
        target_brand="Maker",
    )

    assert [(lead.brand, lead.reference, lead.low_confidence) for lead in result.accepted] == [
        ("Maker", "ZX-41-7", True),
    ]
    assert result.accepted[0].occurrences[0].field == "url"


def test_deterministic_fallback_never_scans_content_beyond_discovery_limit():
    document = build_discovery_document(
        url="https://maker.example/products/item",
        title="Maker product catalogue",
        snippets=[],
        content=("ordinary technical text " * 20) + "ZX-41-7",
        rank=1,
        limits=DiscoveryLimits(content_chars=24, total_chars=2_000),
    )

    result = CandidateRegistry().ingest(
        (),
        document,
        requirements(),
        target_brand="Maker",
    )

    assert result.accepted == ()


def test_legacy_discovery_document_without_bounded_fields_caps_prompt_source():
    legacy_document = candidats.DiscoveryDocument(
        url="https://maker.example/products/item",
        title="Maker product catalogue",
        snippets=(),
        content="",
        prompt_source="TITLE: Maker\n" + ("x" * 50_000) + " ZX-41-7",
        rank=1,
        truncated_fields=(),
    )

    result = CandidateRegistry().ingest(
        (),
        legacy_document,
        requirements(),
        target_brand="Maker",
    )

    assert result.accepted == ()


def test_audit_proposals_keeps_only_audited_identities():
    audit = PageAudit(
        page_url="https://maker.example/page",
        candidates=[
            CandidateAudit(
                brand="Maker",
                reference="ZX-41-7",
                criteria=[CriterionAudit(requirement_id="r", requested_value="10 A", status="not_proven")],
            )
        ],
    )

    assert audit_proposals(audit) == [CandidateProposal(brand="Maker", reference="ZX-41-7")]


def test_targeted_page_filter_can_confirm_reference_from_same_url_without_editing_facts():
    candidate = CandidateAudit(
        brand="Maker",
        reference="ZX-41-7",
        criteria=[
            CriterionAudit(
                requirement_id="r",
                requested_value="10 A",
                status="not_proven",
            )
        ],
        limitations=["Current is not stated"],
    )
    audit = PageAudit(
        page_url="https://maker.example/page?utm_source=test",
        candidates=[candidate],
    )
    lead = CandidateLead(
        brand="Maker",
        reference="ZX-41-7",
        canonical_brand="maker",
        canonical_reference="zx417",
        occurrences=(
            CandidateOccurrence(
                url="https://maker.example/page",
                field="snippet",
                fragment="ZX-41-7",
                rank=1,
            ),
        ),
    )

    filtered = candidats.filter_page_audit(
        audit,
        page_url="https://maker.example/page",
        title="Maker product page",
        content="Maker technical data",
        authorized_candidates=[lead],
        targeted=True,
    )

    assert [item.model_dump() for item in filtered.candidates] == [candidate.model_dump()]


def test_targeted_filter_uses_current_hit_metadata_but_page_for_brand_confirmation():
    candidate = CandidateAudit(
        brand="Maker",
        reference="ZX-41-7",
        criteria=[CriterionAudit(
            requirement_id="r",
            requested_value="10 A",
            status="not_proven",
        )],
    )
    audit = PageAudit(
        page_url="https://maker.example/products/ZX-41-7",
        candidates=[candidate],
    )
    old_lead = CandidateLead(
        brand="Maker",
        reference="ZX-41-7",
        canonical_brand="maker",
        canonical_reference="zx417",
        occurrences=(CandidateOccurrence(
            url="https://maker.example/catalogue",
            field="snippet",
            fragment="ZX-41-7",
            rank=1,
        ),),
    )

    for current_document in (
        build_discovery_document(
            url="https://maker.example/products/ZX-41-7",
            title="Technical product data",
            snippets=[],
            content="Maker rated current 10 A",
            rank=1,
        ),
        build_discovery_document(
            url="https://maker.example/products/item",
            title="Technical product data",
            snippets=["Exact model ZX-41-7"],
            content="Maker rated current 10 A",
            rank=1,
        ),
    ):
        filtered = candidats.filter_page_audit(
            audit,
            page_url=current_document.url,
            title="Maker technical data",
            content="Maker rated current 10 A",
            document=current_document,
            authorized_candidates=[old_lead],
            targeted=True,
        )
        assert [item.reference for item in filtered.candidates] == ["ZX-41-7"]

        without_page_brand = candidats.filter_page_audit(
            audit,
            page_url=current_document.url,
            title="Technical data",
            content="Rated current 10 A",
            document=current_document,
            authorized_candidates=[old_lead],
            targeted=True,
        )
        assert without_page_brand.candidates == []


def test_prompt_source_is_bounded_while_content_remains_available_for_later_verification():
    document = build_discovery_document(
        url="https://maker.example/" + "u" * 40,
        title="t" * 8,
        snippets=["s" * 8],
        content="c" * 16,
        rank=1,
        limits=DiscoveryLimits(title_chars=3, snippet_chars=4, content_chars=5, total_chars=20),
    )

    assert document.content == "c" * 16
    assert document.truncated_fields == ("title", "snippet", "content", "url", "total")
    assert len(document.prompt_source) == 20


def test_total_prompt_budget_names_each_otherwise_untruncated_field_it_cuts():
    document = build_discovery_document(
        url="u",
        title="abc",
        snippets=["def"],
        content="ghi",
        rank=1,
        limits=DiscoveryLimits(title_chars=10, snippet_chars=10, content_chars=10, total_chars=9),
    )

    assert document.prompt_source == "URL: u\nTI"
    assert document.truncated_fields == ("title", "snippet", "content", "total")


def test_total_prompt_budget_records_a_url_that_it_cuts():
    document = build_discovery_document(
        url="https://maker.example/" + "u" * 20,
        title="",
        snippets=[],
        content="",
        rank=1,
        limits=DiscoveryLimits(total_chars=10),
    )

    assert document.prompt_source == "URL: https"
    assert document.truncated_fields == ("url", "total")


def test_an_abbreviation_dot_does_not_cut_the_reference_from_its_brand():
    """Rupture visée : `a.c./d.c.` coupait le titre avant la référence.

    Mesure du 2026-08-27 sur un run réel : le titre `Contacteur Norel 3 Poles 9A
    24-60 V a.c./d.c. XZ07201011` perdait `XZ07201011`, parce que le point de
    `d.c.` était lu comme une fin de phrase. La page prouvait pourtant les six
    critères du contacteur cherché, et la mission entière finissait sans
    aucun candidat.
    """
    result = CandidateRegistry().ingest(
        [],
        document_with(
            "Contacteur Norel 3 Poles 9A 24-60 V a.c./d.c. XZ07201011",
            url="https://revendeur.example/p/xz07201011",
        ),
        requirements(product="Contacteur de puissance tripolaire"),
        target_brand="Norel",
    )

    assert [lead.reference for lead in result.accepted] == ["XZ07201011"]


def test_a_real_sentence_end_still_separates_a_competitor_reference():
    """La distinction ne doit pas rouvrir l'attribution d'une référence voisine."""
    result = CandidateRegistry().ingest(
        [],
        document_with(
            "Kessner-Roy contactor 200-C30GK10 vendu seul. Norel catalogue.",
            url="https://revendeur.example/comparaison",
        ),
        requirements(product="Contacteur industriel"),
        target_brand="Norel",
    )

    assert result.accepted == ()
