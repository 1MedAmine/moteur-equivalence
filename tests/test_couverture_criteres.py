# -*- coding: utf-8 -*-
from __future__ import annotations

import io
import logging
import sys
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

import pytest
from langchain_core.exceptions import OutputParserException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from configuration import B2Config
from modeles import (
    Requirement,
    RequirementAddition,
    RequirementSet,
    RequirementSupplement,
)
from planification import (
    Planner,
    RequirementExtractionError,
    VisualLocation,
    _clip_rectangle,
    _crop_rectangles,
    _has_unit_label_disagreement,
)


def _requirements(*values: str) -> RequirementSet:
    return RequirementSet(
        product="Belvia Z-TRH 4712 0600",
        origin_brand="Belvia",
        criteria=[
            Requirement(
                id=f"c{index}",
                label=f"Critère {index}",
                requested_value=value,
                critical=True,
            )
            for index, value in enumerate(values, start=1)
        ],
    )


def test_compteur_detecte_les_specs_evidentes_absentes_du_requirement_set():
    """Rupture visée : une valeur PDF propre peut disparaître sans aucun signal."""
    from couverture_criteres import evaluer_couverture

    fiche = "Durée de vie : 16 ans\nPoids : 41 g\nIndice de protection : IP65"

    bilan = evaluer_couverture(fiche, _requirements("16 ans"))

    assert [(item.value, item.reason) for item in bilan.orphans] == [
        ("41 g", "missing_spec"),
        ("IP65", "missing_spec"),
    ]


def test_source_initiale_masque_les_cellules_corrompues_sans_perdre_le_lisible():
    """Rupture visée : le premier LLM recopie des contrôles et casse son JSON."""
    from couverture_criteres import neutraliser_source_initiale

    fiche = """Titre Z-TRH
\x11\x0b\x13\x03&'Y
[TABLE page=1 table=1]
row=1 | col=1: Fréquence | col=2: \x11\x0b\x13\x03&'Y | col=3: 868 MHz
row=2 | col=1: Durée de vie | col=2: 16 ans | col=3: 10 ans
[/TABLE]"""

    source = neutraliser_source_initiale(fiche)

    assert "Titre Z-TRH" in source
    assert "16 ans" in source
    assert "[VALEUR ILLISIBLE]" in source
    assert "[LIGNE ILLISIBLE]" in source
    assert not any(ord(character) < 32 and character not in "\r\n\t" for character in source)


def test_compteur_ne_declenche_rien_quand_les_specs_sont_deja_couvertes():
    """Rupture visée : la seconde passe est payée même sans oubli factuel."""
    from couverture_criteres import evaluer_couverture

    fiche = "Durée de vie : 16 ans\nPoids : 41 g\nIndice de protection : IP65"

    bilan = evaluer_couverture(fiche, _requirements("16 ans", "41 g", "IP65"))

    assert bilan.orphans == ()


def test_compteur_compare_des_specs_entieres_et_ne_confond_pas_9a_avec_19a():
    from couverture_criteres import evaluer_couverture

    bilan = evaluer_couverture("Courant nominal : 9 A", _requirements("19 A"))

    assert [(item.value, item.reason) for item in bilan.orphans] == [
        ("9 A", "missing_spec"),
    ]


def test_compteur_conserve_la_page_dune_spec_pdf_hors_tableau():
    from couverture_criteres import evaluer_couverture

    fiche = """[PAGE page=2]
Poids net : 41 g
[/PAGE]"""

    bilan = evaluer_couverture(fiche, _requirements("16 ans"))

    assert [(item.value, item.page) for item in bilan.orphans] == [("41 g", 2)]


def test_compteur_conserve_une_plage_ecrite_avec_des_points_de_suspension():
    """Rupture visée : `0 ... 100%` est réduit à `100%` et perd sa borne basse."""
    from couverture_criteres import evaluer_couverture

    bilan = evaluer_couverture(
        "Plage d'humidité : 0 ... 100%",
        _requirements("16 ans"),
    )

    assert [item.value for item in bilan.orphans] == ["0 ... 100%"]


def test_compteur_signale_la_cellule_corrompue_de_la_colonne_du_produit_seulement():
    """Rupture visée : Z-MAG contamine Z-TRH ou une ligne illisible passe inaperçue."""
    from couverture_criteres import evaluer_couverture

    fiche = """[TABLE page=1 table=1]
row=1 | col=1: Caractéristique | col=2: Z-TRH | col=3: Z-MAG
row=2 | col=1: Fréquence | col=2: \x11ILLISIBLE | col=3: 868 MHz
row=3 | col=1: Durée de vie | col=2: 16 ans | col=3: 10 ans
[/TABLE]"""

    bilan = evaluer_couverture(fiche, _requirements("16 ans"))

    corrupted = [item for item in bilan.orphans if item.reason == "corrupted_cell"]
    assert [(item.label, item.page, item.table, item.column) for item in corrupted] == [
        ("Fréquence", 1, 1, 2),
    ]
    assert all(item.value != "868 MHz" for item in bilan.orphans)


def test_compteur_signale_une_ligne_ambigue_sans_adopter_la_valeur_voisine():
    """Rupture visée : une dérive de grille masque la spec Z-TRH ou copie Z-MAG."""
    from couverture_criteres import evaluer_couverture

    fiche = """[TABLE page=1 table=1]
row=1 | col=1: Caractéristique | col=2: Z-TRH | col=3: Z-MAG
row=2 | col=1: Portée | col=2:  | col=3: \x11\x0b\x13\x03&'Y
row=3 | col=1: Durée de vie | col=2: 16 ans | col=3: 10 ans
[/TABLE]"""

    bilan = evaluer_couverture(fiche, _requirements("16 ans"))

    assert [(item.label, item.reason) for item in bilan.orphans] == [
        ("Portée", "corrupted_cell"),
    ]
    assert bilan.orphans[0].value == ""


def test_compteur_ignore_la_corruption_dune_colonne_soeur_si_la_cible_est_lisible():
    from couverture_criteres import evaluer_couverture

    fiche = """[TABLE page=1 table=1]
row=1 | col=1: Caractéristique | col=2: Z-TRH | col=3: Z-MAG
row=2 | col=1: Fréquence | col=2: 2,4 GHz | col=3: \x11\x0b\x13\x03&'Y
[/TABLE]"""

    bilan = evaluer_couverture(fiche, _requirements("2,4 GHz"))

    assert bilan.orphans == ()


