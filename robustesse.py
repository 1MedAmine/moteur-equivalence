# -*- coding: utf-8 -*-
"""Ce qu'il faut corriger dans ScrapeGraphAI pour tenir sur des requetes industrielles.

Constat de terrain (2026-08-14, recherche d'une alternative Dorval a un
Kerion K7C48208) : SearXNG a rendu parmi ses resultats
`https://assets.dorval.example/general/cession/autre/3d_lg-080054.stp` — un
fichier CAO. Chromium a tente d'y naviguer, a expire au bout de 30 s, et le run
ENTIER est tombe sur un `RuntimeError`.

Ce n'est pas un cas rare : sur des references industrielles, les fabricants
publient massivement des modeles 3D, des schemas et des archives, que les
moteurs indexent au meme titre que les fiches produit. Sans ce module, B2
echoue sur le premier fabricant qui publie bien ses CAO.

Les correctifs sont poses par monkey-patch plutot qu'en modifiant la
bibliotheque : la mise a jour du paquet reste possible, et la raison de chaque
ecart reste lisible ici. Meme parti pris que la generation precedente
avec ses `_patcher_*`.
"""

from __future__ import annotations

import os
import re
from urllib.parse import urlsplit

# Formats que B2 sait extraire sans navigateur, via `pdf_web.WebPDFScraper`.
# Un navigateur, lui, bloque dessus jusqu'au timeout : ils restent donc
# illisibles pour ScrapeGraphAI et exploitables pour B2. D'ou deux predicats
# plutot qu'un seul (`url_lisible` / `url_exploitable`).
#
# Mesure du 2026-08-20 : c'est sur `library.e.norel.example` qu'Norel publie les
# caracteristiques de ses contacteurs, sans anti-robot ni rendu JS, la ou les
# distributeurs repondaient 403.
_EXTENSIONS_DOCUMENTAIRES = ("pdf",)

# Extensions que ni un navigateur ni B2 ne savent lire, et sur lesquelles
# ChromiumLoader bloque jusqu'au timeout.
#
# CAO et 3D dominent : c'est ce que publient les fabricants d'appareillage.
_EXTENSIONS_ILLISIBLES = (
    # CAO / 3D
    "stp", "step", "stl", "igs", "iges", "dwg", "dxf", "dwf", "sldprt", "3ds",
    "catpart", "prt", "ipt", "easm", "edrw",
    # archives et binaires
    "zip", "rar", "7z", "gz", "tgz", "exe", "msi", "dmg",
    # bureautique et medias lourds. Les formats OpenDocument comptent autant
    # que ceux de Microsoft : constat du 2026-08-14, un `.ods` publie par
    # administration.example est arrive dans les resultats d'une recherche de
    # disjoncteur, la ou seuls `xls`/`xlsx` etaient prevus.
    "doc", "docx", "xls", "xlsx", "ppt", "pptx", "csv",
    "odt", "ods", "odp", "odg", "rtf",
    "mp4", "avi", "mov", "wmv", "mp3", "wav",
    "dwt", "eps", "ai", "psd",
)

def _motif(extensions: tuple[str, ...]) -> "re.Pattern[str]":
    return re.compile(
        r"\.(?:%s)(?:#.*)?(?:\?.*)?$" % "|".join(extensions),
        re.IGNORECASE,
    )


_MOTIF_ILLISIBLE = _motif(_EXTENSIONS_ILLISIBLES)
_MOTIF_DOCUMENTAIRE = _motif(_EXTENSIONS_DOCUMENTAIRES)

_pose = False


