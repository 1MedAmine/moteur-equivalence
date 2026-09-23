# -*- coding: utf-8 -*-
"""La boucle doit être adaptative : une vague informe la suivante.

Défaut mesuré le 2026-08-19 (`NV1T05BD -> Norel`) : trois vagues de quatre
requêtes consommaient les douze places avant que l'audit de la dernière vague
ne révèle le near-miss. Deux tâches générées, zéro envoyée — un blocage
déterministe, pas un hasard du moteur.

La correction structurelle donne quatre vagues de trois requêtes : le budget
global reste douze, mais un tour reste disponible pour consommer ce que
l'audit vient de découvrir.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from configuration import B2Config
from recherche_adaptative import (
    MAX_LOGICAL_QUERIES,
    MAX_QUERIES_PER_WAVE,
    AdaptiveResearch,
    _eligible_targeted_candidate_keys,
    _group_targeted_queries,
    _interleave_targeted_queries,
    _prioritize_targeted_leads,
    _proven_criteria_by_candidate,
    _targeted_wave_progressed,
)
from planification import TargetedQuery
from planification import Planner
from test_recherche_near_miss import (
    _Analyzer,
    _Fetcher,
    _Gateway,
    _Planner,
    _candidat_rejete,
    _candidat_sans_incompatibilite,
    _candidat_sous_documente,
)


class _GatewayUrlsDistinctes(_Gateway):
    """Une URL différente par requête, sinon la déduplication vide les vagues."""

    def search(self, query: str):
        from modeles import SearchAttempt, SearchBatch, SearchHit

        self.queries.append(query)
        return SearchBatch(
            query=query,
            hits=[SearchHit(
                url=f"https://revendeur.example/xz07-20-10-13/run-{len(self.queries)}",
                title="XZ07", snippet="", engine="bi", rank=1,
            )],
            attempts=[
                SearchAttempt(engine="bi", status="ok", result_count=1)
                for _ in range(self.engine_attempts)
            ],
        )


def test_distributor_queries_are_interleaved_with_normal_targeted_queries():
    normal = (
        TargetedQuery(
            query="Norel 4KBL103001R8110 fiche produit",
            candidate_key=("norel", "4kbl"), angle="fiche",
        ),
        TargetedQuery(
            query="Norel 4KBL103001R8110 fiche technique",
            candidate_key=("norel", "4kbl"), angle="technique",
        ),
    )
    distributors = (
        TargetedQuery(
            query="site:distributeur-a.example 4KBL103001R8110",
            candidate_key=("norel", "4kbl"), angle="distributor:distributeur-a.example",
        ),
        TargetedQuery(
            query="site:distributeur-b.example 4KBL103001R8110",
            candidate_key=("norel", "4kbl"), angle="distributor:distributeur-b.example",
        ),
    )

    planned = _interleave_targeted_queries(normal, distributors)

    assert [item.query for item in planned] == [
        "Norel 4KBL103001R8110 fiche produit",
        "site:distributeur-a.example 4KBL103001R8110",
        "Norel 4KBL103001R8110 fiche technique",
        "site:distributeur-b.example 4KBL103001R8110",
    ]


class _PlannerLarge(_Planner):
    """Propose plus de requêtes que le plafond par vague ne permet."""

    def plan_queries(self, requirement_set, target_brand, missing, previous):
        self.calls += 1
        from types import SimpleNamespace

        return SimpleNamespace(
            queries=[f"generale-{self.calls}-{index}" for index in range(6)],
            strategy="model",
        )

    def plan_targeted_queries(self, requirement_set, leads, previous):
        self.calls += 1
        from types import SimpleNamespace

        return SimpleNamespace(
            queries=[
                SimpleNamespace(
                    query=f"generale-{self.calls}-{index}",
                    candidate_key=lead.key,
                )
                for index, lead in enumerate((leads * 6)[:6])
            ],
            strategy="model",
        )


class _PlannerWithDistributorQueries(_Planner):
    def __init__(self) -> None:
        super().__init__()
        self.config = B2Config(
            api_key="test",
            distributor_domains=("distributeur-a.example",),
        )

    plan_distributor_queries = Planner.plan_distributor_queries


def test_targeted_wave_sends_a_distributor_query_after_a_lead_is_discovered():
    gateway = _GatewayUrlsDistinctes()
    service = AdaptiveResearch(
        config=B2Config(
            api_key="test",
            adaptive_max_waves=2,
            distributor_domains=("distributeur-a.example",),
        ),
        planner=_PlannerWithDistributorQueries(),
        gateway=gateway,
        fetcher=_Fetcher(),
        analyze_pages=_Analyzer(candidat_factory=_candidat_sans_incompatibilite),
        graph_config={},
    )

    service.run("fiche", "Norel")

    assert "site:distributeur-a.example XZ07-20-10-13" in gateway.queries
    assert any(query.startswith("generale-") for query in gateway.queries)


class _AnalyzerTardif(_Analyzer):
    """Le candidat rejeté n'apparaît qu'après plusieurs vagues.

    C'est la situation réelle : l'audit qui révèle l'incompatibilité arrive
    après que les vagues précédentes ont consommé leurs requêtes.
    """

    def __init__(self, analyses_avant_declenchement: int = 2) -> None:
        super().__init__()
        self.analyses_avant_declenchement = analyses_avant_declenchement
        self.analyses = 0

    def __call__(self, pages, requirement_set, target_brand, graph_config, **kwargs):
        from analyse import PageAnalysis
        from candidats import CandidateProposal
        from modeles import PageAudit

        self.analyses += 1
        mode = kwargs.get("mode", "audit")
        self.calls.append({"pages": tuple(pages), "mode": mode})
        if not pages:
            return [], []

        page = pages[0]
        # Avant le déclenchement : la découverte propose la piste sans auditer.
        if self.analyses <= self.analyses_avant_declenchement:
            return [PageAnalysis(
                page_url=page.url,
                content=page.content,
                audit=PageAudit(page_url=page.url),
                proposals=(
                    CandidateProposal(brand="Norel", reference="XZ07-20-10-13"),
                ) if mode == "discovery" else (),
                mode=mode,
            )], []

        # La preuve doit pointer la page réellement visitée, sans quoi le
        # contrat de preuve la rétrograde et le bloqueur disparaît.
        candidat = _candidat_rejete()
        for critere in candidat.criteria:
            for preuve in critere.proofs:
                preuve.url = page.url
        return [PageAnalysis(
            page_url=page.url,
            content=page.content,
            audit=PageAudit(page_url=page.url, candidates=[candidat]),
            mode=mode,
        )], []


class _PlannerUnePermutation(_PlannerLarge):
    def plan_targeted_queries(self, requirement_set, leads, previous):
        self.calls += 1
        from types import SimpleNamespace

        lead = leads[0]
        return SimpleNamespace(
            queries=[SimpleNamespace(
                query=f"permutation-{self.calls}",
                candidate_key=lead.key,
            )],
            strategy="model",
        )


class _AnalyzerAvecProgres(_AnalyzerTardif):
    def __init__(self) -> None:
        super().__init__(analyses_avant_declenchement=1)

    def __call__(self, pages, requirement_set, target_brand, graph_config, **kwargs):
        if self.analyses == 0:
            return super().__call__(
                pages, requirement_set, target_brand, graph_config, **kwargs
            )
        from analyse import PageAnalysis
        from modeles import PageAudit

        self.analyses += 1
        mode = kwargs.get("mode", "audit")
        self.calls.append({"pages": tuple(pages), "mode": mode})
        if not pages:
            return [], []
        page = pages[0]
        candidat = _candidat_sous_documente()
        for critere in candidat.criteria:
            for preuve in critere.proofs:
                preuve.url = page.url
        return [PageAnalysis(
            page_url=page.url,
            content=page.content,
            audit=PageAudit(page_url=page.url, candidates=[candidat]),
            mode=mode,
        )], []


class _FetcherReferenceRaccourcie:
    """La vague 1 voit une forme longue, les suivantes la forme emboitee courte."""

    def fetch(self, url: str):
        from scraping import PageContent

        run = int(url.rsplit("run-", 1)[1])
        reference = "XXXZ07-20-10-13" if run <= 3 else "XZ07-20-10-13"
        return PageContent(
            url,
            f"Contacteur Norel {reference}",
            f"Norel {reference}",
            "scrapling",
        )


class _AnalyzerReferenceLongue:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, pages, requirement_set, target_brand, graph_config, **kwargs):
        from analyse import PageAnalysis
        from candidats import CandidateProposal
        from modeles import PageAudit

        self.calls += 1
        if not pages:
            return [], []
        page = pages[0]
        return [PageAnalysis(
            page_url=page.url,
            content=page.content,
            audit=PageAudit(page_url=page.url),
            proposals=(
                (CandidateProposal(
                    brand="Norel", reference="XXXZ07-20-10-13",
                ),)
                if self.calls == 1 else ()
            ),
            mode=kwargs.get("mode", "discovery"),
        )], []


class _RegistrySansFallbackLongInitial:
    """Laisse la premiere identite venir du graphe, puis active le fallback."""

    def __init__(self) -> None:
        from candidats import CandidateRegistry

        self._registry = CandidateRegistry()

    def ingest(self, proposals, document, requirements, target_brand, **kwargs):
        from candidats import CandidateIngestResult

        if not proposals and "XXXZ07" in document.prompt_source:
            return CandidateIngestResult((), ())
        return self._registry.ingest(
            proposals, document, requirements, target_brand, **kwargs,
        )

    def active(self):
        return self._registry.active()


def _service(*, analyzer=None, planner=None, waves=None):
    gateway = _GatewayUrlsDistinctes()
    analyzer = analyzer or _AnalyzerTardif()
    config = B2Config(api_key="test")
    if waves is not None:
        config = B2Config(api_key="test", adaptive_max_waves=waves)
    service = AdaptiveResearch(
        config=config,
        planner=planner or _PlannerLarge(),
        gateway=gateway,
        fetcher=_Fetcher(),
        analyze_pages=analyzer,
        graph_config={},
    )
    return service, gateway


def _ciblees(gateway) -> list[str]:
    return [item for item in gateway.queries if not item.startswith("generale-")]


# --------------------------------------------------------------------------
# Structure des vagues
# --------------------------------------------------------------------------

def test_default_configuration_leaves_a_wave_for_what_the_audit_discovers():
    """Quatre vagues de trois requêtes : le budget reste douze."""
    config = B2Config(api_key="test")

    assert config.adaptive_max_waves == 4
    assert MAX_QUERIES_PER_WAVE == 3
    assert config.adaptive_max_waves * MAX_QUERIES_PER_WAVE == MAX_LOGICAL_QUERIES


def test_a_wave_never_sends_more_than_the_per_wave_cap():
    """Mutation détectée : une vague monopolise le budget global."""
    service, gateway = _service(analyzer=_Analyzer(_candidat_sans_incompatibilite))

    diagnostics = service.run("fiche", "Norel").diagnostics

    for detail in diagnostics.wave_details:
        assert detail["query_count"] <= MAX_QUERIES_PER_WAVE, detail


def test_targeted_wave_without_new_proof_returns_to_discovery():
    """Mutation detectee : une piste sterile garde toutes les vagues suivantes."""
    service, _ = _service(analyzer=_AnalyzerTardif())

    diagnostics = service.run("fiche", "Norel").diagnostics

    assert [item["mode"] for item in diagnostics.wave_modes[:3]] == [
        "discovery",
        "targeted",
        "discovery",
    ]


def test_targeted_wave_with_a_new_proven_criterion_keeps_the_lead():
    """Un candidat sous 75 % continue s'il gagne au moins une preuve."""
    service, _ = _service(
        analyzer=_AnalyzerAvecProgres(),
        planner=_PlannerUnePermutation(),
    )

    diagnostics = service.run("fiche", "Norel").diagnostics

    assert [item["mode"] for item in diagnostics.wave_modes[:3]] == [
        "discovery",
        "targeted",
        "targeted",
    ]


