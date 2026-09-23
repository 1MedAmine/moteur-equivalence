# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analyse import PageAnalysis
from candidats import (
    CandidateIngestResult,
    CandidateLead,
    CandidateProposal,
    CandidateRegistry,
    DiscoveryLimits,
    RejectedCandidate,
    build_discovery_document,
)
from configuration import B2Config
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
from planification import (
    PlanningError,
    QueryPlan,
    SearchCandidateHint,
    TargetedQuery,
    TargetedQueryPlan,
)
from recherche_adaptative import (
    MAX_LOGICAL_QUERIES,
    AdaptiveResearch,
    HitContext,
    SelectedHitContext,
    _cached_product_pages,
    _merge_fetched_contexts,
    merge_page_audits,
    select_hit_contexts,
)
from scraping import PageContent


def _batch(query: str, urls: list[str]) -> SearchBatch:
    return SearchBatch(
        query=query,
        hits=[
            SearchHit(url=url, title=f"{query} result {rank}", engine="bing", rank=rank)
            for rank, url in enumerate(urls, start=1)
        ],
    )


def _contexts(
    batches: list[SearchBatch],
    candidate_keys_by_query: dict[str, tuple[tuple[str, str], ...]] | None = None,
) -> list[list[HitContext]]:
    candidate_keys_by_query = candidate_keys_by_query or {}
    return [
        [
            HitContext(
                wave=1,
                query=batch.query,
                candidate_keys=candidate_keys_by_query.get(batch.query, ()),
                hit=hit,
            )
            for hit in batch.hits
        ]
        for batch in batches
    ]


def test_select_hit_contexts_is_fair_by_rank_then_query_order():
    """Un premier lot abondant ne doit pas affamer les trois autres requêtes."""
    batches = [
        _batch(
            f"query-{query_index}",
            [
                f"https://maker{query_index}.example/product-{rank}"
                for rank in range(1, 13)
            ],
        )
        for query_index in range(1, 5)
    ]

    selected = select_hit_contexts(
        _contexts(batches),
        seen_urls=set(),
        limit=12,
    )

    assert [item.url for item in selected] == [
        f"https://maker{query_index}.example/product-{rank}"
        for rank in range(1, 4)
        for query_index in range(1, 5)
    ]
    assert selected[0].queries == ("query-1",)


def test_cached_product_pages_audits_a_visited_official_page_without_a_lead_occurrence():
    """Le cache est sélectionné par contenu, jamais par l'historique de découverte."""
    lead = CandidateLead(
        brand="Norel",
        reference="4KBL136001R3001",
        canonical_brand="norel",
        canonical_reference="4kbl136001r3001",
        occurrences=(),
    )
    official_url = "https://empower.norel.example/ecatalog/ec/FR_CA/p/4KBL136001R3001"
    content = "Norel 4KBL136001R3001 XZ07Z-20-01-30 9 A 24 V DC"

    selected = _cached_product_pages(
        {lead.key: lead},
        (lead.key,),
        {official_url: content},
        already_audited=set(),
    )

    assert selected == [(official_url, content, (lead.key,))]


def test_cached_product_pages_resolves_nested_audited_and_active_candidate_keys():
    """Le cache suit une identité fusionnée, quelle que soit sa forme auditée."""
    short = CandidateLead(
        brand="Norel",
        reference="4KBL136001R3001",
        canonical_brand="norel",
        canonical_reference="4kbl136001r3001",
        occurrences=(),
    )
    long = CandidateLead(
        brand="Norel",
        reference="4KBL136001R3001XZ07Z200130",
        canonical_brand="norel",
        canonical_reference="4kbl136001r3001xz07z200130",
        occurrences=(),
    )
    url = "https://empower.norel.example/ecatalog/ec/FR_CA/p/4KBL136001R3001"
    content = "Norel 4KBL136001R3001 4KBL136001R3001XZ07Z200130 9 A"

    active_short = _cached_product_pages(
        {short.key: short},
        (long.key,),
        {url: content},
        already_audited=set(),
    )
    active_long = _cached_product_pages(
        {long.key: long},
        (short.key,),
        {url: content},
        already_audited=set(),
    )

    assert active_short == [(url, content, (short.key,))]
    assert active_long == [(url, content, (long.key,))]


def test_select_hit_contexts_counts_a_canonical_duplicate_once_and_unions_context():
    """Deux requêtes ciblées vers la même page autorisent leurs deux pistes."""
    duplicate = "https://maker.com/product/zx417-qk900?utm_source=search"
    batches = [
        _batch("query-1", [duplicate]),
        _batch("query-2", ["https://maker.com/product/zx417-qk900#details"]),
    ]
    keys = {
        "query-1": (("maker", "zx417"),),
        "query-2": (("maker", "qk900"),),
    }
    seen_urls: set[str] = set()

    selected = select_hit_contexts(
        _contexts(batches, keys),
        seen_urls=seen_urls,
        limit=12,
    )

    assert len(selected) == 1
    assert selected[0].url == duplicate
    assert selected[0].queries == ("query-1", "query-2")
    assert selected[0].candidate_keys == (
        ("maker", "zx417"),
        ("maker", "qk900"),
    )
    assert len(selected[0].hits) == 2
    assert seen_urls == {"https://maker.com/product/zx417-qk900"}


def test_select_hit_contexts_uses_numeric_rank_when_query_ranks_are_sparse():
    """Un rang 1 tardif dans l'ordre des requêtes précède toujours un rang 2."""
    contexts = [
        [HitContext(
            wave=1,
            query="query-1",
            candidate_keys=(),
            hit=SearchHit(
                url="https://maker.com/query-1-rank-2",
                engine="bing",
                rank=2,
            ),
        )],
        [
            HitContext(
                wave=1,
                query="query-2",
                candidate_keys=(),
                hit=SearchHit(
                    url="https://maker.com/query-2-rank-1",
                    engine="bing",
                    rank=1,
                ),
            ),
            HitContext(
                wave=1,
                query="query-2",
                candidate_keys=(),
                hit=SearchHit(
                    url="https://maker.com/query-2-rank-3",
                    engine="bing",
                    rank=3,
                ),
            ),
        ],
    ]

    selected = select_hit_contexts(contexts, seen_urls=set(), limit=3)

    assert [item.url for item in selected] == [
        "https://maker.com/query-2-rank-1",
        "https://maker.com/query-1-rank-2",
        "https://maker.com/query-2-rank-3",
    ]


def test_targeted_hit_without_candidate_reference_is_not_opened():
    contexts = [[HitContext(
        wave=2,
        query="Maker ZX-41-7 fiche produit",
        candidate_keys=(("maker", "zx417"),),
        hit=SearchHit(
            url="https://unrelated.example/products/bearing-latest",
            title="Unrelated news",
            snippet="No product identity here",
            engine="bing",
            rank=1,
        ),
    )]]

    assert select_hit_contexts(contexts, seen_urls=set(), limit=12) == []


def test_discovery_hit_must_repeat_a_numeric_anchor_or_two_query_terms():
    contexts = [[
        HitContext(
            wave=1,
            query="deep groove ball bearing 25x52x15 sealed",
            candidate_keys=(),
            hit=SearchHit(
                url="https://sports.example/season-2026",
                title="Football season",
                snippet="Latest standings",
                engine="bing",
                rank=1,
            ),
        ),
        HitContext(
            wave=1,
            query="deep groove ball bearing 25x52x15 sealed",
            candidate_keys=(),
            hit=SearchHit(
                url="https://catalog.example/bearing-25x52x15",
                title="Deep groove ball bearing 25x52x15",
                snippet="Sealed bearing product page",
                engine="bing",
                rank=2,
            ),
        ),
    ]]

    selected = select_hit_contexts(contexts, seen_urls=set(), limit=12)

    assert [item.url for item in selected] == [
        "https://catalog.example/bearing-25x52x15"
    ]


