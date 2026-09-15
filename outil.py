# -*- coding: utf-8 -*-
"""B2 : trouver une alternative industrielle a partir d'une fiche technique.

    .venv\\Scripts\\python outil.py ^
        --fiche chemin\\vers\\fiche.pdf --marque Dorval

Objectif et sorties repris de la specification d'origine ; seul le moteur change —
ScrapeGraphAI au lieu d'un rédacteur agentique. La recherche est menee ici plutot que deleguee
a `SearchGraph`, pour les raisons detaillees dans le README.
"""

from __future__ import annotations

import argparse
import inspect
import time
import sys

import configuration
import fiche as module_fiche
import rapport
from indisponibilite import est_limitation_llm
from configuration import B2Config
from planification import Planner
from recherche import SearxGateway
from recherche_adaptative import AdaptiveResearch
from routage_searx import ENGINE_SHORTCUTS, EngineRotation
from cache_pages import avec_cache
from scraping import PageFetcher

def _build_research(config: B2Config) -> AdaptiveResearch:
    return AdaptiveResearch(
        config=config,
        planner=Planner(config),
        gateway=SearxGateway(
            base_url=config.searxng_url,
            rotation=EngineRotation(ENGINE_SHORTCUTS),
            timeout=config.network_timeout,
            max_results=config.max_results_per_query,
            max_attempts=config.adaptive_engine_attempts,
        ),
        # Le cache s'interpose ici et nulle part ailleurs : les tests
        # injectent leur propre recuperateur et ne doivent jamais ecrire
        # sur le disque de l'utilisateur.
        fetcher=avec_cache(
            PageFetcher(
                timeout=config.network_timeout,
                rate_limit_delay=config.scraper_rate_limit_delay,
            ),
            config,
        ),
        graph_config=configuration.config_graphe(config, verbose=False),
    )


def _horodater(outcome, secondes: float) -> None:
    """Inscrit la duree du run dans le diagnostic, sans jamais le faire echouer.

    Les tests injectent leurs propres objets de recherche : certains n'ont pas
    de diagnostic, d'autres l'ont fige. Une mesure d'observation ne doit pas
    decider du sort d'un run.
    """
    diagnostics = getattr(outcome, "diagnostics", None)
    try:
        diagnostics.duration_seconds = round(secondes, 1)
    except (AttributeError, TypeError):
        pass


def executer(
    chemin_fiche: str,
    marque: str | None,
    pages: int | None = None,
    pertinence_minimale: int | None = None,
    *,
    config_loader=None,
    research_factory=None,
) -> dict:
    """Deroule la recherche et rend le dictionnaire de sortie.

    Ne leve pas : chaque panne connue devient un statut et un avertissement.
    Un outil de recherche qui s'interrompt sur une page protegee ne servirait a
    rien ; l'appelant doit toujours recevoir un resultat qu'il peut lire.
    """
    try:
        texte = module_fiche.lire(chemin_fiche)
    except module_fiche.FicheInvalide as exc:
        return rapport.entree_invalide(str(exc))

    try:
        loader = config_loader or B2Config.from_env
        config = loader()
        config.require_runtime()
        factory = research_factory or _build_research
        research = factory(config)
        try:
            parameters = inspect.signature(research.run).parameters.values()
            source_parameter = next(
                (item for item in parameters if item.name == "source_path"),
                None,
            )
            supports_source_keyword = (
                source_parameter is not None
                and source_parameter.kind in {
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    inspect.Parameter.KEYWORD_ONLY,
                }
            ) or any(
                item.kind is inspect.Parameter.VAR_KEYWORD
                for item in parameters
            )
            supports_source_position = (
                source_parameter is not None
                and source_parameter.kind is inspect.Parameter.POSITIONAL_ONLY
            )
        except (TypeError, ValueError):
            supports_source_keyword = False
            supports_source_position = False
        depart = time.perf_counter()
        if supports_source_position:
            outcome = research.run(texte, marque, chemin_fiche)
        else:
            run_options = (
                {"source_path": chemin_fiche} if supports_source_keyword else {}
            )
            outcome = research.run(texte, marque, **run_options)
        _horodater(outcome, time.perf_counter() - depart)
        return rapport.construire(outcome)
    except configuration.ConfigurationIncomplete as exc:
        return rapport.erreur_configuration(str(exc))
    except Exception as exc:
        if est_limitation_llm(exc):
            return rapport.service_indisponible()
        return rapport.erreur_analyse(
            type(exc).__name__,
            getattr(exc, "safe_diagnostics", None),
        )


def main(argv: list[str] | None = None) -> int:
    analyseur = argparse.ArgumentParser(description=__doc__)
    analyseur.add_argument("--fiche", required=True,
                           help="chemin de la fiche technique (PDF ou texte)")
    analyseur.add_argument("--marque", default=None,
                           help="restreindre la recherche a ce fabricant")
    analyseur.add_argument("--output-dir", default="resultats",
                           help="dossier des sorties (defaut : resultats)")
    arguments = analyseur.parse_args(argv)

    donnees = executer(arguments.fiche, arguments.marque)
    chemin_json, chemin_md = rapport.ecrire(arguments.output_dir, donnees)

    print(f"statut : {donnees['status']}"
          + (f" ({donnees['pertinence']} %)" if donnees.get("pertinence") else ""))
    print(f"JSON   : {chemin_json}")
    print(f"MD     : {chemin_md}")
    for avertissement in donnees["warnings"]:
        print(f"  ! {avertissement}")

    # `invalid_input` est une erreur d'appel : le signaler au shell. Un
    # `not_resolved` est un resultat legitime, pas un echec du programme.
    resultats_metier = {
        rapport.STATUT_COMPLET,
        rapport.STATUT_PARTIEL,
        rapport.STATUT_REJETE,
        rapport.STATUT_SANS_REPONSE,
    }
    return 0 if donnees["status"] in resultats_metier else 1


if __name__ == "__main__":
    sys.exit(main())
