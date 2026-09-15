# -*- coding: utf-8 -*-
"""Les fiches PDF : trouvees par le moteur, jetees avant toute recuperation.

Mesure du 2026-08-20 sur `NV1T05BD -> Norel`. SearXNG ramenait
`https://library.e.norel.example/public/.../4KBC101401D0201.pdf` — la fiche
technique officielle de l'XZ07, sur le serveur documentaire d'Norel, sans
anti-robot ni rendu JS. `WebPDFScraper` en tire 43 146 caracteres portant
`XZ07`, `24 V`, `coil`, `AC-3`, `9 A` et `690`, soit tous les criteres que la
mission tentait d'arracher a des distributeurs qui repondaient 403.

Elle n'etait jamais recuperee : `select_hit_contexts` filtrait sur
`url_lisible`, ecrit pour proteger le ChromiumLoader de ScrapeGraphAI, qui
bloque effectivement sur un PDF jusqu'au timeout. Or B2 ne navigue pas ses
PDF, il les extrait — les deux questions sont distinctes :

    url_lisible     un navigateur sait-il rendre cette URL ?      (PDF : non)
    url_exploitable B2 sait-il en tirer du texte ?                (PDF : oui)
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import robustesse
from modeles import SearchHit
from recherche_adaptative import HitContext, select_hit_contexts
from scraping import PageContent, PageFetchError, PageFetcher

FICHE_Norel = (
    "https://library.e.norel.example/public/e9e16115249c6c27c12578610033fd77"
    "/4KBC101401D0201.pdf"
)

TEXTE = (
    "XZ07-20-10-11 3-pole Contactor. Coil voltage 24 V DC. "
    "Rated operational current AC-3 : 9 A. Rated voltage 690 V. " * 4
)


# --------------------------------------------------------------------------
# Deux questions distinctes, deux predicats


def test_url_exploitable_retient_la_fiche_pdf_ecartee_sur_la_mission():
    """Mutation détectée : la fiche technique du fabricant reste inatteignable."""
    assert robustesse.url_exploitable(FICHE_Norel)


def test_url_lisible_continue_d_ecarter_le_pdf_pour_scrapegraphai():
    """Le ChromiumLoader bloque sur un PDF jusqu'au timeout : rien ne change pour lui."""
    assert not robustesse.url_lisible(FICHE_Norel)
    assert robustesse.filtrer_urls([FICHE_Norel]) == []


@pytest.mark.parametrize("url", [
    "https://assets.dorval.example/general/cession/autre/3d_lg-080054.stp",
    "https://exemple.fr/plan.dwg?v=2",
    "https://exemple.fr/archive.zip#bloc",
    "https://www.administration.example/files/BPU21102025-def.ods",
])
def test_url_exploitable_ecarte_toujours_cao_archives_et_bureautique(url):
    """Le .stp reste le cas reel qui avait fait tomber un run entier."""
    assert not robustesse.url_exploitable(url)


@pytest.mark.parametrize("url", [
    "https://www.dorval.example/produits/disjoncteur-dx3-16a",
    "https://new.norel.example/products/4KBL137001R1110/xz07-20-10-11",
])
def test_url_exploitable_conserve_les_pages_ordinaires(url):
    assert robustesse.url_exploitable(url)


# --------------------------------------------------------------------------
# La selection laisse desormais passer la fiche


def _context(url: str, rank: int = 1) -> HitContext:
    return HitContext(
        wave=1,
        query="Norel XZ07-20-10-11 technical data coil voltage",
        candidate_keys=(),
        hit=SearchHit(url=url, title="Norel Technical Datasheet", engine="bing", rank=rank),
    )


def test_select_hit_contexts_retient_la_fiche_pdf_du_fabricant():
    """Mutation détectée : la seule source non bloquee est filtree avant fetch."""
    selected = select_hit_contexts(
        [[
            _context("https://new.norel.example/products/xz07-20-10-11", rank=1),
            _context(FICHE_Norel, rank=2),
        ]],
        seen_urls=set(),
        limit=4,
    )

    assert FICHE_Norel in [item.url for item in selected]


def test_select_hit_contexts_ecarte_encore_un_modele_3d():
    """La CAO reste hors de portee : B2 ne sait pas la lire."""
    cao = "https://assets.dorval.example/general/cession/autre/3d_lg-080054.stp"

    selected = select_hit_contexts(
        [[_context(cao)]], seen_urls=set(), limit=4
    )

    assert selected == []


# --------------------------------------------------------------------------
# Le fetcher route le PDF vers l'extracteur, sans navigateur


