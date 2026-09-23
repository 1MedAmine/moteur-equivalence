# -*- coding: utf-8 -*-
"""Récupération B2 : Scrapling rapide, furtif, puis URL pour ScrapeGraphAI."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Literal
from urllib.parse import urlparse

from bs4 import BeautifulSoup, Comment


MIN_CONTENT_LENGTH = 100
ZERO_WIDTH = re.compile("[\u200b\u200c\u200d\ufeff]")
REMOVED_TAGS = ("script", "style", "noscript", "svg", "template")

# Le chrome de navigation est identique sur toutes les pages d'un site : le
# garder rend n'importe quel seuil de longueur inoperant. Mesure sur la fiche
# Norel XZ07-20-10-11 : 22 000 caracteres de menu global, zero fait technique,
# et un contenu qui passait pour exploitable. Ce corpus partait tel quel dans
# le contexte du modele, qui devait auditer un produit a partir du plan du site.
# Volontairement limite a ces quatre reperes : `aside`, `.menu` ou `.sidebar`
# emportent, selon les sites, des tableaux de caracteristiques.
NAVIGATION_SELECTORS = ("nav", "header", "footer", "[role='navigation']")

# Le message d'origine peut transporter un chemin local ou une URL signee : on
# garde de quoi distinguer un 403 d'un timeout, jamais un secret complet.
MAX_ERROR_MESSAGE = 200

#: Statuts qui denoncent un filtrage anti-robot plutot qu'une page absente.
#:
#: Mesure du 2026-08-19 (`NV1T05BD -> Norel`) : Distri B, Distri E et Guilde
#: Batisseur rendaient 403 sur les deux modes Scrapling, qui pilotent tous
#: deux Chromium via patchright. Camoufox est un moteur Firefox distinct, donc
#: une surface d'empreinte differente — c'est la seule raison de l'appeler.
#:
#: Un 404 n'y figure pas : une page absente n'a rien a contourner.
BOT_PROTECTION_STATUSES: frozenset[int] = frozenset({403, 429, 503})

#: Camoufox coute vingt a quarante secondes par page. Il n'est donc tente que
#: sur un blocage avere, jamais sur une page simplement vide.
CAMOUFOX_TIMEOUT_MS = 45_000

#: Documents que `pdf_web.WebPDFScraper` extrait sans navigateur. Un navigateur
#: bloque dessus jusqu'au timeout : l'echelle habituelle ne s'y applique pas.
DOCUMENT_EXTENSIONS = (".pdf",)

# Seuls les champs d'identité structurés peuvent sortir du JSON-LD. Le script
# complet reste exclu du corpus : descriptions, code et données sans rapport ne
# deviennent jamais une preuve par accident.
SAFE_JSONLD_IDENTITY_KEYS: frozenset[str] = frozenset({
    "name",
    "sku",
    "mpn",
    "productid",
    "item",
})
MAX_HEAD_IDENTITY_VALUES = 32
MAX_HEAD_IDENTITY_VALUE_LENGTH = 500

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class PageContent:
    url: str
    content: str
    title: str = ""
    mode: Literal["scrapling", "stealthy", "camoufox", "scrapegraph_url", "pdf"] = "scrapling"
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class FetchAttempt:
    """Trace structuree d'un seul essai de recuperation."""

    mode: Literal["scrapling", "stealthy", "camoufox", "pdf"]
    url: str
    outcome: Literal[
        "success",
        "unusable_status",
        "invalid_status",
        "empty_content",
        "exception",
    ]
    status: int | None = None
    content_type: str = ""
    html_length: int = 0
    text_length: int = 0
    duration_ms: int = 0
    error_type: str = ""
    error_message: str = ""

    @property
    def succeeded(self) -> bool:
        return self.outcome == "success"

    def describe(self) -> str:
        if self.outcome == "exception":
            detail = self.error_type
            if self.error_message:
                detail = f"{detail}: {self.error_message}"
        elif self.outcome == "unusable_status":
            detail = f"HTTP {self.status}, type {self.content_type or 'inconnu'}"
        elif self.outcome == "invalid_status":
            detail = "statut HTTP illisible"
        elif self.outcome == "empty_content":
            detail = self._avec_statut(
                f"{self.html_length} caracteres HTML, "
                f"{self.text_length} caracteres utiles apres nettoyage"
            )
        else:
            detail = self._avec_statut(f"{self.text_length} caracteres utiles")
        return f"{self.mode}={self.outcome} [{detail}] en {self.duration_ms} ms"

    def _avec_statut(self, detail: str) -> str:
        """Le barreau document n'expose pas de statut : `HTTP None` induirait en erreur."""
        return f"HTTP {self.status}, {detail}" if self.status is not None else detail


