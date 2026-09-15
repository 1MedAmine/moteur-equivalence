#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fabrique le corpus de rejeu de démonstration.

Le rejeu permet de faire tourner le moteur hors ligne : il relit des pages
déjà capturées au lieu d'en ouvrir de nouvelles. Encore faut-il un corpus.

Capturer de vraies pages reposerait le problème de droit d'auteur que ce dépôt
évite. Ce script fabrique donc les pages de toutes pièces, avec des fabricants
et des références inventés, sur des composants techniques génériques — le
vocabulaire employé partout ailleurs dans le dépôt.

Quatre situations, parce que ce sont celles qui distinguent un moteur qui
prouve d'un moteur qui devine :

1. un candidat entièrement prouvé, sur le site du fabricant ;
2. un candidat prouvé par corroboration, deux domaines apportant chacun une
   partie des preuves ;
3. un candidat non résolu faute de preuve, dont la page ne dit rien d'utile ;
4. un candidat contredit, dont une caractéristique contredit la demande.

Hors ligne, déterministe, sans clé ni réseau.

    python scripts/fabriquer_corpus_rejeu.py
    python rejouer.py --corpus .replay/demonstration

Écrit dans `.replay/demonstration/`, qui n'est pas versionné : ce script l'est.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

RACINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RACINE))

from compatibilite import canonical_url  # noqa: E402
from rejeu import _code_sha256  # noqa: E402

CORPUS = RACINE / ".replay" / "demonstration"

#: Date figée : un corpus fabriqué n'a pas d'heure de capture, et une date
#: courante rendrait le manifeste différent à chaque exécution.
GENERE_LE = "2026-01-01T00:00:00Z"

#: La fiche d'origine, telle que l'opérateur l'aurait fournie.
FICHE = (
    "Contacteur tripolaire\n"
    "Tension de commande 24 V DC\n"
    "Courant nominal 9 A\n"
    "3 pôles\n"
)

EXIGENCES = {
    "product": "Contacteur tripolaire",
    "origin_brand": "",
    "criteria": [
        {"id": "tension", "label": "Tension de commande", "requested_value": "24 V DC"},
        {"id": "courant", "label": "Courant nominal", "requested_value": "9 A"},
        {"id": "poles", "label": "Nombre de pôles", "requested_value": "3 pôles"},
    ],
}


# --------------------------------------------------------------- les pages
PAGES = {
    # 1. le fabricant publie tout : preuve complète, un seul domaine
    "https://vantek.example/pieces/vt-4120": (
        "Vantek VT-4120 — Contacteur tripolaire.\n"
        "Tension de commande 24 V DC. Courant nominal 9 A. 3 pôles.\n"
        "Montage sur rail. Contact auxiliaire intégré."
    ),
    # 2. le fabricant publie les valeurs électriques, le revendeur publie le
    #    nombre de pôles : la preuve ne tient que si les deux sont recoupés
    "https://orbex.example/catalogue/ob-4120": (
        "Orbex OB-4120 — Contacteur tripolaire.\n"
        "Tension de commande 24 V DC. Courant nominal 9 A.\n"
        "Fiche technique complète disponible auprès du réseau."
    ),
    "https://revendeur-industriel.example/orbex/ob-4120": (
        "OB-4120 Orbex, contacteur de puissance.\n"
        "3 pôles, montage sur rail symétrique.\n"
        "Disponible sous 48 heures."
    ),
    # 3. la page existe et nomme la référence, mais ne dit rien d'utile
    "https://solira.example/produits/sl-4120": (
        "Solira SL-4120 — Contacteur tripolaire.\n"
        "Consultez votre distributeur pour les caractéristiques détaillées.\n"
        "Conditionnement : boîte individuelle."
    ),
    # 4. la page contredit la demande : la tension n'est pas la bonne
    "https://kirova.example/fr/kv-4120": (
        "Kirova KV-4120 — Contacteur tripolaire.\n"
        "Tension de commande 48 V DC. Courant nominal 9 A. 3 pôles.\n"
        "Destiné aux armoires à commande 48 V."
    ),
}


def _preuve(url: str, extrait: str, officiel: bool = True) -> dict:
    return {
        "url": canonical_url(url),
        "excerpt": extrait,
        "type": "web_officiel" if officiel else "web_secondaire",
    }


def _critere(identifiant: str, demande: str, observe: str, statut: str, preuves) -> dict:
    return {
        "requirement_id": identifiant,
        "requested_value": demande,
        "observed_value": observe,
        "status": statut,
        "proofs": list(preuves),
    }


VANTEK = "https://vantek.example/pieces/vt-4120"
ORBEX_OFFICIEL = "https://orbex.example/catalogue/ob-4120"
ORBEX_REVENDEUR = "https://revendeur-industriel.example/orbex/ob-4120"
SOLIRA = "https://solira.example/produits/sl-4120"
KIROVA = "https://kirova.example/fr/kv-4120"