def test_merge_fetched_contexts_preserves_query_order_and_stable_union():
    final_url = "https://maker.com/products/shared"
    first_hit = SearchHit(
        url="https://search.example/alias-a",
        title="first",
        engine="bing",
        rank=1,
    )
    second_hit = SearchHit(
        url="https://search.example/alias-b",
        title="second",
        engine="qwant",
        rank=1,
    )
    selected = [
        SelectedHitContext(
            url=first_hit.url,
            hits=(first_hit,),
            queries=("query-2", "query-1"),
            candidate_keys=(("maker", "zx417"),),
        ),
        SelectedHitContext(
            url=second_hit.url,
            hits=(second_hit,),
            queries=("query-1", "query-3"),
            candidate_keys=(("maker", "qk900"), ("maker", "zx417")),
        ),
    ]
    pages = [
        PageContent(final_url, "Maker data", "Maker", "scrapling"),
        PageContent(final_url, "Maker duplicate", "Maker", "scrapling"),
    ]

    contexts, merged_pages = _merge_fetched_contexts(
        selected,
        pages,
        seen_urls=set(),
        processed_final_urls=set(),
    )

    assert len(contexts) == len(merged_pages) == 1
    assert contexts[0].queries == ("query-2", "query-1", "query-3")
    assert contexts[0].candidate_keys == (
        ("maker", "zx417"),
        ("maker", "qk900"),
    )


def _requirements() -> RequirementSet:
    return RequirementSet(
        product="Source product",
        origin_brand="SourceCo",
        criteria=[
            Requirement(
                id=f"r{index}",
                label=f"Criterion {index}",
                # Litteralement ce que porte l'extrait par defaut de
                # `_candidate` (`f"proof for r {index}"`) et le contenu par
                # defaut de `_Fetcher` : une valeur placeholder doit rester
                # trouvable dans sa propre preuve.
                requested_value=f"r {index}",
                critical=True,
            )
            for index in range(1, 5)
        ],
    )


def _candidate(
    url: str,
    statuses: tuple[str, str, str, str],
    *,
    brand: str = "Maker",
    reference: str = "ZX-41-7",
) -> CandidateAudit:
    return CandidateAudit(
        brand=brand,
        reference=reference,
        criteria=[
            CriterionAudit(
                requirement_id=f"r{index}",
                requested_value=f"r {index}",
                observed_value=f"r {index}" if status != "not_proven" else "",
                status=status,
                proofs=[
                    SourceProof(
                        url=url,
                        excerpt=f"proof for r {index}",
                        type="web_officiel",
                    )
                ] if status != "not_proven" else [],
            )
            for index, status in enumerate(statuses, start=1)
        ],
    )


class _Planner:
    def __init__(self, *, fail_targeted_once: bool = False) -> None:
        self.general_calls: list[dict] = []
        self.targeted_calls: list[dict] = []
        self._plan_number = 0
        self._fail_targeted_once = fail_targeted_once

    def extract_requirements(self, fiche: str) -> RequirementSet:
        return _requirements()

    def plan_queries(self, requirement_set, target_brand, missing, previous):
        self._plan_number += 1
        self.general_calls.append({
            "missing": tuple(missing),
            "previous": frozenset(previous),
        })
        return QueryPlan(queries=tuple(
            f"general-{self._plan_number}-query-{index}" for index in range(1, 5)
        ))

    def plan_targeted_queries(self, requirement_set, candidates, previous):
        self._plan_number += 1
        selected = tuple(candidates)
        self.targeted_calls.append({
            "candidates": selected,
            "previous": frozenset(previous),
        })
        if self._fail_targeted_once:
            self._fail_targeted_once = False
            raise PlanningError("unsafe model response must not escape")
        return TargetedQueryPlan(queries=tuple(
            TargetedQuery(
                query=f"{selected[(index - 1) % len(selected)].brand} "
                      f"{selected[(index - 1) % len(selected)].reference} "
                      f"proof-angle-{self._plan_number}-{index}",
                candidate_key=selected[(index - 1) % len(selected)].key,
                angle=f"angle-{index}",
            )
            for index in range(1, 5)
        ))


class _Gateway:
    def __init__(
        self,
        *,
        hits_per_query: int = 1,
        identity_metadata: bool = True,
    ) -> None:
        self.calls: list[str] = []
        self.hits_per_query = hits_per_query
        self.identity_metadata = identity_metadata

    def search(self, query: str) -> SearchBatch:
        self.calls.append(query)
        call = len(self.calls)
        return SearchBatch(
            query=query,
            hits=[
                SearchHit(
                    url=f"https://maker.com/call-{call}/rank-{rank}",
                    title=(
                        "Maker ZX-41-7 technical page"
                        if self.identity_metadata else "Generic technical page"
                    ),
                    snippet=(
                        "Maker ZX-41-7 product data"
                        if self.identity_metadata else "Generic product data"
                    ),
                    engine="bing",
                    rank=rank,
                )
                for rank in range(1, self.hits_per_query + 1)
            ],
            attempts=[
                SearchAttempt(
                    engine="bing",
                    status="ok",
                    result_count=self.hits_per_query,
                )
            ],
        )


class _Fetcher:
    def __init__(
        self,
        content_by_url: dict[str, str] | None = None,
        *,
        identity_metadata: bool = True,
    ) -> None:
        self.calls: list[str] = []
        self.content_by_url = content_by_url or {}
        self.identity_metadata = identity_metadata

    def fetch(self, url: str) -> PageContent:
        self.calls.append(url)
        content = self.content_by_url.get(
            url,
            (
                "Maker ZX-41-7 technical data proof for r 1 proof for r 2 "
                "proof for r 3 proof for r 4 "
                + ("bounded corpus text " * 20)
                if self.identity_metadata else
                "Generic technical page without a product identity."
            ),
        )
        title = (
            "Maker ZX-41-7 technical page"
            if self.identity_metadata else "Generic technical page"
        )
        return PageContent(url, content, title, "scrapling")


class _Analyzer:
    def __init__(self, handler) -> None:
        self.handler = handler
        self.calls: list[dict] = []

    def __call__(self, pages, requirement_set, target_brand, graph_config, **kwargs):
        call = {
            "pages": tuple(pages),
            "mode": kwargs.get("mode", "audit"),
            "documents_by_url": kwargs.get("documents_by_url", {}),
            "authorized_by_url": kwargs.get("authorized_by_url", {}),
        }
        self.calls.append(call)
        return self.handler(len(self.calls), call), []


def _empty_analyses(call_number: int, call: dict) -> list[PageAnalysis]:
    return [
        PageAnalysis(
            page_url=page.url,
            content=page.content,
            audit=PageAudit(page_url=page.url),
            mode=call["mode"],
        )
        for page in call["pages"]
    ]


def _service(
    *,
    planner: _Planner | None = None,
    gateway: _Gateway | None = None,
    fetcher: _Fetcher | None = None,
    analyzer: _Analyzer | None = None,
    **injections,
) -> tuple[AdaptiveResearch, _Planner, _Gateway, _Fetcher, _Analyzer]:
    planner = planner or _Planner()
    gateway = gateway or _Gateway()
    fetcher = fetcher or _Fetcher()
    analyzer = analyzer or _Analyzer(_empty_analyses)
    service = AdaptiveResearch(
        config=B2Config(api_key="test"),
        planner=planner,
        gateway=gateway,
        fetcher=fetcher,
        analyze_pages=analyzer,
        graph_config={},
        **injections,
    )
    return service, planner, gateway, fetcher, analyzer


