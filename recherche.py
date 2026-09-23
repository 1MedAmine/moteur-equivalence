# -*- coding: utf-8 -*-
"""Trouver les pages a examiner : requete, interrogation de SearXNG, filtrage.

B2 fabrique sa requete et interroge SearXNG lui-meme, au lieu de confier les
deux a `SearchGraph`. Ce n'est pas de la defiance envers la bibliotheque : son
`MergeAnswersNode` ecrase `sources` par la liste brute des URLs, ce qui rend
impossible le contrat de preuves de la specification d'origine (url + extrait + type). Voir le README.
"""

from __future__ import annotations

from collections.abc import Mapping
import re
import unicodedata
from typing import Callable, List, Optional, Protocol
from urllib.parse import parse_qsl, urlsplit

import requests

import robustesse
from mission import construire_prompt_requete
from modeles import SearchAttempt, SearchBatch, SearchHit
from routage_searx import ENGINE_SHORTCUTS, EngineRotation, build_bang_query

# Une requete de plus de quelques mots cesse d'etre une requete. Si le modele
# se met a rediger, on tronque plutot que d'envoyer un paragraphe a SearXNG.
MOTS_MAXIMUM = 16


class RechercheIndisponible(Exception):
    """SearXNG n'a pas repondu, ou a repondu quelque chose d'inexploitable."""


def _nettoyer(requete: str) -> str:
    """Ramene la reponse du modele a une requete utilisable."""
    requete = (requete or "").strip().strip('"').strip("'")
    # Le modele glisse parfois une phrase d'introduction malgre la consigne :
    # la derniere ligne non vide est alors la requete.
    lignes = [ligne.strip() for ligne in requete.splitlines() if ligne.strip()]
    if lignes:
        requete = lignes[-1]
    mots = requete.split()
    return " ".join(mots[:MOTS_MAXIMUM])


# Mots trop generiques pour identifier un produit : les retirer d'une requete
# la viderait de son sens, meme s'ils apparaissent dans le nom de l'origine.
_MOTS_GENERIQUES = {
    "electric", "electrique", "electriques", "france", "sa", "sas", "group",
    "contacteur", "disjoncteur", "relais", "variateur", "interrupteur",
    "de", "du", "des", "la", "le", "les", "et", "a", "pour",
}

# Nombre de mots en dessous duquel une requete cesse d'etre exploitable. Si le
# nettoyage descend sous ce seuil, c'est le nettoyage qui a tort : on rend la
# requete d'origine plutot qu'un fragment.
MOTS_MINIMUM = 3


def _mots_interdits(origine: str) -> set:
    """Les mots d'une marque/gamme d'origine qu'il faut bannir d'une requete."""
    mots = {m.strip(" ,;:.").lower() for m in (origine or "").replace(";", " ").split()}
    return {m for m in mots if m and m not in _MOTS_GENERIQUES and len(m) > 1}


def retirer_origine(requete: str, origine: str) -> str:
    """Retire de la requete la marque et la gamme du produit d'origine.

    Le prompt le demande deja, mais ne l'obtient pas de facon fiable : deux
    essais reels (2026-08-14) ont produit « ... Sercia equivalent » puis
    « Contacteur Tersa D ... Norel A9 », ramenant a chaque fois le produit de
    depart. Une consigne qu'on ne peut pas verifier n'est pas une garantie ;
    ce retrait-ci est du code, donc il tient.
    """
    interdits = _mots_interdits(origine)
    if not interdits:
        return requete

    # Retirer « Tersa » de « Tersa D » laisse un « D » seul, qui n'identifie
    # rien et detourne la recherche : a l'essai du 2026-08-14 il a ramene
    # l'article Wikipedia sur la lettre D.
    #
    # Mais une lettre isolee n'est pas toujours du dechet : « courbe C » est un
    # critere essentiel en appareillage. On ne retire donc une lettre que si le
    # mot qui la precedait vient d'etre retire — c'est-a-dire si elle est
    # orpheline, et non si elle qualifie le mot d'avant.
    gardes = []
    precedent_retire = False
    for mot in requete.split():
        nu = mot.strip(" ,;:.")
        if nu.lower() in interdits:
            precedent_retire = True
            continue
        if precedent_retire and len(nu) == 1 and nu.isalpha():
            continue  # orpheline : le nom de gamme dont elle faisait partie a saute
        gardes.append(mot)
        precedent_retire = False

    return " ".join(gardes) if len(gardes) >= MOTS_MINIMUM else requete


