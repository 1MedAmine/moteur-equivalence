# -*- coding: utf-8 -*-
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from configuration import B2Config
import rejeu
from modeles import (
    CandidateAudit,
    CriterionAudit,
    PageAudit,
    Requirement,
    RequirementSet,
    SourceProof,
)
from recherche_adaptative import ResearchDiagnostics, ResearchOutcome
from rejeu import ReplayCorpusError, replay_corpus, write_replay_corpus
import rejouer


URL = "https://new.norel.example/products/ref-1?utm_source=test#details"
CANONICAL_URL = "https://new.norel.example/products/ref-1"
RAW_PAGE = "Norel REF-1. Contacteur tripolaire industriel. RAW_PAGE_SENTINEL"
EXCERPT = "Contacteur tripolaire industriel"


def _requirements() -> RequirementSet:
    return RequirementSet(
        product="Kerion NV1T05BD",
        origin_brand="Kerion Electric",
        criteria=[
            Requirement(
                id="manufacturer",
                label="Fabricant",
                requested_value="Kerion Electric",
            ),
            Requirement(
                id="function",
                label="Fonction",
                requested_value="Contacteur tripolaire industriel",
            ),
        ],
    )


def _audit() -> PageAudit:
    return PageAudit(
        page_url=URL,
        candidates=[CandidateAudit(
            brand="Norel",
            reference="REF-1",
            criteria=[
                CriterionAudit(
                    requirement_id="manufacturer",
                    requested_value="Kerion Electric",
                    status="not_proven",
                ),
                CriterionAudit(
                    requirement_id="function",
                    requested_value="Contacteur tripolaire industriel",
                    observed_value="Contacteur tripolaire industriel",
                    status="proven",
                    proofs=[SourceProof(
                        url=URL,
                        excerpt=EXCERPT,
                        type="web_officiel",
                    )],
                ),
            ],
        )],
    )


def _expected_evaluation() -> dict:
    return {
        "wave": 1,
        "url": URL,
        "reference": "REF-1",
        "brand": "Norel",
        "score": 100,
        "eligible": True,
        "complete": True,
        "proof_url_count": 1,
        "official_proof_count": 1,
        "proven_criteria": ["Fonction"],
        "not_proven_criteria": [],
        "incompatible_criteria": [],
        "unverified_criteria": [],
        "missing_audit_criteria": [],
        "rejected_proof_criteria": [],
        "proof_grades": {"Fonction": "contiguous"},
        "non_applicable_criteria": ["Fabricant"],
    }


def _outcome() -> ResearchOutcome:
    return ResearchOutcome(
        status="complete",
        requirements=_requirements(),
        evaluation=None,
        audits=[_audit()],
        visited_pages={URL: RAW_PAGE},
        diagnostics=ResearchDiagnostics(
            waves=1,
            logical_queries=3,
            candidate_evaluations=[_expected_evaluation()],
        ),
        audit_waves=[1],
        page_waves={CANONICAL_URL: 1},
        replay_waves=[{
            "wave": 1,
            "logical_queries_at_entry": 0,
            "logical_queries_before_near_miss": 3,
            "seen_signatures_at_entry": [],
            "seen_signatures_before_near_miss": [],
        }],
    )


def test_corpus_round_trip_renders_the_full_safe_report_without_raw_leak(
    tmp_path,
):
    """Régression visée : un rejeu change le score ou réémet le corpus brut."""
    source = tmp_path / "fiche Norel.pdf"
    source_bytes = b"synthetic-pdf-bytes"
    source.write_bytes(source_bytes)
    corpus = tmp_path / "corpus"

    write_replay_corpus(
        corpus,
        outcome=_outcome(),
        config=B2Config(api_key="NEVER_SERIALIZE_THIS", model="test/model"),
        target_brand="Norel",
        strict_evidence=False,
        source_path=source,
        source_text="texte extrait distinct des octets",
    )

    assert {path.name for path in corpus.iterdir()} >= {
        ".gitignore",
        "pages.json",
        "audits.json",
        "requirements.json",
        "manifest.json",
    }
    pages = json.loads((corpus / "pages.json").read_text(encoding="utf-8"))
    manifest = json.loads((corpus / "manifest.json").read_text(encoding="utf-8"))
    assert pages == {CANONICAL_URL: RAW_PAGE}
    assert manifest["target_brand"] == "Norel"
    assert manifest["threshold"] == 75
    assert manifest["strict_evidence"] is False
    assert manifest["B2_MODEL"] == "test/model"
    assert manifest["fiche"] == {
        "path": str(source.resolve()),
        "sha256": hashlib.sha256(source_bytes).hexdigest(),
        "hash_scope": "file_bytes",
    }
    assert len(manifest["code"]["sha256"]) == 64
    assert manifest["generated_at"].endswith("Z")

    serialized = "\n".join(
        path.read_text(encoding="utf-8")
        for path in corpus.iterdir()
        if path.suffix == ".json"
    )
    assert "NEVER_SERIALIZE_THIS" not in serialized

    replayed = replay_corpus(corpus)
    assert replayed["status"] == "complete"
    assert replayed["compatibility"]["reference"] == "REF-1"
    assert replayed["compatibility"]["score"] == 100
    assert replayed["criteria"] == [{
        "label": "Fabricant",
        "requested_value": "Kerion Electric",
        "status": "non_applicable",
        "proofs": [],
    }, {
        "label": "Fonction",
        "requested_value": "Contacteur tripolaire industriel",
        "status": "proven",
        "proofs": [{
            "url": URL,
            "excerpt": EXCERPT,
            "type": "web_officiel",
        }],
    }]
    assert replayed["diagnostics"]["candidate_evaluations"] == [
        _expected_evaluation()
    ]
    assert "RAW_PAGE_SENTINEL" not in json.dumps(replayed, ensure_ascii=False)


