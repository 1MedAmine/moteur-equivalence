# -*- coding: utf-8 -*-
"""Mise en forme et ecriture des sorties, JSON et Markdown.

La convention est celle de la specification d'origine, elle-meme reprise de
`assistant_industriel_multimode_v1_0_0` : un JSON exploitable par un appelant,
un Markdown lisible par un humain, tous deux portant les memes faits.
"""

from __future__ import annotations

from collections.abc import Mapping
import json
from pathlib import Path
import re
from typing import List, Optional, Tuple

from mission import Alternative
from recherche_adaptative import ResearchOutcome

STATUT_COMPLET = "complete"
STATUT_AVEC_ADAPTATION = "avec_adaptation"
STATUT_SANS_REPONSE = "not_resolved"
STATUT_ENTREE_INVALIDE = "invalid_input"
STATUT_PARTIEL = "partial"
STATUT_REJETE = "rejected"
STATUT_CONFIGURATION = "configuration_error"
STATUT_ANALYSE = "analysis_error"
STATUT_SERVICE_INDISPONIBLE = "service_unavailable"

_DISCOVERY_FIELDS = frozenset({"title", "snippet", "url", "content"})
_TRUNCATED_FIELDS = frozenset({"title", "snippet", "url", "content", "total"})
_WAVE_MODES = frozenset({"discovery", "targeted"})
_PAGE_ANALYSIS_STATES = frozenset({
    "audited", "discovered", "failed", "no_candidate",
})
_REJECTION_REASONS = frozenset({
    "brand_not_literal",
    "isolated_requirement_value",
    "malformed_proposal",
    "origin_identity",
    "reference_not_literal",
    "target_brand_mismatch",
})
_FINAL_STATES = frozenset({
    "audited_but_non_verifiable",
    "candidate_selected",
    "candidate_rejected",
    "candidates_discovered_but_not_audited",
    "no_candidate_discovered",
    "provider_unavailable",
    "rejected_by_evidence",
})


def _safe_int(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _safe_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _safe_names(value: object, allowed: frozenset[str]) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item in allowed]


