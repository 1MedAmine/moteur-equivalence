# -*- coding: utf-8 -*-
"""Contrat de configuration strict de B2, sans accès à une clé réelle."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import configuration


_VARIABLES = {
    "B2_API_KEY",
    "B2_API_BASE",
    "B2_MODEL",
    "B2_ENABLE_THINKING",
    "B2_REASONING_BUDGET",
    "B2_TEMPERATURE",
    "B2_LLM_TIMEOUT",
    "B2_TIMEOUT",
    "B2_NETWORK_TIMEOUT",
    "B2_MODEL_TOKENS",
    "B2_MAX_SCRAPER_WORKERS",
    "B2_MAX_ANALYSIS_WORKERS",
    "B2_EXCLUDED_DOMAINS",
    "B2_SCRAPER_RATE_LIMIT_DELAY",
    "B2_MAX_RESULTS_PER_QUERY",
    "B2_MAX_RESULTS",
    "B2_DISTRIBUTOR_DOMAINS",
    "B2_ADAPTIVE_MAX_WAVES",
    "B2_ADAPTIVE_QUERIES_PER_WAVE",
    "B2_ADAPTIVE_ENGINE_ATTEMPTS",
    "B2_SEARCH_ENGINE_SHORTCUTS",
    "B2_ADAPTIVE_PAGES_PER_WAVE",
    "B2_MIN_COMPATIBILITY_PERCENT",
    "B2_SEARXNG_URL",
    "B2_BACKEND",
    "B2_HEADLESS",
    "B2_VERBOSE",
    "B2_PAGE_CACHE_DIR",
    "B2_PAGE_CACHE_TTL_HOURS",
    "NVIDIA_API_KEY",
    "INDUSTRIAL_API_KEY",
    "NVIDIA_BASE_URL",
    "INDUSTRIAL_API_BASE",
    "SEARXNG_URL",
    "INDUSTRIAL_SEARX_URL",
}


def _clear(monkeypatch) -> None:
    for name in _VARIABLES:
        monkeypatch.delenv(name, raising=False)


def test_les_budgets_par_defaut_restent_ceux_valides(monkeypatch, tmp_path):
    """Mutation détectée : un budget B2 diverge des valeurs validées à l'origine."""
    _clear(monkeypatch)

    config = configuration.B2Config.from_env(tmp_path / "absent.env")

    assert config.model == "nvidia/nemotron-3-super-120b-a12b"
    assert config.temperature == 0.1
    assert config.enable_thinking is True
    assert config.reasoning_budget == 8192
    assert config.llm_timeout == 180.0
    assert config.network_timeout == 25.0
    assert config.max_scraper_workers == 2
    assert config.scraper_rate_limit_delay == 0.5
    assert config.max_results_per_query == 12
    assert config.adaptive_max_waves == 4
    assert config.adaptive_queries_per_wave == 3
    assert config.adaptive_engine_attempts == 3
    assert config.adaptive_pages_per_wave == 12
    assert config.min_compatibility_percent == 75


def test_default_distributor_domains_are_readable_multibrand_sources(
    monkeypatch, tmp_path
):
    """Les cibles sont configurees par domaine, jamais par marque produit."""
    _clear(monkeypatch)

    config = configuration.B2Config.from_env(tmp_path / "absent.env")

    assert config.distributor_domains == ()


def test_search_engines_can_use_an_available_alternative_without_changing_defaults(
    monkeypatch, tmp_path
):
    _clear(monkeypatch)
    monkeypatch.setenv("B2_SEARCH_ENGINE_SHORTCUTS", "yd,szn,nvr")

    config = configuration.B2Config.from_env(tmp_path / "absent.env")

    assert config.search_engine_shortcuts == ("yd", "szn", "nvr")


def test_unknown_search_engine_shortcut_fails_closed(monkeypatch, tmp_path):
    _clear(monkeypatch)
    monkeypatch.setenv("B2_SEARCH_ENGINE_SHORTCUTS", "yd,unknown")

    with pytest.raises(configuration.ConfigurationIncomplete):
        configuration.B2Config.from_env(tmp_path / "absent.env")


def test_distributor_domains_can_be_overridden_without_a_brand_rule(
    monkeypatch, tmp_path
):
    _clear(monkeypatch)
    monkeypatch.setenv(
        "B2_DISTRIBUTOR_DOMAINS",
        " catalogue-a.com , catalogue-b.net ",
    )

    config = configuration.B2Config.from_env(tmp_path / "absent.env")

    assert config.distributor_domains == (
        "catalogue-a.com",
        "catalogue-b.net",
    )