def test_replay_proposes_a_100_percent_candidate_from_one_secondary_source_as_partial(
    tmp_path,
):
    secondary_url = "https://www.distributeur-a.example/products/ref-1"
    result = _outcome()
    candidate = result.audits[0].candidates[0]
    result.audits[0].page_url = secondary_url
    candidate.criteria[1].proofs = [SourceProof(
        url=secondary_url,
        excerpt=EXCERPT,
        type="web_secondaire",
    )]
    result.visited_pages = {
        secondary_url: f"Norel REF-1. {EXCERPT}.",
    }
    result.audit_waves = [1]
    result.page_waves = {secondary_url: 1}
    corpus = tmp_path / "single-secondary-100"
    write_replay_corpus(
        corpus,
        outcome=result,
        config=B2Config(api_key="secret"),
        target_brand="Norel",
        strict_evidence=False,
        source_path=None,
        source_text="fiche",
    )

    replayed = replay_corpus(corpus)

    assert replayed["status"] == "partial"
    assert replayed["compatibility"]["reference"] == "REF-1"
    assert replayed["compatibility"]["score"] == 100
    assert replayed["compatibility"]["evidence_note"] == (
        "Source unique, non corroborée."
    )


def test_replay_below_threshold_without_incompatibility_is_not_resolved(tmp_path):
    result = _outcome()
    result.requirements.criteria.append(Requirement(
        id="coil",
        label="Tension de bobine",
        requested_value="24 V DC",
    ))
    result.audits[0].candidates[0].criteria.append(CriterionAudit(
        requirement_id="coil",
        requested_value="24 V DC",
        status="not_proven",
    ))
    corpus = tmp_path / "below-threshold"
    write_replay_corpus(
        corpus,
        outcome=result,
        config=B2Config(api_key="secret"),
        target_brand="Norel",
        strict_evidence=False,
        source_path=None,
        source_text="fiche",
    )

    replayed = replay_corpus(corpus)

    assert replayed["status"] == "not_resolved"
    assert replayed["compatibility"] is None
    assert replayed["discarded_candidates"] == []