def test_planner_lance_une_seconde_passe_ciblee_et_fusionne_un_ajout_litteral():
    """Rupture visée : un orphelin est détecté mais jamais récupéré."""
    calls: list[dict] = []
    responses = iter([
        _requirements("16 ans").model_dump(),
        {
            "criteria": [{
                "id": "poids",
                "label": "Poids",
                "requested_value": "41 g",
                "critical": False,
                "evidence_excerpt": "Poids : 41 g",
                "page": None,
            }],
        },
    ])

    class Graph:
        def run(self):
            return next(responses)

    def graph_factory(**kwargs):
        calls.append(kwargs)
        return Graph()

    planner = Planner(B2Config(api_key="test"), graph_factory=graph_factory)
    result = planner.extract_requirements(
        "Produit Belvia Z-TRH. Durée de vie : 16 ans. Poids : 41 g."
    )

    assert [item.requested_value for item in result.criteria] == ["16 ans", "41 g"]
    assert len(calls) == 2
    assert calls[1]["schema"].__name__ == "RequirementSupplementEnvelope"
    assert "41 g" in calls[1]["source"]
    assert "Durée de vie : 16 ans" not in calls[1]["source"]


def test_seconde_passe_textuelle_valide_chaque_critere_separement():
    responses = iter([
        _requirements("16 ans").model_dump(),
        {
            "criteria": [
                {
                    "id": "poids",
                    "label": "Poids",
                    "requested_value": "41 g",
                    "critical": False,
                    "evidence_excerpt": "Poids : 41 g",
                },
                {
                    "id": "vide",
                    "label": "Indice de protection",
                    "requested_value": "",
                    "critical": True,
                    "evidence_excerpt": "Indice de protection :",
                },
            ],
        },
    ])

    class Graph:
        def run(self):
            return next(responses)

    planner = Planner(
        B2Config(api_key="test"),
        graph_factory=lambda **_: Graph(),
    )

    result = planner.extract_requirements(
        "Produit Belvia Z-TRH. Durée de vie : 16 ans. Poids : 41 g."
    )

    assert [item.requested_value for item in result.criteria] == ["16 ans", "41 g"]
    assert planner.last_requirement_diagnostics == [{
        "stage": "requirement_text_supplement",
        "path": "criteria[1].requested_value",
        "issue": "invalid_item",
        "action": "discarded",
    }]


def test_schema_scrapegraph_du_complement_ne_recree_pas_le_tout_ou_rien():
    calls = []
    payloads = iter([
        _requirements("16 ans").model_dump(),
        {
            "criteria": [
                {
                    "id": "poids",
                    "label": "Poids",
                    "requested_value": "41 g",
                    "critical": False,
                    "evidence_excerpt": "Poids : 41 g",
                },
                {
                    "id": "vide",
                    "label": "Indice de protection",
                    "requested_value": "",
                    "critical": True,
                    "evidence_excerpt": "Indice de protection :",
                },
            ],
        },
    ])

    def graph_factory(**kwargs):
        calls.append(kwargs)
        payload = next(payloads)

        class Graph:
            def run(self):
                return kwargs["schema"].model_validate(payload)

        return Graph()

    planner = Planner(
        B2Config(api_key="test"),
        graph_factory=graph_factory,
    )

    result = planner.extract_requirements(
        "Produit Belvia Z-TRH. Durée de vie : 16 ans. Poids : 41 g."
    )

    assert [item.requested_value for item in result.criteria] == ["16 ans", "41 g"]
    supplement_schema = calls[1]["schema"].model_json_schema()
    assert calls[1]["schema"].__name__ == "RequirementSupplementEnvelope"
    assert supplement_schema["properties"]["criteria"]["items"] == {
        "type": "object",
        "properties": {
            "id": {"type": "string", "minLength": 1},
            "label": {"type": "string", "minLength": 1},
            "requested_value": {"type": "string", "minLength": 1},
            "critical": {"type": "boolean"},
            "evidence_excerpt": {"type": "string", "minLength": 1},
            "page": {"type": ["integer", "null"], "minimum": 1},
            "table": {"type": ["integer", "null"], "minimum": 1},
            "column": {"type": ["integer", "null"], "minimum": 1},
        },
        "required": [
            "id",
            "label",
            "requested_value",
            "evidence_excerpt",
        ],
    }
    assert planner.last_requirement_diagnostics[0]["path"] == (
        "criteria[1].requested_value"
    )


def test_planner_ne_lance_pas_la_seconde_passe_sans_orphelin():
    """Rupture visée : deux appels LLM deviennent systématiques."""
    calls: list[dict] = []

    class Graph:
        def run(self):
            return _requirements("16 ans", "41 g", "IP65").model_dump()

    def graph_factory(**kwargs):
        calls.append(kwargs)
        return Graph()

    planner = Planner(B2Config(api_key="test"), graph_factory=graph_factory)
    result = planner.extract_requirements(
        "Durée de vie : 16 ans\nPoids : 41 g\nIndice de protection : IP65"
    )

    assert len(result.criteria) == 3
    assert len(calls) == 1


def test_extraction_initiale_valide_chaque_critere_separement():
    class Graph:
        def run(self):
            return {
                "product": "Belvia Z-TRH 4712 0600",
                "origin_brand": "Belvia",
                "criteria": [
                    {
                        "id": "duree_vie",
                        "label": "Durée de vie",
                        "requested_value": "16 ans",
                        "critical": True,
                    },
                    {
                        "id": "vide",
                        "label": "Poids",
                        "requested_value": "",
                        "critical": False,
                    },
                ],
            }

    planner = Planner(
        B2Config(api_key="test"),
        graph_factory=lambda **_: Graph(),
    )

    result = planner.extract_requirements("Durée de vie : 16 ans")

    assert [item.requested_value for item in result.criteria] == ["16 ans"]
    assert planner.last_requirement_diagnostics == [{
        "stage": "requirement_initial_extraction",
        "path": "criteria[1].requested_value",
        "issue": "invalid_item",
        "action": "discarded",
    }]