def test_only_two_angle_permutations_are_sent_for_one_lead():
    """Le plan peut en proposer six ; le moteur n'en recoit que deux."""
    service, _ = _service(analyzer=_AnalyzerTardif())

    diagnostics = service.run("fiche", "Norel").diagnostics
    assert len(diagnostics.targeted_queries) == 2


def test_near_miss_budget_survives_after_the_angle_cap_is_exhausted():
    """Les requetes directionnelles ne partagent jamais les deux permutations."""
    service, gateway = _service(analyzer=_AnalyzerTardif())

    diagnostics = service.run("fiche", "Norel").diagnostics

    assert len(diagnostics.targeted_queries) == 2
    assert _ciblees(gateway)
    assert 1 <= diagnostics.near_miss_queries_scheduled <= 3


def test_nested_reference_merge_cannot_reset_the_two_angle_budget():
    """La forme courte herite des deux angles deja envoyes a la forme longue."""
    gateway = _GatewayUrlsDistinctes()
    service = AdaptiveResearch(
        config=B2Config(api_key="test"),
        planner=_PlannerLarge(),
        gateway=gateway,
        fetcher=_FetcherReferenceRaccourcie(),
        analyze_pages=_AnalyzerReferenceLongue(),
        graph_config={},
        registry_factory=_RegistrySansFallbackLongInitial,
    )

    diagnostics = service.run("fiche", "Norel").diagnostics

    assert len(diagnostics.targeted_queries) == 2
    assert {
        item["reference"] for item in diagnostics.targeted_queries
    } == {"XXXZ07-20-10-13"}
    assert [item["mode"] for item in diagnostics.wave_modes[:3]] == [
        "discovery", "targeted", "discovery",
    ]