def test_replay_rejects_an_incomplete_corpus(tmp_path):
    """Régression visée : un corpus partiel produit silencieusement un faux score."""
    corpus = tmp_path / "incomplete"
    corpus.mkdir()
    (corpus / "manifest.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ReplayCorpusError, match="requirements.json"):
        replay_corpus(corpus)


def test_replay_cli_prints_the_full_safe_report(tmp_path, capsys):
    """Régression visée : le CLI réimprime pages.json ou un extrait brut."""
    source = tmp_path / "source.pdf"
    source.write_bytes(b"source")
    corpus = tmp_path / "corpus"
    write_replay_corpus(
        corpus,
        outcome=_outcome(),
        config=B2Config(api_key="secret"),
        target_brand="Norel",
        strict_evidence=False,
        source_path=source,
        source_text="texte",
    )

    assert rejouer.main(["--corpus", str(corpus)]) == 0
    output = capsys.readouterr().out
    result = json.loads(output)
    assert result["status"] == "complete"
    assert result["compatibility"]["reference"] == "REF-1"
    assert result["criteria"][1]["proofs"][0]["url"] == URL
    assert result["diagnostics"]["candidate_evaluations"] == [
        _expected_evaluation()
    ]
    assert "RAW_PAGE_SENTINEL" not in output


def test_replay_cli_forces_utf8_when_windows_code_page_cannot_encode_report(
    tmp_path,
):
    outcome = _outcome()
    unicode_excerpt = "Řídicí stykač tripolaire"
    # La valeur demandee et observee doit rester alignee sur ce nouvel
    # extrait : ce test verifie l'encodage UTF-8 du rejeu, pas la pertinence
    # de la preuve, mais celle-ci reste requise pour que le critere passe
    # `proven` et atteigne la sortie JSON.
    outcome.requirements.criteria[1].requested_value = unicode_excerpt
    outcome.audits[0].candidates[0].criteria[1].requested_value = unicode_excerpt
    outcome.audits[0].candidates[0].criteria[1].observed_value = unicode_excerpt
    outcome.audits[0].candidates[0].criteria[1].proofs[0].excerpt = unicode_excerpt
    outcome.visited_pages[URL] = f"Norel REF-1. {unicode_excerpt}."
    corpus = tmp_path / "unicode-corpus"
    write_replay_corpus(
        corpus,
        outcome=outcome,
        config=B2Config(api_key="secret"),
        target_brand="Norel",
        strict_evidence=False,
        source_path=None,
        source_text="fiche",
    )
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "cp1252"

    completed = subprocess.run(
        [sys.executable, str(Path(rejouer.__file__)), "--corpus", str(corpus)],
        capture_output=True,
        check=False,
        env=environment,
    )

    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    report = json.loads(completed.stdout.decode("utf-8"))
    assert report["criteria"][1]["proofs"][0]["excerpt"] == unicode_excerpt


def test_replay_respects_the_first_wave_where_a_page_was_available(tmp_path):
    """Régression visée : les pages finales réécrivent le score des vagues passées."""
    source = tmp_path / "source.pdf"
    source.write_bytes(b"source")
    outcome = _outcome()
    audit_url = "https://new.norel.example/products/audit-source"
    outcome.audits[0].page_url = audit_url
    outcome.visited_pages = {
        audit_url: "Page Norel qui désigne REF-1 sans reprendre la preuve technique.",
        URL: RAW_PAGE,
    }
    outcome.page_waves = {
        audit_url: 1,
        CANONICAL_URL: 2,
    }
    outcome.replay_waves = [
        {
            "wave": 1,
            "logical_queries_at_entry": 0,
            "logical_queries_before_near_miss": 3,
            "seen_signatures_at_entry": [],
            "seen_signatures_before_near_miss": [],
        },
        {
            "wave": 2,
            "logical_queries_at_entry": 3,
            "logical_queries_before_near_miss": 6,
            "seen_signatures_at_entry": [],
            "seen_signatures_before_near_miss": [],
        },
    ]
    corpus = tmp_path / "corpus"
    write_replay_corpus(
        corpus,
        outcome=outcome,
        config=B2Config(api_key="secret"),
        target_brand="Norel",
        strict_evidence=False,
        source_path=source,
        source_text="texte",
    )

    result = replay_corpus(corpus)["diagnostics"]["candidate_evaluations"]

    assert result[0] == {
        **_expected_evaluation(),
        "wave": 1,
        "url": audit_url,
        "score": 0,
        "eligible": False,
        "complete": False,
        "proof_url_count": 0,
        "official_proof_count": 0,
        "proven_criteria": [],
        "unverified_criteria": ["Fonction"],
        "rejected_proof_criteria": ["Fonction"],
        "proof_grades": {},
    }
    assert result[1] == {**_expected_evaluation(), "wave": 2, "url": audit_url}


@pytest.mark.parametrize("missing_field", [
    "generated_at",
    "target_brand",
    "threshold",
    "strict_evidence",
    "B2_MODEL",
    "code",
    "fiche",
    "audit_waves",
    "page_waves",
    "waves",
])
def test_replay_rejects_each_missing_manifest_field(tmp_path, missing_field):
    """Régression visée : un manifest tronqué produit tout de même un score."""
    source = tmp_path / "source.pdf"
    source.write_bytes(b"source")
    corpus = tmp_path / "corpus"
    write_replay_corpus(
        corpus,
        outcome=_outcome(),
        config=B2Config(api_key="secret"),
        target_brand="Norel",
        strict_evidence=False,
        source_path=source,
        source_text="texte",
    )
    path = corpus / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest.pop(missing_field)
    path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ReplayCorpusError, match="manifest.json"):
        replay_corpus(corpus)


def test_replay_rejects_threshold_above_one_hundred_without_raw_value_error(tmp_path):
    """Régression visée : un seuil 101 traverse le CLI avec un traceback."""
    source = tmp_path / "source.pdf"
    source.write_bytes(b"source")
    corpus = tmp_path / "corpus"
    write_replay_corpus(
        corpus,
        outcome=_outcome(),
        config=B2Config(api_key="secret"),
        target_brand="Norel",
        strict_evidence=False,
        source_path=source,
        source_text="texte",
    )
    path = corpus / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["threshold"] = 101
    path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ReplayCorpusError, match="manifest.json"):
        replay_corpus(corpus)


def test_replay_rejects_a_string_threshold_instead_of_coercing_it(tmp_path):
    """Régression visée : le schéma dit strict mais convertit `"75"` en 75."""
    source = tmp_path / "source.pdf"
    source.write_bytes(b"source")
    corpus = tmp_path / "corpus"
    write_replay_corpus(
        corpus,
        outcome=_outcome(),
        config=B2Config(api_key="secret"),
        target_brand="Norel",
        strict_evidence=False,
        source_path=source,
        source_text="texte",
    )
    path = corpus / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["threshold"] = "75"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ReplayCorpusError, match="manifest.json"):
        replay_corpus(corpus)


