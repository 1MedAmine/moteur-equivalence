# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from configuration import B2Config
from candidats import CandidateLead
from modeles import Requirement, RequirementSet, SearchHit
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

    return Planner(B2Config(
        api_key="test",
        distributor_domains=(
            "distributeur-a.example",
            "distributeur-b.example",
            "distributeur-c.example",
            "distributeur-d.example",
        ),
    ), call_llm=llm_must_not_run)


def test_two_leads_share_three_exact_queries():
    plan = targeted_planner().plan_targeted_queries(
        targeted_requirements(),
        [lead("Maker", "ZX-41-7"), lead("Maker", "QK-900")],
        set(),
    )

    assert [item.query for item in plan.queries] == [
        "Maker ZX-41-7 fiche produit",
        "Maker QK-900 fiche produit",
        "Maker ZX-41-7 fiche technique pdf",
    ]
    assert [item.candidate_key for item in plan.queries] == [
        ("maker", "zx417"),
        ("maker", "qk900"),
        ("maker", "zx417"),
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
    ]
    assert [item.angle for item in plan.queries] == ["fiche produit"] * 3


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


def test_one_lead_receives_three_stable_angles():
    plan = targeted_planner().plan_targeted_queries(
        targeted_requirements(),
        [lead("Maker", "ZX-41-7")],
        set(),
    )

    assert [item.query for item in plan.queries] == [
        "Maker ZX-41-7 fiche produit",
        "Maker ZX-41-7 fiche technique pdf",
        "Maker ZX-41-7 spécifications techniques",
    ]


def test_wave_three_skips_wave_two_queries_and_uses_the_next_three_angles():
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
    ]


def test_changed_top_two_share_three_unique_queries():
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
    ]
    assert [item.angle for item in plan.queries] == [
        "spécifications techniques",
        "fiche produit",
        "documentation fabricant",
    ]


def test_targeted_plan_contains_exactly_three_unique_outputs():
    plan = targeted_planner().plan_targeted_queries(
        targeted_requirements(),
        [lead("Maker", "ZX-41-7"), lead("Maker", "ZX-41-7")],
        set(),
    )

    assert len(plan.queries) == 3
    assert len({item.query.casefold() for item in plan.queries}) == 3


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

    assert len(plan.queries) == 3
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


def test_query_plan_has_exactly_three_clean_targeted_queries():
    """Mutation détectée : le plan contient moins de quatre angles ou garde l'origine."""
    response = """[
      "contacteur Tersa 9A 24V DC Norel",
      "Norel contacteur 3P bobine 24V DC",
      "Norel contactor 9A DC coil datasheet"
    ]"""
    planner = Planner(B2Config(api_key="test"), call_llm=lambda _: response)

    plan = planner.plan_queries(requirements(), "Norel", [], set())

    assert len(plan.queries) == 3
    assert len({query.casefold() for query in plan.queries}) == 3
    assert all("Norel" in query for query in plan.queries)
    assert all("Tersa" not in query and "Kerion" not in query for query in plan.queries)


def test_query_plan_uses_the_same_three_query_budget_as_a_wave():
    response = """[
      "Norel contacteur 9A 24V DC",
      "Norel contacteur trois poles 24V",
      "Norel contacteur bobine DC datasheet"
    ]"""
    prompts = []
    planner = Planner(
        B2Config(api_key="test"),
        call_llm=lambda prompt: prompts.append(prompt) or response,
    )

    plan = planner.plan_queries(requirements(), "Norel", [], set())

    assert len(plan.queries) == 3
    assert "liste JSON de 3 chaînes" in prompts[0]
    assert "Les 3 angles" in prompts[0]
    assert "quatre" not in prompts[0]


def test_duplicate_or_previous_query_uses_unique_fallback():
    """Une réponse répétée ne doit ni repayer une requête ni arrêter la mission."""
    response = '["Norel contacteur 9A", "Norel contacteur 9A", "Norel bobine 24V"]'
    planner = Planner(B2Config(api_key="test"), call_llm=lambda _: response)

    plan = planner.plan_queries(requirements(), "Norel", [], {"Norel contacteur 9A"})

    assert plan.strategy == "fallback"
    assert len(set(plan.queries)) == 3
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


