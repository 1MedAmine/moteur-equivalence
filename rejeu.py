# -*- coding: utf-8 -*-
"""Capture et rejeu déterministes d'un corpus B2, sans réseau ni modèle."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Sequence

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from compatibilite import (
    CompatibilityContractError,
    EvidenceContractError,
    cached_reaudit_downgraded_criteria,
    canonical_url,
    evaluate_candidates,
    reaudit_cached_product_evidence,
    select_best_candidate,
)
from configuration import (
    B2Config,
    DOSSIER,
    MAX_LOGICAL_QUERIES,
    MAX_QUERIES_PER_WAVE,
)
from modeles import CandidateEvaluation, PageAudit, RequirementSet
from near_miss import (
    assess_candidates,
    build_targeted_tasks,
    select_targeted_queries,
)


SCHEMA_VERSION = 1
REQUIRED_FILES = (
    "requirements.json",
    "audits.json",
    "pages.json",
    "manifest.json",
)


class ReplayCorpusError(ValueError):
    """Corpus absent, incomplet ou incohérent : aucun score n'est produit."""


_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_GIT_COMMIT_PATTERN = re.compile(r"^(?:unavailable|[0-9a-f]{40,64})$")


class _StrictReplayModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ReplayCodeIdentity(_StrictReplayModel):
    git_commit: str
    sha256: str

    @field_validator("git_commit")
    @classmethod
    def valid_commit(cls, value: str) -> str:
        if not _GIT_COMMIT_PATTERN.fullmatch(value):
            raise ValueError("git_commit invalide")
        return value

    @field_validator("sha256")
    @classmethod
    def valid_sha256(cls, value: str) -> str:
        if not _SHA256_PATTERN.fullmatch(value):
            raise ValueError("sha256 invalide")
        return value


class ReplaySourceIdentity(_StrictReplayModel):
    path: str
    sha256: str
    hash_scope: Literal["file_bytes", "extracted_text"]

    @field_validator("sha256")
    @classmethod
    def valid_sha256(cls, value: str) -> str:
        if not _SHA256_PATTERN.fullmatch(value):
            raise ValueError("sha256 invalide")
        return value

    @model_validator(mode="after")
    def file_scope_has_path(self):
        if self.hash_scope == "file_bytes" and not self.path.strip():
            raise ValueError("file_bytes exige un chemin")
        if self.hash_scope == "extracted_text" and self.path:
            raise ValueError("extracted_text ne doit pas revendiquer un chemin")
        return self


class ReplayWaveState(_StrictReplayModel):
    wave: int = Field(ge=1, le=4)
    logical_queries_at_entry: int = Field(ge=0, le=MAX_LOGICAL_QUERIES)
    logical_queries_before_near_miss: int = Field(
        ge=0, le=MAX_LOGICAL_QUERIES
    )
    seen_signatures_at_entry: list[str]
    seen_signatures_before_near_miss: list[str]

    @model_validator(mode="after")
    def coherent_progress(self):
        if self.logical_queries_before_near_miss < self.logical_queries_at_entry:
            raise ValueError("le budget logique régresse dans une vague")
        if (
            self.logical_queries_before_near_miss
            - self.logical_queries_at_entry
            > MAX_QUERIES_PER_WAVE
        ):
            raise ValueError("une vague consomme trop de requêtes logiques")
        if self.seen_signatures_at_entry != self.seen_signatures_before_near_miss:
            raise ValueError("les signatures ne changent qu'après le snapshot")
        if len(self.seen_signatures_at_entry) != len(set(self.seen_signatures_at_entry)):
            raise ValueError("signatures dupliquées à l'entrée")
        if len(self.seen_signatures_before_near_miss) != len(
            set(self.seen_signatures_before_near_miss)
        ):
            raise ValueError("signatures dupliquées avant near-miss")
        return self