def identifier_origine(texte_fiche: str, appeler_llm: Callable[[str], str]) -> str:
    """Demande au modele la marque et la gamme du produit d'origine.

    Extraction volontairement etroite — une ligne, deux informations — la ou
    la fiche peut les exprimer de mille facons. Une reponse vide n'est pas une
    panne : elle prive seulement du filtre.
    """
    demande = (
        "Donne UNIQUEMENT la marque et le nom de gamme du produit decrit "
        "ci-dessous, separes par un point-virgule, sans phrase ni commentaire. "
        "Si la gamme n'apparait pas, ne donne que la marque.\n\n"
        f"{texte_fiche.strip()[:1500]}"
    )
    try:
        return " ".join(appeler_llm(demande).strip().splitlines()[:1])
    except Exception:
        return ""


def construire_requete(
    texte_fiche: str,
    marque: Optional[str],
    appeler_llm: Callable[[str], str],
) -> str:
    """Fabrique la requete de recherche a partir de la fiche.

    `appeler_llm` est injecte : les tests n'ont ainsi besoin ni de reseau ni de
    cle, et l'appelant reste libre du client qu'il utilise.
    """
    requete = _nettoyer(appeler_llm(construire_prompt_requete(texte_fiche, marque)))
    if not requete:
        raise RechercheIndisponible("Le modele n'a pas produit de requete.")

    origine = identifier_origine(texte_fiche, appeler_llm)
    # La marque VISEE doit survivre au nettoyage, meme si elle apparaissait
    # dans la reponse d'identification.
    if marque and marque.strip():
        origine = " ".join(m for m in origine.split()
                           if m.strip(" ,;:.").lower() != marque.strip().lower())
    requete = retirer_origine(requete, origine)

    # Une recherche ciblee sans le nom de la marque cherche le produit
    # d'origine : constat du 2026-08-14, la requete « Contacteur 9A 24V DC 3P
    # NO+NF rail DIN » sans « Norel » a rendu six pages Kerion du produit de
    # depart. Le prompt le demande deja ; on le garantit ici.
    if marque and marque.strip():
        requete = ajouter_marque(requete, marque.strip())
    return requete


def ajouter_marque(requete: str, marque: str) -> str:
    """Garantit la presence de la marque visee dans la requete."""
    mots = {mot.strip(" ,;:.").lower() for mot in requete.split()}
    if marque.lower() in mots:
        return requete
    return f"{marque} {requete}"


# Parametres qui trahissent une page de LISTE plutot qu'une fiche produit. Une
# page paginee ou triee est, par construction, un catalogue : elle enumere des
# produits sans donner les caracteristiques d'aucun. Constat du 2026-08-14 : sur
# une recherche de disjoncteur, les quatre premiers resultats etaient des
# `?page=44`, `?page=67` et des listes marchandes — aucune fiche, donc aucune
# alternative etayable.
#
# Heuristique volontairement etroite : on n'ecarte que sur ces marqueurs
# explicites, jamais sur la forme generale de l'URL, pour ne pas perdre une
# fiche produit dont l'adresse ressemblerait a une liste.
_PARAMETRES_DE_LISTE = frozenset({
    "page", "sort-by", "sort_by", "tri", "_nkw", "q", "search",
})
_SEGMENTS_DE_LISTE = frozenset({
    "shop", "search", "cat", "collections", "manufacturers", "g",
})


