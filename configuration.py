# -*- coding: utf-8 -*-
"""Configuration du moteur, avec les budgets hérités de la génération
précédente : ils avaient été mesurés, pas devinés."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

import robustesse
from routage_searx import ENGINE_SHORTCUTS, SUPPORTED_ENGINE_SHORTCUTS


DOSSIER = Path(__file__).resolve().parent
FICHIER_ENV = DOSSIER / ".env"

# Budgets métier partagés par le run réel et le validateur de corpus de rejeu.
# Une source unique empêche le banc hors ligne d'accepter une provenance que
# l'orchestrateur Web ne pourrait jamais produire.
MAX_LOGICAL_QUERIES = 12
MAX_QUERIES_PER_WAVE = 3

# Aucun domaine factice ne doit atteindre un moteur de recherche réel. Les
# distributeurs sont une option de déploiement explicite via
# ``B2_DISTRIBUTOR_DOMAINS`` ; l'absence de configuration désactive cet angle.
DEFAULT_DISTRIBUTOR_DOMAINS: tuple[str, ...] = ()
_NON_ROUTABLE_TEST_TLDS = (".example", ".invalid", ".test", ".localhost")


class ConfigurationIncomplete(ValueError):
    """Erreur de configuration sûre et destinée à l'utilisateur."""


def _search_engine_shortcuts() -> tuple[str, ...]:
    raw = os.getenv("B2_SEARCH_ENGINE_SHORTCUTS", "").strip()
    if not raw:
        return ENGINE_SHORTCUTS
    shortcuts = tuple(item.strip().casefold() for item in raw.split(","))
    if (
        not shortcuts
        or any(not item or item not in SUPPORTED_ENGINE_SHORTCUTS for item in shortcuts)
        or len(set(shortcuts)) != len(shortcuts)
    ):
        raise ConfigurationIncomplete(
            "B2_SEARCH_ENGINE_SHORTCUTS contient un raccourci inconnu ou répété."
        )
    return shortcuts


def _premier_non_vide(*noms: str, defaut: str = "") -> str:
    for nom in noms:
        valeur = os.getenv(nom)
        if valeur is not None and valeur.strip():
            return valeur.strip()
    return defaut


def _texte(nom: str, defaut: str) -> str:
    valeur = os.getenv(nom)
    return defaut if valeur is None or not valeur.strip() else valeur.strip()


def _entier(nom: str, defaut: int, *, minimum: int | None = None,
            maximum: int | None = None) -> int:
    brut = os.getenv(nom)
    if brut is None or not brut.strip():
        valeur = defaut
    else:
        try:
            valeur = int(brut)
        except ValueError:
            raise ConfigurationIncomplete(f"{nom} doit être un entier.") from None
    if minimum is not None and valeur < minimum:
        raise ConfigurationIncomplete(f"{nom} doit être supérieur ou égal à {minimum}.")
    if maximum is not None and valeur > maximum:
        raise ConfigurationIncomplete(f"{nom} doit être inférieur ou égal à {maximum}.")
    return valeur


def _flottant(nom: str, defaut: float, *, minimum: float | None = None,
              maximum: float | None = None, minimum_strict: bool = False) -> float:
    brut = os.getenv(nom)
    if brut is None or not brut.strip():
        valeur = defaut
    else:
        try:
            valeur = float(brut)
        except ValueError:
            raise ConfigurationIncomplete(f"{nom} doit être un nombre.") from None
    if not math.isfinite(valeur):
        raise ConfigurationIncomplete(f"{nom} doit être un nombre fini.")
    if minimum is not None:
        invalide = valeur <= minimum if minimum_strict else valeur < minimum
        if invalide:
            comparaison = "strictement supérieur à" if minimum_strict else "supérieur ou égal à"
            raise ConfigurationIncomplete(f"{nom} doit être {comparaison} {minimum}.")
    if maximum is not None and valeur > maximum:
        raise ConfigurationIncomplete(f"{nom} doit être inférieur ou égal à {maximum}.")
    return valeur


def _booleen(nom: str, defaut: bool) -> bool:
    brut = os.getenv(nom)
    if brut is None or not brut.strip():
        return defaut
    normalise = brut.strip().casefold()
    if normalise in {"1", "true", "vrai", "oui", "yes", "on"}:
        return True
    if normalise in {"0", "false", "faux", "non", "no", "off"}:
        return False
    raise ConfigurationIncomplete(f"{nom} doit être un booléen.")