class PageFetchError(RuntimeError):
    """Les deux modes Scrapling ont echoue ; les deux diagnostics sont joints."""

    def __init__(self, url: str, attempts: tuple[FetchAttempt, ...]) -> None:
        self.url = url
        self.attempts = tuple(attempts)
        super().__init__(
            f"Recuperation impossible pour {url} : "
            + " | ".join(attempt.describe() for attempt in self.attempts)
        )


def _safe_error_message(error: BaseException) -> str:
    return " ".join(str(error).split())[:MAX_ERROR_MESSAGE]


class B2RateLimiter:
    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._clock = clock
        self._sleeper = sleeper
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def wait(self, delay: float) -> None:
        if delay <= 0:
            return
        with self._lock:
            now = self._clock()
            if now < self._next_allowed:
                self._sleeper(self._next_allowed - now)
                now = self._next_allowed
            self._next_allowed = now + delay


_RATE_LIMITER = B2RateLimiter()


def _normalized_text(value: object) -> str:
    without_invisible = ZERO_WIDTH.sub("", str(value or ""))
    lines = (" ".join(line.split()) for line in without_invisible.splitlines())
    return "\n".join(line for line in lines if line)


def _content_type(response: object) -> str:
    headers = getattr(response, "headers", {})
    if not isinstance(headers, Mapping):
        return ""
    for key, value in headers.items():
        if str(key).casefold() == "content-type":
            return str(value).split(";", 1)[0].strip().casefold()
    return ""


def _safe_head_identity(soup: BeautifulSoup, title: str) -> str:
    """Extrait seulement l'identité littérale du titre et du JSON-LD."""
    lines: list[str] = []

    def add(label: str, value: object) -> None:
        if len(lines) >= MAX_HEAD_IDENTITY_VALUES:
            return
        normalized = _normalized_text(value)[:MAX_HEAD_IDENTITY_VALUE_LENGTH]
        line = f"{label}: {normalized}" if normalized else ""
        if line and line not in lines:
            lines.append(line)

    if title:
        add("title", title)

    def walk(value: object, parent_key: str = "") -> None:
        if len(lines) >= MAX_HEAD_IDENTITY_VALUES:
            return
        if isinstance(value, Mapping):
            for key, child in value.items():
                normalized_key = str(key).casefold()
                if (
                    normalized_key in SAFE_JSONLD_IDENTITY_KEYS
                    and isinstance(child, (str, int, float))
                    and not isinstance(child, bool)
                ):
                    label = (
                        f"jsonld.{parent_key}.{normalized_key}"
                        if parent_key in {"brand", "manufacturer", "seller"}
                        else f"jsonld.{normalized_key}"
                    )
                    add(label, child)
                if isinstance(child, (Mapping, list, tuple)):
                    walk(child, normalized_key)
        elif isinstance(value, (list, tuple)):
            for child in value:
                walk(child, parent_key)

    for script in soup.find_all("script"):
        if str(script.get("type") or "").casefold() != "application/ld+json":
            continue
        raw = script.string or script.get_text(" ", strip=True)
        try:
            walk(json.loads(raw))
        except (json.JSONDecodeError, TypeError, ValueError):
            continue

    return "\n".join(lines)


