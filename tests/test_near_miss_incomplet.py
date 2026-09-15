# -*- coding: utf-8 -*-
"""Un candidat prometteur mais sous-documente est aussi une direction.

Mission reelle `NV1T05BD -> Norel` du 2026-08-20. Le candidat `4KBL103001R8110`
(BSL07-20-10-81) sort de l'audit avec :

    proven_compatible_count : 4
    blocking_criteria       : []
    reason : NOT_ELIGIBLE_NO_PROVEN_INCOMPATIBILITY

Quatre criteres prouves compatibles, aucun bloqueur — et zero recherche
ciblee, parce que l'eligibilite exigeait une incompatibilite prouvee. Le
candidat n'etait pas ecarte : il etait incomplet, et rien dans la boucle
n'allait chercher ce qui manquait. La mission a fini `NO_PROVABLE_CANDIDATE`
avec douze requetes depensees ailleurs.

Un tel candidat est plus proche du but qu'un near-miss classique : celui-ci
porte une incompatibilite prouvee et ne peut donc pas etre la reponse telle
quelle, alors que celui-la tient deja sur ce qu'on a pu lire. C'est pourquoi
`INCOMPLETE` passe devant.

Les criteres qui portent la direction sont ceux qui manquent de preuve —
`not_proven` (le modele n'a rien etabli) comme `unverified` (le contrat de
preuve a retrograde une citation reconstruite). Les deux disent la meme
chose : la preuve reste a trouver.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modeles import RequirementSet
from near_miss import (
    REASON_INCOMPLETE,
    REASON_NO_INCOMPATIBILITY,
    EligibilityPath,
    assess_near_miss,
    build_targeted_tasks,
    select_targeted_queries,
)
from test_near_miss_eligibilite import _critere, _evaluer, _identite, _requirements


def _incomplet(**kwargs):
    """Trois criteres prouves, aucun incompatible, deux sans preuve."""
    return _evaluer([
        _critere("poles", "3P", "3P", "proven", litteral=True),
        _critere("courant", "9 A (AC-3)", "9 A (AC-3)", "proven", litteral=True),
        _critere("usage", "Contacteur de puissance", "Contacteur de puissance",
                 "proven", litteral=True),
        _critere("contacts", "3 NO", "", "not_proven", litteral=True),
        _critere("coil", "24 V DC", "", "not_proven", litteral=True),
    ], **kwargs)


# --------------------------------------------------------------------------
# Le chemin


def test_eligibility_path_expose_desormais_trois_chemins():
    assert EligibilityPath.INCOMPLETE.value == "INCOMPLETE"
    assert len(list(EligibilityPath)) == 3


def test_un_candidat_sans_bloqueur_mais_sous_documente_devient_une_direction():
    """Mutation détectée : le cas mesuré sur la mission ne produit rien."""
    assessment = assess_near_miss(_incomplet(), _requirements(), _identite())

    assert assessment.eligible
    assert assessment.path is EligibilityPath.INCOMPLETE
    assert REASON_INCOMPLETE in assessment.reason
    # La direction nomme les criteres a prouver, pas un texte libre du modele.
    assert set(assessment.missing_criteria) == {"contacts", "coil"}
    assert "coil" in assessment.reason
    # Le chemin vit dans son propre champ, jamais dans `reason`.
    assert "INCOMPLETE" not in assessment.reason


def test_un_candidat_entierement_prouve_ne_relance_aucune_recherche():
    """Rien ne manque : depenser du budget dessus serait du gaspillage."""
    evaluation = _evaluer([
        _critere("poles", "3P", "3P", "proven", litteral=True),
        _critere("courant", "9 A (AC-3)", "9 A (AC-3)", "proven", litteral=True),
        _critere("contacts", "3 NO", "3 NO", "proven", litteral=True),
        _critere("coil", "24 V DC", "24 V DC", "proven", litteral=True),
        _critere("usage", "Contacteur de puissance", "Contacteur de puissance",
                 "proven", litteral=True),
    ])

    assessment = assess_near_miss(evaluation, _requirements(), _identite())

    assert not assessment.eligible
    assert assessment.reason == REASON_NO_INCOMPATIBILITY


def test_un_candidat_a_peine_etaye_ne_merite_pas_le_budget():
    """Sans plusieurs compatibles prouves, rien n'atteste la proximite."""
    evaluation = _evaluer([
        _critere("poles", "3P", "3P", "proven", litteral=True),
        _critere("courant", "9 A (AC-3)", "", "not_proven", litteral=True),
        _critere("contacts", "3 NO", "", "not_proven", litteral=True),
        _critere("coil", "24 V DC", "", "not_proven", litteral=True),
        _critere("usage", "Contacteur de puissance", "", "not_proven", litteral=True),
    ])

    assessment = assess_near_miss(evaluation, _requirements(), _identite())

    assert not assessment.eligible
    assert assessment.reason == REASON_NO_INCOMPATIBILITY