def test_replay_rejects_duplicate_or_disordered_waves(tmp_path):
    """Régression visée : deux états de la même vague sont évalués deux fois."""
    source = tmp_path / "source.pdf"
    source.write_bytes(b"source")
    corpus = tmp_path / "corpus"
    write_replay_corpus(
        corpus,
        outcome=_outcome(),
        config=B2Config(api_key="secret"),
        target_brand="Norel",
        strict_evidence=False,
        source_path=source,
        source_text="texte",
    )
    path = corpus / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["waves"].append(dict(manifest["waves"][0]))
    path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ReplayCorpusError, match="manifest.json"):
        replay_corpus(corpus)


def test_replay_rejects_a_wave_sequence_that_does_not_start_at_one(tmp_path):
    """Régression visée : un corpus commençant à la vague 2 omet la vague 1."""
    source = tmp_path / "source.pdf"
    source.write_bytes(b"source")
    corpus = tmp_path / "corpus"
    write_replay_corpus(
        corpus,
        outcome=_outcome(),
        config=B2Config(api_key="secret"),
        target_brand="Norel",
        strict_evidence=False,
        source_path=source,
        source_text="texte",
    )
    path = corpus / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["waves"][0]["wave"] = 2
    manifest["audit_waves"] = [2]
    manifest["page_waves"] = {
        url: 2 for url in manifest["page_waves"]
    }
    path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ReplayCorpusError, match="manifest.json"):
        replay_corpus(corpus)


def test_replay_rejects_a_logical_query_gap_between_waves(tmp_path):
    """Régression visée : le manifest invente une requête entre deux vagues."""
    source = tmp_path / "source.pdf"
    source.write_bytes(b"source")
    corpus = tmp_path / "corpus"
    write_replay_corpus(
        corpus,
        outcome=_outcome(),
        config=B2Config(api_key="secret"),
        target_brand="Norel",
        strict_evidence=False,
        source_path=source,
        source_text="texte",
    )
    path = corpus / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["waves"].append({
        "wave": 2,
        "logical_queries_at_entry": 4,
        "logical_queries_before_near_miss": 6,
        "seen_signatures_at_entry": [],
        "seen_signatures_before_near_miss": [],
    })
    path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ReplayCorpusError, match="manifest.json"):
        replay_corpus(corpus)


@pytest.mark.parametrize(("entry", "before"), [(1, 3), (0, 4)])
def test_replay_rejects_impossible_per_wave_query_budgets(tmp_path, entry, before):
    """Régression visée : la vague 1 commence tard ou consomme plus de 3 requêtes."""
    source = tmp_path / "source.pdf"
    source.write_bytes(b"source")
    corpus = tmp_path / "corpus"
    write_replay_corpus(
        corpus,
        outcome=_outcome(),
        config=B2Config(api_key="secret"),
        target_brand="Norel",
        strict_evidence=False,
        source_path=source,
        source_text="texte",
    )
    path = corpus / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["waves"][0]["logical_queries_at_entry"] = entry
    manifest["waves"][0]["logical_queries_before_near_miss"] = before
    path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ReplayCorpusError, match="manifest.json"):
        replay_corpus(corpus)


def test_replay_rejects_a_signature_invented_inside_a_wave(tmp_path):
    """Régression visée : une signature apparaît avant l'appel near-miss réel."""
    source = tmp_path / "source.pdf"
    source.write_bytes(b"source")
    corpus = tmp_path / "corpus"
    write_replay_corpus(
        corpus,
        outcome=_outcome(),
        config=B2Config(api_key="secret"),
        target_brand="Norel",
        strict_evidence=False,
        source_path=source,
        source_text="texte",
    )
    path = corpus / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["waves"][0]["seen_signatures_before_near_miss"] = ["phantom"]
    path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ReplayCorpusError, match="manifest.json"):
        replay_corpus(corpus)


def test_capture_rejects_an_audit_before_its_own_page(tmp_path):
    """Régression visée : un audit est rejoué avant récupération de sa page."""
    source = tmp_path / "source.pdf"
    source.write_bytes(b"source")
    outcome = _outcome()
    outcome.replay_waves.append({
        "wave": 2,
        "logical_queries_at_entry": 3,
        "logical_queries_before_near_miss": 6,
        "seen_signatures_at_entry": [],
        "seen_signatures_before_near_miss": [],
    })
    outcome.page_waves = {CANONICAL_URL: 2}

    with pytest.raises(ReplayCorpusError, match="page"):
        write_replay_corpus(
            tmp_path / "corpus",
            outcome=outcome,
            config=B2Config(api_key="secret"),
            target_brand="Norel",
            strict_evidence=False,
            source_path=source,
            source_text="texte",
        )


