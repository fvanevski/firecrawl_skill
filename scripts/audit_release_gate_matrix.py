"""Validate and execute the ARC-17 machine-readable release gate matrix."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

MATRIX_SCHEMA = "audit-remediation-release-gates-v1"
EVIDENCE_SCHEMA = "audit-remediation-gate-evidence-v1"
REQUIRED_GATE_IDS = (
    "unit",
    "formatting",
    "lint",
    "type",
    "full_integration",
    "forward_migration",
    "fresh_database_migration",
    "rollback_compatibility",
    "audited_concurrency_reproduction",
    "indexing_waits_for_final_32",
    "restart_idempotent_resume",
    "concurrent_finish_resume",
    "curated_four_article",
    "autonomous_ranking_budget",
    "empty_provider_provenance",
    "bounded_empty_extraction",
    "ingestion_batch_truth",
    "authoritative_synthesis",
    "verifier_zero_object",
    "doctor_separation",
    "offline_export_late32",
    "postgres_qdrant_reconciliation",
    "secret_scan",
    "credentialed_release_campaign",
)
ALLOWED_PHASES = {"ci", "disposable", "credentialed"}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError("gate matrix must be a JSON object")
    return value


def validate_matrix(matrix: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if matrix.get("schema_version") != MATRIX_SCHEMA:
        errors.append(f"schema_version must be {MATRIX_SCHEMA}")
    gates = matrix.get("gates")
    if not isinstance(gates, list):
        return [*errors, "gates must be a list"]
    ids = [gate.get("id") for gate in gates if isinstance(gate, dict)]
    if tuple(ids) != REQUIRED_GATE_IDS:
        errors.append("gate IDs/order do not match the mandatory ARC-17 contract")
    if len(set(ids)) != len(ids):
        errors.append("gate IDs must be unique")
    for index, gate in enumerate(gates):
        if not isinstance(gate, dict):
            errors.append(f"gate[{index}] is not an object")
            continue
        gate_id = gate.get("id", f"index-{index}")
        for field in ("title", "command", "expected_evidence", "artifact"):
            if not isinstance(gate.get(field), str) or not gate[field].strip():
                errors.append(f"{gate_id}: {field} must be nonempty")
        if gate.get("execution_phase") not in ALLOWED_PHASES:
            errors.append(f"{gate_id}: invalid execution_phase")
        if gate.get("blocking") is not True:
            errors.append(f"{gate_id}: all ARC-17 gates must be blocking")
        if gate.get("source_result") != "pending":
            errors.append(f"{gate_id}: source matrix may only declare pending")
        timeout = gate.get("timeout_seconds", 900)
        if not isinstance(timeout, int) or timeout <= 0 or timeout > 3600:
            errors.append(f"{gate_id}: timeout_seconds must be 1..3600")
    return errors


def _git(*args: str, cwd: Path) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False, timeout=30
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip())
    return completed.stdout.strip()


def _service_versions() -> dict[str, Any]:
    result: dict[str, Any] = {}
    dsn = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL")
    if dsn:
        try:
            import psycopg

            with psycopg.connect(dsn) as connection, connection.cursor() as cursor:
                cursor.execute("SHOW server_version")
                result["postgresql"] = cursor.fetchone()[0]
                cursor.execute("SELECT to_regclass('public.alembic_version')")
                if cursor.fetchone()[0]:
                    cursor.execute("SELECT version_num FROM alembic_version")
                    row = cursor.fetchone()
                    result["alembic_revision"] = row[0] if row else None
        except Exception as exc:  # noqa: BLE001
            result["postgresql_error"] = f"{type(exc).__name__}: {exc}"
    qdrant_url = os.environ.get("QDRANT_URL", "").rstrip("/")
    if qdrant_url:
        try:
            with urllib.request.urlopen(qdrant_url, timeout=5) as response:
                body = json.loads(response.read().decode("utf-8"))
            result["qdrant"] = body.get("version") or body.get("title") or body
        except Exception as exc:  # noqa: BLE001
            result["qdrant_error"] = f"{type(exc).__name__}: {exc}"
    return result


def execute_phase(
    matrix: dict[str, Any],
    *,
    phase: str,
    repo: Path,
    output_dir: Path,
    candidate_sha: str | None,
) -> dict[str, Any]:
    errors = validate_matrix(matrix)
    if errors:
        raise ValueError("; ".join(errors))
    if phase not in ALLOWED_PHASES:
        raise ValueError(f"unknown phase: {phase}")

    repo = repo.resolve()
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    head = _git("rev-parse", "HEAD", cwd=repo)
    tree = _git("rev-parse", "HEAD^{tree}", cwd=repo)
    dirty = _git("status", "--porcelain=v1", "--untracked-files=all", cwd=repo)
    identity_errors: list[str] = []
    if candidate_sha and head != candidate_sha:
        identity_errors.append(f"checkout {head} != candidate {candidate_sha}")
    if dirty:
        identity_errors.append("working tree is not clean")

    environment = os.environ.copy()
    environment["AUDIT_GATE_DIR"] = str(output_dir)
    results: list[dict[str, Any]] = []
    for gate in matrix["gates"]:
        if gate["execution_phase"] != phase:
            continue
        gate_id = gate["id"]
        stdout_path = output_dir / f"{gate_id}.stdout"
        stderr_path = output_dir / f"{gate_id}.stderr"
        started = time.monotonic()
        timed_out = False
        try:
            completed = subprocess.run(
                ["bash", "-lc", gate["command"]],
                cwd=repo,
                env=environment,
                capture_output=True,
                check=False,
                timeout=int(gate.get("timeout_seconds", 900)),
            )
            stdout = completed.stdout
            stderr = completed.stderr
            exit_code = completed.returncode
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            stdout = exc.stdout or b""
            stderr = exc.stderr or b""
            exit_code = 124
        duration = round(time.monotonic() - started, 6)
        stdout_path.write_bytes(stdout)
        stderr_path.write_bytes(stderr)
        results.append(
            {
                "id": gate_id,
                "title": gate["title"],
                "command": gate["command"],
                "expected_evidence": gate["expected_evidence"],
                "artifact": gate["artifact"],
                "result": "pass" if exit_code == 0 else "fail",
                "exit_code": exit_code,
                "timed_out": timed_out,
                "duration_seconds": duration,
                "stdout": {
                    "path": str(stdout_path.relative_to(output_dir)),
                    "bytes": len(stdout),
                    "sha256": _sha256(stdout),
                },
                "stderr": {
                    "path": str(stderr_path.relative_to(output_dir)),
                    "bytes": len(stderr),
                    "sha256": _sha256(stderr),
                },
            }
        )

    evidence = {
        "schema_version": EVIDENCE_SCHEMA,
        "matrix_schema_version": MATRIX_SCHEMA,
        "issue": matrix.get("issue"),
        "epic": matrix.get("epic"),
        "phase": phase,
        "candidate_sha": candidate_sha or head,
        "checkout_sha": head,
        "tree_hash": tree,
        "working_tree_clean": not bool(dirty),
        "identity_errors": identity_errors,
        "environment": {
            "python": sys.version,
            "platform": platform.platform(),
            "services": _service_versions(),
        },
        "gates": results,
        "summary": {
            "selected": len(results),
            "passed": sum(item["result"] == "pass" for item in results),
            "failed": sum(item["result"] == "fail" for item in results),
        },
    }
    evidence["gate"] = (
        "PASS"
        if (
            not identity_errors
            and results
            and all(item["result"] == "pass" for item in results)
        )
        else "FAIL"
    )
    evidence_path = output_dir / "gate-evidence.json"
    evidence_path.write_text(
        json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    evidence["evidence_sha256"] = _sha256(evidence_path.read_bytes())
    return evidence


def _default_matrix() -> Path:
    return Path(__file__).parents[1] / "references" / "audit-remediation-release-gates.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate")
    validate.add_argument("--matrix", type=Path, default=_default_matrix())
    run = subparsers.add_parser("run")
    run.add_argument("--matrix", type=Path, default=_default_matrix())
    run.add_argument("--phase", choices=sorted(ALLOWED_PHASES), required=True)
    run.add_argument("--repo", type=Path, default=Path("."))
    run.add_argument("--output-dir", type=Path, required=True)
    run.add_argument("--candidate-sha")
    args = parser.parse_args(argv)

    matrix = _load(args.matrix)
    errors = validate_matrix(matrix)
    if args.command == "validate":
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        if not errors:
            print(f"Gate matrix PASS: {len(matrix['gates'])} mandatory gates")
        return 0 if not errors else 1

    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    evidence = execute_phase(
        matrix,
        phase=args.phase,
        repo=args.repo,
        output_dir=args.output_dir,
        candidate_sha=args.candidate_sha,
    )
    print(
        json.dumps(
            {
                "gate": evidence["gate"],
                "phase": evidence["phase"],
                "candidate_sha": evidence["candidate_sha"],
                "passed": evidence["summary"]["passed"],
                "failed": evidence["summary"]["failed"],
                "evidence_sha256": evidence["evidence_sha256"],
            },
            sort_keys=True,
        )
    )
    return 0 if evidence["gate"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
