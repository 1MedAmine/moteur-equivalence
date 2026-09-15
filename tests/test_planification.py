# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from configuration import B2Config
from candidats import CandidateLead
from modeles import Requirement, RequirementSet
from planification import Planner
from mission import construire_mission_criteres


def requirements():
    return RequirementSet(
        product="Kerion Tersa NV1T05",
        origin_brand="Kerion Electric; Tersa D",
        criteria=[
            Requirement(id="courant", label="Courant", requested_value="9 A", critical=True),
            Requirement(id="bobine", label="Bobine", requested_value="24 V DC", critical=True),
        ],
    )


def targeted_requirements():
    return RequirementSet(
        product="OriginCo OR-100",
        origin_brand="OriginCo",
        criteria=[
            Requirement(id="current", label="Current", requested_value="10 A", critical=True),
        ],
    )


def lead(brand: str, reference: str) -> CandidateLead:
    return CandidateLead(
        brand=brand,
        reference=reference,
        canonical_brand="".join(character for character in brand.casefold() if character.isalnum()),
        canonical_reference="".join(
            character for character in reference.casefold() if character.isalnum()
        ),
        occurrences=(),
    )


def targeted_planner() -> Planner:
    def llm_must_not_run(_: str) -> str:
        raise AssertionError("targeted planning must be deterministic")

    return Planner(B2Config(api_key="test"), call_llm=llm_must_not_run)


def test_two_leads_receive_two_exact_queries_each():
    plan = targeted_planner().plan_targeted_queries(
        targeted_requirements(),
        [lead("Maker", "ZX-41-7"), lead("Maker", "QK-900")],
        set(),
    )

    assert [item.query for item in plan.queries] == [
        "Maker ZX-41-7 fiche produit",
        "Maker QK-900 fiche produit",
        "Maker ZX-41-7 fiche technique pdf",
        "Maker QK-900 fiche technique pdf",
    ]
    assert [item.candidate_key for item in plan.queries] == [
        ("maker", "zx417"),
        ("maker", "qk900"),
        ("maker", "zx417"),
        ("maker", "qk900"),
    ]


def test_four_leads_each_receive_an_initial_independent_query():
    """Les trois requêtes d'une vague couvrent trois pistes distinctes."""
    plan = targeted_planner().plan_targeted_queries(
        targeted_requirements(),
        [
            lead("Maker", "ZX-41-7"),
            lead("Maker", "QK-900"),
            lead("Maker", "RT-200"),
            lead("Maker", "LM-400"),
        ],
        set(),
    )

    assert [item.candidate_key for item in plan.queries] == [
        ("maker", "zx417"),
        ("maker", "qk900"),
        ("maker", "rt200"),
        ("maker", "lm400"),
    ]
    assert [item.angle for item in plan.queries] == ["fiche produit"] * 4


def test_distributor_queries_target_each_configured_domain_by_reference_only():
    """Une piste Norel/capteur est cherchable chez tout distributeur configure."""
    queries = targeted_planner().plan_distributor_queries(
        targeted_requirements(),
        [lead("Norel", "4KBL103001R8110")],
        set(),
    )

    assert [item.query for item in queries] == [
        "site:distributeur-a.example 4KBL103001R8110",
        "site:distributeur-b.example 4KBL103001R8110",
        "site:distributeur-c.example 4KBL103001R8110",
        "site:distributeur-d.example 4KBL103001R8110",
    ]
    assert {item.candidate_key for item in queries} == {("norel", "4kbl103001r8110")}


def test_one_lead_receives_four_stable_angles():
    plan = targeted_planner().plan_targeted_queries(
        targeted_requirements(),
        [lead("Maker", "ZX-41-7")],
        set(),
    )

    assert [item.query for item in plan.queries] == [
        "Maker ZX-41-7 fiche produit",
        "Maker ZX-41-7 fiche technique pdf",
        "Maker ZX-41-7 spécifications techniques",
        "Maker ZX-41-7 documentation fabricant",
    ]


def test_wave_three_skips_wave_two_queries_and_uses_the_next_four_angles():
    previous = {
        "Maker ZX-41-7 fiche produit",
        "Maker ZX-41-7 fiche technique pdf",
        "Maker ZX-41-7 spécifications techniques",
        "Maker ZX-41-7 documentation fabricant",
    }

    plan = targeted_planner().plan_targeted_queries(
        targeted_requirements(),
        [lead("Maker", "ZX-41-7")],
        previous,
    )

    assert [item.query for item in plan.queries] == [
        "Maker ZX-41-7 manuel technique pdf",
        "Maker ZX-41-7 catalogue produit pdf",
        "Maker ZX-41-7 données électriques",
        "Maker ZX-41-7 caractéristiques mécaniques",
    ]


