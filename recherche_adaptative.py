# -*- coding: utf-8 -*-
"""Orchestration B2 adaptative en quatre vagues autour de ScrapeGraphAI."""

from __future__ import annotations

import re
import inspect
import unicodedata
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Literal, TypeVar
from urllib.parse import unquote

from analyse import (
    PageAnalysis,
    _safe_page_url,
    examiner_pages,
    sanitize_page_audit_diagnostics,
)
from candidats import (
    CandidateLead,
    CandidateProposal,
    CandidateRegistry,
    DiscoveryDocument,
    DiscoveryLimits,
    RejectedCandidate,
    audit_proposals,
    brand_is_present,
    build_discovery_document,
    canonical_candidate_key,
    filter_page_audit,
    is_origin_identity,
    reference_is_present,
)
from compatibilite import (
    CompatibilityContractError,
    EvidenceContractError,
    cached_reaudit_downgraded_criteria,
    canonical_url,
    evaluate_candidates,
    reaudit_cached_product_evidence,
    sanitize_candidate_evidence,
    select_best_candidate,
)
from configuration import B2Config, MAX_LOGICAL_QUERIES, MAX_QUERIES_PER_WAVE
from content_gate import GateCounters, qualify_fetched_page
from identite import IdentityExtractor, deterministic_identity_leads
from indisponibilite import avertissement_est_limitation_llm
from near_miss import (
    assess_candidates,
    build_targeted_tasks,
    select_targeted_queries,
)
from scraping import PageFetchError
from modeles import (
    CandidateAudit,
    CandidateEvaluation,
    CriterionAudit,
    PageAudit,
    RequirementSet,
    SearchAttempt,
    SearchHit,
    SourceProof,
)
from planification import PlanningError, sanitize_requirement_diagnostics
from recherche import (
    SearxUnavailable,
    est_page_de_liste,
    metadata_has_numeric_anchor,
    numeric_query_anchors,
)
from robustesse import hors_sujet, url_exploitable
from rejeu import (
    candidate_contract_error_diagnostic,
    candidate_evaluation_diagnostic,
    capture_replay_if_configured,
    journaliser_evaluation,
)
from scraping import PageContent


@dataclass(frozen=True)
class HitContext:
    wave: int
    query: str
    candidate_keys: tuple[tuple[str, str], ...]
    hit: SearchHit


@dataclass(frozen=True)
class SelectedHitContext:
    url: str
    hits: tuple[SearchHit, ...]
    queries: tuple[str, ...]
    candidate_keys: tuple[tuple[str, str], ...]


def _cached_product_pages(
    leads_by_key: Mapping[tuple[str, str], CandidateLead],
    candidate_keys: Sequence[tuple[str, str]],
    visited_pages: Mapping[str, str],
    *,
    already_audited: set[tuple[str, tuple[str, str]]],
) -> list[tuple[str, str, tuple[tuple[str, str], ...]]]:
    """Sélectionne les pages cache liées littéralement à un candidat audité.

    La provenance de découverte d'une page n'a aucune autorité ici : seule la
    page réellement récupérée peut rendre une identité candidate admissible au
    ré-audit. Les garde-fous produit restent inchangés.
    """
    grouped: dict[str, tuple[str, str, list[tuple[str, str]]]] = {}
    for audited_key in dict.fromkeys(candidate_keys):
        # Le registre peut avoir remplacé une référence longue par sa forme
        # courte (ou l'inverse) depuis l'audit. Retrouver la piste active par
        # l'invariant de fusion évite de rendre le cache invisible.
        active_leads = [
            (active_key, lead)
            for active_key, lead in leads_by_key.items()
            if _same_nested_candidate_identity(active_key, audited_key)
        ]
        for key, lead in active_leads:
            if lead.low_confidence:
                continue
            for url, content in visited_pages.items():
                canonical = canonical_url(url)
                cache_key = (canonical, key)
                if (
                    not canonical
                    or cache_key in already_audited
                    or not content
                    or est_page_de_liste(url)
                    or not brand_is_present(lead.brand, content)
                    or not reference_is_present(lead.reference, content)
                ):
                    continue
                already_audited.add(cache_key)
                if canonical not in grouped:
                    grouped[canonical] = (url, content, [])
                grouped[canonical][2].append(key)
    return [
        (url, content, tuple(dict.fromkeys(keys)))
        for url, content, keys in grouped.values()
    ]


_ContextValue = TypeVar("_ContextValue")


def _stable_union(
    existing: tuple[_ContextValue, ...],
    additions: tuple[_ContextValue, ...],
) -> tuple[_ContextValue, ...]:
    merged = list(existing)
    for item in additions:
        if item not in merged:
            merged.append(item)
    return tuple(merged)


def _merge_fetched_contexts(
    selected_contexts: Sequence[SelectedHitContext],
    pages: Sequence[PageContent],
    *,
    seen_urls: set[str],
    processed_final_urls: set[str],
) -> tuple[list[SelectedHitContext], list[PageContent]]:
    """Agrège les alias après redirection avant document et analyse."""
    grouped: dict[str, tuple[SelectedHitContext, PageContent]] = {}
    for selected, page in zip(selected_contexts, pages):
        final_url = canonical_url(page.url)
        if not final_url:
            continue
        seen_urls.add(final_url)
        if final_url in processed_final_urls:
            continue
        existing = grouped.get(final_url)
        if existing is None:
            grouped[final_url] = (
                SelectedHitContext(
                    url=page.url,
                    hits=selected.hits,
                    queries=selected.queries,
                    candidate_keys=selected.candidate_keys,
                ),
                page,
            )
            continue
        context, first_page = existing
        grouped[final_url] = (
            SelectedHitContext(
                url=context.url,
                hits=_stable_union(context.hits, selected.hits),
                queries=_stable_union(context.queries, selected.queries),
                candidate_keys=_stable_union(
                    context.candidate_keys, selected.candidate_keys
                ),
            ),
            first_page,
        )

    processed_final_urls.update(grouped)
    return (
        [context for context, _ in grouped.values()],
        [page for _, page in grouped.values()],
    )


def select_hit_contexts(
    contexts_by_query: Sequence[Sequence[HitContext]],
    *,
    seen_urls: set[str],
    limit: int,
) -> list[SelectedHitContext]:
    """Sélectionne les pages équitablement sans perdre le contexte des doublons."""
    aggregated: dict[str, dict[str, object]] = {}
    ordered_by_query: list[list[HitContext]] = []
    for contexts in contexts_by_query:
        ordered = sorted(contexts, key=lambda item: item.hit.rank)
        ordered_by_query.append(ordered)
        for context in ordered:
            canonical = canonical_url(context.hit.url)
            if (
                not canonical
                or canonical in seen_urls
                or not url_exploitable(context.hit.url)
                or est_page_de_liste(context.hit.url)
                # Ecarte avant ouverture, donc sans consommer ni temps de
                # recuperation ni place dans le budget de trente-six pages.
                or hors_sujet(context.hit.url)
                or not _hit_matches_context(context)
            ):
                continue
            item = aggregated.setdefault(
                canonical,
                {
                    "url": context.hit.url,
                    "hits": [],
                    "queries": [],
                    "candidate_keys": [],
                },
            )
            hits = item["hits"]
            queries = item["queries"]
            candidate_keys = item["candidate_keys"]
            assert isinstance(hits, list)
            assert isinstance(queries, list)
            assert isinstance(candidate_keys, list)
            if context.hit not in hits:
                hits.append(context.hit)
            if context.query not in queries:
                queries.append(context.query)
            for key in context.candidate_keys:
                if key not in candidate_keys:
                    candidate_keys.append(key)

    selected: list[SelectedHitContext] = []
    selected_urls: set[str] = set()
    ranks = sorted({
        context.hit.rank
        for contexts in ordered_by_query
        for context in contexts
    })
    for rank in ranks:
        for contexts in ordered_by_query:
            for context in contexts:
                if context.hit.rank != rank:
                    continue
                canonical = canonical_url(context.hit.url)
                if canonical in selected_urls or canonical not in aggregated:
                    continue
                item = aggregated[canonical]
                selected.append(SelectedHitContext(
                    url=str(item["url"]),
                    hits=tuple(item["hits"]),  # type: ignore[arg-type]
                    queries=tuple(item["queries"]),  # type: ignore[arg-type]
                    candidate_keys=tuple(item["candidate_keys"]),  # type: ignore[arg-type]
                ))
                selected_urls.add(canonical)
                seen_urls.add(canonical)
                if len(selected) == limit:
                    return selected
    return selected


