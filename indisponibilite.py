# -*- coding: utf-8 -*-
"""Classification sûre des indisponibilités temporaires du fournisseur LLM."""

from __future__ import annotations


_RATE_LIMIT_NAMES = frozenset({
    "RateLimitError",
    "TooManyRequests",
    "TooManyRequestsError",
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
