# -*- coding: utf-8 -*-
"""La porte dans l'orchestrateur : ce qui est récupéré n'est pas ce qui est analysé."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from scraping import FetchAttempt, PageContent, PageFetchError
from test_recherche_ciblee import _Analyzer, _empty_analyses, _service


CHROME = (
    "Select Country Argentina Bahamas Barbados Belgium Bolivia Brazil "
    "Products Solutions Industries Services About us Careers " * 8
)

FICHE = "Maker ZX-41-7 technical data. Coil 24 V DC. Rated current 9 A."


class _ChromeFetcher:
    """Récupère toujours, ne porte jamais d'identifiant produit."""

    def __init__(self, content: str = CHROME) -> None:
        self.calls: list[str] = []
        self.content = content

    def fetch(self, url: str) -> PageContent:
        self.calls.append(url)
        return PageContent(url, self.content, "Maker | page", "scrapling")


class _OneBrokenFetcher:
    """Une seule URL est injoignable ; les autres se récupèrent normalement."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def fetch(self, url: str) -> PageContent:
        self.calls.append(url)
        if "1" in url:
            raise PageFetchError(url, (
                FetchAttempt(mode="scrapling", url=url, outcome="exception",
                             error_type="TimeoutError", error_message="expiré"),
                FetchAttempt(mode="stealthy", url=url, outcome="unusable_status",
                             status=403, content_type="text/html"),
            ))
        return PageContent(url, FICHE, "Maker | page", "scrapling")


def test_one_unreachable_page_never_kills_the_whole_wave():
    """Mutation détectée : une page injoignable détruit toute la mission.

    `executor.map` matérialise ses résultats : une exception qui remonte
    emporte le lot entier, y compris les pages correctement récupérées.
    """
    fetcher = _OneBrokenFetcher()
    service, _, _, _, _ = _service(fetcher=fetcher)

    outcome = service.run("source sheet", "Maker")

    # La mission aboutit à un état de recherche, pas à une panne d'analyse.
    assert outcome.status != "analysis_error"
    assert len(fetcher.calls) > 1
    # Le diagnostic des deux modes est conservé, pas réduit à un nom de classe.
    echecs = [w for w in outcome.diagnostics.warnings if "Page non recuperee" in w]
    assert echecs
    assert "TimeoutError" in echecs[0]
    assert "403" in echecs[0]
    # L'URL est lisible dans le diagnostic, pas remplacée par un booléen.
    assert "http" in echecs[0]


def test_pages_fetched_but_never_analyzed_are_counted_apart():
    """Mutation détectée : une vague sans page analysée ressemble à une vague vide."""
    fetcher = _ChromeFetcher()
    analyzer = _Analyzer(_empty_analyses)
    service, _, _, _, _ = _service(fetcher=fetcher, analyzer=analyzer)

    outcome = service.run("source sheet", "Maker")
    diagnostics = outcome.diagnostics

    # Le budget de récupération a bien été consommé.
    assert fetcher.calls
    assert diagnostics.pages_fetched == len(fetcher.calls)
    # Mais aucune page n'a atteint le modèle en mode preuve.
    assert diagnostics.pages_analyzed < diagnostics.pages_fetched
    # Et le rapport dit pourquoi, page par page.
    assert diagnostics.gate_rejections
    assert all(
        item["reason"] in {
            "REJECTED_FOR_PRODUCT_EVIDENCE",
            "NOT_READY_FOR_PRODUCT_EVIDENCE",
            "REJECTED_EMPTY_CONTENT",
        }
        for item in diagnostics.gate_rejections
    )


def test_counters_partition_every_fetched_page():
    """L'invariant tient de bout en bout : fetched == analyzed + rejected + not_ready."""
    service, _, _, _, _ = _service(fetcher=_ChromeFetcher())

    diagnostics = service.run("source sheet", "Maker").diagnostics

    assert diagnostics.pages_fetched == (
        diagnostics.pages_analyzed
        + diagnostics.pages_rejected_by_gate
        + diagnostics.pages_not_ready_for_evidence
    )


def test_a_page_carrying_the_reference_still_reaches_the_model():
    """Mutation détectée : la porte ferme aussi sur les pages légitimes."""
    service, _, _, _, analyzer = _service(fetcher=_ChromeFetcher(FICHE))

    diagnostics = service.run("source sheet", "Maker").diagnostics

    assert diagnostics.pages_analyzed > 0
    assert any(call["pages"] for call in analyzer.calls)


def test_gate_rejection_diagnostics_never_leak_page_content():
    """Le rejet est un diagnostic sûr : ni contenu brut, ni fragment de page."""
    service, _, _, _, _ = _service(fetcher=_ChromeFetcher())

    diagnostics = service.run("source sheet", "Maker").diagnostics

    for item in diagnostics.gate_rejections:
        assert set(item) == {
            "url", "reason", "mode", "content_length",
            "expected_identifiers", "matched_identifiers",
        }
        assert "Bahamas" not in repr(item)