def test_run_switches_from_discovery_to_targeted_queries_with_page_authorization():
    """A literal lead drives the next wave without duplicate URL analysis."""
    class IdentityOnlyGateway(_Gateway):
        def search(self, query: str) -> SearchBatch:
            self.calls.append(query)
            suffix = chr(ord("a") + len(self.calls) - 1)
            return SearchBatch(
                query=query,
                hits=[SearchHit(
                    url=f"https://maker.com/catalogue/page-{suffix}",
                    title="Maker ZX-41-7 technical page",
                    snippet="Maker ZX-41-7 product data",
                    engine="bing",
                    rank=1,
                )],
                attempts=[SearchAttempt(
                    engine="bing", status="ok", result_count=1
                )],
            )
    def handler(call_number: int, call: dict) -> list[PageAnalysis]:
        analyses = _empty_analyses(call_number, call)
        first = analyses[0]
        if call_number == 1:
            analyses[0] = PageAnalysis(
                page_url=first.page_url,
                content=first.content,
                audit=first.audit,
                proposals=(CandidateProposal(brand="Maker", reference="ZX-41-7"),),
                proposal_rejections=(RejectedCandidate("", "", "malformed_proposal"),),
                mode=call["mode"],
            )
        else:
            analyses[0] = PageAnalysis(
                page_url=first.page_url,
                content=first.content,
                audit=PageAudit(
                    page_url=first.page_url,
                    candidates=[_candidate(first.page_url, ("proven",) * 4)],
                ),
                mode=call["mode"],
            )
        return analyses

    analyzer = _Analyzer(handler)
    service, planner, gateway, fetcher, _ = _service(
        analyzer=analyzer,
        gateway=IdentityOnlyGateway(),
    )

    outcome = service.run("source sheet", "Maker")

    assert outcome.status == "complete"
    assert outcome.evaluation is not None
    assert outcome.evaluation.summary.score == 100
    assert outcome.evaluation.summary.reference == "ZX-41-7"
    assert outcome.diagnostics.final_state == "candidate_selected"
    # Un candidat complet n'interrompt plus la mission. La vague 2 cible bien la
    # piste découverte ; les vagues 3 et 4 reprennent la découverte, le quota de
    # deux angles par piste étant alors consommé.
    assert [call["mode"] for call in analyzer.calls] == [
        "discovery", "targeted", "discovery", "discovery",
    ]
    assert len(planner.general_calls) == 3
    assert len(planner.targeted_calls) == 1
    targeted_queries = gateway.calls[3:5]
    assert len(targeted_queries) == 2
    assert all("Maker" in query and "ZX-41-7" in query for query in targeted_queries)
    assert len(fetcher.calls) == len(set(fetcher.calls)) == 11
    authorized = analyzer.calls[1]["authorized_by_url"]
    assert set(authorized) == {page.url for page in analyzer.calls[1]["pages"]}
    assert all(
        [(lead.brand, lead.reference) for lead in leads] == [("Maker", "ZX-41-7")]
        for leads in authorized.values()
    )
    assert outcome.diagnostics.wave_modes == [
        {"wave": 1, "mode": "discovery"},
        {"wave": 2, "mode": "targeted"},
        {"wave": 3, "mode": "discovery"},
        {"wave": 4, "mode": "discovery"},
    ]
    assert outcome.diagnostics.candidate_leads == [{
        "brand": "Maker",
        "reference": "ZX-41-7",
        "rank": 1,
        "source_count": 11,
        "fields": ["content", "snippet", "title"],
    }]
    assert len(outcome.diagnostics.targeted_queries) == 2
    assert outcome.diagnostics.targeted_queries[0] == {
        "wave": 2,
        "query": targeted_queries[0],
        "brand": "Maker",
        "reference": "ZX-41-7",
    }
    assert {item["analysis_status"] for item in outcome.diagnostics.page_states} == {
        "discovered",
        "no_candidate",
        "audited",
    }
    audites = [
        item for item in outcome.diagnostics.page_states
        if item["analysis_status"] == "audited"
    ]
    assert audites and audites[0]["audit_candidate_count"] == 1
    assert outcome.diagnostics.rejected_candidates == [{
        "brand": "",
        "reference": "",
        "reason": "malformed_proposal",
    }]


def test_all_search_metadata_leads_are_targeted_even_when_only_one_page_is_opened():
    identities = (
        ("MakerA", "ZX-417"),
        ("MakerB", "QK-900"),
        ("MakerC", "RT200X"),
        ("MakerD", "LM-400"),
    )

    class MetadataGateway:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def search(self, query: str) -> SearchBatch:
            self.calls.append(query)
            hits = []
            if len(self.calls) == 1:
                hits = [
                    SearchHit(
                        url=f"https://distributor.example/item-{index}",
                        title=f"{brand} {reference} Source product",
                        snippet=f"{brand} {reference} technical product page",
                        engine="bing",
                        rank=index,
                    )
                    for index, (brand, reference) in enumerate(identities, start=1)
                ]
            return SearchBatch(
                query=query,
                hits=hits,
                attempts=[SearchAttempt(
                    engine="bing",
                    status="ok" if hits else "empty",
                    result_count=len(hits),
                )],
            )

    planner = _Planner()
    gateway = MetadataGateway()
    fetcher = _Fetcher(identity_metadata=False)
    analyzer = _Analyzer(_empty_analyses)
    service = AdaptiveResearch(
        config=B2Config(
            api_key="test",
            adaptive_max_waves=2,
            adaptive_pages_per_wave=1,
        ),
        planner=planner,
        gateway=gateway,
        fetcher=fetcher,
        analyze_pages=analyzer,
        graph_config={},
    )

    service.run("source sheet", None)

    assert len(fetcher.calls) == 1
    assert len(planner.targeted_calls) == 1
    assert {
        (lead.brand, lead.reference)
        for lead in planner.targeted_calls[0]["candidates"]
    } == set(identities)


def test_llm_search_hints_override_noisy_metadata_before_targeted_planning():
    class HintPlanner(_Planner):
        def discover_search_candidates(self, requirement_set, hits, target_brand):
            return (
                SearchCandidateHint(
                    result_index=0,
                    brand="Maker",
                    reference="ZX-417",
                ),
            )

    class NoisyGateway:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def search(self, query: str) -> SearchBatch:
            self.calls.append(query)
            hits = []
            if len(self.calls) == 1:
                hits = [SearchHit(
                    url="https://distributor.example/zx-417",
                    title="Technical review Maker ZX-417 product page",
                    snippet="Maker ZX-417 exact model",
                    engine="bing",
                    rank=1,
                )]
            return SearchBatch(query=query, hits=hits)

    planner = HintPlanner()
    service = AdaptiveResearch(
        config=B2Config(api_key="test", adaptive_max_waves=2),
        planner=planner,
        gateway=NoisyGateway(),
        fetcher=_Fetcher(identity_metadata=False),
        analyze_pages=_Analyzer(_empty_analyses),
        graph_config={},
    )

    service.run("source sheet", None)

    assert len(planner.targeted_calls) == 1
    assert (
        planner.targeted_calls[0]["candidates"][0].brand,
        planner.targeted_calls[0]["candidates"][0].reference,
    ) == ("Maker", "ZX-417")


def test_targeted_planning_receives_up_to_four_active_leads():
    references = ("ZX-41-7", "QK-900", "RT-200", "LM-400")

    class FourLeadFetcher(_Fetcher):
        def fetch(self, url: str) -> PageContent:
            self.calls.append(url)
            identities = " ".join(f"Maker {reference}" for reference in references)
            return PageContent(
                url,
                f"{identities} technical product data",
                identities,
                "scrapling",
            )

    def handler(call_number: int, call: dict) -> list[PageAnalysis]:
        analyses = _empty_analyses(call_number, call)
        if call_number == 1:
            first = analyses[0]
            analyses[0] = PageAnalysis(
                page_url=first.page_url,
                content=first.content,
                audit=first.audit,
                proposals=tuple(
                    CandidateProposal(brand="Maker", reference=reference)
                    for reference in references
                ),
                mode=call["mode"],
            )
        return analyses

    service, planner, _, _, _ = _service(
        fetcher=FourLeadFetcher(),
        analyzer=_Analyzer(handler),
    )

    service.run("source sheet", "Maker")

    first_targeted = planner.targeted_calls[0]["candidates"]
    assert len(first_targeted) == 4
    assert {lead.reference for lead in first_targeted} == set(references)


