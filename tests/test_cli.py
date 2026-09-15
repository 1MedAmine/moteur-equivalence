# -*- coding: utf-8 -*-
"""Le point d'entrée process-à-process : ce qu'il rédige, ce qu'il transmet."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

import cli
import fiche as module_fiche


ENTREE = {
    "cahier_des_charges": "Contacteur 3P, bobine 24 V DC, courant AC-3 9 A",
    "produit": "contacteur",
    "marque": "Norel",
    "reference": "XZ07-20-10-11",
    "caracteristiques": {"tension de commande": "24 V DC", "poles": "3"},
    "mode_marques": "ouvert",
    "marques_cibles": [],
    "marques_exclues": [],
}


def test_la_fiche_reprend_l_identite_les_caracteristiques_et_le_cahier():
    texte = cli.rediger_fiche(ENTREE)

    assert "Produit : contacteur" in texte
    assert "Référence : XZ07-20-10-11" in texte
    assert "- tension de commande : 24 V DC" in texte
    assert texte.rstrip().endswith("Contacteur 3P, bobine 24 V DC, courant AC-3 9 A")


def test_la_fiche_redigee_est_lisible_par_le_moteur(tmp_path):
    """Ce que la CLI écrit doit passer le contrôle d'entrée du moteur."""
    chemin = tmp_path / "fiche.txt"
    chemin.write_text(cli.rediger_fiche(ENTREE), encoding="utf-8")

    assert module_fiche.lire(str(chemin)).startswith("Produit : contacteur")


def test_une_entree_sans_contenu_est_refusee_par_le_controle_de_fiche(tmp_path):
    chemin = tmp_path / "fiche.txt"
    chemin.write_text(cli.rediger_fiche({"produit": "x"}), encoding="utf-8")

    with pytest.raises(module_fiche.FicheInvalide):
        module_fiche.lire(str(chemin))


@pytest.mark.parametrize(
    "entree,attendu",
    [
        ({"mode_marques": "ouvert", "marques_cibles": ["Dorval"]}, None),
        ({"mode_marques": "strict", "marques_cibles": ["Dorval"]}, "Dorval"),
        ({"mode_marques": "preferentiel", "marques_cibles": []}, None),
        (
            {"mode_marques": "strict", "marques_cibles": ["", " Kerion "]},
            "Kerion",
        ),
    ],
)
def test_la_marque_transmise_suit_le_mode_demande(entree, attendu):
    """Le moteur ne restreint qu'à un fabricant : en mode ouvert, aucun."""
    assert cli.marque_recherchee(entree) == attendu


def test_le_fichier_temporaire_ne_survit_pas_a_l_appel(monkeypatch):
    vus = {}

    def faux_executer(chemin, marque):
        vus["chemin"] = Path(chemin)
        vus["marque"] = marque
        vus["existait"] = Path(chemin).is_file()
        return {"status": "not_resolved", "proposed_candidates": []}

    monkeypatch.setattr(cli.outil, "executer", faux_executer)

    resultat = cli.executer(ENTREE)

    assert resultat["status"] == "not_resolved"
    assert vus["existait"] is True
    assert not vus["chemin"].exists()
    assert not vus["chemin"].parent.exists()


def test_une_panne_du_moteur_sort_en_json_et_non_en_trace(monkeypatch, capsys):
    def moteur_casse(chemin, marque):
        raise OSError("disque plein")

    monkeypatch.setattr(cli.outil, "executer", moteur_casse)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(ENTREE)))

    code = cli.main()

    assert code == 1
    assert json.loads(capsys.readouterr().out) == {"erreur": "OSError: disque plein"}
