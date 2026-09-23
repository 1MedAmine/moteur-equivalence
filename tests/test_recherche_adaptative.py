# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analyse import PageAnalysis
from configuration import MAX_QUERIES_PER_WAVE, B2Config
from modeles import (
    CandidateAudit,
    CriterionAudit,
    PageAudit,
    Requirement,
    RequirementSet,
    SearchAttempt,
    SearchBatch,
    SearchHit,
    SourceProof,
)
from planification import QueryPlan, TargetedQuery, TargetedQueryPlan
from recherche_adaptative import (
    MAX_LOGICAL_QUERIES,
    MAX_PAGES_OPENED,
    AdaptiveResearch,
    HitContext,
    _hit_matches_context,
)
from scraping import PageContent
from rejeu import replay_corpus


def test_sparse_search_result_without_numeric_query_anchor_is_rejected():
    context = HitContext(
        wave=1,
        query="Deep groove ball bearing 6205 technical sheet",
        candidate_keys=(),
        hit=SearchHit(
            url="https://sports.example/horse-racing/live",
            title="Live racing",
            snippet="",
            engine="test",
            rank=1,
        ),
    )

    assert not _hit_matches_context(context)


def test_sparse_search_result_with_numeric_query_anchor_is_kept():
    context = HitContext(
        wave=1,
        query="Deep groove ball bearing 6205 technical sheet",
        candidate_keys=(),
        hit=SearchHit(
            url="https://catalog.example/products/6205",
            title="",
            snippet="",
            engine="test",
            rank=1,
        ),
    )

    assert _hit_matches_context(context)


def test_generic_bearing_result_without_numeric_family_does_not_consume_a_page():
    context = HitContext(
        wave=1,
        query="6205 deep groove ball bearing sealed both sides",
        candidate_keys=(),
        hit=SearchHit(
            url="https://catalog.example/general-bearings",
            title="Deep groove ball bearing sealed both sides catalog",
            snippet="General bearing dimensions",
            engine="test",
            rank=1,
        ),
    )

    assert not _hit_matches_context(context)


#: Une phrase reelle par indice de critere placeholder (r1..r4), pour que la
#: preuve partagee de `candidate()` enonce ce qu'elle est censee prouver.
_PLACEHOLDER_PHRASES = (
    "Tension de commande 24 V DC",
    "Trois pôles",
    "Courant 9 A",
    "Fréquence 50 Hz",
)
_PLACEHOLDER_VALUES = ("24 V DC", "Trois pôles", "9 A", "50 Hz")


def requirements():
    return RequirementSet(
        product="Produit source",
        criteria=[
            Requirement(
                id=f"r{i}", label=f"Critère {i}",
                requested_value=_PLACEHOLDER_VALUES[i - 1], critical=True,
            )
            for i in range(1, 5)
        ],
    )


class FakePlanner:
    def __init__(self):
        self.wave = 0
        self.targeted_wave = 0

    def extract_requirements(self, fiche):
        return requirements()

    def plan_queries(self, requirement_set, target_brand, missing, previous):
        self.wave += 1
        return QueryPlan(queries=tuple(
            f"Norel requête vague {self.wave} angle {i}" for i in range(1, 5)
        ))

    def plan_targeted_queries(self, requirement_set, candidates, previous):
        self.targeted_wave += 1
        lead = candidates[0]
        return TargetedQueryPlan(queries=tuple(
            TargetedQuery(
                query=(
                    f"{lead.brand} {lead.reference} preuve "
                    f"{self.targeted_wave} angle {i}"
                ),
                candidate_key=lead.key,
                angle=f"angle {i}",
            )
            for i in range(1, 5)
        ))


class FakeGateway:
    def __init__(self):
        self.calls = []

    def search(self, query):
        self.calls.append(query)
        call = len(self.calls)
        hits = [SearchHit(
            url=f"https://new.norel.example/w{(call - 1) // 4 + 1}/p{index}",
            title=f"Produit {index}",
            engine="bing",
            rank=index,
        ) for index in range(1, 13)]
        return SearchBatch(
            query=query,
            hits=hits,
            attempts=[SearchAttempt(engine="bing", status="ok", result_count=12)],
        )


def _phrases_for(statuses):
    """Une phrase par statut qui a besoin d'une preuve, jamais pour `not_proven`.

    Une phrase laissee pour un critere volontairement non prouve fuirait sur
    la page simulee et se ferait mecaniquement "retrouver" par le
    reaudit des pages officielles — ce n'est pas ce qu'un scenario `not_proven`
    verifie.
    """
    return [
        _PLACEHOLDER_PHRASES[i] for i, status in enumerate(statuses)
        if status != "not_proven"
    ]


