"""Disposable-only destructive fault profile for canonical live validation."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from live_validation import SCRIPT_DIR, _json_dict, bounded, now_stamp

DESTRUCTIVE_MATRIX_CASES = (
    "persistent_postgres_target_refused",
    "persistent_qdrant_target_refused",
    "disposable_setup",
    "disposable_migrate",
    "disposable_positive_identity",
    "controlled_schema_fault",
    "schema_fault_fails_closed",
    "disposable_fault_teardown",
    "disposable_recovery_setup",
    "disposable_recovery_migrate",
    "disposable_recovery_ready",
    "disposable_final_teardown",
)


class DisposableDestructiveCampaign:
    """Inject one schema fault only after repository-owned disposable admission."""

    def __init__(
        self,
        args: Any,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.args = args
        self.runner = runner
        self.campaign_id = args.run_id or now_stamp()
        self.started = time.monotonic()
        self.cases: list[dict[str, Any]] = []
        self.service_env: dict[str, str] = {}
        self.implementation_head: str | None = None
        self._service_started = False
        self._blob_root = Path(
            tempfile.mkdtemp(prefix=f"{self.args.disposable_namespace}-blobs-")
        )

    def _record(
        self,
        name: str,
        *,
        category: str = "matrix",
        passed: bool | None,
        disposition: str,
        returncode: int,
        stdout: str = "",
        stderr: str = "",
        details: dict[str, Any] | None = None,
    ) -> None:
        self.cases.append(
            {
                "name": name,
                "category": category,
                "contract_result": (
                    "NOT_EVALUATED"
                    if passed is None
                    else ("PASS" if passed else "FAIL")
                ),
                "capability_result": "NOT_EVALUATED",
                "observed_disposition": disposition,
                "returncode": int(returncode),
                "stdout": bounded(stdout),
                "stderr": bounded(stderr),
                "details": details or {},
            }
        )

    def _call(
        self,
        name: str,
        command: list[str],
        *,
        expected_returncodes: tuple[int, ...] = (0,),
        env: dict[str, str] | None = None,
        category: str = "matrix",
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = self.runner(
                command,
                text=True,
                capture_output=True,
                env=env,
                timeout=self.args.case_timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            result = subprocess.CompletedProcess(
                command,
                124,
                stdout=str(exc.stdout or ""),
                stderr=f"TIMEOUT after {self.args.case_timeout}s\n{exc.stderr or ''}",
            )
        except OSError as exc:
            result = subprocess.CompletedProcess(
                command,
                127,
                stdout="",
                stderr=f"{type(exc).__name__}: {exc}",
            )
        passed = int(result.returncode) in expected_returncodes
        self._record(
            name,
            category=category,
            passed=passed,
            disposition=(
                "completed" if result.returncode == 0 else f"exit_{result.returncode}"
            ),
            returncode=int(result.returncode),
            stdout=result.stdout or "",
            stderr=result.stderr or "",
        )
        return result

    def _helper(self, command: str, *, pg_port: int, qdrant_port: int) -> list[str]:
        return [
            str(SCRIPT_DIR / "disposable-test-services"),
            "--namespace",
            self.args.disposable_namespace,
            "--pg-port",
            str(pg_port),
            "--qdrant-port",
            str(qdrant_port),
            "--format",
            "json",
            command,
        ]

    def _implementation_identity(self) -> bool:
        repo_root = SCRIPT_DIR.parent
        head = self.runner(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        status = self.runner(
            [
                "git",
                "-C",
                str(repo_root),
                "status",
                "--porcelain",
                "--untracked-files=no",
            ],
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        observed = (head.stdout or "").strip().lower()
        expected = (self.args.expected_head_sha or "").lower()
        passed = (
            head.returncode == 0
            and status.returncode == 0
            and not (status.stdout or "").strip()
            and bool(re.fullmatch(r"[0-9a-f]{40}", observed))
            and (not expected or observed == expected)
        )
        if passed:
            self.implementation_head = observed
        self._record(
            "implementation_identity",
            category="plumbing",
            passed=passed,
            disposition=observed or "unresolved",
            returncode=0 if passed else 1,
            stderr=(head.stderr or "") + "\n" + (status.stderr or ""),
            details={
                "expected_head_sha": expected or None,
                "observed_head_sha": observed or None,
                "tracked_worktree_clean": not bool((status.stdout or "").strip()),
            },
        )
        return passed

    def _start(self, name: str = "disposable_setup") -> bool:
        # Reserve cleanup authority before helper `up`: a timeout or partial
        # setup may create owned containers before a receipt is returned.
        self._service_started = True
        result = self._call(
            name,
            self._helper(
                "up",
                pg_port=self.args.disposable_pg_port,
                qdrant_port=self.args.disposable_qdrant_port,
            ),
        )
        if result.returncode != 0:
            return False
        payload = _json_dict(result.stdout or "")
        environment = payload.get("environment") if payload else None
        if (
            not payload
            or payload.get("schema_version") != "firecrawl-disposable-services-v1"
            or payload.get("namespace") != self.args.disposable_namespace
            or not isinstance(environment, dict)
        ):
            self.cases[-1]["contract_result"] = "FAIL"
            self.cases[-1]["stderr"] = bounded(
                f"{self.cases[-1]['stderr']}\ninvalid disposable service receipt"
            )
            return False

        database_url = str(environment.get("RESEARCH_STORE_TEST_DATABASE_URL") or "")
        qdrant_url = str(environment.get("RESEARCH_STORE_TEST_QDRANT_URL") or "")
        database = urlsplit(database_url)
        qdrant = urlsplit(qdrant_url)
        postgres_receipt = payload.get("postgres")
        qdrant_receipt = payload.get("qdrant")
        expected_db = f"{self.args.disposable_namespace.replace('-', '_')}_test"
        identity_ok = (
            isinstance(postgres_receipt, dict)
            and isinstance(qdrant_receipt, dict)
            and postgres_receipt.get("database") == expected_db
            and int(postgres_receipt.get("port") or -1) == self.args.disposable_pg_port
            and int(qdrant_receipt.get("port") or -1)
            == self.args.disposable_qdrant_port
            and database.hostname == "127.0.0.1"
            and database.port == self.args.disposable_pg_port
            and database.path == f"/{expected_db}"
            and str(environment.get("RESEARCH_STORE_TEST_ALLOW_RESET") or "")
            == expected_db
            and qdrant.hostname == "127.0.0.1"
            and qdrant.port == self.args.disposable_qdrant_port
            and str(environment.get("QDRANT_URL") or "") == qdrant_url
            and str(environment.get("RESEARCH_STORE_TEST_QDRANT_ALLOW_RESET") or "")
            == qdrant_url
        )
        if not identity_ok:
            self.cases[-1]["contract_result"] = "FAIL"
            self.cases[-1]["stderr"] = bounded(
                f"{self.cases[-1]['stderr']}\ndisposable identity mismatch"
            )
            return False

        self.service_env = {
            **os.environ,
            **{str(key): str(value) for key, value in environment.items()},
            "DATABASE_URL": database_url,
            "BLOB_ROOT": str(self._blob_root),
            "FIRECRAWL_RESEARCH_AUTO_ENV": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        return True

    def _down(self, name: str) -> bool:
        result = self._call(
            name,
            self._helper(
                "down",
                pg_port=self.args.disposable_pg_port,
                qdrant_port=self.args.disposable_qdrant_port,
            ),
        )
        if result.returncode == 0:
            self._service_started = False
            return True
        return False

    def _inject_schema_fault(self) -> None:
        import psycopg

        with psycopg.connect(self.service_env["DATABASE_URL"]) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "ALTER TABLE research_runs RENAME TO research_runs_faulted"
                )
            connection.commit()

    def execute(self) -> int:
        fault_injected = False
        try:
            if self._implementation_identity():
                # Exercise both repository-known persistent datastore guards
                # through the helper's non-mutating `env` admission path.
                protected_postgres = self._call(
                    "persistent_postgres_target_refused",
                    self._helper(
                        "env",
                        pg_port=55432,
                        qdrant_port=self.args.disposable_qdrant_port,
                    ),
                    expected_returncodes=(1,),
                )
                protected_qdrant = self._call(
                    "persistent_qdrant_target_refused",
                    self._helper(
                        "env",
                        pg_port=self.args.disposable_pg_port,
                        qdrant_port=6333,
                    ),
                    expected_returncodes=(1,),
                )
                protected_targets_refused = (
                    protected_postgres.returncode == 1
                    and protected_qdrant.returncode == 1
                )
                if protected_targets_refused and self._start():
                    migrated = self._call(
                        "disposable_migrate",
                        [str(SCRIPT_DIR / "research-db"), "migrate"],
                        env=self.service_env,
                    )
                    ready = self._call(
                        "disposable_positive_identity",
                        [str(SCRIPT_DIR / "research-db"), "ingest-ready"],
                        env=self.service_env,
                    )
                    if migrated.returncode == 0 and ready.returncode == 0:
                        try:
                            self._inject_schema_fault()
                        except Exception as exc:  # noqa: BLE001
                            self._record(
                                "controlled_schema_fault",
                                passed=False,
                                disposition="fault_injection_failed",
                                returncode=1,
                                stderr=f"{type(exc).__name__}: {exc}",
                            )
                        else:
                            fault_injected = True
                            self._record(
                                "controlled_schema_fault",
                                passed=True,
                                disposition=(
                                    "research_runs_renamed_on_disposable_postgres"
                                ),
                                returncode=0,
                            )
                            self._call(
                                "schema_fault_fails_closed",
                                [str(SCRIPT_DIR / "research-db"), "ingest-ready"],
                                expected_returncodes=(1,),
                                env=self.service_env,
                            )

                    fault_teardown_ok = True
                    if self._service_started:
                        fault_teardown_ok = self._down("disposable_fault_teardown")

                    # Recovery is a fresh helper-owned lifecycle and cannot
                    # begin until teardown of the faulted namespace is proven.
                    if (
                        fault_injected
                        and fault_teardown_ok
                        and self._start("disposable_recovery_setup")
                    ):
                        self._call(
                            "disposable_recovery_migrate",
                            [str(SCRIPT_DIR / "research-db"), "migrate"],
                            env=self.service_env,
                        )
                        self._call(
                            "disposable_recovery_ready",
                            [str(SCRIPT_DIR / "research-db"), "ingest-ready"],
                            env=self.service_env,
                        )
        except Exception as exc:  # noqa: BLE001
            self._record(
                "destructive_execution_error",
                category="plumbing",
                passed=False,
                disposition="execution_failed",
                returncode=2,
                stderr=f"{type(exc).__name__}: {exc}",
            )
        finally:
            if self._service_started:
                self._down("disposable_final_teardown")
            try:
                shutil.rmtree(self._blob_root)
            except FileNotFoundError:
                pass
            except OSError as exc:
                self._record(
                    "disposable_blob_cleanup",
                    category="plumbing",
                    passed=False,
                    disposition="cleanup_failed",
                    returncode=1,
                    stderr=f"{type(exc).__name__}: {exc}",
                )
            else:
                self._record(
                    "disposable_blob_cleanup",
                    category="plumbing",
                    passed=True,
                    disposition="removed",
                    returncode=0,
                )
        return self.finish()

    def _materialize_unexecuted_matrix_cases(self) -> None:
        observed = {case["name"] for case in self.cases if case["category"] == "matrix"}
        for name in DESTRUCTIVE_MATRIX_CASES:
            if name in observed:
                continue
            self._record(
                name,
                passed=None,
                disposition="not_run",
                returncode=0,
                details={
                    "reason": "case was declared by the destructive profile but execution did not reach it"
                },
            )

    def _report_markdown(self, manifest: dict[str, Any]) -> str:
        lines = [
            "# Firecrawl disposable destructive validation",
            "",
            f"- Implementation HEAD: `{manifest['implementation_head_sha']}`",
            f"- Host evidence: `{manifest['host_evidence']}`",
            f"- Cleanup: `{manifest['cleanup']['result']}`",
            "",
            "## Cases",
            "",
            "| Case | Category | Contract | Capability | Disposition |",
            "|---|---|---|---|---|",
        ]
        lines.extend(
            f"| {case['name']} | {case['category']} | {case['contract_result']} | "
            f"{case['capability_result']} | {case['observed_disposition']} |"
            for case in manifest["cases"]
        )
        lines += [
            "",
            "## Accounting",
            "",
            "```json",
            json.dumps(manifest["accounting"], indent=2, sort_keys=True),
            "```",
            "",
            manifest["host_evidence_semantics"],
        ]
        return "\n".join(lines) + "\n"

    def finish(self) -> int:
        self._materialize_unexecuted_matrix_cases()
        failed = any(case["contract_result"] != "PASS" for case in self.cases)
        teardown_cases = [
            case
            for case in self.cases
            if case["name"]
            in {"disposable_fault_teardown", "disposable_final_teardown"}
            and case["contract_result"] != "NOT_EVALUATED"
        ]
        cleanup_pass = (
            bool(teardown_cases)
            and all(case["contract_result"] == "PASS" for case in teardown_cases)
            and not self._service_started
        )
        host_pass = not failed and cleanup_pass
        manifest = {
            "schema_version": "live-validation-v2",
            "campaign_id": self.campaign_id,
            "profile": "destructive",
            "implementation_head_sha": self.implementation_head,
            "duration_seconds": round(time.monotonic() - self.started, 2),
            "operations": {
                "count": 0,
                "max": self.args.max_operations,
                "calls": [],
            },
            "retry_policy": {
                "max_attempts_per_case": 1,
                "automatic_retry": False,
                "finalization_cleanup_attempts": 1,
                "note": (
                    "No matrix case is retried. Finalization may make one independent "
                    "helper-owned teardown attempt after an earlier teardown failure."
                ),
            },
            "cases": self.cases,
            "accounting": {
                "declared_matrix_cases": len(DESTRUCTIVE_MATRIX_CASES),
                "executed_matrix_cases": sum(
                    case["category"] == "matrix"
                    and case["contract_result"] != "NOT_EVALUATED"
                    for case in self.cases
                ),
                "plumbing_operations": sum(
                    case["category"] == "plumbing" for case in self.cases
                ),
                "not_run_cases": sum(
                    case["category"] == "matrix"
                    and case["contract_result"] == "NOT_EVALUATED"
                    for case in self.cases
                ),
                "not_run_plumbing_operations": sum(
                    case["category"] == "plumbing"
                    and case["contract_result"] == "NOT_EVALUATED"
                    for case in self.cases
                ),
                "failed_cases": sum(
                    case["contract_result"] != "PASS" for case in self.cases
                ),
                "passed_contract_cases": sum(
                    case["contract_result"] == "PASS" for case in self.cases
                ),
                "successful_capability_cases": 0,
            },
            "quality_metrics": [],
            "quality_result": "NOT_EVALUATED",
            "cleanup": {
                "result": "PASS" if cleanup_pass else "FAIL",
                "owned_run_ids": [],
            },
            "host_evidence": "PASS" if host_pass else "FAIL",
            "host_evidence_semantics": (
                "destructive PASS requires protected persistent-target refusal, "
                "repository-sanctioned disposable identity, one controlled schema "
                "fault with fail-closed observation, fresh-service recovery, and "
                "verified helper-owned teardown."
            ),
        }
        if self.args.artifact_root:
            destination = Path(self.args.artifact_root) / self.campaign_id
            destination.mkdir(parents=True, exist_ok=False)
            (destination / "manifest.json").write_text(
                json.dumps(manifest, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            (destination / "report.md").write_text(
                self._report_markdown(manifest),
                encoding="utf-8",
            )
            print(f"Artifacts: {destination}")
        else:
            print(json.dumps(manifest, indent=2, sort_keys=True))
        return 0 if host_pass else 1


__all__ = ["DisposableDestructiveCampaign"]