def test_schema_scrapegraph_initial_ne_recree_pas_le_tout_ou_rien():
    calls = []
    payload = {
        "product": "Belvia Z-TRH 4712 0600",
        "origin_brand": "Belvia",
        "criteria": [
            {
                "id": "duree_vie",
                "label": "Durée de vie",
                "requested_value": "16 ans",
                "critical": True,
            },
            {
                "id": "vide",
                "label": "Poids",
                "requested_value": {"value": "41 g"},
                "critical": False,
            },
        ],
    }

    def graph_factory(**kwargs):
        calls.append(kwargs)

        class Graph:
            def run(self):
                return kwargs["schema"].model_validate(payload)

        return Graph()

    planner = Planner(
        B2Config(api_key="test"),
        graph_factory=graph_factory,
    )

    result = planner.extract_requirements("Durée de vie : 16 ans")

    assert [item.requested_value for item in result.criteria] == ["16 ans"]
    initial_schema = calls[0]["schema"].model_json_schema()
    assert calls[0]["schema"].__name__ == "RequirementSetEnvelope"
    assert initial_schema["properties"]["product"]["type"] == "string"
    assert initial_schema["properties"]["origin_brand"]["type"] == "string"
    assert initial_schema["properties"]["criteria"]["items"] == {
        "type": "object",
        "properties": {
            "id": {"type": "string", "minLength": 1},
            "label": {"type": "string", "minLength": 1},
            "requested_value": {"type": "string", "minLength": 1},
            "critical": {"type": "boolean"},
        },
        "required": ["id", "label", "requested_value"],
    }
    assert planner.last_requirement_diagnostics[0]["path"] == (
        "criteria[1].requested_value"
    )


def test_extraction_initiale_sans_aucun_critere_valide_echoue_avec_le_chemin():
    class Graph:
        def run(self):
            return {
                "product": "Belvia Z-TRH 4712 0600",
                "origin_brand": "Belvia",
                "criteria": [{
                    "id": "vide",
                    "label": "Poids",
                    "requested_value": "",
                    "critical": False,
                }],
            }

    planner = Planner(
        B2Config(api_key="test"),
        graph_factory=lambda **_: Graph(),
    )

    with pytest.raises(RequirementExtractionError) as captured:
        planner.extract_requirements("Produit Belvia Z-TRH")

    assert captured.value.safe_diagnostics == [{
        "stage": "requirement_initial_extraction",
        "path": "criteria[0].requested_value",
        "issue": "invalid_item",
        "action": "discarded",
    }]


def test_extraction_initiale_sans_items_indique_le_chemin_criteria():
    class Graph:
        def run(self):
            return {
                "product": "Belvia Z-TRH 4712 0600",
                "origin_brand": "Belvia",
                "criteria": [],
            }

    planner = Planner(
        B2Config(api_key="test"),
        graph_factory=lambda **_: Graph(),
    )

    with pytest.raises(RequirementExtractionError) as captured:
        planner.extract_requirements("Produit Belvia Z-TRH")

    assert captured.value.safe_diagnostics == [{
        "stage": "requirement_initial_extraction",
        "path": "criteria",
        "issue": "invalid_item",
        "action": "discarded",
    }]


def test_valeur_composee_uniquement_despaces_est_jetee_et_journalisee():
    class Graph:
        def run(self):
            return {
                "product": "Belvia Z-TRH 4712 0600",
                "origin_brand": "Belvia",
                "criteria": [{
                    "id": "poids",
                    "label": "Poids",
                    "requested_value": "   ",
                    "critical": False,
                }],
            }

    planner = Planner(
        B2Config(api_key="test"),
        graph_factory=lambda **_: Graph(),
    )

    with pytest.raises(RequirementExtractionError) as captured:
        planner.extract_requirements("Produit Belvia Z-TRH")

    assert captured.value.safe_diagnostics == [{
        "stage": "requirement_initial_extraction",
        "path": "criteria[0].requested_value",
        "issue": "invalid_item",
        "action": "discarded",
    }]


def test_identifiant_duplique_garde_le_premier_critere_et_jette_le_second():
    class Graph:
        def run(self):
            return {
                "product": "Belvia Z-TRH 4712 0600",
                "origin_brand": "Belvia",
                "criteria": [
                    {
                        "id": "duree",
                        "label": "Durée de vie",
                        "requested_value": "16 ans",
                        "critical": True,
                    },
                    {
                        "id": "duree",
                        "label": "Autonomie",
                        "requested_value": "16 ans",
                        "critical": False,
                    },
                ],
            }

    planner = Planner(
        B2Config(api_key="test"),
        graph_factory=lambda **_: Graph(),
    )

    result = planner.extract_requirements("Durée de vie : 16 ans")

    assert [item.label for item in result.criteria] == ["Durée de vie"]
    assert planner.last_requirement_diagnostics == [{
        "stage": "requirement_initial_extraction",
        "path": "criteria[1].id",
        "issue": "invalid_item",
        "action": "discarded",
    }]


def test_champ_superieur_invalide_echoue_avec_un_diagnostic_sans_valeurs():
    class Graph:
        def run(self):
            return {
                "product": 123456789,
                "origin_brand": "Belvia",
                "criteria": [{
                    "id": "vie",
                    "label": "Durée de vie",
                    "requested_value": "16 ans",
                    "critical": True,
                }],
            }

    planner = Planner(
        B2Config(api_key="test"),
        graph_factory=lambda **_: Graph(),
    )

    with pytest.raises(RequirementExtractionError) as captured:
        planner.extract_requirements("Durée de vie : 16 ans")

    assert captured.value.safe_diagnostics == [{
        "stage": "requirement_initial_extraction",
        "path": "product",
        "issue": "invalid_structure",
        "action": "aborted",
    }]
    assert "123456789" not in str(captured.value)


def test_desaccord_unite_libelle_est_signale_sans_supprimer_le_critere():
    class Graph:
        def run(self):
            return RequirementSet(
                product="Belvia Z-TRH 4712 0600",
                origin_brand="Belvia",
                criteria=[Requirement(
                    id="frequence_transmission",
                    label="Fréquence de transmission",
                    requested_value="Configurable de 0,1 à 10 secondes",
                    critical=True,
                )],
            )

    planner = Planner(
        B2Config(api_key="test"),
        graph_factory=lambda **_: Graph(),
    )

    result = planner.extract_requirements(
        "Fréquence de transmission : configurable de 0,1 à 10 secondes"
    )

    assert result.criteria[0].requested_value == "Configurable de 0,1 à 10 secondes"
    assert planner.last_requirement_diagnostics == [{
        "stage": "requirement_initial_extraction",
        "path": "criteria[0].requested_value",
        "issue": "unit_label_disagreement",
        "action": "review",
    }]