def _safe_diagnostic_items(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


_REQUIREMENT_STAGES = frozenset({
    "requirement_initial_extraction",
    "requirement_text_supplement",
    "requirement_visual_supplement",
})
_REQUIREMENT_ISSUES = frozenset({
    "invalid_item",
    "invalid_structure",
    "json_parse_error",
    "unit_label_disagreement",
})
_REQUIREMENT_ACTIONS = frozenset({
    "aborted",
    "discarded",
    "retried",
    "retry_failed",
    "review",
})
_REQUIREMENT_PATH = re.compile(
    r"(?:\$|product|origin_brand|criteria(?:\[\d+\])?(?:\.[a-z_]+)?)"
)


def _projection_requirement_validation(value: object) -> list[dict[str, str]]:
    """Allowlist stricte : aucune réponse modèle ne peut atteindre le rapport."""
    projected: list[dict[str, str]] = []
    for item in _safe_diagnostic_items(value):
        stage = item.get("stage")
        path = item.get("path")
        issue = item.get("issue")
        action = item.get("action")
        if (
            stage in _REQUIREMENT_STAGES
            and isinstance(path, str)
            and _REQUIREMENT_PATH.fullmatch(path)
            and issue in _REQUIREMENT_ISSUES
            and action in _REQUIREMENT_ACTIONS
        ):
            projected.append({
                "stage": stage,
                "path": path,
                "issue": issue,
                "action": action,
            })
    return projected


_PAGE_AUDIT_PATH = re.compile(
    r"(?:\$|candidates(?:\[\d+\])?"
    r"(?:\.(?:brand|reference|criteria|deviations|limitations|"
    r"requirement_id|requested_value|observed_value|status|proofs|"
    r"url|excerpt|type)(?:\[\d+\])?)*)"
)


def _projection_page_audit_validation(value: object) -> list[dict[str, str]]:
    """Projette uniquement le diagnostic structurel, jamais la sortie modele."""
    projected: list[dict[str, str]] = []
    for item in _safe_diagnostic_items(value):
        path = item.get("path")
        issue = item.get("issue")
        action = item.get("action")
        if (
            item.get("stage") == "page_audit"
            and isinstance(path, str)
            and _PAGE_AUDIT_PATH.fullmatch(path)
            and issue in {"invalid_item", "invalid_structure", "json_parse_error"}
            and action in {"aborted", "discarded", "retried", "retry_failed"}
        ):
            projected.append({
                "stage": "page_audit",
                "path": path,
                "issue": str(issue),
                "action": str(action),
            })
    return projected


def _safe_candidate_diagnostics(
    diagnostics,
    allowed_criteria: frozenset[str],
) -> dict[str, list[dict]]:
    wave_modes: list[dict] = []
    for item in _safe_diagnostic_items(diagnostics.wave_modes):
        wave = _safe_int(item.get("wave"))
        mode = item.get("mode")
        if wave is not None and mode in _WAVE_MODES:
            wave_modes.append({"wave": wave, "mode": mode})

    candidate_leads: list[dict] = []
    for item in _safe_diagnostic_items(diagnostics.candidate_leads):
        brand = _safe_text(item.get("brand"))
        reference = _safe_text(item.get("reference"))
        source_count = _safe_int(item.get("source_count"))
        if brand is not None and reference is not None and source_count is not None:
            candidate_leads.append({
                "brand": brand,
                "reference": reference,
                "source_count": source_count,
                "fields": _safe_names(item.get("fields"), _DISCOVERY_FIELDS),
            })

    rejected_candidates: list[dict] = []
    for item in _safe_diagnostic_items(diagnostics.rejected_candidates):
        brand = _safe_text(item.get("brand"))
        reference = _safe_text(item.get("reference"))
        reason = item.get("reason")
        if (
            brand is not None
            and reference is not None
            and isinstance(reason, str)
            and reason in _REJECTION_REASONS
        ):
            rejected_candidates.append({
                "brand": brand,
                "reference": reference,
                "reason": reason,
            })

    targeted_queries: list[dict] = []
    for item in _safe_diagnostic_items(diagnostics.targeted_queries):
        wave = _safe_int(item.get("wave"))
        query = _safe_text(item.get("query"))
        brand = _safe_text(item.get("brand"))
        reference = _safe_text(item.get("reference"))
        if wave is not None and query and brand and reference:
            targeted_queries.append({
                "wave": wave,
                "query": query,
                "brand": brand,
                "reference": reference,
            })

    page_states: list[dict] = []
    for item in _safe_diagnostic_items(diagnostics.page_states):
        wave = _safe_int(item.get("wave"))
        mode = item.get("mode")
        url = _safe_text(item.get("url"))
        selected = item.get("selected")
        fetched = item.get("fetched")
        analysis_status = item.get("analysis_status")
        audit_candidate_count = _safe_int(item.get("audit_candidate_count"))
        if (
            wave is not None
            and mode in _WAVE_MODES
            and url is not None
            and type(selected) is bool
            and type(fetched) is bool
            and analysis_status in _PAGE_ANALYSIS_STATES
            and audit_candidate_count is not None
        ):
            page_states.append({
                "wave": wave,
                "mode": mode,
                "url": url,
                "selected": selected,
                "fetched": fetched,
                "analysis_status": analysis_status,
                "audit_candidate_count": audit_candidate_count,
                "truncated_fields": _safe_names(
                    item.get("truncated_fields"), _TRUNCATED_FIELDS
                ),
            })

    candidate_evaluations: list[dict] = []
    allowed_contract_errors = frozenset({
        "CompatibilityContractError",
        "EvidenceContractError",
    })
    for item in _safe_diagnostic_items(
        getattr(diagnostics, "candidate_evaluations", None)
    ):
        wave = _safe_int(item.get("wave"))
        url = _safe_text(item.get("url"))
        reference = _safe_text(item.get("reference"))
        if wave is None or url is None or reference is None:
            continue
        contract_error = item.get("contract_error")
        if contract_error in allowed_contract_errors:
            candidate_evaluations.append({
                "wave": wave,
                "url": url,
                "reference": reference,
                "contract_error": contract_error,
            })
            continue
        brand = _safe_text(item.get("brand"))
        score = _safe_int(item.get("score"))
        proof_url_count = _safe_int(item.get("proof_url_count"))
        official_proof_count = _safe_int(item.get("official_proof_count"))
        eligible = item.get("eligible")
        complete = item.get("complete")
        if (
            brand is None
            or score is None
            or proof_url_count is None
            or official_proof_count is None
            or type(eligible) is not bool
            or type(complete) is not bool
        ):
            continue
        raw_proof_grades = item.get("proof_grades")
        proof_grades = {
            label: grade
            for label, grade in (
                raw_proof_grades.items()
                if isinstance(raw_proof_grades, dict)
                else ()
            )
            if label in allowed_criteria and grade in {"contiguous", "windowed"}
        }
        candidate_evaluation = {
            "wave": wave,
            "url": url,
            "reference": reference,
            "brand": brand,
            "score": score,
            "eligible": eligible,
            "complete": complete,
            "proof_url_count": proof_url_count,
            "official_proof_count": official_proof_count,
            "proven_criteria": _safe_names(
                item.get("proven_criteria"), allowed_criteria
            ),
            "not_proven_criteria": _safe_names(
                item.get("not_proven_criteria"), allowed_criteria
            ),
            "incompatible_criteria": _safe_names(
                item.get("incompatible_criteria"), allowed_criteria
            ),
            "unverified_criteria": _safe_names(
                item.get("unverified_criteria"), allowed_criteria
            ),
            "missing_audit_criteria": _safe_names(
                item.get("missing_audit_criteria"), allowed_criteria
            ),
            "rejected_proof_criteria": _safe_names(
                item.get("rejected_proof_criteria"), allowed_criteria
            ),
            "proof_grades": proof_grades,
            "non_applicable_criteria": _safe_names(
                item.get("non_applicable_criteria"), allowed_criteria
            ),
        }
        downgraded = _safe_names(
            item.get("downgraded_by_official_source"), allowed_criteria,
        )
        if downgraded:
            candidate_evaluation["downgraded_by_official_source"] = downgraded
        candidate_evaluations.append(candidate_evaluation)

    return {
        "wave_modes": wave_modes,
        "candidate_leads": candidate_leads,
        "rejected_candidates": rejected_candidates,
        "targeted_queries": targeted_queries,
        "page_states": page_states,
        "candidate_evaluations": candidate_evaluations,
    }


def _preuves_evaluation(evaluation) -> list[dict]:
    if evaluation is None:
        return []
    sources: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for criterion in evaluation.candidate.criteria:
        for proof in criterion.proofs:
            key = (proof.url, " ".join(proof.excerpt.split()).casefold())
            if key in seen:
                continue
            seen.add(key)
            sources.append(proof.model_dump())
    return sources


def _preuves_adaptatives(outcome: ResearchOutcome) -> list[dict]:
    return _preuves_evaluation(outcome.evaluation)


def _details_criteres(evaluation, requirements, *, incompatibles_seuls: bool = False) -> list[dict]:
    """Projette les faits par critère après les contrôles anti-invention.

    Les preuves ne sont exposées que pour un statut validé par
    `evaluate_candidates`. Un extrait rejeté demeure donc interne, même si le
    modèle l'avait fourni dans l'audit brut.
    """
    if evaluation is None:
        return []
    by_requirement = {
        criterion.requirement_id: criterion
        for criterion in evaluation.candidate.criteria
    }
    non_applicable = set(evaluation.summary.non_applicable_criteria)
    details: list[dict] = []
    for requirement in requirements.criteria:
        criterion = by_requirement.get(requirement.id)
        status = (
            "non_applicable"
            if requirement.label in non_applicable
            else criterion.status
            if criterion is not None
            else "not_proven"
        )
        if incompatibles_seuls and status != "incompatible":
            continue
        proofs = []
        if criterion is not None and status in {"proven", "incompatible"}:
            proofs = [proof.model_dump() for proof in criterion.proofs]
        details.append({
            "label": requirement.label,
            "requested_value": requirement.requested_value,
            "status": status,
            "proofs": proofs,
        })
    return details


def _evaluation_sort_key(evaluation) -> tuple:
    return (
        -evaluation.summary.score,
        -evaluation.official_proof_count,
        -evaluation.proof_url_count,
        evaluation.summary.brand.casefold(),
        evaluation.summary.reference.casefold(),
    )


def _evaluations_proposees(outcome: ResearchOutcome) -> list:
    pool = list(outcome.evaluations)
    if not pool and outcome.evaluation is not None:
        pool = [outcome.evaluation]
    by_identity = {}
    for evaluation in pool:
        if not evaluation.eligible or evaluation.summary.incompatible_criteria:
            continue
        key = (
            evaluation.summary.brand.strip().casefold(),
            evaluation.summary.reference.strip().casefold(),
        )
        previous = by_identity.get(key)
        if previous is None or _evaluation_sort_key(evaluation) < _evaluation_sort_key(previous):
            by_identity[key] = evaluation
    return sorted(by_identity.values(), key=_evaluation_sort_key)


def _niveau_preuve(evaluation) -> str:
    return (
        "Source officielle vérifiée."
        if evaluation.official_proof_count > 0
        else "Sources multiples corroborées."
        if evaluation.proof_url_count >= 3
        else "Deux sources secondaires, corroboration insuffisante."
        if evaluation.proof_url_count == 2
        else "Source unique, non corroborée."
    )


def _candidats_proposes(outcome: ResearchOutcome) -> list[dict]:
    candidates: list[dict] = []
    for evaluation in _evaluations_proposees(outcome):
        item = evaluation.summary.model_dump()
        item.update({
            "status": "complete" if evaluation.complete else "partial",
            "complete": evaluation.complete,
            "official_proof_count": evaluation.official_proof_count,
            "proof_url_count": evaluation.proof_url_count,
            "deviations": list(evaluation.candidate.deviations),
            "limitations": list(evaluation.candidate.limitations),
            "evidence_note": _niveau_preuve(evaluation),
            "sources": _preuves_evaluation(evaluation),
            "criteria": _details_criteres(evaluation, outcome.requirements),
        })
        candidates.append(item)
    return candidates


def _candidats_ecartes(outcome: ResearchOutcome) -> list[dict]:
    discarded: list[dict] = []
    for evaluation in outcome.evaluations:
        incompatible = list(evaluation.summary.incompatible_criteria)
        if not incompatible:
            continue
        reason = (
            f"{incompatible[0]} incompatible"
            if len(incompatible) == 1
            else f"{', '.join(incompatible)} incompatibles"
        )
        discarded.append({
            "brand": evaluation.summary.brand,
            "reference": evaluation.summary.reference,
            "reason": reason,
            "criteria": _details_criteres(
                evaluation, outcome.requirements, incompatibles_seuls=True,
            ),
        })
    return discarded


def _construire_adaptatif(outcome: ResearchOutcome) -> dict:
    evaluation = outcome.evaluation
    criteria = _details_criteres(evaluation, outcome.requirements)
    proposed_candidates = _candidats_proposes(outcome)
    discarded_candidates = _candidats_ecartes(outcome)
    if evaluation is None:
        if outcome.status == STATUT_SERVICE_INDISPONIBLE:
            alternative = (
                f"1. Produit demandé : {outcome.requirements.product}\n"
                "2. Alternative la plus pertinente : indisponible\n"
                "3. Justification et limites : le fournisseur LLM a limité "
                "les appels (HTTP 429). Aucun résultat métier n'est conclu."
            )
        else:
            alternative = "" if outcome.status == STATUT_REJETE else (
                f"1. Produit demandé : {outcome.requirements.product}\n"
                "2. Alternative la plus pertinente : aucune\n"
                "3. Justification et limites : aucune référence n'est suffisamment "
                "prouvée par les pages réellement consultées."
            )
        compatibility = None
    else:
        summary = evaluation.summary
        evidence_note = _niveau_preuve(evaluation)
        proposed_names = "; ".join(
            f"{item['brand']} {item['reference']} ({item['score']} %)"
            for item in proposed_candidates
        )
        alternative = (
            f"1. Produit demandé : {outcome.requirements.product}\n"
            f"2. Alternative la plus pertinente : {summary.brand} {summary.reference}\n"
            f"3. Justification et limites : compatibilité déterministe de "
            f"{summary.score} %. Critères prouvés : "
            f"{', '.join(summary.proven_criteria) or 'aucun'}. "
            f"Points non prouvés : {', '.join(summary.not_proven_criteria) or 'aucun'}. "
            f"Incompatibilités : {', '.join(summary.incompatible_criteria) or 'aucune'}. "
            f"Niveau de preuve : {evidence_note}\n"
            f"4. Candidats admissibles : {proposed_names}"
        )
        compatibility = summary.model_dump()
        compatibility["deviations"] = list(evaluation.candidate.deviations)
        compatibility["limitations"] = list(evaluation.candidate.limitations)
        compatibility["evidence_note"] = evidence_note

    diagnostics = outcome.diagnostics
    candidate_diagnostics = _safe_candidate_diagnostics(
        diagnostics,
        frozenset(item.label for item in outcome.requirements.criteria),
    )
    final_state = (
        diagnostics.final_state
        if isinstance(diagnostics.final_state, str)
        and diagnostics.final_state in _FINAL_STATES
        else "no_candidate_discovered"
    )
    return {
        "status": outcome.status,
        "alternative_proposee": alternative,
        "compatibility": compatibility,
        "proposed_candidates": proposed_candidates,
        "warnings": list(diagnostics.warnings),
        "sources": _preuves_adaptatives(outcome),
        "criteria": criteria,
        "discarded_candidates": discarded_candidates,
        "diagnostics": {
            "duration_seconds": getattr(diagnostics, "duration_seconds", 0.0),
            "waves": diagnostics.waves,
            "logical_queries": diagnostics.logical_queries,
            "engine_calls": diagnostics.engine_calls,
            "pages_opened": diagnostics.pages_opened,
            "pages_fetched": diagnostics.pages_fetched,
            "pages_rejected_by_gate": diagnostics.pages_rejected_by_gate,
            "pages_not_ready_for_evidence": diagnostics.pages_not_ready_for_evidence,
            "pages_analyzed": diagnostics.pages_analyzed,
            "llm_rate_limit_count": diagnostics.llm_rate_limit_count,
            "requirement_coverage": {
                "detected_specs": diagnostics.requirement_specs_detected,
                "covered_specs": diagnostics.requirement_specs_covered,
                "orphan_specs": diagnostics.requirement_orphans_detected,
                "orphan_pages": list(diagnostics.requirement_orphan_pages),
                "second_pass_triggered": diagnostics.requirement_second_pass,
            },
            "requirement_validation": _projection_requirement_validation(
                diagnostics.requirement_validation
            ),
            "page_audit_validation": _projection_page_audit_validation(
                diagnostics.page_audit_validation
            ),
            "gate_rejections": _projection_rejets(diagnostics),
            "near_miss_research": _projection_near_miss(diagnostics),
            "waves_detail": _projection_vagues(diagnostics),
            "stop_reason": str(getattr(diagnostics, "stop_reason", "") or ""),
            "unresolved_reason": _raison_non_resolue(outcome, diagnostics),
            "queries": list(diagnostics.queries),
            "engine_attempts": [item.model_dump() for item in diagnostics.attempts],
            "returned_urls": [item.url for item in diagnostics.hits],
            "opened_pages": list(diagnostics.opened_pages),
            "offtopic_urls_skipped": list(
                getattr(diagnostics, "offtopic_urls_skipped", ())
            ),
            **candidate_diagnostics,
            "final_state": final_state,
        },
    }


#: Champs autorises d'un rejet de porte. Allowlist explicite : le diagnostic
#: ne doit jamais transporter un fragment de page ni un message d'exception.
_CHAMPS_REJET = (
    "url",
    "reason",
    "mode",
    "content_length",
    "expected_identifiers",
    "matched_identifiers",
)


def _projection_rejets(diagnostics) -> list[dict]:
    rejets = getattr(diagnostics, "gate_rejections", None) or []
    projetes = []
    for item in rejets:
        if not isinstance(item, dict):
            continue
        projete = {
            champ: item[champ] for champ in _CHAMPS_REJET if champ in item
        }
        # Les identifiants restent des listes de chaines, jamais des objets.
        for champ in ("expected_identifiers", "matched_identifiers"):
            if champ in projete:
                projete[champ] = [str(value) for value in projete[champ]]
        projetes.append(projete)
    return projetes


#: Champs autorises d'une tache ciblee. Allowlist stricte : une tache est une
#: direction de recherche, elle ne transporte ni extrait, ni prompt, ni objet
#: candidat complet.
_CHAMPS_TACHE_CIBLEE = (
    "signature",
    "eligibility_path",
    "blocking_criterion",
    "target_value",
    "reason",
)


def _projection_near_miss(diagnostics) -> dict:
    """Projette les diagnostics de recherche ciblee, sans rien recalculer.

    L'objet est toujours present, meme a zero : un schema stable evite qu'un
    lecteur ait a distinguer « absent » de « aucune activite ».
    """
    taches = []
    for item in getattr(diagnostics, "near_miss_tasks", None) or []:
        if not isinstance(item, dict):
            continue
        taches.append({
            champ: str(item.get(champ, "") or "")
            for champ in _CHAMPS_TACHE_CIBLEE
        })

    return {
        "candidates_considered": getattr(
            diagnostics, "near_miss_candidates_considered", 0),
        "tasks_generated": getattr(diagnostics, "near_miss_tasks_generated", 0),
        "tasks_selected": getattr(diagnostics, "near_miss_tasks_selected", 0),
        "tasks_deduplicated": getattr(
            diagnostics, "near_miss_tasks_deduplicated", 0),
        "queries_scheduled": getattr(
            diagnostics, "near_miss_queries_scheduled", 0),
        "queries_skipped_budget": getattr(
            diagnostics, "near_miss_queries_skipped_budget", 0),
        "reserved_directional_signature": getattr(
            diagnostics, "reserved_directional_signature", "") or "",
        "seen_signatures": [
            str(value) for value in
            getattr(diagnostics, "seen_near_miss_signatures", None) or []
        ],
        "selected_tasks": taches,
        "skipped_candidates": _projection_ecartes(diagnostics),
    }


#: Champs autorises d'un near-miss ecarte : la condition non satisfaite et le
#: contexte qui permet de la comprendre, rien de plus.
_CHAMPS_ECARTE = (
    "reason",
    "identity",
    "family",
    "proven_compatible_count",
    "blocking_criteria",
    # Un candidat sans bloqueur s'ecarte — ou s'engage — sur ce qui lui manque.
    # Sans ce champ, `blocking_criteria: []` serait le seul indice, et le
    # rapport ne dirait pas ce qu'il restait a prouver.
    "missing_criteria",
)


def _projection_ecartes(diagnostics) -> list[dict]:
    """Pourquoi un candidat rejete n'a pas ouvert de recherche ciblee."""
    ecartes = []
    for item in getattr(diagnostics, "near_miss_skipped", None) or []:
        if not isinstance(item, dict):
            continue
        projete = {}
        for champ in _CHAMPS_ECARTE:
            valeur = item.get(champ)
            if champ in {"blocking_criteria", "missing_criteria"}:
                projete[champ] = [str(value) for value in (valeur or [])]
            elif champ == "proven_compatible_count":
                projete[champ] = int(valeur or 0)
            else:
                projete[champ] = str(valeur or "")
        ecartes.append(projete)
    return ecartes


#: Champs autorises d'une vague. Allowlist stricte : une vague expose ce
#: qu'elle a tente et consomme, jamais une requete brute, un extrait de page
#: ni une reponse de modele.
_CHAMPS_VAGUE = (
    "wave_index",
    "mode",
    "query_count",
    "logical_queries_before",
    "logical_queries_after",
    "task_types",
)


def _projection_vagues(diagnostics) -> list[dict]:
    """Rend la boucle adaptative lisible vague par vague."""
    vagues = []
    for item in getattr(diagnostics, "wave_details", None) or []:
        if not isinstance(item, dict):
            continue
        projete = {}
        for champ in _CHAMPS_VAGUE:
            valeur = item.get(champ)
            if champ == "task_types":
                projete[champ] = [str(value) for value in (valeur or [])]
            elif champ == "mode":
                projete[champ] = str(valeur or "")
            else:
                projete[champ] = int(valeur or 0)
        vagues.append(projete)
    return vagues


def _raison_non_resolue(outcome, diagnostics) -> str | None:
    """Distingue « rien trouve » de « rien de prouvable recupere ».

    Sans cette raison, une mission qui a ouvert 28 pages officielles et les a
    toutes rejetees faute de reference dans le contenu rend le meme
    `NOT_RESOLVED` qu'une mission qui n'a rien trouve du tout. Ce sont deux
    situations opposees : la premiere designe un probleme de recuperation, la
    seconde un probleme de recherche.
    """
    if outcome.status != "not_resolved":
        return None
    if diagnostics.pages_analyzed:
        return "NO_PROVABLE_CANDIDATE"
    if diagnostics.pages_rejected_by_gate:
        return "PRODUCT_CONTENT_NOT_RETRIEVED"
    if diagnostics.pages_not_ready_for_evidence:
        return "NO_VALIDATED_IDENTITY"
    if diagnostics.pages_fetched == 0:
        return "NO_PAGE_RETRIEVED"
    return None


def _section_vagues(donnees: dict) -> list[str]:
    """Section Markdown : ce que chaque vague a tente et consomme."""
    diagnostics = donnees.get("diagnostics") or {}
    vagues = diagnostics.get("waves_detail") or []
    if not vagues:
        return []

    lignes = ["## Déroulé des vagues", ""]
    for vague in vagues:
        types = ", ".join(vague.get("task_types") or []) or "aucune"
        lignes.append(
            f"- Vague {vague.get('wave_index', 0)} ({vague.get('mode', '')}) : "
            f"{vague.get('query_count', 0)} requête(s), "
            f"budget {vague.get('logical_queries_before', 0)} → "
            f"{vague.get('logical_queries_after', 0)} — {types}"
        )
    arret = diagnostics.get("stop_reason")
    if arret:
        lignes += ["", f"Arrêt : {arret}"]
    lignes.append("")
    return lignes


def _section_near_miss(donnees: dict) -> list[str]:
    """Section Markdown, presente seulement s'il y a eu une activite ciblee.

    Le vocabulaire evite deliberement « preuve » et « equivalent » : une tache
    ciblee est une direction de recherche declenchee par un rejet prouve, pas
    un resultat.
    """
    section = (donnees.get("diagnostics") or {}).get("near_miss_research") or {}
    if not section.get("tasks_generated") and not section.get("candidates_considered"):
        return []

    lignes = ["## Recherche ciblée depuis les candidats proches", ""]
    lignes += [
        f"- Candidats examinés : {section.get('candidates_considered', 0)}",
        f"- Tâches générées : {section.get('tasks_generated', 0)}",
        f"- Tâches sélectionnées : {section.get('tasks_selected', 0)}",
        f"- Requêtes envoyées : {section.get('queries_scheduled', 0)}",
        f"- Tâches dédupliquées : {section.get('tasks_deduplicated', 0)}",
        f"- Tâches écartées faute de budget : "
        f"{section.get('queries_skipped_budget', 0)}",
        f"- Réservation directionnelle : "
        f"{section.get('reserved_directional_signature') or 'aucune'}",
        "",
    ]

    taches = section.get("selected_tasks") or []
    if taches:
        lignes += ["### Directions retenues", ""]
        for tache in taches:
            lignes += [
                f"- Chemin : {tache.get('eligibility_path', '')}",
                f"  - Critère bloquant : {tache.get('blocking_criterion', '')}",
                f"  - Valeur cible : {tache.get('target_value', '')}",
                f"  - Motif : {tache.get('reason', '')}",
                f"  - Signature : {tache.get('signature', '')}",
            ]
        lignes.append("")

    return lignes


def _section_candidats_proposes(donnees: dict) -> list[str]:
    candidates = donnees.get("proposed_candidates") or []
    if not candidates:
        return []
    lignes = ["## Candidats proposés", ""]
    for candidate in candidates:
        lignes += [
            f"### {candidate.get('brand', '')} {candidate.get('reference', '')} "
            f"— {candidate.get('score', 0)} % ({candidate.get('status', 'partial')})",
            "",
            f"Niveau de preuve : {candidate.get('evidence_note', '')}",
            "",
        ]
        for criterion in candidate.get("criteria") or []:
            lignes += [
                f"#### {criterion.get('label', 'Critère')} — "
                f"{criterion.get('status', 'not_proven')}",
                "",
            ]
            proofs = criterion.get("proofs") or []
            if proofs:
                for proof in proofs:
                    url = proof.get("url", "")
                    lignes += [
                        f"- [{url}]({url})",
                        f"  > {(proof.get('excerpt') or '').strip()}",
                    ]
            else:
                lignes.append("_Aucune preuve vérifiée._")
            lignes.append("")
    return lignes


def _texte_rapport(resultat: Alternative) -> str:
    """Les trois sections imposees par la specification d'origine, dans l'ordre.

    Le texte reprend les champs du resultat sans les reecrire : tout ce qui est
    affirme ici a deja ete etabli face aux sources.

    Les ecarts precedent les limites, et sont annonces meme quand il n'y en a
    pas : c'est l'information qui decide si le produit se remplace tel quel.
    """
    lignes = [
        f"1. Produit demandé : {resultat.produit_demande}",
        f"2. Alternative la plus pertinente : {resultat.alternative}",
        f"3. Justification et limites : {resultat.justification}",
        "",
        f"Pertinence estimée : {resultat.pertinence} %",
        "",
        "Écarts constatés :",
    ]
    lignes += ([f"- {ecart}" for ecart in resultat.ecarts]
               or ["- Aucun : le produit se remplace en l'état."])
    lignes += ["", "Points à confirmer :"]
    lignes += ([f"- {limite}" for limite in resultat.limites]
               or ["- Aucun."])
    return "\n".join(lignes)


def construire(
    resultat: Optional[Alternative] | ResearchOutcome,
    avertissements: Optional[List[str]] = None,
    *,
    produit_demande: str = "",
) -> dict:
    """Rend le dictionnaire de sortie, resultat ou non.

    `produit_demande` sert quand aucune alternative n'a ete retenue : le rapport
    doit tout de meme dire de quel produit il s'agissait, sinon la sortie
    negative est inexploitable.
    """
    if isinstance(resultat, ResearchOutcome):
        return _construire_adaptatif(resultat)

    avertissements = list(avertissements or [])

    if resultat is None:
        return {
            "status": STATUT_SANS_REPONSE,
            "alternative_proposee": (
                f"1. Produit demandé : {produit_demande or 'non identifié'}\n"
                "2. Alternative la plus pertinente : aucune\n"
                "3. Justification et limites : aucune alternative n'est "
                "suffisamment étayée par les sources consultées. Aucune "
                "référence n'est proposée."
            ),
            "warnings": avertissements,
            "sources": [],
        }

    # Le statut rendu decoule des ecarts constates, pas de ce que le modele a
    # ecrit dans son champ `statut` : un candidat qui declare des ecarts est
    # « avec_adaptation », meme s'il s'est annonce « complete ».
    statut = STATUT_AVEC_ADAPTATION if resultat.ecarts else STATUT_COMPLET
    return {
        "status": statut,
        "alternative_proposee": _texte_rapport(resultat),
        "pertinence": resultat.pertinence,
        "warnings": avertissements,
        "sources": [s.model_dump() for s in resultat.sources],
    }


def entree_invalide(motif: str) -> dict:
    """Sortie d'une fiche absente, vide ou illisible."""
    return {
        "status": STATUT_ENTREE_INVALIDE,
        "alternative_proposee": "",
        "warnings": [motif],
        "sources": [],
    }


def erreur_configuration(motif: str) -> dict:
    return {
        "status": STATUT_CONFIGURATION,
        "alternative_proposee": "",
        "compatibility": None,
        "warnings": [motif],
        "sources": [],
        "diagnostics": {},
    }


def erreur_analyse(
    type_erreur: str,
    requirement_diagnostics: object = None,
) -> dict:
    return {
        "status": STATUT_ANALYSE,
        "alternative_proposee": "",
        "compatibility": None,
        "warnings": [f"Analyse ScrapeGraphAI impossible : {type_erreur}."],
        "sources": [],
        "diagnostics": {
            "requirement_validation": _projection_requirement_validation(
                requirement_diagnostics
            ),
        },
    }


def service_indisponible() -> dict:
    return {
        "status": STATUT_SERVICE_INDISPONIBLE,
        "alternative_proposee": "",
        "compatibility": None,
        "warnings": [
            "Service LLM temporairement indisponible : quota HTTP 429 atteint."
        ],
        "sources": [],
        "diagnostics": {"stop_reason": "LLM_RATE_LIMITED"},
    }


def en_markdown(donnees: dict) -> str:
    """Rend la version lisible. Les sections vides sont dites, jamais omises."""
    lignes = ["# Alternative produit", "", f"**Statut** : {donnees['status']}"]
    if donnees.get("pertinence"):
        lignes.append(f"**Pertinence** : {donnees['pertinence']} %")
    compatibility = donnees.get("compatibility")
    if compatibility:
        lignes.append(f"**Compatibilité** : {compatibility['score']} %")
    lignes.append("")

    rapport = (donnees.get("alternative_proposee") or "").strip()
    lignes += [rapport or "_Aucun rapport : l'entrée n'a pas pu être exploitée._", ""]

    lignes += _section_candidats_proposes(donnees)

    lignes += ["## Sources", ""]
    sources = donnees.get("sources") or []
    if sources:
        for source in sources:
            lignes += [
                f"- [{source.get('url', '')}]({source.get('url', '')}) "
                f"({source.get('type', 'type inconnu')})",
                f"  > {(source.get('excerpt') or source.get('extrait') or '').strip()}",
            ]
    else:
        lignes.append("_Aucune source retenue._")
    lignes.append("")

    criteria = donnees.get("criteria") or []
    if criteria:
        lignes += ["## Vérification par critère", ""]
        for criterion in criteria:
            label = criterion.get("label", "Critère")
            status = criterion.get("status", "not_proven")
            lignes += [f"### {label} — {status}", ""]
            proofs = criterion.get("proofs") or []
            if proofs:
                for proof in proofs:
                    url = proof.get("url", "")
                    lignes += [
                        f"- [{url}]({url})",
                        f"  > {(proof.get('excerpt') or '').strip()}",
                    ]
            else:
                lignes.append("_Aucune preuve vérifiée._")
            lignes.append("")

    discarded = donnees.get("discarded_candidates") or []
    if discarded:
        lignes += ["## Candidats écartés", ""]
        for candidate in discarded:
            lignes.append(
                f"- Écarté : {candidate.get('brand', '')} "
                f"{candidate.get('reference', '')} — {candidate.get('reason', '')}"
            )
            for criterion in candidate.get("criteria") or []:
                for proof in criterion.get("proofs") or []:
                    url = proof.get("url", "")
                    lignes += [
                        f"  - [{url}]({url})",
                        f"    > {(proof.get('excerpt') or '').strip()}",
                    ]
        lignes.append("")

    lignes += _section_vagues(donnees)
    lignes += _section_near_miss(donnees)

    lignes += ["## Avertissements", ""]
    avertissements = donnees.get("warnings") or []
    if avertissements:
        lignes += [f"- {a}" for a in avertissements]
    else:
        lignes.append("_Aucun._")

    if compatibility:
        lignes += ["", "## Critères prouvés", ""]
        lignes += ([f"- {item}" for item in compatibility.get("proven_criteria", [])]
                   or ["_Aucun._"])
        lignes += ["", "## Critères non prouvés", ""]
        lignes += ([f"- {item}" for item in compatibility.get("not_proven_criteria", [])]
                   or ["_Aucun._"])
        lignes += ["", "## Incompatibilités", ""]
        lignes += ([f"- {item}" for item in compatibility.get("incompatible_criteria", [])]
                   or ["_Aucune._"])

    diagnostics = donnees.get("diagnostics") or {}
    if diagnostics:
        requirement_validation = diagnostics.get("requirement_validation") or []
        if requirement_validation:
            lignes += ["", "## Diagnostic d'extraction des critères", ""]
            for item in requirement_validation:
                if (
                    item["issue"] == "unit_label_disagreement"
                    and item["action"] == "review"
                ):
                    detail = "unités incompatibles — à vérifier"
                else:
                    detail = f"{item['issue']} ({item['action']})"
                lignes.append(
                    f"- Étage `{item['stage']}`, chemin `{item['path']}` : "
                    f"{detail}."
                )
        page_audit_validation = diagnostics.get("page_audit_validation") or []
        if page_audit_validation:
            lignes += ["", "## Diagnostic d'audit des pages", ""]
            for item in page_audit_validation:
                lignes.append(
                    f"- Etage `{item['stage']}`, chemin `{item['path']}` : "
                    f"{item['issue']} ({item['action']})."
                )
        wave_modes = diagnostics.get("wave_modes") or []
        modes = "; ".join(
            f"{item['wave']}: {item['mode']}"
            for item in wave_modes
            if isinstance(item, dict) and "wave" in item and "mode" in item
        )
        candidate_leads = diagnostics.get("candidate_leads") or []
        references = "; ".join(
            f"{item['brand']} {item['reference']}"
            for item in candidate_leads
            if isinstance(item, dict) and item.get("brand") and item.get("reference")
        )
        page_counts: dict[str, int] = {}
        for item in diagnostics.get("page_states") or []:
            if not isinstance(item, dict):
                continue
            status = item.get("analysis_status")
            if isinstance(status, str) and status:
                page_counts[status] = page_counts.get(status, 0) + 1
        page_states = "; ".join(
            f"{status}: {page_counts[status]}" for status in sorted(page_counts)
        )
        lignes += [
            "",
            "## Diagnostic de recherche",
            "",
            f"- Vagues : {diagnostics.get('waves', 0)}",
            f"- Requêtes logiques : {diagnostics.get('logical_queries', 0)}",
            f"- Appels moteur : {diagnostics.get('engine_calls', 0)}",
            f"- Pages ouvertes : {diagnostics.get('pages_opened', 0)}",
            f"- Pages recuperees : {diagnostics.get('pages_fetched', 0)}",
            f"- Pages rejetees par la porte : "
            f"{diagnostics.get('pages_rejected_by_gate', 0)}",
            f"- Pages sans identite validee : "
            f"{diagnostics.get('pages_not_ready_for_evidence', 0)}",
            f"- Pages analysees : {diagnostics.get('pages_analyzed', 0)}",
            f"- Raison de non-resolution : "
            f"{diagnostics.get('unresolved_reason') or 'sans objet'}",
            f"- Etat final : {diagnostics.get('final_state', 'non renseigne')}",
            f"- Modes de vague : {modes or 'aucun'}",
            f"- References decouvertes : {references or 'aucune'}",
            f"- Etats des pages : {page_states or 'aucun'}",
        ]

    return "\n".join(lignes) + "\n"


def ecrire(dossier: str | Path, donnees: dict, *, nom: str = "alternative") -> Tuple[Path, Path]:
    """Ecrit les deux fichiers et rend leurs chemins.

    `encoding="utf-8"` est explicite : sous Windows, le defaut est la page de
    code ANSI, qui mutile les accents des fiches produit.
    """
    dossier = Path(dossier)
    dossier.mkdir(parents=True, exist_ok=True)

    chemin_json = dossier / f"{nom}.json"
    chemin_md = dossier / f"{nom}.md"

    chemin_json.write_text(
        json.dumps(donnees, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    chemin_md.write_text(en_markdown(donnees), encoding="utf-8")
    return chemin_json, chemin_md
