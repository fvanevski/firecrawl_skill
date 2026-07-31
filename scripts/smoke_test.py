#!/usr/bin/env python3
"""Reduced real smoke gate for authoritative release execution.

This gate runs exactly two strict campaigns and one benchmark objective.
By default it exercises ``autonomous_local`` and ``deterministic_debug``;
``agent_led`` is opt-in.  It is not closure evidence.  It blocks the full
campaign unless both repetitions return ``go`` and the production
reproducibility comparator passes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlsplit

SCRIPTS = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))

from research_domain.models import RecommendationOutcome
from research_store.execution_policy import ExecutionModePolicy
from research_store.release_benchmark import (
    MANDATORY_PERFORMANCE_METRICS,
    MANDATORY_QUALITY_METRICS,
    RELEASE_MODES,
    MetricStatus,
    ReleaseBenchmarkConfig,
    ReleaseBenchmarkResult,
    ReleaseBenchmarkRunner,
    ReproducibilityComparison,
)
from research_store.strict_benchmark import (
    _build_env_manifest,
    _compute_file_hash,
    _preflight_check,
    _write_json_atomic,
)
from research_store.workflow_benchmark import load_benchmark_dataset

DEFAULT_DATASET = REPO_ROOT / "tests" / "fixtures" / "benchmark" / "benchmark-v1.json"
_SHA_RE = re.compile(r"[0-9a-f]{40}")
_ALLOWED_METRIC_STATUSES = {MetricStatus.MEASURED, MetricStatus.NOT_APPLICABLE}
DEFAULT_SMOKE_MODES = tuple(mode for mode in RELEASE_MODES if mode != "agent_led")
_TRUE_ENV_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_ENV_VALUES = frozenset({"0", "false", "no", "off", ""})


class SmokeGateError(RuntimeError):
    """The reduced real smoke gate failed a mandatory invariant."""


def _env_flag(name: str, environ: Mapping[str, str]) -> bool:
    raw = environ.get(name)
    if raw is None:
        return False
    normalized = raw.strip().lower()
    if normalized in _TRUE_ENV_VALUES:
        return True
    if normalized in _FALSE_ENV_VALUES:
        return False
    raise SmokeGateError(f"{name} must be one of: 1, true, yes, on, 0, false, no, off")


def resolve_execution_modes(
    *,
    include_agent_led: bool,
    environ: Mapping[str, str] | None = None,
) -> tuple[tuple[str, ...], bool]:
    """Resolve selected modes with the disable environment override winning."""
    environment = os.environ if environ is None else environ
    disabled_by_env = _env_flag("SMOKE_DISABLE_AGENT_LED", environment)
    effective_agent_led = include_agent_led and not disabled_by_env
    modes = RELEASE_MODES if effective_agent_led else DEFAULT_SMOKE_MODES
    return tuple(modes), disabled_by_env


@dataclass(frozen=True)
class ExternalArtifactResult:
    """Duck-typed structured result returned by an external host process."""

    value: dict[str, Any] | None
    provenance: dict[str, Any]
    attempts: tuple[dict[str, Any], ...]
    error: str = ""


def _canonical_endpoint(value: str) -> tuple[str, str, int | None, str]:
    parsed = urlsplit(value.rstrip("/"))
    return (
        parsed.scheme.lower(),
        (parsed.hostname or "").lower(),
        parsed.port,
        parsed.path.rstrip("/"),
    )


def _sha256_json(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _run_git(*args: str, repo_root: Path = REPO_ROOT) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if completed.returncode != 0:
        raise SmokeGateError(
            f"git {' '.join(args)} failed: {completed.stderr.strip() or completed.stdout.strip()}"
        )
    return completed.stdout.strip()


def verify_candidate_checkout(candidate_sha: str, repo_root: Path = REPO_ROOT) -> dict:
    """Require an exact candidate SHA and a completely clean checkout."""
    if not _SHA_RE.fullmatch(candidate_sha):
        raise SmokeGateError(
            "candidate SHA must be exactly 40 lowercase hexadecimal characters"
        )
    head = _run_git("rev-parse", "HEAD", repo_root=repo_root)
    if head != candidate_sha:
        raise SmokeGateError(
            f"candidate SHA {candidate_sha} does not match HEAD {head}"
        )
    status = _run_git(
        "status", "--porcelain=v1", "--untracked-files=all", repo_root=repo_root
    )
    if status:
        raise SmokeGateError(
            "smoke execution requires a clean checkout; commit or remove:\n" + status
        )
    tree_hash = _run_git("rev-parse", "HEAD^{tree}", repo_root=repo_root)
    return {"candidate_sha": head, "tree_hash": tree_hash, "working_tree_clean": True}


class ExternalProcessHostArtifactSupplier:
    """Read host-authored artifacts from a distinct external process over stdio.

    The subprocess receives one JSON request on stdin and must emit one JSON
    response on stdout.  It must not identify the autonomous local model
    endpoint as its source endpoint.
    """

    def __init__(
        self,
        command: Sequence[str],
        *,
        supplier_identity: str,
        source_endpoint: str,
        timeout_seconds: float = 300.0,
        autonomous_endpoints: Sequence[str] = (),
    ) -> None:
        if not command:
            raise SmokeGateError("an external host supplier command is required")
        if not supplier_identity.strip():
            raise SmokeGateError("external host supplier identity is required")
        if not source_endpoint.strip():
            raise SmokeGateError("external host supplier endpoint is required")
        if not autonomous_endpoints:
            raise SmokeGateError(
                "an explicit autonomous local model endpoint fingerprint is required"
            )
        canonical_source = _canonical_endpoint(source_endpoint)
        for endpoint in autonomous_endpoints:
            if endpoint and _canonical_endpoint(endpoint) == canonical_source:
                raise SmokeGateError(
                    "agent_led host supplier endpoint must differ from the autonomous local model endpoint"
                )
        self.command = tuple(command)
        self.supplier_identity = supplier_identity.strip()
        self.source_endpoint = source_endpoint.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.command_sha256 = hashlib.sha256(
            "\0".join(self.command).encode("utf-8")
        ).hexdigest()

    def _invoke(self, request: dict[str, Any]) -> dict[str, Any]:
        completed = subprocess.run(
            self.command,
            input=json.dumps(request, sort_keys=True),
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            check=False,
        )
        if completed.returncode != 0:
            raise SmokeGateError(
                "external host supplier failed "
                f"(exit {completed.returncode}): {completed.stderr.strip()[:1000]}"
            )
        try:
            response = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise SmokeGateError(
                "external host supplier returned invalid JSON"
            ) from exc
        if not isinstance(response, dict):
            raise SmokeGateError(
                "external host supplier response must be a JSON object"
            )
        return response

    def probe(self) -> dict[str, Any]:
        response = self._invoke(
            {
                "protocol": "firecrawl-host-artifact-stdio-v1",
                "operation": "probe",
                "supplier_identity": self.supplier_identity,
            }
        )
        if response.get("status") != "available":
            raise SmokeGateError(
                f"external host supplier probe failed: {response.get('error') or response}"
            )
        if response.get("supplier_identity") != self.supplier_identity:
            raise SmokeGateError("external host supplier probe identity mismatch")
        response_endpoint = str(response.get("source_endpoint") or "").rstrip("/")
        if response_endpoint != self.source_endpoint:
            raise SmokeGateError("external host supplier probe endpoint mismatch")
        return response

    def supply(
        self,
        *,
        semantic_context: dict[str, Any],
        schema: Mapping[str, Any],
        **call_kwargs: Any,
    ) -> ExternalArtifactResult:
        request = {
            "protocol": "firecrawl-host-artifact-stdio-v1",
            "operation": "supply",
            "semantic_context": semantic_context,
            "schema": dict(schema),
            "provider": call_kwargs.get("provider"),
            "model": call_kwargs.get("model"),
            "system_prompt": call_kwargs.get("system_prompt"),
            "user_prompt": call_kwargs.get("user_prompt"),
            "prompt_version": call_kwargs.get("prompt_version"),
        }
        request_sha256 = _sha256_json(request)
        try:
            response = self._invoke(request)
        except (SmokeGateError, subprocess.TimeoutExpired) as exc:
            return ExternalArtifactResult(
                value=None,
                provenance={
                    "supplier_identity": self.supplier_identity,
                    "source_endpoint": self.source_endpoint,
                    "request_sha256": request_sha256,
                    "command_sha256": self.command_sha256,
                },
                attempts=(),
                error=str(exc),
            )
        value = response.get("value")
        error = str(response.get("error") or "")
        if not error and not isinstance(value, dict):
            error = "external host supplier returned no object artifact"
        response_provenance = response.get("provenance")
        if not isinstance(response_provenance, dict):
            response_provenance = {}
        artifact_sha256 = _sha256_json(value) if isinstance(value, dict) else ""
        provenance = {
            **response_provenance,
            "protocol": "firecrawl-host-artifact-stdio-v1",
            "authority_origin": "external-process",
            "supplier_identity": self.supplier_identity,
            "source_endpoint": self.source_endpoint,
            "source_process": Path(self.command[0]).name,
            "request_sha256": request_sha256,
            "artifact_sha256": artifact_sha256,
            "command_sha256": self.command_sha256,
            "schema_name": semantic_context.get("schema_name"),
            "schema_version": semantic_context.get("schema_version"),
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
        }
        attempts = response.get("attempts")
        normalized_attempts = (
            tuple(item for item in attempts if isinstance(item, dict))
            if isinstance(attempts, list)
            else ()
        )
        return ExternalArtifactResult(value, provenance, normalized_attempts, error)


def _metric_source_to_dict(source: Any) -> dict[str, Any]:
    return {
        "table": source.table,
        "column": source.column,
        "run_id": source.run_id,
        "method": source.method,
        "record_ids": list(source.event_ids),
        "stages": list(source.stages),
        "stage_set_version": source.stage_set_version,
        "sample_count": source.sample_count,
        "device_type": source.device_type,
        "device_index": source.device_index,
        "device_uuid": source.device_uuid,
        "collector": source.collector,
        "collector_version": source.collector_version,
        "status_counts": dict(source.status_counts),
    }


def _metric_to_dict(metric: Any) -> dict[str, Any]:
    status = getattr(metric, "status", MetricStatus.UNEVALUATED)
    return {
        "name": metric.name,
        "value": metric.value,
        "status": status.value if isinstance(status, MetricStatus) else str(status),
        "formula": metric.formula,
        "source": _metric_source_to_dict(metric.source),
    }


def _check_to_dict(check: Any) -> dict[str, Any]:
    return {
        "name": getattr(check, "name", getattr(check, "check_name", "unknown")),
        "passed": bool(getattr(check, "passed", False)),
        "details": str(getattr(check, "details", "")),
    }


def result_to_dict(result: ReleaseBenchmarkResult) -> dict[str, Any]:
    """Serialize the complete release result used by the smoke gate."""
    recommendation = result.recommendation
    return {
        "schema_version": result.schema_version,
        "campaign_id": result.campaign_id,
        "campaign_timestamp": result.campaign_timestamp,
        "environment": result.environment,
        "total_duration_ms": result.total_duration_ms,
        "recommendation": {
            "outcome": recommendation.outcome,
            "supported_claims": list(recommendation.supported_claims),
            "withdrawn_claims": list(recommendation.withdrawn_claims),
            "known_limitations": list(recommendation.known_limitations),
            "conditions": list(recommendation.conditions),
            "p0_regressions": list(recommendation.p0_regressions),
        }
        if recommendation
        else None,
        "runs": [
            {
                "schema_version": run.schema_version,
                "campaign_id": run.campaign_id,
                "run_id": run.run_id,
                "mode": run.mode,
                "objective_id": run.objective_id,
                "orchestration_outcome": getattr(run, "orchestration_outcome", None),
                "errors": list(run.errors),
                "quality": vars(run.quality) if run.quality else None,
                "performance": vars(run.performance) if run.performance else None,
                "quality_metrics": [
                    _metric_to_dict(item) for item in run.quality_metrics
                ],
                "performance_metrics": [
                    _metric_to_dict(item) for item in run.performance_metrics
                ],
                "integrity_checks": [
                    _check_to_dict(item) for item in run.integrity_checks
                ],
                "completeness_invariants": [
                    _check_to_dict(item)
                    for item in getattr(run, "completeness_invariants", ())
                ],
            }
            for run in result.runs
        ],
    }


def comparison_to_dict(comparison: ReproducibilityComparison) -> dict[str, Any]:
    return {
        "schema_version": comparison.schema_version,
        "run_a_id": comparison.run_a_id,
        "run_b_id": comparison.run_b_id,
        "mode": comparison.mode,
        "objective_id": comparison.objective_id,
        "all_within_tolerance": comparison.all_within_tolerance,
        "quality_tolerances": list(comparison.quality_tolerances),
        "performance_tolerances": list(comparison.performance_tolerances),
        "details": list(comparison.details),
    }


def validate_metric_completeness(run: Any) -> list[str]:
    errors: list[str] = []
    quality = {item.name: item for item in run.quality_metrics}
    performance = {item.name: item for item in run.performance_metrics}
    for name in sorted(MANDATORY_QUALITY_METRICS):
        metric = quality.get(name)
        if metric is None:
            errors.append(f"missing mandatory quality metric: {name}")
            continue
        if metric.status not in _ALLOWED_METRIC_STATUSES:
            errors.append(f"quality metric {name} is {metric.status.value}")
        elif metric.status == MetricStatus.MEASURED and metric.value is None:
            errors.append(f"quality metric {name} is measured with null value")
    for name in sorted(MANDATORY_PERFORMANCE_METRICS):
        metric = performance.get(name)
        if metric is None:
            errors.append(f"missing mandatory performance metric: {name}")
            continue
        if metric.status not in _ALLOWED_METRIC_STATUSES:
            errors.append(f"performance metric {name} is {metric.status.value}")
        elif metric.status == MetricStatus.MEASURED and metric.value is None:
            errors.append(f"performance metric {name} is measured with null value")
    return errors


def validate_run_contract(run: Any) -> list[str]:
    errors = list(run.errors)
    if getattr(run, "orchestration_outcome", None) != "completed":
        errors.append(
            "orchestration outcome is not completed: "
            f"{getattr(run, 'orchestration_outcome', None)}"
        )
    errors.extend(validate_metric_completeness(run))
    for check in run.integrity_checks:
        if not check.passed:
            errors.append(f"integrity check failed: {check.check_name}")
    for check in getattr(run, "completeness_invariants", ()):
        if not check.passed:
            errors.append(
                "completeness invariant failed: "
                f"{getattr(check, 'name', getattr(check, 'check_name', 'unknown'))}"
            )
    return errors


class RunEvidenceInspector:
    """Inspect exact-run assets, reports, and persisted semantic authority."""

    _COUNT_QUERIES: ClassVar[dict[str, str]] = {
        "search_candidates": "SELECT COUNT(*) FROM search_candidates WHERE run_id=%s",
        "run_assets": "SELECT COUNT(*) FROM research_run_assets WHERE run_id=%s",
        "snapshots": """
            SELECT COUNT(*) FROM asset_snapshots s
            JOIN research_run_assets rra ON rra.snapshot_id=s.id
            WHERE rra.run_id=%s
        """,
        "documents": """
            SELECT COUNT(*) FROM documents d
            JOIN research_run_assets rra ON rra.snapshot_id=d.snapshot_id
            WHERE rra.run_id=%s
        """,
        "chunks": """
            SELECT COUNT(*) FROM chunks c
            JOIN documents d ON d.id=c.document_id
            JOIN research_run_assets rra ON rra.snapshot_id=d.snapshot_id
            WHERE rra.run_id=%s
        """,
        "claims": "SELECT COUNT(*) FROM research_claims WHERE run_id=%s",
        "claim_evidence_links": "SELECT COUNT(*) FROM claim_evidence_links WHERE run_id=%s",
        "evidence_packets": "SELECT COUNT(*) FROM evidence_packets WHERE run_id=%s",
        "semantic_calls": "SELECT COUNT(*) FROM semantic_calls WHERE run_id=%s AND status='complete'",
        "synthesis_stages": "SELECT COUNT(*) FROM synthesis_stages WHERE run_id=%s AND stage_status='completed'",
    }

    def __init__(self, database_url: str) -> None:
        self.database_url = database_url

    @staticmethod
    def _longest_text(value: Any) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, Mapping):
            candidates = [
                RunEvidenceInspector._longest_text(item) for item in value.values()
            ]
            return max(candidates, key=len, default="")
        if isinstance(value, list):
            candidates = [RunEvidenceInspector._longest_text(item) for item in value]
            return max(candidates, key=len, default="")
        return ""

    @staticmethod
    def _draft_report_text(
        synthesis_rows: Sequence[tuple[Any, Any, Any]],
    ) -> str:
        """Return only persisted completed draft report-section bodies."""
        for stage, status, artifact in reversed(synthesis_rows):
            if stage != "draft" or status != "completed":
                continue
            if not isinstance(artifact, Mapping):
                return ""
            sections = artifact.get("report_sections")
            if not isinstance(sections, list):
                return ""
            bodies = [
                str(section.get("body") or "").strip()
                for section in sections
                if isinstance(section, Mapping)
            ]
            return "\n\n".join(body for body in bodies if body)
        return ""

    def inspect(self, run: Any) -> dict[str, Any]:
        import psycopg

        run_id = str(run.run_id)
        expected_authority = ExecutionModePolicy().authority_for(run.mode).value
        with (
            psycopg.connect(self.database_url) as connection,
            connection.cursor() as cur,
        ):
            counts: dict[str, int] = {}
            for name, query in self._COUNT_QUERIES.items():
                cur.execute(query, (run_id,))
                counts[name] = int(cur.fetchone()[0] or 0)

            cur.execute(
                """SELECT authority, COUNT(*) FROM semantic_calls
                   WHERE run_id=%s AND status='complete'
                   GROUP BY authority ORDER BY authority""",
                (run_id,),
            )
            authority_counts = {str(name): int(count) for name, count in cur.fetchall()}

            cur.execute(
                """SELECT response_metadata FROM semantic_calls
                   WHERE run_id=%s AND authority='host-agent' AND status='complete'""",
                (run_id,),
            )
            host_metadata = [row[0] for row in cur.fetchall()]

            cur.execute(
                """SELECT stage_name, stage_status, artifact FROM synthesis_stages
                   WHERE run_id=%s ORDER BY updated_at""",
                (run_id,),
            )
            synthesis_rows = cur.fetchall()

        errors = [
            f"{name} count is zero" for name, count in counts.items() if count <= 0
        ]
        if set(authority_counts) != {expected_authority}:
            errors.append(
                f"semantic authority mismatch: expected only {expected_authority}, got {authority_counts}"
            )
        completed_stages = {
            str(stage)
            for stage, status, _artifact in synthesis_rows
            if status == "completed"
        }
        for required_stage in (
            "outline",
            "binding",
            "draft",
            "citation_pass",
            "validation",
        ):
            if required_stage not in completed_stages:
                errors.append(f"missing completed synthesis stage: {required_stage}")
        report_text = self._draft_report_text(synthesis_rows)
        if len(report_text) < 200:
            errors.append(
                "no substantive completed draft report body (minimum 200 characters)"
            )

        if run.mode == "agent_led":
            if not host_metadata:
                errors.append("agent_led has no completed host-agent semantic metadata")
            for index, metadata in enumerate(host_metadata):
                provenance = (
                    metadata.get("provenance", {}) if isinstance(metadata, dict) else {}
                )
                required = {
                    "authority_origin",
                    "supplier_identity",
                    "source_endpoint",
                    "source_process",
                    "request_sha256",
                    "artifact_sha256",
                    "command_sha256",
                    "schema_name",
                    "schema_version",
                    "created_at",
                }
                missing = sorted(name for name in required if not provenance.get(name))
                if missing:
                    errors.append(
                        f"host-agent semantic call {index} lacks provenance: {', '.join(missing)}"
                    )
                if provenance.get("authority_origin") != "external-process":
                    errors.append(
                        f"host-agent semantic call {index} is not external-process authority"
                    )

        return {
            "run_id": run_id,
            "mode": run.mode,
            "objective_id": run.objective_id,
            "expected_authority": expected_authority,
            "authority_counts": authority_counts,
            "counts": counts,
            "completed_synthesis_stages": sorted(completed_stages),
            "report_text_sha256": hashlib.sha256(
                report_text.encode("utf-8")
            ).hexdigest(),
            "report_text_length": len(report_text),
            "errors": errors,
        }


def run_campaign(
    *,
    label: str,
    loader: Any,
    dataset_path: Path,
    database_url: str,
    blob_root: Path,
    qdrant_url: str,
    qdrant_api_key: str,
    objective_id: str,
    tolerance: float,
    execution_modes: tuple[str, ...],
    supplier: ExternalProcessHostArtifactSupplier | None,
    campaign_root: Path,
    candidate_sha: str,
) -> tuple[ReleaseBenchmarkResult, ReleaseBenchmarkRunner, Path, dict[str, Any]]:
    config = ReleaseBenchmarkConfig(
        database_url=database_url,
        blob_root=blob_root,
        qdrant_url=qdrant_url,
        qdrant_api_key=qdrant_api_key,
        execution_modes=execution_modes,
        objective_ids=(objective_id,),
        strict=True,
        reproducibility_tolerance=tolerance,
        host_artifact_supplier=supplier,
    )
    runner = ReleaseBenchmarkRunner(loader, config)
    result = runner.run()
    expected_pairs = {(mode, objective_id) for mode in execution_modes}
    actual_pairs = {(run.mode, run.objective_id) for run in result.runs}
    if actual_pairs != expected_pairs:
        raise SmokeGateError(
            f"campaign {label} run set mismatch: expected {sorted(expected_pairs)}, got {sorted(actual_pairs)}"
        )

    inspector = RunEvidenceInspector(database_url)
    inspections: list[dict[str, Any]] = []
    contract_errors: list[str] = []
    for run in result.runs:
        for error in validate_run_contract(run):
            contract_errors.append(f"{run.mode}/{run.objective_id}: {error}")
        inspection = inspector.inspect(run)
        inspections.append(inspection)
        for error in inspection["errors"]:
            contract_errors.append(f"{run.mode}/{run.objective_id}: {error}")

    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    artifacts_dir = campaign_root / candidate_sha / label / timestamp
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    result_hash = _write_json_atomic(
        artifacts_dir / "result.json", result_to_dict(result)
    )
    inspection_hash = _write_json_atomic(
        artifacts_dir / "run-evidence.json",
        {"schema_version": "smoke-run-evidence-v1", "runs": inspections},
    )
    environment = {
        **_build_env_manifest(
            candidate_sha, dataset_path, _compute_file_hash(dataset_path)
        ),
        "smoke_label": label,
        "strict": True,
        "execution_modes": list(execution_modes),
        "objective_ids": [objective_id],
    }
    if supplier is not None:
        environment.update(
            {
                "host_supplier_identity": supplier.supplier_identity,
                "host_supplier_endpoint": supplier.source_endpoint,
                "host_supplier_command_sha256": supplier.command_sha256,
            }
        )
    _write_json_atomic(artifacts_dir / "environment.json", environment)
    (artifacts_dir / "summary.txt").write_text(
        result.summary() + "\n", encoding="utf-8"
    )
    if contract_errors:
        _write_json_atomic(
            artifacts_dir / "failures.json",
            {"schema_version": "smoke-failures-v1", "errors": contract_errors},
        )
        raise SmokeGateError(
            f"campaign {label} failed smoke invariants:\n- "
            + "\n- ".join(contract_errors)
        )
    return (
        result,
        runner,
        artifacts_dir,
        {
            "result_hash": result_hash,
            "inspection_hash": inspection_hash,
        },
    )


def gate_passes(
    result_a: ReleaseBenchmarkResult,
    result_b: ReleaseBenchmarkResult,
    comparison: ReproducibilityComparison,
) -> bool:
    return (
        result_a.recommendation is not None
        and result_b.recommendation is not None
        and result_a.recommendation.outcome == RecommendationOutcome.GO.value
        and result_b.recommendation.outcome == RecommendationOutcome.GO.value
        and comparison.all_within_tolerance
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-sha", required=True)
    parser.add_argument("--objective", default="obj-001")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument(
        "--campaign-dir", type=Path, default=Path("/tmp/firecrawl_smoke_test")
    )
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL", ""))
    parser.add_argument(
        "--blob-root",
        type=Path,
        default=Path(os.environ.get("BLOB_ROOT", "/tmp/smoke-blobs")),
    )
    parser.add_argument("--qdrant-url", default=os.environ.get("QDRANT_URL", ""))
    parser.add_argument(
        "--qdrant-api-key", default=os.environ.get("QDRANT_API_KEY", "")
    )
    parser.add_argument("--tolerance", type=float, default=0.15)
    parser.add_argument(
        "--include-agent-led",
        action="store_true",
        help=(
            "include agent_led mode; requires the external host supplier unless "
            "SMOKE_DISABLE_AGENT_LED is truthy"
        ),
    )
    parser.add_argument(
        "--host-supplier-command",
        default=os.environ.get("SMOKE_HOST_SUPPLIER_COMMAND", ""),
    )
    parser.add_argument(
        "--host-supplier-identity",
        default=os.environ.get("SMOKE_HOST_SUPPLIER_IDENTITY", ""),
    )
    parser.add_argument(
        "--host-supplier-endpoint",
        default=os.environ.get("SMOKE_HOST_SUPPLIER_ENDPOINT", ""),
    )
    parser.add_argument(
        "--host-supplier-timeout",
        type=float,
        default=float(os.environ.get("SMOKE_HOST_SUPPLIER_TIMEOUT", "300")),
    )
    parser.add_argument("--manifest", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        checkout = verify_candidate_checkout(args.candidate_sha)
        if not args.dataset.is_file():
            raise SmokeGateError(f"benchmark dataset not found: {args.dataset}")
        if not args.database_url:
            raise SmokeGateError("DATABASE_URL or --database-url is required")
        if not args.qdrant_url:
            raise SmokeGateError("QDRANT_URL or --qdrant-url is required")
        if not 0.0 <= args.tolerance <= 1.0:
            raise SmokeGateError("tolerance must be between 0 and 1")
        execution_modes, agent_led_disabled_by_env = resolve_execution_modes(
            include_agent_led=args.include_agent_led
        )
        agent_led_effective = "agent_led" in execution_modes
        supplier: ExternalProcessHostArtifactSupplier | None = None
        supplier_probe: dict[str, Any] | None = None
        if agent_led_effective:
            command = shlex.split(args.host_supplier_command)
            autonomous_endpoints = tuple(
                value
                for value in (
                    os.environ.get("GENERATIVE_URL", ""),
                    os.environ.get("FIRECRAWL_LLM_LOCAL_BASE_URL", ""),
                    os.environ.get("FIRECRAWL_AUDIT_LOCAL_BASE_URL", ""),
                )
                if value
            )
            supplier = ExternalProcessHostArtifactSupplier(
                command,
                supplier_identity=args.host_supplier_identity,
                source_endpoint=args.host_supplier_endpoint,
                timeout_seconds=args.host_supplier_timeout,
                autonomous_endpoints=autonomous_endpoints,
            )
            supplier_probe = supplier.probe()
        args.campaign_dir.mkdir(parents=True, exist_ok=True)
        args.blob_root.mkdir(parents=True, exist_ok=True)

        preflight_ok, preflight_errors = _preflight_check(
            database_url=args.database_url,
            blob_root=args.blob_root,
            qdrant_url=args.qdrant_url,
            qdrant_api_key=args.qdrant_api_key,
            dataset_path=args.dataset,
            campaign_dir=args.campaign_dir,
            candidate_sha=args.candidate_sha,
        )
        if not preflight_ok:
            raise SmokeGateError(
                "complete real-stack preflight failed:\n- "
                + "\n- ".join(preflight_errors)
            )
        if args.dry_run:
            print("Smoke preflight passed; no campaigns executed.")
            return 0

        os.environ["CANDIDATE_SHA"] = args.candidate_sha
        os.environ["FIRECRAWL_RELEASE_DETERMINISTIC_FIXTURES"] = "1"
        loader = load_benchmark_dataset(args.dataset)
        objective_ids = {item.id for item in loader.objectives}
        if args.objective not in objective_ids:
            raise SmokeGateError(f"objective not found in dataset: {args.objective}")

        result_a, runner, dir_a, hashes_a = run_campaign(
            label="A",
            loader=loader,
            dataset_path=args.dataset,
            database_url=args.database_url,
            blob_root=args.blob_root,
            qdrant_url=args.qdrant_url,
            qdrant_api_key=args.qdrant_api_key,
            objective_id=args.objective,
            tolerance=args.tolerance,
            execution_modes=execution_modes,
            supplier=supplier,
            campaign_root=args.campaign_dir,
            candidate_sha=args.candidate_sha,
        )
        result_b, _runner_b, dir_b, hashes_b = run_campaign(
            label="B",
            loader=loader,
            dataset_path=args.dataset,
            database_url=args.database_url,
            blob_root=args.blob_root,
            qdrant_url=args.qdrant_url,
            qdrant_api_key=args.qdrant_api_key,
            objective_id=args.objective,
            tolerance=args.tolerance,
            execution_modes=execution_modes,
            supplier=supplier,
            campaign_root=args.campaign_dir,
            candidate_sha=args.candidate_sha,
        )
        comparison = runner.compare_campaigns(
            result_a, result_b, tolerance=args.tolerance
        )
        comparison_dir = args.campaign_dir / args.candidate_sha / "reproducibility"
        comparison_dir.mkdir(parents=True, exist_ok=True)
        comparison_hash = _write_json_atomic(
            comparison_dir / "comparison.json", comparison_to_dict(comparison)
        )
        passed = gate_passes(result_a, result_b, comparison)
        manifest = {
            "schema_version": "reduced-real-smoke-manifest-v1",
            **checkout,
            "dataset_path": str(args.dataset),
            "dataset_hash": _compute_file_hash(args.dataset),
            "objective_id": args.objective,
            "execution_modes": list(execution_modes),
            "agent_led": {
                "requested": args.include_agent_led,
                "disabled_by_env": agent_led_disabled_by_env,
                "effective": agent_led_effective,
            },
            "strict": True,
            "repetitions": 2,
            "host_supplier": (
                {
                    "identity": supplier.supplier_identity,
                    "source_endpoint": supplier.source_endpoint,
                    "command_sha256": supplier.command_sha256,
                    "probe": supplier_probe,
                }
                if supplier is not None
                else None
            ),
            "campaign_a": {
                "campaign_id": result_a.campaign_id,
                "recommendation": result_a.recommendation.outcome
                if result_a.recommendation
                else None,
                "artifact_dir": str(dir_a),
                **hashes_a,
            },
            "campaign_b": {
                "campaign_id": result_b.campaign_id,
                "recommendation": result_b.recommendation.outcome
                if result_b.recommendation
                else None,
                "artifact_dir": str(dir_b),
                **hashes_b,
            },
            "reproducibility": {
                "pass": comparison.all_within_tolerance,
                "comparison_hash": comparison_hash,
                "details": list(comparison.details),
            },
            "gate": "PASS" if passed else "FAIL",
            "full_campaign_authorized": passed,
        }
        manifest_path = args.manifest or (
            args.campaign_dir / args.candidate_sha / "manifest.json"
        )
        _write_json_atomic(manifest_path, manifest)
        print(
            json.dumps(
                {
                    "campaign_a": manifest["campaign_a"]["recommendation"],
                    "campaign_b": manifest["campaign_b"]["recommendation"],
                    "reproducibility": manifest["reproducibility"]["pass"],
                    "gate": manifest["gate"],
                    "manifest": str(manifest_path),
                },
                indent=2,
            )
        )
        return 0 if passed else 1
    except (SmokeGateError, subprocess.TimeoutExpired) as exc:
        print(f"SMOKE GATE FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