class ReplayManifest(_StrictReplayModel):
    schema_version: Literal[1]
    generated_at: str
    target_brand: str
    threshold: int = Field(ge=1, le=100)
    strict_evidence: bool
    b2_model: str = Field(alias="B2_MODEL", min_length=1)
    code: ReplayCodeIdentity
    fiche: ReplaySourceIdentity
    audit_waves: list[int]
    page_waves: dict[str, int]
    waves: list[ReplayWaveState] = Field(min_length=1, max_length=4)

    @field_validator("generated_at")
    @classmethod
    def timezone_required(cls, value: str) -> str:
        if not value.endswith("Z"):
            raise ValueError("generated_at doit être exprimé en UTC avec Z")
        try:
            parsed = datetime.fromisoformat(value[:-1] + "+00:00")
        except ValueError as error:
            raise ValueError("generated_at invalide") from error
        if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
            raise ValueError("generated_at doit être exprimé en UTC")
        return value

    @field_validator("audit_waves")
    @classmethod
    def valid_audit_waves(cls, values: list[int]) -> list[int]:
        if any(type(value) is not int or value < 1 for value in values):
            raise ValueError("audit_waves invalide")
        return values

    @field_validator("page_waves")
    @classmethod
    def valid_page_waves(cls, values: dict[str, int]) -> dict[str, int]:
        if any(
            not url
            or canonical_url(url) != url
            or type(wave) is not int
            or wave < 1
            for url, wave in values.items()
        ):
            raise ValueError("page_waves invalide")
        return values

    @model_validator(mode="after")
    def coherent_waves(self):
        wave_ids = [item.wave for item in self.waves]
        if wave_ids != list(range(1, len(wave_ids) + 1)):
            raise ValueError("les vagues doivent être consécutives depuis 1")
        if self.waves[0].logical_queries_at_entry != 0:
            raise ValueError("la première vague doit commencer sans requête consommée")
        known = set(wave_ids)
        if self.audit_waves != sorted(self.audit_waves):
            raise ValueError("audit_waves doit suivre l'ordre chronologique")
        if not set(self.audit_waves).issubset(known):
            raise ValueError("audit_waves référence une vague absente")
        if not set(self.page_waves.values()).issubset(known):
            raise ValueError("page_waves référence une vague absente")
        for previous, current in zip(self.waves, self.waves[1:]):
            if (
                current.logical_queries_at_entry
                != previous.logical_queries_before_near_miss
            ):
                raise ValueError("le budget logique diverge entre deux vagues")
            if not set(previous.seen_signatures_before_near_miss).issubset(
                current.seen_signatures_at_entry
            ):
                raise ValueError("les signatures vues régressent entre deux vagues")
        return self


def candidate_evaluation_diagnostic(
    evaluation: CandidateEvaluation,
    *,
    wave: int,
    url: str,
    downgraded_by_official_source: Sequence[str] = (),
) -> dict:
    """Projection sûre partagée par le run réel et le rejeu."""
    summary = evaluation.summary
    result = {
        "wave": wave,
        "url": url,
        "reference": summary.reference,
        "brand": summary.brand,
        "score": summary.score,
        "eligible": evaluation.eligible,
        "complete": evaluation.complete,
        "proof_url_count": evaluation.proof_url_count,
        "official_proof_count": evaluation.official_proof_count,
        "proven_criteria": list(summary.proven_criteria),
        "not_proven_criteria": list(summary.not_proven_criteria),
        "incompatible_criteria": list(summary.incompatible_criteria),
        "unverified_criteria": list(summary.unverified_criteria),
        "missing_audit_criteria": list(summary.missing_audit_criteria),
        "rejected_proof_criteria": list(summary.rejected_proof_criteria),
        "proof_grades": dict(summary.proof_grades),
        "non_applicable_criteria": list(summary.non_applicable_criteria),
    }
    if downgraded_by_official_source:
        result["downgraded_by_official_source"] = list(
            dict.fromkeys(downgraded_by_official_source)
        )
    return result