def test_changed_top_two_still_receive_two_unique_queries_each():
    previous = {
        "Maker OLD-10 fiche produit",
        "Maker QK-900 fiche produit",
        "Maker OLD-10 fiche technique pdf",
        "Maker QK-900 fiche technique pdf",
    }

    plan = targeted_planner().plan_targeted_queries(
        targeted_requirements(),
        [lead("Maker", "QK-900"), lead("Maker", "NEW-20")],
        previous,
    )

    assert [item.candidate_key for item in plan.queries] == [
        ("maker", "qk900"),
        ("maker", "new20"),
        ("maker", "qk900"),
        ("maker", "new20"),
    ]
    assert [item.angle for item in plan.queries] == [
        "spécifications techniques",
        "fiche produit",
        "documentation fabricant",
        "fiche technique pdf",
    ]


def test_targeted_plan_contains_exactly_four_unique_outputs():
    plan = targeted_planner().plan_targeted_queries(
        targeted_requirements(),
        [lead("Maker", "ZX-41-7"), lead("Maker", "ZX-41-7")],
        set(),
    )

    assert len(plan.queries) == 4
    assert len({item.query.casefold() for item in plan.queries}) == 4


def test_targeted_plan_never_adds_the_origin_identity():
    plan = targeted_planner().plan_targeted_queries(
        targeted_requirements(),
        [lead("Maker", "ZX-41-7")],
        set(),
    )

    assert all("OriginCo" not in item.query for item in plan.queries)
    assert all("OR-100" not in item.query for item in plan.queries)


def test_targeted_plan_filters_a_structured_origin_lead_before_planning():
    source = RequirementSet(
        product="SourceCo AlphaLine SRC-100 motor starter",
        origin_brand=(
            "manufacturer: SourceCo; range: AlphaLine; reference: SRC-100"
        ),
        criteria=[
            Requirement(id="current", label="Current", requested_value="10 A")
        ],
    )

    plan = targeted_planner().plan_targeted_queries(
        source,
        [lead("SourceCo", "SRC-100"), lead("Maker", "ZX-41-7")],
        set(),
    )

    assert len(plan.queries) == 4
    assert {item.candidate_key for item in plan.queries} == {("maker", "zx417")}
    assert all("SourceCo" not in item.query for item in plan.queries)
    assert all("SRC-100" not in item.query for item in plan.queries)


def test_targeted_plan_excludes_unicode_separator_variants_of_origin_reference():
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
        plan = targeted_planner().plan_targeted_queries(
            source,
            [
                lead("DifferentMaker", candidate_reference),
                lead("Maker", "ALT-900"),
            ],
            set(),
        )

        assert {item.candidate_key for item in plan.queries} == {
            ("maker", "alt900")
        }


def test_query_plan_has_exactly_four_clean_targeted_queries():
    """Mutation détectée : le plan contient moins de quatre angles ou garde l'origine."""
    response = """[
      "contacteur Tersa 9A 24V DC Norel",
      "Norel contacteur 3P bobine 24V DC",
      "Norel contactor 9A DC coil datasheet",
      "Norel contacteur industriel trois poles 24V"
    ]"""
    planner = Planner(B2Config(api_key="test"), call_llm=lambda _: response)

    plan = planner.plan_queries(requirements(), "Norel", [], set())

    assert len(plan.queries) == 4
    assert len({query.casefold() for query in plan.queries}) == 4
    assert all("Norel" in query for query in plan.queries)
    assert all("Tersa" not in query and "Kerion" not in query for query in plan.queries)


def test_duplicate_or_previous_query_uses_unique_fallback():
    """Une réponse répétée ne doit ni repayer une requête ni arrêter la mission."""
    response = '["Norel contacteur 9A", "Norel contacteur 9A", "Norel bobine 24V", "Norel trois poles"]'
    planner = Planner(B2Config(api_key="test"), call_llm=lambda _: response)

    plan = planner.plan_queries(requirements(), "Norel", [], {"Norel contacteur 9A"})

    assert plan.strategy == "fallback"
    assert len(set(plan.queries)) == 4
    assert "Norel contacteur 9A" not in plan.queries


def test_requirement_extraction_is_owned_by_scrapegraphai():
    """Mutation détectée : le jeu immuable est fabriqué par une heuristique libre."""
    captured = {}

    class Graph:
        def run(self):
            return requirements().model_dump()

    def graph_factory(**kwargs):
        captured.update(kwargs)
        return Graph()

    planner = Planner(B2Config(api_key="test"), graph_factory=graph_factory)
    result = planner.extract_requirements("Contacteur Kerion 9 A bobine 24 V DC")

    assert result.criteria[0].requested_value == "9 A"
    assert captured["source"] == "Contacteur Kerion 9 A bobine 24 V DC"
    assert captured["schema"].__name__ == "RequirementSetEnvelope"