def test_deux_familles_unites_signalent_le_critere_sans_le_neutraliser():
    requested_value = "Jusqu'à 10 mètres en secondes"

    class Graph:
        def run(self):
            return RequirementSet(
                product="Belvia Z-TRH 4712 0600",
                origin_brand="Belvia",
                criteria=[Requirement(
                    id="portee",
                    label="Portée",
                    requested_value=requested_value,
                    critical=True,
                )],
            )

    planner = Planner(
        B2Config(api_key="test"),
        graph_factory=lambda **_: Graph(),
    )

    result = planner.extract_requirements(
        f"Portée : {requested_value}"
    )

    assert len(result.criteria) == 1
    assert result.criteria[0].requested_value == requested_value
    assert result.criteria[0].critical
    assert planner.last_requirement_diagnostics == [{
        "stage": "requirement_initial_extraction",
        "path": "criteria[0].requested_value",
        "issue": "unit_label_disagreement",
        "action": "review",
    }]


def test_une_unite_de_distance_coherente_ne_declenche_pas_de_signalement():
    requirement = Requirement(
        id="portee",
        label="Portée",
        requested_value="Jusqu'à 10 mètres",
        critical=True,
    )

    assert not _has_unit_label_disagreement(requirement)


def test_desaccord_unite_garde_lindice_original_apres_un_item_jete():
    class Graph:
        def run(self):
            return {
                "product": "Belvia Z-TRH 4712 0600",
                "origin_brand": "Belvia",
                "criteria": [
                    {
                        "id": "vide",
                        "label": "Poids",
                        "requested_value": "",
                        "critical": False,
                    },
                    {
                        "id": "frequence_transmission",
                        "label": "Fréquence de transmission",
                        "requested_value": "Configurable de 0,1 à 10 secondes",
                        "critical": True,
                    },
                ],
            }

    planner = Planner(
        B2Config(api_key="test"),
        graph_factory=lambda **_: Graph(),
    )

    result = planner.extract_requirements(
        "Fréquence de transmission : configurable de 0,1 à 10 secondes"
    )

    assert result.criteria[0].id == "frequence_transmission"
    assert planner.last_requirement_diagnostics == [
        {
            "stage": "requirement_initial_extraction",
            "path": "criteria[0].requested_value",
            "issue": "invalid_item",
            "action": "discarded",
        },
        {
            "stage": "requirement_initial_extraction",
            "path": "criteria[1].requested_value",
            "issue": "unit_label_disagreement",
            "action": "review",
        },
    ]


def test_json_initial_casse_est_repris_une_seule_fois_sans_fuite_brute(capsys):
    calls = []
    outcomes = iter([
        OutputParserException(
            "RAW_MODEL_RESPONSE_SENTINEL",
            llm_output=(
                '{"product":"Z-TRH","criteria":[{"id":"ref",'
                '"label":"label":"Référence"}]}'
            ),
        ),
        _requirements("16 ans").model_dump(),
    ])

    class Graph:
        def __init__(self, outcome):
            self.outcome = outcome

        def run(self):
            if isinstance(self.outcome, BaseException):
                print("RAW_MODEL_RESPONSE_SENTINEL")
                raise self.outcome
            return self.outcome

    def graph_factory(**kwargs):
        calls.append(kwargs)
        return Graph(next(outcomes))

    planner = Planner(
        B2Config(api_key="test"),
        graph_factory=graph_factory,
    )

    result = planner.extract_requirements("Durée de vie : 16 ans")

    assert result.criteria[0].requested_value == "16 ans"
    assert len(calls) == 2
    assert "REPRISE DE PARSING" in calls[1]["prompt"]
    assert planner.last_requirement_diagnostics == [{
        "stage": "requirement_initial_extraction",
        "path": "criteria[0].label",
        "issue": "json_parse_error",
        "action": "retried",
    }]
    captured = capsys.readouterr()
    assert "RAW_MODEL_RESPONSE_SENTINEL" not in captured.out
    assert "RAW_MODEL_RESPONSE_SENTINEL" not in captured.err


def test_cle_json_arbitraire_ne_devient_jamais_un_chemin_public():
    outcomes = iter([
        OutputParserException(
            "erreur de parsing",
            llm_output='{"RAW_MODEL_RESPONSE_SENTINEL":',
        ),
        _requirements("16 ans").model_dump(),
    ])

    class Graph:
        def __init__(self, outcome):
            self.outcome = outcome

        def run(self):
            if isinstance(self.outcome, BaseException):
                raise self.outcome
            return self.outcome

    planner = Planner(
        B2Config(api_key="test"),
        graph_factory=lambda **_: Graph(next(outcomes)),
    )

    planner.extract_requirements("Durée de vie : 16 ans")

    assert planner.last_requirement_diagnostics[0]["path"] == "$"
    assert "RAW_MODEL_RESPONSE_SENTINEL" not in str(
        planner.last_requirement_diagnostics
    )


def test_logger_scrapegraph_ne_peut_pas_imprimer_la_reponse_brute():
    stream = io.StringIO()
    logger = logging.getLogger("scrapegraphai.parser.test")
    handler = logging.StreamHandler(stream)
    old_level = logger.level
    old_propagate = logger.propagate
    logger.addHandler(handler)
    logger.setLevel(logging.ERROR)
    logger.propagate = False
    outcomes = iter([
        OutputParserException("erreur de parsing"),
        _requirements("16 ans").model_dump(),
    ])

    class Graph:
        def __init__(self, outcome):
            self.outcome = outcome

        def run(self):
            if isinstance(self.outcome, BaseException):
                logger.error("RAW_MODEL_RESPONSE_SENTINEL")
                raise self.outcome
            return self.outcome

    try:
        planner = Planner(
            B2Config(api_key="test"),
            graph_factory=lambda **_: Graph(next(outcomes)),
        )
        planner.extract_requirements("Durée de vie : 16 ans")
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)
        logger.propagate = old_propagate

    assert "RAW_MODEL_RESPONSE_SENTINEL" not in stream.getvalue()