def journaliser_evaluation(journal: list[dict], diagnostic: dict) -> None:
    """N'inscrit un verdict que la première fois qu'il est établi.

    Les audits sont refusionnés depuis tout l'historique à chaque vague : un
    candidat dont rien n'a changé se réévalue à l'identique et se représenterait
    au seul `wave` près. N'en garder que la première occurrence laisse lisible
    ce qui a réellement bougé — et, parce que le run réel et le rejeu passent
    tous deux par ici, garde leurs deux journaux comparables.
    """
    sans_vague = {key: value for key, value in diagnostic.items() if key != "wave"}
    for existant in journal:
        if {
            key: value for key, value in existant.items() if key != "wave"
        } == sans_vague:
            return
    journal.append(diagnostic)


def candidate_contract_error_diagnostic(
    audit: PageAudit,
    *,
    wave: int,
    error: Exception,
) -> dict:
    candidate = audit.candidates[0]
    return {
        "wave": wave,
        "url": audit.page_url,
        "reference": candidate.reference,
        "contract_error": type(error).__name__,
    }


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _code_sha256() -> str:
    digest = hashlib.sha256()
    for path in sorted(DOSSIER.glob("*.py"), key=lambda item: item.name.casefold()):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _git_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=DOSSIER.parent,
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unavailable"
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else "unavailable"


def _fiche_identity(source_path: str | Path | None, source_text: str) -> dict:
    if source_path:
        path = Path(source_path).resolve()
        try:
            if not path.is_file():
                raise OSError("source absente")
            content = path.read_bytes()
        except OSError as error:
            raise ReplayCorpusError(
                "La fiche source est absente ou illisible au moment de la capture."
            ) from error
        else:
            return {
                "path": str(path),
                "sha256": hashlib.sha256(content).hexdigest(),
                "hash_scope": "file_bytes",
            }
    return {
        "path": "",
        "sha256": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
        "hash_scope": "extracted_text",
    }


def _validated_trace(outcome) -> tuple[list[int], dict[str, int], list[dict]]:
    audit_waves = list(getattr(outcome, "audit_waves", ()))
    if len(audit_waves) != len(outcome.audits) or any(
        type(wave) is not int or wave < 1 for wave in audit_waves
    ):
        raise ReplayCorpusError(
            "audit_waves doit associer une vague valide à chaque audit."
        )
    page_waves = {
        canonical_url(str(url)): wave
        for url, wave in dict(getattr(outcome, "page_waves", {})).items()
        if canonical_url(str(url))
    }
    canonical_pages = {
        canonical_url(url): content
        for url, content in outcome.visited_pages.items()
        if canonical_url(url)
    }
    if set(page_waves) != set(canonical_pages) or any(
        type(wave) is not int or wave < 1 for wave in page_waves.values()
    ):
        raise ReplayCorpusError(
            "page_waves doit associer une première vague à chaque page."
        )
    waves = list(getattr(outcome, "replay_waves", ()))
    if not waves:
        raise ReplayCorpusError("Le journal des vagues de rejeu est vide.")
    try:
        validated_waves = [ReplayWaveState.model_validate(item) for item in waves]
    except (TypeError, ValidationError) as error:
        raise ReplayCorpusError("Le journal des vagues de rejeu est invalide.") from error
    wave_ids = [item.wave for item in validated_waves]
    if wave_ids != sorted(set(wave_ids)):
        raise ReplayCorpusError("Les vagues de rejeu ne sont pas strictement ordonnées.")
    if not set(audit_waves).issubset(wave_ids) or not set(page_waves.values()).issubset(
        wave_ids
    ):
        raise ReplayCorpusError("Une page ou un audit référence une vague absente.")
    if audit_waves != sorted(audit_waves):
        raise ReplayCorpusError("Les audits ne suivent pas l'ordre des vagues.")
    for audit, audit_wave in zip(outcome.audits, audit_waves):
        page_url = canonical_url(audit.page_url)
        page_wave = page_waves.get(page_url)
        if page_wave is None or page_wave > audit_wave:
            raise ReplayCorpusError(
                "La page d'un audit doit être récupérée au plus tard dans "
                "la vague de l'audit."
            )
    return (
        audit_waves,
        page_waves,
        [item.model_dump(mode="json") for item in validated_waves],
    )