#: Domaines qui ne portent jamais une caracteristique produit industrielle. Ce
#: ne sont pas de mauvais sites, ce sont de mauvaises sources pour ce travail :
#: aucun n'a vocation a publier un courant nominal ou une tension de bobine.
#:
#: Mesure du run du 2026-08-27 (contacteur Norel) : SearXNG a remonte cinq pages
#: Stack Overflow sur la mise en cache navigateur. Les trois modes de
#: recuperation s'y sont acharnes pour 160 s de 403 — soit 72 % du temps perdu
#: du run — et elles ont pris cinq des trente-six places du budget de pages.
#: Les ecarter avant ouverture rend donc a la fois du temps et des places aux
#: pages qui peuvent reellement porter une preuve.
_DOMAINES_HORS_SUJET = frozenset({
    # Questions-reponses informatiques. `stackexchange.com` reste admis : son
    # reseau heberge des sites d'electronique ou une caracteristique peut
    # legitimement etre citee.
    "stackoverflow.com",
    "superuser.com",
    "serverfault.com",
    "askubuntu.com",
    # Encyclopedies : elles decrivent un fabricant, jamais une reference.
    "wikipedia.org",
    "wikimedia.org",
    "wiktionary.org",
    # Reseaux sociaux et video : aucun texte de specification exploitable.
    "facebook.com",
    "instagram.com",
    "twitter.com",
    "x.com",
    "tiktok.com",
    "pinterest.com",
    "linkedin.com",
    "youtube.com",
    "reddit.com",
    # Emploi.
    "indeed.com",
    "glassdoor.com",
    "welcometothejungle.com",
})

#: Un sous-domaine de recrutement reste hors sujet meme chez le fabricant vise :
#: `careers.norel` a ete ouvert pendant le meme run, sur une recherche de
#: contacteur, et a consomme une place pour rien.
_PREFIXES_HOTE_HORS_SUJET = ("careers.", "jobs.", "emploi.", "recrutement.")


def _hote(url: str) -> str:
    try:
        return (urlsplit(url or "").hostname or "").casefold()
    except ValueError:
        return ""


def domaines_exclus() -> frozenset[str]:
    """Liste effective, etendue par `B2_EXCLUDED_DOMAINS`.

    La variable ajoute a la liste plutot que de la remplacer : personne ne veut
    recopier les vingt entrees par defaut pour en ajouter une seule.
    """
    supplement = os.getenv("B2_EXCLUDED_DOMAINS", "")
    ajouts = {
        element.strip().casefold().lstrip(".")
        for element in supplement.split(",")
        if element.strip()
    }
    return _DOMAINES_HORS_SUJET | ajouts


def domaine_hors_sujet(url: str) -> bool:
    """Vrai si l'URL vient d'un site qui ne publie pas de caracteristiques produit.

    Predicat de pertinence, distinct de `url_exploitable` qui ne juge que la
    capacite d'extraction : une page Stack Overflow est parfaitement lisible,
    elle n'a simplement rien a prouver ici.
    """
    hote = _hote(url)
    if not hote:
        return False
    if hote.startswith(_PREFIXES_HOTE_HORS_SUJET):
        return True
    return any(
        hote == domaine or hote.endswith("." + domaine)
        for domaine in domaines_exclus()
    )


#: Marqueurs editoriaux dans le chemin d'une URL. Une page produit n'annonce
#: jamais qu'elle est une definition, un glossaire ou un fil de forum : ces
#: mots designent du contenu explicatif, qui parle du type d'appareil sans
#: jamais porter une reference ni une caracteristique chiffree.
#:
#: Complement indispensable a la liste de domaines, qui ne peut rien contre eux :
#: mesure du run du 2026-08-28, sept des trente-six pages ouvertes etaient des
#: articles pedagogiques — « what is a contactor in electrical », « qu'est-ce
#: qu'un contacteur moteur », « glossaire-electricite », « definition-dun-
#: contacteur » — publies sur des domaines electriques parfaitement legitimes.
#: Le run a rendu `rejected` faute d'avoir ouvert la moindre page produit.
#:
#: Verifie contre les URLs reellement ouvertes des deux runs : sur les
#: trente-six du run du 2026-08-27, ce motif n'en coupe qu'une, la page
#: Wikipedia deja ecartee par le domaine. Aucune page produit n'est touchee.
_CHEMIN_EDITORIAL = re.compile(
    r"""
        /glossaire | /glossary | /lexique
      | definition[-_] | /definitions?/
      | what[-_]is[-_]a?[-_] | qu[-_]est[-_]ce[-_]qu
      | /forum/ | /forums/ | /encyc
      | /blog/ | /guides?/ | /actualites?/ | /news/
      | /wiki/
    """,
    re.IGNORECASE | re.VERBOSE,
)


def chemin_editorial(url: str) -> bool:
    """Vrai si l'URL annonce du contenu explicatif plutot qu'une fiche produit."""
    return bool(_CHEMIN_EDITORIAL.search(url or ""))


