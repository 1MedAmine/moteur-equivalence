# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import configuration
import outil
import verifier_installation
from configuration import B2Config
from recherche_adaptative import ResearchDiagnostics, ResearchOutcome
from modeles import Requirement, RequirementSet
from planification import RequirementExtractionError


def unresolved_outcome():
    return ResearchOutcome(
        "not_resolved",
        RequirementSet(
            product="Produit source",
            criteria=[Requirement(id="r1", label="Tension", requested_value="24 V DC")],
        ),
        None,
        [],
        {},
        ResearchDiagnostics(waves=3, logical_queries=12),
    )


def fiche(tmp_path):
    path = tmp_path / "fiche.txt"
    path.write_text("Contacteur industriel 9 A, bobine 24 V DC, trois pôles.", encoding="utf-8")
    return path


def test_service_routes_the_fiche_to_adaptive_research(tmp_path):
    """Mutation détectée : le CLI conserve le vieux chemin une requête/cinq pages."""
    calls = []

    class Research:
        def run(self, text, brand):
            calls.append((text, brand))
            return unresolved_outcome()

    data = outil.executer(
        str(fiche(tmp_path)),
        "Norel",
        config_loader=lambda: B2Config(api_key="test"),
        research_factory=lambda config: Research(),
    )

    assert data["status"] == "not_resolved"
    assert calls == [("Contacteur industriel 9 A, bobine 24 V DC, trois pôles.", "Norel")]
    assert data["diagnostics"]["logical_queries"] == 12


def test_service_passes_the_source_path_when_research_supports_replay(tmp_path):
    """Régression visée : le manifest hash seulement le texte PDF extrait."""
    source = fiche(tmp_path)
    calls = []

    class Research:
        def run(self, text, brand, *, source_path=None):
            calls.append((text, brand, source_path))
            return unresolved_outcome()

    outil.executer(
        str(source),
        "Norel",
        config_loader=lambda: B2Config(api_key="test"),
        research_factory=lambda config: Research(),
    )

    assert calls == [(
        "Contacteur industriel 9 A, bobine 24 V DC, trois pôles.",
        "Norel",
        str(source),
    )]


def test_service_passes_a_positional_only_source_path(tmp_path):
    """Régression visée : l'introspection appelle un paramètre `/` par mot-clé."""
    source = fiche(tmp_path)
    calls = []

    class Research:
        def run(self, text, brand, source_path, /):
            calls.append((text, brand, source_path))
            return unresolved_outcome()

    data = outil.executer(
        str(source),
        "Norel",
        config_loader=lambda: B2Config(api_key="test"),
        research_factory=lambda config: Research(),
    )

    assert data["status"] == "not_resolved"
    assert calls[0][2] == str(source)


def test_configuration_failure_has_its_own_status(tmp_path):
    """Mutation détectée : une mauvaise configuration ressemble à aucun résultat."""
    def fail():
        raise configuration.ConfigurationIncomplete("B2 invalide")

    data = outil.executer(str(fiche(tmp_path)), None, config_loader=fail)

    assert data["status"] == "configuration_error"
    assert "B2 invalide" in data["warnings"][0]


def test_analysis_failure_is_safe_and_not_not_resolved(tmp_path):
    """Mutation détectée : une panne fournisseur est masquée ou révèle son détail."""
    class Research:
        def run(self, text, brand):
            raise RuntimeError("secret-provider-detail")

    data = outil.executer(
        str(fiche(tmp_path)),
        None,
        config_loader=lambda: B2Config(api_key="test"),
        research_factory=lambda config: Research(),
    )

    assert data["status"] == "analysis_error"
    assert "RuntimeError" in data["warnings"][0]
    assert "secret-provider-detail" not in data["warnings"][0]


