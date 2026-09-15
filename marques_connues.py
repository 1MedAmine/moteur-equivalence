# -*- coding: utf-8 -*-
"""Connaissance de marques, tenue hors du moteur.

Le moteur d'équivalence ne connaît aucune marque : il raisonne sur des
critères, des preuves et des domaines, jamais sur un catalogue de noms appris.
Deux tables faisaient pourtant exception, écrites en dur dans
`compatibilite.py` : les domaines officiels reconnus et les alias courts
documentés. Les voici sorties du code, dans un fichier de configuration.

Le fichier livré (`marques.exemple.json`) ne contient que des valeurs
d'exemple. `B2_MARQUES_CONNUES` désigne le vôtre.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

DOSSIER = Path(__file__).resolve().parent
FICHIER_EXEMPLE = DOSSIER / "marques.exemple.json"


class ConfigurationMarquesInvalide(ValueError):
    """Le fichier de connaissance de marques est illisible ou mal formé."""


def chemin() -> Path:
    """Fichier effectivement lu : `B2_MARQUES_CONNUES`, sinon l'exemple."""
    configure = os.getenv("B2_MARQUES_CONNUES", "").strip()
    return Path(configure) if configure else FICHIER_EXEMPLE


def _jeu(cle: str) -> frozenset[str]:
    """« kerion electric » -> frozenset({"kerion", "electric"})."""
    return frozenset(jeton for jeton in str(cle).casefold().split() if jeton)


def _table(brut: object, nom: str) -> dict[frozenset[str], frozenset[str]]:
    if brut is None:
        return {}
    if not isinstance(brut, dict):
        raise ConfigurationMarquesInvalide(f"{nom} doit être un objet JSON.")
    table: dict[frozenset[str], frozenset[str]] = {}
    for cle, valeurs in brut.items():
        if isinstance(valeurs, str):
            valeurs = [valeurs]
        if not isinstance(valeurs, (list, tuple)):
            raise ConfigurationMarquesInvalide(
                f"{nom}[{cle!r}] doit être une liste de chaînes."
            )
        jeu = _jeu(cle)
        if not jeu:
            continue
        table[jeu] = frozenset(str(v).casefold().strip() for v in valeurs if str(v).strip())
    return table


@lru_cache(maxsize=1)
def _charger() -> dict[str, dict[frozenset[str], frozenset[str]]]:
    fichier = chemin()
    if not fichier.is_file():
        # L'absence n'est pas une panne : un moteur sans connaissance de
        # marque retombe sur la règle générale (domaine enregistrable
        # comparé au nom), qui est le comportement par défaut voulu.
        return {"domaines_officiels": {}, "alias_canoniques": {}}
    try:
        donnees = json.loads(fichier.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as erreur:
        raise ConfigurationMarquesInvalide(
            f"{fichier} illisible : {erreur}"
        ) from erreur
    return {
        "domaines_officiels": _table(donnees.get("domaines_officiels"), "domaines_officiels"),
        "alias_canoniques": _table(donnees.get("alias_canoniques"), "alias_canoniques"),
    }


def domaines_officiels() -> dict[frozenset[str], frozenset[str]]:
    """Marque -> domaines acceptés comme site officiel du fabricant."""
    return dict(_charger()["domaines_officiels"])


def alias_canoniques() -> dict[frozenset[str], frozenset[str]]:
    """Alias court documenté -> forme canonique de la marque."""
    return dict(_charger()["alias_canoniques"])


def oublier() -> None:
    """Vide le cache : utile après avoir changé `B2_MARQUES_CONNUES`."""
    _charger.cache_clear()
