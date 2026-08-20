"""EvidencePacketValidator — completeness, referential, and state validation.

This module provides a dedicated validator for ``EvidencePacket`` instances
that enforces the requirements of issue #58:

- Referential integrity (unknown references are rejected).
- Claim coverage (every claim has at least one binding or is marked unsupported).
- Group completeness (group states are valid and internally consistent).
- Freshness (freshness summary is present and within bounds).
- Unresolved requirements (unresolved items are traceable to coverage).
- Packet completeness state (semantic stages, retrieval execution, provenance).
- Token-budget violations (passages fit within budget, omitted tracked).
- Missing retrieval execution (provenance is present when candidates exist).
- Incomplete provenance (every passage traces back to a source).

The validator does **not** mutate the packet.  It returns a
``ValidationResult`` that can be consumed by CLI commands, agent consumers,
or downstream synthesis without risk of a packet being falsely marked
complete.

**Defense in depth.**  Several checks duplicate constraints enforced by the
domain model's ``__post_init__`` (e.g. non-empty ``source_url``,
non-empty ``model``, ``input_packet_revision >= 1``).  Those checks are
retained because packets may be deserialized from external JSON (e.g. from
PostgreSQL via ``load_model``) that bypasses ``__post_init__``.  The
validator is the authoritative gate before synthesis or export.
"""

from __future__ import annotations

import datetime
import json
import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from budget_policy import ResourceCaps