_QUERY_TOKEN = re.compile(r"[a-z0-9]+(?:[-_./×][a-z0-9]+)*", re.IGNORECASE)
_GENERIC_QUERY_WORDS = frozenset({
    "catalogue", "caracteristiques", "comparison", "comparaison",
    "constructeur", "datasheet", "documentation", "equivalent", "fiche",
    "manufacturer", "produit", "product", "specifications", "technique",
    "technical",
})


def _query_token_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).casefold()
    normalized = "".join(
        character for character in normalized
        if not unicodedata.combining(character)
    )
    return "".join(character for character in normalized if character.isalnum())


def _hit_matches_context(context: HitContext) -> bool:
    """Ne dépense une ouverture que si le résultat reprend la recherche."""
    haystack = "\n".join((
        unquote(context.hit.url), context.hit.title, context.hit.snippet,
    ))
    haystack_key = _query_token_key(haystack)
    if context.candidate_keys:
        return any(
            len(reference) >= 4 and reference in haystack_key
            for _, reference in context.candidate_keys
        )

    family_anchors = numeric_query_anchors(context.query)
    if family_anchors:
        return metadata_has_numeric_anchor(haystack, family_anchors)

    raw_tokens = _QUERY_TOKEN.findall(context.query)
    tokens = [_query_token_key(token) for token in raw_tokens]
    significant = {
        token for token in tokens
        if len(token) >= 4 and token not in _GENERIC_QUERY_WORDS
    }
    # Les doubles de tests et certains moteurs internes emploient des clés
    # opaques (`general-1`). Sans au moins deux termes de recherche réels, la
    # métadonnée du résultat ne permet pas de conclure à l'absence de rapport.
    metadata_tokens = {
        _query_token_key(token)
        for token in _QUERY_TOKEN.findall(
            f"{context.hit.title}\n{context.hit.snippet}"
        )
    }
    metadata_significant = {
        token for token in metadata_tokens
        if len(token) >= 4 and token not in _GENERIC_QUERY_WORDS
    }
    numeric_anchors = {
        normalized for raw, normalized in zip(raw_tokens, tokens)
        if len(normalized) >= 4 and re.search(r"\d{2,}", raw)
    }
    if numeric_anchors:
        if any(anchor in haystack_key for anchor in numeric_anchors):
            return True
        textual_anchors = {
            token for token in significant
            if not any(character.isdigit() for character in token)
        }
        normalized_haystack = unicodedata.normalize("NFKD", haystack).casefold()
        normalized_haystack = "".join(
            character for character in normalized_haystack
            if not unicodedata.combining(character)
        )
        haystack_tokens = set(re.findall(r"[a-z0-9]+", normalized_haystack))
        return any(
            token == anchor or token.startswith(anchor)
            for anchor in textual_anchors
            for token in haystack_tokens
        )
    if len(significant) < 2 or len(metadata_significant) < 2:
        return True
    haystack_tokens = {
        _query_token_key(token) for token in _QUERY_TOKEN.findall(haystack)
    }
    return len(significant & haystack_tokens) >= 2


#: Plafonds de pages, inchanges : ils bornent la mission independamment du
#: nombre de vagues. Avec quatre vagues, le produit par vague les depasserait
#: (4 x 12 = 48) : le plafond global est donc applique a chaque selection.
MAX_PAGES_ANALYZED = 36
MAX_PAGES_OPENED = 36
MAX_TARGETED_LEADS_PER_WAVE = 4

# Les reformulations generales d'une meme piste ont un rendement rapidement
# decroissant. Les directions near-miss, elles, portent un critere et une
# valeur cible : elles disposent donc d'une allocation distincte.
MAX_TARGETED_ANGLE_QUERIES_PER_LEAD = 2
MAX_NEAR_MISS_QUERIES = 3


def _same_nested_candidate_identity(
    left: tuple[str, str],
    right: tuple[str, str],
) -> bool:
    """Aligne le quota d'angles sur la fusion emboitee du registre."""
    if left[0] != right[0]:
        return False
    if left[1] == right[1]:
        return True
    shorter, longer = sorted((left[1], right[1]), key=len)
    return len(shorter) >= 8 and shorter in longer


def _targeted_angle_query_count(
    key: tuple[str, str],
    counts: Mapping[tuple[str, str], int],
) -> int:
    # Une piste longue peut devenir sa forme courte apres une page ciblee.
    # Additionner ses anciennes cles empeche ce renommage de remettre le
    # compteur a zero.
    return sum(
        count for counted_key, count in counts.items()
        if _same_nested_candidate_identity(key, counted_key)
    )


def _eligible_targeted_candidate_keys(
    keys: Sequence[tuple[str, str]],
    counts: Mapping[tuple[str, str], int],
) -> tuple[tuple[str, str], ...]:
    return tuple(
        key for key in keys
        if _targeted_angle_query_count(key, counts)
        < MAX_TARGETED_ANGLE_QUERIES_PER_LEAD
    )


def _prioritize_targeted_leads(
    leads: Sequence[CandidateLead],
    counts: Mapping[tuple[str, str], int],
) -> list[CandidateLead]:
    """Donne d'abord une chance aux pistes ayant reçu le moins d'angles.

    Le tri est stable : à nombre d'angles égal, le rang de découverte reste
    l'ordre de priorité. Ainsi la quatrième piste non envoyée à cause du
    plafond de trois requêtes devient la première de la vague ciblée suivante.
    """
    return sorted(
        (
            lead for lead in leads
            if _targeted_angle_query_count(lead.key, counts)
            < MAX_TARGETED_ANGLE_QUERIES_PER_LEAD
        ),
        key=lambda lead: _targeted_angle_query_count(lead.key, counts),
    )


def _prioritize_distributor_leads(
    leads: Sequence[CandidateLead],
    requirements: RequirementSet,
    previous_queries: Sequence[str],
    domains: Sequence[str],
) -> list[CandidateLead]:
    """Donne d'abord une chance aux pistes jamais cherchées par domaine."""
    previous = {" ".join(query.split()).casefold() for query in previous_queries}

    def sent_count(lead: CandidateLead) -> int:
        return sum(
            f"site:{domain} {lead.reference}".casefold() in previous
            for domain in domains
        )

    return sorted(
        (
            lead for lead in leads
            if not is_origin_identity(lead.brand, lead.reference, requirements)
            and sent_count(lead) < len(domains)
        ),
        key=sent_count,
    )


def _interleave_targeted_queries(
    normal: Sequence,
    distributors: Sequence,
) -> tuple:
    """Alterne les deux chemins sans modifier leurs requêtes elles-mêmes."""
    planned: list = []
    for index in range(max(len(normal), len(distributors))):
        if index < len(normal):
            planned.append(normal[index])
        if index < len(distributors):
            planned.append(distributors[index])
    return tuple(planned)


def _group_targeted_queries(items: Sequence) -> tuple[
    tuple[str, ...],
    dict[str, tuple[tuple[str, str], ...]],
]:
    """Dedoublonne le texte et conserve chaque groupe d'identite debite."""
    ordered: list[str] = []
    keys_by_query: dict[str, list[tuple[str, str]]] = {}
    for item in items:
        query = item.query
        if query not in keys_by_query:
            ordered.append(query)
            keys_by_query[query] = []
        if not any(
            _same_nested_candidate_identity(item.candidate_key, existing)
            for existing in keys_by_query[query]
        ):
            keys_by_query[query].append(item.candidate_key)
    return tuple(ordered), {
        query: tuple(keys) for query, keys in keys_by_query.items()
    }


def _targeted_wave_progressed(
    before: Mapping[tuple[str, str], frozenset[str]],
    after: Mapping[tuple[str, str], frozenset[str]],
) -> bool:
    """Ignore un simple renommage long/court de la meme piste."""
    for key, values in after.items():
        previous: set[str] = set()
        for previous_key, previous_values in before.items():
            if _same_nested_candidate_identity(key, previous_key):
                previous.update(previous_values)
        if values - previous:
            return True
    return False