def test_capture_rejects_audits_out_of_wave_order(tmp_path):
    """Régression visée : un audit tardif est rejoué avant un audit antérieur."""
    source = tmp_path / "source.pdf"
    source.write_bytes(b"source")
    outcome = _outcome()
    second_url = "https://new.norel.example/products/ref-2"
    second_audit = _audit().model_copy(deep=True)
    second_audit.page_url = second_url
    outcome.audits.append(second_audit)
    outcome.audit_waves = [2, 1]
    outcome.visited_pages[second_url] = "Norel REF-2. Page auditée en vague 1."
    outcome.page_waves = {CANONICAL_URL: 2, second_url: 1}
    outcome.replay_waves.append({
        "wave": 2,
        "logical_queries_at_entry": 3,
        "logical_queries_before_near_miss": 6,
        "seen_signatures_at_entry": [],
        "seen_signatures_before_near_miss": [],
    })

    with pytest.raises(ReplayCorpusError, match="audit"):
        write_replay_corpus(
            tmp_path / "corpus",
            outcome=outcome,
            config=B2Config(api_key="secret"),
            target_brand="Norel",
            strict_evidence=False,
            source_path=source,
            source_text="texte",
        )


def test_capture_and_replay_allow_a_cached_page_audited_in_a_later_wave(tmp_path):
    """Une page de vague 1 peut fournir un nouvel audit ciblé en vague 2."""
    source = tmp_path / "source.pdf"
    source.write_bytes(b"source")
    outcome = _outcome()
    outcome.audit_waves = [2]
    outcome.replay_waves.append({
        "wave": 2,
        "logical_queries_at_entry": 3,
        "logical_queries_before_near_miss": 6,
        "seen_signatures_at_entry": [],
        "seen_signatures_before_near_miss": [],
    })

    corpus = tmp_path / "corpus"
    write_replay_corpus(
        corpus,
        outcome=outcome,
        config=B2Config(api_key="secret"),
        target_brand="Norel",
        strict_evidence=False,
        source_path=source,
        source_text="texte",
    )

    replayed = replay_corpus(corpus)
    assert replayed["status"] == "complete"
    assert replayed["diagnostics"]["candidate_evaluations"] == [
        {**_expected_evaluation(), "wave": 2}
    ]


def test_capture_refuses_to_overwrite_an_existing_directory(tmp_path):
    """Régression visée : deux runs mélangent silencieusement leurs fichiers."""
    source = tmp_path / "source.pdf"
    source.write_bytes(b"source")
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    marker = corpus / ".gitignore"
    marker.write_text("keep-me\n", encoding="utf-8")

    with pytest.raises(ReplayCorpusError, match="existe déjà"):
        write_replay_corpus(
            corpus,
            outcome=_outcome(),
            config=B2Config(api_key="secret"),
            target_brand="Norel",
            strict_evidence=False,
            source_path=source,
            source_text="texte",
        )
    assert marker.read_text(encoding="utf-8") == "keep-me\n"
    assert not (corpus / "pages.json").exists()


def test_manifest_reads_commit_from_the_project_root(monkeypatch):
    import rejeu

    captured = {}

    class Result:
        returncode = 0
        stdout = "abc123\n"

    def fake_run(*args, **kwargs):
        captured.update(kwargs)
        return Result()

    monkeypatch.setattr(rejeu.subprocess, "run", fake_run)

    assert rejeu._git_commit() == "abc123"
    assert captured["cwd"] == rejeu.DOSSIER


def test_capture_failure_never_publishes_a_partial_destination(
    monkeypatch,
    tmp_path,
):
    """Régression visée : une écriture interrompue ressemble à un corpus complet."""
    source = tmp_path / "source.pdf"
    source.write_bytes(b"source")
    corpus = tmp_path / "corpus"

    def fail_write(path, value):
        raise OSError("synthetic disk failure")

    monkeypatch.setattr(rejeu, "_write_json", fail_write)
    with pytest.raises(ReplayCorpusError, match="écriture"):
        write_replay_corpus(
            corpus,
            outcome=_outcome(),
            config=B2Config(api_key="secret"),
            target_brand="Norel",
            strict_evidence=False,
            source_path=source,
            source_text="texte",
        )
    assert not corpus.exists()
    assert not list(tmp_path.glob(".corpus.tmp-*"))


def test_capture_does_not_replace_a_missing_source_file_with_extracted_text(tmp_path):
    """Régression visée : le hash change silencieusement de portée."""
    missing_source = tmp_path / "missing.pdf"

    with pytest.raises(ReplayCorpusError, match="fiche source"):
        write_replay_corpus(
            tmp_path / "corpus",
            outcome=_outcome(),
            config=B2Config(api_key="secret"),
            target_brand="Norel",
            strict_evidence=False,
            source_path=missing_source,
            source_text="texte extrait",
        )


