# -*- coding: utf-8 -*-
"""cli.py — point d'entrée process-à-process du moteur d'équivalence.

Lit un JSON sur stdin, écrit un JSON sur stdout. Ce moteur a été construit
comme composant d'un système appelant plus large, non publié dans ce dépôt ;
`cli.py` est la frontière entre les deux. Ses dépendances sont lourdes et
parfois incompatibles avec celles d'un appelant (navigateurs pilotés,
extraction PDF, client LLM) : les invoquer en sous-processus, plutôt qu'en
import direct, évite d'imposer cet environnement à qui l'embarque.

`outil.py` reste la ligne de commande humaine, qui prend une fiche déjà sur
disque. Ici l'appelant n'a pas de fiche : il a un cahier des charges déjà
construit. Ce module le met en forme de fiche, le temps de l'appel.

Entrée (stdin) — le contrat canonique de l'appelant :
{
  "cahier_des_charges": "...",          # bloc technique, obligatoire
  "produit": "...", "marque": "...",    # identité du produit d'origine
  "reference": "...", "description": "...",
  "caracteristiques": {"tension": "24 V", ...},
  "mode_marques": "ouvert" | "strict" | "preferentiel",
  "marques_cibles": ["..."],            # vide en mode ouvert
  "marques_exclues": ["..."]
}

Sortie (stdout), code 0 : le dictionnaire de `rapport.construire`, tel quel
(status, proposed_candidates, discarded_candidates, compatibility, warnings,
sources, criteria, diagnostics). Aucune traduction ici : l'appelant traduit.

Sortie (stdout), code 1 : {"erreur": "..."}.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import outil


def rediger_fiche(entree: dict) -> str:
    """Rend le cahier des charges sous la forme d'une fiche technique.

    Le moteur lit une fiche, jamais un dictionnaire : c'est sa seule entrée, et
    elle est rendue au modèle telle quelle, sans résumé. On se contente donc de
    mettre en page ce que l'appelant a déjà établi, sans rien ajouter.
    """
    lignes = []
    for etiquette, cle in (
        ("Produit", "produit"),
        ("Marque", "marque"),
        ("Référence", "reference"),
        ("Description", "description"),
    ):
        valeur = " ".join(str(entree.get(cle) or "").split())
        if valeur:
            lignes.append(f"{etiquette} : {valeur}")

    caracteristiques = entree.get("caracteristiques") or {}
    if isinstance(caracteristiques, dict) and caracteristiques:
        lignes.append("")
        lignes.append("Caractéristiques :")
        for nom, valeur in caracteristiques.items():
            texte = " ".join(str(valeur or "").split())
            if texte:
                lignes.append(f"- {nom} : {texte}")

    cahier = str(entree.get("cahier_des_charges") or "").strip()
    if cahier:
        lignes.append("")
        lignes.append("Cahier des charges :")
        lignes.append(cahier)

    return "\n".join(lignes).strip()


def marque_recherchee(entree: dict) -> str | None:
    """Fabricant auquel restreindre la recherche, ou `None` si elle est libre.

    Le moteur restreint à UN fabricant. En mode ouvert il n'y en a aucun. Quand
    l'appelant en vise plusieurs, on lance la recherche sur le premier : les
    autres restent sa contrainte à lui, qui filtre ce qui revient.
    """
    if str(entree.get("mode_marques") or "ouvert") == "ouvert":
        return None
    for marque in entree.get("marques_cibles") or []:
        texte = " ".join(str(marque or "").split())
        if texte:
            return texte
    return None


def executer(entree: dict) -> dict:
    """Écrit la fiche, lance le moteur, rend sa sortie telle quelle."""
    texte = rediger_fiche(entree)
    dossier = tempfile.mkdtemp(prefix="fiche_equivalence_")
    chemin = Path(dossier) / "fiche.txt"
    try:
        chemin.write_text(texte, encoding="utf-8")
        return outil.executer(str(chemin), marque_recherchee(entree))
    finally:
        try:
            chemin.unlink(missing_ok=True)
            Path(dossier).rmdir()
        except OSError:
            pass


def main() -> int:
    try:
        entree = json.load(sys.stdin)
        resultat = executer(entree)
    except Exception as erreur:  # une panne doit rester lisible par l'appelant
        json.dump(
            {"erreur": f"{type(erreur).__name__}: {erreur}"},
            sys.stdout,
            ensure_ascii=False,
        )
        return 1

    json.dump(resultat, sys.stdout, ensure_ascii=False, default=str)
    return 0


if __name__ == "__main__":
    sys.exit(main())