def _page_content(phrases):
    return "Norel REF-1 REF-INVENTEE. " + (". ".join(phrases) + ". ") * 8


class FakeFetcher:
    def fetch(self, url):
        # Contenu minimal par defaut : une page non explicitement couverte
        # par un test (les autres resultats de recherche d'une meme vague,
        # notamment) ne doit porter que la premiere phrase, jamais celle
        # d'un critere qu'un scenario precis veut laisser `not_proven` —
        # sans quoi le reaudit des pages officielles la retrouverait ailleurs
        # que sur la page que le test construit lui-meme.
        content = _page_content(_PLACEHOLDER_PHRASES[:1])
        return PageContent(url, content, "Norel REF-1", "scrapling")


def candidate(statuses, url):
    evidence = SourceProof(
        url=url,
        excerpt=". ".join(_phrases_for(statuses)),
        type="web_officiel",
    )
    return CandidateAudit(
        brand="Norel",
        reference="REF-1",
        criteria=[CriterionAudit(
            requirement_id=f"r{i}",
            requested_value=_PLACEHOLDER_VALUES[i - 1],
            observed_value=_PLACEHOLDER_VALUES[i - 1],
            status=status,
            proofs=[evidence] if status != "not_proven" else [],
        ) for i, status in enumerate(statuses, start=1)],
    )


def analyzer_sequence(statuses_by_wave):
    calls = {"count": 0}

    def analyze(pages, requirement_set, target_brand, graph_config, **kwargs):
        index = calls["count"]
        calls["count"] += 1
        # Un candidat complet n'interrompt plus la mission : les vagues
        # suivantes appellent encore l'analyseur. Au-dela de la sequence
        # decrite, elles n'apportent simplement aucun audit.
        statuses = (
            statuses_by_wave[index] if index < len(statuses_by_wave) else None
        )
        if statuses is None:
            return [], []
        page = pages[0]
        audit = PageAudit(page_url=page.url, candidates=[candidate(statuses, page.url)])
        # Le contenu simule ne porte que ce que cette vague prouve : sinon la
        # page contiendrait aussi la phrase d'un critere volontairement
        # `not_proven`, laissant le reaudit des pages officielles la retrouver
        # mecaniquement et contredire le scenario teste.
        content = _page_content(_phrases_for(statuses))
        return [PageAnalysis(page.url, content, audit)], []

    return analyze


def research(analyzer):
    return AdaptiveResearch(
        config=B2Config(api_key="test"),
        planner=FakePlanner(),
        gateway=FakeGateway(),
        fetcher=FakeFetcher(),
        analyze_pages=analyzer,
        graph_config={},
    )


def test_four_waves_stay_within_three_queries_and_the_global_budget():
    """Mutation détectée : le budget global de douze requêtes dérive.

    Quatre vagues de trois plutôt que trois de quatre : le plafond global reste
    douze, mais un tour demeure disponible pour consommer ce que l'audit de la
    vague précédente vient de découvrir.

    La deuxième vague n'envoie que deux requêtes : elle cible l'unique piste
    découverte, dont le quota d'angles vaut deux. Une vague ne comble pas ce
    reliquat — la répétition sur une même piste est justement ce que le quota
    protège.
    """
    outcome = research(analyzer_sequence([None, None, None, None])).run("fiche", "Norel")
    par_vague = [item["query_count"] for item in outcome.diagnostics.wave_details]

    assert outcome.status == "not_resolved"
    assert outcome.diagnostics.waves == 4
    assert par_vague == [3, 2, 3, 3]
    assert all(nombre <= MAX_QUERIES_PER_WAVE for nombre in par_vague)
    assert outcome.diagnostics.logical_queries == sum(par_vague)
    assert outcome.diagnostics.logical_queries <= MAX_LOGICAL_QUERIES
    assert outcome.diagnostics.engine_calls == sum(par_vague)
    assert len(set(outcome.diagnostics.queries)) == sum(par_vague)