@dataclass
class ResearchDiagnostics:
    requirement_specs_detected: int = 0
    requirement_specs_covered: int = 0
    requirement_orphans_detected: int = 0
    requirement_orphan_pages: list[int] = field(default_factory=list)
    requirement_second_pass: bool = False
    requirement_validation: list[dict] = field(default_factory=list)
    page_audit_validation: list[dict] = field(default_factory=list)
    llm_rate_limit_count: int = 0
    #: Duree totale de la recherche, en secondes. Une optimisation qu'on
    #: ne mesure pas est une croyance : la duree voyage donc avec le
    #: diagnostic, run apres run.
    duration_seconds: float = 0.0
    waves: int = 0
    logical_queries: int = 0
    engine_calls: int = 0
    pages_opened: int = 0
    # Partition des pages effectivement recuperees :
    #   pages_fetched == pages_analyzed
    #                  + pages_rejected_by_gate
    #                  + pages_not_ready_for_evidence
    pages_fetched: int = 0
    pages_rejected_by_gate: int = 0
    pages_not_ready_for_evidence: int = 0
    pages_analyzed: int = 0
    gate_rejections: list[dict] = field(default_factory=list)
    # Recherche ciblee issue d'un candidat rejete sur incompatibilite prouvee.
    near_miss_candidates_considered: int = 0
    near_miss_tasks_generated: int = 0
    near_miss_tasks_selected: int = 0
    near_miss_tasks_deduplicated: int = 0
    near_miss_queries_scheduled: int = 0
    near_miss_queries_skipped_budget: int = 0
    near_miss_tasks: list[dict] = field(default_factory=list)
    #: Candidats ecartes, avec la condition d eligibilite non satisfaite.
    near_miss_skipped: list[dict] = field(default_factory=list)
    seen_near_miss_signatures: list[str] = field(default_factory=list)
    reserved_directional_signature: str = ""
    #: Une entree par vague, projetee par allowlist : ce que la vague a tente,
    #: ce qu'elle a consomme, ce qu'elle a decouvert.
    wave_details: list[dict] = field(default_factory=list)
    #: Pourquoi la boucle s'est arretee. Jamais vide en fin de mission.
    stop_reason: str = ""
    queries: list[str] = field(default_factory=list)
    attempts: list[SearchAttempt] = field(default_factory=list)
    hits: list[SearchHit] = field(default_factory=list)
    opened_pages: list[str] = field(default_factory=list)
    #: URLs remontees par le moteur puis ecartees comme hors sujet, avant
    #: toute ouverture. Rendre le compte visible est ce qui permet de
    #: verifier que le filtre coupe le bruit et non des pages produit.
    offtopic_urls_skipped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    wave_modes: list[dict] = field(default_factory=list)
    candidate_leads: list[dict] = field(default_factory=list)
    candidate_evaluations: list[dict] = field(default_factory=list)
    rejected_candidates: list[dict] = field(default_factory=list)
    targeted_queries: list[dict] = field(default_factory=list)
    page_states: list[dict] = field(default_factory=list)
    final_state: str = "no_candidate_discovered"


@dataclass
class ResearchOutcome:
    status: Literal[
        "complete",
        "partial",
        "rejected",
        "not_resolved",
        "service_unavailable",
        "analysis_error",
    ]
    requirements: RequirementSet
    evaluation: CandidateEvaluation | None
    audits: list[PageAudit]
    visited_pages: dict[str, str]
    diagnostics: ResearchDiagnostics
    # Métadonnées strictement internes au corpus de rejeu. `rapport.py` ne
    # projette aucun de ces champs dans `alternative.json`.
    audit_waves: list[int] = field(default_factory=list)
    page_waves: dict[str, int] = field(default_factory=dict)
    replay_waves: list[dict] = field(default_factory=list)
    #: Evaluations finales, conservées pour projeter les candidats écartés sans
    #: relire de contenu de page ni de réponse de modèle dans le rapport.
    evaluations: list[CandidateEvaluation] = field(default_factory=list)


def _unique_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = " ".join(value.split())
        key = normalized.casefold()
        if normalized and key not in seen:
            seen.add(key)
            result.append(normalized)
    return result


def _proven_criteria_by_candidate(
    evaluations: Sequence[CandidateEvaluation],
) -> dict[tuple[str, str], frozenset[str]]:
    result: dict[tuple[str, str], set[str]] = {}
    for evaluation in evaluations:
        # Une incompatibilite obligatoire rejette le candidat, meme si un
        # critere secondaire vient d'etre prouve. Ce gain ne doit pas retenir
        # la boucle en targeted sur un produit d'une autre fonction/categorie.
        if evaluation.summary.critical_blockers:
            continue
        key = canonical_candidate_key(
            evaluation.candidate.brand,
            evaluation.candidate.reference,
        )
        result.setdefault(key, set()).update(
            evaluation.summary.proven_criteria
        )
    return {key: frozenset(values) for key, values in result.items()}


def merge_page_audits(
    audits: list[PageAudit],
    requirements: RequirementSet,
    visited_pages: Mapping[str, str],
) -> list[PageAudit]:
    """Cumule les faits d'une même référence sans nouvel appel au modèle."""
    groups: dict[tuple[str, str], list[tuple[str, CandidateAudit]]] = {}
    invalid_audits: list[PageAudit] = []
    for audit in audits:
        # Une liste peut alimenter CandidateRegistry pendant la decouverte,
        # jamais la priorite technique d'un audit fusionne. La retirer ici,
        # avant le choix du meilleur verdict, empeche une liste marchande
        # de supplanter une fiche produit officielle dans un corpus ancien.
        if est_page_de_liste(audit.page_url):
            continue
        for candidate in audit.candidates:
            try:
                sanitized = sanitize_candidate_evidence(
                    requirements,
                    candidate,
                    visited_pages,
                    strict=False,
                )
            except CompatibilityContractError:
                invalid_audits.append(PageAudit(
                    page_url=audit.page_url,
                    candidates=[candidate],
                ))
                continue
            key = canonical_candidate_key(sanitized.brand, sanitized.reference)
            groups.setdefault(key, []).append((audit.page_url, sanitized))

    merged: list[PageAudit] = []
    # Disjonction page par page : une page qui prouve le critere sauve le
    # candidat d'une page contradictoire. `incompatible` ne subsiste que si
    # aucune page produit ne fournit de preuve compatible pour ce critere.
    priority = {"not_proven": 0, "incompatible": 1, "proven": 2}
    for contributions in groups.values():
        # L'identite et l'URL representatives suivent elles aussi la meilleure
        # page, au lieu de dependre de l'ordre d'arrivee des resultats.
        first_page_url, first = max(
            contributions,
            key=lambda contribution: (
                sum(
                    criterion.status == "proven"
                    for criterion in contribution[1].criteria
                ),
                -sum(
                    criterion.status == "incompatible"
                    for criterion in contribution[1].criteria
                ),
            ),
        )
        candidates = [candidate for _, candidate in contributions]
        by_requirement: dict[str, list[CriterionAudit]] = {}
        for candidate in candidates:
            for criterion in candidate.criteria:
                by_requirement.setdefault(criterion.requirement_id, []).append(criterion)
        criteria: list[CriterionAudit] = []
        selected_statuses: dict[str, str] = {}
        for requirement_id, items in by_requirement.items():
            selected = max(items, key=lambda item: priority[item.status])
            selected_statuses[requirement_id] = selected.status
            proofs: list[SourceProof] = []
            seen_proofs: set[tuple[str, str]] = set()
            for item in items:
                if item.status != selected.status:
                    continue
                for proof in item.proofs:
                    key = (canonical_url(proof.url), " ".join(proof.excerpt.split()).casefold())
                    if key not in seen_proofs:
                        seen_proofs.add(key)
                        proofs.append(proof)
            criteria.append(CriterionAudit(
                requirement_id=requirement_id,
                requested_value=selected.requested_value,
                observed_value=selected.observed_value,
                status=selected.status,
                proofs=proofs,
                evidence_rejected=selected.evidence_rejected,
            ))
        retained_candidates = [
            candidate
            for candidate in candidates
            if any(
                criterion.status in {"proven", "incompatible"}
                and selected_statuses.get(criterion.requirement_id) == criterion.status
                for criterion in candidate.criteria
            )
            and not any(
                criterion.status in {"proven", "incompatible"}
                and selected_statuses.get(criterion.requirement_id) != criterion.status
                for criterion in candidate.criteria
            )
        ]
        merged_candidate = CandidateAudit(
            brand=first.brand,
            reference=first.reference,
            criteria=criteria,
            deviations=_unique_strings([
                value
                for candidate in retained_candidates
                for value in candidate.deviations
            ]),
            limitations=_unique_strings([
                value
                for candidate in retained_candidates
                for value in candidate.limitations
            ]),
        )
        merged.append(PageAudit(page_url=first_page_url, candidates=[merged_candidate]))
    return merged + invalid_audits


def _famille_observee(reference: str) -> str:
    """Prefixe stable d'une reference reellement observee.

    C'est un decoupage sur le premier separateur, pas une connaissance du
    schema de nommage d'un fabricant : `XZ07-20-10-13` donne `XZ07`, et une
    reference sans separateur se rend telle quelle. Aucune variante n'est
    fabriquee — la famille sert d'ancre de recherche, la variante recherchee
    sera decouverte par le moteur.
    """
    valeur = str(reference or "").strip()
    if not valeur:
        return ""
    tete = re.split(r"[-_/\s]", valeur, maxsplit=1)[0]
    return tete if len(tete) >= 3 else valeur


