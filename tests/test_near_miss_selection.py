# -*- coding: utf-8 -*-
"""Sélection déterministe des requêtes ciblées sous budget contraint.

Fonction pure : ni Web, ni SearXNG, ni orchestrateur. Elle répartit un budget
déjà décidé ailleurs et ne le crée jamais.

La règle centrale protège les tâches directionnelles de la famine. L'ordre
`ESTABLISHED`-first les éliminerait systématiquement — précisément les cas
comme `XZ07-20-10-13`, dont le seul fait prouvé est l'incompatibilité.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from near_miss import TargetedResearchTask, select_targeted_queries


def _task(
    *,
    famille: str,
    critere: str = "coil",
    cible: str = "24 V DC",
    chemin: str = "ESTABLISHED",
    compatibles: int = 2,
    bloqueurs: int = 1,
    requetes: tuple[str, ...] | None = None,
    signature: str | None = None,
) -> TargetedResearchTask:
    principales = requetes or (
        f'{famille} {critere} "{cible}"',
        f'{famille} {critere} secondaire "{cible}"',
    )
    return TargetedResearchTask(
        candidate_identity=f"{famille}-00",
        product_family=famille,
        blocking_criterion=critere,
        observed_value="autre valeur",
        target_value=cible,
        search_queries=principales,
        reason=f"NEAR_MISS_BLOCKING_CRITERION:{critere}",
        eligibility_path=chemin,
        signature=signature or f"{famille.casefold()}|{critere}|{cible.casefold()}",
        proven_compatible_count=compatibles,
        blocker_count=bloqueurs,
    )


def _etabli(famille="AA", **kwargs):
    return _task(famille=famille, chemin="ESTABLISHED", compatibles=3, **kwargs)


def _directionnel(famille="ZZ", **kwargs):
    return _task(famille=famille, chemin="DIRECTIONAL", compatibles=0, **kwargs)


def _chemins(resultat) -> list[str]:
    return [item.eligibility_path for item in resultat.selected]


# --------------------------------------------------------------------------
# Budget
# --------------------------------------------------------------------------

def test_zero_budget_selects_nothing():
    resultat = select_targeted_queries(
        (_etabli(), _directionnel()), remaining_logical_queries=0
    )

    assert resultat.selected == ()
    assert resultat.queries == ()


def test_budget_is_never_exceeded():
    taches = tuple(
        _etabli(famille=f"F{index}", signature=f"f{index}|coil|24 v dc")
        for index in range(6)
    )

    for budget in range(0, 7):
        resultat = select_targeted_queries(
            taches, remaining_logical_queries=budget
        )
        assert len(resultat.queries) <= budget


# --------------------------------------------------------------------------
# Réservation anti-famine
# --------------------------------------------------------------------------

def test_two_places_give_one_to_each_path():
    """Mutation détectée : ESTABLISHED-first affame les tâches directionnelles."""
    resultat = select_targeted_queries(
        (_etabli(), _directionnel()), remaining_logical_queries=2
    )

    assert sorted(_chemins(resultat)) == ["DIRECTIONAL", "ESTABLISHED"]
    assert len(resultat.queries) == 2


def test_a_single_place_goes_to_established():
    resultat = select_targeted_queries(
        (_etabli(), _directionnel()), remaining_logical_queries=1
    )

    assert _chemins(resultat) == ["ESTABLISHED"]


def test_without_established_the_best_directional_is_selected():
    resultat = select_targeted_queries(
        (
            _directionnel(famille="ZZ", signature="zz|coil|24 v dc"),
            _directionnel(famille="YY", signature="yy|coil|24 v dc", bloqueurs=2),
        ),
        remaining_logical_queries=1,
    )

    assert _chemins(resultat) == ["DIRECTIONAL"]
    # Le mieux classé : moins de bloqueurs à compatibilité égale.
    assert resultat.selected[0].product_family == "ZZ"


def test_only_one_place_is_ever_reserved_for_directional():
    """La réservation porte sur une requête, pas sur toutes les directionnelles."""
    resultat = select_targeted_queries(
        (
            _etabli(famille="AA", signature="aa|coil|24 v dc"),
            _etabli(famille="BB", signature="bb|coil|24 v dc"),
            _directionnel(famille="YY", signature="yy|coil|24 v dc"),
            _directionnel(famille="ZZ", signature="zz|coil|24 v dc"),
        ),
        remaining_logical_queries=3,
    )

    assert _chemins(resultat).count("DIRECTIONAL") == 1
    assert _chemins(resultat).count("ESTABLISHED") == 2


def test_a_lone_directional_is_never_sacrificed_with_two_places():
    """Le cas XZ07 : une seule tâche directionnelle face à des établies."""
    resultat = select_targeted_queries(
        (
            _etabli(famille="AA", signature="aa|coil|24 v dc"),
            _etabli(famille="BB", signature="bb|coil|24 v dc"),
            _etabli(famille="CC", signature="cc|coil|24 v dc"),
            _directionnel(famille="XZ07", signature="xz07|coil|24 v dc"),
        ),
        remaining_logical_queries=2,
    )

    familles = [item.product_family for item in resultat.selected]
    assert "XZ07" in familles


def test_no_reservation_when_a_single_path_exists():
    resultat = select_targeted_queries(
        (
            _etabli(famille="AA", signature="aa|coil|24 v dc"),
            _etabli(famille="BB", signature="bb|coil|24 v dc"),
        ),
        remaining_logical_queries=2,
    )

    assert _chemins(resultat) == ["ESTABLISHED", "ESTABLISHED"]


def test_reservation_never_increases_the_total_consumed():
    resultat = select_targeted_queries(
        (_etabli(), _directionnel()), remaining_logical_queries=2
    )

    assert len(resultat.queries) == 2


# --------------------------------------------------------------------------
# Déduplication
# --------------------------------------------------------------------------

def test_duplicate_signatures_keep_a_single_task():
    resultat = select_targeted_queries(
        (
            _etabli(famille="AA", signature="commune|coil|24 v dc"),
            _etabli(famille="BB", signature="commune|coil|24 v dc"),
        ),
        remaining_logical_queries=4,
    )

    assert len(resultat.selected) == 1
    assert len(resultat.deduplicated) == 1
    assert resultat.deduplicated[0] == "commune|coil|24 v dc"


def test_an_already_seen_signature_consumes_no_budget():
    resultat = select_targeted_queries(
        (
            _etabli(famille="AA", signature="deja|coil|24 v dc"),
            _etabli(famille="BB", signature="bb|coil|24 v dc"),
        ),
        remaining_logical_queries=2,
        seen_signatures={"deja|coil|24 v dc"},
    )

    familles = [item.product_family for item in resultat.selected]
    assert familles == ["BB"]
    assert "deja|coil|24 v dc" in resultat.already_seen


def test_all_signatures_seen_selects_nothing():
    resultat = select_targeted_queries(
        (_etabli(famille="AA", signature="aa|coil|24 v dc"),),
        remaining_logical_queries=4,
        seen_signatures={"aa|coil|24 v dc"},
    )

    assert resultat.selected == ()
    assert resultat.queries == ()


# --------------------------------------------------------------------------
# Requêtes primaires avant secondaires
# --------------------------------------------------------------------------

def test_primary_queries_come_before_any_secondary():
    """Mutation détectée : une tâche monopolise le budget avec ses variantes."""
    resultat = select_targeted_queries(
        (
            _etabli(famille="AA", signature="aa|coil|24 v dc"),
            _etabli(famille="BB", signature="bb|coil|24 v dc"),
        ),
        remaining_logical_queries=3,
    )

    assert resultat.queries[0] == 'AA coil "24 V DC"'
    assert resultat.queries[1] == 'BB coil "24 V DC"'
    # La secondaire n'arrive qu'après toutes les primaires retenues.
    assert "secondaire" in resultat.queries[2]


def test_secondary_queries_fill_the_remaining_budget():
    resultat = select_targeted_queries(
        (_etabli(famille="AA", signature="aa|coil|24 v dc"),),
        remaining_logical_queries=5,
    )

    # La tâche n'a que deux requêtes : le budget restant n'est pas comblé.
    assert len(resultat.queries) == 2


# --------------------------------------------------------------------------
# Déterminisme
# --------------------------------------------------------------------------

def test_result_is_stable_under_input_permutation():
    a = _etabli(famille="AA", signature="aa|coil|24 v dc")
    b = _etabli(famille="BB", signature="bb|coil|24 v dc")
    c = _directionnel(famille="ZZ", signature="zz|coil|24 v dc")

    direct = select_targeted_queries((a, b, c), remaining_logical_queries=3)
    inverse = select_targeted_queries((c, b, a), remaining_logical_queries=3)

    assert direct.queries == inverse.queries
    assert [item.signature for item in direct.selected] == [
        item.signature for item in inverse.selected
    ]


def test_selection_is_repeatable():
    taches = (
        _etabli(famille="AA", signature="aa|coil|24 v dc"),
        _directionnel(famille="ZZ", signature="zz|coil|24 v dc"),
    )

    assert select_targeted_queries(taches, remaining_logical_queries=2).queries == (
        select_targeted_queries(taches, remaining_logical_queries=2).queries
    )


def test_negative_budget_is_treated_as_zero():
    resultat = select_targeted_queries(
        (_etabli(),), remaining_logical_queries=-3
    )

    assert resultat.queries == ()