def test_requirement_extraction_retries_once_after_a_transient_model_503():
    """Une indisponibilité serveur passagère ne doit pas annuler le run."""
    calls = {"count": 0}

    class OpenAIAPIError(Exception):
        """Même nom que l'enveloppe LangChain d'un InternalServerError."""

    class Graph:
        def run(self):
            calls["count"] += 1
            if calls["count"] == 1:
                raise OpenAIAPIError()
            return requirements().model_dump()

    planner = Planner(B2Config(api_key="test"), graph_factory=lambda **_: Graph())

    result = planner.extract_requirements("Contacteur Kerion 9 A bobine 24 V DC")

    assert result.criteria[0].requested_value == "9 A"
    assert calls["count"] == 2
    assert planner.last_requirement_diagnostics == [{
        "stage": "requirement_initial_extraction",
        "path": "$",
        "issue": "temporary_model_unavailable",
        "action": "retried",
    }]


def test_requirement_extraction_survives_two_consecutive_model_503(monkeypatch):
    """Deux surcharges consécutives ne doivent plus faire perdre le run."""
    calls = {"count": 0}
    delays = []

    class OpenAIAPIError(Exception):
        pass

    class Graph:
        def run(self):
            calls["count"] += 1
            if calls["count"] < 3:
                raise OpenAIAPIError()
            return requirements().model_dump()

    monkeypatch.setattr(time, "sleep", delays.append)
    planner = Planner(B2Config(api_key="test"), graph_factory=lambda **_: Graph())

    result = planner.extract_requirements("Contacteur Kerion 9 A bobine 24 V DC")

    assert result.criteria[0].requested_value == "9 A"
    assert calls["count"] == 3
    assert delays == [1.0, 3.0]


def test_direct_llm_call_retries_temporary_503_with_backoff(monkeypatch):
    """La planification directe reprend un appel fournisseur temporairement saturé."""
    calls = {"count": 0}
    delays = []

    class InternalServerError(Exception):
        pass

    class Completions:
        def create(self, **_kwargs):
            calls["count"] += 1
            if calls["count"] < 3:
                raise InternalServerError()
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="OK"))]
            )

    class Client:
        chat = SimpleNamespace(completions=Completions())

    monkeypatch.setattr("openai.OpenAI", lambda **_kwargs: Client())
    monkeypatch.setattr(time, "sleep", delays.append)

    result = Planner(B2Config(api_key="test"))._call_llm("ping")

    assert result == "OK"
    assert calls["count"] == 3
    assert delays == [1.0, 3.0]


def test_direct_llm_call_uses_structured_payload_for_json_contract(monkeypatch):
    """Planification et découverte exigent du JSON, sans canal de réflexion."""
    received = {}

    class Completions:
        def create(self, **kwargs):
            received.update(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="[]"))]
            )

    class Client:
        chat = SimpleNamespace(completions=Completions())

    monkeypatch.setattr("openai.OpenAI", lambda **_kwargs: Client())

    Planner(B2Config(api_key="test"))._call_llm("JSON only")

    assert received["temperature"] == 0.0
    assert received["extra_body"] == {
        "chat_template_kwargs": {"enable_thinking": False}
    }


def test_reference_only_product_is_completed_from_adjacent_literal_source_line():
    extracted = RequirementSet(
        product="6205-2RSH",
        origin_brand="SKF",
        criteria=[Requirement(
            id="bore", label="Bore diameter", requested_value="25 mm",
        )],
    )

    class Graph:
        def run(self):
            return extracted.model_dump()

    planner = Planner(
        B2Config(api_key="test"), graph_factory=lambda **_: Graph(),
    )
    result = planner.extract_requirements(
        "6205-2RSH\nDeep groove ball bearing with seals\n\nBore diameter: 25 mm"
    )

    assert result.product == "Deep groove ball bearing with seals 6205-2RSH"


