# -*- coding: utf-8 -*-
"""Contrat du cache de pages : servir les succès, ne jamais figer un échec."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import get_args, get_type_hints

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cache_pages
import configuration
import scraping
from cache_pages import CachePages, FetcherAvecCache, avec_cache
from scraping import FetchAttempt, PageContent, PageFetchError


class _Compteur:
    """Récupérateur qui dénonce chaque aller sur le réseau."""

    def __init__(self, page: PageContent | None = None, erreur: Exception | None = None):
        self.appels: list[str] = []
        self._page = page
        self._erreur = erreur

    def fetch(self, url: str) -> PageContent:
        self.appels.append(url)
        if self._erreur is not None:
            raise self._erreur
        return self._page or PageContent(url=url, content="contenu utile", title="T")


def _cache(tmp_path: Path, heures: float = 168.0) -> CachePages:
    return CachePages(dossier=tmp_path / "pages", duree_de_vie=timedelta(hours=heures))


def _entree(tmp_path: Path) -> Path:
    return next((tmp_path / "pages").glob("*.json"))


def test_le_second_appel_ne_touche_plus_le_reseau(tmp_path):
    """C'est la raison d'être du cache : deux runs voient la même page."""
    fetcher = _Compteur()
    enrobe = FetcherAvecCache(fetcher=fetcher, cache=_cache(tmp_path))

    premiere = enrobe.fetch("https://exemple.fr/produit")
    seconde = enrobe.fetch("https://exemple.fr/produit")

    assert fetcher.appels == ["https://exemple.fr/produit"]
    assert seconde.content == premiere.content
    assert seconde.title == premiere.title
    assert seconde.mode == premiere.mode


def test_la_page_servie_signale_son_origine(tmp_path):
    """Une preuve relue sur disque ne doit pas se faire passer pour fraîche."""
    enrobe = FetcherAvecCache(fetcher=_Compteur(), cache=_cache(tmp_path))
    enrobe.fetch("https://exemple.fr/produit")

    relue = enrobe.fetch("https://exemple.fr/produit")

    assert any("cache local" in avertissement for avertissement in relue.warnings)


def test_un_echec_reseau_n_est_jamais_mis_en_cache(tmp_path):
    """Régression visée : un 403 transitoire fige la page pour sept jours."""
    echec = PageFetchError(
        "https://exemple.fr/bloque",
        (
            FetchAttempt(
                mode="scrapling",
                url="https://exemple.fr/bloque",
                outcome="unusable_status",
                status=403,
            ),
        ),
    )
    fetcher = _Compteur(erreur=echec)
    enrobe = FetcherAvecCache(fetcher=fetcher, cache=_cache(tmp_path))

    for _ in range(2):
        with pytest.raises(PageFetchError):
            enrobe.fetch("https://exemple.fr/bloque")

    assert len(fetcher.appels) == 2


def test_une_page_vide_n_est_pas_un_succes(tmp_path):
    """`_fetch_page` convertit un échec en contenu vide : il ne doit pas rester."""
    vide = PageContent(url="https://exemple.fr/vide", content="   ", mode="scrapegraph_url")
    fetcher = _Compteur(page=vide)
    enrobe = FetcherAvecCache(fetcher=fetcher, cache=_cache(tmp_path))

    enrobe.fetch("https://exemple.fr/vide")
    enrobe.fetch("https://exemple.fr/vide")

    assert len(fetcher.appels) == 2


def test_une_entree_perimee_repart_sur_le_reseau(tmp_path):
    fetcher = _Compteur()
    enrobe = FetcherAvecCache(fetcher=fetcher, cache=_cache(tmp_path))
    enrobe.fetch("https://exemple.fr/produit")

    chemin = _entree(tmp_path)
    charge = json.loads(chemin.read_text(encoding="utf-8"))
    charge["fetched_at"] = (
        datetime.now(timezone.utc) - timedelta(days=30)
    ).isoformat(timespec="seconds")
    chemin.write_text(json.dumps(charge), encoding="utf-8")

    enrobe.fetch("https://exemple.fr/produit")

    assert len(fetcher.appels) == 2


def test_une_entree_horodatee_dans_le_futur_est_refusee(tmp_path):
    """Sans ce refus, une entrée corrompue vivrait indéfiniment."""
    fetcher = _Compteur()
    enrobe = FetcherAvecCache(fetcher=fetcher, cache=_cache(tmp_path))
    enrobe.fetch("https://exemple.fr/produit")

    chemin = _entree(tmp_path)
    charge = json.loads(chemin.read_text(encoding="utf-8"))
    charge["fetched_at"] = (
        datetime.now(timezone.utc) + timedelta(days=1)
    ).isoformat(timespec="seconds")
    chemin.write_text(json.dumps(charge), encoding="utf-8")

    enrobe.fetch("https://exemple.fr/produit")

    assert len(fetcher.appels) == 2


CORRUPTIONS = [
    "{ pas du json",
    json.dumps({"version": 999, "content": "x"}),
    json.dumps({
        "version": 1, "content": "x", "mode": "inconnu",
        "title": "", "warnings": [], "fetched_at": "2026-08-27T00:00:00+00:00",
    }),
    json.dumps({
        "version": 1, "content": "  ", "mode": "scrapling",
        "title": "", "warnings": [], "fetched_at": "2026-08-27T00:00:00+00:00",
    }),
    json.dumps({
        "version": 1, "content": "x", "mode": "scrapling",
        "title": 4, "warnings": [], "fetched_at": "2026-08-27T00:00:00+00:00",
    }),
    json.dumps({
        "version": 1, "content": "x", "mode": "scrapling",
        "title": "", "warnings": [2], "fetched_at": "2026-08-27T00:00:00+00:00",
    }),
    json.dumps({
        "version": 1, "content": "x", "mode": "scrapling",
        "title": "", "warnings": [], "fetched_at": "hier",
    }),
]


@pytest.mark.parametrize("corruption", CORRUPTIONS)
def test_une_entree_corrompue_retombe_sur_le_reseau(tmp_path, corruption):
    """Le cache est un raccourci : son échec ne coûte qu'un aller réseau."""
    fetcher = _Compteur()
    enrobe = FetcherAvecCache(fetcher=fetcher, cache=_cache(tmp_path))
    enrobe.fetch("https://exemple.fr/produit")

    _entree(tmp_path).write_text(corruption, encoding="utf-8")

    assert enrobe.fetch("https://exemple.fr/produit").content == "contenu utile"
    assert len(fetcher.appels) == 2