def test_deux_extractions_simultanees_restaurent_les_logs_sans_fuite():
    stream = io.StringIO()
    logger = logging.getLogger("scrapegraphai.parser.concurrent")
    handler = logging.StreamHandler(stream)
    old_level = logger.level
    old_propagate = logger.propagate
    old_disable = logging.root.manager.disable
    first_inside = Event()
    second_ready = Event()
    first_finished = Event()
    errors = []

    class FirstGraph:
        def run(self):
            first_inside.set()
            if not second_ready.wait(1):
                raise AssertionError("la seconde extraction n'a pas démarré")
            return _requirements("16 ans").model_dump()

    class SecondGraph:
        def run(self):
            first_finished.wait(2)
            logger.error("RAW_MODEL_RESPONSE_SENTINEL")
            return _requirements("16 ans").model_dump()

    def second_graph_factory(**_):
        # Le factory est exécuté juste avant l'acquisition de la garde : cet
        # événement prouve que le second thread tente réellement d'entrer.
        second_ready.set()
        return SecondGraph()

    first = Planner(
        B2Config(api_key="test"),
        graph_factory=lambda **_: FirstGraph(),
    )
    second = Planner(
        B2Config(api_key="test"),
        graph_factory=second_graph_factory,
    )

    def run_first():
        try:
            first.extract_requirements("Durée de vie : 16 ans")
        except Exception as error:  # pragma: no cover - rendu par l'assertion
            errors.append(error)
        finally:
            first_finished.set()

    def run_second():
        try:
            second.extract_requirements("Durée de vie : 16 ans")
        except Exception as error:  # pragma: no cover - rendu par l'assertion
            errors.append(error)

    logger.addHandler(handler)
    logger.setLevel(logging.ERROR)
    logger.propagate = False
    first_thread = Thread(target=run_first)
    second_thread = Thread(target=run_second)
    try:
        first_thread.start()
        assert first_inside.wait(1)
        second_thread.start()
        first_thread.join(3)
        second_thread.join(3)
        final_disable = logging.root.manager.disable
    finally:
        logging.disable(old_disable)
        logger.removeHandler(handler)
        logger.setLevel(old_level)
        logger.propagate = old_propagate

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert errors == []
    assert final_disable == old_disable
    assert "RAW_MODEL_RESPONSE_SENTINEL" not in stream.getvalue()


def test_json_valide_en_erreur_de_schema_est_valide_par_item_sans_reprise():
    calls = []
    valid_json = """{
      "product":"Belvia Z-TRH 4712 0600",
      "origin_brand":"Belvia",
      "criteria":[
        {"id":"vie","label":"Durée de vie","requested_value":"16 ans","critical":true},
        {"id":"vide","label":"Poids","requested_value":"","critical":false}
      ]
    }"""

    class Graph:
        def run(self):
            raise OutputParserException(
                "validation de schéma",
                llm_output=valid_json,
            )

    def graph_factory(**kwargs):
        calls.append(kwargs)
        return Graph()

    planner = Planner(
        B2Config(api_key="test"),
        graph_factory=graph_factory,
    )

    result = planner.extract_requirements("Durée de vie : 16 ans")

    assert len(calls) == 1
    assert [item.requested_value for item in result.criteria] == ["16 ans"]
    assert planner.last_requirement_diagnostics == [{
        "stage": "requirement_initial_extraction",
        "path": "criteria[1].requested_value",
        "issue": "invalid_item",
        "action": "discarded",
    }]


def test_json_visuel_casse_est_repris_une_seule_fois():
    table = """[TABLE page=2 table=1]
[GEOMETRY bbox=0,0,300,100 columns=0:100;100:200;200:300]
row=1 | col=1: Caractéristique | col=2: Z-TRH | col=3: Z-MAG
row=2 | col=1: Poids | col=2: 41 g | col=3: 50 g
[/TABLE]"""
    vision_calls = []
    contents = iter([
        '{"criteria":[{"label": "label": "Poids"}]}',
        """{"criteria":[{
          "id":"poids","label":"Poids","requested_value":"41 g",
          "critical":false,"evidence_excerpt":"Poids : 41 g",
          "page":2,"table":1,"column":2
        }]}""",
    ])

    class Graph:
        def run(self):
            return _requirements("16 ans").model_dump()

    class Completions:
        def create(self, **kwargs):
            vision_calls.append(kwargs)
            message = type("Message", (), {"content": next(contents)})()
            return type("Response", (), {"choices": [
                type("Choice", (), {"message": message})()
            ]})()

    client = type(
        "Client",
        (),
        {"chat": type("Chat", (), {"completions": Completions()})()},
    )()
    planner = Planner(
        B2Config(api_key="test"),
        graph_factory=lambda **_: Graph(),
        vision_client_factory=lambda **_: client,
        page_renderer=lambda *_: [
            (2, 1, 2, "produit", "data:image/png;base64,AAA")
        ],
    )

    result = planner.extract_requirements(table, source_path="belvia.pdf")

    assert [item.requested_value for item in result.criteria] == ["16 ans", "41 g"]
    assert len(vision_calls) == 2
    recovery_content = vision_calls[1]["messages"][1]["content"]
    assert any(
        item.get("type") == "text" and "REPRISE DE PARSING" in item.get("text", "")
        for item in recovery_content
    )
    assert planner.last_requirement_diagnostics == [{
        "stage": "requirement_visual_supplement",
        "path": "criteria[0].label",
        "issue": "json_parse_error",
        "action": "retried",
    }]


def test_json_visuel_valide_mais_hors_contrat_nest_pas_repris():
    table = """[TABLE page=2 table=1]
[GEOMETRY bbox=0,0,300,100 columns=0:100;100:200;200:300]
row=1 | col=1: Caractéristique | col=2: Z-TRH | col=3: Z-MAG
row=2 | col=1: Poids | col=2: 41 g | col=3: 50 g
[/TABLE]"""
    vision_calls = []

    class Graph:
        def run(self):
            return _requirements("16 ans").model_dump()

    class Completions:
        def create(self, **kwargs):
            vision_calls.append(kwargs)
            message = type("Message", (), {"content": "[]"})()
            return type("Response", (), {"choices": [
                type("Choice", (), {"message": message})()
            ]})()

    client = type(
        "Client",
        (),
        {"chat": type("Chat", (), {"completions": Completions()})()},
    )()
    planner = Planner(
        B2Config(api_key="test"),
        graph_factory=lambda **_: Graph(),
        vision_client_factory=lambda **_: client,
        page_renderer=lambda *_: [
            (2, 1, 2, "produit", "data:image/png;base64,AAA")
        ],
    )

    with pytest.raises(RequirementExtractionError) as captured:
        planner.extract_requirements(table, source_path="belvia.pdf")

    assert len(vision_calls) == 1
    assert captured.value.safe_diagnostics == [{
        "stage": "requirement_visual_supplement",
        "path": "$",
        "issue": "invalid_structure",
        "action": "aborted",
    }]