AUDITS = [
    # vague 1 — le candidat prouvé et le candidat contredit
    {
        "page_url": canonical_url(VANTEK),
        "candidates": [
            {
                "brand": "Vantek",
                "reference": "VT-4120",
                "criteria": [
                    _critere("tension", "24 V DC", "24 V DC", "proven",
                             [_preuve(VANTEK, "Tension de commande 24 V DC")]),
                    _critere("courant", "9 A", "9 A", "proven",
                             [_preuve(VANTEK, "Courant nominal 9 A")]),
                    _critere("poles", "3 pôles", "3 pôles", "proven",
                             [_preuve(VANTEK, "3 pôles")]),
                ],
            }
        ],
    },
    {
        "page_url": canonical_url(KIROVA),
        "candidates": [
            {
                "brand": "Kirova",
                "reference": "KV-4120",
                "criteria": [
                    _critere("tension", "24 V DC", "48 V DC", "incompatible",
                             [_preuve(KIROVA, "Tension de commande 48 V DC")]),
                    _critere("courant", "9 A", "9 A", "proven",
                             [_preuve(KIROVA, "Courant nominal 9 A")]),
                    _critere("poles", "3 pôles", "3 pôles", "proven",
                             [_preuve(KIROVA, "3 pôles")]),
                ],
                "deviations": ["Commande 48 V DC au lieu des 24 V demandés."],
            }
        ],
    },
    # vague 2 — la corroboration, puis le candidat sans preuve
    {
        "page_url": canonical_url(ORBEX_OFFICIEL),
        "candidates": [
            {
                "brand": "Orbex",
                "reference": "OB-4120",
                "criteria": [
                    _critere("tension", "24 V DC", "24 V DC", "proven",
                             [_preuve(ORBEX_OFFICIEL, "Tension de commande 24 V DC")]),
                    _critere("courant", "9 A", "9 A", "proven",
                             [_preuve(ORBEX_OFFICIEL, "Courant nominal 9 A")]),
                    _critere("poles", "3 pôles", "", "not_proven", []),
                ],
            }
        ],
    },
    {
        "page_url": canonical_url(ORBEX_REVENDEUR),
        "candidates": [
            {
                "brand": "Orbex",
                "reference": "OB-4120",
                "criteria": [
                    _critere("tension", "24 V DC", "", "not_proven", []),
                    _critere("courant", "9 A", "", "not_proven", []),
                    _critere("poles", "3 pôles", "3 pôles", "proven",
                             [_preuve(ORBEX_REVENDEUR, "3 pôles",
                                      officiel=False)]),
                ],
                "limitations": ["Nombre de pôles confirmé par un revendeur, pas par le fabricant."],
            }
        ],
    },
    {
        "page_url": canonical_url(SOLIRA),
        "candidates": [
            {
                "brand": "Solira",
                "reference": "SL-4120",
                "criteria": [
                    _critere("tension", "24 V DC", "", "not_proven", []),
                    _critere("courant", "9 A", "", "not_proven", []),
                    _critere("poles", "3 pôles", "", "not_proven", []),
                ],
                "limitations": ["La page du fabricant ne publie aucune valeur."],
            }
        ],
    },
]

#: Vague de chaque audit, dans l'ordre où ils sont déclarés.
VAGUES_AUDITS = [1, 1, 2, 2, 2]
VAGUES_PAGES = {
    canonical_url(VANTEK): 1,
    canonical_url(KIROVA): 1,
    canonical_url(ORBEX_OFFICIEL): 2,
    canonical_url(ORBEX_REVENDEUR): 2,
    canonical_url(SOLIRA): 2,
}


def construire_manifeste() -> dict:
    return {
        "schema_version": 1,
        "generated_at": GENERE_LE,
        "target_brand": "",
        "threshold": 75,
        "strict_evidence": True,
        "B2_MODEL": "corpus-fabrique/demonstration",
        "code": {
            # Le corpus est FABRIQUÉ, pas capturé : il ne revendique aucun
            # commit. L'empreinte du code, elle, est réelle : elle dit contre
            # quelle version du moteur le corpus a été produit.
            "git_commit": "unavailable",
            "sha256": _code_sha256(),
        },
        "fiche": {
            "path": "",
            "sha256": hashlib.sha256(FICHE.encode("utf-8")).hexdigest(),
            "hash_scope": "extracted_text",
        },
        "audit_waves": VAGUES_AUDITS,
        "page_waves": VAGUES_PAGES,
        "waves": [
            {
                "wave": 1,
                "logical_queries_at_entry": 0,
                "logical_queries_before_near_miss": 3,
                "seen_signatures_at_entry": [],
                "seen_signatures_before_near_miss": [],
            },
            {
                "wave": 2,
                "logical_queries_at_entry": 3,
                "logical_queries_before_near_miss": 6,
                "seen_signatures_at_entry": ["contacteur-3p-24vdc"],
                "seen_signatures_before_near_miss": ["contacteur-3p-24vdc"],
            },
        ],
    }


def _ecrire(chemin: Path, valeur: object) -> None:
    chemin.write_text(
        json.dumps(valeur, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )


def ecrire_corpus(dossier: Path) -> Path:
    """Écrit les quatre fichiers du corpus dans `dossier`, et le rend.

    Exposé pour que la suite de tests fabrique le corpus dans un dossier
    temporaire et le rejoue : un jeu de données qui n'est jamais rejoué
    finit par ne plus correspondre au format attendu.
    """
    dossier.mkdir(parents=True, exist_ok=True)
    _ecrire(dossier / "requirements.json", EXIGENCES)
    _ecrire(dossier / "audits.json", AUDITS)
    _ecrire(dossier / "pages.json", PAGES)
    _ecrire(dossier / "manifest.json", construire_manifeste())
    return dossier


def main() -> int:
    ecrire_corpus(CORPUS)

    print(f"corpus : {CORPUS}")
    print(f"{len(PAGES)} pages, {len(AUDITS)} audits, 4 candidats, 2 vagues.")
    print("rejeu  : python rejouer.py --corpus .replay/demonstration")
    return 0


if __name__ == "__main__":
    sys.exit(main())