def _url(nom: str, valeur: str) -> str:
    try:
        resultat = urlparse(valeur)
    except ValueError:
        resultat = None
    if resultat is None or resultat.scheme not in {"http", "https"} or not resultat.hostname:
        raise ConfigurationIncomplete(f"{nom} doit être une URL HTTP(S) avec un hôte.")
    return valeur.rstrip("/")


def _dossier_cache(nom: str, defaut: Path) -> str:
    """Un chemin relatif se lit depuis le module, jamais depuis le cwd.

    `B2_PAGE_CACHE_DIR=.cache_pages` doit designer le meme dossier quel que
    soit le repertoire d'invocation. La lecon vient de `--basetemp`, resolu
    contre le cwd et qui semait des dossiers hors du module. Une valeur
    explicitement vide desactive le cache.
    """
    brut = os.getenv(nom)
    if brut is None:
        return str(defaut)
    brut = brut.strip()
    if not brut:
        return ""
    chemin = Path(brut).expanduser()
    return str(chemin if chemin.is_absolute() else DOSSIER / chemin)


def _domaines(nom: str, defaut: tuple[str, ...]) -> tuple[str, ...]:
    brut = os.getenv(nom)
    valeurs = defaut if brut is None or not brut.strip() else tuple(
        item.strip().casefold() for item in brut.split(",") if item.strip()
    )
    domaines: list[str] = []
    for valeur in valeurs:
        try:
            resultat = urlparse(f"//{valeur}")
        except ValueError:
            resultat = None
        if (
            resultat is None
            or resultat.hostname != valeur
            or any(character in valeur for character in "/?#:@")
        ):
            raise ConfigurationIncomplete(
                f"{nom} doit contenir des noms de domaine separes par des virgules."
            )
        if valeur == "localhost" or valeur.endswith(_NON_ROUTABLE_TEST_TLDS):
            continue
        if valeur not in domaines:
            domaines.append(valeur)
    if not domaines and defaut:
        raise ConfigurationIncomplete(f"{nom} doit contenir au moins un domaine.")
    return tuple(domaines)