def test_reserved_example_domains_are_never_sent_to_live_search(
    monkeypatch, tmp_path
):
    _clear(monkeypatch)
    monkeypatch.setenv(
        "B2_DISTRIBUTOR_DOMAINS",
        "distributeur-a.example, distributeur-b.invalid, catalogue.test",
    )

    config = configuration.B2Config.from_env(tmp_path / "absent.env")

    assert config.distributor_domains == ()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("B2_TEMPERATURE", "nan"),
        ("B2_TEMPERATURE", "2.1"),
        ("B2_ENABLE_THINKING", "peut-etre"),
        ("B2_LLM_TIMEOUT", "0"),
        ("B2_ADAPTIVE_MAX_WAVES", "5"),
        ("B2_ADAPTIVE_QUERIES_PER_WAVE", "4"),
        ("B2_ADAPTIVE_ENGINE_ATTEMPTS", "4"),
        ("B2_ADAPTIVE_PAGES_PER_WAVE", "13"),
        ("B2_MIN_COMPATIBILITY_PERCENT", "101"),
        ("B2_DISTRIBUTOR_DOMAINS", "https://distributeur-a.example"),
        ("B2_SEARXNG_URL", "localhost:8080"),
    ],
)
def test_invalid_values_fail_closed(monkeypatch, tmp_path, name, value):
    """Mutation détectée : une valeur invalide retombe silencieusement au défaut."""
    _clear(monkeypatch)
    monkeypatch.setenv(name, value)

    with pytest.raises(configuration.ConfigurationIncomplete):
        configuration.B2Config.from_env(tmp_path / "absent.env")


def test_blank_b2_credentials_use_ordered_fallbacks(monkeypatch, tmp_path):
    """Mutation détectée : une ligne B2 vide masque les fallbacks compatibles."""
    _clear(monkeypatch)
    monkeypatch.setenv("B2_API_KEY", " ")
    monkeypatch.setenv("NVIDIA_API_KEY", "fallback-key")
    monkeypatch.setenv("B2_API_BASE", "")
    monkeypatch.setenv("NVIDIA_BASE_URL", "https://provider.example/v1")
    monkeypatch.setenv("B2_SEARXNG_URL", "")
    monkeypatch.setenv("SEARXNG_URL", "http://127.0.0.1:8080")

    config = configuration.B2Config.from_env(tmp_path / "absent.env")

    assert config.api_key == "fallback-key"
    assert config.api_base == "https://provider.example/v1"
    assert config.searxng_url == "http://127.0.0.1:8080"


def test_runtime_requires_a_key_without_exposing_it(monkeypatch, tmp_path):
    """Mutation détectée : B2 démarre sans clé ou la recopie dans l'erreur."""
    _clear(monkeypatch)
    config = configuration.B2Config.from_env(tmp_path / "absent.env")

    with pytest.raises(configuration.ConfigurationIncomplete) as raised:
        config.require_runtime()

    assert "B2_API_KEY" in str(raised.value)
    assert "fallback-key" not in str(raised.value)


def test_llm_payload_matches_nemotron_contract(monkeypatch, tmp_path):
    """Mutation détectée : raisonnement réactivé ou timeout/température divergents."""
    _clear(monkeypatch)
    monkeypatch.setenv("B2_API_KEY", "test-only-key")
    config = configuration.B2Config.from_env(tmp_path / "absent.env")

    payload = configuration.config_llm(config)

    assert payload["model"] == "openai/nvidia/nemotron-3-super-120b-a12b"
    assert payload["temperature"] == 0.1
    assert payload["timeout"] == 180.0
    assert payload["max_retries"] == 0
    assert payload["extra_body"] == {
        "reasoning_budget": 8192,
        "chat_template_kwargs": {"enable_thinking": True, "low_effort": False},
    }


def test_structured_graph_disables_thinking_but_keeps_it_for_planning():
    config = configuration.B2Config(
        api_key="test-only-key",
        enable_thinking=True,
        reasoning_budget=8192,
        temperature=0.1,
    )

    planning = configuration.config_llm(config)
    structured = configuration.config_graphe(config, verbose=False)["llm"]

    assert planning["extra_body"] == {
        "reasoning_budget": 8192,
        "chat_template_kwargs": {"enable_thinking": True, "low_effort": False},
    }
    assert planning["temperature"] == 0.1
    assert structured["extra_body"] == {
        "chat_template_kwargs": {"enable_thinking": False},
    }
    assert structured["temperature"] == 0.0