class _Extracteur:
    def __init__(self, page: PageContent | None = None, error: Exception | None = None):
        self.page = page
        self.error = error
        self.calls: list[str] = []

    def scrape(self, url: str) -> PageContent:
        self.calls.append(url)
        if self.error:
            raise self.error
        assert self.page is not None
        return self.page


class _Interdit:
    """Tout appel navigateur sur une URL de PDF est une regression."""

    def get(self, url, **kwargs):
        raise AssertionError(f"navigateur ouvert sur un PDF : {url}")

    fetch = get


def _fetcher(**kwargs) -> PageFetcher:
    interdit = _Interdit()
    kwargs.setdefault("fast", interdit)
    kwargs.setdefault("stealthy", interdit)
    kwargs.setdefault("camoufox", interdit)
    return PageFetcher(rate_limit_delay=0, **kwargs)


def test_une_url_pdf_va_droit_a_l_extracteur_sans_ouvrir_de_navigateur():
    """Mutation détectée : le PDF part dans l'echelle navigateur qui bloque dessus."""
    extracteur = _Extracteur(
        PageContent(url=FICHE_Norel, content=TEXTE, title="Technical Datasheet", mode="pdf")
    )

    page = _fetcher(pdf_scraper=extracteur).fetch(FICHE_Norel)

    assert extracteur.calls == [FICHE_Norel]
    assert page.mode == "pdf"
    assert "Coil voltage 24 V DC" in page.content


def test_l_essai_pdf_est_journalise_comme_les_autres_barreaux(caplog):
    """Un mode muet dans le journal redevient un echec sans cause."""
    extracteur = _Extracteur(
        PageContent(url=FICHE_Norel, content=TEXTE, mode="pdf")
    )

    with caplog.at_level(logging.DEBUG, logger="scraping"):
        _fetcher(pdf_scraper=extracteur).fetch(FICHE_Norel)

    journal = [record.fetch_attempt for record in caplog.records]
    assert [item.mode for item in journal] == ["pdf"]
    assert journal[0].outcome == "success"
    assert journal[0].text_length == len(TEXTE)
    # Le barreau document n'a pas de statut HTTP a rapporter : l'annoncer
    # `HTTP None` ferait passer une extraction reussie pour une reponse cassee.
    assert "HTTP None" not in journal[0].describe()
    assert f"{len(TEXTE)} caracteres utiles" in journal[0].describe()


def test_un_pdf_refuse_devient_une_trace_datee_et_non_un_arret():
    """Mesure : `library.e.norel.example` rend aussi des 403 sur certains documents."""
    extracteur = _Extracteur(error=RuntimeError("403 Client Error: Forbidden"))

    with pytest.raises(PageFetchError) as raised:
        _fetcher(pdf_scraper=extracteur).fetch(FICHE_Norel)

    attempt = raised.value.attempts[-1]
    assert [item.mode for item in raised.value.attempts] == ["pdf"]
    assert attempt.outcome == "exception"
    assert attempt.error_type == "RuntimeError"
    assert "403" in attempt.error_message


def test_un_pdf_sans_texte_est_refuse_comme_une_page_vide():
    """Un scan sans couche texte ne doit pas passer pour une preuve."""
    extracteur = _Extracteur(PageContent(url=FICHE_Norel, content="  ", mode="pdf"))

    with pytest.raises(PageFetchError) as raised:
        _fetcher(pdf_scraper=extracteur).fetch(FICHE_Norel)

    assert raised.value.attempts[-1].outcome == "empty_content"


@pytest.mark.parametrize("url", [
    "https://library.e.norel.example/public/xyz/4KBC101401D0201.PDF",
    "https://exemple.fr/fiche.pdf?download=1",
])
def test_la_route_pdf_suit_l_extension_quelle_que_soit_sa_forme(url):
    extracteur = _Extracteur(PageContent(url=url, content=TEXTE, mode="pdf"))

    page = _fetcher(pdf_scraper=extracteur).fetch(url)

    assert page.mode == "pdf"
    assert extracteur.calls == [url]


def test_une_page_html_ne_passe_jamais_par_l_extracteur_pdf():
    """Le routage se fait sur le document, pas sur tout ce qui est recupere."""
    extracteur = _Extracteur(PageContent(url="", content=TEXTE, mode="pdf"))
    html = f"<html><head><title>XZ07</title></head><body><main>{TEXTE}</main></body></html>"

    class Fast:
        def get(self, url, **kwargs):
            from types import SimpleNamespace

            return SimpleNamespace(
                status=200, html_content=html, headers={"content-type": "text/html"}
            )

    page = PageFetcher(
        fast=Fast(), stealthy=_Interdit(), pdf_scraper=extracteur, rate_limit_delay=0
    ).fetch("https://new.norel.example/products/xz07-20-10-11")

    assert page.mode == "scrapling"
    assert extracteur.calls == []