def hors_sujet(url: str) -> bool:
    """Predicat retenu avant ouverture : ni source impossible, ni contenu explicatif.

    Juge la pertinence, jamais la lisibilite — c'est `url_exploitable` qui dit
    si B2 sait tirer du texte d'une adresse. Une page de glossaire se lit tres
    bien ; elle n'a simplement aucune caracteristique a prouver.
    """
    return domaine_hors_sujet(url) or chemin_editorial(url)


def url_lisible(url: str) -> bool:
    """Vrai si un navigateur peut esperer rendre cette URL comme une page."""
    return url_exploitable(url) and not _MOTIF_DOCUMENTAIRE.search(url or "")


def url_exploitable(url: str) -> bool:
    """Vrai si B2 sait tirer du texte de cette URL, navigateur ou non.

    Plus permissif que `url_lisible` du seul cote des documents : B2 ne
    navigue pas ses PDF, il les telecharge et les extrait.
    """
    return not _MOTIF_ILLISIBLE.search(url or "")


def filtrer_urls(urls):
    """Ecarte les ressources qu'un navigateur ne sait pas afficher.

    Remplace `research_web.filter_pdf_links`, qui ne connait que `.pdf`.
    """
    return [url for url in urls if url_lisible(url)]


def patcher_filtrage_resultats() -> None:
    """Etend le filtre de resultats de recherche a toutes les ressources binaires.

    `search_on_web` termine par `return filter_pdf_links(results)` en resolvant
    le nom dans les globales du module a l'appel : remplacer l'attribut du
    module suffit, sans toucher a `search_on_web` lui-meme.

    Idempotent : appele par `configuration.config_recherche`, donc a chaque
    construction de configuration.
    """
    global _pose
    if _pose:
        return
    from scrapegraphai.utils import research_web

    research_web.filter_pdf_links = filtrer_urls
    _pose = True


# Signatures des pages qui repondent 200 sans jamais livrer leur contenu :
# interstitiels anti-robot, murs de consentement, refus d'acces. Le scraping
# « reussit » alors, et le modele conclut honnetement qu'il n'a rien trouve —
# une page bloquee devient indiscernable d'une page sans equivalent.
#
# Constat de terrain : un distributeur rendait 6 533 caracteres titres
# « Just a moment... » (Cloudflare). Le diagnostic a demande une inspection
# manuelle ; il doit apparaitre dans les avertissements.
_MARQUEURS_ANTIBOT = (
    "just a moment",
    "checking your browser",
    "enable javascript and cookies",
    "vérification de votre navigateur",
    "access denied",
    "attention required",
    "captcha",
    "ddos protection",
)

# En dessous, une page ne porte pas de fiche produit, quoi qu'elle affiche.
TAILLE_PAGE_MINIMALE = 1500


def diagnostic_page(contenu: str) -> str:
    """Rend la raison pour laquelle une page est inexploitable, ou "" si elle l'est.

    Separe du filtrage d'URL : ici la page a ete chargee, et c'est son contenu
    qui trahit le blocage.
    """
    texte = (contenu or "").lower()
    for marqueur in _MARQUEURS_ANTIBOT:
        if marqueur in texte:
            return f"page protegee contre le scraping ({marqueur})"
    if len(texte.strip()) < TAILLE_PAGE_MINIMALE:
        return f"page quasi vide ({len(texte.strip())} caracteres)"
    return ""


# Un resultat de recherche sur cinq peut etre lent ou momentanement injoignable
# sans que la recherche soit perdue pour autant. `ChromiumLoader` part avec
# `retry_limit=1` — une seule tentative, malgre une docstring qui annonce 3 —
# et fait remonter l'echec en `RuntimeError` qui traverse tout le graphe.
# Une seconde tentative absorbe le hoquet reseau ; au-dela on s'entete pour
# rien, le budget temps de la recherche etant partage par toutes les pages.
TENTATIVES_PAR_PAGE = 2

# Plafond, en secondes, pour charger une page. Le defaut de Playwright
# (30 s sur `page.goto`) est deja long pour une fiche produit ; on ne cherche
# pas a l'allonger, mais a le rendre explicite et regle au meme endroit.
DELAI_PAGE = 30
