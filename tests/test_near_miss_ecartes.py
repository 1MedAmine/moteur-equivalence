# -*- coding: utf-8 -*-
"""Un near-miss écarté doit dire pourquoi.

Sur la mission réelle du 2026-08-19, trois candidats portaient une
incompatibilité prouvée et aucune tâche n'a été générée. Le diagnostic ne
permettait pas de savoir laquelle des conditions d'éligibilité avait manqué —
un rejet opaque remplaçant l'ancienne panne opaque.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from compatibilite import evaluate_candidates
from modeles import (
    CandidateAudit,
    CriterionAudit,
    PageAudit,
    RequirementSet,
    SourceProof,
)
from near_miss import assess_candidates


URL = "https://revendeur.example/candidat"


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
            {"id": "usage", "label": "Usage vise",
             "requested_value": "Commande de moteur", "critical": True},
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


def test_assessments_are_returned_for_every_candidate():
    """Éligibles comme écartés : tous doivent être visibles."""
    evaluation = _evaluer([
        _critere("poles", "3P", "4P", "incompatible"),
        _critere("courant", "9 A (AC-3)", "12 A", "incompatible"),
        _critere("contacts", "3 NO", "2 NO", "incompatible"),
        _critere("coil", "24 V DC", "100 V", "incompatible"),
        _critere("usage", "Commande de moteur", "Commande de moteur", "proven"),
    ])

    verdicts = assess_candidates(((evaluation, _identite()),), _requirements())

    assert len(verdicts) == 1
    assert verdicts[0].eligible is False
    assert "TOO_MANY_BLOCKERS" in verdicts[0].reason


def test_a_category_blocker_is_named_as_such():
    """« Usage visé » incompatible désigne la mauvaise famille de produit."""
    evaluation = _evaluer([
        _critere("poles", "3P", "3P", "proven"),
        _critere("courant", "9 A (AC-3)", "9 A (AC-3)", "proven"),
        _critere("contacts", "3 NO", "3 NO", "proven"),
        _critere("coil", "24 V DC", "24 V DC", "proven"),
        _critere("usage", "Commande de moteur", "Protection de ligne",
                 "incompatible"),
    ])

    verdicts = assess_candidates(((evaluation, _identite()),), _requirements())

    assert verdicts[0].eligible is False
    assert "WRONG_PRODUCT_CATEGORY" in verdicts[0].reason


def test_an_eligible_candidate_is_reported_as_eligible():
    evaluation = _evaluer([
        _critere("poles", "3P", "3P", "proven"),
        _critere("courant", "9 A (AC-3)", "9 A (AC-3)", "proven"),
        _critere("contacts", "3 NO", "3 NO", "proven"),
        _critere("coil", "24 V DC", "100...250V AC/DCC", "incompatible"),
        _critere("usage", "Commande de moteur", "Commande de moteur", "proven"),
    ])

    verdicts = assess_candidates(((evaluation, _identite()),), _requirements())

    assert verdicts[0].eligible is True
    assert verdicts[0].blocking_criteria == ("coil",)


def test_assessment_diagnostic_is_safe_by_construction():
    evaluation = _evaluer([
        _critere("poles", "3P", "4P", "incompatible"),
        _critere("courant", "9 A (AC-3)", "12 A", "incompatible"),
        _critere("contacts", "3 NO", "2 NO", "incompatible"),
        _critere("coil", "24 V DC", "100 V", "incompatible"),
        _critere("usage", "Commande de moteur", "Commande de moteur", "proven"),
    ])

    diagnostic = assess_candidates(
        ((evaluation, _identite()),), _requirements()
    )[0].as_diagnostic()

    assert set(diagnostic) == {
        "eligible", "reason", "eligibility_path", "identity", "family",
        "proven_compatible_count", "blocking_criteria", "missing_criteria",
    }
