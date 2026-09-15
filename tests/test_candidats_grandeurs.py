# -*- coding: utf-8 -*-
"""Une grandeur physique n'est pas une reference produit.

Mission reelle `NV1T05BD -> Norel` du 2026-08-20. Les quatre pistes retenues
etaient :

    00026001627   numero d'article d'un distributeur
    245921        idem
    24V           une tension
    100Hz         une frequence

Neuf des douze requetes sont parties chercher `Norel 00026001627 ...`, une
chaine qu'Norel n'a jamais publiee. Le moteur a donc ramene n'importe quoi —
manuels de nettoyeur vapeur, blog Excel, club de football anglais — et la
mission a fini `NO_PROVABLE_CANDIDATE`.

La seule garde existante etait `len(reference) <= 3`, qui laisse passer
`100Hz` comme `00026001627`.

Deux regles distinctes, deux traitements distincts :

- une grandeur physique — un nombre suivi d'une unite — n'est jamais une
  reference produit. Elle est rejetee.
- une suite purement numerique peut etre une reference de catalogue
  legitime (Dorval publie `512345`) comme un SKU de distributeur. Elle est
  gardee mais retrogradee, pour passer derriere toute reference
  alphanumerique.

Le vocabulaire d'unites est celui du SI et de l'electrotechnique. Aucune
regle propre a un fabricant, aucun catalogue.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from candidats import (
    build_discovery_document,
    est_grandeur_physique,
    reference_est_purement_numerique,
)


# --------------------------------------------------------------------------
# Grandeurs physiques


@pytest.mark.parametrize("valeur", [
    "24V", "24 V", "100Hz", "9A", "4kW", "690V", "50 Hz", "1,5 mm",
    "24V DC", "230 V AC", "9 A", "0.75kW", "16A",
])
def test_une_grandeur_physique_n_est_pas_une_reference(valeur):
    """Mutation détectée : une tension devient un candidat produit."""
    assert est_grandeur_physique(valeur)


@pytest.mark.parametrize("valeur", [
    "XZ07-20-10-11",
    "4KBL137001R1110",
    "K7C48208",
    "NV1T05BD",
    "ZX-41-7",
    "XZ07Z-20-10-21",
    "7RT2016-1BB41",
])
def test_une_vraie_reference_n_est_jamais_prise_pour_une_grandeur(valeur):
    assert not est_grandeur_physique(valeur)


@pytest.mark.parametrize("valeur", ["", "   ", "Norel", "contacteur"])
def test_ni_le_vide_ni_un_mot_ne_sont_des_grandeurs(valeur):
    assert not est_grandeur_physique(valeur)


def test_un_nombre_suivi_d_un_suffixe_inconnu_reste_une_reference_possible():
    """Sans unite reconnue, rien ne permet d'affirmer que c'est une grandeur."""
    assert not est_grandeur_physique("3PH4")
    assert not est_grandeur_physique("12XY")


# --------------------------------------------------------------------------
# Suites purement numeriques


@pytest.mark.parametrize("valeur", ["00026001627", "245921", "512345"])
def test_une_suite_purement_numerique_est_signalee(valeur):
    assert reference_est_purement_numerique(valeur)


@pytest.mark.parametrize("valeur", ["XZ07-20-10-11", "4KBL137001R1110", "24V"])
def test_une_reference_alphanumerique_ne_l_est_pas(valeur):
    assert not reference_est_purement_numerique(valeur)


# --------------------------------------------------------------------------
# Effet sur l'ingestion


def _ingest(reference: str, contenu: str):
    from candidats import CandidateProposal, CandidateRegistry
    from modeles import RequirementSet

    return CandidateRegistry().ingest(
        [CandidateProposal(brand="Norel", reference=reference)],
        build_discovery_document(
            url="https://revendeur.example/contacteur-norel",
            title="Contacteur Norel",
            snippets=(),
            content=contenu,
            rank=1,
        ),
        RequirementSet(
            product="contacteur 9 A bobine 24 V DC",
            criteria=[{"id": "coil", "label": "Tension de bobine",
                       "requested_value": "24 V DC", "critical": True}],
        ),
        target_brand="Norel",
        allow_deterministic_fallback=False,
    )


def test_une_tension_lue_sur_la_page_est_rejetee_et_nommee():
    """Mutation détectée : `Norel 24V` part comme requête de recherche produit."""
    resultat = _ingest("24V", "Contacteur Norel, bobine 24V, courant 9A.")

    assert resultat.accepted == ()
    assert [r.reason for r in resultat.rejected] == ["reference_is_a_quantity"]


def test_une_vraie_reference_lue_sur_la_page_reste_acceptee():
    resultat = _ingest(
        "XZ07-20-10-11", "Contacteur Norel XZ07-20-10-11, bobine 24V."
    )

    assert [lead.reference for lead in resultat.accepted] == ["XZ07-20-10-11"]


def test_un_numero_d_article_est_garde_mais_retrograde():
    """Dorval publie des references numeriques : on ne tranche pas, on classe."""
    resultat = _ingest("00026001627", "Article Norel 00026001627 en stock.")

    assert [lead.reference for lead in resultat.accepted] == ["00026001627"]
    assert resultat.accepted[0].low_confidence is True