def test_requirement_validation_diagnostics_reach_the_research_outcome():
    class DiagnosticPlanner(FakePlanner):
        last_requirement_diagnostics = [
            {
                "stage": "requirement_initial_extraction",
                "path": "criteria[1].requested_value",
                "issue": "invalid_item",
                "action": "discarded",
            },
            {
                "stage": "RAW_MODEL_RESPONSE_SENTINEL",
                "path": "RAW_MODEL_RESPONSE_SENTINEL",
                "issue": "RAW_MODEL_RESPONSE_SENTINEL",
                "action": "RAW_MODEL_RESPONSE_SENTINEL",
            },
        ]

    service = AdaptiveResearch(
        config=B2Config(api_key="test"),
        planner=DiagnosticPlanner(),
        gateway=FakeGateway(),
        fetcher=FakeFetcher(),
        analyze_pages=analyzer_sequence([None, None, None, None]),
        graph_config={},
    )

    outcome = service.run("fiche", "Norel")

    assert outcome.diagnostics.requirement_validation == [{
        "stage": "requirement_initial_extraction",
        "path": "criteria[1].requested_value",
        "issue": "invalid_item",
        "action": "discarded",
    }]
    assert "RAW_MODEL_RESPONSE_SENTINEL" not in str(
        outcome.diagnostics.requirement_validation
    )


def test_page_audit_validation_diagnostics_reach_the_research_outcome():
    calls = {"count": 0}

    def analyze(pages, requirement_set, target_brand, graph_config, **kwargs):
        calls["count"] += 1
        if calls["count"] != 1:
            return [], []
        page = pages[0]
        return [PageAnalysis(
            page_url=page.url,
            content=page.content,
            audit=PageAudit(page_url=page.url),
            validation_diagnostics=({
                "stage": "page_audit",
                "path": "candidates[0].criteria[2].status",
                "issue": "invalid_item",
                "action": "discarded",
            },),
        )], []

    outcome = research(analyze).run("fiche", "Norel")

    assert outcome.diagnostics.page_audit_validation == [{
        "stage": "page_audit",
        "path": "candidates[0].criteria[2].status",
        "issue": "invalid_item",
        "action": "discarded",
    }]


def test_page_level_rate_limits_without_business_result_are_service_unavailable():
    """Rupture visée : douze refus LLM sont présentés comme aucun équivalent."""
    def rate_limited(*args, **kwargs):
        return [], [
            "Page non exploitée (https://maker.example/x) : RateLimitError "
            "[mode=audit, source=contenu, 100 caractères récupérés]."
        ]

    outcome = research(rate_limited).run("fiche", "Norel")

    assert outcome.status == "service_unavailable"
    assert outcome.diagnostics.llm_rate_limit_count == 4


def test_proven_result_stays_prioritary_over_page_level_rate_limit():
    """Rupture visée : un 429 secondaire efface un candidat déjà prouvé."""
    def proven_with_rate_limit(pages, requirement_set, target_brand, graph_config, **kwargs):
        page = pages[0]
        audit = PageAudit(
            page_url=page.url,
            candidates=[candidate(["proven"] * 4, page.url)],
        )
        content = _page_content(_phrases_for(["proven"] * 4))
        return [PageAnalysis(page.url, content, audit)], [
            "Page non exploitée (https://maker.example/x) : RateLimitError "
            "[mode=audit, source=contenu, 100 caractères récupérés]."
        ]

    outcome = research(proven_with_rate_limit).run("fiche", "Norel")

    assert outcome.status == "complete"
    assert outcome.evaluation is not None


def test_complete_candidate_keeps_collecting_within_its_budget():
    """Mutation détectée : après 100 %, B2 franchit un plafond ou se renomme.

    Un candidat complet n'interrompt plus la mission : les vagues restantes
    servent à collecter les autres propositions admissibles. Ce qui a conclu
    la mission reste l'équivalent prouvé — c'est aussi ce que `rejeu.py` rend
    sur un corpus `complete`, et les deux chemins doivent concorder.
    """
    outcome = research(analyzer_sequence([["proven"] * 4])).run("fiche", "Norel")

    assert outcome.status == "complete"
    assert outcome.evaluation.summary.score == 100
    assert outcome.diagnostics.waves == 4
    assert outcome.diagnostics.logical_queries <= MAX_LOGICAL_QUERIES
    assert outcome.diagnostics.pages_opened <= MAX_PAGES_OPENED
    assert outcome.diagnostics.final_state == "candidate_selected"
    assert outcome.diagnostics.stop_reason == "PROVEN_EQUIVALENT"