def test_replay_reaudits_cached_secondary_pages_after_official_identity_confirmation(
    tmp_path,
):
    """Regression visee : les specs deja payees restent hors du score."""
    official_url = (
        "https://new.norel.example/products/fr/4KBL103001R8110/"
        "bsl07-20-10-81"
    )
    secondary_url = (
        "https://www.distributeur-b.example/catalog/fr-fr/products/"
        "norel-contacteur-bsl07"
    )
    redundant_url = "https://market.example/category/contactors"
    requirements = RequirementSet(
        product="Contacteur de puissance tripolaire NV1T05BD",
        origin_brand="Kerion Electric Tersa D",
        criteria=[
            Requirement(id="fabricant", label="Fabricant", requested_value="Kerion Electric"),
            Requirement(id="reference_exacte", label="Reference exacte", requested_value="NV1T05BD"),
            Requirement(id="famille", label="Famille", requested_value="Tersa D"),
            Requirement(id="fonction", label="Fonction", requested_value="Contacteur de puissance tripolaire"),
            Requirement(id="nombre_de_poles", label="Nombre de poles", requested_value="3P"),
            Requirement(id="courant_nominal", label="Courant nominal", requested_value="9 A (AC-3)"),
            Requirement(id="tension_de_bobine", label="Tension de bobine", requested_value="24 V DC"),
            Requirement(id="contacts_principaux", label="Contacts principaux", requested_value="3 NO"),
            Requirement(id="usage_vise", label="Usage vise", requested_value="Commande de moteur / charge industrielle"),
        ],
    )
    official_content = (
        "Norel 4KBL103001R8110 BSL07-20-10-81. "
        "Contacteur de puissance tripolaire. 3P. "
        "Rated Operational Current AC-3 9 A."
    )
    official_proof = SourceProof(
        url=official_url,
        excerpt="4KBL103001R8110",
        type="web_officiel",
    )
    candidate = CandidateAudit(
        brand="Norel",
        reference="4KBL103001R8110",
        criteria=[
            CriterionAudit(
                requirement_id="fabricant",
                requested_value="Kerion Electric",
                observed_value="Norel",
                status="incompatible",
                proofs=[official_proof],
            ),
            CriterionAudit(
                requirement_id="reference_exacte",
                requested_value="NV1T05BD",
                observed_value="4KBL103001R8110",
                status="incompatible",
                proofs=[official_proof],
            ),
            CriterionAudit(
                requirement_id="famille",
                requested_value="Tersa D",
                observed_value="BSL07",
                status="incompatible",
                proofs=[SourceProof(
                    url=official_url,
                    excerpt="BSL07-20-10-81",
                    type="web_officiel",
                )],
            ),
            CriterionAudit(
                requirement_id="fonction",
                requested_value="Contacteur de puissance tripolaire",
                observed_value="Contacteur de puissance tripolaire",
                status="proven",
                proofs=[SourceProof(
                    url=official_url,
                    excerpt="Contacteur de puissance tripolaire",
                    type="web_officiel",
                )],
            ),
            CriterionAudit(
                requirement_id="nombre_de_poles",
                requested_value="3P",
                observed_value="3P",
                status="proven",
                proofs=[SourceProof(
                    url=official_url,
                    excerpt="3P",
                    type="web_officiel",
                )],
            ),
            CriterionAudit(
                requirement_id="courant_nominal",
                requested_value="9 A (AC-3)",
                observed_value="12 A (AC-3)",
                status="incompatible",
                proofs=[SourceProof(
                    url=secondary_url,
                    excerpt="Norel 4KBL103001R8110 12 A (AC-3)",
                    type="web_secondaire",
                )],
            ),
            CriterionAudit(
                requirement_id="tension_de_bobine",
                requested_value="24 V DC",
                status="not_proven",
            ),
            CriterionAudit(
                requirement_id="contacts_principaux",
                requested_value="3 NO",
                status="not_proven",
            ),
            CriterionAudit(
                requirement_id="usage_vise",
                requested_value="Commande de moteur / charge industrielle",
                status="not_proven",
            ),
        ],
    )
    secondary_content = (
        "Norel 4KBL103001R8110 Contacteur AS 9A AC3-3P+1NF-24VDC. "
        "Nombre de contacts a fermeture en tant que contacts principaux: 3. "
        "Les contacteurs BSL07 sont principalement utilises pour la commande "
        "de moteurs triphases et la commande de circuits de puissance."
    )
    outcome = ResearchOutcome(
        status="not_resolved",
        requirements=requirements,
        evaluation=None,
        audits=[PageAudit(page_url=official_url, candidates=[candidate])],
        visited_pages={
            official_url: official_content,
            secondary_url: secondary_content,
            redundant_url: "BSL07 24VDC",
        },
        diagnostics=ResearchDiagnostics(waves=1, logical_queries=3),
        audit_waves=[1],
        page_waves={official_url: 1, secondary_url: 1, redundant_url: 1},
        replay_waves=[{
            "wave": 1,
            "logical_queries_at_entry": 0,
            "logical_queries_before_near_miss": 3,
            "seen_signatures_at_entry": [],
            "seen_signatures_before_near_miss": [],
        }],
    )
    corpus = tmp_path / "cached-secondary"
    write_replay_corpus(
        corpus,
        outcome=outcome,
        config=B2Config(api_key="secret"),
        target_brand="Norel",
        strict_evidence=False,
        source_path=None,
        source_text="fiche",
    )

    replayed = replay_corpus(corpus)
    evaluations = replayed["diagnostics"]["candidate_evaluations"]

    assert len(evaluations) == 1
    assert evaluations[0]["reference"] == "4KBL103001R8110"
    assert evaluations[0]["score"] == 100
    assert evaluations[0]["eligible"] is True
    assert evaluations[0]["complete"] is True
    assert evaluations[0]["not_proven_criteria"] == []
    assert evaluations[0]["proof_url_count"] == 2
    # La contradiction secondaire `12 A` n'existe pas dans la page visitée :
    # elle est retirée avant la ré-audit et ne peut donc pas être présentée
    # comme une incompatibilité ensuite « sauvée » par la source officielle.
    assert "downgraded_by_official_source" not in evaluations[0]
    assert replayed["status"] == "complete"
    assert replayed["compatibility"]["reference"] == "4KBL103001R8110"


