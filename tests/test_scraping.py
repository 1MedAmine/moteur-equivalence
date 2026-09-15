# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scraping import (
    MIN_CONTENT_LENGTH,
    B2RateLimiter,
    PageFetchError,
    PageFetcher,
)


HTML = "<html><head><title>REF-1</title></head><body>" + (
    "Caractéristique technique 9 A bobine 24 V DC. " * 8
) + "</body></html>"


class Fast:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        return self.response


class Stealthy:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def fetch(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        return self.response


def response(html=HTML, status=200, content_type="text/html"):
    return SimpleNamespace(
        status=status,
        html_content=html,
        headers={"content-type": content_type},
    )


def test_fast_fetcher_success_stops_the_ladder():
    """Mutation détectée : le navigateur furtif est lancé malgré un contenu rapide utile."""
    fast = Fast(response())
    stealthy = Stealthy(error=AssertionError("ne doit pas être appelé"))

    page = PageFetcher(fast=fast, stealthy=stealthy, rate_limit_delay=0).fetch(
        "https://maker.example/ref"
    )

    assert page.mode == "scrapling"
    assert "bobine 24 V DC" in page.content
    assert page.title == "REF-1"
    assert len(fast.calls) == 1
    assert stealthy.calls == []
    assert fast.calls[0][1]["timeout"] == 25.0
    assert fast.calls[0][1]["follow_redirects"] == "safe"


def test_short_fast_content_uses_stealthy_then_stops():
    """Mutation détectée : une page vide est envoyée à ScrapeGraphAI sans second essai."""
    fast = Fast(response("<html><body>vide</body></html>"))
    stealthy = Stealthy(response())

    page = PageFetcher(fast=fast, stealthy=stealthy, rate_limit_delay=0).fetch(
        "https://maker.example/ref"
    )

    assert page.mode == "stealthy"
    assert len(stealthy.calls) == 1
    assert stealthy.calls[0][1]["timeout"] == 25_000
    # Scrapling refuse `retries=0` (`Expected int >= 1`) : le barreau furtif
    # mourait en TypeError avant d'ouvrir un navigateur.
    assert stealthy.calls[0][1]["retries"] >= 1


def test_fast_exception_is_logged_without_stopping_the_ladder(caplog):
    """Mutation détectée : une exception en fast avale la page au lieu de tenter stealth."""
    fast = Fast(error=TimeoutError("fast a expiré"))
    stealthy = Stealthy(response())

    with caplog.at_level(logging.WARNING, logger="scraping"):
        page = PageFetcher(fast=fast, stealthy=stealthy, rate_limit_delay=0).fetch(
            "https://maker.example/ref"
        )

    assert page.mode == "stealthy"
    assert "bobine 24 V DC" in page.content
    assert len(stealthy.calls) == 1

    journal = [record.fetch_attempt for record in caplog.records]
    assert [entry.mode for entry in journal] == ["scrapling"]
    assert journal[0].outcome == "exception"
    assert journal[0].error_type == "TimeoutError"
    assert "fast a expiré" in journal[0].error_message


def test_both_modes_failing_raise_one_error_carrying_both_diagnostics():
    """Mutation détectée : le double échec repart en scrapegraph_url et perd sa cause."""
    fast = Fast(error=TimeoutError("fast a expiré"))
    stealthy = Stealthy(response(status=403, content_type="text/html"))
    # Le 403 arme le troisième barreau : sans double, ce test ouvrirait un
    # vrai navigateur Camoufox sur un domaine fictif.
    camoufox = Stealthy(response(status=403, content_type="text/html"))

    with pytest.raises(PageFetchError) as raised:
        PageFetcher(
            fast=fast, stealthy=stealthy, camoufox=camoufox, rate_limit_delay=0
        ).fetch("https://maker.example/ref")

    error = raised.value
    assert error.url == "https://maker.example/ref"
    assert [attempt.mode for attempt in error.attempts] == [
        "scrapling", "stealthy", "camoufox",
    ]

    fast_attempt, stealthy_attempt, _ = error.attempts
    assert fast_attempt.outcome == "exception"
    assert fast_attempt.error_type == "TimeoutError"

    # La réponse HTTP inexploitable reste dans le journal, statut et type inclus.
    assert stealthy_attempt.outcome == "unusable_status"
    assert stealthy_attempt.status == 403
    assert stealthy_attempt.content_type == "text/html"
    assert stealthy_attempt.html_length > 0

    assert "TimeoutError" in str(error)
    assert "403" in str(error)


PRODUIT = (
    "Contacteur XZ07-20-10-11. Bobine 24 V DC. Courant AC-3 : 9 A. "
    "Tension assignée d'emploi 690 V. " * 3
)

PAGE_AVEC_CHROME = f"""
<html><head><title>XZ07-20-10-11 | Norel</title></head><body>
  <header>Products &amp; Solutions Industries Services About us Careers</header>
  <nav>Control Room Control Systems Drives E-Mobility Robotics</nav>
  <div role="navigation">Low Voltage Products Measurement and Analytics</div>
  <main><article>{PRODUIT}</article></main>
  <footer>Contact us Where to buy Mentions légales Cookies</footer>
</body></html>
"""


def test_navigation_chrome_is_stripped_and_product_content_is_kept():
    """Mutation détectée : le menu global passe pour du contenu produit."""
    fast = Fast(response(PAGE_AVEC_CHROME))

    page = PageFetcher(fast=fast, stealthy=Stealthy(), rate_limit_delay=0).fetch(
        "https://maker.example/ref"
    )

    # nav, header, footer et [role=navigation] : supprimés.
    for chrome in ("Control Systems", "E-Mobility", "About us", "Where to buy",
                   "Measurement and Analytics", "Mentions légales"):
        assert chrome not in page.content

    # main/article : conservé, référence comprise.
    assert "XZ07-20-10-11" in page.content
    assert "Bobine 24 V DC" in page.content
    assert "AC-3 : 9 A" in page.content
    # Le titre reste lisible : il vient du <head>, hors des repères retirés.
    assert page.title == "XZ07-20-10-11 | Norel"


def test_page_reduced_to_navigation_becomes_unusable():
    """Mutation détectée : 22 k caractères de menu restent une preuve potentielle."""
    menu = "Control Room Control Systems Drives E-Mobility Robotics " * 200
    chrome_seul = (
        f"<html><head><title>XZ07-20-10-11 | Norel</title></head><body>"
        f"<header>{menu}</header><nav>{menu}</nav>"
        f"<main><p>Haven't found product you are looking for ?</p></main>"
        f"<footer>{menu}</footer></body></html>"
    )
    fast = Fast(response(chrome_seul))
    stealthy = Stealthy(response(chrome_seul))

    with pytest.raises(PageFetchError) as raised:
        PageFetcher(fast=fast, stealthy=stealthy, rate_limit_delay=0).fetch(
            "https://maker.example/ref"
        )

    # Le menu pesait largement plus que le seuil ; une fois retiré, il ne reste
    # rien de prouvable et la page est refusée au lieu d'être auditée.
    assert all(attempt.outcome == "empty_content" for attempt in raised.value.attempts)
    assert all(attempt.html_length > MIN_CONTENT_LENGTH for attempt in raised.value.attempts)
    assert all(attempt.text_length < MIN_CONTENT_LENGTH for attempt in raised.value.attempts)


def test_safe_head_identity_is_preserved_without_importing_arbitrary_scripts():
    """Mutation détectée : l'identité Norel du head disparaît du contenu auditable."""
    generic_body = "Distributor inventory search Select country Contact us. " * 8
    norel_html = f"""
    <html>
      <head>
        <title>XZ07-20-10-11 | Norel</title>
        <script>window.secret = "NE_DOIT_PAS_SORTIR";</script>
        <script type="application/ld+json">
          {{
            "@context": "https://schema.org",
            "@type": "BreadcrumbList",
            "description": "DESCRIPTION_NON_AUTORISEE",
            "itemListElement": [
              {{
                "@type": "ListItem",
                "position": 2,
                "name": "4KBL137001R1110",
                "item": "https://new.norel.example/products/fr-lu/4KBL137001R1110/xz07-20-10-11"
              }}
            ]
          }}
        </script>
      </head>
      <body><main>{generic_body}</main></body>
    </html>
    """

    page = PageFetcher(
        fast=Fast(response(norel_html)),
        stealthy=Stealthy(error=AssertionError("ne doit pas être appelé")),
        rate_limit_delay=0,
    ).fetch("https://new.norel.example/products/fr-lu/4KBL137001R1110/xz07-20-10-11")

    assert "XZ07-20-10-11" in page.content
    assert "4KBL137001R1110" in page.content
    assert "DESCRIPTION_NON_AUTORISEE" not in page.content
    assert "NE_DOIT_PAS_SORTIR" not in page.content


def test_rate_limiter_spaces_shared_attempts():
    """Mutation détectée : deux workers frappent les sites au même instant."""
    clock_values = iter([0.0, 0.1])
    sleeps = []
    limiter = B2RateLimiter(clock=lambda: next(clock_values), sleeper=sleeps.append)

    limiter.wait(0.5)
    limiter.wait(0.5)

    assert sleeps == [0.4]