def test_source_path_reaches_requirement_extraction_for_targeted_pdf_recovery():
    """Rupture visée : le planificateur sait qu'il faut relire une page, sans PDF."""
    received = []

    class PathPlanner(FakePlanner):
        def extract_requirements(self, fiche, *, source_path=None):
            received.append((fiche, source_path))
            return requirements()

    service = AdaptiveResearch(
        config=B2Config(api_key="test"),
        planner=PathPlanner(),
        gateway=FakeGateway(),
        fetcher=FakeFetcher(),
        analyze_pages=analyzer_sequence([["proven"] * 4]),
        graph_config={},
    )

    service.run("fiche", "Norel", source_path="belvia.pdf")

    assert received == [("fiche", "belvia.pdf")]


def test_successful_candidate_evaluation_is_journaled():
    outcome = research(analyzer_sequence([["proven"] * 4])).run("fiche", "Norel")

    assert outcome.diagnostics.candidate_evaluations == [{
        "wave": 1,
        "url": "https://new.norel.example/w1/p1",
        "reference": "REF-1",
        "brand": "Norel",
        "score": 100,
        "eligible": True,
        "complete": True,
        "proof_url_count": 1,
        "official_proof_count": 4,
        "proven_criteria": [
            "Critère 1", "Critère 2", "Critère 3", "Critère 4"
        ],
        "not_proven_criteria": [],
        "incompatible_criteria": [],
        "unverified_criteria": [],
        "missing_audit_criteria": [],
        "rejected_proof_criteria": [],
        "proof_grades": {"Critère 1": "contiguous", "Critère 2": "contiguous",
                         "Critère 3": "contiguous", "Critère 4": "contiguous"},
        "non_applicable_criteria": [],
    }]


def test_candidate_contract_error_is_journaled_without_raw_message():
    calls = {"count": 0}

    def analyze(pages, requirement_set, target_brand, graph_config, **kwargs):
        calls["count"] += 1
        if calls["count"] > 1:
            return [], []
        page = pages[0]
        malformed = candidate(["proven"] * 4, page.url)
        malformed.criteria[0].requested_value = "valeur réécrite"
        audit = PageAudit(page_url=page.url, candidates=[malformed])
        content = _page_content(_phrases_for(["proven"] * 4))
        return [PageAnalysis(page.url, content, audit)], []

    outcome = research(analyze).run("fiche", "Norel")

    assert outcome.diagnostics.candidate_evaluations[0] == {
        "wave": 1,
        "url": "https://new.norel.example/w1/p1",
        "reference": "REF-1",
        "contract_error": "CompatibilityContractError",
    }
    assert "valeur réécrite" not in repr(outcome.diagnostics.candidate_evaluations)


def test_partial_candidate_does_not_stop_remaining_waves():
    """Mutation détectée : le premier résultat à 75 % arrête la recherche."""
    outcome = research(analyzer_sequence([
        ["proven", "proven", "proven", "not_proven"],
        None,
        None,
        None,
    ])).run("fiche", "Norel")

    assert outcome.status == "partial"
    assert outcome.evaluation.summary.score == 75
    assert outcome.diagnostics.waves == 4


def test_invalid_candidate_is_ignored_without_losing_valid_candidate():
    """Une hallucination de preuve ne doit pas annuler les autres candidats."""
    calls = {"count": 0}

    def analyze(pages, requirement_set, target_brand, graph_config, **kwargs):
        calls["count"] += 1
        if calls["count"] > 1:
            return [], []
        page = pages[0]
        valid = candidate(["proven"] * 4, page.url)
        invalid = candidate(["proven"] * 4, page.url).model_copy(
            update={"reference": "REF-INVENTEE"}, deep=True
        )
        invalid.criteria[0].proofs[0].excerpt = "extrait absent de la page"
        audit = PageAudit(page_url=page.url, candidates=[invalid, valid])
        content = _page_content(_phrases_for(["proven"] * 4))
        return [PageAnalysis(page.url, content, audit)], []

    outcome = research(analyze).run("fiche", "Norel")

    assert outcome.status == "complete"
    assert outcome.evaluation.summary.reference == "REF-1"
    assert outcome.diagnostics.final_state == "candidate_selected"
    # La référence inventée n'est jamais retenue, et son extrait absent de la
    # page ne fuite pas dans les diagnostics.
    assert outcome.evaluation.summary.reference != "REF-INVENTEE"
    assert all(
        "extrait absent de la page" not in warning
        for warning in outcome.diagnostics.warnings
    )


