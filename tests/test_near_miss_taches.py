# -*- coding: utf-8 -*-
"""Composition des tâches ciblées : requêtes, signature, priorité.

Périmètre strict : produire les tâches et les ordonner. La réservation de
budget, la consommation des `logical_queries` et le câblage dans la boucle
adaptative appartiennent aux tâches suivantes.
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
from near_miss import build_targeted_tasks, task_signature


URL = "https://revendeur.example/candidat"
AGREGAT = "* agregat reconstruit * introuvable tel quel *"


def _requirements() -> RequirementSet:
    return RequirementSet(
        product="contacteur 9 A bobine 24 V DC",
        criteria=[
            {"id": "poles", "label": "Nombre de poles",
             "requested_value": "3P", "critical": False},
            {"id": "courant", "label": "Courant nominal",
             "requested_value": "9 A (AC-3)", "critical": False},
            {"id": "contacts", "label": "Contacts principaux",
             "requested_value": "3 NO", "critical": False},
            {"id": "coil", "label": "Tension de bobine",
             "requested_value": "24 V DC", "critical": True},
        ],
    )


def _critere(identifiant, demande, observe, statut, *, litteral=True):
    extrait = f"{demande} {observe}" if litteral else AGREGAT
    return CriterionAudit(
        requirement_id=identifiant,
        requested_value=demande,
        observed_value=observe,
        status=statut,
        proofs=(
            [SourceProof(url=URL, excerpt=extrait, type="web_officiel")]
            if statut in {"proven", "incompatible"} else []
        ),
    )


def _evaluer(criteres, *, reference="XZ07-20-10-13", requirements=None):
    requirements = requirements or _requirements()
    contenu = " ".join(
        f"{item.requested_value} {item.observed_value}"
        for item in criteres
        if item.proofs and item.proofs[0].excerpt != AGREGAT
    )
    return evaluate_candidates(
        requirements,
        [PageAudit(page_url=URL, candidates=[
            CandidateAudit(brand="Norel", reference=reference, criteria=criteres)
        ])],
        target_brand="Norel",
        threshold=75,
        visited_pages={URL: contenu or "sans preuve"},
        strict_evidence=False,
    )[0]


def _identite(reference="XZ07-20-10-13", famille="XZ07"):
    return {"identity": reference, "family": famille}


def _deux_bloqueurs(reference="XZ07-20-10-13"):
    """Chemin ESTABLISHED : deux compatibles prouvés, deux bloqueurs prouvés."""
    return _evaluer([
        _critere("poles", "3P", "3P", "proven"),
        _critere("contacts", "3 NO", "3 NO", "proven"),
        _critere("courant", "9 A (AC-3)", "12 A", "incompatible"),
        _critere("coil", "24 V DC", "100...250V AC/DCC", "incompatible"),
    ], reference=reference)


# --------------------------------------------------------------------------
# Une tâche par bloqueur
# --------------------------------------------------------------------------

def test_two_blockers_produce_two_distinct_tasks():
    """Mutation détectée : les bloqueurs sont fusionnés en une requête confuse."""
    tasks = build_targeted_tasks(
        ((_deux_bloqueurs(), _identite()),), _requirements()
    )

    assert len(tasks) == 2
    assert {task.blocking_criterion for task in tasks} == {"courant", "coil"}


def test_each_task_keeps_its_own_target_and_observed_values():
    tasks = build_targeted_tasks(
        ((_deux_bloqueurs(), _identite()),), _requirements()
    )
    par_critere = {task.blocking_criterion: task for task in tasks}

    assert par_critere["coil"].target_value == "24 V DC"
    assert par_critere["coil"].observed_value == "100...250V AC/DCC"
    assert par_critere["courant"].target_value == "9 A (AC-3)"
    assert par_critere["courant"].observed_value == "12 A"


def test_each_task_query_targets_only_its_own_criterion():
    """Une requête sur la bobine ne doit pas partir chercher le courant."""
    tasks = build_targeted_tasks(
        ((_deux_bloqueurs(), _identite()),), _requirements()
    )
    par_critere = {task.blocking_criterion: task for task in tasks}

    for query in par_critere["coil"].search_queries:
        assert '"24 V DC"' in query
        assert '"9 A (AC-3)"' not in query


# --------------------------------------------------------------------------
# Signature
# --------------------------------------------------------------------------

def test_signature_is_stable_for_identical_data():
    premiere = build_targeted_tasks(((_deux_bloqueurs(), _identite()),), _requirements())
    seconde = build_targeted_tasks(((_deux_bloqueurs(), _identite()),), _requirements())

    assert [task.signature for task in premiere] == [task.signature for task in seconde]


@pytest.mark.parametrize("famille, critere, cible", [
    ("XZ12", "coil", "24 V DC"),
    ("XZ07", "courant", "24 V DC"),
    ("XZ07", "coil", "48 V DC"),
])
def test_signature_changes_with_family_criterion_or_target(famille, critere, cible):
    reference = task_signature("XZ07", "coil", "24 V DC")

    assert task_signature(famille, critere, cible) != reference


def test_signature_ignores_case_and_separators_but_not_meaning():
    """La normalisation sert la déduplication, jamais les requêtes émises."""
    assert task_signature("XZ07", "coil", "24 V DC") == task_signature(
        "xz07", "coil", "24  v  dc"
    )
    assert task_signature("XZ07", "coil", "24 V DC") != task_signature(
        "XZ07", "coil", "24 V AC"
    )


def test_signature_never_embeds_a_derived_reference():
    """Mutation détectée : une variante fabriquée entre dans la clé."""
    tasks = build_targeted_tasks(((_deux_bloqueurs(), _identite()),), _requirements())

    for task in tasks:
        assert "-11" not in task.signature
        assert "XZ07-20-10-13" not in task.signature or "XZ07" in task.signature


def test_two_candidates_of_the_same_family_share_one_signature():
    """Deux variantes fautives sur le même critère appellent la même recherche."""
    premiere = build_targeted_tasks(
        ((_deux_bloqueurs("XZ07-20-10-13"), _identite("XZ07-20-10-13")),),
        _requirements(),
    )
    seconde = build_targeted_tasks(
        ((_deux_bloqueurs("XZ07-20-10-14"), _identite("XZ07-20-10-14")),),
        _requirements(),
    )

    signatures_premiere = {task.signature for task in premiere}
    signatures_seconde = {task.signature for task in seconde}

    assert signatures_premiere == signatures_seconde


# --------------------------------------------------------------------------
# Priorité
# --------------------------------------------------------------------------

def _un_bloqueur_sans_compatible(reference="ZX-99"):
    """Chemin DIRECTIONAL : toutes les compatibilités rétrogradées."""
    return _evaluer([
        _critere("poles", "3P", "3P", "proven", litteral=False),
        _critere("contacts", "3 NO", "3 NO", "proven", litteral=False),
        _critere("courant", "9 A (AC-3)", "9 A (AC-3)", "proven", litteral=False),
        _critere("coil", "24 V DC", "100...250V AC/DCC", "incompatible"),
    ], reference=reference)


def _un_bloqueur_deux_compatibles(reference="YW-50"):
    return _evaluer([
        _critere("poles", "3P", "3P", "proven"),
        _critere("contacts", "3 NO", "3 NO", "proven"),
        _critere("courant", "9 A (AC-3)", "9 A (AC-3)", "proven", litteral=False),
        _critere("coil", "24 V DC", "100...250V AC/DCC", "incompatible"),
    ], reference=reference)


def test_more_proven_compatible_criteria_come_first():
    """Mutation détectée : un near-miss faible passe devant un near-miss établi."""
    tasks = build_targeted_tasks(
        (
            (_un_bloqueur_sans_compatible(), _identite("ZX-99", "ZX")),
            (_un_bloqueur_deux_compatibles(), _identite("YW-50", "YW")),
        ),
        _requirements(),
    )

    assert [task.eligibility_path for task in tasks] == ["ESTABLISHED", "DIRECTIONAL"]
    assert tasks[0].proven_compatible_count > tasks[1].proven_compatible_count


def test_fewer_blockers_come_first_at_equal_compatibility():
    tasks = build_targeted_tasks(
        (
            (_deux_bloqueurs("AA-1"), _identite("AA-1", "AA")),
            (_un_bloqueur_deux_compatibles("BB-2"), _identite("BB-2", "BB")),
        ),
        _requirements(),
    )

    # Même nombre de compatibles prouvés : le moins de bloqueurs passe devant.
    assert tasks[0].family == "BB"


def test_ordering_does_not_depend_on_input_order():
    """Deux exécutions, ordres d'entrée opposés, même sortie."""
    a = (_un_bloqueur_sans_compatible(), _identite("ZX-99", "ZX"))
    b = (_un_bloqueur_deux_compatibles(), _identite("YW-50", "YW"))

    direct = build_targeted_tasks((a, b), _requirements())
    inverse = build_targeted_tasks((b, a), _requirements())

    assert [task.signature for task in direct] == [task.signature for task in inverse]


def test_priority_is_a_dense_deterministic_rank():
    tasks = build_targeted_tasks(
        (
            (_un_bloqueur_sans_compatible(), _identite("ZX-99", "ZX")),
            (_un_bloqueur_deux_compatibles(), _identite("YW-50", "YW")),
        ),
        _requirements(),
    )

    assert [task.priority for task in tasks] == list(range(len(tasks)))


def test_ineligible_candidates_produce_no_task():
    """Trois bloqueurs : écarté à l'éligibilité, aucune tâche composée."""
    evaluation = _evaluer([
        _critere("poles", "3P", "4P", "incompatible"),
        _critere("contacts", "3 NO", "2 NO", "incompatible"),
        _critere("courant", "9 A (AC-3)", "9 A (AC-3)", "proven"),
        _critere("coil", "24 V DC", "100...250V AC/DCC", "incompatible"),
    ])

    assert build_targeted_tasks(((evaluation, _identite()),), _requirements()) == ()
