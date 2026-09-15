# -*- coding: utf-8 -*-
"""Le rapport doit rendre la boucle adaptative lisible vague par vague."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modeles import RequirementSet
from rapport import construire, en_markdown
from recherche_adaptative import ResearchDiagnostics, ResearchOutcome


def _requirements() -> RequirementSet:
    return RequirementSet(
        product="contacteur 9 A bobine 24 V DC",
        criteria=[{
            "id": "tension_de_bobine",
            "label": "Tension de bobine",
            "requested_value": "24 V DC",
        }],
    )


def _outcome(diagnostics, *, status="not_resolved"):
    return ResearchOutcome(
        status=status,
        requirements=_requirements(),
        evaluation=None,
        audits=[],
        visited_pages={},
        diagnostics=diagnostics,
    )


def _diagnostics() -> ResearchDiagnostics:
    diagnostics = ResearchDiagnostics()
    diagnostics.waves = 4
    diagnostics.logical_queries = 12
    diagnostics.stop_reason = "QUERY_BUDGET_EXHAUSTED"
    diagnostics.wave_details = [
        {"wave_index": 1, "mode": "discovery", "query_count": 3,
         "logical_queries_before": 0, "logical_queries_after": 3,
         "task_types": ["PLANNED"]},
        {"wave_index": 4, "mode": "targeted", "query_count": 3,
         "logical_queries_before": 9, "logical_queries_after": 12,
         "task_types": ["NEAR_MISS", "PLANNED"]},
    ]
    return diagnostics


def _section(data) -> list:
    return data["diagnostics"]["waves_detail"]


def test_each_wave_is_projected_with_its_counters():
    section = _section(construire(_outcome(_diagnostics())))

    assert len(section) == 2
    assert section[0]["wave_index"] == 1
    assert section[1]["task_types"] == ["NEAR_MISS", "PLANNED"]
    assert section[1]["logical_queries_after"] == 12


def test_stop_reason_is_projected():
    diagnostics = construire(_outcome(_diagnostics()))["diagnostics"]

    assert diagnostics["stop_reason"] == "QUERY_BUDGET_EXHAUSTED"


def test_wave_projection_uses_a_strict_allowlist():
    """Mutation détectée : la projection recopie le diagnostic tel quel."""
    diagnostics = _diagnostics()
    diagnostics.wave_details[0].update({
        "page_content": "Select Country Bahamas Barbados",
        "raw_prompt": "consigne complete",
        "api_key": "secret-a-ne-jamais-serialiser",
    })

    data = construire(_outcome(diagnostics))
    serialise = json.dumps(data, ensure_ascii=False)

    assert set(_section(data)[0]) == {
        "wave_index", "mode", "query_count",
        "logical_queries_before", "logical_queries_after", "task_types",
    }
    for interdit in ("page_content", "raw_prompt", "api_key", "Bahamas",
                     "consigne complete", "secret-a-ne-jamais"):
        assert interdit not in serialise, interdit


def test_markdown_shows_the_waves_when_they_exist():
    markdown = en_markdown(construire(_outcome(_diagnostics())))

    assert "Déroulé des vagues" in markdown
    assert "NEAR_MISS" in markdown
    assert "QUERY_BUDGET_EXHAUSTED" in markdown


def test_markdown_stays_silent_without_any_wave():
    markdown = en_markdown(construire(_outcome(ResearchDiagnostics())))

    assert "Déroulé des vagues" not in markdown


def test_the_section_exists_even_empty_for_a_stable_schema():
    diagnostics = construire(_outcome(ResearchDiagnostics()))["diagnostics"]

    assert diagnostics["waves_detail"] == []
    assert diagnostics["stop_reason"] == ""
