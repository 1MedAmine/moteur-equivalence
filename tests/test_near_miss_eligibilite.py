# -*- coding: utf-8 -*-
"""Éligibilité near-miss : quel candidat rejeté mérite une recherche ciblée.

Périmètre strict : les contrats et la décision d'éligibilité. La composition
des requêtes, la priorité, la réservation de budget et la déduplication
appartiennent aux tâches suivantes.
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
from near_miss import (
    EligibilityPath,
    NearMissAssessment,
    assess_near_miss,
)


URL = "https://revendeur.example/candidat"

# Un extrait introuvable dans la page : le contrat de preuve le rétrograde.
AGREGAT = "* agregat reconstruit par le modele * introuvable tel quel *"


def _requirements(*, avec_categorie: bool = True) -> RequirementSet:
    criteres = [
        {"id": "poles", "label": "Nombre de poles",
         "requested_value": "3P", "critical": False},
        {"id": "courant", "label": "Courant nominal",
         "requested_value": "9 A (AC-3)", "critical": False},
        {"id": "contacts", "label": "Contacts principaux",
         "requested_value": "3 NO", "critical": False},
        {"id": "coil", "label": "Tension de bobine",
         "requested_value": "24 V DC", "critical": True},
    ]
    if avec_categorie:
        criteres.append({
            "id": "usage", "label": "Type de produit",
            "requested_value": "Contacteur de puissance", "critical": True,
        })
    return RequirementSet(product="contacteur 9 A bobine 24 V DC", criteria=criteres)


def _critere(identifiant, demande, observe, statut, *, litteral: bool):
    """`litteral=False` produit une preuve que le contrat refusera."""
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


def _evaluer(criteres, *, requirements=None, brand="Norel", reference="XZ07-20-10-13"):
    requirements = requirements or _requirements()
    contenu = " ".join(
        f"{item.requested_value} {item.observed_value}"
        for item in criteres
        if item.proofs and item.proofs[0].excerpt != AGREGAT
    )
    return evaluate_candidates(
        requirements,
        [PageAudit(page_url=URL, candidates=[
            CandidateAudit(brand=brand, reference=reference, criteria=criteres)
        ])],
        target_brand="Norel",
        threshold=75,
        visited_pages={URL: contenu or "contenu sans preuve"},
        strict_evidence=False,
    )[0]


def _identite(reference="XZ07-20-10-13", famille="XZ07"):
    return {"identity": reference, "family": famille}


# --------------------------------------------------------------------------
# Contrats
# --------------------------------------------------------------------------

def test_eligibility_path_exposes_exactly_three_values():
    assert EligibilityPath.ESTABLISHED.value == "ESTABLISHED"
    assert EligibilityPath.DIRECTIONAL.value == "DIRECTIONAL"
    assert EligibilityPath.INCOMPLETE.value == "INCOMPLETE"
    assert len(list(EligibilityPath)) == 3


def test_reason_is_structured_and_separate_from_the_path():
    """Mutation détectée : `reason` se contente de répéter le chemin."""
    evaluation = _evaluer([
        _critere("poles", "3P", "3P", "proven", litteral=True),
        _critere("courant", "9 A (AC-3)", "9 A (AC-3)", "proven", litteral=True),
        _critere("contacts", "3 NO", "3 NO", "not_proven", litteral=True),
        _critere("coil", "24 V DC", "100...250V AC/DCC", "incompatible", litteral=True),
        _critere("usage", "Contacteur de puissance", "Contacteur de puissance",
                 "proven", litteral=True),
    ])

    assessment = assess_near_miss(evaluation, _requirements(), _identite())

    assert assessment.eligible
    assert assessment.path is EligibilityPath.ESTABLISHED
    assert "NEAR_MISS_BLOCKING_CRITERION" in assessment.reason
    assert "coil" in assessment.reason
    # Le chemin vit dans son propre champ, jamais dans `reason`.
    assert "ESTABLISHED" not in assessment.reason
    assert isinstance(assessment, NearMissAssessment)


# --------------------------------------------------------------------------
# Chemin ESTABLISHED
# --------------------------------------------------------------------------

def test_established_needs_several_proven_compatible_criteria():
    evaluation = _evaluer([
        _critere("poles", "3P", "3P", "proven", litteral=True),
        _critere("courant", "9 A (AC-3)", "9 A (AC-3)", "proven", litteral=True),
        _critere("contacts", "3 NO", "3 NO", "not_proven", litteral=True),
        _critere("coil", "24 V DC", "100...250V AC/DCC", "incompatible", litteral=True),
        _critere("usage", "Contacteur de puissance", "Contacteur de puissance",
                 "proven", litteral=True),
    ])

    assessment = assess_near_miss(evaluation, _requirements(), _identite())

    assert assessment.path is EligibilityPath.ESTABLISHED
    assert assessment.proven_compatible_count >= 2
    assert assessment.blocking_criteria == ("coil",)


def test_established_accepts_two_blockers():
    evaluation = _evaluer([
        _critere("poles", "3P", "3P", "proven", litteral=True),
        _critere("courant", "9 A (AC-3)", "12 A", "incompatible", litteral=True),
        _critere("contacts", "3 NO", "3 NO", "proven", litteral=True),
        _critere("coil", "24 V DC", "100...250V AC/DCC", "incompatible", litteral=True),
        _critere("usage", "Contacteur de puissance", "Contacteur de puissance",
                 "proven", litteral=True),
    ])

    assessment = assess_near_miss(evaluation, _requirements(), _identite())

    assert assessment.eligible
    assert assessment.path is EligibilityPath.ESTABLISHED
    assert len(assessment.blocking_criteria) == 2


def test_three_blockers_are_never_eligible():
    """Mutation détectée : un candidat qui échoue partout reste un near-miss."""
    evaluation = _evaluer([
        _critere("poles", "3P", "4P", "incompatible", litteral=True),
        _critere("courant", "9 A (AC-3)", "12 A", "incompatible", litteral=True),
        _critere("contacts", "3 NO", "3 NO", "proven", litteral=True),
        _critere("coil", "24 V DC", "100...250V AC/DCC", "incompatible", litteral=True),
        _critere("usage", "Contacteur de puissance", "Contacteur de puissance",
                 "proven", litteral=True),
    ])

    assessment = assess_near_miss(evaluation, _requirements(), _identite())

    assert not assessment.eligible
    assert assessment.path is None
    assert "TOO_MANY_BLOCKERS" in assessment.reason


# --------------------------------------------------------------------------
# Chemin DIRECTIONAL
# --------------------------------------------------------------------------

def test_directional_accepts_zero_proven_compatible_with_one_blocker():
    """Le cas XZ07 : toutes les preuves de compatibilité rétrogradées."""
    evaluation = _evaluer([
        _critere("poles", "3P", "3P", "proven", litteral=False),
        _critere("courant", "9 A (AC-3)", "9 A (AC-3)", "proven", litteral=False),
        _critere("contacts", "3 NO", "3 NO", "proven", litteral=False),
        _critere("coil", "24 V DC", "100...250V AC/DCC", "incompatible", litteral=True),
        _critere("usage", "Contacteur de puissance", "Contacteur de puissance",
                 "proven", litteral=False),
    ])

    assessment = assess_near_miss(evaluation, _requirements(), _identite())

    assert assessment.eligible
    assert assessment.path is EligibilityPath.DIRECTIONAL
    assert assessment.proven_compatible_count == 0
    assert assessment.blocking_criteria == ("coil",)


def test_directional_refuses_two_blockers_without_any_compatible():
    """Sans critère compatible pour attester la proximité, deux bloqueurs = inadapté."""
    evaluation = _evaluer([
        _critere("poles", "3P", "3P", "proven", litteral=False),
        _critere("courant", "9 A (AC-3)", "12 A", "incompatible", litteral=True),
        _critere("contacts", "3 NO", "3 NO", "proven", litteral=False),
        _critere("coil", "24 V DC", "100...250V AC/DCC", "incompatible", litteral=True),
        _critere("usage", "Contacteur de puissance", "Contacteur de puissance",
                 "proven", litteral=False),
    ])

    assessment = assess_near_miss(evaluation, _requirements(), _identite())

    assert not assessment.eligible
    assert assessment.path is None
    assert "DIRECTIONAL_REQUIRES_SINGLE_BLOCKER" in assessment.reason


def test_one_compatible_and_one_blocker_is_eligible():
    evaluation = _evaluer([
        _critere("poles", "3P", "3P", "proven", litteral=True),
        _critere("courant", "9 A (AC-3)", "9 A (AC-3)", "proven", litteral=False),
        _critere("contacts", "3 NO", "3 NO", "proven", litteral=False),
        _critere("coil", "24 V DC", "100...250V AC/DCC", "incompatible", litteral=True),
        _critere("usage", "Contacteur de puissance", "Contacteur de puissance",
                 "proven", litteral=False),
    ])

    assessment = assess_near_miss(evaluation, _requirements(), _identite())

    assert assessment.eligible
    assert assessment.proven_compatible_count == 1


# --------------------------------------------------------------------------
# Rétrogradation
# --------------------------------------------------------------------------

def test_unverified_criteria_never_count_as_compatible():
    """Mutation détectée : une citation refusée compte comme preuve de compatibilité."""
    evaluation = _evaluer([
        _critere("poles", "3P", "3P", "proven", litteral=False),
        _critere("courant", "9 A (AC-3)", "9 A (AC-3)", "proven", litteral=False),
        _critere("contacts", "3 NO", "3 NO", "proven", litteral=False),
        _critere("coil", "24 V DC", "100...250V AC/DCC", "incompatible", litteral=True),
        _critere("usage", "Contacteur de puissance", "Contacteur de puissance",
                 "proven", litteral=False),
    ])

    assessment = assess_near_miss(evaluation, _requirements(), _identite())

    # Quatre critères annoncés `proven`, aucun retenu comme compatible.
    assert assessment.proven_compatible_count == 0
    assert assessment.path is EligibilityPath.DIRECTIONAL


# --------------------------------------------------------------------------
# Conditions communes
# --------------------------------------------------------------------------

def test_a_missing_positive_is_not_a_proven_incompatibility():
    """L'absence de preuve n'ouvre pas le chemin des ecarts prouves.

    Depuis le 2026-08-20 un tel candidat n'est plus ecarte — il emprunte
    `INCOMPLETE`, ou la direction vient des criteres a prouver. Mais il ne doit
    jamais passer pour un near-miss : aucun ecart n'a ete constate sur lui.
    """
    evaluation = _evaluer([
        _critere("poles", "3P", "3P", "proven", litteral=True),
        _critere("courant", "9 A (AC-3)", "9 A (AC-3)", "proven", litteral=True),
        _critere("contacts", "3 NO", "", "not_proven", litteral=True),
        _critere("coil", "24 V DC", "", "not_proven", litteral=True),
        _critere("usage", "Contacteur de puissance", "Contacteur de puissance",
                 "proven", litteral=True),
    ])

    assessment = assess_near_miss(evaluation, _requirements(), _identite())

    assert assessment.path is not EligibilityPath.ESTABLISHED
    assert "NEAR_MISS_BLOCKING_CRITERION" not in assessment.reason
    assert assessment.blocking_criteria == ()


def test_identity_must_be_credible():
    evaluation = _evaluer([
        _critere("poles", "3P", "3P", "proven", litteral=True),
        _critere("courant", "9 A (AC-3)", "9 A (AC-3)", "proven", litteral=True),
        _critere("contacts", "3 NO", "3 NO", "proven", litteral=True),
        _critere("coil", "24 V DC", "100...250V AC/DCC", "incompatible", litteral=True),
        _critere("usage", "Contacteur de puissance", "Contacteur de puissance",
                 "proven", litteral=True),
    ])

    sans_identite = assess_near_miss(
        evaluation, _requirements(), {"identity": "", "family": ""}
    )

    assert not sans_identite.eligible
    assert "NO_CREDIBLE_IDENTITY" in sans_identite.reason


def test_family_alone_is_a_credible_identity():
    """La famille suffit : c'est elle qui portera la recherche."""
    evaluation = _evaluer([
        _critere("poles", "3P", "3P", "proven", litteral=True),
        _critere("courant", "9 A (AC-3)", "9 A (AC-3)", "proven", litteral=True),
        _critere("contacts", "3 NO", "3 NO", "proven", litteral=True),
        _critere("coil", "24 V DC", "100...250V AC/DCC", "incompatible", litteral=True),
        _critere("usage", "Contacteur de puissance", "Contacteur de puissance",
                 "proven", litteral=True),
    ])

    assessment = assess_near_miss(
        evaluation, _requirements(), {"identity": "", "family": "XZ07"}
    )

    assert assessment.eligible