def _manifest(
    *,
    audit_waves: list[int],
    page_waves: dict[str, int],
    waves: list[dict],
    config: B2Config,
    target_brand: str | None,
    strict_evidence: bool,
    source_path: str | Path | None,
    source_text: str,
) -> ReplayManifest:
    try:
        return ReplayManifest.model_validate({
            "schema_version": SCHEMA_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat().replace(
                "+00:00", "Z"
            ),
            "target_brand": target_brand or "",
            "threshold": config.min_compatibility_percent,
            "strict_evidence": strict_evidence,
            "B2_MODEL": config.model,
            "code": {
                "git_commit": _git_commit(),
                "sha256": _code_sha256(),
            },
            "fiche": _fiche_identity(source_path, source_text),
            "audit_waves": audit_waves,
            "page_waves": page_waves,
            "waves": waves,
        })
    except ValidationError as error:
        raise ReplayCorpusError("Le manifest de capture est incohérent.") from error


def write_replay_corpus(
    directory: str | Path,
    *,
    outcome,
    config: B2Config,
    target_brand: str | None,
    strict_evidence: bool,
    source_path: str | Path | None,
    source_text: str,
) -> Path:
    """Écrit le corpus brut local ; aucune donnée n'entre dans le rapport."""
    audit_waves, page_waves, waves = _validated_trace(outcome)
    destination = Path(directory).resolve()
    if destination.exists():
        raise ReplayCorpusError(
            f"Le dossier de corpus existe déjà : {destination}"
        )
    pages = {
        canonical_url(url): content
        for url, content in outcome.visited_pages.items()
        if canonical_url(url)
    }
    manifest = _manifest(
        audit_waves=audit_waves,
        page_waves=page_waves,
        waves=waves,
        config=config,
        target_brand=target_brand,
        strict_evidence=strict_evidence,
        source_path=source_path,
        source_text=source_text,
    )

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(
        prefix=f".{destination.name}.tmp-",
        dir=destination.parent,
    ))
    try:
        # Même hors du dossier recommandé `.replay`, les artefacts bruts sont
        # ignorés avant la première écriture de contenu.
        (temporary / ".gitignore").write_text("*\n", encoding="utf-8")
        _write_json(temporary / "pages.json", pages)
        _write_json(
            temporary / "audits.json",
            [audit.model_dump(mode="json") for audit in outcome.audits],
        )
        _write_json(
            temporary / "requirements.json",
            outcome.requirements.model_dump(mode="json"),
        )
        _write_json(
            temporary / "manifest.json",
            manifest.model_dump(mode="json", by_alias=True),
        )
        temporary.rename(destination)
    except (OSError, UnicodeError, TypeError, ValueError) as error:
        shutil.rmtree(temporary, ignore_errors=True)
        raise ReplayCorpusError(
            "Échec d'écriture atomique du corpus de rejeu."
        ) from error
    return destination


def capture_replay_if_configured(
    *,
    outcome,
    config: B2Config,
    target_brand: str | None,
    strict_evidence: bool,
    source_path: str | Path | None,
    source_text: str,
) -> Path | None:
    """Capture uniquement quand `B2_REPLAY_DIR` contient un chemin non vide."""
    directory = os.getenv("B2_REPLAY_DIR", "").strip()
    if not directory:
        return None
    return write_replay_corpus(
        directory,
        outcome=outcome,
        config=config,
        target_brand=target_brand,
        strict_evidence=strict_evidence,
        source_path=source_path,
        source_text=source_text,
    )


