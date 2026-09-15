# -*- coding: utf-8 -*-
"""Projection des diagnostics near-miss dans le livrable.

`rapport.py` projette et ne recalcule rien : ni éligibilité, ni priorité, ni
budget. Une tâche ciblée est une direction de recherche déclenchée par un rejet
prouvé — jamais une preuve, jamais un équivalent trouvé.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modeles import RequirementSet
from rapport import construire, en_markdown
from recherche_adaptative import ResearchDiagnostics, ResearchOutcome


SIGNATURE = "xz07|coil|24 v dc"


def _requirements() -> RequirementSet:
    return RequirementSet(
        product="contacteur 9 A bobine 24 V DC",
        criteria=[{
            "id": "tension_de_bobine",
            "label": "Tension de bobine",
            "requested_value": "24 V DC",
        }],
    )


def _outcome(diagnostics: ResearchDiagnostics, *, status="not_resolved"):
    return ResearchOutcome(
        status=status,
        requirements=_requirements(),
        evaluation=None,
        audits=[],
        visited_pages={},
        diagnostics=diagnostics,
    )


def _tache(signature=SIGNATURE, chemin="DIRECTIONAL"):
    return {
        "signature": signature,
        "eligibility_path": chemin,
        "blocking_criterion": "tension_de_bobine",
        "target_value": "24 V DC",
        "reason": "NEAR_MISS_BLOCKING_CRITERION:tension_de_bobine",
    }


def _diagnostics_xz07() -> ResearchDiagnostics:
    diagnostics = ResearchDiagnostics()
    diagnostics.near_miss_candidates_considered = 1
    diagnostics.near_miss_tasks_generated = 1
    diagnostics.near_miss_tasks_selected = 1
    diagnostics.near_miss_tasks_deduplicated = 0
    diagnostics.near_miss_queries_scheduled = 2
    diagnostics.near_miss_queries_skipped_budget = 0
    diagnostics.reserved_directional_signature = SIGNATURE
    diagnostics.seen_near_miss_signatures = [SIGNATURE]
    diagnostics.near_miss_tasks = [_tache()]
    return diagnostics


def _section(data: dict) -> dict:
    return data["diagnostics"]["near_miss_research"]


# --------------------------------------------------------------------------
# Compteurs
# --------------------------------------------------------------------------

def test_every_counter_is_projected():
    section = _section(construire(_outcome(_diagnostics_xz07())))

    assert section["candidates_considered"] == 1
    assert section["tasks_generated"] == 1
    assert section["tasks_selected"] == 1
    assert section["tasks_deduplicated"] == 0
    assert section["queries_scheduled"] == 2
    assert section["queries_skipped_budget"] == 0


def test_deduplicated_and_skipped_budget_stay_distinct():
    """Mutation détectée : un doublon se lit « budget insuffisant »."""
    diagnostics = _diagnostics_xz07()
    diagnostics.near_miss_tasks_deduplicated = 3
    diagnostics.near_miss_queries_skipped_budget = 0

    section = _section(construire(_outcome(diagnostics)))

    assert section["tasks_deduplicated"] == 3
    assert section["queries_skipped_budget"] == 0


def test_reserved_signature_appears_when_a_reservation_happened():
    section = _section(construire(_outcome(_diagnostics_xz07())))

    assert section["reserved_directional_signature"] == SIGNATURE
    assert section["seen_signatures"] == [SIGNATURE]


def test_no_reservation_leaves_an_empty_signature():
    diagnostics = _diagnostics_xz07()
    diagnostics.reserved_directional_signature = ""

    section = _section(construire(_outcome(diagnostics)))

    assert section["reserved_directional_signature"] == ""


# --------------------------------------------------------------------------
# Allowlist
# --------------------------------------------------------------------------

def test_selected_task_exposes_only_allowed_fields():
    section = _section(construire(_outcome(_diagnostics_xz07())))

    assert len(section["selected_tasks"]) == 1
    assert set(section["selected_tasks"][0]) == {
        "signature", "eligibility_path", "blocking_criterion",
        "target_value", "reason",
    }


def test_a_forbidden_field_never_reaches_json_or_markdown():
    """Mutation détectée : la projection recopie le diagnostic tel quel."""
    diagnostics = _diagnostics_xz07()
    diagnostics.near_miss_tasks[0].update({
        "page_content": "Bobine 100...250V AC/DCC sur toute la page",
        "prompt": "consigne complete envoyee au modele",
        "api_key": "secret-a-ne-jamais-serialiser",
        "candidate": {"brand": "Norel", "criteria": ["objet complet"]},
    })

    data = construire(_outcome(diagnostics))
    serialise = json.dumps(data, ensure_ascii=False)
    markdown = en_markdown(data)

    assert set(_section(data)["selected_tasks"][0]) == {
        "signature", "eligibility_path", "blocking_criterion",
        "target_value", "reason",
    }
    for interdit in ("page_content", "prompt", "api_key", "secret-a-ne-jamais",
                     "objet complet", "100...250V"):
        assert interdit not in serialise, interdit
        assert interdit not in markdown, interdit


def test_a_malformed_task_entry_is_ignored_without_crashing():
    diagnostics = _diagnostics_xz07()
    diagnostics.near_miss_tasks = ["pas un dictionnaire", _tache()]

    section = _section(construire(_outcome(diagnostics)))

    assert len(section["selected_tasks"]) == 1


# --------------------------------------------------------------------------
# Schéma stable
# --------------------------------------------------------------------------

def test_skipped_candidates_name_the_unmet_condition():
    """Mutation détectée : un candidat écarté disparaît sans dire pourquoi."""
    diagnostics = _diagnostics_xz07()
    diagnostics.near_miss_skipped = [{
        "eligible": False,
        "reason": "NOT_ELIGIBLE_TOO_MANY_BLOCKERS",
        "eligibility_path": None,
        "identity": "XZ12-20-10",
        "family": "XZ12",
        "proven_compatible_count": 1,
        "blocking_criteria": ["coil", "courant", "poles"],
        "missing_criteria": [],
        "page_content": "fragment interdit",
    }]

    section = _section(construire(_outcome(diagnostics)))
    ecarte = section["skipped_candidates"][0]

    assert ecarte["reason"] == "NOT_ELIGIBLE_TOO_MANY_BLOCKERS"
    assert ecarte["family"] == "XZ12"
    assert ecarte["blocking_criteria"] == ["coil", "courant", "poles"]
    # Allowlist stricte, même sur cette projection.
    assert set(ecarte) == {
        "reason", "identity", "family",
        "proven_compatible_count", "blocking_criteria", "missing_criteria",
    }
    assert "fragment interdit" not in json.dumps(
        construire(_outcome(diagnostics)), ensure_ascii=False
    )


def test_skipped_candidates_name_the_criteria_left_to_prove():
    """Un candidat sans bloqueur s'ecarte sur ce qui manque, pas sur un vide."""
    diagnostics = _diagnostics_xz07()
    diagnostics.near_miss_skipped = [{
        "eligible": False,
        "reason": "NOT_ELIGIBLE_NO_PROVEN_INCOMPATIBILITY",
        "eligibility_path": None,
        "identity": "BSL07-20-10-81",
        "family": "BSL07",
        "proven_compatible_count": 4,
        "blocking_criteria": [],
        "missing_criteria": ["coil", "contacts"],
    }]

    ecarte = _section(construire(_outcome(diagnostics)))["skipped_candidates"][0]

    assert ecarte["missing_criteria"] == ["coil", "contacts"]
    assert ecarte["blocking_criteria"] == []


