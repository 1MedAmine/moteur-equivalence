# -*- coding: utf-8 -*-
"""Garde-fous de la suite : basetemp stable, sans clé, hors ligne.

Trois choses, appliquées à toute la suite :

1. Le dossier temporaire de pytest est ancré sur ce module, pas sur le
   répertoire d'invocation. `--basetemp` est résolu par pytest depuis le
   répertoire d'invocation, jamais depuis le fichier qui le porte ; et le
   défaut système (`%TEMP%\\pytest-of-<user>`) est refusé en écriture dans
   certains environnements.
2. Les clés d'API connues du moteur sont vidées. Le dépôt n'en contient
   aucune ; les vider explicitement empêche qu'un `.env` posé à côté fasse
   passer un test pour une raison qui n'est pas la sienne.
3. Le réseau sortant est coupé. Un test qui ouvre une connexion vers
   l'extérieur échoue en le disant, au lieu de dépendre de la machine qui
   l'exécute — ou de masquer un vrai appel réseau derrière un faux succès.
   La boucle locale reste ouverte : rien ici n'a besoin d'un service local.
"""

from __future__ import annotations

import os
import socket
from pathlib import Path

DOSSIER_TEMPORAIRE = Path(__file__).resolve().parent / ".pytest_tmp"

#: Connaissance de marques de la suite, distincte de celle livrée avec le
#: dépôt. Posée AVANT tout import de `compatibilite`, qui lit la table au
#: chargement du module : les tests exercent ainsi une table stable, et le
#: domaine de démonstration peut changer sans les toucher.
os.environ.setdefault(
    "B2_MARQUES_CONNUES",
    str(Path(__file__).resolve().parent / "tests" / "marques.reference.json"),
)

CLES_VIDEES = ("B2_API_KEY", "NVIDIA_API_KEY", "INDUSTRIAL_API_KEY")

_connexion_reelle = socket.socket.connect
_connexion_ex_reelle = socket.socket.connect_ex
_creation_reelle = socket.create_connection


class SortieReseauInterdite(RuntimeError):
    """Un test a tenté d'ouvrir une connexion vers l'extérieur."""


def _hote_local(adresse) -> bool:
    if not isinstance(adresse, tuple) or not adresse:
        return True  # AF_UNIX et consorts : rien ne sort de la machine
    hote = str(adresse[0])
    if hote in {"localhost", "::1", "", "0.0.0.0"}:
        return True
    return hote.startswith("127.")


def _refuser(adresse):
    raise SortieReseauInterdite(
        f"Connexion sortante vers {adresse!r} : la suite tourne hors ligne. "
        "Remplacez l'appel réseau par un double dans le test."
    )


def _connect(self, adresse):
    if not _hote_local(adresse):
        _refuser(adresse)
    return _connexion_reelle(self, adresse)


def _connect_ex(self, adresse):
    if not _hote_local(adresse):
        _refuser(adresse)
    return _connexion_ex_reelle(self, adresse)


def _create_connection(adresse, *args, **kwargs):
    if not _hote_local(adresse):
        _refuser(adresse)
    return _creation_reelle(adresse, *args, **kwargs)


def pytest_configure(config) -> None:
    if not config.option.basetemp:
        config.option.basetemp = str(DOSSIER_TEMPORAIRE)
    for nom in CLES_VIDEES:
        os.environ[nom] = ""
    socket.socket.connect = _connect
    socket.socket.connect_ex = _connect_ex
    socket.create_connection = _create_connection


def pytest_unconfigure(config) -> None:
    socket.socket.connect = _connexion_reelle
    socket.socket.connect_ex = _connexion_ex_reelle
    socket.create_connection = _creation_reelle