def test_nested_reference_rename_does_not_count_old_proofs_as_progress():
    long_key = ("norel", "xxxz07201013")
    short_key = ("norel", "xz07201013")

    assert not _targeted_wave_progressed(
        {long_key: frozenset({"Nombre de poles"})},
        {short_key: frozenset({"Nombre de poles"})},
    )


def test_proofs_from_a_mandatory_blocked_candidate_do_not_count_as_progress():
    from types import SimpleNamespace

    evaluation = SimpleNamespace(
        candidate=SimpleNamespace(brand="Norel", reference="RT40F3"),
        summary=SimpleNamespace(
            proven_criteria=["Nombre de poles"],
            critical_blockers=["Fonction"],
        ),
    )

    assert _proven_criteria_by_candidate([evaluation]) == {}


def test_identical_targeted_queries_are_sent_once_and_charge_each_distinct_lead():
    from types import SimpleNamespace

    lead_a = ("norel", "xz07201011")
    lead_b = ("norel", "bsl07201081")
    queries, keys_by_query = _group_targeted_queries([
        SimpleNamespace(query="Norel contactor 24 V DC", candidate_key=lead_a),
        SimpleNamespace(query="Norel contactor 24 V DC", candidate_key=lead_b),
        # Une forme emboitee de A ne cree pas un troisieme debit.
        SimpleNamespace(
            query="Norel contactor 24 V DC",
            candidate_key=("norel", "xxxz07201011"),
        ),
    ])

    assert queries == ("Norel contactor 24 V DC",)
    assert keys_by_query == {"Norel contactor 24 V DC": (lead_a, lead_b)}