#: Une designation produit : des segments alphanumeriques ponctues, portant a
#: la fois lettres et chiffres. `b6-30-10-01` en est une, `contacteurs-et-relais`
#: n'en est pas une (aucun chiffre), `fr-be` non plus (trop court).
_DESIGNATION = re.compile(r"[a-z0-9]+(?:[-_][a-z0-9]+)+", re.IGNORECASE)

#: En deca, l'ancre designe une locale ou un rayon, pas une gamme : chercher
#: dessus ne ramene que du bruit.
MIN_ANCRE = 5


def _designations_lues(urls: Sequence[str]) -> tuple[str, ...]:
    """Designations produit lisibles telles quelles dans des URL."""
    trouvees: list[str] = []
    for url in urls:
        for brut in _DESIGNATION.findall(unquote(str(url or "")).casefold()):
            if not (any(c.isdigit() for c in brut) and any(c.isalpha() for c in brut)):
                continue
            if brut not in trouvees:
                trouvees.append(brut)
    return tuple(trouvees)


def famille_de_recherche(reference: str, urls: Sequence[str]) -> str:
    """Ancre de recherche : la part qu'un produit partage avec ses variantes.

    `_famille_observee` coupe au premier separateur. Une reference qui n'en a
    aucun — un code de commande comme `HPL1211001R0101` — se rend donc telle
    quelle, et la recherche ciblee ne peut plus trouver que le produit deja
    juge incompatible.

    Mesure du 2026-08-20 : `HPL1211001R0101` prouve a 6 A la ou il en fallait
    9, et la requete partie etait `HPL1211001R0101 Courant nominal "9 A"` —
    une chaine qui ne peut designer que ce produit-la.

    On lit alors une autre ecriture du meme produit dans les URL ou il a ete
    prouve : `new.norel.example/products/fr/HPL1211001R0101/b6-30-10-01` porte
    `b6-30-10-01`, dont la part commune avec ses variantes est `b6-30-10`.

    Rien n'est fabrique. La designation est lue telle quelle et l'ancre en est
    un prefixe ; quelles variantes existent reste au moteur de le dire.
    """
    valeur = str(reference or "").strip()
    famille = _famille_observee(valeur)
    if not valeur or famille != valeur:
        return famille

    canonique = _sans_separateur(valeur)
    for designation in _designations_lues(urls):
        # Une autre ponctuation du meme code n'apprend rien de plus.
        if _sans_separateur(designation) == canonique:
            continue
        tete = re.match(r"^(.*)[-_][a-z0-9]+$", designation)
        if tete and len(tete.group(1)) >= MIN_ANCRE:
            return tete.group(1)
    return valeur


def _sans_separateur(valeur: str) -> str:
    return "".join(c for c in str(valeur or "").casefold() if c.isalnum())


def _urls_de_preuve(evaluation) -> tuple[str, ...]:
    """Pages ou ce candidat a reellement ete constate, sans leur contenu."""
    urls: list[str] = []
    for critere in getattr(evaluation.candidate, "criteria", ()) or ():
        for preuve in getattr(critere, "proofs", ()) or ():
            url = getattr(preuve, "url", "") or ""
            if url and url not in urls:
                urls.append(url)
    return tuple(urls)