def test_requirement_prompt_requires_origin_brand_and_range():
    """La marque et la gamme source doivent être disponibles pour les retirer des requêtes."""
    prompt = construire_mission_criteres("Contacteur Kerion Tersa NV1T05")

    assert "origin_brand" in prompt
    assert "marque" in prompt.casefold()
    assert "gamme" in prompt.casefold()


def test_requirement_prompt_uses_filename_only_to_select_one_literal_product():
    """Rupture visée : une fiche multi-produit fusionne Z-TRH et Z-MAG."""
    prompt = construire_mission_criteres(
        "Tableau comparatif Z-TRH et Z-MAG",
        source_hint="belvia_z-trh.pdf",
    )

    assert "belvia_z-trh.pdf" in prompt
    assert "un seul produit" in prompt.casefold()
    assert "plusieurs colonnes" in prompt.casefold()
    assert "ignorer" in prompt.casefold()


def test_query_plan_accepts_a_markdown_json_fence():
    """Une clôture Markdown courante ne doit pas arrêter la mission."""
    response = """```json
["Norel contacteur 9A", "Norel bobine 24V DC", "Norel contacteur trois poles", "Norel fiche technique contacteur"]
```"""
    planner = Planner(B2Config(api_key="test"), call_llm=lambda _: response)

    plan = planner.plan_queries(requirements(), "Norel", [], set())

    assert plan.strategy == "model"
    assert len(plan.queries) == 4


def test_query_plan_accepts_an_object_with_queries():
    """Un objet JSON enveloppant la liste reste un plan exploitable."""
    response = """{"queries": [
      "Norel contacteur 9A",
      "Norel bobine 24V DC",
      "Norel contacteur trois poles",
      "Norel fiche technique contacteur"
    ]}"""
    planner = Planner(B2Config(api_key="test"), call_llm=lambda _: response)

    plan = planner.plan_queries(requirements(), "Norel", [], set())

    assert plan.strategy == "model"
    assert len(plan.queries) == 4


def test_query_plan_retries_once_after_an_invalid_response():
    """La disparition de la relance ferait revenir le PlanningError réel."""
    responses = iter([
        "Je propose plusieurs recherches.",
        '["Norel contacteur 9A", "Norel bobine 24V DC", "Norel trois poles", "Norel datasheet contacteur"]',
    ])
    planner = Planner(B2Config(api_key="test"), call_llm=lambda _: next(responses))

    plan = planner.plan_queries(requirements(), "Norel", [], set())

    assert plan.strategy == "retry"
    assert len(plan.queries) == 4


def test_query_plan_falls_back_after_two_invalid_responses():
    """Deux formats invalides doivent produire quatre requêtes, jamais une panne globale."""
    calls = {"count": 0}

    def invalid(_):
        calls["count"] += 1
        return "réponse non structurée"

    previous = {
        "Norel contacteur 9 A 24 V DC fiche produit",
        "Norel contacteur 9 A 24 V DC caractéristiques techniques",
    }
    planner = Planner(B2Config(api_key="test"), call_llm=invalid)

    plan = planner.plan_queries(requirements(), "Norel", ["Bobine"], previous)

    assert calls["count"] == 2
    assert plan.strategy == "fallback"
    assert len(plan.queries) == 4
    assert len({query.casefold() for query in plan.queries}) == 4
    assert not {query.casefold() for query in plan.queries} & {
        query.casefold() for query in previous
    }
    assert all("Norel" in query for query in plan.queries)
    assert all("Kerion" not in query and "Tersa" not in query for query in plan.queries)


def test_fallback_never_reuses_the_origin_reference_as_a_criterion():
    """Le secours ne doit pas rechercher le produit d'origine par sa référence."""
    source = RequirementSet(
        product="Kerion Tersa NV1T05",
        origin_brand="Kerion Electric; Tersa D",
        criteria=[
            Requirement(
                id="reference",
                label="Référence fabricant",
                requested_value="NV1T05BD",
                critical=True,
            ),
            Requirement(id="courant", label="Courant", requested_value="9 A", critical=True),
            Requirement(id="bobine", label="Bobine", requested_value="24 V DC", critical=True),
        ],
    )
    planner = Planner(
        B2Config(api_key="test"), call_llm=lambda _: "réponse non structurée"
    )

    plan = planner.plan_queries(source, "Norel", [], set())

    assert plan.strategy == "fallback"
    assert all("NV1T05" not in query for query in plan.queries)
