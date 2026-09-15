# -*- coding: utf-8 -*-
"""Le corpus de démonstration se fabrique, se rejoue, et dit ce qu'il promet.

Un jeu de données qu'on ne rejoue jamais finit par ne plus correspondre au
format attendu, sans que personne ne s'en aperçoive. Ce test le fabrique dans
un dossier temporaire et le rejoue pour de bon : hors ligne, sans clé, sans
réseau.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from rejeu import replay_corpus

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import fabriquer_corpus_rejeu as fabrique  # noqa: E402


@pytest.fixture(scope="module")
def rejeu(tmp_path_factory):
    """Rejoue le corpus avec la connaissance de marques LIVRÉE.

    Le reste de la suite exerce une table de marques qui lui est propre. Ce
    corpus-ci appartient au domaine de démonstration : ses fabricants sont
    ceux que `marques.exemple.json` déclare, et c'est cette table qu'il faut
    charger pour que les domaines officiels soient reconnus.
    """
    import compatibilite
    import marques_connues

    os.environ["B2_MARQUES_CONNUES"] = str(
        Path(__file__).resolve().parents[1] / "marques.exemple.json"
    )
    marques_connues.oublier()
    domaines = compatibilite._KNOWN_OFFICIAL_DOMAINS
    alias = compatibilite._CANONICAL_BRAND_ALIASES
    compatibilite._KNOWN_OFFICIAL_DOMAINS = marques_connues.domaines_officiels()
    compatibilite._CANONICAL_BRAND_ALIASES = marques_connues.alias_canoniques()
    try:
        dossier = fabrique.ecrire_corpus(
            tmp_path_factory.mktemp("corpus") / "demonstration"
        )
        yield replay_corpus(dossier)
    finally:
        compatibilite._KNOWN_OFFICIAL_DOMAINS = domaines
        compatibilite._CANONICAL_BRAND_ALIASES = alias
        os.environ.pop("B2_MARQUES_CONNUES", None)
        marques_connues.oublier()


def _par_reference(candidats):
    return {candidat["reference"]: candidat for candidat in candidats}


def test_le_candidat_entierement_prouve_sort_complet_et_officiel(rejeu):
    candidat = _par_reference(rejeu["proposed_candidates"])["VT-4120"]

    assert candidat["complete"] is True
    assert candidat["score"] == 100
    assert candidat["not_proven_criteria"] == []
    assert "officielle" in candidat["evidence_note"].casefold()


def test_la_corroboration_de_deux_domaines_prouve_un_candidat(rejeu):
    """Aucun domaine ne prouve tout : c'est leur recoupement qui prouve."""
    candidat = _par_reference(rejeu["proposed_candidates"])["OB-4120"]

    assert candidat["complete"] is True
    domaines = {
        source["url"].split("/")[2]
        for source in candidat["sources"]
    }
    assert len(domaines) >= 2


def test_le_candidat_sans_preuve_n_est_jamais_propose(rejeu):
    """Ne rien pouvoir prouver n'est pas une raison de proposer quand même."""
    assert "SL-4120" not in _par_reference(rejeu["proposed_candidates"])

    evaluations = rejeu["diagnostics"]["candidate_evaluations"]
    sans_preuve = next(e for e in evaluations if e["reference"] == "SL-4120")
    assert sans_preuve["score"] == 0
    assert sans_preuve["eligible"] is False


def test_le_candidat_contredit_est_ecarte_avec_son_motif(rejeu):
    ecarte = _par_reference(rejeu["discarded_candidates"])["KV-4120"]

    assert "Tension" in ecarte["reason"]
    assert "KV-4120" not in _par_reference(rejeu["proposed_candidates"])


def test_le_rejeu_conclut_sur_le_meilleur_candidat(rejeu):
    assert rejeu["status"] == "complete"
    assert rejeu["compatibility"]["reference"] == "VT-4120"
    assert rejeu["diagnostics"]["final_state"] == "candidate_selected"
    assert rejeu["diagnostics"]["waves"] == 2


def test_le_corpus_se_regenere_a_l_identique(tmp_path):
    """Deux mesures ne se comparent que si le jeu n'a pas bougé entre elles."""
    premier = fabrique.ecrire_corpus(tmp_path / "un")
    second = fabrique.ecrire_corpus(tmp_path / "deux")

    for nom in ("requirements.json", "audits.json", "pages.json", "manifest.json"):
        assert (premier / nom).read_bytes() == (second / nom).read_bytes()
