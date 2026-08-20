"""Canonical EvidencePacket completeness and referential validator."""

from __future__ import annotations

import datetime
import json
from dataclasses import dataclass
from enum import Enum
from typing import Any
from uuid import UUID

from firecrawl_skill.research_domain.models import EvidencePacket, SemanticStatus

from ..budget_policy import ResourceCaps
from ..tokenizer_registry import get_tokenizer


class ValidationSeverity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass(frozen=True)
class ValidationFinding:
    code: str
    severity: ValidationSeverity
    message: str
    path: str = ""
    detail: dict[str, Any] | None = None


@dataclass(frozen=True)
class ValidationResult:
    is_valid: bool
    is_complete: bool
    errors: tuple[ValidationFinding, ...] = ()
    warnings: tuple[ValidationFinding, ...] = ()
    info: tuple[ValidationFinding, ...] = ()

    @property
    def summary(self) -> str:
        if self.is_valid and self.is_complete:
            return "packet is valid and complete"
        if self.is_valid:
            return f"packet is valid but incomplete ({len(self.warnings)} warnings)"
        return f"packet is invalid ({len(self.errors)} errors)"

    def to_dict(self) -> dict[str, Any]:
        def finding(value: ValidationFinding) -> dict[str, Any]:
            return {
                "code": value.code,
                "severity": value.severity.value,
                "message": value.message,
                "path": value.path,
                "detail": value.detail,
            }

        return {
            "is_valid": self.is_valid,
            "is_complete": self.is_complete,
            "summary": self.summary,
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "info_count": len(self.info),
            "errors": [finding(item) for item in self.errors],
            "warnings": [finding(item) for item in self.warnings],
            "info": [finding(item) for item in self.info],
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)