def test_replay_partially_proposes_candidate_when_one_clean_page_beats_a_bad_page(tmp_path):
    clean_url = "https://www.distributeur-c.example/xz07-20-10-11"
    bad_url = "https://www.revendeur-e.example/xz07-20-10-11"
    identity_url = "https://new.norel.example/products/xz07-20-10-11"
    clean = _audit()
    clean.page_url = clean_url
    clean.candidates[0].reference = "XZ07-20-10-11"
    clean.candidates[0].criteria[1].proofs[0].url = clean_url
    bad = _audit()
    bad.page_url = bad_url
    bad.candidates[0].reference = "XZ07 20 10 11"
    bad.candidates[0].criteria[1].status = "incompatible"
    bad.candidates[0].criteria[1].observed_value = "Produit contradictoire"
    bad.candidates[0].criteria[1].proofs[0].url = bad_url
    bad.candidates[0].criteria[1].proofs[0].excerpt = "Produit contradictoire"
    outcome = _outcome()
    outcome.audits = [bad, clean]
    outcome.audit_waves = [1, 1]
    outcome.visited_pages = {
        bad_url: "Norel XZ07-20-10-11. Produit contradictoire.",
        clean_url: f"Norel XZ07-20-10-11. {EXCERPT}.",
        identity_url: "Norel XZ07-20-10-11.",
    }
    outcome.page_waves = {bad_url: 1, clean_url: 1, identity_url: 1}
    corpus = tmp_path / "clean-wins"
    write_replay_corpus(
        corpus,
        outcome=outcome,
        config=B2Config(api_key="secret"),
        target_brand="Norel",
        strict_evidence=False,
        source_path=None,
        source_text="fiche",
    )

    replayed = replay_corpus(corpus)

    assert replayed["status"] == "partial"
    assert replayed["compatibility"]["reference"] == "XZ07-20-10-11"
    assert replayed["discarded_candidates"] == []
    function = next(
        item for item in replayed["criteria"] if item["label"] == "Fonction"
    )
    assert function["status"] == "proven"
    assert [proof["url"] for proof in function["proofs"]] == [clean_url]


def test_replay_discards_candidate_only_when_its_best_page_is_incompatible(tmp_path):
    page_a = "https://new.norel.example/products/xz07-20-10-13"
    page_b = "https://seller.example/xz07-20-10-13"
    requirements = RequirementSet(
        product="Kerion NV1T05BD",
        origin_brand="Kerion Electric",
        criteria=[
            Requirement(
                id="manufacturer",
                label="Fabricant",
                requested_value="Kerion Electric",
            ),
            Requirement(id="coil", label="Bobine", requested_value="24 V DC"),
        ],
    )

    def incompatible_audit(url: str) -> PageAudit:
        return PageAudit(page_url=url, candidates=[CandidateAudit(
            brand="Norel",
            reference="XZ07-20-10-13",
            criteria=[
                CriterionAudit(
                    requirement_id="manufacturer",
                    requested_value="Kerion Electric",
                    status="not_proven",
                ),
                CriterionAudit(
                    requirement_id="coil",
                    requested_value="24 V DC",
                    observed_value="100-250 V AC/DC",
                    status="incompatible",
                    proofs=[SourceProof(
                        url=url,
                        excerpt="Coil 100-250 V AC/DC",
                        type=(
                            "web_officiel"
                            if "norel.example" in url
                            else "web_secondaire"
                        ),
                    )],
                ),
            ],
        )])

    outcome = ResearchOutcome(
        status="not_resolved",
        requirements=requirements,
        evaluation=None,
        audits=[incompatible_audit(page_a), incompatible_audit(page_b)],
        visited_pages={
            page_a: "Norel XZ07-20-10-13. Coil 100-250 V AC/DC.",
            page_b: "Norel XZ07-20-10-13. Coil 100-250 V AC/DC.",
        },
        diagnostics=ResearchDiagnostics(waves=1, logical_queries=3),
        audit_waves=[1, 1],
        page_waves={page_a: 1, page_b: 1},
        replay_waves=[{
            "wave": 1,
            "logical_queries_at_entry": 0,
            "logical_queries_before_near_miss": 3,
            "seen_signatures_at_entry": [],
            "seen_signatures_before_near_miss": [],
        }],
    )
    corpus = tmp_path / "all-pages-incompatible"
    write_replay_corpus(
        corpus,
        outcome=outcome,
        config=B2Config(api_key="secret"),
        target_brand="Norel",
        strict_evidence=False,
        source_path=None,
        source_text="fiche",
    )

    replayed = replay_corpus(corpus)

    assert replayed["status"] == "rejected"
    assert replayed["compatibility"] is None
    assert replayed["alternative_proposee"] == ""
    assert replayed["discarded_candidates"] == [{
        "brand": "Norel",
        "reference": "XZ07-20-10-13",
        "reason": "Bobine incompatible",
        "criteria": [{
            "label": "Bobine",
            "requested_value": "24 V DC",
            "status": "incompatible",
            "proofs": [{
                "url": page_a,
                "excerpt": "Coil 100-250 V AC/DC",
                "type": "web_officiel",
            }, {
                "url": page_b,
                "excerpt": "Coil 100-250 V AC/DC",
                "type": "web_secondaire",
            }],
        }],
    }]


