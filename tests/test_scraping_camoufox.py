# -*- coding: utf-8 -*-
"""Troisième barreau : Camoufox contre les blocages anti-robot.

Mesure du 2026-08-19 sur `NV1T05BD -> Norel` : plusieurs distributeurs — RS
Online, Distri E, Revendeur F — rendaient 403 sur les deux modes existants.
Scrapling 0.4.14 pilote Chromium via patchright ; Camoufox est un moteur
Firefox distinct, donc une surface d'empreinte différente.

Il n'est appelé que sur un blocage anti-robot avéré : c'est le mode le plus
lent (vingt à quarante secondes), et le déclencher sur une page simplement
vide gaspillerait le budget.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scraping import (
    BOT_PROTECTION_STATUSES,
    PageFetchError,
    PageFetcher,
)


HTML = "<html><head><title>REF-1</title></head><body><main>" + (
    "Caractéristique technique 9 A bobine 24 V DC. " * 8
) + "</main></body></html>"


class _Fetcher:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        return self.response

    # Stealthy et Camoufox exposent `fetch`.
    def fetch(self, url, **kwargs):
        return self.get(url, **kwargs)


def _reponse(html=HTML, status=200, content_type="text/html"):
    return SimpleNamespace(
        status=status,
        html_content=html,
        headers={"content-type": content_type},
    )


def _fetcher(*, fast, stealthy, camoufox):
    return PageFetcher(
        fast=fast, stealthy=stealthy, camoufox=camoufox, rate_limit_delay=0
    )


def test_bot_protection_statuses_are_named_not_scattered():
    assert 403 in BOT_PROTECTION_STATUSES


def test_a_403_on_both_rungs_calls_camoufox():
    """Mutation détectée : un blocage anti-robot abandonne la page."""
    fast = _Fetcher(_reponse(status=403))
    stealthy = _Fetcher(_reponse(status=403))
    camoufox = _Fetcher(_reponse())

    page = _fetcher(fast=fast, stealthy=stealthy, camoufox=camoufox).fetch(
        "https://distributeur.example/ref"
    )

    assert len(camoufox.calls) == 1
    assert page.mode == "camoufox"
    assert "bobine 24 V DC" in page.content


def test_camoufox_is_not_automatic_without_an_explicit_fetcher():
    """Le navigateur tiers peut bloquer hors de son timeout ; il est opt-in."""
    fetcher = PageFetcher(
        fast=_Fetcher(_reponse(status=403)),
        stealthy=_Fetcher(_reponse(status=403)),
        rate_limit_delay=0,
    )
    fetcher._camoufox_fetcher = lambda: (_ for _ in ()).throw(
        AssertionError("Camoufox ne doit pas demarrer automatiquement")
    )

    with pytest.raises(PageFetchError) as raised:
        fetcher.fetch("https://distributeur.example/ref")

    assert [item.mode for item in raised.value.attempts] == ["scrapling", "stealthy"]


def test_camoufox_is_not_called_when_the_page_is_merely_empty():
    """Le mode le plus lent ne doit pas servir là où il n'apporte rien."""
    vide = _reponse("<html><body><main>court</main></body></html>")
    fast = _Fetcher(vide)
    stealthy = _Fetcher(vide)
    camoufox = _Fetcher(_reponse())

    with pytest.raises(PageFetchError) as raised:
        _fetcher(fast=fast, stealthy=stealthy, camoufox=camoufox).fetch(
            "https://maker.example/ref"
        )

    assert camoufox.calls == []
    assert [item.mode for item in raised.value.attempts] == ["scrapling", "stealthy"]


def test_camoufox_is_never_called_when_an_earlier_rung_succeeds():
    fast = _Fetcher(_reponse())
    camoufox = _Fetcher(_reponse())

    page = _fetcher(
        fast=fast,
        stealthy=_Fetcher(error=AssertionError("ne doit pas être appelé")),
        camoufox=camoufox,
    ).fetch("https://maker.example/ref")

    assert page.mode == "scrapling"
    assert camoufox.calls == []


def test_a_failing_camoufox_keeps_the_three_diagnostics():
    """Mutation détectée : le troisième échec efface les deux premiers."""
    fast = _Fetcher(_reponse(status=403))
    stealthy = _Fetcher(_reponse(status=403))
    camoufox = _Fetcher(_reponse(status=403))

    with pytest.raises(PageFetchError) as raised:
        _fetcher(fast=fast, stealthy=stealthy, camoufox=camoufox).fetch(
            "https://distributeur.example/ref"
        )

    error = raised.value
    assert [item.mode for item in error.attempts] == [
        "scrapling", "stealthy", "camoufox",
    ]
    assert all(item.status == 403 for item in error.attempts)
    assert str(error).count("403") >= 3