@dataclass(frozen=True)
class B2Config:
    api_key: str = field(default="", repr=False)
    api_base: str = "https://integrate.api.nvidia.com/v1"
    model: str = "nvidia/nemotron-3-super-120b-a12b"
    vision_model: str = "nvidia/nemotron-nano-12b-v2-vl"
    enable_thinking: bool = True
    reasoning_budget: int = 8192
    temperature: float = 0.1
    llm_timeout: float = 180.0
    network_timeout: float = 25.0
    model_tokens: int = 256_000
    max_scraper_workers: int = 2
    # Distinct de `max_scraper_workers` : un navigateur furtif tient en
    # memoire, un audit LLM n'occupe qu'une socket. Les confondre obligeait
    # a serialiser les audits des qu'on baissait les navigateurs pour
    # tenir sur une machine chargee, sans que la memoire y gagne rien.
    max_analysis_workers: int = 2
    scraper_rate_limit_delay: float = 0.5
    max_results_per_query: int = 12
    distributor_domains: tuple[str, ...] = DEFAULT_DISTRIBUTOR_DOMAINS
    # Quatre vagues de trois requetes plutot que trois de quatre : le budget
    # global reste douze, mais un tour demeure disponible pour consommer ce
    # que l'audit de la vague precedente vient de decouvrir. Mesure du
    # 2026-08-19 : avec 3x4, un near-miss detecte a la derniere vague ne
    # pouvait jamais donner lieu a une requete.
    adaptive_max_waves: int = 4
    adaptive_queries_per_wave: int = MAX_QUERIES_PER_WAVE
    adaptive_engine_attempts: int = 3
    search_engine_shortcuts: tuple[str, ...] = ENGINE_SHORTCUTS
    adaptive_pages_per_wave: int = 12
    min_compatibility_percent: int = 75
    searxng_url: str = "http://localhost:8080"
    backend: str = "playwright"
    headless: bool = True
    verbose: bool = True
    # Le reseau n'est pas reproductible : une page recuperee une fois est
    # relue depuis le disque plutot que redemandee a un site qui peut
    # repondre 403 au run suivant. Sept jours bornent la fraicheur d'une
    # fiche produit sans rendre le cache inutile d'un run a l'autre.
    page_cache_dir: str = str(DOSSIER / ".cache_pages")
    page_cache_ttl_hours: float = 168.0

    @classmethod
    def from_env(cls, env_file: Path | None = FICHIER_ENV) -> "B2Config":
        if env_file is not None and env_file.exists():
            load_dotenv(env_file, override=False)

        temperature = _flottant("B2_TEMPERATURE", 0.1, minimum=0.0, maximum=2.0)
        max_waves = _entier("B2_ADAPTIVE_MAX_WAVES", 4, minimum=1, maximum=4)
        queries = _entier(
            "B2_ADAPTIVE_QUERIES_PER_WAVE",
            MAX_QUERIES_PER_WAVE,
            minimum=1,
        )
        # Le produit reste borne a douze requetes logiques : c'est le budget
        # global, et lui seul, qui protege la mission. Quatre vagues de trois
        # laissent un tour pour consommer ce que l'audit vient de decouvrir.
        if queries != MAX_QUERIES_PER_WAVE:
            raise ConfigurationIncomplete(
                "B2_ADAPTIVE_QUERIES_PER_WAVE doit valoir exactement "
                f"{MAX_QUERIES_PER_WAVE}."
            )
        engine_attempts = _entier(
            "B2_ADAPTIVE_ENGINE_ATTEMPTS", 3, minimum=1, maximum=3
        )
        pages = _entier("B2_ADAPTIVE_PAGES_PER_WAVE", 12, minimum=1, maximum=12)
        threshold = _entier(
            "B2_MIN_COMPATIBILITY_PERCENT", 75, minimum=1, maximum=100
        )
        backend = _texte("B2_BACKEND", "playwright")
        if backend not in {"playwright", "selenium"}:
            raise ConfigurationIncomplete(
                "B2_BACKEND doit valoir playwright ou selenium."
            )

        api_base = _premier_non_vide(
            "B2_API_BASE", "NVIDIA_BASE_URL", "INDUSTRIAL_API_BASE",
            defaut="https://integrate.api.nvidia.com/v1",
        )
        searxng_url = _premier_non_vide(
            "B2_SEARXNG_URL", "SEARXNG_URL", "INDUSTRIAL_SEARX_URL",
            defaut="http://localhost:8080",
        )
        return cls(
            api_key=_premier_non_vide(
                "B2_API_KEY", "NVIDIA_API_KEY", "INDUSTRIAL_API_KEY"
            ),
            api_base=_url("B2_API_BASE", api_base),
            model=_texte("B2_MODEL", "nvidia/nemotron-3-super-120b-a12b"),
            vision_model=_texte(
                "B2_VISION_MODEL", "nvidia/nemotron-nano-12b-v2-vl"
            ),
            enable_thinking=_booleen("B2_ENABLE_THINKING", True),
            reasoning_budget=_entier("B2_REASONING_BUDGET", 8192, minimum=1),
            temperature=temperature,
            llm_timeout=_flottant(
                "B2_LLM_TIMEOUT",
                _flottant("B2_TIMEOUT", 180.0, minimum=0.0, minimum_strict=True),
                minimum=0.0,
                minimum_strict=True,
            ),
            network_timeout=_flottant(
                "B2_NETWORK_TIMEOUT", 25.0, minimum=0.0, minimum_strict=True
            ),
            model_tokens=_entier("B2_MODEL_TOKENS", 256_000, minimum=1),
            max_scraper_workers=_entier("B2_MAX_SCRAPER_WORKERS", 2, minimum=1),
            max_analysis_workers=_entier(
                "B2_MAX_ANALYSIS_WORKERS", 2, minimum=1
            ),
            scraper_rate_limit_delay=_flottant(
                "B2_SCRAPER_RATE_LIMIT_DELAY", 0.5, minimum=0.0
            ),
            max_results_per_query=_entier(
                "B2_MAX_RESULTS_PER_QUERY",
                _entier("B2_MAX_RESULTS", 12, minimum=1, maximum=12),
                minimum=1,
                maximum=12,
            ),
            distributor_domains=_domaines(
                "B2_DISTRIBUTOR_DOMAINS", DEFAULT_DISTRIBUTOR_DOMAINS
            ),
            adaptive_max_waves=max_waves,
            adaptive_queries_per_wave=queries,
            adaptive_engine_attempts=engine_attempts,
            search_engine_shortcuts=_search_engine_shortcuts(),
            adaptive_pages_per_wave=pages,
            min_compatibility_percent=threshold,
            searxng_url=_url("B2_SEARXNG_URL", searxng_url),
            backend=backend,
            headless=_booleen("B2_HEADLESS", True),
            verbose=_booleen("B2_VERBOSE", True),
            page_cache_dir=_dossier_cache(
                "B2_PAGE_CACHE_DIR", DOSSIER / ".cache_pages"
            ),
            page_cache_ttl_hours=_flottant(
                "B2_PAGE_CACHE_TTL_HOURS", 168.0, minimum=0.0
            ),
        )

    def require_runtime(self) -> None:
        if not self.api_key.strip():
            raise ConfigurationIncomplete(
                "B2_API_KEY doit être configurée avant de lancer B2."
            )


