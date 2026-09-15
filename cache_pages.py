# -*- coding: utf-8 -*-
"""Cache de pages : ce qui a ete recupere une fois n'est plus redemande.

Le reseau n'est pas reproductible. Un distributeur qui repond 200 a un run
renvoie 403 au suivant, et la preuve qu'il portait disparait avec lui : le
second run n'a pas trouve autre chose, il a trouve moins. Ce cache retire
cette source de variance sans rien changer au reste de la chaine — il
s'interpose devant le recuperateur, sert ce qu'il a deja vu, delegue le reste.

Regle non negociable : seuls les succes sont ecrits. Figer un 403 transitoire
le rendrait permanent pour toute la duree de vie de l'entree, soit exactement
l'inverse du but recherche.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from compatibilite import canonical_url
from scraping import PageContent


LOGGER = logging.getLogger(__name__)

SCHEMA_VERSION = 1

#: Miroir de `PageContent.mode`. `from __future__ import annotations` reduit
#: l'annotation a une chaine, donc le `Literal` n'est pas lisible a l'execution :
#: la liste est recopiee ici et `test_cache_pages` verifie qu'elle ne derive pas.
MODES_CONNUS = frozenset({
    "scrapling",
    "stealthy",
    "camoufox",
    "scrapegraph_url",
    "pdf",
})


def _cle(url: str) -> str:
    """Rend la cle de cache, ou une chaine vide quand l'URL n'en merite pas.

    `canonical_url("")` vaut `/`, et une URL relative se reduit de meme : sans
    ce controle, toutes partageraient une seule et meme entree. Le filtre
    reprend celui de `PageFetcher.fetch`, qui ne va sur le reseau que pour
    http(s) et refuse une adresse sans hote.
    """
    canonique = canonical_url(url)
    decoupe = urlsplit(canonique)
    if decoupe.scheme.casefold() not in {"http", "https"} or not decoupe.hostname:
        return ""
    return canonique


def _maintenant() -> datetime:
    return datetime.now(timezone.utc)


def _note_de_cache(recuperee_le: datetime) -> str:
    """Une preuve servie depuis le disque doit le dire dans le rapport."""
    return (
        "page servie depuis le cache local, recuperee le "
        f"{recuperee_le.isoformat(timespec='seconds')}"
    )


def _lire_horodatage(valeur: object) -> datetime | None:
    if not isinstance(valeur, str):
        return None
    try:
        lu = datetime.fromisoformat(valeur)
    except ValueError:
        return None
    # Une entree ecrite sans fuseau, comparee a un instant aware, leverait
    # `TypeError` : on la rattache a UTC, qui est le fuseau d'ecriture.
    return lu if lu.tzinfo else lu.replace(tzinfo=timezone.utc)


def _est_liste_de_textes(valeur: object) -> bool:
    return isinstance(valeur, list) and all(isinstance(item, str) for item in valeur)


def _ecrire_atomiquement(chemin: Path, charge: dict) -> None:
    """Un run interrompu ne doit pas laisser une entree tronquee derriere lui.

    `_fetch_pages` recupere en parallele : deux fils peuvent viser la meme
    entree. Chacun ecrit dans son propre fichier provisoire et `os.replace`
    bascule d'un coup, donc le perdant se contente d'ecraser un contenu
    equivalent — jamais d'en produire un a moitie ecrit.
    """
    descripteur, provisoire = tempfile.mkstemp(
        dir=str(chemin.parent),
        prefix=f".{chemin.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(descripteur, "w", encoding="utf-8") as fichier:
            json.dump(charge, fichier, ensure_ascii=False)
        os.replace(provisoire, chemin)
    except BaseException:
        Path(provisoire).unlink(missing_ok=True)
        raise


@dataclass(frozen=True)
class CachePages:
    """Stockage disque d'une page, indexe par URL canonique."""

    dossier: Path
    duree_de_vie: timedelta

    def _chemin(self, canonique: str) -> Path:
        # L'URL ne peut pas servir de nom de fichier : elle depasse la limite
        # de chemin de Windows et contient des caracteres interdits. L'empreinte
        # est de longueur fixe et ne collisionne pas en pratique.
        empreinte = hashlib.sha256(canonique.encode("utf-8")).hexdigest()
        return self.dossier / f"{empreinte}.json"

    def lire(self, url: str) -> PageContent | None:
        """Rend la page stockee, ou `None` des que le moindre doute existe.

        Toute anomalie — fichier illisible, schema inconnu, entree perimee —
        se traduit par un manque, jamais par une exception : le cache est un
        raccourci, et son echec ne doit couter qu'un aller sur le reseau.
        """
        canonique = _cle(url)
        if not canonique:
            return None
        try:
            brut = json.loads(self._chemin(canonique).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(brut, dict) or brut.get("version") != SCHEMA_VERSION:
            return None

        contenu = brut.get("content")
        mode = brut.get("mode")
        titre = brut.get("title")
        avertissements = brut.get("warnings")
        if not isinstance(contenu, str) or not contenu.strip():
            return None
        if mode not in MODES_CONNUS or not isinstance(titre, str):
            return None
        if not _est_liste_de_textes(avertissements):
            return None

        recuperee_le = _lire_horodatage(brut.get("fetched_at"))
        if recuperee_le is None:
            return None
        age = _maintenant() - recuperee_le
        # Un age negatif denonce une entree horodatee dans le futur, donc
        # corrompue : elle ne doit pas beneficier d'une duree de vie infinie.
        if age < timedelta(0) or age > self.duree_de_vie:
            return None

        stockee = brut.get("url")
        return PageContent(
            url=stockee if isinstance(stockee, str) and stockee else url,
            content=contenu,
            title=titre,
            mode=mode,
            warnings=(*avertissements, _note_de_cache(recuperee_le)),
        )

    def ecrire(self, page: PageContent) -> bool:
        """N'enregistre qu'un succes, et ne propage jamais un echec d'ecriture."""
        canonique = _cle(page.url)
        if not canonique or not page.content.strip():
            return False
        if page.mode not in MODES_CONNUS:
            return False
        charge = {
            "version": SCHEMA_VERSION,
            "url": page.url,
            "canonical": canonique,
            "fetched_at": _maintenant().isoformat(timespec="seconds"),
            "content": page.content,
            "title": page.title,
            "mode": page.mode,
            "warnings": list(page.warnings),
        }
        try:
            self.dossier.mkdir(parents=True, exist_ok=True)
            _ecrire_atomiquement(self._chemin(canonique), charge)
        except (OSError, UnicodeError, TypeError, ValueError) as error:
            LOGGER.warning("cache de pages : ecriture impossible (%s)", error)
            return False
        return True


@dataclass(frozen=True)
class FetcherAvecCache:
    """Interpose le cache devant un recuperateur sans toucher a son contrat."""

    fetcher: object
    cache: CachePages

    def fetch(self, url: str) -> PageContent:
        stockee = self.cache.lire(url)
        if stockee is not None:
            LOGGER.debug("cache de pages : %s servie depuis le disque", url)
            return stockee
        # `PageFetchError` remonte telle quelle : un echec ne s'ecrit pas.
        page = self.fetcher.fetch(url)
        self.cache.ecrire(page)
        return page


def cache_depuis_config(config) -> CachePages | None:
    """Rend `None` quand le cache est desactive, par dossier vide ou duree nulle."""
    dossier = (getattr(config, "page_cache_dir", "") or "").strip()
    duree = float(getattr(config, "page_cache_ttl_hours", 0.0) or 0.0)
    if not dossier or duree <= 0:
        return None
    return CachePages(
        dossier=Path(dossier).expanduser(),
        duree_de_vie=timedelta(hours=duree),
    )


def avec_cache(fetcher, config):
    """Laisse le recuperateur nu quand le cache est desactive."""
    cache = cache_depuis_config(config)
    if cache is None:
        return fetcher
    return FetcherAvecCache(fetcher=fetcher, cache=cache)