def test_a_camoufox_exception_is_journaled_and_never_swallowed(caplog):
    fast = _Fetcher(_reponse(status=403))
    stealthy = _Fetcher(_reponse(status=403))
    camoufox = _Fetcher(error=TimeoutError("navigateur indisponible"))

    with caplog.at_level(logging.WARNING, logger="scraping"):
        with pytest.raises(PageFetchError) as raised:
            _fetcher(fast=fast, stealthy=stealthy, camoufox=camoufox).fetch(
                "https://distributeur.example/ref"
            )

    dernier = raised.value.attempts[-1]
    assert dernier.mode == "camoufox"
    assert dernier.outcome == "exception"
    assert dernier.error_type == "TimeoutError"
    assert any(
        record.fetch_attempt.mode == "camoufox" for record in caplog.records
    )


@pytest.mark.parametrize("statut", sorted(BOT_PROTECTION_STATUSES))
def test_every_bot_protection_status_triggers_the_third_rung(statut):
    camoufox = _Fetcher(_reponse())

    page = _fetcher(
        fast=_Fetcher(_reponse(status=statut)),
        stealthy=_Fetcher(_reponse(status=statut)),
        camoufox=camoufox,
    ).fetch("https://distributeur.example/ref")

    assert len(camoufox.calls) == 1
    assert page.mode == "camoufox"


def test_a_plain_404_does_not_trigger_camoufox():
    """Une page absente n'est pas un blocage : rien à contourner."""
    camoufox = _Fetcher(_reponse())

    with pytest.raises(PageFetchError):
        _fetcher(
            fast=_Fetcher(_reponse(status=404)),
            stealthy=_Fetcher(_reponse(status=404)),
            camoufox=camoufox,
        ).fetch("https://maker.example/absent")

    assert camoufox.calls == []


def test_un_hote_qui_a_deja_refuse_camoufox_ne_le_repaye_pas():
    """Regression visee : chaque page d'un hote bloque repaye les 45 s de Camoufox.

    Mesure du run du 2026-08-27 : quatre pages Stack Overflow et trois pages
    Distri B ont chacune subi l'echelle complete pour le meme 403.
    """
    bloque = _reponse(status=403)
    camoufox = _Fetcher(response=bloque)
    fetcher = _fetcher(
        fast=_Fetcher(response=bloque),
        stealthy=_Fetcher(response=bloque),
        camoufox=camoufox,
    )

    for chemin_page in ("/questions/1", "/questions/2", "/questions/3"):
        with pytest.raises(PageFetchError):
            fetcher.fetch(f"https://exemple-bloque.fr{chemin_page}")

    assert len(camoufox.calls) == 1


def test_un_autre_hote_garde_son_droit_a_camoufox():
    """Le souvenir est par hote : il ne doit pas condamner tout le run."""
    bloque = _reponse(status=403)
    camoufox = _Fetcher(response=bloque)
    fetcher = _fetcher(
        fast=_Fetcher(response=bloque),
        stealthy=_Fetcher(response=bloque),
        camoufox=camoufox,
    )

    for hote in ("premier.fr", "second.fr", "premier.fr"):
        with pytest.raises(PageFetchError):
            fetcher.fetch(f"https://{hote}/p")

    assert len(camoufox.calls) == 2


def test_le_bannissement_vaut_pour_le_reste_du_run():
    """Comportement assume : aucune reprise, car l'hote ecarte n'est plus
    sollicite et ne peut donc plus prouver qu'il repond.

    Les deux modes rapides, eux, continuent d'etre payes a chaque page : c'est
    ce qui laisse une chance a un chemin different sur le meme hote.
    """
    rapide = _Fetcher(response=_reponse(status=403))
    camoufox = _Fetcher(response=_reponse(status=403))
    fetcher = _fetcher(
        fast=rapide,
        stealthy=_Fetcher(response=_reponse(status=403)),
        camoufox=camoufox,
    )

    for index in range(4):
        with pytest.raises(PageFetchError):
            fetcher.fetch(f"https://bloque.fr/page-{index}")

    assert len(camoufox.calls) == 1
    assert len(rapide.calls) == 4
