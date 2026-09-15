# -*- coding: utf-8 -*-
"""Une citation reconstruite ne doit plus faire perdre une incompatibilité prouvée."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from compatibilite import evaluate_candidates
from modeles import (
    CandidateAudit,
    CriterionAudit,
    PageAudit,
    RequirementSet,
    SourceProof,
)


URL = "https://revendeur.example/xz07-20-10-13"

# La page porte la bobine, mais pas l'agrégat « * 3P * 1 contact ... ».
CONTENU = (
    "Contacteur Norel XZ07-20-10-13. Bobine 100...250V AC/DCC. "
    "Nombre de poles 3 poles. Courant AC-3 9 A."
)

EXTRAIT_RECONSTRUIT = "* 100...250V AC/DCC * 3P * 1 contact auxiliaire N/O * 400 V - CA-3 : 9A / 4kW"


def _requirements() -> RequirementSet:
    return RequirementSet(
        product="contacteur 9 A bobine 24 V DC",
        criteria=[
            {"id": "poles", "label": "Nombre de poles",
             "requested_value": "3P", "critical": False},
            {"id": "coil", "label": "Tension de bobine",
             "requested_value": "24 V DC", "critical": True},
        ],
    )


def _candidat() -> CandidateAudit:
    return CandidateAudit(
        brand="Norel",
        reference="XZ07-20-10-13",
        criteria=[
            # Preuve reconstruite : introuvable telle quelle dans la page.
            CriterionAudit(
                requirement_id="poles",
                requested_value="3P",
                observed_value="3P",
                status="proven",
                proofs=[SourceProof(
                    url=URL, excerpt=EXTRAIT_RECONSTRUIT, type="web_officiel"
                )],
            ),
            # Preuve littérale : la bobine est réellement incompatible.
            CriterionAudit(
                requirement_id="coil",
                requested_value="24 V DC",
                observed_value="100...250V AC/DCC",
                status="incompatible",
                proofs=[SourceProof(
                    url=URL, excerpt="Bobine 100...250V AC/DCC", type="web_officiel"
                )],
            ),
        ],
    )


def _evaluer(*, strict: bool):
    return evaluate_candidates(
        _requirements(),
        [PageAudit(page_url=URL, candidates=[_candidat()])],
        target_brand="Norel",
        threshold=75,
        visited_pages={URL: CONTENU},
        strict_evidence=strict,
    )


def test_reconstructed_excerpt_is_downgraded_not_fatal():
    """Mutation détectée : un extrait non littéral supprime tout le candidat."""
    evaluation = _evaluer(strict=False)[0]

    # Le contrat de preuve reste strict : la citation reconstruite est refusée.
    assert evaluation.summary.unverified_criteria == ["Nombre de poles"]
    assert "Nombre de poles" not in evaluation.summary.proven_criteria


def test_proven_incompatibility_survives_and_rejects_the_candidate():
    """Le fait décisif est conservé : bobine 100-250 V AC/DC vs 24 V DC demandés."""
    evaluation = _evaluer(strict=False)[0]

    assert evaluation.summary.incompatible_criteria == ["Tension de bobine"]
    assert evaluation.summary.critical_blockers == ["Tension de bobine"]
    # Incompatibilité obligatoire prouvée -> rejeté, quel que soit le score.
    assert evaluation.eligible is False
    assert evaluation.complete is False


def test_strict_mode_remains_available_and_unchanged():
    """Le comportement global de `_validate_candidate` n'est pas modifié."""
    import pytest
    from compatibilite import EvidenceContractError

    with pytest.raises(EvidenceContractError):
        _evaluer(strict=True)
