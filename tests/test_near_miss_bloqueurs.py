# -*- coding: utf-8 -*-
"""Seul un écart technique donne une direction de recherche.

Mesure de la mission réelle du 2026-08-19 (`NV1T05BD -> Norel`) : les candidats
étaient écartés en `TOO_MANY_BLOCKERS` avec

    blocking_criteria = ['fabricant', 'reference_exacte', 'famille']

Ces trois « bloqueurs » ne sont pas des caractéristiques : ce sont des
attributs d'identité. Un équivalent en diffère nécessairement — c'est même la
définition de la recherche. Les compter rendait le chemin near-miss
structurellement inatteignable dès qu'on change de marque, et noyait le seul
écart réellement exploitable.

Ils ne sont pas non plus cherchables : `fabricant "Kerion Electric"`
enverrait chercher l'inverse de ce qu'on veut.
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
from near_miss import assess_candidates, build_targeted_tasks


URL = "https://revendeur.example/candidat"


def _requirements() -> RequirementSet:
    """Cahier des charges réel : identité et technique mêlées."""
    return RequirementSet(
        product="contacteur 9 A bobine 24 V DC",
        criteria=[
            {"id": "fabricant", "label": "Fabricant",
             "requested_value": "Kerion Electric", "critical": True},
            {"id": "reference_exacte", "label": "Reference exacte",
             "requested_value": "NV1T05BD", "critical": True},
            {"id": "famille", "label": "Famille",
             "requested_value": "Tersa D", "critical": True},
            {"id": "coil", "label": "Tension de bobine",
             "requested_value": "24 V DC", "critical": True},
            {"id": "courant", "label": "Courant nominal",
             "requested_value": "9 A (AC-3)", "critical": False},
        ],
    )


def _critere(identifiant, demande, observe, statut):
    return CriterionAudit(
        requirement_id=identifiant,
        requested_value=demande,
        observed_value=observe,
        status=statut,
        proofs=(
            [SourceProof(url=URL, excerpt=f"{demande} {observe}",
                         type="web_officiel")]
            if statut in {"proven", "incompatible"} else []
        ),
    )


def _evaluer(criteres):
    contenu = " ".join(
        f"{item.requested_value} {item.observed_value}"
        for item in criteres if item.proofs
    )
    return evaluate_candidates(
        _requirements(),
        [PageAudit(page_url=URL, candidates=[
            CandidateAudit(brand="Norel", reference="XZ07-20-10-13",
                           criteria=criteres)
        ])],
        target_brand="Norel",
        threshold=75,
        visited_pages={URL: contenu or "sans preuve"},
        strict_evidence=False,
    )[0]


def _identite():
    return {"identity": "XZ07-20-10-13", "family": "XZ07"}


def _candidat_norel_reel():
    """Le cas mesuré : identité forcément différente, bobine incompatible."""
    return _evaluer([
        _critere("fabricant", "Kerion Electric", "Norel", "incompatible"),
        _critere("reference_exacte", "NV1T05BD", "XZ07-20-10-13", "incompatible"),
        _critere("famille", "Tersa D", "AF", "incompatible"),
        _critere("coil", "24 V DC", "100...250V AC/DCC", "incompatible"),
        _critere("courant", "9 A (AC-3)", "9 A (AC-3)", "proven"),
    ])


def test_identity_criteria_never_count_as_technical_blockers():
    """Mutation détectée : changer de marque suffit à disqualifier un near-miss."""
    verdict = assess_candidates(
        ((_candidat_norel_reel(), _identite()),), _requirements()
    )[0]

    assert verdict.blocking_criteria == ("coil",)
    assert verdict.eligible is True


def test_the_real_case_becomes_the_expected_directional_near_miss():
    """Le scénario complet : un seul écart technique, donc une direction."""
    tasks = build_targeted_tasks(
        ((_candidat_norel_reel(), _identite()),), _requirements()
    )

    assert len(tasks) == 1
    task = tasks[0]
    assert task.blocking_criterion == "coil"
    assert task.target_value == "24 V DC"
    assert task.observed_value == "100...250V AC/DCC"
    for query in task.search_queries:
        assert "XZ07" in query
        assert '"24 V DC"' in query
        # Jamais la marque d'origine : ce serait chercher l'inverse du besoin.
        assert "Kerion" not in query
        assert "NV1T05BD" not in query


def test_a_candidate_blocked_only_on_identity_gives_no_direction():
    """Sans écart technique, il n'y a rien à chercher autrement."""
    evaluation = _evaluer([
        _critere("fabricant", "Kerion Electric", "Norel", "incompatible"),
        _critere("reference_exacte", "NV1T05BD", "XZ07-20-10-11", "incompatible"),
        _critere("famille", "Tersa D", "AF", "incompatible"),
        _critere("coil", "24 V DC", "24 V DC", "proven"),
        _critere("courant", "9 A (AC-3)", "9 A (AC-3)", "proven"),
    ])

    verdict = assess_candidates(((evaluation, _identite()),), _requirements())[0]

    assert verdict.eligible is False
    assert "NO_PROVEN_INCOMPATIBILITY" in verdict.reason


def test_too_many_technical_blockers_still_disqualifies():
    """L'exclusion vise l'identité, elle n'assouplit pas la règle technique."""
    evaluation = _evaluer([
        _critere("fabricant", "Kerion Electric", "Norel", "incompatible"),
        _critere("reference_exacte", "NV1T05BD", "XZ12-20", "incompatible"),
        _critere("famille", "Tersa D", "AF", "incompatible"),
        _critere("coil", "24 V DC", "100 V", "incompatible"),
        _critere("courant", "9 A (AC-3)", "16 A", "incompatible"),
    ])

    verdict = assess_candidates(((evaluation, _identite()),), _requirements())[0]

    # Deux bloqueurs techniques, zéro compatible prouvé : chemin DIRECTIONAL
    # refusé faute de bloqueur unique.
    assert verdict.eligible is False
    assert "DIRECTIONAL_REQUIRES_SINGLE_BLOCKER" in verdict.reason


@pytest.mark.parametrize("label", [
    "Fabricant", "Marque", "Manufacturer", "Brand",
    "Reference exacte", "Référence", "MPN",
    "Famille", "Gamme", "Serie", "Modele",
])
def test_identity_labels_are_recognised_across_wordings(label):
    from near_miss import _est_critere_identite

    assert _est_critere_identite(label) is True


@pytest.mark.parametrize("label", [
    "Tension de bobine", "Courant nominal", "Nombre de poles",
    "Contacts principaux", "Puissance", "Indice de protection",
])
def test_technical_labels_are_never_treated_as_identity(label):
    from near_miss import _est_critere_identite

    assert _est_critere_identite(label) is False