def test_corpus_5_replays_bsl07_at_83_percent_with_readable_proofs(tmp_path):
    url = "https://new.norel.example/products/4KBL103001R8110/bsl07-20-10-81"
    technical = [
        ("function", "Fonction", "Contacteur tripolaire"),
        ("poles", "Nombre de poles", "3P"),
        ("current", "Courant nominal", "9 A AC-3"),
        ("coil", "Tension de bobine", "24 V DC"),
        ("contacts", "Contacts principaux", "3 NO"),
        ("usage", "Usage vise", "Commande de moteur"),
    ]
    requirements = RequirementSet(
        product="Kerion NV1T05BD",
        origin_brand="Kerion Electric",
        criteria=[Requirement(
            id="manufacturer",
            label="Fabricant",
            requested_value="Kerion Electric",
        )] + [
            Requirement(id=criterion_id, label=label, requested_value=value)
            for criterion_id, label, value in technical
        ],
    )
    candidate = CandidateAudit(
        brand="Norel",
        reference="BSL07-20-10-81",
        criteria=[CriterionAudit(
            requirement_id="manufacturer",
            requested_value="Kerion Electric",
            status="not_proven",
        )] + [
            CriterionAudit(
                requirement_id=criterion_id,
                requested_value=value,
                observed_value=value if index < 5 else "",
                status="proven" if index < 5 else "not_proven",
                proofs=(
                    [SourceProof(url=url, excerpt=value, type="web_officiel")]
                    if index < 5
                    else []
                ),
            )
            for index, (criterion_id, _label, value) in enumerate(technical)
        ],
    )
    content = "Norel BSL07-20-10-81. " + ". ".join(
        value for _criterion_id, _label, value in technical[:5]
    )
    outcome = ResearchOutcome(
        status="not_resolved",
        requirements=requirements,
        evaluation=None,
        audits=[PageAudit(page_url=url, candidates=[candidate])],
        visited_pages={url: content},
        diagnostics=ResearchDiagnostics(waves=1, logical_queries=3),
        audit_waves=[1],
        page_waves={url: 1},
        replay_waves=[{
            "wave": 1,
            "logical_queries_at_entry": 0,
            "logical_queries_before_near_miss": 3,
            "seen_signatures_at_entry": [],
            "seen_signatures_before_near_miss": [],
        }],
    )
    corpus = tmp_path / "corpus-5"
    write_replay_corpus(
        corpus,
        outcome=outcome,
        config=B2Config(api_key="secret"),
        target_brand="Norel",
        strict_evidence=False,
        source_path=None,
        source_text="fiche",
    )

    replayed = replay_corpus(corpus)

    assert replayed["status"] == "partial"
    assert replayed["compatibility"]["reference"] == "BSL07-20-10-81"
    assert replayed["compatibility"]["score"] == 83
    # `Fabricant` vise la marque d'origine : elle ne peut pas s'appliquer a une
    # alternative Norel. Le score l'ecarte deja du denominateur (5 preuves sur 6
    # criteres techniques = 83 %) ; le rapport le nomme desormais pareil.
    assert [item["status"] for item in replayed["criteria"]] == [
        "non_applicable", "proven", "proven", "proven", "proven", "proven",
        "not_proven",
    ]
    proven_details = [
        item for item in replayed["criteria"] if item["status"] == "proven"
    ]
    assert all(item["proofs"][0]["url"] == url for item in proven_details)
    assert all(item["proofs"][0]["excerpt"] for item in proven_details)
