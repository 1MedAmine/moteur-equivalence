# -*- coding: utf-8 -*-
"""Filtre de pertinence par domaine : couper le bruit sans couper les preuves."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from recherche import SearchHit
from recherche_adaptative import HitContext, select_hit_contexts
from robustesse import (
    chemin_editorial,
    domaine_hors_sujet,
    domaines_exclus,
    hors_sujet,
)


#: URLs réellement ouvertes par le run du 2026-08-27 et qui ont coûté 160 s de
#: 403 pour cinq places du budget de pages.
BRUIT_MESURE = [
    "https://stackoverflow.com/questions/3421144/how-to-disable-caching",
    "https://stackoverflow.com/questions/321865/how-to-clear-a-cached-image",
    "https://es.stackoverflow.com/questions/63716/como-evitar-el-cache",
    "https://fr.wikipedia.org/wiki/Norel_(entreprise)",
    "https://careers.norel/global/en/home",
]

#: URLs du même run qui ont porté ou pouvaient porter une preuve. Aucune ne doit
#: être écartée : c'est la garantie que l'accélération ne coûte pas de qualité.
SOURCES_LEGITIMES = [
    "https://new.norel.example/products/HPL1213001R0101/kb6-20-10-01",
    "https://fr.distri-a.example/norel/hpl1213001r0101/kb6-20-10-24dc-mini/dp/4483591",
    "https://www.fiches-techniques.example/norel/hpl1213001r0101",
    "https://fr.distri-b.example/web/p/contacteurs/4457860",
    "https://www.distributeur-a.example/frx/Categorie/Produits-Industriels/Contacteur",
    "https://www.distributeur-d.example/p/contacteur-as-9a-ac3-3p1no-24vdc",
    "https://stykace.comparateur-a.example/norel-4kbl177001r1110/",
    "https://www.distributeur-b.example/catalog/fr-fr/products/norel-contacteur-as-9a",
    "https://empower.norel.example/ecatalog/ec/EN_NA/p/HPL1213001R0101/pdf",
    "https://library.e.norel.example/public/fiche.pdf",
    "https://www.comparateur-b.example/vyrobek/norel-4kbl157001r1310/",
    "https://revendeur-d.example/collections/power-contactors",
]


@pytest.mark.parametrize("url", BRUIT_MESURE)
def test_le_bruit_mesure_est_ecarte(url):
    assert domaine_hors_sujet(url) is True


@pytest.mark.parametrize("url", SOURCES_LEGITIMES)
def test_aucune_source_produit_n_est_ecartee(url):
    """Le test qui protège la qualité : un faux positif ici coûte une preuve."""
    assert domaine_hors_sujet(url) is False


def test_le_reseau_stackexchange_reste_admis():
    """`electronics.stackexchange.com` peut légitimement citer une caractéristique.

    Seuls les sites purement informatiques du réseau sont exclus.
    """
    assert domaine_hors_sujet("https://electronics.stackexchange.com/q/1") is False
    assert domaine_hors_sujet("https://superuser.com/questions/1") is True


def test_un_sous_domaine_de_recrutement_du_fabricant_est_ecarte():
    """`careers.norel` est hors sujet, `new.norel.example` ne l'est pas."""
    assert domaine_hors_sujet("https://careers.norel/global/en/home") is True
    assert domaine_hors_sujet("https://jobs.kerion-electric.example/fr") is True
    assert domaine_hors_sujet("https://new.norel.example/fr") is False


def test_le_filtre_couvre_les_sous_domaines():
    assert domaine_hors_sujet("https://fr.m.wikipedia.org/wiki/Norel") is True
    assert domaine_hors_sujet("https://www.youtube.com/watch?v=x") is True


def test_un_domaine_qui_contient_un_exclu_reste_admis():
    """`monwikipedia.org.fr` n'est pas `wikipedia.org` : la comparaison est
    ancrée sur la frontière de label, pas sur une sous-chaîne."""
    assert domaine_hors_sujet("https://notwikipedia.org/page") is False
    assert domaine_hors_sujet("https://reddit.com.exemple.fr/p") is False


