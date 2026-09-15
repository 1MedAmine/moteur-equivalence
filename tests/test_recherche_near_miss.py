# -*- coding: utf-8 -*-
"""Câblage near-miss dans l'orchestrateur.

Un candidat rejeté sur une incompatibilité prouvée alimente les requêtes de la
vague suivante. Les résultats reviennent par le pipeline normal : aucune voie
spéciale, aucun privilège de preuve.

Périmètre : orchestrateur et diagnostics en mémoire. La sérialisation JSON et
Markdown appartient à la tâche suivante.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from configuration import B2Config
from modeles import (
    CandidateAudit,
    CriterionAudit,
    PageAudit,
    SearchAttempt,
    SearchBatch,
    SearchHit,
    SourceProof,
)
from analyse import PageAnalysis
from recherche_adaptative import AdaptiveResearch
from scraping import PageContent


URL = "https://revendeur.example/xz07-20-10-13"
CONTENU = (
    "Contacteur Norel XZ07-20-10-13. Bobine 100...250V AC/DCC. "
    "Nombre de poles 3 poles. Contacts principaux 3 NO."
)


class _Planner:
    """Quatre requêtes par vague, sans dépendance au modèle."""

    def __init__(self) -> None:
        self.calls = 0

    def extract_requirements(self, fiche):
        from modeles import RequirementSet

        return RequirementSet(
            product="contacteur 9 A bobine 24 V DC",
            criteria=[
                {"id": "poles", "label": "Nombre de poles",
                 "requested_value": "3P", "critical": False},
                {"id": "contacts", "label": "Contacts principaux",
                 "requested_value": "3 NO", "critical": False},
                {"id": "coil", "label": "Tension de bobine",
                 "requested_value": "24 V DC", "critical": True},
            ],
        )

    def plan_queries(self, requirement_set, target_brand, missing, previous):
        self.calls += 1
        from types import SimpleNamespace

        return SimpleNamespace(
            queries=[f"generale-{self.calls}-{index}" for index in range(4)],
            strategy="model",
        )

    def plan_targeted_queries(self, requirement_set, leads, previous):
        """Plan ciblé classique, distinct des requêtes near-miss."""
        self.calls += 1
        from types import SimpleNamespace

        return SimpleNamespace(
            queries=[
                SimpleNamespace(
                    query=f"generale-{self.calls}-{index}",
                    candidate_key=lead.key,
                )
                for index, lead in enumerate(leads[:4])
            ],
            strategy="model",
        )


class _Gateway:
    """Compte les requêtes logiques réellement envoyées."""

    def __init__(self, *, engine_attempts: int = 1) -> None:
        self.queries: list[str] = []
        self.engine_attempts = engine_attempts

    def search(self, query: str) -> SearchBatch:
        self.queries.append(query)
        return SearchBatch(
            query=query,
            hits=[SearchHit(url=URL, title="XZ07", snippet="", engine="bi", rank=1)],
            attempts=[
                SearchAttempt(engine="bi", status="ok", result_count=1)
                for _ in range(self.engine_attempts)
            ],
        )


class _Fetcher:
    def fetch(self, url: str) -> PageContent:
        return PageContent(url, CONTENU, "XZ07-20-10-13 | Norel", "scrapling")


def _candidat_rejete() -> CandidateAudit:
    """Bobine incompatible prouvée littéralement ; le reste non prouvé."""
    return CandidateAudit(
        brand="Norel",
        reference="XZ07-20-10-13",
        criteria=[
            CriterionAudit(requirement_id="poles", requested_value="3P",
                           observed_value="", status="not_proven"),
            CriterionAudit(requirement_id="contacts", requested_value="3 NO",
                           observed_value="", status="not_proven"),
            CriterionAudit(
                requirement_id="coil", requested_value="24 V DC",
                observed_value="100...250V AC/DCC", status="incompatible",
                proofs=[SourceProof(url=URL, excerpt="Bobine 100...250V AC/DCC",
                                    type="web_officiel")],
            ),
        ],
    )


def _candidat_sans_incompatibilite() -> CandidateAudit:
    return CandidateAudit(
        brand="Norel",
        reference="XZ07-20-10-13",
        criteria=[
            CriterionAudit(requirement_id="poles", requested_value="3P",
                           observed_value="", status="not_proven"),
            CriterionAudit(requirement_id="contacts", requested_value="3 NO",
                           observed_value="", status="not_proven"),
            CriterionAudit(requirement_id="coil", requested_value="24 V DC",
                           observed_value="", status="not_proven"),
        ],
    )


def _candidat_sous_documente() -> CandidateAudit:
    """Deux critères prouvés littéralement, un sans preuve, aucun écart.

    Forme mesurée le 2026-08-20 sur `4KBL103001R8110` : rejeté faute de score,
    mais bien étayé et sans aucun bloqueur. L'ancienne règle d'éligibilité
    exigeait une incompatibilité prouvée et n'en tirait donc rien.
    """
    return CandidateAudit(
        brand="Norel",
        reference="XZ07-20-10-13",
        criteria=[
            CriterionAudit(
                requirement_id="poles", requested_value="3P",
                observed_value="3 poles", status="proven",
                proofs=[SourceProof(url=URL, excerpt="Nombre de poles 3 poles",
                                    type="web_officiel")],
            ),
            CriterionAudit(
                requirement_id="contacts", requested_value="3 NO",
                observed_value="3 NO", status="proven",
                proofs=[SourceProof(url=URL, excerpt="Contacts principaux 3 NO",
                                    type="web_officiel")],
            ),
            CriterionAudit(requirement_id="coil", requested_value="24 V DC",
                           observed_value="", status="not_proven"),
        ],
    )


class _Analyzer:
    """Rend le candidat voulu à la première vague, rien ensuite."""

    def __init__(self, candidat_factory=None) -> None:
        self.calls: list[dict] = []
        self.candidat_factory = candidat_factory or _candidat_rejete

    def __call__(self, pages, requirement_set, target_brand, graph_config, **kwargs):
        self.calls.append({"pages": tuple(pages), "mode": kwargs.get("mode")})
        if len(self.calls) > 1 or not pages:
            return [], []
        page = pages[0]
        return [PageAnalysis(
            page_url=page.url,
            content=page.content,
            audit=PageAudit(page_url=page.url, candidates=[self.candidat_factory()]),
            mode=kwargs.get("mode", "audit"),
        )], []


def _service(*, gateway=None, analyzer=None, waves=2, **injections):
    gateway = gateway or _Gateway()
    analyzer = analyzer or _Analyzer()
    service = AdaptiveResearch(
        config=B2Config(api_key="test", adaptive_max_waves=waves),
        planner=_Planner(),
        gateway=gateway,
        fetcher=_Fetcher(),
        analyze_pages=analyzer,
        graph_config={},
        **injections,
    )
    return service, gateway, analyzer


def _requetes_near_miss(gateway) -> list[str]:
    return [item for item in gateway.queries if not item.startswith("generale-")]


# --------------------------------------------------------------------------
# Déclenchement métier
# --------------------------------------------------------------------------

def test_rejected_candidate_with_proven_incompatibility_emits_a_targeted_query():
    """Le cas XZ07 de bout en bout : identité + critère + valeur cible."""
    service, gateway, _ = _service()

    outcome = service.run("fiche", "Norel")

    ciblees = _requetes_near_miss(gateway)
    assert ciblees, gateway.queries
    for query in ciblees:
        assert "XZ07" in query
        assert any(mot in query.casefold() for mot in ("bobine", "coil"))
        assert '"24 V DC"' in query
        assert "XZ07-20-10-11" not in query

    assert outcome.diagnostics.near_miss_tasks_generated >= 1
    assert outcome.diagnostics.near_miss_queries_scheduled >= 1


def test_an_underdocumented_candidate_emits_a_targeted_query():
    """Mutation détectée : le cas mesuré sur la mission ne produit aucune requête."""
    service, gateway, _ = _service(
        analyzer=_Analyzer(_candidat_sous_documente)
    )

    outcome = service.run("fiche", "Norel")

    ciblees = _requetes_near_miss(gateway)
    assert ciblees, gateway.queries
    # La direction porte le critère restant à prouver et sa valeur cible.
    assert any('"24 V DC"' in requete for requete in ciblees)
    assert outcome.diagnostics.near_miss_tasks_generated >= 1
    assert any(
        tache.get("eligibility_path") == "INCOMPLETE"
        for tache in outcome.diagnostics.near_miss_tasks
    )


def test_a_candidate_without_any_proven_criterion_emits_nothing():
    """Sans plusieurs compatibles prouvés, rien n'atteste la proximité."""
    service, gateway, _ = _service(
        analyzer=_Analyzer(_candidat_sans_incompatibilite)
    )

    outcome = service.run("fiche", "Norel")

    assert _requetes_near_miss(gateway) == []
    assert outcome.diagnostics.near_miss_tasks_generated == 0