def _read_json(directory: Path, name: str) -> object:
    path = directory / name
    if not path.is_file():
        raise ReplayCorpusError(f"Corpus incomplet : {name} est absent.")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ReplayCorpusError(f"Corpus invalide : {name} est illisible.") from error


def _load_corpus(directory: str | Path):
    root = Path(directory).resolve()
    for name in REQUIRED_FILES:
        if not (root / name).is_file():
            raise ReplayCorpusError(f"Corpus incomplet : {name} est absent.")
    raw_requirements = _read_json(root, "requirements.json")
    raw_audits = _read_json(root, "audits.json")
    raw_pages = _read_json(root, "pages.json")
    manifest = _read_json(root, "manifest.json")
    try:
        requirements = RequirementSet.model_validate(raw_requirements)
        if not isinstance(raw_audits, list):
            raise TypeError("audits")
        audits = [PageAudit.model_validate(item) for item in raw_audits]
    except (TypeError, ValueError) as error:
        raise ReplayCorpusError("Le schéma des exigences ou audits est invalide.") from error
    if not isinstance(raw_pages, dict) or not all(
        isinstance(url, str) and isinstance(content, str)
        for url, content in raw_pages.items()
    ):
        raise ReplayCorpusError("Le schéma de pages.json est invalide.")
    try:
        validated_manifest = ReplayManifest.model_validate(manifest)
    except (TypeError, ValidationError) as error:
        raise ReplayCorpusError("Le schéma de manifest.json est invalide.") from error
    if len(validated_manifest.audit_waves) != len(audits):
        raise ReplayCorpusError("manifest.json : audit_waves est incohérent.")
    if set(validated_manifest.page_waves) != set(raw_pages):
        raise ReplayCorpusError("manifest.json : page_waves est incohérent.")
    for audit, audit_wave in zip(audits, validated_manifest.audit_waves):
        page_url = canonical_url(audit.page_url)
        page_wave = validated_manifest.page_waves.get(page_url)
        if page_wave is None or page_wave > audit_wave:
            raise ReplayCorpusError(
                "manifest.json : une page auditée doit être disponible au plus "
                "tard dans sa vague d'audit."
            )
    return requirements, audits, raw_pages, validated_manifest


def _assessed(evaluations: Sequence[CandidateEvaluation]):
    # Imports locaux : `recherche_adaptative` peut lui-même utiliser les
    # projections de ce module sans cycle d'import au chargement.
    from recherche_adaptative import famille_de_recherche, _urls_de_preuve

    return tuple(
        (
            evaluation,
            {
                "identity": evaluation.candidate.reference,
                "family": famille_de_recherche(
                    evaluation.candidate.reference,
                    _urls_de_preuve(evaluation),
                ),
            },
        )
        for evaluation in evaluations
        if not evaluation.eligible
    )