from firecrawl_skill.research_domain.models import (
    EvidencePacket,
    SemanticStatus,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Validation result types
# ---------------------------------------------------------------------------


class ValidationSeverity(str):
    """Severity levels for validation findings."""

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass(frozen=True)
class ValidationFinding:
    """A single validation finding (error or warning)."""

    code: str
    severity: ValidationSeverity
    message: str
    path: str = ""
    detail: dict[str, Any] | None = None


@dataclass(frozen=True)
class ValidationResult:
    """Aggregated validation result for an EvidencePacket.

    Attributes:
        is_valid: ``True`` when there are zero ``ERROR`` findings.
        is_complete: ``True`` when all required semantic stages ran and
            coverage is satisfied.  A packet can be ``is_valid`` but not
            ``is_complete`` (e.g. warnings about stale freshness).
        errors: List of error-level findings.
        warnings: List of warning-level findings.
        info: List of info-level findings.
        summary: Human-readable one-liner.
    """

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
        return {
            "is_valid": self.is_valid,
            "is_complete": self.is_complete,
            "summary": self.summary,
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "info_count": len(self.info),
            "errors": [
                {
                    "code": e.code,
                    "severity": e.severity,
                    "message": e.message,
                    "path": e.path,
                    "detail": e.detail,
                }
                for e in self.errors
            ],
            "warnings": [
                {
                    "code": w.code,
                    "severity": w.severity,
                    "message": w.message,
                    "path": w.path,
                    "detail": w.detail,
                }
                for w in self.warnings
            ],
            "info": [
                {
                    "code": i.code,
                    "severity": i.severity,
                    "message": i.message,
                    "path": i.path,
                    "detail": i.detail,
                }
                for i in self.info
            ],
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


class EvidencePacketValidator:
    """Validates an ``EvidencePacket`` for completeness and referential integrity.

    The validator runs a fixed set of checks and collects findings.  It never
    raises — instead it returns a ``ValidationResult`` so callers can decide
    how to handle errors vs warnings.
    """

    # Required semantic statuses that indicate a claim was actually evaluated.
    EVALUATED_STATUSES = frozenset(
        {
            SemanticStatus.SUPPORTED,
            SemanticStatus.CONTRADICTED,
            SemanticStatus.QUALIFIED,
        }
    )

    # Statuses that indicate the model could not determine support.
    UNAVALUABLE_STATUSES = frozenset(
        {
            SemanticStatus.UNSUPPORTED,
            SemanticStatus.UNCERTAIN,
        }
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
        """Run all validation checks on *packet*.

        Args:
            packet: The EvidencePacket to validate.
            effective_caps: Authorized resource caps (used for token-budget
                checks).  When ``None`` token-budget validation is skipped.
            coverage_items: Set of known coverage item IDs.  When provided,
                unresolved items are cross-checked against this set.
            candidate_ids: Set of known candidate IDs.  When provided,
                passage candidate references are validated.
            snapshot_ids: Set of known snapshot IDs.  When provided,
                passage snapshot references are validated.

        Returns:
            A ``ValidationResult`` with all findings.
        """
        errors: list[ValidationFinding] = []
        warnings: list[ValidationFinding] = []
        info: list[ValidationFinding] = []

        # 1. Referential integrity (unknown IDs).
        self._check_referential_integrity(
            packet,
            candidate_ids=candidate_ids,
            snapshot_ids=snapshot_ids,
            errors=errors,
            warnings=warnings,
        )

        # 2. Claim coverage.
        self._check_claim_coverage(packet, errors=errors, warnings=warnings)

        # 3. Group completeness.
        self._check_group_completeness(
            packet, errors=errors, warnings=warnings, info=info
        )

        # 4. Freshness.
        self._check_freshness(packet, warnings=warnings, info=info)

        # 5. Unresolved requirements.
        self._check_unresolved_requirements(
            packet, coverage_items=coverage_items, errors=errors, warnings=warnings
        )

        # 6. Token budget.
        if effective_caps is not None:
            self._check_token_budget(
                packet, effective_caps, errors=errors, warnings=warnings
            )

        # 7. Retrieval execution.
        self._check_retrieval_execution(
            packet, errors=errors, warnings=warnings, info=info
        )

        # 8. Provenance completeness.
        self._check_provenance(packet, errors=errors, warnings=warnings)

        # 9. Semantic stage completeness.
        self._check_semantic_stages(packet, errors=errors, warnings=warnings, info=info)

        return ValidationResult(
            is_valid=len(errors) == 0,
            is_complete=(len(errors) == 0 and len(warnings) == 0),
            errors=tuple(errors),
            warnings=tuple(warnings),
            info=tuple(info),
        )

    # ------------------------------------------------------------------
    # Individual checks
    # ------------------------------------------------------------------

    def _check_referential_integrity(
        self,
        packet: EvidencePacket,
        *,
        candidate_ids: frozenset[UUID] | None,
        snapshot_ids: frozenset[UUID] | None,
        errors: list[ValidationFinding],
        warnings: list[ValidationFinding],
    ) -> None:
        """Check that all IDs referenced in the packet are known."""
        all_passages = packet.passages + packet.omitted_passages
        passage_ids = {p.passage_id for p in all_passages}
        claim_ids = {c.claim_id for c in packet.claims}

        # Check binding claim references.
        for binding in packet.claim_evidence_bindings:
            if binding.claim_id not in claim_ids:
                errors.append(
                    ValidationFinding(
                        code="UNKNOWN_CLAIM_REF",
                        severity=ValidationSeverity.ERROR,
                        message=(
                            f"binding {binding.binding_id} references unknown "
                            f"claim {binding.claim_id}"
                        ),
                        path=f"claim_evidence_bindings/{binding.binding_id}",
                    )
                )
            for pid in binding.passage_ids:
                if pid not in passage_ids:
                    errors.append(
                        ValidationFinding(
                            code="UNKNOWN_PASSAGE_REF",
                            severity=ValidationSeverity.ERROR,
                            message=(
                                f"binding {binding.binding_id} references "
                                f"unknown passage {pid}"
                            ),
                            path=(
                                f"claim_evidence_bindings/{binding.binding_id}/passages"
                            ),
                        )
                    )

        # Check group passage references.
        for group in (
            packet.corroborating_groups
            + packet.contradicting_groups
            + packet.qualifying_groups
            + packet.near_duplicate_groups
        ):
            for pid in group.passage_ids:
                if pid not in passage_ids:
                    errors.append(
                        ValidationFinding(
                            code="UNKNOWN_PASSAGE_REF",
                            severity=ValidationSeverity.ERROR,
                            message=(
                                f"group {group.group_id} references "
                                f"unknown passage {pid}"
                            ),
                            path=f"groups/{group.group_id}/passages",
                        )
                    )

        # Check retrieval provenance passage references.
        for rp in packet.retrieval_provenance:
            for pid in rp.selected_passage_ids:
                if pid not in passage_ids:
                    errors.append(
                        ValidationFinding(
                            code="UNKNOWN_PASSAGE_REF",
                            severity=ValidationSeverity.ERROR,
                            message=(
                                f"retrieval_provenance {rp.retrieval_event_id} "
                                f"references unknown passage {pid}"
                            ),
                            path=(
                                f"retrieval_provenance/{rp.retrieval_event_id}/passages"
                            ),
                        )
                    )

        # Check candidate_id references when candidate_ids is provided.
        if candidate_ids:
            for passage in all_passages:
                if passage.candidate_id not in candidate_ids:
                    errors.append(
                        ValidationFinding(
                            code="UNKNOWN_CANDIDATE_REF",
                            severity=ValidationSeverity.ERROR,
                            message=(
                                f"passage {passage.passage_id} references "
                                f"unknown candidate {passage.candidate_id}"
                            ),
                            path=f"passages/{passage.passage_id}",
                        )
                    )

        # Check snapshot_id references when snapshot_ids is provided.
        if snapshot_ids:
            for passage in all_passages:
                if passage.snapshot_id not in snapshot_ids:
                    errors.append(
                        ValidationFinding(
                            code="UNKNOWN_SNAPSHOT_REF",
                            severity=ValidationSeverity.ERROR,
                            message=(
                                f"passage {passage.passage_id} references "
                                f"unknown snapshot {passage.snapshot_id}"
                            ),
                            path=f"passages/{passage.passage_id}",
                        )
                    )

        # Check independence assessment candidate references.
        if candidate_ids:
            for assessment in packet.independence_assessments:
                if assessment.candidate_id not in candidate_ids:
                    errors.append(
                        ValidationFinding(
                            code="UNKNOWN_CANDIDATE_REF",
                            severity=ValidationSeverity.ERROR,
                            message=(
                                f"independence_assessment references "
                                f"unknown candidate {assessment.candidate_id}"
                            ),
                            path="independence_assessments",
                            detail={"candidate_id": str(assessment.candidate_id)},
                        )
                    )

    def _check_claim_coverage(
        self,
        packet: EvidencePacket,
        *,
        errors: list[ValidationFinding],
        warnings: list[ValidationFinding],
    ) -> None:
        """Check that every claim has at least one binding or is unsupported."""
        claims_with_bindings: set[UUID] = set()

        for binding in packet.claim_evidence_bindings:
            claims_with_bindings.add(binding.claim_id)

        for claim in packet.claims:
            if claim.semantic_status in self.EVALUATED_STATUSES:
                if claim.claim_id not in claims_with_bindings:
                    errors.append(
                        ValidationFinding(
                            code="CLAIM_NO_BINDING",
                            severity=ValidationSeverity.ERROR,
                            message=(
                                f"claim {claim.claim_id} has status "
                                f"{claim.semantic_status.value} but has no "
                                f"claim-evidence binding"
                            ),
                            path=f"claims/{claim.claim_id}",
                        )
                    )
            elif claim.semantic_status in self.UNAVALUABLE_STATUSES:
                # Unsupported/uncertain claims are allowed without bindings.
                pass
            else:
                # unassessed/uncertain — warn but don't error.
                if claim.claim_id not in claims_with_bindings:
                    warnings.append(
                        ValidationFinding(
                            code="CLAIM_NO_BINDING",
                            severity=ValidationSeverity.WARNING,
                            message=(
                                f"claim {claim.claim_id} has status "
                                f"{claim.semantic_status.value} and no binding"
                            ),
                            path=f"claims/{claim.claim_id}",
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
        """Check that group states are valid and internally consistent."""
        all_groups = (
            packet.corroborating_groups
            + packet.contradicting_groups
            + packet.qualifying_groups
            + packet.near_duplicate_groups
        )

        for group in all_groups:
            if not group.passage_ids:
                if group.evaluated:
                    errors.append(
                        ValidationFinding(
                            code="EMPTY_EVALUATED_GROUP",
                            severity=ValidationSeverity.ERROR,
                            message=(
                                f"group {group.group_id} is evaluated but "
                                f"has no passages"
                            ),
                            path=f"groups/{group.group_id}",
                        )
                    )
                else:
                    warnings.append(
                        ValidationFinding(
                            code="UNEVALUATED_EMPTY_GROUP",
                            severity=ValidationSeverity.WARNING,
                            message=(
                                f"group {group.group_id} is unevaluated and "
                                f"has no passages"
                            ),
                            path=f"groups/{group.group_id}",
                        )
                    )

            # Check for duplicate passage IDs within a group.
            passage_list = list(group.passage_ids)
            if len(passage_list) != len(set(passage_list)):
                errors.append(
                    ValidationFinding(
                        code="DUPLICATE_PASSAGE_IN_GROUP",
                        severity=ValidationSeverity.ERROR,
                        message=(
                            f"group {group.group_id} contains duplicate passage IDs"
                        ),
                        path=f"groups/{group.group_id}",
                    )
                )

            # Check that evaluated groups have a rationale.
            if group.evaluated and not group.rationale.strip():
                errors.append(
                    ValidationFinding(
                        code="MISSING_GROUP_RATIONALE",
                        severity=ValidationSeverity.ERROR,
                        message=(
                            f"group {group.group_id} is evaluated but has no rationale"
                        ),
                        path=f"groups/{group.group_id}",
                    )
                )

        # Info: count groups by type.
        info.append(
            ValidationFinding(
                code="GROUP_SUMMARY",
                severity=ValidationSeverity.INFO,
                message=(
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
        """Check that freshness summary is present and reasonable."""
        freshness = packet.freshness_summary

        if not freshness:
            warnings.append(
                ValidationFinding(
                    code="MISSING_FRESHNESS_SUMMARY",
                    severity=ValidationSeverity.WARNING,
                    message="freshness_summary is empty",
                    path="freshness_summary",
                )
            )
            return

        most_recent = freshness.get("most_recent")
        oldest = freshness.get("oldest")

        if most_recent is None and oldest is None:
            warnings.append(
                ValidationFinding(
                    code="NO_FRESHNESS_DATES",
                    severity=ValidationSeverity.WARNING,
                    message=("freshness_summary has no most_recent or oldest dates"),
                    path="freshness_summary",
                )
            )
            return

        # Parse dates and check ordering.
        try:
            if most_recent and oldest:
                mr = datetime.datetime.fromisoformat(most_recent)
                ol = datetime.datetime.fromisoformat(oldest)
                if mr < ol:
                    errors = []  # noqa: F841 — we already have warnings.
                    warnings.append(
                        ValidationFinding(
                            code="FRESHNESS_ORDERING",
                            severity=ValidationSeverity.WARNING,
                            message=("freshness_summary most_recent is before oldest"),
                            path="freshness_summary",
                            detail={
                                "most_recent": most_recent,
                                "oldest": oldest,
                            },
                        )
                    )
        except (ValueError, TypeError):
            warnings.append(
                ValidationFinding(
                    code="FRESHNESS_DATE_PARSE",
                    severity=ValidationSeverity.WARNING,
                    message="freshness_summary dates could not be parsed",
                    path="freshness_summary",
                )
            )

        info.append(
            ValidationFinding(
                code="FRESHNESS_SUMMARY",
                severity=ValidationSeverity.INFO,
                message=(f"freshness: oldest={oldest}, most_recent={most_recent}"),
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
        """Check that unresolved items are traceable to coverage."""
        if not packet.unresolved_items:
            return

        if coverage_items is not None:
            unknown = set(packet.unresolved_items) - coverage_items
            if unknown:
                errors.append(
                    ValidationFinding(
                        code="UNRESOLVED_UNKNOWN_COVERAGE",
                        severity=ValidationSeverity.ERROR,
                        message=(
                            f"unresolved items reference unknown coverage "
                            f"items: {sorted(map(str, unknown))}"
                        ),
                        path="unresolved_items",
                        detail={"unknown_items": sorted(map(str, unknown))},
                    )
                )
        else:
            warnings.append(
                ValidationFinding(
                    code="UNRESOLVED_NO_COVERAGE_CONTEXT",
                    severity=ValidationSeverity.WARNING,
                    message=(
                        f"packet has {len(packet.unresolved_items)} "
                        f"unresolved items but no coverage context was "
                        f"provided for validation"
                    ),
                    path="unresolved_items",
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
        """Check that passages fit within the token budget."""
        from firecrawl_skill.research_store.tokenizer_registry import get_tokenizer

        tokenizer = get_tokenizer("cl100k_base")
        max_tokens = effective_caps.max_evidence_packet_tokens

        total_tokens = 0
        for passage in packet.passages:
            total_tokens += len(tokenizer.encode(passage.text))

        if total_tokens > max_tokens:
            errors.append(
                ValidationFinding(
                    code="TOKEN_BUDGET_EXCEEDED",
                    severity=ValidationSeverity.ERROR,
                    message=(
                        f"included passages use {total_tokens} tokens, "
                        f"exceeding budget of {max_tokens}"
                    ),
                    path="passages",
                    detail={
                        "used_tokens": total_tokens,
                        "max_tokens": max_tokens,
                    },
                )
            )
        elif total_tokens == max_tokens:
            warnings.append(
                ValidationFinding(
                    code="TOKEN_BUDGET_FULL",
                    severity=ValidationSeverity.WARNING,
                    message=(
                        f"included passages use all {total_tokens} tokens "
                        f"(budget: {max_tokens})"
                    ),
                    path="passages",
                )
            )

        if packet.omitted_passages:
            warnings.append(
                ValidationFinding(
                    code="OMITTED_PASSAGES",
                    severity=ValidationSeverity.WARNING,
                    message=(
                        f"{len(packet.omitted_passages)} passage(s) were "
                        f"omitted due to token budget"
                    ),
                    path="omitted_passages",
                    detail={
                        "omitted_count": len(packet.omitted_passages),
                    },
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
        """Check that retrieval execution is present when candidates exist."""
        all_passages = packet.passages + packet.omitted_passages

        if all_passages and not packet.retrieval_provenance:
            errors.append(
                ValidationFinding(
                    code="MISSING_RETRIEVAL_PROVENANCE",
                    severity=ValidationSeverity.ERROR,
                    message=(
                        f"packet has {len(all_passages)} passages but no "
                        f"retrieval_provenance entries"
                    ),
                    path="retrieval_provenance",
                )
            )
        elif packet.retrieval_provenance:
            info.append(
                ValidationFinding(
                    code="RETRIEVAL_PROVENANCE_COUNT",
                    severity=ValidationSeverity.INFO,
                    message=(
                        f"{len(packet.retrieval_provenance)} "
                        f"retrieval_provenance entry/entries"
                    ),
                )
            )

    def _check_provenance(
        self,
        packet: EvidencePacket,
        *,
        errors: list[ValidationFinding],
        warnings: list[ValidationFinding],
    ) -> None:
        """Check that every passage has complete provenance."""
        for passage in packet.passages + packet.omitted_passages:
            if not passage.source_url:
                warnings.append(
                    ValidationFinding(
                        code="MISSING_SOURCE_URL",
                        severity=ValidationSeverity.WARNING,
                        message=(f"passage {passage.passage_id} has no source_url"),
                        path=f"passages/{passage.passage_id}",
                    )
                )

            if not passage.candidate_id:
                errors.append(
                    ValidationFinding(
                        code="MISSING_CANDIDATE_ID",
                        severity=ValidationSeverity.ERROR,
                        message=(f"passage {passage.passage_id} has no candidate_id"),
                        path=f"passages/{passage.passage_id}",
                    )
                )

            if not passage.snapshot_id:
                errors.append(
                    ValidationFinding(
                        code="MISSING_SNAPSHOT_ID",
                        severity=ValidationSeverity.ERROR,
                        message=(f"passage {passage.passage_id} has no snapshot_id"),
                        path=f"passages/{passage.passage_id}",
                    )
                )

            if not passage.chunk_id:
                errors.append(
                    ValidationFinding(
                        code="MISSING_CHUNK_ID",
                        severity=ValidationSeverity.ERROR,
                        message=(f"passage {passage.passage_id} has no chunk_id"),
                        path=f"passages/{passage.passage_id}",
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
        """Check that required semantic stages have been executed."""
        # Check that claims with bindings have semantic_status set.
        bound_claim_ids = {b.claim_id for b in packet.claim_evidence_bindings}

        for claim in packet.claims:
            if claim.semantic_status == SemanticStatus.UNASSESSED:
                if claim.claim_id in bound_claim_ids:
                    errors.append(
                        ValidationFinding(
                            code="BOUND_UNASSESSED_CLAIM",
                            severity=ValidationSeverity.ERROR,
                            message=(
                                f"claim {claim.claim_id} has binding(s) but "
                                f"semantic_status is unassessed"
                            ),
                            path=f"claims/{claim.claim_id}",
                        )
                    )
                else:
                    warnings.append(
                        ValidationFinding(
                            code="UNASSESSED_CLAIM",
                            severity=ValidationSeverity.WARNING,
                            message=(
                                f"claim {claim.claim_id} is unassessed and "
                                f"has no bindings"
                            ),
                            path=f"claims/{claim.claim_id}",
                        )
                    )

        # Check binding model and prompt_version are populated.
        for binding in packet.claim_evidence_bindings:
            if not binding.model:
                errors.append(
                    ValidationFinding(
                        code="MISSING_BINDING_MODEL",
                        severity=ValidationSeverity.ERROR,
                        message=(f"binding {binding.binding_id} has no model"),
                        path=f"claim_evidence_bindings/{binding.binding_id}",
                    )
                )
            if not binding.prompt_version:
                errors.append(
                    ValidationFinding(
                        code="MISSING_BINDING_PROMPT_VERSION",
                        severity=ValidationSeverity.ERROR,
                        message=(f"binding {binding.binding_id} has no prompt_version"),
                        path=f"claim_evidence_bindings/{binding.binding_id}",
                    )
                )

        # Check input_packet_revision.
        for binding in packet.claim_evidence_bindings:
            if binding.input_packet_revision < 1:
                errors.append(
                    ValidationFinding(
                        code="INVALID_INPUT_PACKET_REVISION",
                        severity=ValidationSeverity.ERROR,
                        message=(
                            f"binding {binding.binding_id} has "
                            f"input_packet_revision={binding.input_packet_revision}"
                        ),
                        path=f"claim_evidence_bindings/{binding.binding_id}",
                    )
                )

        # Info: semantic status distribution.
        status_counts: dict[str, int] = {}
        for claim in packet.claims:
            key = claim.semantic_status.value
            status_counts[key] = status_counts.get(key, 0) + 1

        if status_counts:
            info.append(
                ValidationFinding(
                    code="SEMANTIC_STATUS_DISTRIBUTION",
                    severity=ValidationSeverity.INFO,
                    message=f"semantic status distribution: {status_counts}",
                )
            )


# ---------------------------------------------------------------------------
# Bounded citation-ready output
# ---------------------------------------------------------------------------


def bounded_citation_ready_output(
    packet: EvidencePacket,
    *,
    max_passages: int = 20,
    max_claims: int = 10,
) -> dict[str, Any]:
    """Produce bounded, citation-ready output for agent consumers.

    This function takes a validated ``EvidencePacket`` and produces a
    compact, JSON-serialisable dict that contains only the information
    needed for downstream synthesis.  It is bounded so that agent
    consumers never receive an oversized payload.

    Args:
        packet: The EvidencePacket to serialise.
        max_passages: Maximum number of passages to include.
        max_claims: Maximum number of claims to include.

    Returns:
        A dict with keys ``claims``, ``passages``, ``bindings``,
        ``groups``, and ``metadata``.  All IDs are stringified.

    Design choices:

    - **First-N slicing.**  Claims and passages are truncated by position
      (``[:max_claims]`` / ``[:max_passages]``), not by confidence or
      semantic status.  This keeps the function deterministic and O(1)
      beyond the slice boundary.  A downstream synthesiser should
      re-sort or re-filter if quality matters more than reproducibility.

    - **Near-duplicate groups excluded.**  ``near_duplicate_groups`` are
      omitted from the output because they are metadata about source
      redundancy, not direct evidence for or against a claim.  They are
      tracked in the full packet for auditability.
    """
    claims_out = []
    for claim in packet.claims[:max_claims]:
        claims_out.append(
            {
                "claim_id": str(claim.claim_id),
                "statement": claim.statement,
                "semantic_status": claim.semantic_status.value,
                "uncertainty": claim.uncertainty,
            }
        )

    passages_out = []
    for passage in packet.passages[:max_passages]:
        passages_out.append(
            {
                "passage_id": str(passage.passage_id),
                "text": passage.text,
                "source_url": passage.source_url,
                "candidate_id": str(passage.candidate_id),
                "snapshot_id": str(passage.snapshot_id),
                "chunk_id": str(passage.chunk_id),
            }
        )

    # Bindings: map claim_id -> list of passage_ids with relationship.
    bindings_out: dict[str, list[dict[str, Any]]] = {}
    for binding in packet.claim_evidence_bindings:
        claim_key = str(binding.claim_id)
        if claim_key not in bindings_out:
            bindings_out[claim_key] = []
        bindings_out[claim_key].append(
            {
                "binding_id": str(binding.binding_id),
                "passage_ids": [str(p) for p in binding.passage_ids],
                "relationship": binding.relationship.value
                if hasattr(binding.relationship, "value")
                else str(binding.relationship),
                "confidence": binding.confidence,
            }
        )

    groups_out = []
    all_groups = (
        packet.corroborating_groups
        + packet.contradicting_groups
        + packet.qualifying_groups
    )
    for group in all_groups[:max_passages]:
        groups_out.append(
            {
                "group_id": str(group.group_id),
                "passage_ids": [str(p) for p in group.passage_ids],
                "rationale": group.rationale,
                "evaluated": group.evaluated,
            }
        )

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
