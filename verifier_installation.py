# -*- coding: utf-8 -*-
"""Verifie que l'installation de B2 tient debout, etage par etage.

Chaque controle est independant et affiche son propre verdict : une panne de
reseau sur le dernier n'efface pas ce que les precedents ont prouve.

    .venv\\Scripts\\python verifier_installation.py

Le controle du LLM et celui du scraping consomment du reseau ; `--hors-ligne`
s'arrete aux controles locaux.
"""

from __future__ import annotations

import argparse
import sys

import configuration


def _titre(texte: str) -> None:
    print(f"\n[{texte}]")


def controle_import() -> bool:
    _titre("import de ScrapeGraphAI")
    try:
        import scrapegraphai
        from scrapegraphai.graphs import SmartScraperGraph  # noqa: F401
    except Exception as exc:  # pragma: no cover - depend de l'installation
        print(f"  ECHEC : {exc}")
        return False
    print(f"  OK : scrapegraphai {getattr(scrapegraphai, '__version__', 'version inconnue')}")
    return True


def controle_configuration() -> bool:
    _titre("configuration lue depuis .env")
    try:
        llm = configuration.config_llm()
    except configuration.ConfigurationIncomplete as exc:
        print(f"  ECHEC : {exc}")
        return False
    print(f"  modele   : {llm['model']}")
    print(f"  endpoint : {llm['base_url']}")
    print(f"  clé configurée : {'oui' if bool(llm['api_key']) else 'non'}")
    print(f"  contexte : {llm['model_tokens']} tokens")
    gabarit = (llm.get("extra_body") or {}).get("chat_template_kwargs", {})
    print(f"  raisonnement : {gabarit.get('enable_thinking', 'non pilote')}")
    return True


def controle_playwright() -> bool:
    _titre("navigateur Playwright")
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # pragma: no cover
        print(f"  ECHEC : {exc}")
        return False
    try:
        with sync_playwright() as p:
            navigateur = p.chromium.launch(headless=True)
            navigateur.close()
    except Exception as exc:
        print(f"  ECHEC : {exc}")
        print("  Corriger : packages\\equivalence\\.venv\\Scripts\\python -m playwright install chromium")
        return False
    print("  OK : chromium se lance en mode headless")
    return True


def controle_llm() -> bool:
    """Un aller-retour reel vers l'endpoint NVIDIA, sans passer par un graphe.

    C'est le controle qui distingue « la cle et l'URL sont plausibles » de
    « le fournisseur nous repond ».
    """
    _titre("appel au fournisseur (endpoint de la V4)")
    from langchain.chat_models import init_chat_model

    llm = dict(configuration.config_llm())
    llm.pop("model_tokens")
    llm["model"] = llm["model"].split("/", 1)[1]
    try:
        modele = init_chat_model(model_provider="openai", **llm)
        reponse = modele.invoke("Reponds exactement : OK")
    except Exception as exc:
        print(f"  ECHEC : {exc}")
        return False
    print(f"  OK : reponse recue -> {reponse.content.strip()[:80]!r}")
    return True


def controle_scraping() -> bool:
    """Un graphe complet sur une page publique et stable."""
    _titre("SmartScraperGraph de bout en bout")
    from scrapegraphai.graphs import SmartScraperGraph

    config = configuration.config_graphe(verbose=False)
    graphe = SmartScraperGraph(
        prompt="Donne le titre principal de la page et une phrase de resume.",
        source="https://example.com",
        config=config,
    )
    try:
        resultat = graphe.run()
    except Exception as exc:
        print(f"  ECHEC : {exc}")
        return False
    print(f"  OK : {resultat}")
    return True


def controle_recherche() -> bool:
    """SearchGraph complet : SearXNG, scraping des resultats, puis extraction.

    Hors du lot par defaut : il faut SearXNG lance, et l'affaire prend ~1 min
    (plusieurs pages scrapees, plusieurs appels au 120B).
    """
    _titre("SearchGraph via SearXNG")
    from scrapegraphai.graphs import SearchGraph

    try:
        config = configuration.config_recherche(verbose=False)
    except configuration.ConfigurationIncomplete as exc:
        print(f"  ECHEC : {exc}")
        return False
    config["max_results"] = 3

    graphe = SearchGraph(
        prompt="Quelles sont les caracteristiques du disjoncteur Kerion Sercia SC60N ?",
        config=config,
    )
    try:
        resultat = graphe.run()
    except Exception as exc:
        print(f"  ECHEC : {type(exc).__name__} : {exc}")
        print("  SearXNG repond-il sur http://localhost:8080 ?")
        return False
    print(f"  OK : {str(resultat)[:200]}...")
    return True


def main() -> int:
    analyseur = argparse.ArgumentParser(description=__doc__)
    analyseur.add_argument(
        "--hors-ligne",
        action="store_true",
        help="s'en tenir aux controles qui ne demandent pas le reseau",
    )
    analyseur.add_argument(
        "--avec-recherche",
        action="store_true",
        help="ajouter le SearchGraph via SearXNG (~1 min, SearXNG doit tourner)",
    )
    arguments = analyseur.parse_args()

    controles = [controle_import, controle_configuration, controle_playwright]
    if not arguments.hors_ligne:
        controles += [controle_llm, controle_scraping]
        if arguments.avec_recherche:
            controles.append(controle_recherche)

    resultats = [(controle.__name__, controle()) for controle in controles]

    _titre("resume")
    for nom, ok in resultats:
        print(f"  {'OK   ' if ok else 'ECHEC'} {nom}")
    return 0 if all(ok for _, ok in resultats) else 1


if __name__ == "__main__":
    sys.exit(main())
