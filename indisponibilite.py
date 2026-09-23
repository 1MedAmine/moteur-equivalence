# -*- coding: utf-8 -*-
"""Classification sûre des indisponibilités temporaires du fournisseur LLM."""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from typing import TypeVar


T = TypeVar("T")

_DEFAULT_LLM_RETRY_DELAYS = (1.0, 3.0, 8.0, 20.0)
_MAX_PROVIDER_RETRY_AFTER_SECONDS = 60.0


_RATE_LIMIT_NAMES = frozenset({
    "RateLimitError",
    "TooManyRequests",
    "TooManyRequestsError",
})

_TEMPORARY_LLM_ERROR_NAMES = frozenset({
    "APIConnectionError",
    "APITimeoutError",
    "InternalServerError",
    # `langchain_openai` enveloppe l'InternalServerError du fournisseur sous
    # ce type tout en conservant la même sémantique 5xx.
    "OpenAIAPIError",
    "OpenAIConnectionError",
    "OpenAITimeoutError",
    "ServiceUnavailableError",
    "TimeoutError",
})


def est_limitation_llm(error: BaseException) -> bool:
    """Reconnaît un 429 sans inspecter son message potentiellement sensible."""
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if type(current).__name__ in _RATE_LIMIT_NAMES:
            return True
        status = getattr(current, "status_code", None)
        response = getattr(current, "response", None)
        if status == 429 or getattr(response, "status_code", None) == 429:
            return True
        current = current.__cause__ or current.__context__
    return False


def est_indisponibilite_temporaire_llm(error: BaseException) -> bool:
    """Reconnaît une défaillance réessayable sans exposer son message.

    Le fournisseur peut répondre 429 ou 5xx alors que la clé est valide. Les
    graphes sont recréés pour un unique rejeu, donc l'appel suivant ne réutilise
    ni une connexion ni un état de parsing en échec.
    """
    if est_limitation_llm(error):
        return True

    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if type(current).__name__ in _TEMPORARY_LLM_ERROR_NAMES:
            return True
        status = getattr(current, "status_code", None)
        response = getattr(current, "response", None)
        response_status = getattr(response, "status_code", None)
        if any(
            isinstance(code, int) and 500 <= code <= 599
            for code in (status, response_status)
        ):
            return True
        current = current.__cause__ or current.__context__
    return False


def _provider_retry_delay(error: BaseException, fallback: float) -> float:
    """Respecte un délai numérique du fournisseur, sans le laisser déborder."""
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return fallback
    try:
        raw = headers.get("retry-after")
        requested = float(raw)
    except (AttributeError, TypeError, ValueError):
        return fallback
    if not 0 < requested <= _MAX_PROVIDER_RETRY_AFTER_SECONDS:
        return fallback
    return max(fallback, requested)


def executer_avec_reprises_llm(
    operation: Callable[[], T],
    *,
    delays: Sequence[float] = _DEFAULT_LLM_RETRY_DELAYS,
    on_retry: Callable[[BaseException], None] | None = None,
) -> T:
    """Réessaie seulement les indisponibilités temporaires du fournisseur.

    Le SDK garde ``max_retries=0`` : les reprises restent ainsi bornées,
    observables et identiques pour les appels directs et les graphes.
    """
    for attempt in range(len(delays) + 1):
        try:
            return operation()
        except Exception as error:
            if (
                attempt >= len(delays)
                or not est_indisponibilite_temporaire_llm(error)
            ):
                raise
            if on_retry is not None:
                on_retry(error)
            time.sleep(_provider_retry_delay(error, delays[attempt]))
    raise AssertionError("boucle de reprise LLM inaccessible")


class AvertissementAnalyse(str):
    """Message affichable portant séparément sa classification technique."""

    rate_limited: bool

    def __new__(cls, value: str, *, rate_limited: bool = False):
        instance = super().__new__(cls, value)
        instance.rate_limited = rate_limited
        return instance


def avertissement_est_limitation_llm(warning: str) -> bool:
    structured = getattr(warning, "rate_limited", None)
    if structured is not None:
        return bool(structured)
    # Compatibilité de rejeu avec les corpus capturés avant le champ structuré.
    return any(name in str(warning) for name in _RATE_LIMIT_NAMES)