def test_deux_json_initiaux_casses_arretent_apres_la_reprise_unique():
    calls = []

    class Graph:
        def run(self):
            raise OutputParserException("RAW_MODEL_RESPONSE_SENTINEL")

    def graph_factory(**kwargs):
        calls.append(kwargs)
        return Graph()

    planner = Planner(
        B2Config(api_key="test"),
        graph_factory=graph_factory,
    )

    with pytest.raises(RequirementExtractionError) as captured:
        planner.extract_requirements("Durée de vie : 16 ans")

    assert len(calls) == 2
    assert captured.value.safe_diagnostics == [{
        "stage": "requirement_initial_extraction",
        "path": "$",
        "issue": "json_parse_error",
        "action": "retry_failed",
    }]
    assert "RAW_MODEL_RESPONSE_SENTINEL" not in str(captured.value)


def test_fusion_refuse_un_ajout_absent_du_texte_source():
    """Rupture visée : la passe de complément contourne l'anti-invention."""
    responses = iter([
        _requirements("16 ans").model_dump(),
        {
            "criteria": [{
                "id": "poids",
                "label": "Poids",
                "requested_value": "99 kg",
                "critical": False,
                "evidence_excerpt": "Poids : 99 kg",
                "page": None,
            }],
        },
    ])

    class Graph:
        def run(self):
            return next(responses)

    planner = Planner(
        B2Config(api_key="test"),
        graph_factory=lambda **_: Graph(),
    )

    result = planner.extract_requirements(
        "Produit Belvia Z-TRH. Durée de vie : 16 ans. Poids : 41 g."
    )

    assert [item.requested_value for item in result.criteria] == ["16 ans"]


def test_cellule_corrompue_declenche_une_seule_passe_visuelle_sur_sa_page():
    """Rupture visée : la seconde passe reçoit encore le texte illisible."""
    calls = []
    table = """[TABLE page=1 table=1]
[GEOMETRY bbox=0,0,300,100 columns=0:100;100:200;200:300]
row=1 | col=1: Caractéristique | col=2: Z-TRH | col=3: Z-MAG
row=2 | col=1: Fréquence | col=2: \x11\x0b\x13\x03&'Y | col=3: 868 MHz
row=3 | col=1: Durée de vie | col=2: 16 ans | col=3: 10 ans
[/TABLE]"""

    class Graph:
        def run(self):
            return _requirements("16 ans").model_dump()

    def supplement_extractor(requirements, coverage, fiche, source_path):
        calls.append((requirements, coverage, fiche, source_path))
        return RequirementSupplement(criteria=[RequirementAddition(
            id="frequence",
            label="Fréquence",
            requested_value="2,4 GHz",
            critical=True,
            evidence_excerpt="Fréquence : 2,4 GHz - Bluetooth Low Energy 4.0/4.2",
            page=1,
            table=1,
            column=2,
        )])

    planner = Planner(
        B2Config(api_key="test"),
        graph_factory=lambda **_: Graph(),
        supplement_extractor=supplement_extractor,
    )

    result = planner.extract_requirements(table, source_path="belvia.pdf")

    assert [item.requested_value for item in result.criteria] == ["16 ans", "2,4 GHz"]
    assert len(calls) == 1
    assert calls[0][1].orphans[0].page == 1
    assert calls[0][3] == "belvia.pdf"


def test_fusion_visuelle_accepte_un_libelle_court_inclus_dans_la_ligne_orpheline():
    """Rupture visée : `Fréquence` est rejeté face à un en-tête Camelot enrichi."""
    table = """[TABLE page=1 table=1]
[GEOMETRY bbox=0,0,300,100 columns=0:100;100:200;200:300]
row=1 | col=1: Caractéristique | col=2: Z-TRH | col=3: Z-MAG
row=2 | col=1: Caractéristiques de fonctionnement Fréquence | col=2: \x11\x0b\x13\x03&'Y | col=3: 868 MHz
row=3 | col=1: Durée de vie | col=2: 16 ans | col=3: 10 ans
[/TABLE]"""

    class Graph:
        def run(self):
            return _requirements("16 ans").model_dump()

    def supplement_extractor(*_):
        return RequirementSupplement(criteria=[RequirementAddition(
            id="frequence",
            label="Fréquence",
            requested_value="2,4 GHz",
            critical=True,
            evidence_excerpt="Fréquence : 2,4 GHz",
            page=1,
            table=1,
            column=2,
        )])

    planner = Planner(
        B2Config(api_key="test"),
        graph_factory=lambda **_: Graph(),
        supplement_extractor=supplement_extractor,
    )

    result = planner.extract_requirements(table, source_path="belvia_z-trh.pdf")

    assert [item.requested_value for item in result.criteria] == ["16 ans", "2,4 GHz"]


def test_fusion_visuelle_separe_une_nouvelle_grandeur_dune_valeur_deja_connue():
    """Rupture visée : température et humidité deviennent un doublon pondéré deux fois."""
    table = """[TABLE page=1 table=1]
[GEOMETRY bbox=0,0,300,100 columns=0:100;100:200;200:300]
row=1 | col=1: Caractéristique | col=2: Z-TRH | col=3: Z-MAG
row=2 | col=1: Plage | col=2: \x11\x0b\x13\x03&'Y Humidité : 0 ... 100% | col=3: -
[/TABLE]"""

    initial = RequirementSet(
        product="Belvia Z-TRH 4712 0600",
        criteria=[Requirement(
            id="plage_humidite",
            label="Plage d'humidité",
            requested_value="0 ... 100%",
        )],
    )

    class Graph:
        def run(self):
            return initial.model_dump()

    def supplement_extractor(*_):
        return RequirementSupplement(criteria=[RequirementAddition(
            id="plage",
            label="Plage",
            requested_value="Température : -40 ... +85°C Humidité : 0 ... 100%",
            critical=True,
            evidence_excerpt="Température : -40 ... +85°C Humidité : 0 ... 100%",
            page=1,
            table=1,
            column=2,
        )])

    planner = Planner(
        B2Config(api_key="test"),
        graph_factory=lambda **_: Graph(),
        supplement_extractor=supplement_extractor,
    )

    result = planner.extract_requirements(table, source_path="belvia_z-trh.pdf")

    assert [item.requested_value for item in result.criteria] == [
        "0 ... 100%",
        "-40 ... +85°C",
    ]
    assert all("Humidité" not in item.requested_value for item in result.criteria[1:])


