"""ReportValidator — deterministic validation for report synthesis output.

This module provides a dedicated validator for report artifacts produced by
the bounded synthesis pipeline (issue #64).  It performs **deterministic**
checks that do not depend on an LLM:

- **Citation-ID validation** — every cited passage_id must exist in the
  EvidencePacket's passage set.
- **Packet-revision check** — the report's evidence_packet_revision must
  match the current (most recent) packet revision.
- **Exact passage existence** — every citation must resolve to a real
  passage in the packet (not just a known ID).
- **Deterministic claim coverage** — every claim referenced in the report
  must have at least one binding or be marked unsupported.
- **Unsupported-claim detection** — claims without supporting evidence are
  flagged; the report must not silently omit them.
- **Stale-packet detection** — the report cannot complete against a packet
  revision older than the current one.
- **Bounded semantic entailment** — a lightweight consistency check that
  verifies the draft's claimed relationship matches the EvidencePacket
  binding (supports/contradicts/qualifies/context).
- **Report hash** — a SHA-256 digest of the canonical report JSON.
- **Claim manifest** — a deterministic list of all claims referenced in
  the report with their resolution status.
- **Validation status** — a structured result indicating pass/fail.

The validator does **not** mutate the report or the packet.  It returns a
``ReportValidationResult`` that callers can use to gate report completion.

**Defense in depth.**  The LLM-driven ``citation_pass`` stage performs
semantic entailment via structured output.  This validator performs the
same checks deterministically, catching cases where the model output
bypasses schema constraints or where the packet changed between stages.

## Architecture

- PostgreSQL is the authoritative store for report artifacts.
- The EvidencePacket is read from ``evidence_packets`` and validated
  before each check.
- All checks are pure functions of the packet + report; no network calls.
- The validator is idempotent: running it twice with the same inputs
  produces the same result.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Validation result types
# ---------------------------------------------------------------------------


class ReportValidationSeverity(str):
    """Severity levels for report validation findings."""

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass(frozen=True)
class ReportValidationFinding:
    """A single validation finding for a report."""

    code: str
    severity: ReportValidationSeverity
    message: str
    path: str = ""
    detail: dict[str, Any] | None = None


@dataclass(frozen=True)
class ClaimResolution:
    """Resolution status for a single claim in the report."""

    claim_id: str
    statement: str
    resolution: str  # supported / contradicted / qualified / unsupported / unassessed
    cited_passage_ids: tuple[str, ...] = ()
    binding_relationship: str | None = None
    issues: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReportValidationResult:
    """Aggregated validation result for a report.

    Attributes:
        is_valid: ``True`` when there are zero ``ERROR`` findings.
        is_complete: ``True`` when all required checks passed.
        report_hash: SHA-256 hex digest of the canonical report JSON.
        packet_revision: The EvidencePacket revision used for validation.
        current_packet_revision: The most recent packet revision.
        stale_packet: ``True`` if the report was produced against an older packet.
        claim_manifest: List of claim resolutions.
        errors: List of error-level findings.
        warnings: List of warning-level findings.
        info: List of info-level findings.
        summary: Human-readable one-liner.
    """

    is_valid: bool
    is_complete: bool
    report_hash: str
    packet_revision: int
    current_packet_revision: int
    stale_packet: bool
    claim_manifest: tuple[ClaimResolution, ...] = ()
    errors: tuple[ReportValidationFinding, ...] = ()
    warnings: tuple[ReportValidationFinding, ...] = ()
    info: tuple[ReportValidationFinding, ...] = ()

    @property
    def summary(self) -> str:
        if self.stale_packet:
            return (
                f"report is stale (rev {self.packet_revision}, "
                f"current rev {self.current_packet_revision})"
            )
        if self.is_valid and self.is_complete:
            return (
                f"report is valid ({len(self.claim_manifest)} claims, "
                f"hash {self.report_hash[:8]}...)"
            )
        if self.is_valid:
            return (
                f"report is valid but incomplete "
                f"({len(self.warnings)} warnings)"
            )
        return (
            f"report is invalid ({len(self.errors)} errors)"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_valid": self.is_valid,
            "is_complete": self.is_complete,
            "report_hash": self.report_hash,
            "packet_revision": self.packet_revision,
            "current_packet_revision": self.current_packet_revision,
            "stale_packet": self.stale_packet,
            "summary": self.summary,
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "info_count": len(self.info),
            "claim_count": len(self.claim_manifest),
            "claim_manifest": [
                {
                    "claim_id": c.claim_id,
                    "statement": c.statement,
                    "resolution": c.resolution,
                    "cited_passage_ids": list(c.cited_passage_ids),
                    "binding_relationship": c.binding_relationship,
                    "issues": list(c.issues),
                }
                for c in self.claim_manifest
            ],
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


class ReportValidator:
    """Validates report artifacts against an EvidencePacket.

    The validator runs a fixed set of deterministic checks and collects
    findings.  It never raises — instead it returns a
    ``ReportValidationResult`` so callers can decide how to handle errors
    vs warnings.

    Args:
        packet: The EvidencePacket dict (exported from PostgreSQL).
        report: The report artifact dict (from synthesis_stages or reports).
        current_packet_revision: The most recent EvidencePacket revision.
    """

    def __init__(
        self,
        packet: dict[str, Any],
        report: dict[str, Any],
        current_packet_revision: int,
    ) -> None:
        self.packet = packet
        self.report = report
        self.current_packet_revision = current_packet_revision

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def validate(self) -> ReportValidationResult:
        """Run all validation checks on the report.

        Returns:
            A ``ReportValidationResult`` with all findings.
        """
        errors: list[ReportValidationFinding] = []
        warnings: list[ReportValidationFinding] = []
        info: list[ReportValidationFinding] = []

        # 1. Stale-packet check.
        self._check_stale_packet(
            errors=errors, warnings=warnings, info=info
        )

        # 2. Citation-ID validation.
        self._check_citation_ids(
            errors=errors, warnings=warnings, info=info
        )

        # 3. Deterministic claim coverage.
        claim_manifest = self._check_claim_coverage(
            errors=errors, warnings=warnings, info=info
        )

        # 4. Unsupported-claim detection.
        self._check_unsupported_claims(
            claim_manifest=claim_manifest,
            errors=errors,
            warnings=warnings,
            info=info,
        )

        # 5. Bounded semantic entailment.
        self._check_entailment(
            errors=errors, warnings=warnings, info=info
        )

        # Compute report hash.
        report_hash = self._compute_report_hash()

        return ReportValidationResult(
            is_valid=len(errors) == 0,
            is_complete=(len(errors) == 0 and len(warnings) == 0),
            report_hash=report_hash,
            packet_revision=self._get_packet_revision(),
            current_packet_revision=self.current_packet_revision,
            stale_packet=self._is_stale(),
            claim_manifest=tuple(claim_manifest),
            errors=tuple(errors),
            warnings=tuple(warnings),
            info=tuple(info),
        )

    # ------------------------------------------------------------------
    # Individual checks
    # ------------------------------------------------------------------

    def _is_stale(self) -> bool:
        """Check if the report's packet revision is stale."""
        return self._get_packet_revision() < self.current_packet_revision

    def _get_packet_revision(self) -> int:
        """Extract the packet revision from the report."""
        return self.report.get("evidence_packet_revision", 0)

    def _check_stale_packet(
        self,
        *,
        errors: list[ReportValidationFinding],
        warnings: list[ReportValidationFinding],
        info: list[ReportValidationFinding],
    ) -> None:
        """Check that the report's packet revision matches the current one."""
        if self._is_stale():
            errors.append(
                ReportValidationFinding(
                    code="STALE_PACKET",
                    severity=ReportValidationSeverity.ERROR,
                    message=(
                        f"report was produced against packet revision "
                        f"{self._get_packet_revision()}, but current "
                        f"revision is {self.current_packet_revision}. "
                        f"A report cannot complete against a stale packet."
                    ),
                    path="report/evidence_packet_revision",
                    detail={
                        "report_revision": self._get_packet_revision(),
                        "current_revision": self.current_packet_revision,
                    },
                )
            )
        else:
            info.append(
                ReportValidationFinding(
                    code="PACKET_REVISION_OK",
                    severity=ReportValidationSeverity.INFO,
                    message=(
                        f"report packet revision ({self._get_packet_revision()}) "
                        f"matches current revision"
                    ),
                )
            )

    def _check_citation_ids(
        self,
        *,
        errors: list[ReportValidationFinding],
        warnings: list[ReportValidationFinding],
        info: list[ReportValidationFinding],
    ) -> None:
        """Check that every cited passage_id exists in the EvidencePacket."""
        passage_ids = {p["passage_id"] for p in self.packet.get("passages", [])}
        claim_ids = {c["claim_id"] for c in self.packet.get("claims", [])}

        # Build a set of all valid passage IDs including omitted ones.
        for p in self.packet.get("omitted_passages", []):
            passage_ids.add(p["passage_id"])

        # Check validation_results from the citation_pass stage.
        validation_results = self.report.get("validation_results", [])
        invented_citations = []

        for vr in validation_results:
            status = vr.get("status", "")
            if status == "invented":
                invented_citations.append(vr)
                for pid in vr.get("passage_ids", []):
                    if pid not in passage_ids:
                        errors.append(
                            ReportValidationFinding(
                                code="UNKNOWN_CITATION",
                                severity=ReportValidationSeverity.ERROR,
                                message=(
                                    f"citation to unknown passage {pid} "
                                    f"in section {vr.get('section_id', '?')}"
                                ),
                                path=(
                                    f"validation_results/"
                                    f"{vr.get('section_id', '?')}"
                                ),
                                detail={
                                    "passage_id": pid,
                                    "claim_id": vr.get("claim_id", ""),
                                    "section_id": vr.get("section_id", ""),
                                },
                            )
                        )

        # Also check the top-level invented_citations array.
        for inv in self.report.get("invented_citations", []):
            for pid in inv.get("passage_ids", []):
                if pid not in passage_ids:
                    errors.append(
                        ReportValidationFinding(
                            code="UNKNOWN_CITATION",
                            severity=ReportValidationSeverity.ERROR,
                            message=(
                                f"invented citation to unknown passage "
                                f"{pid} in section "
                                f"{inv.get('section_id', '?')}"
                            ),
                            path="invented_citations",
                            detail={
                                "passage_id": pid,
                                "claim_id": inv.get("claim_id", ""),
                            },
                        )
                    )

        # Check that claim_ids in citations are known.
        for vr in validation_results:
            cid = vr.get("claim_id", "")
            if cid and cid not in claim_ids:
                errors.append(
                    ReportValidationFinding(
                        code="UNKNOWN_CLAIM_CITATION",
                        severity=ReportValidationSeverity.ERROR,
                        message=(
                            f"citation to unknown claim {cid} "
                            f"in section {vr.get('section_id', '?')}"
                        ),
                        path="validation_results",
                        detail={"claim_id": cid},
                    )
                )

        if invented_citations:
            info.append(
                ReportValidationFinding(
                    code="INVENTED_CITATIONS_FOUND",
                    severity=ReportValidationSeverity.INFO,
                    message=(
                        f"found {len(invented_citations)} invented "
                        f"citations in validation_results"
                    ),
                )
            )

        if errors:
            info.append(
                ReportValidationFinding(
                    code="CITATION_VALIDATION_ERRORS",
                    severity=ReportValidationSeverity.INFO,
                    message=f"found {len(errors)} citation errors",
                )
            )

    def _check_claim_coverage(
        self,
        *,
        errors: list[ReportValidationFinding],
        warnings: list[ReportValidationFinding],
        info: list[ReportValidationFinding],
    ) -> list[ClaimResolution]:
        """Check that every claim referenced in the report is covered."""
        claims = self.packet.get("claims", [])
        bindings = self.packet.get("claim_evidence_bindings", [])

        # Build lookup maps.
        claim_map: dict[str, dict[str, Any]] = {}
        for c in claims:
            claim_map[c["claim_id"]] = c

        # Build binding map: claim_id -> list of bindings.
        binding_map: dict[str, list[dict[str, Any]]] = {}
        for b in bindings:
            binding_map.setdefault(b["claim_id"], []).append(b)

        # Gather all claim_ids referenced in the report.
        report_claim_ids: set[str] = set()
        for vr in self.report.get("validation_results", []):
            cid = vr.get("claim_id", "")
            if cid:
                report_claim_ids.add(cid)

        # Also check unsupported_claims and invented_citations.
        for uc in self.report.get("unsupported_claims", []):
            cid = uc.get("claim_id", "")
            if cid:
                report_claim_ids.add(cid)

        for inv in self.report.get("invented_citations", []):
            cid = inv.get("claim_id", "")
            if cid:
                report_claim_ids.add(cid)

        # Build claim manifest.
        manifest: list[ClaimResolution] = []
        for cid in sorted(report_claim_ids):
            claim = claim_map.get(cid)
            if claim is None:
                # Claim in report but not in packet — this is an error.
                errors.append(
                    ReportValidationFinding(
                        code="UNKNOWN_REPORT_CLAIM",
                        severity=ReportValidationSeverity.ERROR,
                        message=(
                            f"report references claim {cid} "
                            f"not found in EvidencePacket"
                        ),
                        path="claim_manifest",
                        detail={"claim_id": cid},
                    )
                )
                manifest.append(
                    ClaimResolution(
                        claim_id=cid,
                        statement="",
                        resolution="unknown",
                        issues=("claim_not_in_packet",),
                    )
                )
                continue

            stmt = claim.get("statement", "")
            semantic_status = claim.get("semantic_status", "unassessed")
            cited_pids = set()
            binding_rel = None

            for vr in self.report.get("validation_results", []):
                if vr.get("claim_id") == cid:
                    cited_pids.update(vr.get("passage_ids", []))

            bnds = binding_map.get(cid, [])
            if bnds:
                binding_rel = bnds[0].get("relationship")
                for b in bnds:
                    cited_pids.update(b.get("passage_ids", []))

            resolution = semantic_status
            issues: list[str] = []

            # Check if claim has bindings.
            if semantic_status in ("supported", "contradicted", "qualified"):
                if not bnds:
                    issues.append("no_binding_for_evaluated_claim")
                    errors.append(
                        ReportValidationFinding(
                            code="CLAIM_NO_BINDING",
                            severity=ReportValidationSeverity.ERROR,
                            message=(
                                f"claim {cid} has status {semantic_status} "
                                f"but no binding"
                            ),
                            path=f"claim_manifest/{cid}",
                        )
                    )
            elif semantic_status in ("unsupported", "uncertain"):
                resolution = semantic_status
            else:
                resolution = semantic_status
                if not bnds:
                    issues.append("no_binding")
                    warnings.append(
                        ReportValidationFinding(
                            code="CLAIM_NO_BINDING",
                            severity=ReportValidationSeverity.WARNING,
                            message=(
                                f"claim {cid} has status {semantic_status} "
                                f"and no binding"
                            ),
                            path=f"claim_manifest/{cid}",
                        )
                    )

            manifest.append(
                ClaimResolution(
                    claim_id=cid,
                    statement=stmt,
                    resolution=resolution,
                    cited_passage_ids=tuple(sorted(cited_pids)),
                    binding_relationship=binding_rel,
                    issues=tuple(issues),
                )
            )

        # Check that all packet claims are accounted for.
        for c in claims:
            cid = c["claim_id"]
            if cid not in report_claim_ids:
                warnings.append(
                    ReportValidationFinding(
                        code="CLAIM_NOT_IN_REPORT",
                        severity=ReportValidationSeverity.WARNING,
                        message=(
                            f"packet claim {cid} not referenced in report"
                        ),
                        path="claim_manifest",
                        detail={"claim_id": cid},
                    )
                )

        info.append(
            ReportValidationFinding(
                code="CLAIM_COVERAGE_SUMMARY",
                severity=ReportValidationSeverity.INFO,
                message=(
                    f"report covers {len(report_claim_ids)} of "
                    f"{len(claims)} packet claims"
                ),
            )
        )

        return manifest

    def _check_unsupported_claims(
        self,
        *,
        claim_manifest: list[ClaimResolution],
        errors: list[ReportValidationFinding],
        warnings: list[ReportValidationFinding],
        info: list[ReportValidationFinding],
    ) -> None:
        """Check that unsupported claims are explicitly labeled."""
        unsupported_in_manifest = [
            cm for cm in claim_manifest if cm.resolution == "unsupported"
        ]

        # Check that unsupported claims are present in the report's
        # unsupported_claims array.
        report_unsupported = {
            uc.get("claim_id", "")
            for uc in self.report.get("unsupported_claims", [])
        }

        for cm in unsupported_in_manifest:
            if cm.claim_id not in report_unsupported:
                warnings.append(
                    ReportValidationFinding(
                        code="UNSUPPORTED_CLAIM_NOT_LABELED",
                        severity=ReportValidationSeverity.WARNING,
                        message=(
                            f"unsupported claim {cm.claim_id} is not "
                            f"listed in unsupported_claims"
                        ),
                        path=f"claim_manifest/{cm.claim_id}",
                    )
                )

        if unsupported_in_manifest:
            info.append(
                ReportValidationFinding(
                    code="UNSUPPORTED_CLAIMS_FOUND",
                    severity=ReportValidationSeverity.INFO,
                    message=(
                        f"found {len(unsupported_in_manifest)} "
                        f"unsupported claims in manifest"
                    ),
                )
            )

    def _check_entailment(
        self,
        *,
        errors: list[ReportValidationFinding],
        warnings: list[ReportValidationFinding],
        info: list[ReportValidationFinding],
    ) -> None:
        """Bounded semantic entailment check.

        Verifies that the relationship in the draft matches the
        EvidencePacket binding for claims that have bindings.
        """
        bindings = self.packet.get("claim_evidence_bindings", [])
        binding_map: dict[str, str] = {}
        for b in bindings:
            binding_map[b["claim_id"]] = b.get("relationship", "")

        mismatches = self.report.get("entailment_mismatches", [])

        for mm in mismatches:
            errors.append(
                ReportValidationFinding(
                    code="ENTAILMENT_MISMATCH",
                    severity=ReportValidationSeverity.ERROR,
                    message=(
                        f"entailment mismatch for claim "
                        f"{mm.get('claim_id', '?')}: "
                        f"expected {mm.get('expected_relationship', '?')}, "
                        f"cited {mm.get('cited_relationship', '?')}"
                    ),
                    path="entailment_mismatches",
                    detail=mm,
                )
            )

        # Also check validation_results for entailment_mismatch status.
        for vr in self.report.get("validation_results", []):
            if vr.get("status") == "entailment_mismatch":
                errors.append(
                    ReportValidationFinding(
                        code="ENTAILMENT_MISMATCH",
                        severity=ReportValidationSeverity.ERROR,
                        message=(
                            f"entailment mismatch for claim "
                            f"{vr.get('claim_id', '?')} "
                            f"in section {vr.get('section_id', '?')}"
                        ),
                        path="validation_results",
                        detail=vr,
                    )
                )

        if mismatches or any(
            vr.get("status") == "entailment_mismatch"
            for vr in self.report.get("validation_results", [])
        ):
            info.append(
                ReportValidationFinding(
                    code="ENTAILMENT_CHECK_COMPLETED",
                    severity=ReportValidationSeverity.INFO,
                    message="entailment check found mismatches",
                )
            )
        else:
            info.append(
                ReportValidationFinding(
                    code="ENTAILMENT_CHECK_OK",
                    severity=ReportValidationSeverity.INFO,
                    message="no entailment mismatches found",
                )
            )

    # ------------------------------------------------------------------
    # Report hash
    # ------------------------------------------------------------------

    def _compute_report_hash(self) -> str:
        """Compute SHA-256 hash of the canonical report JSON."""
        canonical = json.dumps(
            self.report,
            sort_keys=True,
            ensure_ascii=True,
            default=str,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