def test_redirected_targeted_urls_merge_context_and_block_later_direct_refetch():
    """Deux alias redirigés vers une page finale produisent une seule analyse."""
    discovery_url = "https://maker.com/discovery"
    alias_a = "https://search.example/redirect-a"
    alias_b = "https://catalog.example/redirect-b"
    final_url = "https://maker.com/products/shared"

    class RedirectGateway:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def search(self, query: str) -> SearchBatch:
            self.calls.append(query)
            call = len(self.calls)
            hit_url = {
                1: discovery_url,
                5: alias_a,
                6: alias_b,
                9: final_url,
            }.get(call)
            hits = [] if hit_url is None else [SearchHit(
                url=hit_url,
                title="Maker ZX-41-7 and Maker QK-900",
                snippet=f"snippet-{call}",
                engine="bing",
                rank=1,
            )]
            return SearchBatch(
                query=query,
                hits=hits,
                attempts=[SearchAttempt(
                    engine="bing",
                    status="ok",
                    result_count=len(hits),
                )],
            )

    class RedirectFetcher:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def fetch(self, url: str) -> PageContent:
            self.calls.append(url)
            content = "Maker ZX-41-7 and Maker QK-900 technical data"
            if url in {alias_a, alias_b}:
                return PageContent(
                    final_url,
                    content,
                    "Maker shared product page",
                    "scrapling",
                )
            return PageContent(url, content, "Maker discovery page", "scrapling")

    def handler(call_number: int, call: dict) -> list[PageAnalysis]:
        analyses = _empty_analyses(call_number, call)
        if call_number == 1:
            first = analyses[0]
            analyses[0] = PageAnalysis(
                page_url=first.page_url,
                content=first.content,
                audit=first.audit,
                proposals=(
                    CandidateProposal(brand="Maker", reference="ZX-41-7"),
                    CandidateProposal(brand="Maker", reference="QK-900"),
                ),
                mode=call["mode"],
            )
        return analyses

    document_calls: list[dict] = []

    def document_builder(**kwargs):
        document_calls.append(kwargs)
        return build_discovery_document(**kwargs)

    gateway = RedirectGateway()
    fetcher = RedirectFetcher()
    analyzer = _Analyzer(handler)
    service, planner, _, _, _ = _service(
        planner=_Planner(),
        gateway=gateway,
        fetcher=fetcher,
        analyzer=analyzer,
        document_builder=document_builder,
    )

    outcome = service.run("source sheet", "Maker")

    assert len(planner.targeted_calls) == 2
    assert [call["mode"] for call in analyzer.calls] == [
        "discovery",
        "targeted",
        "discovery",
        "targeted",
    ]
    assert [page.url for page in analyzer.calls[1]["pages"]] == [
        final_url,
        discovery_url,
    ]
    assert analyzer.calls[2]["pages"] == ()
    assert list(analyzer.calls[1]["documents_by_url"]) == [
        final_url,
        discovery_url,
    ]
    # Les deux pistes sont autorisées sur la page fusionnée ; leur ordre
    # dépend de la requête qui les a découvertes et n'est pas un contrat.
    assert sorted(
        (lead.brand, lead.reference)
        for lead in analyzer.calls[1]["authorized_by_url"][final_url]
    ) == [
        ("Maker", "QK-900"),
        ("Maker", "ZX-41-7"),
    ]
    targeted_documents = [
        call for call in document_calls if call["url"] == final_url
    ]
    # La page finale est construite une fois apres la redirection, puis une
    # seconde fois depuis le cache lors d'un audit cible ulterieur. Elle n'est
    # jamais retelechargee.
    assert len(targeted_documents) == 2
    assert targeted_documents[0]["snippets"] == ["snippet-5", "snippet-6"]
    assert fetcher.calls == [discovery_url, alias_a, alias_b]
    assert [
        item for item in outcome.diagnostics.page_states if item["wave"] == 2
    ][0]["url"] == final_url


def test_run_without_leads_keeps_four_general_waves_and_exact_global_budgets():
    service, planner, gateway, fetcher, analyzer = _service(
        gateway=_Gateway(hits_per_query=12, identity_metadata=False),
        fetcher=_Fetcher(identity_metadata=False),
    )

    outcome = service.run("source sheet", "Maker")

    assert outcome.status == "not_resolved"
    assert len(planner.general_calls) == 4
    assert planner.targeted_calls == []
    assert [call["mode"] for call in analyzer.calls] == ["discovery"] * 4
    # Quatre vagues de trois : le budget global reste douze.
    assert outcome.diagnostics.logical_queries == len(gateway.calls) == 12
    assert outcome.diagnostics.pages_opened == len(fetcher.calls) == 36
    assert outcome.diagnostics.final_state == "no_candidate_discovered"


def test_deterministic_discovery_survives_page_graph_failures():
    """Chaque contenu litteral doit alimenter le registre meme sans PageAnalysis."""
    class FailingAnalyzer:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def __call__(self, pages, requirement_set, target_brand, graph_config, **kwargs):
            self.calls.append({"pages": tuple(pages), "mode": kwargs["mode"]})
            return [], ["Page non exploitee (https://maker.com/x) : RuntimeError."]

    analyzer = FailingAnalyzer()
    service, planner, _, _, _ = _service(
        analyzer=analyzer,  # type: ignore[arg-type]
        gateway=_Gateway(identity_metadata=False),
        fetcher=_Fetcher(identity_metadata=True),
    )

    outcome = service.run("source sheet", "Maker")

    # Le repli deterministe rend la piste ciblable des la vague 2. Son quota de
    # deux angles y est entierement consomme, donc les vagues suivantes
    # reprennent la decouverte : c'est le registre qui compte ici, pas le
    # nombre de tours ciblés.
    assert [call["mode"] for call in analyzer.calls] == [
        "discovery",
        "targeted",
        "discovery",
        "discovery",
    ]
    assert len(planner.targeted_calls) == 1
    assert ("Maker", "ZX-41-7") in {
        (item["brand"], item["reference"])
        for item in outcome.diagnostics.candidate_leads
    }


def test_lead_only_run_is_unresolved_without_deterministic_evaluation():
    def handler(call_number: int, call: dict) -> list[PageAnalysis]:
        analyses = _empty_analyses(call_number, call)
        if call_number == 1:
            first = analyses[0]
            analyses[0] = PageAnalysis(
                page_url=first.page_url,
                content=first.content,
                audit=first.audit,
                proposals=(CandidateProposal(brand="Maker", reference="ZX-41-7"),),
                mode=call["mode"],
            )
        return analyses

    service, _, _, _, _ = _service(analyzer=_Analyzer(handler))

    outcome = service.run("source sheet", "Maker")

    assert outcome.status == "not_resolved"
    assert outcome.evaluation is None
    assert not any(audit.candidates for audit in outcome.audits)
    assert outcome.diagnostics.final_state == "candidates_discovered_but_not_audited"