def test_fusion_visuelle_garde_une_precision_composee_sans_egalite_exacte():
    """Rupture visée : `90-100%` est confondu avec la plage connue `0-100%`."""
    table = """[TABLE page=1 table=1]
[GEOMETRY bbox=0,0,300,100 columns=0:100;100:200;200:300]
row=1 | col=1: Caractéristique | col=2: Z-TRH | col=3: Z-MAG
row=2 | col=1: Précision | col=2: \x11\x0b\x13\x03&'Y | col=3: -
[/TABLE]"""
    initial = RequirementSet(
        product="Belvia Z-TRH 4712 0600",
        criteria=[Requirement(
            id="plage_humidite",
            label="Plage d'humidité",
            requested_value="0 ... 100%",
        )],
    )

    class Graph:
        def run(self):
            return initial.model_dump()

    precision = (
        "Température : ± 0,4°C Humidité : ± 2,5% max (0 - 90%); "
        "± 3,5% max (90 - 100%)"
    )
    planner = Planner(
        B2Config(api_key="test"),
        graph_factory=lambda **_: Graph(),
        supplement_extractor=lambda *_: RequirementSupplement(criteria=[
            RequirementAddition(
                id="precision",
                label="Précision",
                requested_value=precision,
                critical=True,
                evidence_excerpt=precision,
                page=1,
                table=1,
                column=2,
            )
        ]),
    )

    result = planner.extract_requirements(table, source_path="belvia_z-trh.pdf")

    assert [item.requested_value for item in result.criteria] == [
        "0 ... 100%",
        precision,
    ]


def test_passe_visuelle_par_defaut_rend_uniquement_les_pages_orphelines():
    """Rupture visée : le flux réel repasse par le texte corrompu ou tout le PDF."""
    graph_calls = []
    vision_calls = []
    render_calls = []
    table = """[TABLE page=3 table=1]
[GEOMETRY bbox=0,0,300,100 columns=0:100;100:200;200:300]
row=1 | col=1: Caractéristique | col=2: Z-TRH | col=3: Z-MAG
row=2 | col=1: Fréquence | col=2: \x11\x0b\x13\x03&'Y | col=3: 868 MHz
row=3 | col=1: Durée de vie | col=2: 16 ans | col=3: 10 ans
[/TABLE]"""

    class Graph:
        def run(self):
            return _requirements("16 ans").model_dump()

    def graph_factory(**kwargs):
        graph_calls.append(kwargs)
        return Graph()

    class Completions:
        def create(self, **kwargs):
            vision_calls.append(kwargs)
            message = type("Message", (), {"content": """{
              "criteria": [{
                "id": "frequence",
                "label": "Fréquence",
                "requested_value": "2,4 GHz",
                "critical": true,
                "evidence_excerpt": "Fréquence : 2,4 GHz - Bluetooth Low Energy 4.0/4.2",
                "page": 3,
                "table": 1,
                "column": 2
              }]
            }"""})()
            choice = type("Choice", (), {"message": message})()
            return type("Response", (), {"choices": [choice]})()

    client = type(
        "Client",
        (),
        {"chat": type("Chat", (), {"completions": Completions()})()},
    )()

    def render(path, locations):
        render_calls.append((path, locations))
        return [
            (3, 1, 2, "libelles", "data:image/png;base64,AAA"),
            (3, 1, 2, "produit", "data:image/png;base64,BBB"),
        ]

    planner = Planner(
        B2Config(api_key="test"),
        graph_factory=graph_factory,
        vision_client_factory=lambda **_: client,
        page_renderer=render,
    )

    result = planner.extract_requirements(table, source_path="belvia.pdf")

    assert [item.requested_value for item in result.criteria] == ["16 ans", "2,4 GHz"]
    assert len(graph_calls) == 1
    assert len(vision_calls) == 1
    location = render_calls[0][1][0]
    assert (location.page, location.table, location.column) == (3, 1, 2)
    assert vision_calls[0]["messages"][0] == {
        "role": "system",
        "content": "/no_think",
    }
    content = vision_calls[0]["messages"][1]["content"]
    assert [item["type"] for item in content] == [
        "text", "text", "image_url", "text", "image_url",
    ]
    assert content[1]["text"] == "page=3 table=1 colonne=2 rôle=libelles"
    assert content[3]["text"] == "page=3 table=1 colonne=2 rôle=produit"
    prompt = content[0]["text"]
    assert "TRANSCRIPTION EXHAUSTIVE" in prompt
    assert "Fréquence" in prompt
    assert "CRITÈRES DÉJÀ EXTRAITS" not in prompt
    assert "CHECKLIST ORPHELINE" in prompt
    assert "INTERDITS DANS LA SORTIE" not in prompt