def test_the_section_exists_even_without_any_activity():
    """Schéma stable : l'objet est toujours présent, à zéro."""
    section = _section(construire(_outcome(ResearchDiagnostics())))

    assert section == {
        "candidates_considered": 0,
        "tasks_generated": 0,
        "tasks_selected": 0,
        "tasks_deduplicated": 0,
        "queries_scheduled": 0,
        "queries_skipped_budget": 0,
        "reserved_directional_signature": "",
        "seen_signatures": [],
        "selected_tasks": [],
        "skipped_candidates": [],
    }


def test_markdown_stays_silent_without_any_activity():
    """Aucune section vide : le lecteur ne doit pas chercher ce qui n'existe pas."""
    markdown = en_markdown(construire(_outcome(ResearchDiagnostics())))

    assert "Recherche ciblée" not in markdown


def test_markdown_reports_the_direction_when_it_exists():
    markdown = en_markdown(construire(_outcome(_diagnostics_xz07())))

    assert "Recherche ciblée depuis les candidats proches" in markdown
    assert "Candidats examinés : 1" in markdown
    assert "Requêtes envoyées : 2" in markdown
    assert "Tâches dédupliquées : 0" in markdown
    assert "DIRECTIONAL" in markdown
    assert "tension_de_bobine" in markdown
    assert "24 V DC" in markdown


def test_markdown_never_presents_the_task_as_a_proven_equivalent():
    """Une direction de recherche n'est ni une preuve ni un équivalent trouvé."""
    markdown = en_markdown(construire(_outcome(_diagnostics_xz07())))

    bloc = markdown.split("Recherche ciblée depuis les candidats proches")[1]
    for mot in ("preuve", "équivalent trouvé", "compatible"):
        assert mot not in bloc.casefold()


# --------------------------------------------------------------------------
# Non-régression
# --------------------------------------------------------------------------

def test_serialisation_mutates_no_counter_and_no_decision():
    diagnostics = _diagnostics_xz07()
    avant = (
        diagnostics.near_miss_tasks_generated,
        diagnostics.near_miss_queries_scheduled,
        list(diagnostics.seen_near_miss_signatures),
        [dict(item) for item in diagnostics.near_miss_tasks],
    )

    outcome = _outcome(diagnostics)
    construire(outcome)
    construire(outcome)

    assert diagnostics.near_miss_tasks_generated == avant[0]
    assert diagnostics.near_miss_queries_scheduled == avant[1]
    assert diagnostics.seen_near_miss_signatures == avant[2]
    assert [dict(item) for item in diagnostics.near_miss_tasks] == avant[3]
    assert outcome.status == "not_resolved"


def test_existing_gate_counters_remain_projected():
    """La nouvelle section ne remplace rien de l'existant."""
    diagnostics = _diagnostics_xz07()
    diagnostics.pages_fetched = 5
    diagnostics.pages_analyzed = 2
    diagnostics.pages_rejected_by_gate = 3

    projete = construire(_outcome(diagnostics))["diagnostics"]

    assert projete["pages_fetched"] == 5
    assert projete["pages_analyzed"] == 2
    assert projete["pages_rejected_by_gate"] == 3
    assert "near_miss_research" in projete
