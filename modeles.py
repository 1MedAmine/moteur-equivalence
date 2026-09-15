# -*- coding: utf-8 -*-
"""Contrats structurés échangés entre ScrapeGraphAI et le code B2."""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator


class Requirement(BaseModel):
    id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    requested_value: str = Field(min_length=1)
    critical: bool = True


class RequirementSet(BaseModel):
    product: str = Field(min_length=1)
    origin_brand: str = ""
    criteria: list[Requirement] = Field(min_length=1)

    @field_validator("criteria")
    @classmethod
    def unique_ids(cls, criteria: list[Requirement]) -> list[Requirement]:
        identifiers = [item.id for item in criteria]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("Les identifiants de critères doivent être uniques.")
        return criteria


class RequirementAddition(Requirement):
    evidence_excerpt: str = Field(min_length=1)
    page: int | None = Field(default=None, ge=1)
    table: int | None = Field(default=None, ge=1)
    column: int | None = Field(default=None, ge=1)


class RequirementSupplement(BaseModel):
    criteria: list[RequirementAddition] = Field(default_factory=list)


class SourceProof(BaseModel):
    url: str = Field(min_length=1)
    excerpt: str = Field(min_length=1)
    type: Literal["web_officiel", "web_secondaire"]

    @field_validator("url")
    @classmethod
    def http_url(cls, value: str) -> str:
        parsed = urlparse(value.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Une preuve exige une URL HTTP(S) valide.")
        return value.strip()

    @field_validator("excerpt")
    @classmethod
    def non_blank_excerpt(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("L'extrait de preuve ne peut pas être vide.")
        return value.strip()


class CriterionAudit(BaseModel):
    requirement_id: str = Field(min_length=1)
    requested_value: str = Field(min_length=1)
    observed_value: str = ""
    status: Literal["proven", "not_proven", "incompatible"]
    proofs: list[SourceProof] = Field(default_factory=list)
    evidence_rejected: bool = Field(default=False, exclude=True)


class CandidateAudit(BaseModel):
    brand: str = Field(min_length=1)
    reference: str = Field(min_length=1)
    criteria: list[CriterionAudit] = Field(min_length=1)
    deviations: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class PageAudit(BaseModel):
    page_url: str = Field(min_length=1)
    candidates: list[CandidateAudit] = Field(default_factory=list)


class CompatibilitySummary(BaseModel):
    score: int = Field(ge=0, le=100)
    brand: str
    reference: str
    proven_criteria: list[str] = Field(default_factory=list)
    not_proven_criteria: list[str] = Field(default_factory=list)
    incompatible_criteria: list[str] = Field(default_factory=list)
    non_applicable_criteria: list[str] = Field(default_factory=list)
    critical_blockers: list[str] = Field(default_factory=list)
    #: Critères dont la preuve n'a pas pu être vérifiée contre la page
    #: récupérée. Vide en mode strict, où un tel critère fait lever
    #: `EvidenceContractError` au lieu d'être rétrogradé (spec CAP-4).
    unverified_criteria: list[str] = Field(default_factory=list)
    #: Critères entièrement absents de la réponse d'audit du modèle.
    missing_audit_criteria: list[str] = Field(default_factory=list)
    #: Critères présents dont au moins une preuve échoue au contrôle littéral.
    rejected_proof_criteria: list[str] = Field(default_factory=list)
    #: Grade conservateur de la preuve acceptée pour chaque critère.
    proof_grades: dict[str, Literal["contiguous", "windowed"]] = Field(
        default_factory=dict
    )


class CandidateEvaluation(BaseModel):
    candidate: CandidateAudit
    summary: CompatibilitySummary
    eligible: bool
    complete: bool
    official_proof_count: int = Field(ge=0)
    proof_url_count: int = Field(ge=0)


class SearchAttempt(BaseModel):
    engine: str
    status: str
    result_count: int = Field(default=0, ge=0, le=12)
    reason: str = ""


class SearchHit(BaseModel):
    url: str
    title: str = ""
    snippet: str = ""
    engine: str
    rank: int = Field(ge=1, le=12)


class SearchBatch(BaseModel):
    query: str
    hits: list[SearchHit] = Field(default_factory=list, max_length=12)
    attempts: list[SearchAttempt] = Field(default_factory=list, max_length=3)
