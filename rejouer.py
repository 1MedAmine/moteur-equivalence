# -*- coding: utf-8 -*-
"""Rejouer localement un corpus B2 sans réseau, LLM ni ScrapeGraphAI."""

from __future__ import annotations

import argparse
import json
import sys

from rejeu import ReplayCorpusError, replay_corpus


def main(argv: list[str] | None = None) -> int:
    # Les extraits verifies peuvent contenir tout Unicode. Sur Windows, la
    # page de code historique (souvent CP1252) ne peut pas toujours les
    # imprimer, alors que le contrat du CLI est un JSON UTF-8.
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--corpus",
        required=True,
        help="dossier contenant pages.json, audits.json, requirements.json et manifest.json",
    )
    arguments = parser.parse_args(argv)
    try:
        result = replay_corpus(arguments.corpus)
    except ReplayCorpusError as error:
        print(f"rejeu impossible : {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