class EvidencePacketValidator:
    """Validate an EvidencePacket without mutating it."""

    EVALUATED_STATUSES = frozenset(
        {
            SemanticStatus.SUPPORTED,
            SemanticStatus.CONTRADICTED,
            SemanticStatus.QUALIFIED,
        }
    )
    UNEVALUABLE_STATUSES = frozenset(
        {SemanticStatus.UNSUPPORTED, SemanticStatus.UNCERTAIN}
    )

    def validate(
        self,
        packet: EvidencePacket,
        *,
        effective_caps: ResourceCaps | None = None,
        coverage_items: frozenset[UUID] | None = None,
        candidate_ids: frozenset[UUID] | None = None,
        snapshot_ids: frozenset[UUID] | None = None,
    ) -> ValidationResult:
        errors: list[ValidationFinding] = []
        warnings: list[ValidationFinding] = []
        info: list[ValidationFinding] = []

        self._check_referential_integrity(
            packet,
            candidate_ids=candidate_ids,
            snapshot_ids=snapshot_ids,
            errors=errors,
            warnings=warnings,
        )
        self._check_claim_coverage(packet, errors=errors, warnings=warnings)
        self._check_group_completeness(
            packet, errors=errors, warnings=warnings, info=info
        )
        self._check_freshness(packet, warnings=warnings, info=info)
        self._check_unresolved_requirements(
            packet,
            coverage_items=coverage_items,
            errors=errors,
            warnings=warnings,
        )
        if effective_caps is not None:
            self._check_token_budget(
                packet, effective_caps, errors=errors, warnings=warnings
            )
        self._check_retrieval_execution(
            packet, errors=errors, warnings=warnings, info=info
        )
        self._check_provenance(packet, errors=errors, warnings=warnings)
        self._check_semantic_stages(packet, errors=errors, warnings=warnings, info=info)

        return ValidationResult(
            is_valid=not errors,
            is_complete=not errors and not warnings,
            errors=tuple(errors),
            warnings=tuple(warnings),
            info=tuple(info),
        )

    def _check_referential_integrity(
        self,
        packet: EvidencePacket,
        *,
        candidate_ids: frozenset[UUID] | None,
        snapshot_ids: frozenset[UUID] | None,
        errors: list[ValidationFinding],
        warnings: list[ValidationFinding],
    ) -> None:
        del warnings
        all_passages = packet.passages + packet.omitted_passages
        passage_ids = {p.passage_id for p in all_passages}
        claim_ids = {c.claim_id for c in packet.claims}

        for binding in packet.claim_evidence_bindings:
            if binding.claim_id not in claim_ids:
                errors.append(
                    ValidationFinding(
                        "UNKNOWN_CLAIM_REF",
                        ValidationSeverity.ERROR,
                        f"binding {binding.binding_id} references unknown claim {binding.claim_id}",
                        f"claim_evidence_bindings/{binding.binding_id}",
                    )
                )
            for passage_id in binding.passage_ids:
                if passage_id not in passage_ids:
                    errors.append(
                        ValidationFinding(
                            "UNKNOWN_PASSAGE_REF",
                            ValidationSeverity.ERROR,
                            f"binding {binding.binding_id} references unknown passage {passage_id}",
                            f"claim_evidence_bindings/{binding.binding_id}/passages",
                        )
                    )

        for group in (
            packet.corroborating_groups
            + packet.contradicting_groups
            + packet.qualifying_groups
            + packet.near_duplicate_groups
        ):
            for passage_id in group.passage_ids:
                if passage_id not in passage_ids:
                    errors.append(
                        ValidationFinding(
                            "UNKNOWN_PASSAGE_REF",
                            ValidationSeverity.ERROR,
                            f"group {group.group_id} references unknown passage {passage_id}",
                            f"groups/{group.group_id}/passages",
                        )
                    )

        for provenance in packet.retrieval_provenance:
            for passage_id in provenance.selected_passage_ids:
                if passage_id not in passage_ids:
                    errors.append(
                        ValidationFinding(
                            "UNKNOWN_PASSAGE_REF",
                            ValidationSeverity.ERROR,
                            (
                                f"retrieval_provenance {provenance.retrieval_event_id} "
                                f"references unknown passage {passage_id}"
                            ),
                            f"retrieval_provenance/{provenance.retrieval_event_id}/passages",
                        )
                    )

        if candidate_ids is not None:
            for passage in all_passages:
                if passage.candidate_id not in candidate_ids:
                    errors.append(
                        ValidationFinding(
                            "UNKNOWN_CANDIDATE_REF",
                            ValidationSeverity.ERROR,
                            (
                                f"passage {passage.passage_id} references unknown "
                                f"candidate {passage.candidate_id}"
                            ),
                            f"passages/{passage.passage_id}",
                        )
                    )
            for assessment in packet.independence_assessments:
                if assessment.candidate_id not in candidate_ids:
                    errors.append(
                        ValidationFinding(
                            "UNKNOWN_CANDIDATE_REF",
                            ValidationSeverity.ERROR,
                            (
                                "independence_assessment references unknown candidate "
                                f"{assessment.candidate_id}"
                            ),
                            "independence_assessments",
                            {"candidate_id": str(assessment.candidate_id)},
                        )
                    )

        if snapshot_ids is not None:
            for passage in all_passages:
                if passage.snapshot_id not in snapshot_ids:
                    errors.append(
                        ValidationFinding(
                            "UNKNOWN_SNAPSHOT_REF",
                            ValidationSeverity.ERROR,
                            (
                                f"passage {passage.passage_id} references unknown "
                                f"snapshot {passage.snapshot_id}"
                            ),
                            f"passages/{passage.passage_id}",
                        )
                    )

    def _check_claim_coverage(
        self,
        packet: EvidencePacket,
        *,
        errors: list[ValidationFinding],
        warnings: list[ValidationFinding],
    ) -> None:
        claims_with_bindings = {
            binding.claim_id for binding in packet.claim_evidence_bindings
        }
        for claim in packet.claims:
            if (
                claim.semantic_status in self.EVALUATED_STATUSES
                and claim.claim_id not in claims_with_bindings
            ):
                errors.append(
                    ValidationFinding(
                        "CLAIM_NO_BINDING",
                        ValidationSeverity.ERROR,
                        (
                            f"claim {claim.claim_id} has status "
                            f"{claim.semantic_status.value} but has no claim-evidence binding"
                        ),
                        f"claims/{claim.claim_id}",
                    )
                )
            elif (
                claim.semantic_status not in self.UNEVALUABLE_STATUSES
                and claim.semantic_status not in self.EVALUATED_STATUSES
                and claim.claim_id not in claims_with_bindings
            ):
                warnings.append(
                    ValidationFinding(
                        "CLAIM_NO_BINDING",
                        ValidationSeverity.WARNING,
                        (
                            f"claim {claim.claim_id} has status "
                            f"{claim.semantic_status.value} and no binding"
                        ),
                        f"claims/{claim.claim_id}",
                    )
                )

    def _check_group_completeness(
        self,
        packet: EvidencePacket,
        *,
        errors: list[ValidationFinding],
        warnings: list[ValidationFinding],
        info: list[ValidationFinding],
    ) -> None:
        groups = (
            packet.corroborating_groups
            + packet.contradicting_groups
            + packet.qualifying_groups
            + packet.near_duplicate_groups
        )
        for group in groups:
            if not group.passage_ids:
                target = errors if group.evaluated else warnings
                target.append(
                    ValidationFinding(
                        "EMPTY_EVALUATED_GROUP"
                        if group.evaluated
                        else "UNEVALUATED_EMPTY_GROUP",
                        ValidationSeverity.ERROR
                        if group.evaluated
                        else ValidationSeverity.WARNING,
                        (
                            f"group {group.group_id} is "
                            f"{'evaluated' if group.evaluated else 'unevaluated'} "
                            "but has no passages"
                        ),
                        f"groups/{group.group_id}",
                    )
                )
            if len(group.passage_ids) != len(set(group.passage_ids)):
                errors.append(
                    ValidationFinding(
                        "DUPLICATE_PASSAGE_IN_GROUP",
                        ValidationSeverity.ERROR,
                        f"group {group.group_id} contains duplicate passage IDs",
                        f"groups/{group.group_id}",
                    )
                )
            if group.evaluated and not group.rationale.strip():
                errors.append(
                    ValidationFinding(
                        "MISSING_GROUP_RATIONALE",
                        ValidationSeverity.ERROR,
                        f"group {group.group_id} is evaluated but has no rationale",
                        f"groups/{group.group_id}",
                    )
                )
        info.append(
            ValidationFinding(
                "GROUP_SUMMARY",
                ValidationSeverity.INFO,
                (
                    f"groups: corroborating={len(packet.corroborating_groups)}, "
                    f"contradicting={len(packet.contradicting_groups)}, "
                    f"qualifying={len(packet.qualifying_groups)}, "
                    f"near_duplicate={len(packet.near_duplicate_groups)}"
                ),
            )
        )

    def _check_freshness(
        self,
        packet: EvidencePacket,
        *,
        warnings: list[ValidationFinding],
        info: list[ValidationFinding],
    ) -> None:
        freshness = packet.freshness_summary
        if not freshness:
            warnings.append(
                ValidationFinding(
                    "MISSING_FRESHNESS_SUMMARY",
                    ValidationSeverity.WARNING,
                    "freshness_summary is empty",
                    "freshness_summary",
                )
            )
            return
        most_recent = freshness.get("most_recent")
        oldest = freshness.get("oldest")
        if most_recent is None and oldest is None:
            warnings.append(
                ValidationFinding(
                    "NO_FRESHNESS_DATES",
                    ValidationSeverity.WARNING,
                    "freshness_summary has no most_recent or oldest dates",
                    "freshness_summary",
                )
            )
            return
        try:
            if most_recent and oldest:
                newest_date = datetime.datetime.fromisoformat(most_recent)
                oldest_date = datetime.datetime.fromisoformat(oldest)
                if newest_date < oldest_date:
                    warnings.append(
                        ValidationFinding(
                            "FRESHNESS_ORDERING",
                            ValidationSeverity.WARNING,
                            "freshness_summary most_recent is before oldest",
                            "freshness_summary",
                            {"most_recent": most_recent, "oldest": oldest},
                        )
                    )
        except (ValueError, TypeError):
            warnings.append(
                ValidationFinding(
                    "FRESHNESS_DATE_PARSE",
                    ValidationSeverity.WARNING,
                    "freshness_summary dates could not be parsed",
                    "freshness_summary",
                )
            )
        info.append(
            ValidationFinding(
                "FRESHNESS_SUMMARY",
                ValidationSeverity.INFO,
                f"freshness: oldest={oldest}, most_recent={most_recent}",
            )
        )

    def _check_unresolved_requirements(
        self,
        packet: EvidencePacket,
        *,
        coverage_items: frozenset[UUID] | None,
        errors: list[ValidationFinding],
        warnings: list[ValidationFinding],
    ) -> None:
        if not packet.unresolved_items:
            return
        if coverage_items is None:
            warnings.append(
                ValidationFinding(
                    "UNRESOLVED_NO_COVERAGE_CONTEXT",
                    ValidationSeverity.WARNING,
                    (
                        f"packet has {len(packet.unresolved_items)} unresolved items "
                        "but no coverage context was provided for validation"
                    ),
                    "unresolved_items",
                )
            )
            return
        unknown = set(packet.unresolved_items) - coverage_items
        if unknown:
            values = sorted(map(str, unknown))
            errors.append(
                ValidationFinding(
                    "UNRESOLVED_UNKNOWN_COVERAGE",
                    ValidationSeverity.ERROR,
                    f"unresolved items reference unknown coverage items: {values}",
                    "unresolved_items",
                    {"unknown_items": values},
                )
            )

    def _check_token_budget(
        self,
        packet: EvidencePacket,
        effective_caps: ResourceCaps,
        *,
        errors: list[ValidationFinding],
        warnings: list[ValidationFinding],
    ) -> None:
        tokenizer = get_tokenizer("cl100k_base")
        max_tokens = effective_caps.max_evidence_packet_tokens
        total_tokens = sum(len(tokenizer.encode(p.text)) for p in packet.passages)
        if total_tokens > max_tokens:
            errors.append(
                ValidationFinding(
                    "TOKEN_BUDGET_EXCEEDED",
                    ValidationSeverity.ERROR,
                    (
                        f"included passages use {total_tokens} tokens, exceeding "
                        f"budget of {max_tokens}"
                    ),
                    "passages",
                    {"used_tokens": total_tokens, "max_tokens": max_tokens},
                )
            )
        elif total_tokens == max_tokens:
            warnings.append(
                ValidationFinding(
                    "TOKEN_BUDGET_FULL",
                    ValidationSeverity.WARNING,
                    (
                        f"included passages use all {total_tokens} tokens "
                        f"(budget: {max_tokens})"
                    ),
                    "passages",
                )
            )
        if packet.omitted_passages:
            warnings.append(
                ValidationFinding(
                    "OMITTED_PASSAGES",
                    ValidationSeverity.WARNING,
                    f"{len(packet.omitted_passages)} passage(s) were omitted due to token budget",
                    "omitted_passages",
                    {"omitted_count": len(packet.omitted_passages)},
                )
            )

    def _check_retrieval_execution(
        self,
        packet: EvidencePacket,
        *,
        errors: list[ValidationFinding],
        warnings: list[ValidationFinding],
        info: list[ValidationFinding],
    ) -> None:
        del warnings
        all_passages = packet.passages + packet.omitted_passages
        if all_passages and not packet.retrieval_provenance:
            errors.append(
                ValidationFinding(
                    "MISSING_RETRIEVAL_PROVENANCE",
                    ValidationSeverity.ERROR,
                    (
                        f"packet has {len(all_passages)} passages but no "
                        "retrieval_provenance entries"
                    ),
                    "retrieval_provenance",
                )
            )
        elif packet.retrieval_provenance:
            info.append(
                ValidationFinding(
                    "RETRIEVAL_PROVENANCE_COUNT",
                    ValidationSeverity.INFO,
                    f"{len(packet.retrieval_provenance)} retrieval_provenance entry/entries",
                )
            )

    def _check_provenance(
        self,
        packet: EvidencePacket,
        *,
        errors: list[ValidationFinding],
        warnings: list[ValidationFinding],
    ) -> None:
        for passage in packet.passages + packet.omitted_passages:
            if not passage.source_url:
                warnings.append(
                    ValidationFinding(
                        "MISSING_SOURCE_URL",
                        ValidationSeverity.WARNING,
                        f"passage {passage.passage_id} has no source_url",
                        f"passages/{passage.passage_id}",
                    )
                )
            if not passage.candidate_id:
                errors.append(
                    ValidationFinding(
                        "MISSING_CANDIDATE_ID",
                        ValidationSeverity.ERROR,
                        f"passage {passage.passage_id} has no candidate_id",
                        f"passages/{passage.passage_id}",
                    )
                )
            if not passage.snapshot_id:
                errors.append(
                    ValidationFinding(
                        "MISSING_SNAPSHOT_ID",
                        ValidationSeverity.ERROR,
                        f"passage {passage.passage_id} has no snapshot_id",
                        f"passages/{passage.passage_id}",
                    )
                )
            if not passage.chunk_id:
                errors.append(
                    ValidationFinding(
                        "MISSING_CHUNK_ID",
                        ValidationSeverity.ERROR,
                        f"passage {passage.passage_id} has no chunk_id",
                        f"passages/{passage.passage_id}",
                    )
                )

    def _check_semantic_stages(
        self,
        packet: EvidencePacket,
        *,
        errors: list[ValidationFinding],
        warnings: list[ValidationFinding],
        info: list[ValidationFinding],
    ) -> None:
        bound_claim_ids = {
            binding.claim_id for binding in packet.claim_evidence_bindings
        }
        for claim in packet.claims:
            if claim.semantic_status == SemanticStatus.UNASSESSED:
                target = errors if claim.claim_id in bound_claim_ids else warnings
                target.append(
                    ValidationFinding(
                        "BOUND_UNASSESSED_CLAIM"
                        if claim.claim_id in bound_claim_ids
                        else "UNASSESSED_CLAIM",
                        ValidationSeverity.ERROR
                        if claim.claim_id in bound_claim_ids
                        else ValidationSeverity.WARNING,
                        (
                            f"claim {claim.claim_id} is unassessed and "
                            f"{'has binding(s)' if claim.claim_id in bound_claim_ids else 'has no bindings'}"
                        ),
                        f"claims/{claim.claim_id}",
                    )
                )

        for binding in packet.claim_evidence_bindings:
            if not binding.model:
                errors.append(
                    ValidationFinding(
                        "MISSING_BINDING_MODEL",
                        ValidationSeverity.ERROR,
                        f"binding {binding.binding_id} has no model",
                        f"claim_evidence_bindings/{binding.binding_id}",
                    )
                )
            if not binding.prompt_version:
                errors.append(
                    ValidationFinding(
                        "MISSING_BINDING_PROMPT_VERSION",
                        ValidationSeverity.ERROR,
                        f"binding {binding.binding_id} has no prompt_version",
                        f"claim_evidence_bindings/{binding.binding_id}",
                    )
                )
            if binding.input_packet_revision < 1:
                errors.append(
                    ValidationFinding(
                        "INVALID_INPUT_PACKET_REVISION",
                        ValidationSeverity.ERROR,
                        (
                            f"binding {binding.binding_id} has input_packet_revision="
                            f"{binding.input_packet_revision}"
                        ),
                        f"claim_evidence_bindings/{binding.binding_id}",
                    )
                )

        status_counts: dict[str, int] = {}
        for claim in packet.claims:
            status_counts[claim.semantic_status.value] = (
                status_counts.get(claim.semantic_status.value, 0) + 1
            )
        if status_counts:
            info.append(
                ValidationFinding(
                    "SEMANTIC_STATUS_DISTRIBUTION",
                    ValidationSeverity.INFO,
                    f"semantic status distribution: {status_counts}",
                )
            )