def test_interrupted_analysis_returns_a_safe_report(tmp_path):
    """Une interruption du terminal ne doit jamais supprimer la sortie."""
    class Research:
        def run(self, text, brand):
            raise KeyboardInterrupt()

    data = outil.executer(
        str(fiche(tmp_path)),
        None,
        config_loader=lambda: B2Config(api_key="test"),
        research_factory=lambda config: Research(),
    )

    assert data["status"] == "analysis_error"
    assert data["warnings"] == [
        "Analyse interrompue avant qu'un verdict soit rendu."
    ]


def test_requirement_failure_exposes_only_stage_and_path(tmp_path):
    class Research:
        def run(self, text, brand):
            raise RequirementExtractionError({
                "stage": "requirement_initial_extraction",
                "path": "criteria[0].label",
                "issue": "json_parse_error",
                "action": "retry_failed",
                "raw_response": "RAW_MODEL_RESPONSE_SENTINEL",
            })

    data = outil.executer(
        str(fiche(tmp_path)),
        None,
        config_loader=lambda: B2Config(api_key="test"),
        research_factory=lambda config: Research(),
    )
    markdown = outil.rapport.en_markdown(data)

    assert data["status"] == "analysis_error"
    assert data["diagnostics"]["requirement_validation"] == [{
        "stage": "requirement_initial_extraction",
        "path": "criteria[0].label",
        "issue": "json_parse_error",
        "action": "retry_failed",
    }]
    assert "requirement_initial_extraction" in markdown
    assert "criteria[0].label" in markdown
    assert "RAW_MODEL_RESPONSE_SENTINEL" not in str(data)
    assert "RAW_MODEL_RESPONSE_SENTINEL" not in markdown


def test_requirement_structure_failure_keeps_its_safe_path(tmp_path):
    class Research:
        def run(self, text, brand):
            raise RequirementExtractionError({
                "stage": "requirement_initial_extraction",
                "path": "product",
                "issue": "invalid_structure",
                "action": "aborted",
            })

    data = outil.executer(
        str(fiche(tmp_path)),
        None,
        config_loader=lambda: B2Config(api_key="test"),
        research_factory=lambda config: Research(),
    )

    assert data["diagnostics"]["requirement_validation"] == [{
        "stage": "requirement_initial_extraction",
        "path": "product",
        "issue": "invalid_structure",
        "action": "aborted",
    }]


def test_rate_limit_failure_has_service_unavailable_status(tmp_path):
    """Rupture visée : un 429 fatal ressemble à une analyse invalide."""
    class RateLimitError(Exception):
        status_code = 429

    class Research:
        def run(self, text, brand):
            raise RateLimitError("secret-provider-detail")

    data = outil.executer(
        str(fiche(tmp_path)),
        None,
        config_loader=lambda: B2Config(api_key="test"),
        research_factory=lambda config: Research(),
    )

    assert data["status"] == "service_unavailable"
    assert "429" in data["warnings"][0]
    assert "secret-provider-detail" not in data["warnings"][0]


def test_installation_check_never_prints_key_fragments(monkeypatch, capsys):
    """Mutation détectée : le diagnostic imprime préfixe et suffixe du secret."""
    monkeypatch.setattr(configuration, "config_llm", lambda: {
        "api_key": "abcdefgh-super-secret-wxyz",
        "model": "openai/nvidia/model",
        "base_url": "https://provider.example/v1",
        "model_tokens": 256000,
        "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
    })

    assert verifier_installation.controle_configuration()
    output = capsys.readouterr().out
    assert "clé configurée : oui" in output
    assert "abcdefgh" not in output
    assert "wxyz" not in output


def test_cli_treats_not_resolved_as_a_valid_result(monkeypatch, tmp_path):
    """Ne rien prouver est un résultat métier, pas une panne du programme."""
    monkeypatch.setattr(outil, "executer", lambda *_: {
        "status": "not_resolved",
        "warnings": [],
        "sources": [],
        "alternative_proposee": "",
    })
    monkeypatch.setattr(outil.rapport, "ecrire", lambda *_: (
        tmp_path / "alternative.json", tmp_path / "alternative.md"
    ))

    assert outil.main(["--fiche", "fiche.txt"]) == 0