def _payload_raisonnement(config: B2Config) -> dict:
    if not any(m in config.model for m in ("nemotron-3-super", "nemotron-3-ultra")):
        return {}
    if not config.enable_thinking:
        return {"chat_template_kwargs": {"enable_thinking": False}}
    return {
        "reasoning_budget": config.reasoning_budget,
        "chat_template_kwargs": {"enable_thinking": True, "low_effort": False},
    }


def config_llm(
    config: B2Config | None = None,
    *,
    structured_output: bool = False,
) -> dict:
    config = config or B2Config.from_env()
    resultat = {
        "model": f"openai/{config.model}",
        "api_key": config.api_key,
        "base_url": config.api_base,
        "temperature": 0.0 if structured_output else config.temperature,
        "timeout": config.llm_timeout,
        # La reprise est gérée au niveau du graphe, avec une erreur
        # diagnostiquable. Laisser ChatOpenAI effectuer ses reprises cachées
        # pouvait bloquer un run pendant plusieurs timeouts successifs.
        "max_retries": 0,
        "model_tokens": config.model_tokens,
    }
    extra = _payload_raisonnement(config)
    if structured_output and any(
        model in config.model
        for model in ("nemotron-3-super", "nemotron-3-ultra")
    ):
        # ScrapeGraphAI valide ces appels contre un schéma Pydantic. Le canal
        # de raisonnement de Nemotron peut alors devenir la valeur parsée et
        # faire disparaître les clés attendues (`criteria`, `candidates`).
        # La planification directe conserve le budget de raisonnement ; seuls
        # les appels qui doivent rendre un JSON contractuel le désactivent.
        extra = {"chat_template_kwargs": {"enable_thinking": False}}
    if extra:
        resultat["extra_body"] = extra
    return resultat


def config_graphe(config: B2Config | None = None, **surcharges) -> dict:
    config = config or B2Config.from_env()
    resultat = {
        "llm": config_llm(config, structured_output=True),
        "verbose": config.verbose,
        "headless": config.headless,
        "timeout": config.network_timeout,
        "loader_kwargs": {
            "backend": config.backend,
            "retry_limit": robustesse.TENTATIVES_PAR_PAGE,
            "timeout": config.network_timeout,
        },
    }
    resultat.update(surcharges)
    return resultat


def url_searxng(config: B2Config | None = None) -> str:
    return (config or B2Config.from_env()).searxng_url


def config_recherche(config: B2Config | None = None, **surcharges) -> dict:
    """Compatibilité avec le contrôle SearchGraph, hors flux principal B2."""
    config = config or B2Config.from_env()
    resultat = config_graphe(config, **surcharges)
    robustesse.patcher_filtrage_resultats()
    resultat.setdefault("search_engine", "searxng")
    resultat.setdefault("serper_api_key", None)
    resultat["max_results"] = config.max_results_per_query
    if config.searxng_url not in {
        "http://localhost:8080", "http://127.0.0.1:8080"
    }:
        raise ConfigurationIncomplete(
            "SearchGraph de ScrapeGraphAI 2.1.6 exige SearXNG sur le port local 8080."
        )
    return resultat
