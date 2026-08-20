"""Deterministic validation for report synthesis output.

This is the canonical reporting-slice owner. It validates citation identity,
packet revision, claim coverage, unsupported claims, bounded entailment,
report hashes, and deterministic claim manifests without network calls.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any


class ReportValidationSeverity(str, Enum):
    """Severity levels for report validation findings."""

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass(frozen=True)
class ReportValidationFinding:
    code: str
    severity: ReportValidationSeverity
    message: str
    path: str = ""
    detail: dict[str, Any] | None = None


@dataclass(frozen=True)
class ClaimResolution:
    claim_id: str
    statement: str
    resolution: str
    cited_passage_ids: tuple[str, ...] = ()
    binding_relationship: str | None = None
    issues: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReportValidationResult:
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
            return f"report is valid but incomplete ({len(self.warnings)} warnings)"
        return f"report is invalid ({len(self.errors)} errors)"

    def to_dict(self) -> dict[str, Any]:
        def finding(value: ReportValidationFinding) -> dict[str, Any]:
            return {
                "code": value.code,
                "severity": value.severity.value,
                "message": value.message,
                "path": value.path,
                "detail": value.detail,
            }

        return {
            "validation_status": "valid" if self.is_valid else "invalid",
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
            "errors": [finding(item) for item in self.errors],
            "warnings": [finding(item) for item in self.warnings],
            "info": [finding(item) for item in self.info],
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)


class ReportValidator:
    """Validate one report artifact against one EvidencePacket."""

    def __init__(
        self,
        packet: dict[str, Any],
        report: dict[str, Any],
        current_packet_revision: int,
    ) -> None:
        self.packet = packet
        self.report = report
        self.current_packet_revision = current_packet_revision

    def validate(self) -> ReportValidationResult:
        errors: list[ReportValidationFinding] = []
        warnings: list[ReportValidationFinding] = []
        info: list[ReportValidationFinding] = []

        self._check_stale_packet(errors=errors, warnings=warnings, info=info)
        self._check_citation_ids(errors=errors, warnings=warnings, info=info)
        claim_manifest = self._check_claim_coverage(
            errors=errors, warnings=warnings, info=info
        )
        self._check_unsupported_claims(
            claim_manifest=claim_manifest,
            errors=errors,
            warnings=warnings,
            info=info,
        )
        self._check_entailment(errors=errors, warnings=warnings, info=info)

        return ReportValidationResult(
            is_valid=not errors,
            is_complete=not errors and not warnings,
            report_hash=self._compute_report_hash(),
            packet_revision=self._get_packet_revision(),
            current_packet_revision=self.current_packet_revision,
            stale_packet=self._is_stale(),
            claim_manifest=tuple(claim_manifest),
            errors=tuple(errors),
            warnings=tuple(warnings),
            info=tuple(info),
        )

    def _is_stale(self) -> bool:
        return self._get_packet_revision() < self.current_packet_revision

    def _get_packet_revision(self) -> int:
        value = self.report.get("evidence_packet_revision", 0)
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    def _check_stale_packet(
        self,
        *,
        errors: list[ReportValidationFinding],
        warnings: list[ReportValidationFinding],
        info: list[ReportValidationFinding],
    ) -> None:
        del warnings
        if self._is_stale():
            errors.append(
                ReportValidationFinding(
                    code="STALE_PACKET",
                    severity=ReportValidationSeverity.ERROR,
                    message=(
                        "report was produced against packet revision "
                        f"{self._get_packet_revision()}, but current revision is "
                        f"{self.current_packet_revision}. A report cannot complete "
                        "against a stale packet."
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
                        "matches current revision"
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
        del warnings
        passage_ids = {p["passage_id"] for p in self.packet.get("passages", [])}
        claim_ids = {c["claim_id"] for c in self.packet.get("claims", [])}
        passage_ids.update(
            p["passage_id"] for p in self.packet.get("omitted_passages", [])
        )

        citation_error_count = 0
        invented_citations = self.report.get("invented_citations", [])
        for inv in invented_citations:
            for pid in inv.get("passage_ids", []):
                if pid not in passage_ids:
                    citation_error_count += 1
                    errors.append(
                        ReportValidationFinding(
                            code="UNKNOWN_CITATION",
                            severity=ReportValidationSeverity.ERROR,
                            message=(
                                f"invented citation to unknown passage {pid} in section "
                                f"{inv.get('section_id', '?')}"
                            ),
                            path="invented_citations",
                            detail={
                                "passage_id": pid,
                                "claim_id": inv.get("claim_id", ""),
                            },
                        )
                    )

        for vr in self.report.get("validation_results", []):
            cid = vr.get("claim_id", "")
            if cid and cid not in claim_ids:
                citation_error_count += 1
                errors.append(
                    ReportValidationFinding(
                        code="UNKNOWN_CLAIM_CITATION",
                        severity=ReportValidationSeverity.ERROR,
                        message=(
                            f"citation to unknown claim {cid} in section "
                            f"{vr.get('section_id', '?')}"
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
                    message=f"found {len(invented_citations)} invented citations",
                )
            )
        if citation_error_count:
            info.append(
                ReportValidationFinding(
                    code="CITATION_VALIDATION_ERRORS",
                    severity=ReportValidationSeverity.INFO,
                    message=f"found {citation_error_count} citation errors",
                )
            )

    def _check_claim_coverage(
        self,
        *,
        errors: list[ReportValidationFinding],
        warnings: list[ReportValidationFinding],
        info: list[ReportValidationFinding],
    ) -> list[ClaimResolution]:
        claims = self.packet.get("claims", [])
        bindings = self.packet.get("claim_evidence_bindings", [])
        claim_map = {c["claim_id"]: c for c in claims}
        binding_map: dict[str, list[dict[str, Any]]] = {}
        for binding in bindings:
            binding_map.setdefault(binding["claim_id"], []).append(binding)

        report_claim_ids: set[str] = set()
        for name in ("validation_results", "unsupported_claims", "invented_citations"):
            for item in self.report.get(name, []):
                cid = item.get("claim_id", "")
                if cid:
                    report_claim_ids.add(cid)

        manifest: list[ClaimResolution] = []
        for cid in sorted(report_claim_ids):
            claim = claim_map.get(cid)
            if claim is None:
                errors.append(
                    ReportValidationFinding(
                        code="UNKNOWN_REPORT_CLAIM",
                        severity=ReportValidationSeverity.ERROR,
                        message=f"report references claim {cid} not found in EvidencePacket",
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

            semantic_status = str(claim.get("semantic_status", "unassessed"))
            cited_pids: set[str] = set()
            for vr in self.report.get("validation_results", []):
                if vr.get("claim_id") == cid:
                    cited_pids.update(str(pid) for pid in vr.get("passage_ids", []))

            claim_bindings = binding_map.get(cid, [])
            binding_rel = (
                str(claim_bindings[0].get("relationship")) if claim_bindings else None
            )
            for binding in claim_bindings:
                cited_pids.update(str(pid) for pid in binding.get("passage_ids", []))

            issues: list[str] = []
            if semantic_status in {"supported", "contradicted", "qualified"}:
                if not claim_bindings:
                    issues.append("no_binding_for_evaluated_claim")
                    errors.append(
                        ReportValidationFinding(
                            code="CLAIM_NO_BINDING",
                            severity=ReportValidationSeverity.ERROR,
                            message=(
                                f"claim {cid} has status {semantic_status} but no binding"
                            ),
                            path=f"claim_manifest/{cid}",
                        )
                    )
            elif semantic_status not in {"unsupported", "uncertain"} and not claim_bindings:
                issues.append("no_binding")
                warnings.append(
                    ReportValidationFinding(
                        code="CLAIM_NO_BINDING",
                        severity=ReportValidationSeverity.WARNING,
                        message=(
                            f"claim {cid} has status {semantic_status} and no binding"
                        ),
                        path=f"claim_manifest/{cid}",
                    )
                )

            manifest.append(
                ClaimResolution(
                    claim_id=cid,
                    statement=str(claim.get("statement", "")),
                    resolution=semantic_status,
                    cited_passage_ids=tuple(sorted(cited_pids)),
                    binding_relationship=binding_rel,
                    issues=tuple(issues),
                )
            )

        for claim in claims:
            cid = claim["claim_id"]
            if cid not in report_claim_ids:
                warnings.append(
                    ReportValidationFinding(
                        code="CLAIM_NOT_IN_REPORT",
                        severity=ReportValidationSeverity.WARNING,
                        message=f"packet claim {cid} not referenced in report",
                        path="claim_manifest",
                        detail={"claim_id": cid},
                    )
                )

        info.append(
            ReportValidationFinding(
                code="CLAIM_COVERAGE_SUMMARY",
                severity=ReportValidationSeverity.INFO,
                message=(
                    f"report covers {len(report_claim_ids)} of {len(claims)} packet claims"
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
        del errors
        unsupported = [cm for cm in claim_manifest if cm.resolution == "unsupported"]
        reported = {
            item.get("claim_id", "")
            for item in self.report.get("unsupported_claims", [])
        }
        for claim in unsupported:
            if claim.claim_id not in reported:
                warnings.append(
                    ReportValidationFinding(
                        code="UNSUPPORTED_CLAIM_NOT_LABELED",
                        severity=ReportValidationSeverity.WARNING,
                        message=(
                            f"unsupported claim {claim.claim_id} is not listed in "
                            "unsupported_claims"
                        ),
                        path=f"claim_manifest/{claim.claim_id}",
                    )
                )
        if unsupported:
            info.append(
                ReportValidationFinding(
                    code="UNSUPPORTED_CLAIMS_FOUND",
                    severity=ReportValidationSeverity.INFO,
                    message=f"found {len(unsupported)} unsupported claims in manifest",
                )
            )

    def _check_entailment(
        self,
        *,
        errors: list[ReportValidationFinding],
        warnings: list[ReportValidationFinding],
        info: list[ReportValidationFinding],
    ) -> None:
        for mismatch in self.report.get("entailment_mismatches", []):
            errors.append(
                ReportValidationFinding(
                    code="ENTAILMENT_MISMATCH",
                    severity=ReportValidationSeverity.ERROR,
                    message=(
                        f"entailment mismatch for claim {mismatch.get('claim_id', '?')}: "
                        f"expected {mismatch.get('expected_relationship', '?')}, "
                        f"cited {mismatch.get('cited_relationship', '?')}"
                    ),
                    path="entailment_mismatches",
                    detail=mismatch,
                )
            )

        for result in self.report.get("validation_results", []):
            if result.get("status") == "entailment_mismatch":
                errors.append(
                    ReportValidationFinding(
                        code="ENTAILMENT_MISMATCH",
                        severity=ReportValidationSeverity.ERROR,
                        message=(
                            f"entailment mismatch for claim {result.get('claim_id', '?')} "
                            f"in section {result.get('section_id', '?')}"
                        ),
                        path="validation_results",
                        detail=result,
                    )
                )

        self._check_passage_support(errors=errors, warnings=warnings, info=info)

    def _check_passage_support(
        self,
        *,
        errors: list[ReportValidationFinding],
        warnings: list[ReportValidationFinding],
        info: list[ReportValidationFinding],
    ) -> None:
        del errors, info
        min_shared_terms = 2
        claim_map = {c["claim_id"]: c for c in self.packet.get("claims", [])}
        passage_map = {
            p["passage_id"]: p for p in self.packet.get("passages", [])
        }
        report_claims: dict[str, list[str]] = {}
        for name in ("validation_results", "invented_citations"):
            for item in self.report.get(name, []):
                cid = item.get("claim_id", "")
                pids = item.get("passage_ids", [])
                if cid and pids:
                    report_claims.setdefault(cid, []).extend(str(pid) for pid in pids)

        weak_support: list[str] = []
        for claim_id, passage_ids in report_claims.items():
            claim = claim_map.get(claim_id)
            if claim is None:
                continue
            claim_terms = set(_extract_terms(str(claim.get("statement", ""))))
            if not claim_terms:
                continue
            all_passage_terms: set[str] = set()
            for pid in set(passage_ids):
                passage = passage_map.get(pid)
                if passage:
                    all_passage_terms.update(
                        _extract_terms(str(passage.get("text", "")))
                    )
            if all_passage_terms and len(claim_terms & all_passage_terms) < min_shared_terms:
                weak_support.append(claim_id)

        if weak_support:
            warnings.append(
                ReportValidationFinding(
                    code="WEAK_PASSAGE_SUPPORT",
                    severity=ReportValidationSeverity.WARNING,
                    message=(
                        f"{len(weak_support)} claim(s) have weak passage support "
                        f"(few shared terms): {', '.join(weak_support[:5])}"
                    ),
                    path="claim_manifest",
                    detail={"weak_claims": weak_support},
                )
            )

    def _compute_report_hash(self) -> str:
        canonical = json.dumps(
            self.report,
            sort_keys=True,
            ensure_ascii=True,
            default=str,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _extract_terms(text: str) -> list[str]:
    return [
        token
        for token in re.split(r"[^a-z0-9]+", text.lower())
        if len(token) >= 2
    ]


__all__ = [
    "ClaimResolution",
    "ReportValidationFinding",
    "ReportValidationResult",
    "ReportValidationSeverity",
    "ReportValidator",
    "_extract_terms",
]
