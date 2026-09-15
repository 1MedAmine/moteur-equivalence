# -*- coding: utf-8 -*-
"""Fixture déterministe : un candidat rejeté devient une direction de recherche.

Reproduit l'audit réel de `XZ07-20-10-13` mesuré le 2026-08-19. Les trois
critères annoncés compatibles reposaient sur une citation reconstruite par le
modèle, refusée par le contrat de preuve et rétrogradée en invérifiable. Seule
l'incompatibilité de bobine s'appuyait sur un extrait littéral valide — c'était
le seul fait solide de cet audit, et c'est lui qui doit donner la direction.

Les évaluations ne sont pas déclarées à la main : elles sortent de
`evaluate_candidates`, pour que la rétrogradation soit réellement produite par
le contrat de preuve et non simplement affirmée par le test.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from compatibilite import evaluate_candidates
from modeles import (
    CandidateAudit,
    CriterionAudit,
    PageAudit,
    RequirementSet,
    SourceProof,
)


def _build_targeted_tasks(*args, **kwargs):
    """Charge `build_targeted_tasks` au dernier moment.

    Importer en tete de module transformerait son absence en erreur de
    collecte, qui interrompt toute la suite et masque de vrais echecs
    ailleurs. Ici, l'absence devient l'echec explicite de ces tests seuls.
    """
    try:
        from near_miss import build_targeted_tasks
    except ImportError as error:  # pragma: no cover - etat rouge transitoire
        pytest.fail(f"build_targeted_tasks n'est pas encore disponible : {error}")
    return build_targeted_tasks(*args, **kwargs)


REVENDEUR_A = "https://www.revendeur-a.example/501399-contacteur-moteur-xz07-20-10-13-norel"
DISTRIBUTEUR_C = "https://www.distributeur-c.example/norel/contacteur-xz07-20-10-13"

# L'agrégat que le modèle a fabriqué en assemblant des fragments dispersés.
# Il n'apparaît tel quel sur aucune page : le contrat de preuve le refuse.
AGREGAT = "* 100...250V AC/DCC * 3P * 1 contact auxiliaire N/O * 400 V - CA-3 : 9A / 4kW"

PAGE_REVENDEUR_A = (
    "Contacteur moteur XZ07-20-10-13 Norel. Bobine 100...250V AC/DCC. "
    "Il permet de commander des moteurs 3 phases jusqu'a 4 kW / 400 V AC (AC-3)."
)
PAGE_DISTRIBUTEUR_C = (
    "Contacteur de puissance Norel 3 poles 9A XZ07-20-10-13. "
    "Nombre de poles 3 poles. Courant de service nominal AC-3 a 400 V 9 A. "
    "Contacts principaux 3 contacts a fermeture."
)

PAGES = {REVENDEUR_A: PAGE_REVENDEUR_A, DISTRIBUTEUR_C: PAGE_DISTRIBUTEUR_C}


def _requirements() -> RequirementSet:
    return RequirementSet(
        product="contacteur 9 A bobine 24 V DC",
        criteria=[
            {"id": "nombre_de_poles", "label": "Nombre de poles",
             "requested_value": "3P", "critical": False},
            {"id": "courant_nominal", "label": "Courant nominal",
             "requested_value": "9 A (AC-3)", "critical": False},
            {"id": "contacts_principaux", "label": "Contacts principaux",
             "requested_value": "3 NO", "critical": False},
            {"id": "tension_de_bobine", "label": "Tension de bobine",
             "requested_value": "24 V DC", "critical": True},
        ],
    )


def _candidat_xz07() -> CandidateAudit:
    """Trois preuves reconstruites, une preuve littérale d'incompatibilité."""
    def _reconstruit(requirement_id: str, requested: str, observed: str):
        return CriterionAudit(
            requirement_id=requirement_id,
            requested_value=requested,
            observed_value=observed,
            status="proven",
            proofs=[SourceProof(url=REVENDEUR_A, excerpt=AGREGAT, type="web_officiel")],
        )

    return CandidateAudit(
        brand="Norel",
        reference="XZ07-20-10-13",
        criteria=[
            _reconstruit("nombre_de_poles", "3P", "3P"),
            _reconstruit("courant_nominal", "9 A (AC-3)", "9 A (AC-3)"),
            _reconstruit("contacts_principaux", "3 NO", "3 NO"),
            CriterionAudit(
                requirement_id="tension_de_bobine",
                requested_value="24 V DC",
                observed_value="100...250V AC/DCC",
                status="incompatible",
                proofs=[SourceProof(
                    url=REVENDEUR_A,
                    excerpt="Bobine 100...250V AC/DCC",
                    type="web_officiel",
                )],
            ),
        ],
    )


def _identite():
    """Identité corroborée par le contenu récupéré, jamais déduite."""
    return {"identity": "XZ07-20-10-13", "family": "XZ07"}


