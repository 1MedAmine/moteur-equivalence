# -*- coding: utf-8 -*-
"""Le livrable doit expliquer un NOT_RESOLVED, pas seulement le déclarer."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rapport import construire, en_markdown
from recherche_adaptative import ResearchDiagnostics, ResearchOutcome
from modeles import RequirementSet


URL = "https://new.norel.example/products/fr-lu/4KBL137001R1110/xz07-20-10-11"


def _requirements() -> RequirementSet:
    return RequirementSet(
        product="contacteur 9 A bobine 24 V DC",
        criteria=[{
            "id": "coil",
            "label": "Tension de bobine",
            "requested_value": "24 V DC",
        }],
    )


def _outcome(diagnostics: ResearchDiagnostics) -> ResearchOutcome:
    return ResearchOutcome(
        status="not_resolved",
        requirements=_requirements(),
        evaluation=None,
        audits=[],
        visited_pages={},
        diagnostics=diagnostics,
    )


def _norel_diagnostics() -> ResearchDiagnostics:
    diagnostics = ResearchDiagnostics()
    diagnostics.logical_queries = 12
    diagnostics.engine_calls = 24
    diagnostics.pages_opened = 28
    diagnostics.pages_fetched = 28
    diagnostics.pages_rejected_by_gate = 28
    diagnostics.pages_not_ready_for_evidence = 0
    diagnostics.pages_analyzed = 0
    diagnostics.gate_rejections = [
        {
            "url": f"{URL}?p={index}",
            "reason": "REJECTED_FOR_PRODUCT_EVIDENCE",
            "mode": "PRODUCT_EVIDENCE",
            "content_length": 3733,
            "expected_identifiers": ["XZ07-20-10-11"],
            "matched_identifiers": [],
        }
        for index in range(28)
    ]
    return diagnostics


def test_json_explains_a_not_resolved_caused_by_unretrieved_content():
    """Mutation détectée : 28 pages rejetées rendent le même rapport que zéro trouvaille."""
    data = construire(_outcome(_norel_diagnostics()))
    diagnostics = data["diagnostics"]

    assert data["status"] == "not_resolved"
    assert diagnostics["pages_fetched"] == 28
    assert diagnostics["pages_rejected_by_gate"] == 28
    assert diagnostics["pages_not_ready_for_evidence"] == 0
    assert diagnostics["pages_analyzed"] == 0
    assert diagnostics["unresolved_reason"] == "PRODUCT_CONTENT_NOT_RETRIEVED"
    # Les compteurs déjà existants restent exposés.
    assert diagnostics["logical_queries"] == 12
    assert diagnostics["engine_calls"] == 24


def test_unresolved_reason_separates_the_three_causes():
    """Chaque cause a son nom : récupération, identité, ou recherche."""
    identite = ResearchDiagnostics()
    identite.pages_fetched = 5
    identite.pages_not_ready_for_evidence = 5
    assert construire(_outcome(identite))["diagnostics"][
        "unresolved_reason"] == "NO_VALIDATED_IDENTITY"

    analysees = ResearchDiagnostics()
    analysees.pages_fetched = 5
    analysees.pages_analyzed = 5
    assert construire(_outcome(analysees))["diagnostics"][
        "unresolved_reason"] == "NO_PROVABLE_CANDIDATE"

    aucune = ResearchDiagnostics()
    assert construire(_outcome(aucune))["diagnostics"][
        "unresolved_reason"] == "NO_PAGE_RETRIEVED"


def test_gate_rejections_are_projected_without_any_page_content():
    """Le rejet reste un diagnostic sûr : allowlist stricte, aucun fragment."""
    diagnostics = _norel_diagnostics()
    # Un champ interdit tente de passer.
    diagnostics.gate_rejections[0]["page_content"] = "Select Country Bahamas"
    diagnostics.gate_rejections[0]["exception"] = "clé secrète"

    data = construire(_outcome(diagnostics))
    rejets = data["diagnostics"]["gate_rejections"]

    assert len(rejets) == 28
    assert set(rejets[0]) == {
        "url", "reason", "mode", "content_length",
        "expected_identifiers", "matched_identifiers",
    }
    serialise = json.dumps(data, ensure_ascii=False)
    assert "page_content" not in serialise
    assert "Bahamas" not in serialise
    assert "secrète" not in serialise


def test_markdown_exposes_the_same_counters_as_json():
    """Mutation détectée : le Markdown tait ce que le JSON explique."""
    markdown = en_markdown(construire(_outcome(_norel_diagnostics())))

    assert "Pages recuperees : 28" in markdown
    assert "Pages rejetees par la porte : 28" in markdown
    assert "Pages analysees : 0" in markdown
    assert "PRODUCT_CONTENT_NOT_RETRIEVED" in markdown


def test_a_resolved_mission_has_no_unresolved_reason():
    diagnostics = _norel_diagnostics()
    outcome = ResearchOutcome(
        status="partial",
        requirements=_requirements(),
        evaluation=None,
        audits=[],
        visited_pages={},
        diagnostics=diagnostics,
    )

    assert construire(outcome)["diagnostics"]["unresolved_reason"] is None