def test_blocker_without_a_target_value_is_rejected():
    """Sans valeur cible, la requête n'aurait rien à chercher."""
    requirements = RequirementSet(
        product="contacteur",
        criteria=[
            {"id": "poles", "label": "Nombre de poles",
             "requested_value": "3P", "critical": False},
            {"id": "courant", "label": "Courant nominal",
             "requested_value": "9 A (AC-3)", "critical": False},
            {"id": "coil", "label": "Tension de bobine",
             "requested_value": "24 V DC", "critical": True},
        ],
    )
    evaluation = _evaluer(
        [
            _critere("poles", "3P", "3P", "proven", litteral=True),
            _critere("courant", "9 A (AC-3)", "9 A (AC-3)", "proven", litteral=True),
            _critere("coil", "24 V DC", "100...250V AC/DCC", "incompatible",
                     litteral=True),
        ],
        requirements=requirements,
    )

    # La valeur cible est retirée du cahier des charges après l'audit.
    ampute = requirements.model_copy(deep=True)
    ampute.criteria[-1].requested_value = "   "

    assessment = assess_near_miss(evaluation, ampute, _identite())

    assert not assessment.eligible
    assert "NO_TARGET_VALUE" in assessment.reason


def test_wrong_product_category_is_rejected():
    """Un disjoncteur pour un besoin de contacteur : mauvaise famille, pas variante."""
    evaluation = _evaluer([
        _critere("poles", "3P", "3P", "proven", litteral=True),
        _critere("courant", "9 A (AC-3)", "9 A (AC-3)", "proven", litteral=True),
        _critere("contacts", "3 NO", "3 NO", "proven", litteral=True),
        _critere("coil", "24 V DC", "100...250V AC/DCC", "incompatible", litteral=True),
        _critere("usage", "Contacteur de puissance", "Disjoncteur modulaire",
                 "incompatible", litteral=True),
    ])

    assessment = assess_near_miss(evaluation, _requirements(), _identite())

    assert not assessment.eligible
    assert "WRONG_PRODUCT_CATEGORY" in assessment.reason


def test_category_detection_uses_no_hardcoded_list():
    """La règle vaut pour tout domaine : ici des vannes, pas des contacteurs."""
    requirements = RequirementSet(
        product="vanne pneumatique",
        criteria=[
            {"id": "diametre", "label": "Diametre",
             "requested_value": "DN50", "critical": False},
            {"id": "pression", "label": "Pression",
             "requested_value": "16 bar", "critical": False},
            {"id": "usage", "label": "Type de produit",
             "requested_value": "Vanne papillon", "critical": True},
        ],
    )
    evaluation = _evaluer(
        [
            _critere("diametre", "DN50", "DN50", "proven", litteral=True),
            _critere("pression", "16 bar", "10 bar", "incompatible", litteral=True),
            _critere("usage", "Vanne papillon", "Pompe centrifuge", "incompatible",
                     litteral=True),
        ],
        requirements=requirements,
        reference="XZ-100",
    )

    assessment = assess_near_miss(
        evaluation, requirements, {"identity": "XZ-100", "family": "XZ"}
    )

    assert not assessment.eligible
    assert "WRONG_PRODUCT_CATEGORY" in assessment.reason