def test_run_merges_criteria_proved_on_two_pages_for_one_canonical_identity():
    def handler(call_number: int, call: dict) -> list[PageAnalysis]:
        first, second = call["pages"][:2]
        return [
            PageAnalysis(
                page_url=first.url,
                content=first.content,
                audit=PageAudit(
                    page_url=first.url,
                    candidates=[_candidate(
                        first.url,
                        ("proven", "proven", "not_proven", "not_proven"),
                        reference="ZX-41-7",
                    )],
                ),
                mode=call["mode"],
            ),
            PageAnalysis(
                page_url=second.url,
                content=second.content,
                audit=PageAudit(
                    page_url=second.url,
                    candidates=[_candidate(
                        second.url,
                        ("not_proven", "not_proven", "proven", "proven"),
                        reference="ZX-41-7",
                    )],
                ),
                mode=call["mode"],
            ),
        ]

    service, _, _, _, _ = _service(analyzer=_Analyzer(handler))

    outcome = service.run("source sheet", "Maker")

    assert outcome.status == "complete"
    assert outcome.evaluation is not None
    assert outcome.evaluation.summary.score == 100


def _sequence_de_candidats(*etats):
    """Emet les etats de candidat dans l'ordre, sans compter les appels.

    La boucle adaptative insere des vagues ciblees selon ce qu'elle apprend, et
    la porte ecarte les pages qui ne portent pas l'identite : indexer sur le
    numero d'appel de l'analyseur rendrait ces scenarios dependants de la
    composition des vagues, qui n'est pas leur objet.

    Le dernier etat persiste donc sur les appels suivants, jusqu'a ce qu'une
    page le porte reellement. C'est le constat final qui est teste, pas la
    vague qui l'a produit.
    """
    restants = list(etats)
    premiere = [True]

    def handler(call_number: int, call: dict) -> list[PageAnalysis]:
        analyses = _empty_analyses(call_number, call)
        if not analyses or not restants:
            return analyses
        statuts = restants.pop(0) if len(restants) > 1 else restants[0]
        premier = analyses[0]
        propositions = (
            (CandidateProposal(brand="Maker", reference="ZX-41-7"),)
            if premiere[0] else ()
        )
        premiere[0] = False
        analyses[0] = PageAnalysis(
            page_url=premier.page_url,
            content=premier.content,
            audit=PageAudit(
                page_url=premier.page_url,
                candidates=[_candidate(premier.page_url, statuts)],
            ),
            proposals=propositions,
            mode=call["mode"],
        )
        return analyses

    return handler


def test_later_incompatibility_replaces_an_older_eligible_partial_candidate():
    """Une fusion cumulative defavorable invalide le meilleur historique."""
    handler = _sequence_de_candidats(
        ("proven", "proven", "proven", "not_proven"),
        ("proven", "proven", "proven", "incompatible"),
    )

    service, _, _, _, _ = _service(analyzer=_Analyzer(handler))

    outcome = service.run("source sheet", "Maker")

    assert outcome.status == "rejected"
    assert outcome.evaluation is None
    merged = merge_page_audits(
        outcome.audits,
        outcome.requirements,
        outcome.visited_pages,
    )
    assert merged[0].candidates[0].criteria[3].status == "incompatible"


def test_missing_resets_to_all_criteria_after_current_candidate_is_invalidated():
    class FailSecondTargetedPlanner(_Planner):
        def plan_targeted_queries(self, requirement_set, candidates, previous):
            if len(self.targeted_calls) == 1:
                selected = tuple(candidates)
                self.targeted_calls.append({
                    "candidates": selected,
                    "previous": frozenset(previous),
                })
                raise PlanningError("forced targeted fallback")
            return super().plan_targeted_queries(
                requirement_set, candidates, previous
            )

    handler = _sequence_de_candidats(
        ("proven", "proven", "proven", "not_proven"),
        ("proven", "proven", "proven", "incompatible"),
    )

    planner = FailSecondTargetedPlanner()
    service, _, _, _, _ = _service(
        planner=planner,
        analyzer=_Analyzer(handler),
    )

    service.run("source sheet", "Maker")

    assert len(planner.general_calls) == 3
    assert planner.general_calls[-1]["missing"] == (
        "Criterion 1",
        "Criterion 2",
        "Criterion 3",
        "Criterion 4",
    )


def test_targeted_planning_failure_falls_back_once_to_general_with_safe_warning():
    planner = _Planner(fail_targeted_once=True)

    def handler(call_number: int, call: dict) -> list[PageAnalysis]:
        analyses = _empty_analyses(call_number, call)
        if call_number == 1:
            first = analyses[0]
            analyses[0] = PageAnalysis(
                page_url=first.page_url,
                content=first.content,
                audit=first.audit,
                proposals=(CandidateProposal(brand="Maker", reference="ZX-41-7"),),
                mode=call["mode"],
            )
        return analyses

    service, _, gateway, fetcher, analyzer = _service(
        planner=planner,
        gateway=_Gateway(hits_per_query=12),
        analyzer=_Analyzer(handler),
    )

    outcome = service.run("source sheet", "Maker")

    assert len(planner.general_calls) == 3
    assert len(planner.targeted_calls) == 2
    assert [call["mode"] for call in analyzer.calls] == [
        "discovery",
        "discovery",
        "targeted",
        "discovery",
    ]
    assert analyzer.calls[1]["authorized_by_url"]
    assert all(
        leads[0].reference == "ZX-41-7"
        for leads in analyzer.calls[1]["authorized_by_url"].values()
    )
    # Onze et non douze : la vague ciblée n'envoie que deux requêtes, le quota
    # d'angles de l'unique piste valant deux. Le plafond global reste douze.
    assert outcome.diagnostics.logical_queries == len(gateway.calls) == 11
    assert outcome.diagnostics.logical_queries <= MAX_LOGICAL_QUERIES
    assert outcome.diagnostics.pages_opened == len(fetcher.calls) == 36
    assert outcome.diagnostics.warnings == [
        "Plan ciblé indisponible : retour à la découverte générale."
    ]


def test_run_uses_injected_registry_document_builder_and_discovery_limits():
    registry = CandidateRegistry()
    calls = {"registry": 0, "documents": 0}
    limits = DiscoveryLimits(content_chars=10, total_chars=80)

    def registry_factory():
        calls["registry"] += 1
        return registry

    def document_builder(**kwargs):
        calls["documents"] += 1
        assert kwargs["limits"] is limits
        return build_discovery_document(**kwargs)

    service, _, _, fetcher, _ = _service(
        registry_factory=registry_factory,
        document_builder=document_builder,
        discovery_limits=limits,
    )

    outcome = service.run("source sheet", "Maker")

    assert calls["registry"] == 1
    assert len(fetcher.calls) == 11
    # Un document par page presentee a la porte : les onze pages recuperees,
    # plus trois pages deja visitees re-auditees depuis le cache autour d'une
    # identite confirmee. Les pistes deterministes faibles restent, elles,
    # exclues du cache par `_cached_product_pages`.
    assert calls["documents"] == 14
    assert all(
        "content" in item["truncated_fields"]
        for item in outcome.diagnostics.page_states
    )