def est_page_de_liste(url: str) -> bool:
    """Vrai si l'URL designe explicitement une liste ou une recherche.

    Les noms de parametres et les segments sont parses, pas cherches comme de
    simples sous-chaines : le ``q`` de ``quality-switch`` ne doit pas faire
    passer une vraie fiche produit pour une page de resultats.
    """
    try:
        parsed = urlsplit(str(url or "").strip())
    except ValueError:
        return False
    parameters = {
        key.casefold() for key, _ in parse_qsl(parsed.query, keep_blank_values=True)
    }
    if parameters & _PARAMETRES_DE_LISTE:
        return True
    segments = {
        segment.casefold() for segment in parsed.path.split("/") if segment
    }
    explicit_s_query = (
        parsed.path.casefold().rstrip("/") == "/s" and bool(parsed.query)
    )
    return explicit_s_query or bool(segments & _SEGMENTS_DE_LISTE) or any(
        segment.startswith("search-") or segment.startswith("search.")
        for segment in segments
    )


def chercher_urls(
    requete: str,
    *,
    url_searxng: str = "http://localhost:8080",
    maximum: int = 5,
    delai: int = 20,
) -> List[str]:
    """Interroge SearXNG et rend les URLs qu'un navigateur saura afficher.

    Le filtrage est celui de `robustesse` : sans lui, un modele 3D publie par
    un fabricant suffit a faire tomber toute la recherche.
    """
    try:
        reponse = requests.get(
            f"{url_searxng.rstrip('/')}/search",
            params={"q": requete, "format": "json", "categories": "general"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=delai,
        )
        reponse.raise_for_status()
        resultats = reponse.json().get("results", [])
    except Exception as exc:
        raise RechercheIndisponible(
            f"SearXNG injoignable sur {url_searxng} : {exc}"
        ) from exc

    urls = [r.get("url", "") for r in resultats if r.get("url")]
    lisibles = robustesse.filtrer_urls(urls)
    fiches = [url for url in lisibles if not est_page_de_liste(url)]
    # Si tout a ete ecarte, mieux vaut examiner des listes que ne rien examiner :
    # l'heuristique sert a mieux classer, pas a rendre la recherche infertile.
    return (fiches or lisibles)[:maximum]


ACTIVE_ENGINES = {
    "bi": "bing",
    "ddg": "duckduckgo",
    "goc": "google cse",
    "nvr": "naver",
    "szn": "seznam",
    "qw": "qwant",
    "sp": "startpage",
    "yd": "yandex",
}


class SearxUnavailable(RuntimeError):
    """Tous les essais autorisés ont échoué techniquement."""

    def __init__(self, message: str, batch: SearchBatch) -> None:
        super().__init__(message)
        self.batch = batch


class SearxTransport(Protocol):
    def search(self, query: str, max_results: int, timeout: float) -> Mapping[str, object]: ...


class RequestsSearxTransport:
    def __init__(self, base_url: str) -> None:
        self._url = f"{base_url.rstrip('/')}/search"

    def search(self, query: str, max_results: int, timeout: float) -> Mapping[str, object]:
        response = requests.get(
            self._url,
            params={
                "q": query,
                "format": "json",
                "categories": "general",
                "pageno": 1,
            },
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, Mapping):
            raise ValueError("Réponse SearXNG invalide")
        return payload


class SearxGateway:
    def __init__(
        self,
        *,
        base_url: str,
        rotation: EngineRotation,
        timeout: float,
        max_results: int,
        max_attempts: int,
        transport: SearxTransport | None = None,
    ) -> None:
        self._transport = transport or RequestsSearxTransport(base_url)
        self._rotation = rotation
        self._timeout = timeout
        self._max_results = min(max(max_results, 1), 12)
        self._max_attempts = min(max(max_attempts, 1), 3)

    def search(self, query: str) -> SearchBatch:
        attempts: list[SearchAttempt] = []
        sequence = self._rotation.next_sequence()[:self._max_attempts]
        for shortcut in sequence:
            engine = ACTIVE_ENGINES[shortcut]
            routed_query = build_bang_query(shortcut, query)
            try:
                payload = self._transport.search(
                    routed_query, self._max_results, self._timeout
                )
                results = payload.get("results")
                if not isinstance(results, list):
                    raise ValueError("Résultats SearXNG invalides")
            except Exception as exc:
                attempts.append(SearchAttempt(
                    engine=engine,
                    status="blocked" if _is_blocked(exc) else "error",
                    result_count=0,
                    reason=type(exc).__name__,
                ))
                continue

            hits = _normalize_hits(results, engine, self._max_results)
            unresponsive_reason = _unresponsive_engine_reason(payload, engine)
            if not hits and unresponsive_reason:
                attempts.append(SearchAttempt(
                    engine=engine,
                    status="blocked",
                    result_count=0,
                    reason=unresponsive_reason,
                ))
                continue
            relevant_hits = [hit for hit in hits if _hit_matches_query(hit, query)]
            attempts.append(SearchAttempt(
                engine=engine,
                status="ok" if relevant_hits else ("irrelevant" if hits else "empty"),
                result_count=len(hits),
                reason="no_relevant_hit" if hits and not relevant_hits else "",
            ))
            if relevant_hits:
                return SearchBatch(query=query, hits=relevant_hits, attempts=attempts)

        batch = SearchBatch(query=query, hits=[], attempts=attempts)
        if attempts and all(item.status in {"blocked", "error"} for item in attempts):
            raise SearxUnavailable("SearXNG indisponible pour cette requête", batch)
        return batch


_SEARCH_QUERY_TOKEN = re.compile(r"[a-z0-9]+(?:[-_./×][a-z0-9]+)*", re.IGNORECASE)
_SEARCH_GENERIC_TOKENS = frozenset({
    "catalogue", "caracteristiques", "datasheet", "documentation", "fiche",
    "product", "produit", "specification", "specifications", "technique",
    "technical", "contacteur", "disjoncteur", "relais", "interrupteur",
    "variateur", "industriel", "industrial",
})


def numeric_query_anchors(query: str) -> tuple[str, ...]:
    """Repère les familles numériques autonomes, sans imposer un domaine."""
    return tuple(dict.fromkeys(re.findall(
        r"(?<![A-Za-z0-9])\d{4,}(?![A-Za-z0-9])", query
    )))


def metadata_has_numeric_anchor(metadata: str, anchors: tuple[str, ...]) -> bool:
    """Accepte aussi une variante suffixée de la famille dans un résultat."""
    tokens = re.findall(r"[A-Za-z0-9]+", metadata.casefold())
    return any(token.startswith(anchor) for token in tokens for anchor in anchors)


def _search_token_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).casefold()
    return "".join(
        character for character in normalized
        if character.isalnum() and not unicodedata.combining(character)
    )


