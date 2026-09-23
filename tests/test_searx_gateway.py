# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from recherche import SearxGateway, SearxUnavailable, est_page_de_liste
from routage_searx import ENGINE_SHORTCUTS, EngineRotation


class FakeTransport:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def search(self, query: str, max_results: int, timeout: float):
        self.calls.append((query, max_results, timeout))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _gateway(transport, attempts=3):
    return SearxGateway(
        base_url="http://searx.test:8080",
        rotation=EngineRotation(ENGINE_SHORTCUTS),
        timeout=25.0,
        max_results=12,
        max_attempts=attempts,
        transport=transport,
    )


def test_blocked_then_empty_then_success_uses_next_engines():
    """Mutation détectée : une requête bloquée n'est pas confiée au moteur suivant."""
    transport = FakeTransport([
        TimeoutError("blocked"),
        {"results": [], "unresponsive_engines": []},
        {"results": [{
            "engine": "google cse",
            "title": "Produit industriel",
            "url": "https://maker.example/p",
            "content": "9 A, 24 V DC",
        }]},
    ])

    batch = _gateway(transport).search("contacteur industriel")

    assert [attempt.status for attempt in batch.attempts] == ["blocked", "empty", "ok"]
    assert [attempt.engine for attempt in batch.attempts] == ["bing", "duckduckgo", "google cse"]
    assert [call[0].split()[0] for call in transport.calls] == ["!bi", "!ddg", "!goc"]
    assert transport.calls[-1][1:] == (12, 25.0)
    assert batch.hits[0].snippet == "9 A, 24 V DC"


def test_foreign_engine_rows_are_rejected_before_next_attempt():
    """Mutation détectée : une réponse agrégée contourne le moteur imposé."""
    transport = FakeTransport([
        {"results": [{"engine": "duckduckgo", "url": "https://wrong.example"}]},
        {"results": [{"engine": "duckduckgo", "url": "https://right.example"}]},
    ])

    batch = _gateway(transport).search("relais 24 V")

    assert [attempt.status for attempt in batch.attempts] == ["empty", "ok"]
    assert [hit.url for hit in batch.hits] == ["https://right.example"]


def test_irrelevant_first_engine_results_do_not_stop_rotation():
    """Une réponse reçue mais hors sujet doit laisser sa chance au moteur suivant."""
    transport = FakeTransport([
        {"results": [{
            "engine": "bing",
            "title": "T-shirt personnalisé",
            "url": "https://clothing.example/shirt",
            "content": "impression photo",
        }]},
        {"results": [{
            "engine": "duckduckgo",
            "title": "Roulement rigide à billes 25 x 52 x 15 mm",
            "url": "https://maker.example/bearing-25-52-15",
            "content": "Fiche produit roulement 25 x 52 x 15 mm",
        }]},
    ])

    batch = _gateway(transport).search("roulement rigide billes 25 52 15")

    assert [attempt.status for attempt in batch.attempts] == ["irrelevant", "ok"]
    assert [hit.url for hit in batch.hits] == ["https://maker.example/bearing-25-52-15"]


def test_catalog_without_numeric_family_does_not_hide_a_product_from_next_engine():
    transport = FakeTransport([
        {"results": [{
            "engine": "bing",
            "title": "Deep groove ball bearing sealed both sides catalog",
            "url": "https://catalog.example/bearings",
            "content": "Ball bearing seal specifications",
        }]},
        {"results": [{
            "engine": "duckduckgo",
            "title": "6205EE Roulement à billes",
            "url": "https://maker.example/products/6205EE",
            "content": "",
        }]},
    ])

    batch = _gateway(transport).search("6205 bearing sealed both sides datasheet")

    assert [attempt.status for attempt in batch.attempts] == ["irrelevant", "ok"]
    assert [hit.url for hit in batch.hits] == ["https://maker.example/products/6205EE"]


def test_results_are_capped_at_twelve():
    """Mutation détectée : une requête ouvre plus de douze résultats."""
    payload = {"results": [{
        "engine": "bing",
        "title": f"Produit {index}",
        "url": f"https://example.test/{index}",
    } for index in range(20)]}

    batch = _gateway(FakeTransport([payload])).search("disjoncteur")

    assert len(batch.hits) == 12
    assert [hit.rank for hit in batch.hits] == list(range(1, 13))


def test_three_technical_failures_preserve_diagnostics():
    """Mutation détectée : une panne de tous les moteurs ressemble à zéro résultat."""
    with pytest.raises(SearxUnavailable) as raised:
        _gateway(FakeTransport([RuntimeError("down")] * 3)).search("contacteur")

    assert [attempt.status for attempt in raised.value.batch.attempts] == ["error"] * 3


def test_three_legitimate_empty_responses_return_empty_batch():
    """Mutation détectée : l'absence de résultat est transformée en panne technique."""
    batch = _gateway(FakeTransport([{"results": []}] * 3)).search("contacteur")

    assert batch.hits == []
    assert [attempt.status for attempt in batch.attempts] == ["empty"] * 3


def test_suspended_engine_is_reported_as_blocked_instead_of_empty():
    transport = FakeTransport([
        {"results": [], "unresponsive_engines": [
            ["bing", "Suspended: too many requests"]
        ]},
        {"results": [], "unresponsive_engines": []},
        {"results": [{
            "engine": "google cse", "title": "6205EE Roulement à billes",
            "url": "https://maker.example/products/6205EE", "content": "",
        }]},
    ])

    batch = _gateway(transport).search("6205 bearing sealed both sides")

    assert [attempt.status for attempt in batch.attempts] == [
        "blocked", "empty", "ok"
    ]
    assert "too many requests" in batch.attempts[0].reason
    assert [hit.url for hit in batch.hits] == [
        "https://maker.example/products/6205EE"
    ]


@pytest.mark.parametrize("url", [
    "https://www.marche-a.example/shop/norel-uk?_nkw=norel+uk",
    "https://www.marchand-b.example/s?k=norel+contactor",
    "https://catalog.example/products?q=contacteur",
    "https://catalog.example/search?term=contacteur",
    "https://catalog.example/products?search=contacteur",
])
def test_commercial_search_urls_are_list_pages(url):
    assert est_page_de_liste(url) is True


def test_product_url_with_an_innocent_q_character_is_not_a_list_page():
    assert est_page_de_liste(
        "https://maker.example/products/quality-switch"
    ) is False


@pytest.mark.parametrize("url", [
    "https://market.example/cat/bearings",
    "https://market.example/collections/sealed-bearings",
    "https://market.example/manufacturers/bearing-6205",
    "https://market.example/g/deep-groove-ball-bearing.html",
])
def test_explicit_collection_paths_do_not_consume_product_page_budget(url):
    assert est_page_de_liste(url) is True


def test_s_path_is_a_list_only_in_the_explicit_s_query_form():
    assert est_page_de_liste(
        "https://maker.example/s/XZ07-20-10-11"
    ) is False
    assert est_page_de_liste(
        "https://market.example/s?k=XZ07-20-10-11"
    ) is True