def test_shared_query_never_recharges_a_lead_whose_angle_budget_is_exhausted():
    exhausted = ("norel", "xz07201011")
    available = ("norel", "bsl07201081")

    assert _eligible_targeted_candidate_keys(
        (exhausted, available),
        {exhausted: 2},
    ) == (available,)


def test_unqueried_targeted_lead_is_prioritized_on_the_next_targeted_wave():
    """La quatrième piste non envoyée passe avant les trois déjà couvertes."""
    from candidats import CandidateLead

    def lead(reference: str) -> CandidateLead:
        return CandidateLead(
            brand="Maker",
            reference=reference,
            canonical_brand="maker",
            canonical_reference=reference.casefold(),
            occurrences=(),
        )

    first, second, third, fourth = (
        lead("FIRST"), lead("SECOND"), lead("THIRD"), lead("FOURTH"),
    )
    prioritized = _prioritize_targeted_leads(
        [first, second, third, fourth],
        {first.key: 1, second.key: 1, third.key: 1},
    )

    assert [item.reference for item in prioritized] == [
        "FOURTH", "FIRST", "SECOND", "THIRD",
    ]


def test_forced_discovery_keeps_one_real_general_query_with_three_near_misses():
    """Le diagnostic `discovery` doit correspondre a une requete generale envoyee."""
    service, gateway = _service(analyzer=_AnalyzerTardif())
    calls = 0

    def pending_three(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return () if calls != 2 else ("near-1", "near-2", "near-3")

    service._near_miss_queries = pending_three
    diagnostics = service.run("fiche", "Norel").diagnostics

    wave_three = next(
        item for item in diagnostics.wave_details if item["wave_index"] == 3
    )
    assert diagnostics.wave_modes[2]["mode"] == "discovery"
    assert "PLANNED" in wave_three["task_types"]
    assert any(query.startswith("generale-3-") for query in gateway.queries)


def test_forced_discovery_deduplicates_a_general_query_also_pending_as_near_miss():
    service, gateway = _service(analyzer=_AnalyzerTardif())
    calls = 0

    def pending_with_same_general(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return () if calls != 2 else ("generale-3-0", "near-2", "near-3")

    service._near_miss_queries = pending_with_same_general
    diagnostics = service.run("fiche", "Norel").diagnostics

    wave_three = next(
        item for item in diagnostics.wave_details if item["wave_index"] == 3
    )
    assert gateway.queries.count("generale-3-0") == 1
    assert "PLANNED" in wave_three["task_types"]


# --------------------------------------------------------------------------
# Critère de réussite central
# --------------------------------------------------------------------------

def test_a_near_miss_found_late_still_reaches_the_search_engine():
    """Le critère central : une découverte influence réellement la suite."""
    service, gateway = _service(analyzer=_AnalyzerTardif())

    diagnostics = service.run("fiche", "Norel").diagnostics

    ciblees = _ciblees(gateway)
    assert ciblees, (
        "aucune requête near-miss envoyée : "
        f"{diagnostics.near_miss_tasks_generated} tâche(s) générée(s), "
        f"{diagnostics.near_miss_queries_skipped_budget} écartée(s) faute de budget"
    )
    for query in ciblees:
        assert "XZ07" in query
        assert any(mot in query.casefold() for mot in ("bobine", "coil"))
        assert '"24 V DC"' in query
    assert diagnostics.near_miss_queries_scheduled >= 1


def test_a_near_miss_born_at_the_last_wave_is_reported_not_hidden():
    """Cas limite honnête : plus aucun tour pour consommer la découverte.

    Ce n'est pas un défaut — c'est la fin de la mission. Le diagnostic doit le
    dire au lieu de laisser croire qu'aucun near-miss n'a été trouvé.
    """
    service, gateway = _service(analyzer=_AnalyzerTardif(3))

    diagnostics = service.run("fiche", "Norel").diagnostics

    assert diagnostics.near_miss_tasks_generated >= 1
    assert _ciblees(gateway) == []
    assert diagnostics.near_miss_queries_skipped_budget >= 1


def test_no_variant_reference_is_ever_fabricated():
    """Interdiction centrale : la variante se découvre, ne se fabrique pas."""
    service, gateway = _service(analyzer=_AnalyzerTardif())

    service.run("fiche", "Norel")

    for query in gateway.queries:
        assert "XZ07-20-10-11" not in query


# --------------------------------------------------------------------------
# Budgets
# --------------------------------------------------------------------------

def test_global_ceilings_always_hold():
    service, gateway = _service(analyzer=_AnalyzerTardif())

    diagnostics = service.run("fiche", "Norel").diagnostics

    assert diagnostics.logical_queries <= MAX_LOGICAL_QUERIES
    assert len(gateway.queries) == diagnostics.logical_queries
    assert diagnostics.pages_analyzed <= 36
    assert diagnostics.pages_fetched <= 36


def test_pages_partition_invariant_holds():
    service, _ = _service(analyzer=_AnalyzerTardif())

    diagnostics = service.run("fiche", "Norel").diagnostics

    assert diagnostics.pages_fetched == (
        diagnostics.pages_analyzed
        + diagnostics.pages_rejected_by_gate
        + diagnostics.pages_not_ready_for_evidence
    )


def test_logical_and_engine_counters_stay_separate():
    """Une requête logique vaut 1, quels que soient les essais moteur."""
    gateway = _GatewayUrlsDistinctes(engine_attempts=3)
    service = AdaptiveResearch(
        config=B2Config(api_key="test"),
        planner=_PlannerLarge(),
        gateway=gateway,
        fetcher=_Fetcher(),
        analyze_pages=_AnalyzerTardif(),
        graph_config={},
    )

    diagnostics = service.run("fiche", "Norel").diagnostics

    assert diagnostics.engine_calls == 3 * diagnostics.logical_queries


# --------------------------------------------------------------------------
# Diagnostics par vague
# --------------------------------------------------------------------------

def test_each_wave_reports_what_it_did():
    service, _ = _service(analyzer=_AnalyzerTardif())

    diagnostics = service.run("fiche", "Norel").diagnostics

    assert diagnostics.wave_details
    for detail in diagnostics.wave_details:
        assert set(detail) >= {
            "wave_index", "query_count", "logical_queries_before",
            "logical_queries_after", "task_types",
        }
        assert detail["logical_queries_after"] >= detail["logical_queries_before"]


def test_wave_details_never_carry_page_content_or_secrets():
    service, _ = _service(analyzer=_AnalyzerTardif())

    diagnostics = service.run("fiche", "Norel").diagnostics

    for detail in diagnostics.wave_details:
        rendu = repr(detail)
        for interdit in ("Bobine 100", "api_key", "prompt", "page_content"):
            assert interdit not in rendu


def test_stop_reason_is_always_explicit():
    service, _ = _service(analyzer=_AnalyzerTardif())

    diagnostics = service.run("fiche", "Norel").diagnostics

    assert diagnostics.stop_reason in {
        "PROVEN_EQUIVALENT",
        "QUERY_BUDGET_EXHAUSTED",
        "PAGE_BUDGET_EXHAUSTED",
        "MAX_WAVES_REACHED",
        "NO_USEFUL_RESEARCH_TASK",
        "NO_PROVABLE_CANDIDATE",
        "PRODUCT_CONTENT_NOT_RETRIEVED",
    }
