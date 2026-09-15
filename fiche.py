# -*- coding: utf-8 -*-
"""Lecture de la fiche technique du produit demande.

La fiche est la source des criteres a preserver : elle est rendue telle quelle,
sans reformulation ni resume. Decider ici ce qui compte reviendrait a trancher
a la place du modele, qui a la mission complete sous les yeux.
"""

from __future__ import annotations

from pathlib import Path

from extraction_pdf import extract_pdf

# En dessous, il n'y a pas de quoi identifier un produit : mieux vaut le dire
# que lancer une recherche sur trois mots et rendre un resultat sans valeur.
LONGUEUR_MINIMALE = 20


class FicheInvalide(Exception):
    """Fiche absente, vide, illisible ou trop pauvre pour etre exploitee."""


def _lire_pdf(chemin: Path) -> str:
    try:
        return extract_pdf(chemin).text
    except FicheInvalide:
        raise
    except Exception as exc:
        raise FicheInvalide(
            f"PDF illisible ({chemin}) : {type(exc).__name__}"
        ) from exc


def _lire_texte(chemin: Path) -> str:
    """Lit un fichier texte sans se laisser arreter par son encodage.

    Les fiches viennent de sources heterogenes : utf-8 le plus souvent, cp1252
    quand elles sortent d'un outil Windows. `errors="replace"` en dernier
    recours perd quelques caracteres mais garde la fiche exploitable — la perdre
    entierement pour un accent serait pire.
    """
    for encodage in ("utf-8", "cp1252"):
        try:
            return chemin.read_text(encoding=encodage)
        except UnicodeDecodeError:
            continue
    return chemin.read_text(encoding="utf-8", errors="replace")


def lire(chemin: str | Path) -> str:
    """Rend le texte de la fiche, ou leve `FicheInvalide`.

    Formats acceptes : PDF, et tout fichier texte (.txt, .md, .csv...).
    """
    chemin = Path(chemin)
    if not chemin.exists():
        raise FicheInvalide(f"Fiche introuvable : {chemin}")
    if not chemin.is_file():
        raise FicheInvalide(f"Ce n'est pas un fichier : {chemin}")

    texte = _lire_pdf(chemin) if chemin.suffix.lower() == ".pdf" else _lire_texte(chemin)
    texte = texte.strip()

    if not texte:
        raise FicheInvalide(f"Fiche vide : {chemin}")
    if len(texte) < LONGUEUR_MINIMALE:
        raise FicheInvalide(
            f"Fiche trop courte pour identifier un produit ({len(texte)} "
            f"caracteres) : {chemin}"
        )
    return texte