def test_un_critere_d_identite_manquant_ne_donne_pas_de_direction():
    """Chercher la marque d'origine enverrait chercher l'inverse du besoin."""
    requirements = RequirementSet(
        product="contacteur 9 A bobine 24 V DC",
        criteria=[
            {"id": "poles", "label": "Nombre de poles",
             "requested_value": "3P", "critical": False},
            {"id": "courant", "label": "Courant nominal",
             "requested_value": "9 A (AC-3)", "critical": False},
            {"id": "marque", "label": "Fabricant",
             "requested_value": "Kerion", "critical": False},
        ],
    )
    evaluation = _evaluer([
        _critere("poles", "3P", "3P", "proven", litteral=True),
        _critere("courant", "9 A (AC-3)", "9 A (AC-3)", "proven", litteral=True),
        _critere("marque", "Kerion", "", "not_proven", litteral=True),
    ], requirements=requirements)

    assessment = assess_near_miss(evaluation, requirements, _identite())

    assert not assessment.eligible
    assert assessment.reason == REASON_NO_INCOMPATIBILITY


def test_sans_identite_ni_famille_la_requete_n_a_rien_a_ancrer():
    assessment = assess_near_miss(
        _incomplet(), _requirements(), {"identity": "", "family": ""}
    )

    assert not assessment.eligible
    assert "NO_CREDIBLE_IDENTITY" in assessment.reason


# --------------------------------------------------------------------------
# Les taches produites


def test_une_tache_par_critere_manquant_avec_sa_valeur_cible():
    taches = build_targeted_tasks(
        [(_incomplet(), _identite())], _requirements()
    )

    par_critere = {tache.blocking_criterion: tache for tache in taches}
    assert set(par_critere) == {"contacts", "coil"}

    bobine = par_critere["coil"]
    assert bobine.eligibility_path == "INCOMPLETE"
    assert bobine.target_value == "24 V DC"
    # La valeur cible est citee telle qu'ecrite au cahier des charges.
    assert all('"24 V DC"' in requete for requete in bobine.search_queries)
    # L'ancre est la famille observee, jamais une reference fabriquee.
    assert all(requete.startswith("XZ07") for requete in bobine.search_queries)


def test_completer_un_candidat_qui_tient_passe_avant_un_near_miss_classique():
    """`INCOMPLETE` tient deja sur ce qui est lu ; `ESTABLISHED` porte un ecart prouve."""
    ecart_prouve = _evaluer([
        _critere("poles", "3P", "3P", "proven", litteral=True),
        _critere("courant", "9 A (AC-3)", "9 A (AC-3)", "proven", litteral=True),
        _critere("contacts", "3 NO", "3 NO", "proven", litteral=True),
        _critere("usage", "Contacteur de puissance", "Contacteur de puissance",
                 "proven", litteral=True),
        _critere("coil", "24 V DC", "100...250V AC/DC", "incompatible", litteral=True),
    ], reference="XZ07-20-10-14")

    taches = build_targeted_tasks(
        [(ecart_prouve, _identite(reference="XZ07-20-10-14")),
         (_incomplet(), _identite())],
        _requirements(),
    )

    assert taches[0].eligibility_path == "INCOMPLETE"


def test_la_place_reservee_au_directionnel_survit_au_troisieme_chemin():
    """Sans reservation, `DIRECTIONAL` disparaitrait derriere les deux autres."""
    directionnel = _evaluer([
        _critere("poles", "3P", "", "not_proven", litteral=True),
        _critere("courant", "9 A (AC-3)", "", "not_proven", litteral=True),
        _critere("contacts", "3 NO", "", "not_proven", litteral=True),
        _critere("usage", "Contacteur de puissance", "", "not_proven", litteral=True),
        _critere("coil", "24 V DC", "100...250V AC/DC", "incompatible", litteral=True),
    ], reference="XZ12-20-10-13")

    taches = build_targeted_tasks(
        [(_incomplet(), _identite()),
         (directionnel, _identite(reference="XZ12-20-10-13", famille="XZ12"))],
        _requirements(),
    )
    selection = select_targeted_queries(taches, remaining_logical_queries=2)

    chemins = {tache.eligibility_path for tache in selection.selected}
    assert "DIRECTIONAL" in chemins


# --------------------------------------------------------------------------
# Diagnostic


def test_le_diagnostic_nomme_les_criteres_manquants():
    """Sans eux, un `INCOMPLETE` serait aussi opaque que le rejet qu'il remplace."""
    projection = assess_near_miss(
        _incomplet(), _requirements(), _identite()
    ).as_diagnostic()

    assert set(projection["missing_criteria"]) == {"contacts", "coil"}
    assert projection["eligibility_path"] == "INCOMPLETE"
    # Aucune fuite de contenu brut ni d'objet candidat complet.
    interdits = {"page_content", "raw_model_response", "candidate", "proofs"}
    assert interdits.isdisjoint(projection)