def test_merge_uses_canonical_identity_and_a_clean_page_beats_a_contradiction():
    """Casse/séparateurs fusionnent, mais jamais deux marques distinctes."""
    unrelated_url = "https://other.example/unrelated"
    url_a = "https://maker.com/zx-a"
    url_b = "https://maker.com/zx-b"
    other_brand_url = "https://rival.example/zx"

    unrelated = PageAudit(
        page_url=unrelated_url,
        candidates=[_candidate(
            unrelated_url,
            ("not_proven",) * 4,
            brand="Other",
            reference="QK-900",
        )],
    )
    page_a = PageAudit(
        page_url=url_a,
        candidates=[_candidate(
            url_a,
            ("proven", "not_proven", "proven", "not_proven"),
            reference="ZX-41-7",
        )],
    )
    page_b = PageAudit(
        page_url=url_b,
        candidates=[_candidate(
            url_b,
            ("not_proven", "proven", "incompatible", "not_proven"),
            reference="zx 41 7",
        )],
    )
    other_brand = PageAudit(
        page_url=other_brand_url,
        candidates=[_candidate(
            other_brand_url,
            ("proven", "not_proven", "not_proven", "not_proven"),
            brand="Rival",
            reference="ZX-41-7",
        )],
    )

    visited_pages = {
        url: " ".join(f"proof for r {index}" for index in range(1, 5))
        for url in {unrelated_url, url_a, url_b, other_brand_url}
    }
    merged = merge_page_audits(
        [unrelated, page_a, page_b, other_brand],
        _requirements(),
        visited_pages,
    )
    maker_audits = [
        audit for audit in merged
        if audit.candidates[0].brand.casefold() == "maker"
    ]
    same_reference = [
        audit for audit in merged
        if "".join(
            character
            for character in audit.candidates[0].reference.casefold()
            if character.isalnum()
        ) == "zx417"
    ]

    assert len(maker_audits) == 1
    assert maker_audits[0].page_url == url_a
    assert {
        item.requirement_id: item.status
        for item in maker_audits[0].candidates[0].criteria
    } == {
        "r1": "proven",
        "r2": "proven",
        "r3": "proven",
        "r4": "not_proven",
    }
    assert len(same_reference) == 2
    assert {audit.candidates[0].brand for audit in same_reference} == {
        "Maker",
        "Rival",
    }


def test_list_audit_cannot_override_product_evidence_during_merge():
    list_url = "https://market.example/shop/contactors?_nkw=zx-41-7"
    product_url = "https://maker.com/products/zx-41-7"
    list_audit = PageAudit(
        page_url=list_url,
        candidates=[_candidate(
            list_url,
            ("incompatible", "not_proven", "not_proven", "not_proven"),
            reference="ZX-41-7",
        )],
    )
    product_audit = PageAudit(
        page_url=product_url,
        candidates=[_candidate(
            product_url,
            ("proven", "not_proven", "not_proven", "not_proven"),
            reference="ZX-41-7",
        )],
    )

    merged = merge_page_audits(
        [list_audit, product_audit],
        _requirements(),
        {product_url: "proof for r 1"},
    )

    assert len(merged) == 1
    assert merged[0].page_url == product_url
    assert merged[0].candidates[0].criteria[0].status == "proven"
    assert {
        proof.url
        for proof in merged[0].candidates[0].criteria[0].proofs
    } == {product_url}


def test_merge_validates_each_page_before_applying_page_disjunction():
    """Une preuve inventee ne peut pas masquer une incompatibilite verifiee."""
    invented_url = "https://bad.example/products/zx-41-7"
    incompatible_url = "https://maker.example/products/zx-41-7"
    invented = PageAudit(
        page_url=invented_url,
        candidates=[_candidate(
            invented_url,
            ("proven", "not_proven", "not_proven", "not_proven"),
        )],
    )
    incompatible = PageAudit(
        page_url=incompatible_url,
        candidates=[_candidate(
            incompatible_url,
            ("incompatible", "not_proven", "not_proven", "not_proven"),
        )],
    )

    merged = merge_page_audits(
        [invented, incompatible],
        _requirements(),
        {
            invented_url: "Maker ZX-41-7 sans preuve du critere",
            incompatible_url: "Maker ZX-41-7 proof for r 1",
        },
    )

    criterion = merged[0].candidates[0].criteria[0]
    assert criterion.status == "incompatible"
    assert [proof.url for proof in criterion.proofs] == [incompatible_url]


def test_merge_drops_annotations_from_a_page_whose_only_verdict_loses():
    clean_url = "https://maker.example/products/zx-41-7"
    stale_url = "https://stale.example/products/zx-41-7"
    clean_candidate = _candidate(
        clean_url,
        ("proven", "not_proven", "not_proven", "not_proven"),
    )
    clean_candidate.deviations = ["Source propre retenue"]
    stale_candidate = _candidate(
        stale_url,
        ("incompatible", "not_proven", "not_proven", "not_proven"),
    )
    stale_candidate.deviations = ["Avertissement obsolète"]

    merged = merge_page_audits(
        [
            PageAudit(page_url=clean_url, candidates=[clean_candidate]),
            PageAudit(page_url=stale_url, candidates=[stale_candidate]),
        ],
        _requirements(),
        {
            clean_url: "Maker ZX-41-7 proof for r 1",
            stale_url: "Maker ZX-41-7 proof for r 1",
        },
    )

    assert merged[0].candidates[0].deviations == ["Source propre retenue"]


def test_merge_drops_page_annotations_when_any_conclusive_verdict_loses():
    clean_url = "https://maker.example/products/zx-41-7"
    mixed_url = "https://mixed.example/products/zx-41-7"
    clean = _candidate(
        clean_url,
        ("proven", "not_proven", "not_proven", "not_proven"),
    )
    mixed = _candidate(
        mixed_url,
        ("incompatible", "proven", "not_proven", "not_proven"),
    )
    mixed.deviations = ["Annotation de page contradictoire"]

    merged = merge_page_audits(
        [
            PageAudit(page_url=clean_url, candidates=[clean]),
            PageAudit(page_url=mixed_url, candidates=[mixed]),
        ],
        _requirements(),
        {
            clean_url: "proof for r 1",
            mixed_url: "proof for r 1 proof for r 2",
        },
    )

    statuses = {
        item.requirement_id: item.status
        for item in merged[0].candidates[0].criteria
    }
    assert statuses["r1"] == statuses["r2"] == "proven"
    assert merged[0].candidates[0].deviations == []


def _content_for(statuses, *, extra: str = "") -> str:
    """Contenu de page assorti a ce que `statuses` prouve reellement.

    Le contenu par defaut de `_Fetcher` porte les quatre `proof for r N` sans
    condition ; laisser cette phrase pour un critere volontairement
    `not_proven` le ferait retrouver par le reaudit des pages officielles.
    """
    phrases = " ".join(
        f"proof for r {index}" for index, status in enumerate(statuses, start=1)
        if status != "not_proven"
    )
    return (
        f"Maker ZX-41-7 technical data {phrases} "
        + ("bounded corpus text " * 20)
        + extra
    )


def test_partial_best_candidate_uses_candidate_selected_final_state():
    def handler(call_number: int, call: dict) -> list[PageAnalysis]:
        if call_number > 1:
            return _empty_analyses(call_number, call)
        page = call["pages"][0]
        statuses = ("proven", "proven", "proven", "not_proven")
        return [PageAnalysis(
            page_url=page.url,
            content=_content_for(statuses),
            audit=PageAudit(
                page_url=page.url,
                candidates=[_candidate(page.url, statuses)],
            ),
            mode=call["mode"],
        )]

    service, _, _, _, _ = _service(analyzer=_Analyzer(handler), fetcher=_Fetcher(identity_metadata=False))

    outcome = service.run("source sheet", "Maker")

    assert outcome.status == "partial"
    assert outcome.evaluation is not None
    assert outcome.evaluation.summary.score == 75
    assert outcome.diagnostics.final_state == "candidate_selected"