def test_fallback_query_plan_is_visible_in_diagnostics():
    """Le secours doit rester auditable sans exposer la réponse brute du modèle."""
    class FallbackPlanner(FakePlanner):
        def plan_queries(self, requirement_set, target_brand, missing, previous):
            plan = super().plan_queries(requirement_set, target_brand, missing, previous)
            return plan.model_copy(update={"strategy": "fallback"})

    service = AdaptiveResearch(
        config=B2Config(api_key="test"),
        planner=FallbackPlanner(),
        gateway=FakeGateway(),
        fetcher=FakeFetcher(),
        analyze_pages=analyzer_sequence([["proven"] * 4]),
        graph_config={},
    )

    outcome = service.run("fiche", "Norel")

    assert outcome.status == "complete"
    assert outcome.diagnostics.warnings == ["Plan de requêtes : secours déterministe."]


def test_fallback_query_plan_surfaces_sanitized_failure_reasons():
    class FallbackPlanner(FakePlanner):
        def plan_queries(self, requirement_set, target_brand, missing, previous):
            plan = super().plan_queries(requirement_set, target_brand, missing, previous)
            return plan.model_copy(update={
                "strategy": "fallback",
                "failures": (
                    "tentative 1 : erreur modèle APITimeoutError",
                    "tentative 2 : réponse invalide — exactement quatre requêtes requises",
                ),
            })

    service = AdaptiveResearch(
        config=B2Config(api_key="test"),
        planner=FallbackPlanner(),
        gateway=FakeGateway(),
        fetcher=FakeFetcher(),
        analyze_pages=analyzer_sequence([["proven"] * 4]),
        graph_config={},
    )

    outcome = service.run("fiche", "Norel")

    assert outcome.status == "complete"
    assert outcome.diagnostics.warnings == [
        "Plan de requêtes : secours déterministe. "
        "Motifs : tentative 1 : erreur modèle APITimeoutError ; "
        "tentative 2 : réponse invalide — exactement quatre requêtes requises"
    ]


def test_run_captures_an_immediately_replayable_corpus_when_enabled(
    monkeypatch,
    tmp_path,
):
    """Régression visée : le run écrit des JSON sans provenance de vague."""
    corpus = tmp_path / "replay-corpus"
    source = tmp_path / "source.pdf"
    source.write_bytes(b"synthetic-source-pdf")
    monkeypatch.setenv("B2_REPLAY_DIR", str(corpus))

    outcome = research(analyzer_sequence([["proven"] * 4])).run(
        "fiche extraite",
        "Norel",
        source_path=source,
    )

    assert outcome.audit_waves == [1]
    assert outcome.page_waves
    # La mission ne s'arrête plus à 100 % : les vagues suivantes ouvrent encore
    # des pages. Chacune doit rester rattachée à une vague réellement menée.
    vagues_menees = set(range(1, outcome.diagnostics.waves + 1))
    assert set(outcome.page_waves.values()) <= vagues_menees
    assert 1 in outcome.page_waves.values()
    assert [item["wave"] for item in outcome.replay_waves] == sorted(vagues_menees)
    assert outcome.replay_waves[0] == {
        "wave": 1,
        "logical_queries_at_entry": 0,
        "logical_queries_before_near_miss": 3,
        "seen_signatures_at_entry": [],
        "seen_signatures_before_near_miss": [],
    }
    # Aucune vague ne doit perdre un champ de provenance en chemin.
    assert all(
        set(item) == set(outcome.replay_waves[0])
        for item in outcome.replay_waves
    )
    replayed = replay_corpus(corpus)
    assert replayed["status"] == "complete"
    assert replayed["compatibility"]["reference"] == "REF-1"
    assert replayed["criteria"][0]["status"] == "proven"
    assert replayed["criteria"][0]["proofs"]
    assert replayed["diagnostics"]["candidate_evaluations"] == (
        outcome.diagnostics.candidate_evaluations
    )


def test_les_audits_ne_suivent_pas_le_nombre_de_navigateurs():
    """Regression visee : baisser les navigateurs pour tenir sur une machine
    chargee serialise aussi les audits LLM, qui n'occupent qu'une socket.

    Mesure du 2026-08-27 : avec `B2_MAX_SCRAPER_WORKERS=1` impose par la RAM,
    les vingt-huit audits du run partaient un par un sans que la memoire y
    gagne quoi que ce soit.
    """
    vus: list[int] = []

    def analyseur(*args, **kwargs):
        vus.append(kwargs["workers"])
        return [], []

    AdaptiveResearch(
        config=B2Config(api_key="test", max_scraper_workers=1),
        planner=FakePlanner(),
        gateway=FakeGateway(),
        fetcher=FakeFetcher(),
        analyze_pages=analyseur,
        graph_config={},
    ).run("fiche", "Norel")

    assert vus, "l'analyseur n'a jamais ete appele"
    assert set(vus) == {2}