def _clean_html(html: object) -> tuple[str, str]:
    if isinstance(html, bytes):
        source = html.decode("utf-8", errors="replace")
    else:
        source = str(html or "")
    if not source.strip():
        return "", ""
    soup = BeautifulSoup(source, "lxml")
    title = _normalized_text(
        soup.title.get_text(" ", strip=True) if soup.title else ""
    )
    head_identity = _safe_head_identity(soup, title)
    for tag in soup.find_all(REMOVED_TAGS):
        tag.decompose()
    for tag in soup.select(", ".join(NAVIGATION_SELECTORS)):
        tag.decompose()
    for tag in soup.select("[hidden], [aria-hidden='true']"):
        tag.decompose()
    for comment in soup.find_all(string=lambda item: isinstance(item, Comment)):
        comment.extract()
    body_content = _normalized_text((soup.body or soup).get_text("\n", strip=True))
    content = _normalized_text("\n".join((head_identity, body_content)))
    if len(content) < MIN_CONTENT_LENGTH:
        return "", title
    return content, title


class _CamoufoxAdapter:
    """Presente Camoufox sous la meme forme que les fetchers Scrapling.

    Le reste de l'echelle attend un objet portant `status`, `html_content` et
    `headers` ; l'adaptateur evite d'avoir a traiter Camoufox a part dans
    `_evaluate`.
    """

    def __init__(self, *, timeout_ms: int = CAMOUFOX_TIMEOUT_MS) -> None:
        self.timeout_ms = timeout_ms

    def fetch(self, url: str, **_: Any) -> Any:
        from camoufox.sync_api import Camoufox

        with Camoufox(headless=True, humanize=True) as navigateur:
            page = navigateur.new_page()
            reponse = page.goto(
                url, timeout=self.timeout_ms, wait_until="domcontentloaded"
            )
            # Un filtrage anti-robot rend d'abord une page de challenge : lire
            # immediatement ramenerait un corps vide.
            try:
                page.wait_for_load_state("networkidle", timeout=self.timeout_ms // 3)
            except Exception:
                pass
            html = page.content()
            statut = reponse.status if reponse is not None else 0
            entetes = dict(reponse.headers) if reponse is not None else {}

        return SimpleNamespace(
            status=statut, html_content=html, headers=entetes
        )


class PageFetcher:
    def __init__(
        self,
        *,
        timeout: float = 25.0,
        rate_limit_delay: float = 0.5,
        fast: Any = None,
        stealthy: Any = None,
        rate_limiter: B2RateLimiter | None = None,
        pdf_scraper: Any = None,
        camoufox: Any = None,
        enable_camoufox: bool = False,
    ) -> None:
        self.timeout = timeout
        self.rate_limit_delay = rate_limit_delay
        self._fast = fast
        self._stealthy = stealthy
        self._rate_limiter = rate_limiter or _RATE_LIMITER
        self._pdf_scraper = pdf_scraper
        self._camoufox = camoufox
        # Camoufox est un navigateur tiers dont le processus peut rester bloque
        # au-dela du timeout de navigation. Il est reserve a un appel explicite
        # (ou a une injection de test), jamais au chemin standard.
        self._enable_camoufox = enable_camoufox or camoufox is not None
        # Les fetchs rapides restent paralleles, mais chaque barreau navigateur
        # demarre un vrai processus. Deux Chromium/Firefox simultanes peuvent
        # epuiser la memoire et bloquer une vague entiere.
        self._browser_lock = threading.Lock()
        # Hotes ayant deja oppose un refus a Camoufox pendant ce run. Un
        # `set` suffit : sous le GIL, `add` et `in` sont atomiques, et les
        # deux fils de `_fetch_pages` n'y ecrivent que des hotes deja
        # constates. Le pire cas est une seconde tentative inutile.
        self._hotes_sans_camoufox: set[str] = set()

    def _fast_fetcher(self) -> Any:
        if self._fast is None:
            from scrapling.fetchers import Fetcher

            self._fast = Fetcher
        return self._fast

    def _stealthy_fetcher(self) -> Any:
        if self._stealthy is None:
            from scrapling.fetchers import StealthyFetcher

            self._stealthy = StealthyFetcher
        return self._stealthy

    def _camoufox_fetcher(self) -> Any:
        if self._camoufox is None:
            self._camoufox = _CamoufoxAdapter(timeout_ms=CAMOUFOX_TIMEOUT_MS)
        return self._camoufox

    def _pdf(self):
        if self._pdf_scraper is None:
            from pdf_web import WebPDFScraper

            self._pdf_scraper = WebPDFScraper(timeout=self.timeout)
        return self._pdf_scraper

    def _evaluate(
        self,
        response: object,
        url: str,
        mode: Literal["scrapling", "stealthy", "camoufox"],
        started: float,
    ) -> tuple[PageContent | None, FetchAttempt]:
        """Traduit une reponse en page exploitable ou en trace d'echec datee."""
        html = getattr(response, "html_content", "")
        html_length = len(html if isinstance(html, (str, bytes)) else "")
        content_type = _content_type(response)

        def trace(outcome: str, **fields: Any) -> FetchAttempt:
            return FetchAttempt(
                mode=mode,
                url=url,
                outcome=outcome,  # type: ignore[arg-type]
                content_type=content_type,
                html_length=html_length,
                duration_ms=self._elapsed_ms(started),
                **fields,
            )

        try:
            status = int(getattr(response, "status"))
        except (TypeError, ValueError, AttributeError):
            return None, trace("invalid_status")
        if not 200 <= status < 400:
            return None, trace("unusable_status", status=status)

        if content_type == "application/pdf":
            page = self._pdf().scrape(url)
            return page, trace(
                "success", status=status, text_length=len(page.content)
            )

        content, title = _clean_html(html)
        if not content:
            return None, trace(
                "empty_content", status=status, text_length=len(content)
            )
        return (
            PageContent(url=url, content=content, title=title, mode=mode),
            trace("success", status=status, text_length=len(content)),
        )

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return int((time.monotonic() - started) * 1000)

    def _attempt(
        self,
        mode: Literal["scrapling", "stealthy", "camoufox"],
        url: str,
        call: Callable[[], object],
    ) -> tuple[PageContent | None, FetchAttempt]:
        """Execute un mode ; une exception devient une trace, jamais un arret."""
        self._rate_limiter.wait(self.rate_limit_delay)
        started = time.monotonic()
        try:
            page, attempt = self._evaluate(call(), url, mode, started)
        except Exception as error:
            page = None
            attempt = FetchAttempt(
                mode=mode,
                url=url,
                outcome="exception",
                duration_ms=self._elapsed_ms(started),
                error_type=type(error).__name__,
                error_message=_safe_error_message(error),
            )
        return self._journal(page, attempt)

    def _attempt_document(self, url: str) -> tuple[PageContent | None, FetchAttempt]:
        """Extrait un document sans navigateur, avec la meme trace que l'echelle."""
        self._rate_limiter.wait(self.rate_limit_delay)
        started = time.monotonic()
        try:
            page = self._pdf().scrape(url)
        except Exception as error:
            return self._journal(None, FetchAttempt(
                mode="pdf",
                url=url,
                outcome="exception",
                duration_ms=self._elapsed_ms(started),
                error_type=type(error).__name__,
                error_message=_safe_error_message(error),
            ))

        contenu = _normalized_text(page.content)
        # Un scan sans couche texte se telecharge sans erreur et ne prouve
        # rien : il est refuse comme une page vide, pas compte comme un succes.
        outcome = "success" if len(contenu) >= MIN_CONTENT_LENGTH else "empty_content"
        attempt = FetchAttempt(
            mode="pdf",
            url=url,
            outcome=outcome,  # type: ignore[arg-type]
            content_type="application/pdf",
            text_length=len(page.content),
            duration_ms=self._elapsed_ms(started),
        )
        return self._journal(page if attempt.succeeded else None, attempt)

    @staticmethod
    def _journal(
        page: PageContent | None, attempt: FetchAttempt
    ) -> tuple[PageContent | None, FetchAttempt]:
        LOGGER.log(
            logging.DEBUG if attempt.succeeded else logging.WARNING,
            "recuperation %s",
            attempt.describe(),
            extra={"fetch_attempt": attempt},
        )
        return page, attempt

    def _camoufox_vaut_la_peine(self, hote: str) -> bool:
        """Une politique anti-robot vaut pour un hote, pas pour une adresse.

        Mesure du run du 2026-08-27 : quatre pages Stack Overflow et trois
        pages Distri B ont chacune repaye les quarante-cinq secondes de
        Camoufox pour se voir opposer le meme 403 — pres d'une minute perdue
        rien qu'en repetitions. Le premier echec complet sur un hote suffit a
        le savoir.

        Les deux modes rapides, eux, continuent d'essayer : ils coutent des
        centaines de millisecondes, et un chemin different peut repondre la ou
        un autre bloque. Seul le barreau a quarante-cinq secondes est retire.
        """
        return self._enable_camoufox and hote not in self._hotes_sans_camoufox

    def fetch(self, url: str) -> PageContent:
        parsed = urlparse(url)
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
            return PageContent(url=url, content="", mode="scrapegraph_url")

        # Un document n'a pas d'echelle : le navigateur bloque dessus jusqu'au
        # timeout, alors que l'extracteur le lit directement. Le routage suit
        # l'extension du chemin, pas la chaine complete, pour qu'un parametre
        # de requete ne masque pas le format.
        if parsed.path.casefold().endswith(DOCUMENT_EXTENSIONS):
            page, document_attempt = self._attempt_document(url)
            if page is not None:
                return page
            raise PageFetchError(url, (document_attempt,))

        page, fast_attempt = self._attempt(
            "scrapling",
            url,
            lambda: self._fast_fetcher().get(
                url,
                timeout=self.timeout,
                impersonate="chrome",
                stealthy_headers=True,
                follow_redirects="safe",
                verify=True,
                retries=1,
            ),
        )
        if page is not None:
            return page

        with self._browser_lock:
            page, stealthy_attempt = self._attempt(
                "stealthy",
                url,
                lambda: self._stealthy_fetcher().fetch(
                    url,
                    timeout=int(self.timeout * 1000),
                    headless=True,
                    block_ads=True,
                    disable_resources=False,
                    solve_cloudflare=False,
                    # 1 = un seul essai, pas un reessai. `0` semble dire la meme
                    # chose mais Scrapling le refuse (`Expected int >= 1`) : le mode
                    # furtif mourait alors en TypeError avant meme d'ouvrir un
                    # navigateur, et l'echelle n'avait en pratique qu'un barreau.
                    retries=1,
                ),
            )
        if page is not None:
            return page

        tentatives = [fast_attempt, stealthy_attempt]

        # Troisieme barreau, reserve aux blocages anti-robot averes. Les deux
        # modes precedents pilotent Chromium via patchright et presentent donc
        # la meme empreinte ; Camoufox est un moteur Firefox distinct. Sur une
        # page simplement vide il n'apporterait rien et couterait trente
        # secondes, d'ou le declenchement conditionnel.
        hote = (parsed.hostname or "").casefold()
        if any(
            item.status in BOT_PROTECTION_STATUSES for item in tentatives
        ) and self._camoufox_vaut_la_peine(hote):
            with self._browser_lock:
                page, camoufox_attempt = self._attempt(
                    "camoufox",
                    url,
                    lambda: self._camoufox_fetcher().fetch(url),
                )
            tentatives.append(camoufox_attempt)
            if page is not None:
                return page
            # Le bannissement vaut pour le reste du run, sans reprise possible :
            # l'hote etant ecarte, Camoufox n'y sera plus appele, donc ne
            # pourra plus prouver qu'il repond. C'est assume — les deux modes
            # rapides continuent d'essayer chaque page, et seul le barreau a
            # quarante-cinq secondes est abandonne.
            self._hotes_sans_camoufox.add(hote)

        # Repli scrapegraph_url desactive temporairement : il transformait cet
        # echec en RuntimeError nu, sans statut ni cause, plus loin dans l'analyse.
        raise PageFetchError(url, tuple(tentatives))