def test_invented_excerpt_is_downgraded_and_never_counts_as_proven():
    """Mutation détectée : une citation inventée compte comme preuve.

    B2 audite en `strict_evidence=False` : le contrat de preuve reste strict —
    l'extrait introuvable est refusé — mais son refus rétrograde le critère au
    lieu de supprimer le candidat. Un candidat dont les quatre critères sont
    invérifiables reste donc sans preuve, et la mission n'aboutit pas.
    """
    def handler(call_number: int, call: dict) -> list[PageAnalysis]:
        if call_number > 1:
            return _empty_analyses(call_number, call)
        page = call["pages"][0]
        invalid = _candidate(page.url, ("proven",) * 4)
        for criterion in invalid.criteria:
            criterion.proofs[0].excerpt = "invented fragment"
        non_verifiable = _candidate(
            page.url,
            ("not_proven",) * 4,
            reference="QK-900",
        )
        content = _content_for(("not_proven",) * 4, extra=" Maker QK-900")
        return [PageAnalysis(
            page_url=page.url,
            content=content,
            audit=PageAudit(
                page_url=page.url,
                candidates=[invalid, non_verifiable],
            ),
            mode=call["mode"],
        )]

    service, _, _, _, _ = _service(analyzer=_Analyzer(handler), fetcher=_Fetcher(identity_metadata=False))

    outcome = service.run("source sheet", "Maker")

    assert outcome.status == "not_resolved"
    assert outcome.evaluation is None
    # Le fragment inventé ne fuite jamais dans les diagnostics.
    assert all(
        "invented fragment" not in warning
        for warning in outcome.diagnostics.warnings
    )


def test_candidate_selected_outranks_a_neighbour_with_unverifiable_proof():
    """Un candidat éligible reste prioritaire sur un voisin invérifiable."""
    def handler(call_number: int, call: dict) -> list[PageAnalysis]:
        if call_number > 1:
            return _empty_analyses(call_number, call)
        page = call["pages"][0]
        invalid = _candidate(page.url, ("proven",) * 4)
        for criterion in invalid.criteria:
            criterion.proofs[0].excerpt = "invented fragment"
        eligible = _candidate(
            page.url,
            ("proven", "proven", "proven", "not_proven"),
            reference="QK-900",
        )
        content = _content_for(
            ("proven", "proven", "proven", "not_proven"), extra=" Maker QK-900"
        )
        return [PageAnalysis(
            page_url=page.url,
            content=content,
            audit=PageAudit(
                page_url=page.url,
                candidates=[invalid, eligible],
            ),
            mode=call["mode"],
        )]

    service, _, _, _, _ = _service(analyzer=_Analyzer(handler), fetcher=_Fetcher(identity_metadata=False))

    outcome = service.run("source sheet", "Maker")

    assert outcome.status == "partial"
    assert outcome.evaluation is not None
    assert outcome.evaluation.summary.reference == "QK-900"
    assert outcome.diagnostics.final_state == "candidate_selected"


def test_post_filter_candidate_without_strict_error_is_audited_but_non_verifiable():
    def handler(call_number: int, call: dict) -> list[PageAnalysis]:
        if call_number > 1:
            return _empty_analyses(call_number, call)
        page = call["pages"][0]
        statuses = ("not_proven",) * 4
        return [PageAnalysis(
            page_url=page.url,
            content=_content_for(statuses),
            audit=PageAudit(
                page_url=page.url,
                candidates=[_candidate(page.url, statuses)],
            ),
            mode=call["mode"],
        )]

    service, _, _, _, _ = _service(analyzer=_Analyzer(handler), fetcher=_Fetcher(identity_metadata=False))

    outcome = service.run("source sheet", "Maker")

    assert outcome.status == "not_resolved"
    assert outcome.evaluation is None
    assert outcome.diagnostics.final_state == "audited_but_non_verifiable"


def test_rejected_candidate_diagnostics_merge_parser_and_registry_rejections():
    def handler(call_number: int, call: dict) -> list[PageAnalysis]:
        analyses = _empty_analyses(call_number, call)
        if call_number == 1:
            first = analyses[0]
            rejected = RejectedCandidate(
                "Maker", "ABSENT-99", "reference_not_literal"
            )
            analyses[0] = PageAnalysis(
                page_url=first.page_url,
                content=first.content,
                audit=first.audit,
                proposals=(
                    CandidateProposal(brand="Maker", reference="ZX-41-7"),
                    CandidateProposal(brand="Maker", reference="ABSENT-99"),
                ),
                proposal_rejections=(
                    rejected,
                    RejectedCandidate(
                        " maker ", "ABSENT 99", "REFERENCE_NOT_LITERAL"
                    ),
                ),
                mode=call["mode"],
            )
        return analyses

    service, _, _, _, _ = _service(analyzer=_Analyzer(handler))

    outcome = service.run("source sheet", "Maker")

    assert outcome.diagnostics.rejected_candidates == [{
        "brand": "Maker",
        "reference": "ABSENT-99",
        "reason": "reference_not_literal",
    }]


def test_selected_page_without_analysis_has_safe_failed_diagnostic_shape():
    analyzer = _Analyzer(lambda call_number, call: [])
    service, _, _, _, _ = _service(analyzer=analyzer)

    outcome = service.run("source sheet", None)

    assert outcome.diagnostics.page_states[0] == {
        "wave": 1,
        "mode": "discovery",
        "url": "https://maker.com/call-1/rank-1",
        "selected": True,
        "fetched": True,
        "analysis_status": "failed",
        "audit_candidate_count": 0,
        "truncated_fields": [],
    }
    assert "invented fragment" not in repr(outcome.diagnostics.page_states)


def test_page_states_stay_attached_to_their_page_when_the_gate_rejects_one(
    monkeypatch,
):
    """Regression visee : un rejet de la porte decale tout le journal d'un cran."""
    service, _, _, _, _ = _service()
    porte = service._qualify_pages
    retenues: list[str] = []

    def rejeter_la_premiere(pages, *args, **kwargs):
        admises = porte(pages, *args, **kwargs)
        survivantes = admises[1:] if len(admises) > 1 else admises
        retenues.extend(page.url for page in survivantes)
        return survivantes

    monkeypatch.setattr(service, "_qualify_pages", rejeter_la_premiere)

    outcome = service.run("source sheet", "Maker")

    assert [item["url"] for item in outcome.diagnostics.page_states] == retenues


