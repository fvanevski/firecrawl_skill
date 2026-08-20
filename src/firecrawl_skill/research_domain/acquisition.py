"""Acquisition and candidate-assessment domain contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from uuid import UUID

from ._common import _confidence, _positive, _text, _unique
from .research import FreshnessStatus


class Relevance(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNRELATED = "unrelated"
    UNCERTAIN = "uncertain"


class SourceRole(str, Enum):
    PRIMARY = "primary"
    CONTROLLING = "controlling"
    AUTHORITATIVE_SECONDARY = "authoritative_secondary"
    INDEPENDENT_SECONDARY = "independent_secondary"
    CONTEXT_ONLY = "context_only"
    UNSUITABLE = "unsuitable"
    UNCERTAIN = "uncertain"


class ExtractionRecommendation(str, Enum):
    SCRAPE = "scrape"
    METADATA_ONLY = "metadata_only"
    DEFER = "defer"
    REJECT = "reject"


class IndependenceStatus(str, Enum):
    INDEPENDENT = "independent"
    DEPENDENT = "dependent"
    UNCERTAIN = "uncertain"
    UNASSESSED = "unassessed"


@dataclass(frozen=True)
class FreshnessAssessment:
    status: FreshnessStatus
    rationale: str

    def __post_init__(self):
        _text(self.rationale, "freshness_assessment.rationale")


@dataclass(frozen=True)
class IndependenceAssessment:
    candidate_id: UUID
    status: IndependenceStatus
    rationale: str

    def __post_init__(self):
        _text(self.rationale, "independence_assessment.rationale")


@dataclass(frozen=True)
class CandidateAssessment:
    schema_version: str
    candidate_id: UUID
    relevance: Relevance
    source_role: SourceRole
    target_question_ids: tuple[UUID, ...]
    target_claim_ids: tuple[UUID, ...]
    freshness_assessment: FreshnessAssessment
    independence_assessment: IndependenceAssessment
    extraction_recommendation: ExtractionRecommendation
    priority: int
    rationale: str
    confidence: float
    uncertainty: str

    SCHEMA_VERSION = "candidate-assessment-v1"

    def __post_init__(self):
        if self.schema_version != self.SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version: {self.schema_version}")
        _positive(self.priority, "priority", allow_zero=True)
        if self.priority > 100:
            raise ValueError("priority must be <= 100")
        _text(self.rationale, "rationale")
        _confidence(self.confidence)
        _unique(self.target_question_ids, "target_question_ids")
        _unique(self.target_claim_ids, "target_claim_ids")
        if not self.target_question_ids and not self.target_claim_ids:
            raise ValueError("candidate assessment must target a question or claim")