def _evaluation_xz07():
    """Évaluation réelle : le contrat de preuve rétrograde les trois citations."""
    return evaluate_candidates(
        _requirements(),
        [PageAudit(page_url=REVENDEUR_A, candidates=[_candidat_xz07()])],
        target_brand="Norel",
        threshold=75,
        visited_pages=PAGES,
        strict_evidence=False,
    )[0]


def test_the_fixture_really_reproduces_the_measured_audit():
    """Garde-fou : si le contrat change, la fixture doit le dire, pas le masquer."""
    summary = _evaluation_xz07().summary

    assert summary.proven_criteria == []
    assert sorted(summary.unverified_criteria) == [
        "Contacts principaux", "Courant nominal", "Nombre de poles",
    ]
    assert summary.incompatible_criteria == ["Tension de bobine"]
    assert summary.critical_blockers == ["Tension de bobine"]


def test_xz07_rejected_on_coil_voltage_emits_a_directional_task():
    """Le seul fait prouvé — l'incompatibilité — suffit à donner une direction."""
    tasks = _build_targeted_tasks(((_evaluation_xz07(), _identite()),), _requirements())

    assert len(tasks) == 1
    task = tasks[0]
    assert task.eligibility_path == "DIRECTIONAL"
    assert task.blocking_criterion == "tension_de_bobine"
    assert task.observed_value == "100...250V AC/DCC"
    assert task.target_value == "24 V DC"


def test_downgraded_criteria_are_never_counted_as_compatible():
    """Sans cette règle, XZ07 basculerait sur ESTABLISHED et masquerait le chemin B."""
    task = _build_targeted_tasks(((_evaluation_xz07(), _identite()),), _requirements())[0]

    assert task.proven_compatible_count == 0
    assert task.eligibility_path == "DIRECTIONAL"


def test_queries_carry_identity_criterion_and_target_value():
    """Les trois composants obligatoires, dans chaque requête émise."""
    task = _build_targeted_tasks(((_evaluation_xz07(), _identite()),), _requirements())[0]

    assert task.search_queries
    for query in task.search_queries:
        assert any(jeton in query for jeton in ("Norel", "XZ07")), query
        assert any(
            jeton in query.casefold() for jeton in ("bobine", "coil")
        ), query
        # Valeur cible citée littéralement, jamais convertie.
        assert '"24 V DC"' in query, query


def test_target_value_is_never_rewritten():
    """`24 V DC` ne devient ni `24V`, ni `24 volts`, ni `DC 24`."""
    task = _build_targeted_tasks(((_evaluation_xz07(), _identite()),), _requirements())[0]

    for query in task.search_queries:
        normalise = query.casefold()
        assert "24v " not in normalise.replace('"', " ")
        assert "24 volts" not in normalise
        assert "dc 24" not in normalise


def test_the_variant_reference_is_never_invented():
    """La découverte de -11 appartient à SearXNG, jamais au générateur.

    C'est l'interdiction centrale : aucune dérivation par substitution de
    segment, aucune connaissance du schéma de nommage Norel.
    """
    task = _build_targeted_tasks(((_evaluation_xz07(), _identite()),), _requirements())[0]

    for query in task.search_queries:
        assert "XZ07-20-10-11" not in query, query
        # Aucune variante fabriquée du dernier segment.
        residuel = query.replace("XZ07-20-10-13", "")
        for suffixe in ("-11", "-01", "-12", "-10-11"):
            assert suffixe not in residuel, (suffixe, query)


def test_task_carries_a_stable_signature_and_structured_reason():
    task = _build_targeted_tasks(((_evaluation_xz07(), _identite()),), _requirements())[0]

    assert task.signature
    assert task.signature == _build_targeted_tasks(
        ((_evaluation_xz07(), _identite()),), _requirements()
    )[0].signature
    # `reason` explique le déclenchement ; le chemin vit dans son propre champ.
    assert "NEAR_MISS_BLOCKING_CRITERION" in task.reason
    assert "DIRECTIONAL" not in task.reason


def test_no_task_without_a_proven_incompatibility():
    """Un candidat sans fait négatif prouvé n'offre aucune direction."""
    requirements = _requirements()
    candidat = _candidat_xz07()
    # La bobine devient simplement non prouvée : plus aucun bloqueur.
    candidat.criteria[-1].status = "not_proven"
    candidat.criteria[-1].proofs = []

    evaluation = evaluate_candidates(
        requirements,
        [PageAudit(page_url=REVENDEUR_A, candidates=[candidat])],
        target_brand="Norel",
        threshold=75,
        visited_pages=PAGES,
        strict_evidence=False,
    )[0]

    assert _build_targeted_tasks(((evaluation, _identite()),), requirements) == ()


def test_no_task_is_emitted_without_any_candidate():
    assert _build_targeted_tasks((), _requirements()) == ()