def replay_corpus(directory: str | Path) -> dict:
    """Rejoue la decision et le rapport reel depuis les fichiers locaux."""
    # Imports locaux : `recherche_adaptative` importe les projections de ce
    # module pendant son chargement. Le rejeu, lui, n'est appele qu'une fois
    # ces modules initialises.
    import rapport
    from recherche_adaptative import (
        ResearchDiagnostics,
        ResearchOutcome,
        merge_page_audits,
    )

    requirements, audits, pages, manifest = _load_corpus(directory)
    target_brand = manifest.target_brand
    threshold = manifest.threshold
    strict_evidence = manifest.strict_evidence
    audit_waves = manifest.audit_waves
    page_waves = manifest.page_waves

    candidate_evaluations: list[dict] = []
    final_evaluations: list[CandidateEvaluation] = []
    evidence_rejection_seen = False
    for state in manifest.waves:
        wave = state.wave
        wave_audits = [
            audit for audit, audit_wave in zip(audits, audit_waves)
            if audit_wave <= wave
        ]
        wave_pages = {
            url: content for url, content in pages.items()
            if page_waves[url] <= wave
        }
        evaluations: list[CandidateEvaluation] = []
        for merged_audit in merge_page_audits(
            wave_audits,
            requirements,
            wave_pages,
        ):
            try:
                current = evaluate_candidates(
                    requirements,
                    [merged_audit],
                    target_brand or None,
                    threshold,
                    wave_pages,
                    strict_evidence=strict_evidence,
                )
                downgrade_pass = reaudit_cached_product_evidence(
                    requirements,
                    merged_audit,
                    wave_pages,
                )
                downgraded = cached_reaudit_downgraded_criteria(
                    requirements, merged_audit, downgrade_pass,
                )
                # Même séquence qu'en réel : le passage 1 ne fait que
                # rétrograder une incompatibilité secondaire, le passage 2
                # peut ensuite établir une preuve depuis `not_proven`.
                enriched_audit = reaudit_cached_product_evidence(
                    requirements,
                    downgrade_pass,
                    wave_pages,
                )
                if enriched_audit != merged_audit:
                    current = evaluate_candidates(
                        requirements,
                        [enriched_audit],
                        target_brand or None,
                        threshold,
                        wave_pages,
                        strict_evidence=strict_evidence,
                    )
                evaluations.extend(current)
                for evaluation in current:
                    journaliser_evaluation(
                        candidate_evaluations,
                        candidate_evaluation_diagnostic(
                            evaluation,
                            wave=wave,
                            url=merged_audit.page_url,
                            downgraded_by_official_source=downgraded,
                        ),
                    )
            except (CompatibilityContractError, EvidenceContractError) as error:
                evidence_rejection_seen = True
                journaliser_evaluation(
                    candidate_evaluations,
                    candidate_contract_error_diagnostic(
                        merged_audit, wave=wave, error=error
                    ),
                )

        assessed = _assessed(evaluations)
        # Les deux appels font partie du banc d'essai : ils garantissent que
        # les prochains correctifs peuvent aussi mesurer le near-miss sans Web.
        assess_candidates(assessed, requirements)
        tasks = build_targeted_tasks(assessed, requirements)
        logical = state.logical_queries_before_near_miss
        seen = set(state.seen_signatures_before_near_miss)
        select_targeted_queries(
            tasks,
            remaining_logical_queries=max(0, MAX_LOGICAL_QUERIES - logical),
            seen_signatures=seen,
        )
        final_evaluations = evaluations

    best = select_best_candidate(final_evaluations)
    incompatibility_seen = any(
        evaluation.summary.incompatible_criteria
        for evaluation in final_evaluations
    )
    status = (
        "complete" if best is not None and best.complete else
        "partial" if best is not None else
        "rejected" if incompatibility_seen else
        "not_resolved"
    )
    if best is not None:
        final_state = "candidate_selected"
    elif incompatibility_seen:
        final_state = "candidate_rejected"
    elif evidence_rejection_seen:
        final_state = "rejected_by_evidence"
    elif final_evaluations:
        final_state = "audited_but_non_verifiable"
    else:
        final_state = "no_candidate_discovered"

    last_wave = manifest.waves[-1]
    diagnostics = ResearchDiagnostics(
        waves=last_wave.wave,
        logical_queries=last_wave.logical_queries_before_near_miss,
        pages_opened=len(pages),
        pages_fetched=len(pages),
        pages_analyzed=len({canonical_url(audit.page_url) for audit in audits}),
        candidate_evaluations=candidate_evaluations,
        stop_reason=(
            "PROVEN_EQUIVALENT"
            if status == "complete"
            else "REPLAY_CORPUS_EXHAUSTED"
        ),
        final_state=final_state,
    )
    outcome = ResearchOutcome(
        status=status,
        requirements=requirements,
        evaluation=best,
        audits=audits,
        visited_pages=pages,
        diagnostics=diagnostics,
        audit_waves=audit_waves,
        page_waves=page_waves,
        replay_waves=[state.model_dump() for state in manifest.waves],
        evaluations=final_evaluations,
    )
    return rapport.construire(outcome)