@pytest.mark.parametrize("url", ["", "pas une url", "http://", "mailto:a@b.fr"])
def test_une_url_illisible_ne_leve_pas(url):
    assert domaine_hors_sujet(url) is False


def test_la_liste_est_extensible_sans_toucher_au_code(monkeypatch):
    monkeypatch.setenv("B2_EXCLUDED_DOMAINS", "exemple-bruit.fr, .autre-bruit.com")

    assert domaine_hors_sujet("https://www.exemple-bruit.fr/p") is True
    assert domaine_hors_sujet("https://autre-bruit.com/p") is True
    # L'ajout complète la liste par défaut, il ne la remplace pas.
    assert domaine_hors_sujet("https://stackoverflow.com/q/1") is True
    assert "stackoverflow.com" in domaines_exclus()


def _contexte(url: str) -> HitContext:
    return HitContext(
        wave=1,
        query="Norel contacteur 9A 24VDC",
        candidate_keys=(),
        hit=SearchHit(url=url, title="t", snippet="s", engine="e", rank=1),
    )


def test_la_selection_n_ouvre_jamais_une_page_hors_sujet():
    """Régression visée : le filtre existe mais la sélection l'ignore, et les
    pages sont ouvertes quand même — le temps est perdu avant toute analyse."""
    contextes = [[
        _contexte("https://stackoverflow.com/questions/3421144/caching"),
        _contexte("https://fr.distri-a.example/norel/hpl1213001r0101/dp/4483591"),
        _contexte("https://fr.wikipedia.org/wiki/Norel_(entreprise)"),
    ]]

    retenues = select_hit_contexts(contextes, seen_urls=set(), limit=10)

    assert [item.url for item in retenues] == [
        "https://fr.distri-a.example/norel/hpl1213001r0101/dp/4483591"
    ]


def test_le_budget_de_pages_profite_du_filtre():
    """Les places libérées vont aux pages produit suivantes, pas au néant."""
    contextes = [[
        _contexte("https://stackoverflow.com/questions/1/a"),
        _contexte("https://stackoverflow.com/questions/2/b"),
        _contexte("https://fr.distri-a.example/norel/ref1/dp/1"),
        _contexte("https://fr.distri-b.example/web/p/contacteurs/2"),
    ]]

    retenues = select_hit_contexts(contextes, seen_urls=set(), limit=2)

    assert [item.url for item in retenues] == [
        "https://fr.distri-a.example/norel/ref1/dp/1",
        "https://fr.distri-b.example/web/p/contacteurs/2",
    ]


#: Pages pedagogiques reellement ouvertes par le run du 2026-08-28, qui a rendu
#: `rejected` apres avoir consomme son budget dessus. Toutes sont publiees sur
#: des domaines electriques legitimes : aucune liste de domaines ne peut les
#: atteindre.
CONTENU_EXPLICATIF = [
    "https://www.contenu-a.example/fr/what-is-a-contactor-in-electrical/",
    "https://www.contenu-d.example/glossaire-electricite/composants",
    "https://www.contenu-b.example/blog/qu-est-ce-qu-un-contacteur-moteur.html",
    "https://www.kr.example/fr/fr/work/products/product-launch/guides/contacteur/",
    "https://contenu-c.example/definition-dun-contacteur/",
    "https://www.contenu-e.example/encyc/encyc.pdf",
    "https://www.forum-a.example/forum/",
]


@pytest.mark.parametrize("url", CONTENU_EXPLICATIF)
def test_le_contenu_explicatif_est_ecarte(url):
    assert chemin_editorial(url) is True
    assert hors_sujet(url) is True


@pytest.mark.parametrize("url", SOURCES_LEGITIMES)
def test_aucune_page_produit_ne_passe_pour_editoriale(url):
    """Le second garde-fou de qualite : verifie contre les URLs reellement
    ouvertes des deux runs, ce motif ne touche aucune page produit."""
    assert chemin_editorial(url) is False
    assert hors_sujet(url) is False


@pytest.mark.parametrize("url", BRUIT_MESURE)
def test_le_predicat_combine_couvre_aussi_les_domaines(url):
    assert hors_sujet(url) is True
