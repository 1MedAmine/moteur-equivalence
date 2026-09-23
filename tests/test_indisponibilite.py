# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
import time
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from indisponibilite import executer_avec_reprises_llm


def test_llm_retry_waits_long_enough_for_four_consecutive_503(monkeypatch):
    """Une surcharge qui dépasse quatre secondes reste récupérable."""
    calls = {"count": 0}
    delays = []

    class InternalServerError(Exception):
        pass

    def operation():
        calls["count"] += 1
        if calls["count"] < 5:
            raise InternalServerError()
        return "ok"

    monkeypatch.setattr(time, "sleep", delays.append)

    assert executer_avec_reprises_llm(operation) == "ok"
    assert calls["count"] == 5
    assert delays == [1.0, 3.0, 8.0, 20.0]


def test_llm_retry_honors_a_provider_retry_after_header(monkeypatch):
    """Le fournisseur peut demander une attente supérieure au délai local."""
    delays = []

    class InternalServerError(Exception):
        status_code = 503
        response = type("Response", (), {"headers": {"retry-after": "12"}})()

    calls = {"count": 0}

    def operation():
        calls["count"] += 1
        if calls["count"] == 1:
            raise InternalServerError()
        return "ok"

    monkeypatch.setattr(time, "sleep", delays.append)

    assert executer_avec_reprises_llm(operation) == "ok"
    assert delays == [12.0]