def test_directional_path_is_visible_in_diagnostics():
    service, _, _ = _service()

    diagnostics = service.run("fiche", "Norel").diagnostics

    chemins = {item["eligibility_path"] for item in diagnostics.near_miss_tasks}
    assert "DIRECTIONAL" in chemins
    detail = diagnostics.near_miss_tasks[0]
    assert detail["blocking_criterion"] == "coil"
    assert detail["target_value"] == "24 V DC"


# --------------------------------------------------------------------------
# Budget
# --------------------------------------------------------------------------

def test_logical_queries_never_exceed_twelve():
    """Aucune branche ne peut faire dépasser le plafond absolu."""
    service, gateway, _ = _service(waves=3)

    diagnostics = service.run("fiche", "Norel").diagnostics

    assert diagnostics.logical_queries <= 12
    assert len(gateway.queries) == diagnostics.logical_queries


# Le cas « near-miss ne des au dernier tour, aucune place pour le servir » est
# couvert par test_recherche_boucle_adaptative.py, avec un double qui rend des
# URL distinctes ; le doublon fragile qui vivait ici a ete retire.


def test_logical_and_engine_counters_stay_separate():
    """Une requête logique vaut 1, quels que soient les essais moteur."""
    gateway = _Gateway(engine_attempts=3)
    service, _, _ = _service(gateway=gateway)

    diagnostics = service.run("fiche", "Norel").diagnostics

    assert diagnostics.engine_calls == 3 * diagnostics.logical_queries