def test_le_marquage_publicitaire_ne_cree_pas_une_seconde_entree(tmp_path):
    """`canonical_url` retire `utm_*` : deux liens du même produit se rejoignent."""
    fetcher = _Compteur()
    enrobe = FetcherAvecCache(fetcher=fetcher, cache=_cache(tmp_path))

    enrobe.fetch("https://exemple.fr/produit?ref=1&utm_source=searx")
    enrobe.fetch("https://exemple.fr/produit?ref=1&gclid=abc")

    assert len(fetcher.appels) == 1


def test_une_ecriture_impossible_ne_tue_pas_le_run(tmp_path):
    """Un disque plein dégrade la performance, il n'interrompt pas la recherche."""
    (tmp_path / "pages").write_text("je ne suis pas un dossier", encoding="utf-8")
    enrobe = FetcherAvecCache(fetcher=_Compteur(), cache=_cache(tmp_path))

    assert enrobe.fetch("https://exemple.fr/produit").content == "contenu utile"


@pytest.mark.parametrize("url", ["", "/produit", "javascript:void(0)", "https://"])
def test_une_url_sans_hote_ne_passe_jamais_par_le_cache(tmp_path, url):
    """Régression visée : `canonical_url` les réduit toutes à `/`, donc à une
    seule entrée partagée où une page en écraserait une autre."""
    cache = _cache(tmp_path)
    fetcher = _Compteur()
    enrobe = FetcherAvecCache(fetcher=fetcher, cache=cache)

    assert cache.lire(url) is None
    assert cache.ecrire(PageContent(url=url, content="x")) is False
    enrobe.fetch(url)
    enrobe.fetch(url)

    assert len(fetcher.appels) == 2
    assert not (tmp_path / "pages").exists()


def test_les_modes_connus_suivent_page_content():
    """Mutation détectée : un mode ajouté à `PageContent` sans l'être au cache."""
    annotations = get_type_hints(scraping.PageContent)

    assert set(get_args(annotations["mode"])) == set(cache_pages.MODES_CONNUS)


def test_le_cache_desactive_rend_le_recuperateur_nu(monkeypatch, tmp_path):
    monkeypatch.setenv("B2_PAGE_CACHE_DIR", "")
    config = configuration.B2Config.from_env(tmp_path / "absent.env")
    fetcher = _Compteur()

    assert config.page_cache_dir == ""
    assert avec_cache(fetcher, config) is fetcher


def test_une_duree_de_vie_nulle_desactive_le_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("B2_PAGE_CACHE_TTL_HOURS", "0")
    config = configuration.B2Config.from_env(tmp_path / "absent.env")
    fetcher = _Compteur()

    assert avec_cache(fetcher, config) is fetcher


def test_un_dossier_relatif_se_resout_depuis_le_module(monkeypatch, tmp_path):
    """Régression visée : le cache se disperse selon le répertoire de lancement."""
    monkeypatch.setenv("B2_PAGE_CACHE_DIR", ".cache_pages")
    config = configuration.B2Config.from_env(tmp_path / "absent.env")

    assert Path(config.page_cache_dir) == configuration.DOSSIER / ".cache_pages"


def test_le_cache_est_actif_par_defaut(monkeypatch, tmp_path):
    """La variance disparaît sans que l'utilisateur ait à s'en souvenir."""
    monkeypatch.delenv("B2_PAGE_CACHE_DIR", raising=False)
    monkeypatch.delenv("B2_PAGE_CACHE_TTL_HOURS", raising=False)
    config = configuration.B2Config.from_env(tmp_path / "absent.env")

    assert Path(config.page_cache_dir) == configuration.DOSSIER / ".cache_pages"
    assert config.page_cache_ttl_hours == 168.0
    assert isinstance(avec_cache(_Compteur(), config), FetcherAvecCache)