class AdaptiveResearch:
    def __init__(
        self,
        *,
        config: B2Config,
        planner,
        gateway,
        fetcher,
        analyze_pages: Callable[..., tuple[list[PageAnalysis], list[str]]] = examiner_pages,
        graph_config: dict,
        registry_factory: Callable[[], CandidateRegistry] = CandidateRegistry,
        document_builder: Callable[..., DiscoveryDocument] = build_discovery_document,
        discovery_limits: DiscoveryLimits = DiscoveryLimits(),
        identity_extractor: IdentityExtractor | None = None,
    ) -> None:
        self.identity_extractor = identity_extractor or IdentityExtractor()
        self._active_leads: tuple = ()
        self._fetch_failures: list[str] = []
        self.config = config
        self.planner = planner
        self.gateway = gateway
        self.fetcher = fetcher
        self.analyze_pages = analyze_pages
        self.graph_config = graph_config
        self.registry_factory = registry_factory
        self.document_builder = document_builder
        self.discovery_limits = discovery_limits

    def _fetch_page(self, url: str) -> PageContent:
        """Isole l'echec d'une page : `PageFetchError` ne doit pas tuer la vague.

        `executor.map` materialise ses resultats : une exception qui remonte
        emporte tout le lot, y compris les pages correctement recuperees. Le
        diagnostic complet des deux modes est conserve dans `warnings`, et la
        page devient un contenu vide que la porte rejettera proprement.
        """
        try:
            return self.fetcher.fetch(url)
        except PageFetchError as error:
            self._fetch_failures.append(
                f"Page non recuperee ({_safe_page_url(url)}) : "
                + " | ".join(attempt.describe() for attempt in error.attempts)
            )
            return PageContent(url=url, content="", mode="scrapegraph_url")

    def _fetch_pages(self, urls: list[str]) -> list[PageContent]:
        with ThreadPoolExecutor(max_workers=min(self.config.max_scraper_workers, 2)) as executor:
            return list(executor.map(self._fetch_page, urls))

    def _qualify_pages(
        self,
        pages: list[PageContent],
        documents_by_url: dict,
        diagnostics: ResearchDiagnostics,
        *,
        mode: str,
        target_brand: str | None,
        record_diagnostics: bool = True,
    ) -> list[PageContent]:
        """Ne laisse passer que les pages pertinentes pour ce mode.

        Une piste ne devient jamais un critere sans passer par
        `IdentityExtractor` : c'est `qualify_fetched_page` qui impose cet
        ordre, et il est le seul chemin vers la porte.
        """
        counters = GateCounters()
        retenues: list[PageContent] = []
        marque = tuple(value for value in (target_brand,) if value)

        for page in pages:
            # Le document de decouverte porte titre et extraits moteur ; a
            # defaut la page suffit, elle a son propre titre et son contenu.
            document = documents_by_url.get(page.url) or page
            decision, _ = qualify_fetched_page(
                page=document,
                analysis_mode=mode,
                identity_extractor=self.identity_extractor,
                structured_values=self._identity_candidates(page, document),
                brand_identifiers=marque,
            )
            if counters.record(decision):
                retenues.append(page)

        if record_diagnostics:
            diagnostics.pages_fetched += counters.pages_fetched
            diagnostics.pages_rejected_by_gate += counters.pages_rejected_by_gate
            diagnostics.pages_not_ready_for_evidence += counters.pages_not_ready_for_evidence
            diagnostics.pages_analyzed += counters.pages_analyzed
            diagnostics.gate_rejections.extend(counters.rejections)
        return retenues

    def _identity_candidates(self, page: PageContent, document) -> list[dict]:
        """Pistes soumises a validation : registre courant plus repli litteral.

        Ce sont des hypotheses, pas des identites : `IdentityExtractor` les
        confronte au corpus avant qu'aucune ne puisse qualifier une page.
        """
        propositions = [
            {"kind": "mpn", "value": lead.reference}
            for lead in self._active_leads
        ]
        propositions.extend(
            {"kind": value.kind, "value": value.raw_value}
            for value in deterministic_identity_leads(document)
        )
        return propositions

    def _near_miss_queries(
        self,
        evaluations: Sequence[CandidateEvaluation],
        requirements: RequirementSet,
        diagnostics: ResearchDiagnostics,
        seen_signatures: set[str],
    ) -> tuple[str, ...]:
        """Transforme les candidats rejetes en requetes pour la vague suivante.

        Deux constats ouvrent cette branche. Une incompatibilite prouvee dit
        quel attribut chercher autrement. Une preuve manquante sur un candidat
        deja bien etaye dit quel attribut reste a etablir. Les deux donnent une
        direction ; `assess_near_miss` tranche laquelle, et refuse les
        candidats trop peu documentes pour en valoir le budget.

        Le filtre reste borne aux candidats rejetes. Un candidat eligible est
        deja poursuivi par la boucle ciblee ordinaire, qui chasse ses criteres
        manquants : l'ouvrir ici dupliquerait ce travail et lui disputerait le
        budget. Seule l'exigence d'une incompatibilite disparait — mesure du
        2026-08-20 : elle privait de recherche ciblee `4KBL103001R8110`, rejete
        avec quatre criteres prouves et aucun bloqueur.

        Le budget vient du plafond global : la branche ne cree jamais de
        requete supplementaire, elle consomme celles qui restent.
        """
        rejetes = [item for item in evaluations if not item.eligible]
        if not rejetes:
            return ()

        diagnostics.near_miss_candidates_considered += len(rejetes)
        evalues = tuple(
            (
                item,
                {
                    "identity": item.candidate.reference,
                    # La famille est la partie stable d'une forme observee.
                    # Elle n'est jamais fabriquee : elle en est un prefixe. A
                    # defaut de separateur dans la reference, une autre
                    # ecriture du produit est lue dans les URL de ses preuves.
                    "family": famille_de_recherche(
                        item.candidate.reference, _urls_de_preuve(item)
                    ),
                },
            )
            for item in rejetes
        )

        # Les verdicts sont enregistres avant les taches : un candidat ecarte
        # doit dire quelle condition d'eligibilite a manque, sinon on remplace
        # une panne opaque par un rejet opaque.
        for verdict in assess_candidates(evalues, requirements):
            if verdict.eligible:
                continue
            diagnostic = verdict.as_diagnostic()
            if diagnostic not in diagnostics.near_miss_skipped:
                diagnostics.near_miss_skipped.append(diagnostic)

        taches = build_targeted_tasks(evalues, requirements)
        diagnostics.near_miss_tasks_generated += len(taches)
        if not taches:
            return ()

        restant = min(
            max(0, MAX_LOGICAL_QUERIES - diagnostics.logical_queries),
            max(0, MAX_NEAR_MISS_QUERIES - diagnostics.near_miss_queries_scheduled),
        )
        selection = select_targeted_queries(
            taches,
            remaining_logical_queries=restant,
            seen_signatures=seen_signatures,
        )

        # Seules les taches ecartees faute de place comptent ici. Un doublon de
        # signature ou une signature deja emise n'est pas un manque de budget :
        # les confondre ferait lire « budget insuffisant » la ou il faut lire
        # « recherche deja programmee ».
        ecartees = set(selection.deduplicated) | set(selection.already_seen)
        candidates = {
            item.signature for item in taches
            if item.signature not in ecartees
        }
        retenues = {item.signature for item in selection.selected}
        diagnostics.near_miss_queries_skipped_budget += len(candidates - retenues)

        diagnostics.near_miss_tasks_selected += len(selection.selected)
        diagnostics.near_miss_tasks_deduplicated += len(selection.deduplicated)
        diagnostics.near_miss_queries_scheduled += len(selection.queries)
        if selection.reserved_directional:
            diagnostics.reserved_directional_signature = selection.reserved_directional

        for tache in selection.selected:
            # Projection sure : ni extrait de page, ni prompt, ni reponse brute.
            diagnostics.near_miss_tasks.append({
                "signature": tache.signature,
                "eligibility_path": tache.eligibility_path,
                "blocking_criterion": tache.blocking_criterion,
                "target_value": tache.target_value,
                "reason": tache.reason,
            })
            # Une signature est vue des que sa requete est programmee : un
            # retry moteur ne recree pas la tache, et une vague ulterieure ne
            # la reemet pas.
            if tache.signature not in seen_signatures:
                seen_signatures.add(tache.signature)
                diagnostics.seen_near_miss_signatures.append(tache.signature)

        return selection.queries

    @staticmethod
    def _record_rejected_candidate(
        diagnostics: ResearchDiagnostics,
        rejected: RejectedCandidate,
    ) -> None:
        item = {
            "brand": rejected.brand,
            "reference": rejected.reference,
            "reason": rejected.reason,
        }
        identity = canonical_candidate_key(rejected.brand, rejected.reference)
        normalized = (
            identity[0],
            identity[1],
            " ".join(rejected.reason.split()).casefold(),
        )
        existing = {
            (
                *canonical_candidate_key(value["brand"], value["reference"]),
                " ".join(value["reason"].split()).casefold(),
            )
            for value in diagnostics.rejected_candidates
        }
        if normalized not in existing:
            diagnostics.rejected_candidates.append(item)

    @staticmethod
    def _refresh_candidate_leads(
        diagnostics: ResearchDiagnostics,
        registry: CandidateRegistry,
    ) -> None:
        diagnostics.candidate_leads = [
            {
                "brand": lead.brand,
                "reference": lead.reference,
                "rank": lead.rank,
                "source_count": len({item.url for item in lead.occurrences}),
                "fields": sorted({item.field for item in lead.occurrences}),
            }
            for lead in registry.active()
        ]

    def run(
        self,
        fiche: str,
        target_brand: str | None,
        *,
        source_path: str | None = None,
    ) -> ResearchOutcome:
        outcome = self._run(fiche, target_brand, source_path=source_path)
        capture_replay_if_configured(
            outcome=outcome,
            config=self.config,
            target_brand=target_brand,
            strict_evidence=False,
            source_path=source_path,
            source_text=fiche,
        )
        return outcome

    def _run(
        self,
        fiche: str,
        target_brand: str | None,
        *,
        source_path: str | None = None,
    ) -> ResearchOutcome:
        extract = self.planner.extract_requirements
        try:
            parameters = inspect.signature(extract).parameters.values()
            supports_source_path = any(
                item.name == "source_path"
                or item.kind is inspect.Parameter.VAR_KEYWORD
                for item in parameters
            )
        except (TypeError, ValueError):
            supports_source_path = False
        requirements = (
            extract(fiche, source_path=source_path)
            if supports_source_path
            else extract(fiche)
        )
        diagnostics = ResearchDiagnostics()
        diagnostics.requirement_validation = sanitize_requirement_diagnostics(
            getattr(self.planner, "last_requirement_diagnostics", None)
        )
        requirement_coverage = getattr(self.planner, "last_coverage", None)
        if requirement_coverage is not None:
            diagnostics.requirement_specs_detected = (
                requirement_coverage.detected_specs
            )
            diagnostics.requirement_specs_covered = (
                requirement_coverage.covered_specs
            )
            diagnostics.requirement_orphans_detected = len(
                requirement_coverage.orphans
            )
            diagnostics.requirement_orphan_pages = sorted({
                item.page
                for item in requirement_coverage.orphans
                if item.page is not None
            })
            diagnostics.requirement_second_pass = bool(
                getattr(self.planner, "last_second_pass", False)
            )
        registry = self.registry_factory()
        previous_queries: set[str] = set()
        seen_urls: set[str] = set()
        processed_final_urls: set[str] = set()
        audits: list[PageAudit] = []
        visited_pages: dict[str, str] = {}
        audit_waves: list[int] = []
        page_waves: dict[str, int] = {}
        replay_waves: list[dict] = []
        best: CandidateEvaluation | None = None
        evidence_rejection_seen = False
        evaluated_candidate_count = 0
        missing = [item.label for item in requirements.criteria]
        # Les signatures vivent au niveau de la mission : une vague ulterieure
        # ne reemet pas un near-miss deja programme.
        seen_near_miss: set[str] = set()
        pending_near_miss_queries: tuple[str, ...] = ()
        targeted_query_counts: dict[tuple[str, str], int] = {}
        cached_target_audits: set[tuple[str, tuple[str, str]]] = set()
        audited_candidate_keys: list[tuple[str, str]] = []
        proven_before_wave: dict[tuple[str, str], frozenset[str]] = {}
        force_discovery_next_wave = False

        for wave in range(1, self.config.adaptive_max_waves + 1):
            diagnostics.waves = wave
            replay_wave = {
                "wave": wave,
                "logical_queries_at_entry": diagnostics.logical_queries,
                "seen_signatures_at_entry": sorted(seen_near_miss),
            }
            replay_waves.append(replay_wave)
            leads = registry.active()
            targetable_leads = _prioritize_targeted_leads(
                leads, targeted_query_counts,
            )
            distributor_planner = getattr(
                self.planner, "plan_distributor_queries", None
            )
            distributor_leads = (
                _prioritize_distributor_leads(
                    leads,
                    requirements,
                    previous_queries,
                    self.config.distributor_domains,
                )
                if callable(distributor_planner)
                else []
            )
            mode: Literal["discovery", "targeted"] = (
                "targeted"
                if (targetable_leads or distributor_leads)
                and not force_discovery_next_wave
                else "discovery"
            )
            force_discovery_next_wave = False
            query_candidate_keys: dict[str, tuple[tuple[str, str], ...]] = {}
            lead_by_key = {lead.key: lead for lead in leads}
            distributor_query_texts: set[str] = set()
            if mode == "targeted":
                plan = None
                normal_items: tuple = ()
                if targetable_leads:
                    try:
                        plan = self.planner.plan_targeted_queries(
                            requirements,
                            targetable_leads[:MAX_TARGETED_LEADS_PER_WAVE],
                            previous_queries,
                        )
                        normal_items = tuple(plan.queries)
                    except PlanningError:
                        pass
                distributor_items = tuple(
                    distributor_planner(
                        requirements,
                        distributor_leads[:MAX_TARGETED_LEADS_PER_WAVE],
                        previous_queries,
                    )
                ) if distributor_leads and callable(distributor_planner) else ()
                distributor_query_texts = {
                    item.query for item in distributor_items
                }
                if normal_items or distributor_items:
                    query_items, query_candidate_keys = _group_targeted_queries(
                        _interleave_targeted_queries(
                            normal_items, distributor_items
                        )
                    )
                else:
                    warning = (
                        "Plan ciblé indisponible : retour à la découverte générale."
                    )
                    if warning not in diagnostics.warnings:
                        diagnostics.warnings.append(warning)
                    mode = "discovery"
                    plan = self.planner.plan_queries(
                        requirements, target_brand, missing, previous_queries
                    )
                    query_items = tuple(plan.queries)
            else:
                plan = self.planner.plan_queries(
                    requirements, target_brand, missing, previous_queries
                )
                query_items = tuple(plan.queries)

            # Les requetes ciblees issues d'un near-miss precedent passent en
            # tete : elles portent une direction etablie, la ou le plan general
            # ne fait que reformuler les criteres manquants.
            planned_query_set = set(query_items)
            # Si le plan general a formule exactement le meme texte, son
            # envoi satisfait aussi le pending mais reste bien diagnostique
            # comme PLANNED. Le near-miss ne doit pas le dupliquer/reclassifier.
            near_miss_en_tete = tuple(
                query for query in pending_near_miss_queries
                if query not in planned_query_set
            )
            if near_miss_en_tete:
                if mode == "discovery" and query_items:
                    # Le mode annonce une vraie reprise de decouverte : une
                    # requete generale part donc avant les near-miss en attente.
                    query_items = (
                        query_items[0],
                        *near_miss_en_tete,
                        *query_items[1:],
                    )
                else:
                    query_items = (*near_miss_en_tete, *query_items)
            query_items = tuple(dict.fromkeys(query_items))

            diagnostics.wave_modes.append({"wave": wave, "mode": mode})
            strategy = getattr(plan, "strategy", "model")
            plan_warning = {
                "retry": "Plan de requêtes : relance corrective.",
                "fallback": "Plan de requêtes : secours déterministe.",
            }.get(strategy)
            plan_failures = tuple(getattr(plan, "failures", ()))
            if plan_warning and plan_failures:
                plan_warning += " Motifs : " + " ; ".join(plan_failures)
            if plan_warning and plan_warning not in diagnostics.warnings:
                diagnostics.warnings.append(plan_warning)
            contexts_by_query: list[list[HitContext]] = []
            logical_avant = diagnostics.logical_queries
            envoyees_cette_vague = 0
            sent_task_types: set[str] = set()
            sent_near_miss_queries: list[str] = []
            sent_candidate_keys: list[tuple[str, str]] = []
            for query in query_items:
                is_near_miss = query in near_miss_en_tete
                is_distributor_query = query in distributor_query_texts
                planned_candidate_keys = query_candidate_keys.get(query, ())
                candidate_keys = (
                    planned_candidate_keys
                    if is_distributor_query
                    else _eligible_targeted_candidate_keys(
                        planned_candidate_keys, targeted_query_counts,
                    )
                )
                # Plafond absolu : aucune branche ne peut le franchir.
                if diagnostics.logical_queries >= MAX_LOGICAL_QUERIES:
                    break
                # Plafond par vague : sans lui, une vague prolixe consommerait
                # tout le budget et ne laisserait aucun tour a la
                # replanification.
                if envoyees_cette_vague >= MAX_QUERIES_PER_WAVE:
                    break
                if (
                    planned_candidate_keys
                    and not is_near_miss
                    and not is_distributor_query
                    and not candidate_keys
                ):
                    continue
                envoyees_cette_vague += 1
                previous_queries.add(query)
                diagnostics.queries.append(query)
                # Une requete logique vaut une unite, quel que soit le nombre
                # d'essais moteur qu'elle declenchera.
                diagnostics.logical_queries += 1
                if is_near_miss:
                    sent_task_types.add("NEAR_MISS")
                    sent_near_miss_queries.append(query)
                else:
                    sent_task_types.add("PLANNED")
                    for key in candidate_keys:
                        if key not in sent_candidate_keys:
                            sent_candidate_keys.append(key)
                        if not is_distributor_query:
                            targeted_query_counts[key] = (
                                targeted_query_counts.get(key, 0) + 1
                            )
                        lead = lead_by_key.get(key)
                        if lead is not None:
                            diagnostics.targeted_queries.append({
                                "wave": wave,
                                "query": query,
                                "brand": lead.brand,
                                "reference": lead.reference,
                            })
                try:
                    batch = self.gateway.search(query)
                except SearxUnavailable as error:
                    batch = error.batch
                    diagnostics.warnings.append(str(error))
                diagnostics.attempts.extend(batch.attempts)
                diagnostics.engine_calls += len(batch.attempts)
                diagnostics.hits.extend(batch.hits)
                contexts_by_query.append([
                    HitContext(
                        wave=wave,
                        query=query,
                        candidate_keys=candidate_keys,
                        hit=hit,
                    )
                    for hit in batch.hits
                ])

            # Les titres, URL et extraits du moteur peuvent déjà publier une
            # identité marque + référence. On les utilise immédiatement pour
            # préparer la vague ciblée suivante, y compris lorsque la limite
            # de pages empêche d'ouvrir ce résultat de découverte. Ces pistes
            # ne prouvent aucun critère : la page ciblée devra encore être
            # téléchargée, passer la porte de contenu puis être auditée.
            if mode == "discovery":
                # Le modèle lit le corpus borné de titres et d'extraits pour
                # distinguer une vraie
                # marque des adjectifs voisins, puis le registre vérifie à
                # nouveau chaque identité contre le résultat indiqué.
                # L'heuristique ne s'exécute qu'en repli : la mélanger à une
                # réponse modèle valide remettrait devant elle des marques
                # parasites comme un matériau ou un titre éditorial.
                discovery_planner = getattr(
                    self.planner, "discover_search_candidates", None
                )
                unique_hits: list[SearchHit] = []
                unique_hit_urls: set[str] = set()
                for contexts in contexts_by_query:
                    for context in contexts:
                        url_key = context.hit.url.strip().casefold()
                        if not url_key or url_key in unique_hit_urls:
                            continue
                        unique_hit_urls.add(url_key)
                        unique_hits.append(context.hit)
                hint_accepted = False
                if callable(discovery_planner) and unique_hits:
                    hints = discovery_planner(
                        requirements, unique_hits, target_brand
                    )
                    for failure in tuple(getattr(
                        self.planner, "last_search_discovery_failures", ()
                    )):
                        warning = f"Découverte de candidats : {failure}."
                        if warning not in diagnostics.warnings:
                            diagnostics.warnings.append(warning)
                    for hint in hints:
                        if hint.result_index >= len(unique_hits):
                            continue
                        hit = unique_hits[hint.result_index]
                        metadata_document = build_discovery_document(
                            url=hit.url,
                            title=hit.title,
                            snippets=[hit.snippet] if hit.snippet else [],
                            content="",
                            rank=hit.rank,
                            limits=self.discovery_limits,
                        )
                        metadata_result = registry.ingest(
                            [CandidateProposal(
                                brand=hint.brand,
                                reference=hint.reference,
                            )],
                            metadata_document,
                            requirements,
                            target_brand,
                            allow_deterministic_fallback=False,
                        )
                        hint_accepted = (
                            hint_accepted or bool(metadata_result.accepted)
                        )
                        for rejected in metadata_result.rejected:
                            self._record_rejected_candidate(
                                diagnostics, rejected
                            )
                if not hint_accepted:
                    for contexts in contexts_by_query:
                        for context in contexts:
                            hit = context.hit
                            metadata_document = build_discovery_document(
                                url=hit.url,
                                title=hit.title,
                                snippets=[hit.snippet] if hit.snippet else [],
                                content="",
                                rank=hit.rank,
                                limits=self.discovery_limits,
                            )
                            metadata_result = registry.ingest(
                                (),
                                metadata_document,
                                requirements,
                                target_brand,
                                allow_deterministic_fallback=True,
                            )
                            for rejected in metadata_result.rejected:
                                self._record_rejected_candidate(
                                    diagnostics, rejected
                                )

            deferred_near_miss_queries = tuple(
                query for query in near_miss_en_tete
                if query not in sent_near_miss_queries
            )

            # Projection sure : compteurs et types de taches seulement, jamais
            # une requete brute, un extrait de page ni une reponse de modele.
            diagnostics.wave_details.append({
                "wave_index": wave,
                "mode": mode,
                "query_count": envoyees_cette_vague,
                "logical_queries_before": logical_avant,
                "logical_queries_after": diagnostics.logical_queries,
                "task_types": sorted(sent_task_types),
            })

            # Le plafond par vague ne suffit plus a borner la mission : quatre
            # vagues de douze pages depasseraient les trente-six autorisees.
            # C'est donc le reste du budget global qui limite la selection.
            # `select_hit_contexts` traite `limit=0` comme une absence de
            # limite : un budget epuise doit donc court-circuiter l'appel, pas
            # lui passer zero.
            places_pages = min(
                self.config.adaptive_pages_per_wave,
                max(0, MAX_PAGES_OPENED - diagnostics.pages_opened),
            )
            diagnostics.offtopic_urls_skipped.extend(dict.fromkeys(
                context.hit.url
                for contexts in contexts_by_query
                for context in contexts
                if hors_sujet(context.hit.url)
            ))
            selected_contexts = select_hit_contexts(
                contexts_by_query,
                seen_urls=seen_urls,
                limit=places_pages,
            ) if places_pages else []
            urls = [item.url for item in selected_contexts]

            diagnostics.opened_pages.extend(urls)
            diagnostics.pages_opened += len(urls)
            self._fetch_failures = []
            pages = self._fetch_pages(urls) if urls else []
            diagnostics.warnings.extend(self._fetch_failures)
            selected_contexts, pages = _merge_fetched_contexts(
                selected_contexts,
                pages,
                seen_urls=seen_urls,
                processed_final_urls=processed_final_urls,
            )
            planned_leads_by_key = {lead.key: lead for lead in registry.active()}
            cached_page_canonicals: set[str] = set()
            cache_candidate_keys = tuple(dict.fromkeys((
                *audited_candidate_keys,
                *sent_candidate_keys,
            )))
            for url, content, distinct_keys in _cached_product_pages(
                planned_leads_by_key,
                cache_candidate_keys,
                visited_pages,
                already_audited=cached_target_audits,
            ):
                canonical = canonical_url(url)
                representative = planned_leads_by_key[distinct_keys[0]]
                hit = SearchHit(
                    url=url,
                    title=f"{representative.brand} {representative.reference}",
                    snippet="",
                    engine="cache",
                    rank=1,
                )
                selected_contexts.append(SelectedHitContext(
                    url=url,
                    hits=(hit,),
                    queries=("cached-product-evidence",),
                    candidate_keys=distinct_keys,
                ))
                pages.append(PageContent(
                    url=url,
                    content=content,
                    title=hit.title,
                    mode="cache",
                ))
                cached_page_canonicals.add(canonical)

            documents_by_url: dict[str, DiscoveryDocument] = {}
            authorized_by_url: dict[str, tuple] = {}
            selected_by_url: dict[str, SelectedHitContext] = {}
            self._active_leads = tuple(planned_leads_by_key.values())
            for selected, page in zip(selected_contexts, pages):
                selected_by_url[canonical_url(page.url)] = selected
                document = self.document_builder(
                    url=page.url,
                    title=page.title or next(
                        (hit.title for hit in selected.hits if hit.title), ""
                    ),
                    snippets=[hit.snippet for hit in selected.hits if hit.snippet],
                    content=page.content,
                    rank=min(hit.rank for hit in selected.hits),
                    limits=self.discovery_limits,
                )
                documents_by_url[page.url] = document
                if selected.candidate_keys:
                    authorized_by_url[page.url] = tuple(
                        planned_leads_by_key[key]
                        for key in selected.candidate_keys
                        if key in planned_leads_by_key
                    )
                if page.content:
                    visited_pages[page.url] = page.content
                    page_waves.setdefault(canonical_url(page.url), wave)

            # Le repli deterministe ne depend pas du resultat du graphe : une
            # panne ScrapeGraphAI ne doit pas faire perdre une identite
            # litterale deja visible dans le document de decouverte.
            if mode == "discovery":
                for document in documents_by_url.values():
                    fallback_result = registry.ingest(
                        (),
                        document,
                        requirements,
                        target_brand,
                        allow_deterministic_fallback=True,
                    )
                    for rejected in fallback_result.rejected:
                        self._record_rejected_candidate(diagnostics, rejected)
                active_after_ingest = registry.active()
                for page in pages:
                    canonical_page = canonical_url(page.url)
                    page_leads = tuple(
                        lead for lead in active_after_ingest
                        if any(
                            canonical_url(occurrence.url) == canonical_page
                            for occurrence in lead.occurrences
                        )
                    )
                    if page_leads:
                        authorized_by_url[page.url] = page_leads

            # La porte decide page par page, `analyze_pages` traite un lot : on
            # filtre donc avant l'appel. Une vague peut ainsi n'avoir aucune
            # page a analyser tout en ayant consomme son budget de
            # recuperation — les compteurs le disent explicitement, sinon cette
            # vague ressemblerait a une vague vide.
            network_pages = [
                page for page in pages
                if canonical_url(page.url) not in cached_page_canonicals
            ]
            cached_pages = [
                page for page in pages
                if canonical_url(page.url) in cached_page_canonicals
            ]
            qualified_network = self._qualify_pages(
                network_pages, documents_by_url, diagnostics, mode=mode,
                target_brand=target_brand,
            )
            qualified_cached = self._qualify_pages(
                cached_pages,
                documents_by_url,
                diagnostics,
                mode=mode,
                target_brand=target_brand,
                record_diagnostics=False,
            )
            qualified_urls = {
                canonical_url(page.url)
                for page in (*qualified_network, *qualified_cached)
            }
            pages = [
                page for page in pages
                if canonical_url(page.url) in qualified_urls
            ]

            analyses, warnings = self.analyze_pages(
                pages,
                requirements,
                target_brand,
                self.graph_config,
                workers=self.config.max_analysis_workers,
                mode=mode,
                documents_by_url=documents_by_url,
                authorized_by_url=authorized_by_url,
            )
            diagnostics.warnings.extend(warnings)
            diagnostics.llm_rate_limit_count += sum(
                avertissement_est_limitation_llm(warning)
                for warning in warnings
            )
            analyses_by_url: dict[str, PageAnalysis] = {}
            for analysis in analyses:
                diagnostics.page_audit_validation.extend(
                    sanitize_page_audit_diagnostics(
                        analysis.validation_diagnostics
                    )
                )
                canonical = canonical_url(analysis.page_url)
                selected = selected_by_url.get(canonical)
                document = next(
                    (
                        item for url, item in documents_by_url.items()
                        if canonical_url(url) == canonical
                    ),
                    None,
                )
                page = next(
                    (item for item in pages if canonical_url(item.url) == canonical),
                    None,
                )
                if selected is None or document is None or page is None:
                    continue
                authorized = next(
                    (
                        item for url, item in authorized_by_url.items()
                        if canonical_url(url) == canonical
                    ),
                    (),
                )
                filtered_audit = filter_page_audit(
                    analysis.audit,
                    page_url=analysis.page_url,
                    title=page.title,
                    content=analysis.content,
                    document=document,
                    authorized_candidates=authorized,
                    targeted=mode == "targeted",
                )
                audits.append(filtered_audit)
                audit_waves.append(wave)
                for candidate in filtered_audit.candidates:
                    candidate_key = canonical_candidate_key(
                        candidate.brand, candidate.reference,
                    )
                    if candidate_key not in audited_candidate_keys:
                        audited_candidate_keys.append(candidate_key)
                visited_pages[analysis.page_url] = analysis.content
                page_waves.setdefault(canonical_url(analysis.page_url), wave)
                analyses_by_url[canonical] = PageAnalysis(
                    page_url=analysis.page_url,
                    content=analysis.content,
                    audit=filtered_audit,
                    proposals=analysis.proposals,
                    proposal_rejections=analysis.proposal_rejections,
                    mode=analysis.mode,
                    validation_diagnostics=analysis.validation_diagnostics,
                )
                for rejected in analysis.proposal_rejections:
                    self._record_rejected_candidate(diagnostics, rejected)

                if mode == "discovery":
                    proposals = (
                        *analysis.proposals,
                        *audit_proposals(filtered_audit),
                    )
                    ingest_result = registry.ingest(
                        proposals,
                        document,
                        requirements,
                        target_brand,
                        allow_deterministic_fallback=False,
                    )
                else:
                    ingest_result = registry.ingest(
                        (),
                        document,
                        requirements,
                        target_brand,
                        allow_deterministic_fallback=True,
                    )
                for rejected in ingest_result.rejected:
                    self._record_rejected_candidate(diagnostics, rejected)

            # `pages` a ete reduit aux seules pages admises par la porte, pas
            # `selected_contexts` : les apparier par position decalerait chaque
            # etat d'un cran des la premiere page rejetee. L'index canonique,
            # lui, reste juste quel que soit le nombre de pages retirees.
            for page in pages:
                canonical = canonical_url(page.url)
                selected = selected_by_url[canonical]
                document = documents_by_url[page.url]
                analysis = analyses_by_url.get(canonical)
                audit_count = len(analysis.audit.candidates) if analysis else 0
                proposal_count = len(analysis.proposals) if analysis else 0
                diagnostics.page_states.append({
                    "wave": wave,
                    "mode": mode,
                    "url": selected.url,
                    "selected": True,
                    "fetched": bool(page.content),
                    "analysis_status": (
                        "failed" if analysis is None else
                        "audited" if audit_count else
                        "discovered" if proposal_count else
                        "no_candidate"
                    ),
                    "audit_candidate_count": audit_count,
                    "truncated_fields": list(document.truncated_fields),
                })

            self._refresh_candidate_leads(diagnostics, registry)

            replay_wave["logical_queries_before_near_miss"] = (
                diagnostics.logical_queries
            )
            replay_wave["seen_signatures_before_near_miss"] = sorted(
                seen_near_miss
            )

            merged = merge_page_audits(audits, requirements, visited_pages)
            evaluations: list[CandidateEvaluation] = []
            for merged_audit in merged:
                evaluated_candidate_count += len(merged_audit.candidates)
                try:
                    # Le contrat de preuve reste strict : une citation
                    # reconstruite par le modele est toujours refusee. Ce qui
                    # change ici est la portee de ce refus — il retrograde le
                    # critere en `not_proven` au lieu de supprimer le candidat.
                    #
                    # Mesure sur XZ07-20-10-13 : un extrait agrege « * 3P *
                    # 1 contact auxiliaire N/O * ... » introuvable tel quel dans
                    # la page faisait perdre le candidat entier, alors que sa
                    # bobine 100-250 V AC/DC etait prouvee incompatible avec les
                    # 24 V DC demandes, sur trois sources concordantes. Une
                    # incompatibilite prouvee est un fait exploitable : elle
                    # oriente vers la variante 24 V. La taire rendait
                    # « rien de prouvable » un resultat faux.
                    new_evaluations = evaluate_candidates(
                        requirements,
                        [merged_audit],
                        target_brand,
                        self.config.min_compatibility_percent,
                        visited_pages,
                        strict_evidence=False,
                    )
                    downgrade_pass = reaudit_cached_product_evidence(
                        requirements,
                        merged_audit,
                        visited_pages,
                    )
                    downgraded = cached_reaudit_downgraded_criteria(
                        requirements, merged_audit, downgrade_pass,
                    )
                    # La première passe peut uniquement rendre une ancienne
                    # incompatibilité secondaire ré-auditable. La seconde suit
                    # alors le chemin ordinaire `not_proven -> proven`; cette
                    # séparation empêche toute promotion directe de source.
                    enriched_audit = reaudit_cached_product_evidence(
                        requirements,
                        downgrade_pass,
                        visited_pages,
                    )
                    if enriched_audit != merged_audit:
                        new_evaluations = evaluate_candidates(
                            requirements,
                            [enriched_audit],
                            target_brand,
                            self.config.min_compatibility_percent,
                            visited_pages,
                            strict_evidence=False,
                        )
                    evaluations.extend(new_evaluations)
                    for evaluation in new_evaluations:
                        journaliser_evaluation(
                            diagnostics.candidate_evaluations,
                            candidate_evaluation_diagnostic(
                                evaluation,
                                wave=wave,
                                url=merged_audit.page_url,
                                downgraded_by_official_source=downgraded,
                            ),
                        )
                except (CompatibilityContractError, EvidenceContractError) as error:
                    evidence_rejection_seen = True
                    candidate = merged_audit.candidates[0]
                    journaliser_evaluation(
                        diagnostics.candidate_evaluations,
                        candidate_contract_error_diagnostic(
                            merged_audit,
                            wave=wave,
                            error=error,
                        ),
                    )
                    warning = (
                        f"Candidat rejeté ({candidate.brand} {candidate.reference}) : "
                        f"contrat de preuve invalide ({type(error).__name__})."
                    )
                    if warning not in diagnostics.warnings:
                        diagnostics.warnings.append(warning)
            current = select_best_candidate(evaluations)
            proven_after_wave = _proven_criteria_by_candidate(evaluations)
            if mode == "targeted":
                progressed = _targeted_wave_progressed(
                    proven_before_wave, proven_after_wave,
                )
                force_discovery_next_wave = not progressed
            proven_before_wave = proven_after_wave
            # Les audits sont refusionnes depuis tout l'historique a chaque
            # vague : leur evaluation courante est donc l'autorite, y compris
            # lorsqu'une nouvelle incompatibilite invalide l'ancien meilleur.
            best = current
            # Un candidat complet ne coupe plus la mission : les vagues et le
            # budget restants servent a collecter les autres propositions
            # admissibles. `evaluation` conserve le meilleur pour compatibilite
            # ascendante, tandis que `evaluations` alimente la liste complete.
            # Un candidat rejete sur une incompatibilite prouvee indique quoi
            # chercher ensuite. La branche est evaluee ici, apres l'audit et
            # avant l'arret, pour que la vague suivante puisse la consommer.
            new_near_miss_queries = self._near_miss_queries(
                evaluations, requirements, diagnostics, seen_near_miss
            )
            pending_near_miss_queries = tuple(dict.fromkeys((
                *deferred_near_miss_queries,
                *new_near_miss_queries,
            )))
            if (
                wave == self.config.adaptive_max_waves
                and pending_near_miss_queries
            ):
                # Le budget logique peut encore contenir les trois places
                # reservees, mais il n'existe plus de vague pour les envoyer.
                # Le diagnostic doit compter cette impossibilite comme un
                # budget d'orchestration epuise, pas comme une requete lancee.
                diagnostics.near_miss_queries_skipped_budget += len(
                    pending_near_miss_queries
                )
                pending_near_miss_queries = ()

            if current is not None:
                missing = (
                    current.summary.not_proven_criteria
                    + current.summary.incompatible_criteria
                )
            else:
                missing = [item.label for item in requirements.criteria]

        # Pourquoi la boucle s'arrete, distinct de ce qu'elle a trouve. Les
        # deux repondent a des questions differentes : l'un dit ou la mission
        # a bute, l'autre ce qu'elle a etabli.
        if not diagnostics.stop_reason:
            if best is not None and best.complete:
                # Les vagues restantes servent desormais a collecter les autres
                # propositions admissibles, donc la boucle va jusqu'au bout meme
                # apres 100 %. Ce qui a conclu la mission reste l'equivalent
                # prouve, et non le budget consomme en chemin : `rejeu.py` rend
                # deja `PROVEN_EQUIVALENT` sur un corpus `complete`, et les deux
                # chemins doivent nommer la meme cause.
                diagnostics.stop_reason = "PROVEN_EQUIVALENT"
            elif diagnostics.logical_queries >= MAX_LOGICAL_QUERIES:
                diagnostics.stop_reason = "QUERY_BUDGET_EXHAUSTED"
            elif diagnostics.pages_analyzed >= MAX_PAGES_ANALYZED:
                diagnostics.stop_reason = "PAGE_BUDGET_EXHAUSTED"
            elif diagnostics.pages_rejected_by_gate and not diagnostics.pages_analyzed:
                diagnostics.stop_reason = "PRODUCT_CONTENT_NOT_RETRIEVED"
            elif evaluated_candidate_count:
                diagnostics.stop_reason = "NO_PROVABLE_CANDIDATE"
            else:
                diagnostics.stop_reason = "MAX_WAVES_REACHED"

        incompatibility_seen = any(
            evaluation.summary.incompatible_criteria
            for evaluation in evaluations
        )
        status = (
            "complete" if best is not None and best.complete else
            "partial" if best is not None else
            "rejected" if incompatibility_seen else
            "service_unavailable" if diagnostics.llm_rate_limit_count else
            "not_resolved"
        )
        if best is not None:
            diagnostics.final_state = "candidate_selected"
        elif incompatibility_seen:
            diagnostics.final_state = "candidate_rejected"
        elif diagnostics.llm_rate_limit_count:
            diagnostics.final_state = "provider_unavailable"
            diagnostics.stop_reason = "LLM_RATE_LIMITED"
        elif evidence_rejection_seen:
            diagnostics.final_state = "rejected_by_evidence"
        elif evaluated_candidate_count:
            diagnostics.final_state = "audited_but_non_verifiable"
        elif registry.active():
            diagnostics.final_state = "candidates_discovered_but_not_audited"
        else:
            diagnostics.final_state = "no_candidate_discovered"
        return ResearchOutcome(
            status,
            requirements,
            best,
            audits,
            visited_pages,
            diagnostics,
            audit_waves,
            page_waves,
            replay_waves,
            evaluations,
        )