def test_pdf_avec_spec_lisible_manquante_utilise_la_passe_visuelle_de_sa_page():
    graph_calls = []
    vision_calls = []
    render_calls = []
    table = """[TABLE page=4 table=1]
[GEOMETRY bbox=0,0,300,100 columns=0:100;100:200;200:300]
row=1 | col=1: Caractéristique | col=2: Z-TRH | col=3: Z-MAG
row=2 | col=1: Durée de vie | col=2: 16 ans | col=3: 10 ans
row=3 | col=1: Poids | col=2: 41 g | col=3: 50 g
[/TABLE]"""

    class Graph:
        def run(self):
            return _requirements("16 ans").model_dump()

    def graph_factory(**kwargs):
        graph_calls.append(kwargs)
        return Graph()

    class Completions:
        def create(self, **kwargs):
            vision_calls.append(kwargs)
            message = type("Message", (), {"content": """{
              "criteria": [{
                "id": "poids",
                "label": "Poids",
                "requested_value": "41 g",
                "critical": false,
                "evidence_excerpt": "Poids : 41 g",
                "page": 4,
                "table": 1,
                "column": 2
              }]
            }"""})()
            choice = type("Choice", (), {"message": message})()
            return type("Response", (), {"choices": [choice]})()

    client = type(
        "Client",
        (),
        {"chat": type("Chat", (), {"completions": Completions()})()},
    )()
    planner = Planner(
        B2Config(api_key="test"),
        graph_factory=graph_factory,
        vision_client_factory=lambda **_: client,
        page_renderer=lambda path, locations: (
            render_calls.append((path, locations))
            or [
                (4, 1, 2, "libelles", "data:image/png;base64,AAA"),
                (4, 1, 2, "produit", "data:image/png;base64,BBB"),
            ]
        ),
    )

    result = planner.extract_requirements(table, source_path="belvia.pdf")

    assert [item.requested_value for item in result.criteria] == ["16 ans", "41 g"]
    assert len(graph_calls) == 1
    assert len(vision_calls) == 1
    location = render_calls[0][1][0]
    assert (location.page, location.table, location.column) == (4, 1, 2)


def test_passe_visuelle_refuse_une_valeur_retournee_depuis_la_colonne_soeur():
    table = """[TABLE page=2 table=1]
[GEOMETRY bbox=0,0,300,100 columns=0:100;100:200;200:300]
row=1 | col=1: Caractéristique | col=2: Z-TRH | col=3: Z-MAG
row=2 | col=1: Fréquence | col=2: \x11ILLISIBLE | col=3: 868 MHz
row=3 | col=1: Durée de vie | col=2: 16 ans | col=3: 10 ans
[/TABLE]"""

    class Graph:
        def run(self):
            return _requirements("16 ans").model_dump()

    class Completions:
        def create(self, **kwargs):
            message = type("Message", (), {"content": """{
              "criteria": [{
                "id": "frequence",
                "label": "Fréquence",
                "requested_value": "868 MHz",
                "critical": true,
                "evidence_excerpt": "Fréquence : 868 MHz",
                "page": 2,
                "table": 1,
                "column": 2
              }]
            }"""})()
            return type("Response", (), {"choices": [
                type("Choice", (), {"message": message})()
            ]})()

    client = type(
        "Client",
        (),
        {"chat": type("Chat", (), {"completions": Completions()})()},
    )()
    planner = Planner(
        B2Config(api_key="test"),
        graph_factory=lambda **_: Graph(),
        vision_client_factory=lambda **_: client,
        page_renderer=lambda *_: [
            (2, 1, 2, "produit", "data:image/png;base64,AAA")
        ],
    )

    result = planner.extract_requirements(table, source_path="belvia.pdf")

    assert [item.requested_value for item in result.criteria] == ["16 ans"]


def test_passe_visuelle_garde_les_criteres_valides_et_journalise_le_vide():
    """Un item vide ne doit plus invalider tout le complément visuel."""
    table = """[TABLE page=2 table=1]
[GEOMETRY bbox=0,0,300,100 columns=0:100;100:200;200:300]
row=1 | col=1: Caractéristique | col=2: Z-TRH | col=3: Z-MAG
row=2 | col=1: Poids | col=2: 41 g | col=3: 50 g
[/TABLE]"""

    class Graph:
        def run(self):
            return _requirements("16 ans").model_dump()

    class Completions:
        def create(self, **kwargs):
            message = type("Message", (), {"content": """{
              "criteria": [
                {
                  "id": "poids",
                  "label": "Poids",
                  "requested_value": "41 g",
                  "critical": false,
                  "evidence_excerpt": "Poids : 41 g",
                  "page": 2,
                  "table": 1,
                  "column": 2
                },
                {
                  "id": "frequence",
                  "label": "Fréquence",
                  "requested_value": "",
                  "critical": true,
                  "evidence_excerpt": "Fréquence :",
                  "page": 2,
                  "table": 1,
                  "column": 2
                }
              ]
            }"""})()
            return type("Response", (), {"choices": [
                type("Choice", (), {"message": message})()
            ]})()

    client = type(
        "Client",
        (),
        {"chat": type("Chat", (), {"completions": Completions()})()},
    )()
    planner = Planner(
        B2Config(api_key="test"),
        graph_factory=lambda **_: Graph(),
        vision_client_factory=lambda **_: client,
        page_renderer=lambda *_: [
            (2, 1, 2, "produit", "data:image/png;base64,AAA")
        ],
    )

    result = planner.extract_requirements(table, source_path="belvia.pdf")

    assert [item.requested_value for item in result.criteria] == ["16 ans", "41 g"]
    assert planner.last_requirement_diagnostics == [{
        "stage": "requirement_visual_supplement",
        "path": "criteria[1].requested_value",
        "issue": "invalid_item",
        "action": "discarded",
    }]


def test_rendu_visuel_recadre_sur_le_plus_grand_tableau_technique():
    """Rupture visée : le tableau reste minuscule dans une page entière."""
    from planification import _largest_table_bbox

    page = SimpleNamespace(find_tables=lambda: SimpleNamespace(tables=[
        SimpleNamespace(bbox=(10.0, 20.0, 410.0, 320.0)),
        SimpleNamespace(bbox=(10.0, 350.0, 210.0, 420.0)),
    ]))

    assert _largest_table_bbox(page) == (10.0, 20.0, 410.0, 320.0)


def test_rendu_geometrique_exclut_mecaniquement_la_colonne_soeur():
    location = VisualLocation(
        page=2,
        table=1,
        column=2,
        table_bbox=(10.0, 20.0, 310.0, 120.0),
        column_bounds=((10.0, 110.0), (110.0, 210.0), (210.0, 310.0)),
    )

    crops = _crop_rectangles(500.0, location)

    assert crops == (
        ("libelles", (10.0, 380.0, 110.0, 480.0)),
        ("produit", (110.0, 380.0, 210.0, 480.0)),
    )
    assert all(rect[2] <= 210.0 for _role, rect in crops)


def test_clip_reel_garde_les_bornes_horizontales_strictes():
    clip = _clip_rectangle(
        page_bounds=(0.0, 0.0, 400.0, 500.0),
        crop=(110.0, 380.0, 210.0, 480.0),
    )

    assert clip == (110.0, 372.0, 210.0, 488.0)