def bounded_citation_ready_output(
    packet: EvidencePacket,
    *,
    max_passages: int = 20,
    max_claims: int = 10,
) -> dict[str, Any]:
    claims_out = [
        {
            "claim_id": str(claim.claim_id),
            "statement": claim.statement,
            "semantic_status": claim.semantic_status.value,
            "uncertainty": claim.uncertainty,
        }
        for claim in packet.claims[:max_claims]
    ]
    passages_out = [
        {
            "passage_id": str(passage.passage_id),
            "text": passage.text,
            "source_url": passage.source_url,
            "candidate_id": str(passage.candidate_id),
            "snapshot_id": str(passage.snapshot_id),
            "chunk_id": str(passage.chunk_id),
        }
        for passage in packet.passages[:max_passages]
    ]

    bindings_out: dict[str, list[dict[str, Any]]] = {}
    for binding in packet.claim_evidence_bindings:
        bindings_out.setdefault(str(binding.claim_id), []).append(
            {
                "binding_id": str(binding.binding_id),
                "passage_ids": [str(p) for p in binding.passage_ids],
                "relationship": binding.relationship.value
                if hasattr(binding.relationship, "value")
                else str(binding.relationship),
                "confidence": binding.confidence,
            }
        )

    all_groups = (
        packet.corroborating_groups
        + packet.contradicting_groups
        + packet.qualifying_groups
    )
    groups_out = [
        {
            "group_id": str(group.group_id),
            "passage_ids": [str(p) for p in group.passage_ids],
            "rationale": group.rationale,
            "evaluated": group.evaluated,
        }
        for group in all_groups[:max_passages]
    ]
    return {
        "claims": claims_out,
        "passages": passages_out,
        "bindings": bindings_out,
        "groups": groups_out,
        "metadata": {
            "run_id": str(packet.run_id),
            "schema_version": packet.schema_version,
            "coverage_revision": packet.coverage_revision,
            "claim_count": len(packet.claims),
            "passage_count": len(packet.passages),
            "omitted_passage_count": len(packet.omitted_passages),
            "binding_count": len(packet.claim_evidence_bindings),
            "group_count": len(all_groups),
            "source_diversity": packet.source_diversity_summary,
            "freshness": packet.freshness_summary,
        },
    }


__all__ = [
    "EvidencePacketValidator",
    "ValidationFinding",
    "ValidationResult",
    "ValidationSeverity",
    "bounded_citation_ready_output",
]
