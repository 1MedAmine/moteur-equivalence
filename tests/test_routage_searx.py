# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from routage_searx import ENGINE_SHORTCUTS, EngineRotation, build_bang_query


def test_rotation_cycles_all_seven_starting_engines():
    """Mutation détectée : certaines requêtes reviennent toujours au même moteur."""
    rotation = EngineRotation(ENGINE_SHORTCUTS)

    starts = [rotation.next_sequence()[0] for _ in range(8)]

    assert starts == ["bi", "ddg", "goc", "nvr", "szn", "qw", "sp", "bi"]


def test_existing_bangs_are_replaced_by_exactly_one_engine():
    """Mutation détectée : SearXNG reçoit deux moteurs contradictoires."""
    assert build_bang_query("nvr", "!ddg contacteur !sp 24 V DC") == (
        "!nvr contacteur 24 V DC"
    )