def test_source_designation_suffix_is_not_a_required_functional_value():
    extracted = RequirementSet(
        product="Deep groove ball bearing AX205-2RSH",
        origin_brand="OriginCo",
        criteria=[Requirement(
            id="sealing", label="Sealing",
            requested_value="2RSH: a seal on both sides",
        )],
    )

    class Graph:
        def run(self):
            return extracted.model_dump()

    planner = Planner(B2Config(api_key="test"), graph_factory=lambda **_: Graph())
    result = planner.extract_requirements(
        "OriginCo AX205-2RSH\n2RSH: a seal on both sides"
    )

    assert result.criteria[0].requested_value == "a seal on both sides"


def test_model_queries_replace_source_suffix_with_its_explained_function():
    source = RequirementSet(
        product="Deep groove ball bearing AX205-2RSH",
        origin_brand="OriginCo",
        criteria=[Requirement(
            id="sealing", label="Sealing",
            requested_value="a seal on both sides",
        )],
    )
    response = (
        '["AX205 bearing 2RSH 25mm", "AX205 deep groove 2RSH datasheet", '
        '"AX205 ball bearing 2RSH product"]'
    )

    plan = Planner(B2Config(api_key="test"), call_llm=lambda _: response).plan_queries(
        source, None, [], set()
    )

    assert all("2RSH" not in query for query in plan.queries)
    assert any("seal on both sides" in query for query in plan.queries)