def _hit_matches_query(hit: SearchHit, query: str) -> bool:
    """Écarte un moteur dont les métadonnées ne reprennent pas la recherche."""
    haystack = "\n".join((hit.url, hit.title, hit.snippet))
    anchors = numeric_query_anchors(query)
    if anchors:
        return metadata_has_numeric_anchor(haystack, anchors)
    significant = {
        _search_token_key(token)
        for token in _SEARCH_QUERY_TOKEN.findall(query)
    }
    significant = {
        token for token in significant
        if len(token) >= 4 and token not in _SEARCH_GENERIC_TOKENS
    }
    if len(significant) < 2:
        return True
    haystack_tokens = {
        _search_token_key(token)
        for token in _SEARCH_QUERY_TOKEN.findall(haystack)
    }
    return len(significant & haystack_tokens) >= 2


def _normalize_hits(results: list[object], engine: str, limit: int) -> list[SearchHit]:
    hits: list[SearchHit] = []
    for item in results:
        if not isinstance(item, Mapping) or item.get("engine") != engine:
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        hits.append(SearchHit(
            url=url,
            title=str(item.get("title") or "").strip(),
            snippet=str(item.get("content") or "").strip(),
            engine=engine,
            rank=len(hits) + 1,
        ))
        if len(hits) == limit:
            break
    return hits


def _unresponsive_engine_reason(payload: Mapping[str, object], engine: str) -> str:
    entries = payload.get("unresponsive_engines")
    if not isinstance(entries, list):
        return ""
    for entry in entries:
        if (
            isinstance(entry, (list, tuple))
            and len(entry) >= 2
            and str(entry[0]).casefold() == engine.casefold()
        ):
            return " ".join(str(entry[1]).split())[:120]
    return ""


def _is_blocked(exc: Exception) -> bool:
    if isinstance(exc, (TimeoutError, requests.Timeout)):
        return True
    if isinstance(exc, requests.HTTPError):
        response = exc.response
        return response is not None and response.status_code in {403, 429}
    return False