# --------------------------------------------------------------------------
# Déduplication entre vagues
# --------------------------------------------------------------------------

def test_the_same_signature_is_never_scheduled_twice():
    """Le même near-miss réapparaît : sa requête n'est envoyée qu'une fois."""
    class _Repete(_Analyzer):
        def __call__(self, pages, requirement_set, target_brand, graph_config, **kwargs):
            self.calls.append({"pages": tuple(pages), "mode": kwargs.get("mode")})
            if not pages:
                return [], []
            page = pages[0]
            return [PageAnalysis(
                page_url=page.url,
                content=page.content,
                audit=PageAudit(page_url=page.url,
                                candidates=[_candidat_rejete()]),
                mode=kwargs.get("mode", "audit"),
            )], []

    service, gateway, _ = _service(analyzer=_Repete(), waves=3)

    diagnostics = service.run("fiche", "Norel").diagnostics

    ciblees = _requetes_near_miss(gateway)
    assert len(ciblees) == len(set(ciblees))
    assert len(diagnostics.seen_near_miss_signatures) >= 1


# --------------------------------------------------------------------------
# Intégration du résultat
# --------------------------------------------------------------------------

def test_targeted_results_return_through_the_normal_pipeline():
    """Aucune voie spéciale : la page découverte repasse par la porte."""
    service, _, analyzer = _service()

    outcome = service.run("fiche", "Norel")

    # Les pages analysées sont passées par la qualification habituelle.
    assert outcome.diagnostics.pages_fetched >= outcome.diagnostics.pages_analyzed
    assert outcome.diagnostics.pages_fetched == (
        outcome.diagnostics.pages_analyzed
        + outcome.diagnostics.pages_rejected_by_gate
        + outcome.diagnostics.pages_not_ready_for_evidence
    )


def test_task_diagnostics_carry_no_page_content():
    service, _, _ = _service()

    diagnostics = service.run("fiche", "Norel").diagnostics

    for item in diagnostics.near_miss_tasks:
        assert set(item) <= {
            "signature", "eligibility_path", "blocking_criterion",
            "target_value", "reason",
        }
        assert "Bobine 100" not in repr(item)


# --------------------------------------------------------------------------
# Non-régression
# --------------------------------------------------------------------------

def test_without_any_near_miss_the_wave_count_is_unchanged():
    """Absence de tâche : même nombre d'appels qu'avant le câblage."""
    service, gateway, _ = _service(
        analyzer=_Analyzer(_candidat_sans_incompatibilite), waves=2
    )

    diagnostics = service.run("fiche", "Norel").diagnostics

    # Aucune requête ne vient de la branche near-miss : toutes sont planifiées.
    assert all(query.startswith("generale-") for query in gateway.queries)
    assert diagnostics.near_miss_tasks_generated == 0
    assert diagnostics.near_miss_queries_scheduled == 0
    # Le compteur logique reste celui du plan seul.
    assert diagnostics.logical_queries == len(gateway.queries)