def test_requirement_prompt_requires_origin_brand_and_range():
    """La marque et la gamme source doivent être disponibles pour les retirer des requêtes."""
    prompt = construire_mission_criteres("Contacteur Kerion Tersa NV1T05")

    assert "origin_brand" in prompt
    assert "marque" in prompt.casefold()
    assert "gamme" in prompt.casefold()
    assert "critical" in prompt


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
["Norel contacteur 9A", "Norel bobine 24V DC", "Norel contacteur trois poles"]
```"""
    planner = Planner(B2Config(api_key="test"), call_llm=lambda _: response)

    plan = planner.plan_queries(requirements(), "Norel", [], set())

    assert plan.strategy == "model"
    assert len(plan.queries) == 3


def test_query_plan_accepts_an_object_with_queries():
    """Un objet JSON enveloppant la liste reste un plan exploitable."""
    response = """{"queries": [
      "Norel contacteur 9A",
      "Norel bobine 24V DC",
      "Norel contacteur trois poles"
    ]}"""
    planner = Planner(B2Config(api_key="test"), call_llm=lambda _: response)

    plan = planner.plan_queries(requirements(), "Norel", [], set())

    assert plan.strategy == "model"
    assert len(plan.queries) == 3


def test_query_plan_retries_once_after_an_invalid_response():
    """La disparition de la relance ferait revenir le PlanningError réel."""
    responses = iter([
        "Je propose plusieurs recherches.",
        '["Norel contacteur 9A", "Norel bobine 24V DC", "Norel trois poles"]',
    ])
    planner = Planner(B2Config(api_key="test"), call_llm=lambda _: next(responses))

    plan = planner.plan_queries(requirements(), "Norel", [], set())

    assert plan.strategy == "retry"
    assert len(plan.queries) == 3


def test_query_plan_falls_back_after_two_invalid_responses():
    """Deux formats invalides doivent produire trois requêtes, jamais une panne globale."""
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
    assert len(plan.queries) == 3
    assert len({query.casefold() for query in plan.queries}) == 3
    assert not {query.casefold() for query in plan.queries} & {
        query.casefold() for query in previous
    }
    assert all("Norel" in query for query in plan.queries)
    assert all("Kerion" not in query and "Tersa" not in query for query in plan.queries)


def test_query_plan_keeps_safe_failure_reason_for_each_attempt():
    calls = {"count": 0}

    def failing_then_invalid(_):
        calls["count"] += 1
        if calls["count"] == 1:
            raise TimeoutError("RAW_PROVIDER_RESPONSE_MUST_NOT_LEAK")
        return "réponse non structurée"

    planner = Planner(B2Config(api_key="test"), call_llm=failing_then_invalid)

    plan = planner.plan_queries(requirements(), "Norel", [], set())

    assert plan.strategy == "fallback"
    assert plan.failures == (
        "tentative 1 : erreur modèle TimeoutError",
        "tentative 2 : réponse invalide — aucune liste JSON valide",
    )
    assert "RAW_PROVIDER_RESPONSE_MUST_NOT_LEAK" not in str(plan.failures)


def test_fallback_uses_safe_product_words_instead_of_generic_placeholder():
    source = RequirementSet(
        product="Deep groove ball bearing 6205-2RSH",
        origin_brand="SKF",
        criteria=[
            Requirement(id="bore", label="Bore diameter", requested_value="25 mm", critical=True),
            Requirement(id="outside", label="Outside diameter", requested_value="52 mm", critical=True),
            Requirement(id="width", label="Width", requested_value="15 mm", critical=True),
        ],
    )
    planner = Planner(
        B2Config(api_key="test"), call_llm=lambda _: "réponse non structurée"
    )

    plan = planner.plan_queries(source, None, [], set())

    assert plan.strategy == "fallback"
    assert all("Deep groove ball bearing" in query for query in plan.queries)
    assert all("produit industriel" not in query for query in plan.queries)
    assert all("SKF" not in query and "6205-2RSH" not in query for query in plan.queries)
    assert all("6205" in query for query in plan.queries)


def test_fallback_removes_a_source_reference_with_a_slash_separator():
    source = RequirementSet(
        product="Memory module KVR32S22S8/16",
        origin_brand="Origin Memory",
        criteria=[
            Requirement(id="capacity", label="Capacity", requested_value="16GB"),
            Requirement(id="type", label="Memory type", requested_value="DDR4"),
        ],
    )

    plan = Planner(
        B2Config(api_key="test"), call_llm=lambda _: "invalid response"
    ).plan_queries(source, None, [], set())

    assert all("KVR32S22S8/16" not in query for query in plan.queries)


def test_fallback_does_not_repeat_technical_values_already_in_product_label():
    source = RequirementSet(
        product="Memory module 16GB DDR4-3200 CL22",
        criteria=[
            Requirement(id="capacity", label="Capacity", requested_value="16GB"),
            Requirement(id="type", label="Memory type", requested_value="DDR4-3200"),
            Requirement(id="latency", label="Latency", requested_value="CL22"),
        ],
    )

    plan = Planner(
        B2Config(api_key="test"), call_llm=lambda _: "invalid response"
    ).plan_queries(source, None, [], set())

    first_query = plan.queries[0]
    assert first_query.casefold().count("16gb") == 1
    assert first_query.casefold().count("ddr4-3200") == 1
    assert first_query.casefold().count("cl22") == 1


def test_fallback_spreads_critical_discriminants_across_early_queries():
    source = RequirementSet(
        product="Deep groove ball bearing 6205-2RSH",
        origin_brand="SKF",
        criteria=[
            Requirement(id="bore", label="Bore diameter", requested_value="25 mm", critical=True),
            Requirement(id="outside", label="Outside diameter", requested_value="52 mm", critical=True),
            Requirement(id="width", label="Width", requested_value="15 mm", critical=True),
            Requirement(id="rows", label="Number of rows", requested_value="1", critical=True),
            Requirement(id="bore_type", label="Bore type", requested_value="Cylindrical", critical=True),
            Requirement(id="sealing", label="Sealing", requested_value="Seal on both sides", critical=True),
            Requirement(id="seal_type", label="Sealing type", requested_value="Contact", critical=True),
        ],
    )
    planner = Planner(
        B2Config(api_key="test"), call_llm=lambda _: "réponse non structurée"
    )

    plan = planner.plan_queries(source, None, [], set())

    assert any("Sealing Seal on both sides" in query for query in plan.queries[:3])
    assert any("Sealing type Contact" in query for query in plan.queries[:3])


def test_model_plan_reserves_one_query_for_the_source_reference_family():
    source = RequirementSet(
        product="Deep groove ball bearing 6205-2RSH",
        origin_brand="SKF",
        criteria=[
            Requirement(id="bore", label="Bore diameter", requested_value="25 mm"),
            Requirement(id="outside", label="Outside diameter", requested_value="52 mm"),
            Requirement(id="width", label="Width", requested_value="15 mm"),
        ],
    )
    response = (
        '["sealed bearing dimensions", "bearing technical sheet", '
        '"bearing manufacturer pdf"]'
    )
    planner = Planner(B2Config(api_key="test"), call_llm=lambda _: response)

    plan = planner.plan_queries(source, None, [], set())

    assert plan.strategy == "model"
    family_queries = [query for query in plan.queries if "6205" in query]
    assert len(family_queries) == 3
    assert all("6205-2RSH" not in query for query in plan.queries)


def test_model_queries_replace_the_exact_source_reference_with_its_family():
    source = RequirementSet(
        product="Deep groove ball bearing 6205-2RSH",
        origin_brand="SKF",
        criteria=[Requirement(id="bore", label="Bore", requested_value="25 mm")],
    )
    response = (
        '["6205-2RSH bearing equivalent", "6205-2RSH technical sheet", '
        '"sealed bearing 25 mm", "bearing manufacturer pdf"]'
    )

    plan = Planner(B2Config(api_key="test"), call_llm=lambda _: response).plan_queries(
        source, None, [], set()
    )

    assert all("6205-2RSH" not in query for query in plan.queries)
    assert any("6205" in query for query in plan.queries[:3])


def test_v4_query_plan_keeps_three_compact_technical_angles():
    """Une sortie V4 conforme ne doit pas être remplacée par une requête longue."""
    source = RequirementSet(
        product="Deep groove ball bearing 6205-2RSH",
        origin_brand="SKF",
        criteria=[
            Requirement(id="bore", label="Bore diameter", requested_value="25 mm"),
            Requirement(id="outside", label="Outside diameter", requested_value="52 mm"),
            Requirement(id="width", label="Width", requested_value="15 mm"),
            Requirement(
                id="sealing",
                label="Sealing",
                requested_value="seals on both sides",
                critical=True,
            ),
        ],
    )
    response = json.dumps({"queries": [
        "6205 25x52x15 deep groove ball bearing 2RS",
        "6205 25x52x15 rubber sealed both sides bearing",
        "deep groove bearing 25x52x15 double seal product",
    ]})

    plan = Planner(B2Config(api_key="test"), call_llm=lambda _: response).plan_queries(
        source, None, [], set()
    )

    assert plan.strategy == "model"
    assert plan.queries == (
        "6205 25x52x15 deep groove ball bearing 2RS",
        "6205 25x52x15 rubber sealed both sides bearing",
        "6205 deep groove bearing 25x52x15 double seal product",
    )


def test_v4_query_prompt_requires_short_source_free_queries():
    prompts: list[str] = []
    response = json.dumps({"queries": [
        "6205 25x52x15 deep groove ball bearing 2RS",
        "6205 25x52x15 rubber sealed both sides bearing",
        "deep groove bearing 25x52x15 double seal product",
    ]})
    source = RequirementSet(
        product="Deep groove ball bearing 6205-2RSH",
        origin_brand="SKF",
        criteria=[Requirement(id="bore", label="Bore diameter", requested_value="25 mm")],
    )

    Planner(
        B2Config(api_key="test"),
        call_llm=lambda prompt: prompts.append(prompt) or response,
    ).plan_queries(source, None, [], set())

    assert "6 à 14 mots" in prompts[0]
    assert "N’invente aucune marque" in prompts[0]
    assert "référence source complète" in prompts[0]


def test_open_query_prompt_requests_a_french_catalogue_angle_for_english_source():
    prompts: list[str] = []
    source = RequirementSet(
        product="Deep groove ball bearing 6205-2RSH",
        origin_brand="OriginCo",
        criteria=[
            Requirement(id="bore", label="Bore diameter", requested_value="25 mm"),
            Requirement(id="seal", label="Sealing", requested_value="seals on both sides"),
        ],
    )
    response = json.dumps({"queries": [
        "6205 deep groove ball bearing sealed both sides",
        "6205 roulement à billes joint des deux côtés",
        "6205 bearing 25mm sealed product datasheet",
    ]})

    Planner(
        B2Config(api_key="test"),
        call_llm=lambda prompt: prompts.append(prompt) or response,
    ).plan_queries(source, None, [], set())

    assert "en français" in prompts[0]
    assert "sans inventer" in prompts[0]


def test_query_prompt_is_generic_and_targets_individual_product_pages():
    prompts: list[str] = []
    source = RequirementSet(
        product="Servo drive OriginDrive SD-40X",
        origin_brand="OriginDrive",
        criteria=[
            Requirement(id="power", label="Rated power", requested_value="4 kW"),
            Requirement(id="supply", label="Supply voltage", requested_value="400 V"),
            Requirement(id="bus", label="Communication bus", requested_value="EtherCAT"),
        ],
    )
    response = json.dumps({"queries": [
        "servo drive 4kW 400V EtherCAT product page",
        "servo drive 4kW EtherCAT technical specifications",
        "servo drive 400V EtherCAT manufacturer datasheet",
    ]})

    Planner(
        B2Config(api_key="test"),
        call_llm=lambda prompt: prompts.append(prompt) or response,
    ).plan_queries(source, None, [], set())

    prompt = prompts[0]
    assert "25x52x15" not in prompt
    assert "Compacte seulement les valeurs présentes" in prompt
    assert "Chaque requête doit contenir le type de produit" in prompt
    assert "Vise 6 à 10 mots" in prompt
    assert "nom générique le plus court" in prompt
    assert "synonyme technique courant" in prompt
    assert "dans chaque requête" in prompt
    assert "notation technique générique" in prompt
    assert "page d’un seul produit identifiable" in prompt
    assert "page de catégorie" in prompt
    assert "produit industriel" not in prompt
    assert "au plus 4 valeurs techniques" in prompt
    assert "détails de fabrication" in prompt


def test_search_candidate_discovery_keeps_only_literal_indexed_identities():
    prompts: list[str] = []
    response = json.dumps({"candidates": [
        {"result_index": 0, "brand": "Maker", "reference": "ZX-417"},
        {"result_index": 1, "brand": "Invented", "reference": "FAKE-99"},
    ]})
    hits = [
        SearchHit(
            url="https://distributor.example/zx-417",
            title="Maker ZX-417 product page",
            snippet="Maker ZX-417 technical data",
            engine="bing",
            rank=1,
        ),
        SearchHit(
            url="https://distributor.example/qk-900",
            title="Other QK-900 product page",
            snippet="Other QK-900 technical data",
            engine="bing",
            rank=2,
        ),
    ]
    planner = Planner(
        B2Config(api_key="test"),
        call_llm=lambda prompt: prompts.append(prompt) or response,
    )

    hints = planner.discover_search_candidates(
        requirements(), hits, target_brand=None,
    )

    assert [(item.result_index, item.brand, item.reference) for item in hints] == [
        (0, "Maker", "ZX-417")
    ]
    assert "même résultat indexé" in prompts[0]
    assert "au maximum 4" in prompts[0]
    assert "Critères techniques demandés" in prompts[0]


def test_v4_query_plan_retries_then_falls_back_when_queries_are_too_long():
    response = json.dumps({"queries": [
        "6205 deep groove ball bearing 25x52x15 rubber sealed both sides alternative manufacturer technical product datasheet catalogue",
        "6205 deep groove ball bearing 25x52x15 rubber sealed both sides alternative manufacturer technical product datasheet distributor",
        "6205 deep groove ball bearing 25x52x15 rubber sealed both sides alternative manufacturer technical product datasheet supplier",
    ]})
    source = RequirementSet(
        product="Deep groove ball bearing 6205-2RSH",
        origin_brand="SKF",
        criteria=[Requirement(id="bore", label="Bore diameter", requested_value="25 mm")],
    )

    plan = Planner(B2Config(api_key="test"), call_llm=lambda _: response).plan_queries(
        source, None, [], set()
    )

    assert plan.strategy == "fallback"
    assert plan.failures == (
        "tentative 1 : réponse invalide — requêtes trop longues",
        "tentative 2 : réponse invalide — requêtes trop longues",
    )


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
