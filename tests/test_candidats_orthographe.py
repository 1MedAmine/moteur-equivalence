# -*- coding: utf-8 -*-
"""L'orthographe retenue pour une piste decide de ce que B2 saura chercher.

Mission reelle `NV1T05BD -> Norel` du 2026-08-20. `canonical_candidate_key`
efface les separateurs : `XZ07-20-10-11` et `XZ07201011` partagent la cle
`xz07201011` et fusionnent en une seule piste. La fusion gardait la forme
arrivee en premier. Une page epelait la reference sans tirets ; toutes les
observations suivantes, correctement ponctuees, ont ete absorbees en silence.

B2 a donc cherche douze fois `Norel XZ07201011 ...`. Mesure SearXNG sur la meme
requete :

    XZ07201011      -> accueil Norel, un PDF italien sans rapport
    XZ07-20-10-11   -> fiche produit, puis library.e.norel.example/.../4KBC101401D0201.pdf

La forme retenue est desormais celle vue dans le plus de sources distinctes.
Elle est toujours choisie parmi les formes reellement rencontrees : rien
n'autorise a reinserer des tirets dans une reference qui n'en a jamais montre.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from candidats import (
    CandidateOccurrence,
    CandidateProposal,
    merge_lead,
)


def _proposal(reference: str) -> CandidateProposal:
    return CandidateProposal(brand="Norel", reference=reference)


def _occurrences(fragment: str, urls, fields=("content",)) -> tuple:
    return tuple(
        CandidateOccurrence(url, field, fragment, rang)
        for rang, url in enumerate(urls, start=1)
        for field in fields
    )


# --------------------------------------------------------------------------
# Le cas mesure sur la mission


def test_la_forme_vue_dans_le_plus_de_sources_l_emporte():
    """Mutation détectée : B2 cherche une orthographe que le web n'emploie pas."""
    # Une seule page, mais elle repete la forme sans tirets dans trois champs.
    sans_tirets = merge_lead(
        None,
        _proposal("XZ07201011"),
        _occurrences(
            "xz07201011",
            ["https://distributeur.example/fiche"],
            fields=("content", "title", "url"),
        ),
    )

    # Deux pages distinctes, un seul champ chacune.
    avec_tirets = merge_lead(
        sans_tirets,
        _proposal("XZ07-20-10-11"),
        _occurrences(
            "xz07-20-10-11",
            ["https://new.norel.example/products/xz07-20-10-11", "https://catalogue.example/p"],
        ),
    )

    assert avec_tirets.reference == "XZ07-20-10-11"
    # La cle canonique ne bouge pas : c'est toujours la meme piste.
    assert avec_tirets.canonical_reference == sans_tirets.canonical_reference


def test_trois_champs_d_une_meme_page_ne_valent_pas_trois_sources():
    """Une page qui se repete ne doit pas peser plus qu'une page de plus."""
    piste = merge_lead(
        None,
        _proposal("XZ07201011"),
        _occurrences(
            "xz07201011",
            ["https://distributeur.example/fiche"],
            fields=("content", "title", "url", "snippet"),
        ),
    )
    piste = merge_lead(
        piste,
        _proposal("XZ07-20-10-11"),
        _occurrences(
            "xz07-20-10-11",
            ["https://new.norel.example/p", "https://autre.example/p"],
        ),
    )

    assert piste.reference == "XZ07-20-10-11"


# --------------------------------------------------------------------------
# Ce que la regle ne doit surtout pas faire


def test_aucune_forme_jamais_vue_n_est_fabriquee():
    """Interdit : reinserer des separateurs dans une reference qui n'en montre pas."""
    piste = merge_lead(
        None,
        _proposal("4KBL136001R3001"),
        _occurrences(
            "4kbl136001r3001",
            ["https://new.norel.example/a", "https://new.norel.example/b", "https://x.example/c"],
        ),
    )

    assert piste.reference == "4KBL136001R3001"


def test_une_egalite_conserve_la_forme_arrivee_en_premier():
    """Sans preuve qu'une forme domine, l'ordre reste stable et previsible."""
    piste = merge_lead(
        None, _proposal("ZX-41-7"), _occurrences("zx-41-7", ["https://a.example/1"])
    )
    piste = merge_lead(
        piste, _proposal("ZX417"), _occurrences("zx417", ["https://b.example/2"])
    )

    assert piste.reference == "ZX-41-7"


def test_la_casse_proposee_est_conservee_et_non_repliee():
    """`xz07-20-10-11` en minuscules serait la trace du filtre, pas de la source."""
    piste = merge_lead(
        None, _proposal("XZ07201011"), _occurrences("xz07201011", ["https://a.example/1"])
    )
    piste = merge_lead(
        piste,
        _proposal("XZ07-20-10-11"),
        _occurrences("xz07-20-10-11", ["https://b.example/2", "https://c.example/3"]),
    )

    assert piste.reference == "XZ07-20-10-11"


def test_une_piste_sans_occurrence_garde_la_reference_proposee():
    """Cas des pistes de repli : rien d'observe, donc rien a departager."""
    piste = merge_lead(None, _proposal("XZ07-20-10-11"), ())

    assert piste.reference == "XZ07-20-10-11"


def test_une_seule_orthographe_observee_n_arbitre_rien():
    """`fragment` sort du filtre, replie : l'elire afficherait `zx417` pour `ZX417`.

    Sans orthographe concurrente il n'y a pas de choix a faire, et la forme
    proposee reste la reference — c'est le comportement d'origine.
    """
    piste = merge_lead(
        None,
        _proposal("ZX-41-7"),
        _occurrences("zx417", ["https://a.example/1", "https://b.example/2"]),
    )

    assert piste.reference == "ZX-41-7"


# --------------------------------------------------------------------------
# Le reste de la fusion ne bouge pas


def test_les_occurrences_restent_cumulees_et_dedupliquees():
    premiere = _occurrences("xz07-20-10-11", ["https://a.example/1"])
    piste = merge_lead(None, _proposal("XZ07-20-10-11"), premiere)
    piste = merge_lead(
        piste,
        _proposal("XZ07-20-10-11"),
        premiere + _occurrences("xz07-20-10-11", ["https://b.example/2"]),
    )

    assert len(piste.occurrences) == 2
    assert {item.url for item in piste.occurrences} == {
        "https://a.example/1",
        "https://b.example/2",
    }


def test_la_confiance_faible_ne_survit_qu_a_l_unanimite():
    piste = merge_lead(
        None,
        _proposal("XZ07-20-10-11"),
        _occurrences("xz07-20-10-11", ["https://a.example/1"]),
        low_confidence=True,
    )
    piste = merge_lead(
        piste,
        _proposal("XZ07-20-10-11"),
        _occurrences("xz07-20-10-11", ["https://b.example/2"]),
        low_confidence=False,
    )

    assert piste.low_confidence is False