def test_live_flow_reaudits_an_already_fetched_page_after_identity_confirmation():
    """Regression visee : le cache enrichit le rejeu, mais pas le run reel."""
    secondary_url = "https://distributor.example/products/bsl07"
    official_url = (
        "https://new.norel.example/products/fr/4KBL103001R8110/"
        "bsl07-20-10-81"
    )
    requirements = RequirementSet(
        product="Contacteur de puissance tripolaire NV1T05BD",
        origin_brand="Kerion Electric Tersa D",
        criteria=[
            Requirement(id="fabricant", label="Fabricant", requested_value="Kerion Electric"),
            Requirement(id="reference_exacte", label="Reference exacte", requested_value="NV1T05BD"),
            Requirement(id="famille", label="Famille", requested_value="Tersa D"),
            Requirement(id="fonction", label="Fonction", requested_value="Contacteur de puissance tripolaire"),
            Requirement(id="nombre_de_poles", label="Nombre de poles", requested_value="3P"),
            Requirement(id="courant_nominal", label="Courant nominal", requested_value="9 A (AC-3)"),
            Requirement(id="tension_de_bobine", label="Tension de bobine", requested_value="24 V DC"),
            Requirement(id="contacts_principaux", label="Contacts principaux", requested_value="3 NO"),
            Requirement(id="usage_vise", label="Usage vise", requested_value="Commande de moteur / charge industrielle"),
        ],
    )
    secondary_content = (
        "Norel 4KBL103001R8110 BSL07. Contacteur AS 9A AC3-3P+1NF-24VDC. "
        "Nombre de contacts a fermeture en tant que contacts principaux: 3. "
        "Utilise pour la commande de moteurs triphases."
    )
    official_content = (
        "Norel 4KBL103001R8110 BSL07-20-10-81. "
        "Contacteur de puissance tripolaire. 3P."
    )

    class CachePlanner(_Planner):
        def extract_requirements(self, fiche: str) -> RequirementSet:
            return requirements

    class CacheGateway(_Gateway):
        def search(self, query: str) -> SearchBatch:
            self.calls.append(query)
            call = len(self.calls)
            hit_url = {1: secondary_url, 4: official_url}.get(call)
            hits = [] if hit_url is None else [SearchHit(
                url=hit_url,
                title=(
                    "Norel 4KBL103001R8110 BSL07 technical data"
                    if call == 1 else
                    "Norel 4KBL103001R8110 official product"
                ),
                snippet="Norel 4KBL103001R8110",
                engine="bing",
                rank=1,
            )]
            return SearchBatch(query=query, hits=hits)

    class CacheFetcher(_Fetcher):
        def fetch(self, url: str) -> PageContent:
            self.calls.append(url)
            if url == secondary_url:
                return PageContent(url, secondary_content, "Norel BSL07", "scrapling")
            return PageContent(url, official_content, "Norel official", "scrapling")

    def official_candidate(url: str) -> CandidateAudit:
        official_reference = SourceProof(
            url=url,
            excerpt="4KBL103001R8110",
            type="web_officiel",
        )
        return CandidateAudit(
            brand="Norel",
            reference="4KBL103001R8110",
            criteria=[
                CriterionAudit(
                    requirement_id="fabricant",
                    requested_value="Kerion Electric",
                    observed_value="Norel",
                    status="incompatible",
                    proofs=[official_reference],
                ),
                CriterionAudit(
                    requirement_id="reference_exacte",
                    requested_value="NV1T05BD",
                    observed_value="4KBL103001R8110",
                    status="incompatible",
                    proofs=[official_reference],
                ),
                CriterionAudit(
                    requirement_id="famille",
                    requested_value="Tersa D",
                    observed_value="BSL07",
                    status="incompatible",
                    proofs=[SourceProof(
                        url=url,
                        excerpt="BSL07-20-10-81",
                        type="web_officiel",
                    )],
                ),
                CriterionAudit(
                    requirement_id="fonction",
                    requested_value="Contacteur de puissance tripolaire",
                    observed_value="Contacteur de puissance tripolaire",
                    status="proven",
                    proofs=[SourceProof(
                        url=url,
                        excerpt="Contacteur de puissance tripolaire",
                        type="web_officiel",
                    )],
                ),
                CriterionAudit(
                    requirement_id="nombre_de_poles",
                    requested_value="3P",
                    observed_value="3P",
                    status="proven",
                    proofs=[SourceProof(
                        url=url,
                        excerpt="3P",
                        type="web_officiel",
                    )],
                ),
                *[
                    CriterionAudit(
                        requirement_id=identifier,
                        requested_value=value,
                        status="not_proven",
                    )
                    for identifier, value in (
                        ("courant_nominal", "9 A (AC-3)"),
                        ("tension_de_bobine", "24 V DC"),
                        ("contacts_principaux", "3 NO"),
                        ("usage_vise", "Commande de moteur / charge industrielle"),
                    )
                ],
            ],
        )

    def handler(call_number: int, call: dict) -> list[PageAnalysis]:
        if not call["pages"]:
            return []
        page = call["pages"][0]
        if call["mode"] == "discovery":
            return [PageAnalysis(
                page_url=page.url,
                content=page.content,
                audit=PageAudit(page_url=page.url),
                proposals=(CandidateProposal(
                    brand="Norel", reference="4KBL103001R8110"
                ),),
                mode="discovery",
            )]
        return [PageAnalysis(
            page_url=page.url,
            content=page.content,
            audit=PageAudit(
                page_url=page.url,
                candidates=[official_candidate(page.url)],
            ),
            mode="targeted",
        )]

    service, _, _, _, _ = _service(
        planner=CachePlanner(),
        gateway=CacheGateway(),
        fetcher=CacheFetcher(),
        analyzer=_Analyzer(handler),
    )

    outcome = service.run("source sheet", "Norel")

    assert outcome.status == "complete"
    assert outcome.evaluation is not None
    assert outcome.evaluation.summary.score == 100
    assert outcome.evaluation.summary.not_proven_criteria == []
    assert outcome.evaluation.proof_url_count == 2


def test_targeted_wave_audits_the_already_seen_identity_page_without_refetching():
    """Une fiche decouverte ne doit pas etre perdue par la deduplication URL."""
    official_url = "https://maker.com/products/zx-41-7"

    class SamePageGateway(_Gateway):
        def search(self, query: str) -> SearchBatch:
            self.calls.append(query)
            return SearchBatch(
                query=query,
                hits=[SearchHit(
                    url=official_url,
                    title="Maker ZX-41-7 official product",
                    snippet="Maker ZX-41-7 technical data",
                    engine="bing",
                    rank=1,
                )],
            )

    def handler(call_number: int, call: dict) -> list[PageAnalysis]:
        if not call["pages"]:
            return []
        page = call["pages"][0]
        if call["mode"] == "discovery":
            return [PageAnalysis(
                page_url=page.url,
                content=page.content,
                audit=PageAudit(page_url=page.url),
                proposals=(CandidateProposal(
                    brand="Maker", reference="ZX-41-7",
                ),),
                mode="discovery",
            )]
        return [PageAnalysis(
            page_url=page.url,
            content=page.content,
            audit=PageAudit(
                page_url=page.url,
                candidates=[_candidate(page.url, ("proven",) * 4)],
            ),
            mode="targeted",
        )]

    service, _, _, fetcher, analyzer = _service(
        gateway=SamePageGateway(),
        analyzer=_Analyzer(handler),
    )

    outcome = service.run("source sheet", "Maker")

    assert outcome.status == "complete"
    # Le point du test : une seule recuperation reseau. La vague ciblee re-audite
    # la page depuis le cache au lieu de la redemander, et les vagues suivantes
    # — la mission ne s'arretant plus a 100 % — n'en ouvrent aucune autre.
    assert fetcher.calls == [official_url]
    assert [call["mode"] for call in analyzer.calls] == [
        "discovery", "targeted", "discovery", "discovery",
    ]


def test_targeted_wave_does_not_audit_a_cached_page_without_literal_brand():
    """Le titre synthétique du cache ne doit pas inventer l'attribution de marque."""
    page_url = "https://maker.com/products/zx-41-7"

    class AmbiguousPageGateway(_Gateway):
        def search(self, query: str) -> SearchBatch:
            self.calls.append(query)
            return SearchBatch(
                query=query,
                hits=[SearchHit(
                    url=page_url,
                    title="Competitor ZX-41-7 product",
                    snippet="Competitor ZX-41-7 technical data",
                    engine="bing",
                    rank=1,
                )],
            )

    def handler(call_number: int, call: dict) -> list[PageAnalysis]:
        if not call["pages"]:
            return []
        page = call["pages"][0]
        if call["mode"] == "discovery":
            return [PageAnalysis(
                page_url=page.url,
                content=page.content,
                audit=PageAudit(page_url=page.url),
                proposals=(CandidateProposal(
                    brand="Maker", reference="ZX-41-7",
                ),),
                mode="discovery",
            )]
        return [PageAnalysis(
            page_url=page.url,
            content=page.content,
            audit=PageAudit(
                page_url=page.url,
                candidates=[_candidate(page.url, ("proven",) * 4)],
            ),
            mode="targeted",
        )]

    service, _, _, fetcher, analyzer = _service(
        gateway=AmbiguousPageGateway(),
        fetcher=_Fetcher(content_by_url={
            page_url: "Competitor ZX-41-7 technical data proof for r 1 "
            "proof for r 2 proof for r 3 proof for r 4",
        }),
        analyzer=_Analyzer(handler),
    )

    outcome = service.run("source sheet", "Maker")

    assert fetcher.calls == [page_url]
    targeted_calls = [
        call for call in analyzer.calls if call["mode"] == "targeted"
    ]
    assert targeted_calls
    assert all(call["pages"] == () for call in targeted_calls)
    assert outcome.diagnostics.candidate_evaluations == []
