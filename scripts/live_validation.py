"""Canonical live smoke/fault validation for current Firecrawl CLI surfaces."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID

SCRIPT_DIR = Path(__file__).resolve().parent
TERMINAL_STATES = frozenset({"completed", "partial", "failed", "cancelled"})
RETIRED_SMART_OPTIONS = (
    "--dry-run",
    "--stop-after-state",
    "--research-run-id",
    "--max-adaptive-cycles",
)
PROFILE_OPERATION_CAPS = {
    "focused": 40,
    "failure-path": 20,
    "full": 100,
    "destructive": 10,
}
_MAX_OUTPUT_CHARS = 4_000

BENCHMARKS = {
    "simple": "current Firecrawl CLI npm package and installation command",
    "academic": "methodological naturalism cosmology burden of proof evidence objections",
    "termux": "Android Termux Vulkan Turnip Mesa Zink acceleration compatibility and failure modes",
}


def now_stamp() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y%m%d_%H%M%S_%f")


def bounded(value: str | None, limit: int = _MAX_OUTPUT_CHARS) -> str:
    text = value or ""
    return text if len(text) <= limit else text[: limit - 15] + "...[truncated]"


def _json_dict(text: str) -> dict[str, Any] | None:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _run_id_from_payload(payload: dict[str, Any] | None) -> str | None:
    if not payload:
        return None
    value = payload.get("research_run_id") or payload.get("run_id")
    text = str(value or "")
    return text if re.fullmatch(r"fr_[0-9a-f]{32}", text) else None


def _observed_disposition(payload: dict[str, Any] | None, returncode: int) -> str:
    if payload:
        if payload.get("failure_stage"):
            return f"{payload['failure_stage']}_failure"
        if payload.get("disposition"):
            return str(payload["disposition"])
        if payload.get("status"):
            return str(payload["status"])
    if returncode == 2:
        return "preflight_rejection"
    if returncode == 5:
        return "extraction_failure"
    return "completed" if returncode == 0 else f"exit_{returncode}"


def _fresearch_contract(payload: dict[str, Any] | None, returncode: int) -> bool:
    if payload is None:
        return False
    schema = payload.get("schema_version")
    disposition = str(payload.get("disposition") or "")
    if schema not in {"workflow-directive-v2", "research-result-v3"}:
        return False
    if disposition in {"failed", "cancelled"}:
        return returncode == 1
    if disposition == "blocked" and (
        payload.get("terminal") is True
        or payload.get("lifecycle_state") in TERMINAL_STATES
    ):
        return returncode == 1
    if disposition in {"blocked", "operator_action_required", "continue_automatic"}:
        return returncode == 75
    if disposition in {"terminal_completed", "terminal_partial"}:
        return returncode == 0
    return False


class AuthoritativeInspector:
    """Minimal PostgreSQL/Qdrant evidence reader used by the live validator."""

    def __init__(
        self,
        database_url: str,
        *,
        qdrant_url: str | None = None,
        qdrant_api_key: str | None = None,
        blob_root: str | Path | None = None,
    ) -> None:
        from firecrawl_skill.research_store.config import StoreConfig

        self.database_url = database_url
        self.qdrant_url = qdrant_url
        self.qdrant_api_key = qdrant_api_key
        self.blob_root = Path(blob_root or StoreConfig.from_env().blob_root)

    def _connect(self):
        import psycopg

        return psycopg.connect(self.database_url)

    def run_state(self, external_run_id: str) -> str:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT state FROM research_runs WHERE external_run_id=%s",
                (external_run_id,),
            )
            row = cursor.fetchone()
        if row is None:
            raise RuntimeError(f"research run not found: {external_run_id}")
        return str(row[0])

    def list_run_ids(self) -> set[str]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT external_run_id FROM research_runs ORDER BY external_run_id")
            return {str(row[0]) for row in cursor.fetchall() if row[0]}

    def run_ids_for_objective(self, objective: str) -> set[str]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT external_run_id FROM research_runs WHERE objective=%s "
                "ORDER BY external_run_id",
                (objective,),
            )
            return {str(row[0]) for row in cursor.fetchall() if row[0]}

    def table_counts(self) -> dict[str, int]:
        names = (
            "research_runs",
            "research_invocations",
            "research_specs",
            "search_plans",
            "search_responses",
            "search_candidates",
            "extraction_attempts",
            "asset_snapshots",
            "documents",
            "chunks",
            "research_events",
            "index_jobs",
        )
        sql = "SELECT " + ",".join(f"(SELECT count(*) FROM {name})" for name in names)
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(sql)
            row = cursor.fetchone()
        if row is None:
            raise RuntimeError("authoritative table-count query returned no row")
        return {name: int(value) for name, value in zip(names, row, strict=True)}

    def probe_qdrant_alias(self) -> dict[str, Any]:
        from firecrawl_skill.research_store.config import StoreConfig
        from firecrawl_skill.research_store.retrieval.projection.qdrant import QdrantIndex

        config = StoreConfig.from_env()
        url = self.qdrant_url or config.qdrant_url
        api_key = self.qdrant_api_key if self.qdrant_api_key is not None else config.qdrant_api_key
        if not url:
            raise RuntimeError("QDRANT_URL is required")
        index = QdrantIndex(url, api_key, config.qdrant_alias, config.embedding_dimension)
        aliases = index.list_aliases()
        target = aliases.get(config.qdrant_alias)
        if target != config.physical_collection:
            raise RuntimeError(
                f"active alias {config.qdrant_alias!r} targets {target!r}, "
                f"expected {config.physical_collection!r}"
            )
        schema = index.for_collection(target, config.embedding_dimension, "Cosine").inspect_schema()
        if not schema.get("exists") or not schema.get("compatible"):
            raise RuntimeError(f"active Qdrant schema is incompatible: {schema!r}")
        return {
            "alias": config.qdrant_alias,
            "collection": target,
            "dimension": config.embedding_dimension,
            "compatible": True,
        }


    def _run_row(self, external_run_id: str) -> tuple[UUID, str]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT id,state FROM research_runs WHERE external_run_id=%s",
                (external_run_id,),
            )
            row = cursor.fetchone()
        if row is None:
            raise RuntimeError(f"research run not found: {external_run_id}")
        return UUID(str(row[0])), str(row[1])

    def _blob_integrity(self, digests: list[str]) -> dict[str, Any]:
        from firecrawl_skill.research_store.blob import ContentAddressedBlobStore

        store = ContentAddressedBlobStore(self.blob_root)
        unique = sorted({digest for digest in digests if digest})
        verified = [digest for digest in unique if store.verify(digest)]
        return {
            "expected": len(unique),
            "verified": len(verified),
            "missing_or_invalid": sorted(set(unique) - set(verified)),
            "complete": bool(unique) and len(verified) == len(unique),
        }

    def _projection_metrics(self, chunk_ids: list[UUID]) -> dict[str, Any]:
        alias = self.probe_qdrant_alias()
        if not chunk_ids:
            return {
                **alias,
                "expected_points": 0,
                "returned_points": 0,
                "coverage": 0.0,
            }
        from firecrawl_skill.research_store.retrieval.projection.qdrant import QdrantIndex

        index = QdrantIndex(
            self.qdrant_url or os.environ.get("QDRANT_URL", "http://localhost:6333"),
            self.qdrant_api_key if self.qdrant_api_key is not None else os.environ.get("QDRANT_API_KEY", ""),
            alias["alias"],
            int(alias["dimension"]),
        )
        returned = index.retrieve(chunk_ids)
        returned_ids = {str(item.get("id")) for item in returned}
        expected_ids = {str(item) for item in chunk_ids}
        matched = len(returned_ids & expected_ids)
        return {
            **alias,
            "expected_points": len(expected_ids),
            "returned_points": matched,
            "coverage": matched / len(expected_ids),
        }

    def run_metrics(
        self,
        external_run_id: str,
        *,
        require_planning: bool,
        require_corpus: bool,
        require_terminal: bool,
    ) -> dict[str, Any]:
        run_id, state = self._run_row(external_run_id)
        with self._connect() as connection, connection.cursor() as cursor:
            scalar_queries = {
                "spec_count": "SELECT count(*) FROM research_specs WHERE run_id=%s",
                "budget_count": "SELECT count(*) FROM research_budget_snapshots WHERE run_id=%s",
                "plan_count": "SELECT count(*) FROM search_plans WHERE run_id=%s",
                "semantic_call_count": "SELECT count(*) FROM semantic_calls WHERE run_id=%s",
                "search_response_count": (
                    "SELECT count(*) FROM search_responses "
                    "WHERE run_id=%s AND backend <> 'orchestrator'"
                ),
                "candidate_count": "SELECT count(*) FROM search_candidates WHERE run_id=%s",
                "extraction_count": "SELECT count(*) FROM extraction_attempts WHERE run_id=%s",
            }
            scalars: dict[str, int] = {}
            for name, statement in scalar_queries.items():
                cursor.execute(statement, (run_id,))
                row = cursor.fetchone()
                if row is None:
                    raise RuntimeError(f"authoritative metric query returned no row: {name}")
                scalars[name] = int(row[0])

            cursor.execute(
                """SELECT DISTINCT s.content_sha256
                   FROM asset_snapshots s
                   JOIN extraction_attempts ea ON ea.id=s.extraction_attempt_id
                   WHERE ea.run_id=%s""",
                (run_id,),
            )
            blob_digests = [str(row[0]) for row in cursor.fetchall() if row[0]]

            cursor.execute(
                """SELECT DISTINCT d.id
                   WHERE ea.run_id=%s""",
                (run_id,),
            )
            document_ids = [UUID(str(row[0])) for row in cursor.fetchall()]

            cursor.execute(
                """SELECT DISTINCT ch.id
                   FROM chunks ch
                   JOIN documents d ON d.id=ch.document_id
                   LEFT JOIN asset_snapshots s ON s.id=d.snapshot_id
                   LEFT JOIN extraction_attempts ea
                     ON ea.id=coalesce(d.extraction_attempt_id,s.extraction_attempt_id)
                   WHERE ea.run_id=%s ORDER BY ch.id""",
                (run_id,),
            )
            chunk_ids = [UUID(str(row[0])) for row in cursor.fetchall()]

            job_rows: list[tuple[Any, ...]] = []
            if chunk_ids:
                cursor.execute(
                    """SELECT status,count(*),sum(attempt_count),
                              count(started_at),count(completed_at)
                       FROM index_jobs
                       WHERE entity_type='chunk' AND entity_id=ANY(%s)
                       GROUP BY status ORDER BY status""",
                    (chunk_ids,),
                )
                job_rows = list(cursor.fetchall())

        blob_integrity = (
            self._blob_integrity(blob_digests)
            if blob_digests
            else {"expected": 0, "verified": 0, "missing_or_invalid": [], "complete": False}
        )
        projection = self._projection_metrics(chunk_ids)
        job_counts = {str(row[0]): int(row[1]) for row in job_rows}
        total_jobs = sum(job_counts.values())
        complete_jobs = job_counts.get("complete", 0)
        worker_complete = (
            bool(chunk_ids)
            and total_jobs > 0
            and total_jobs == complete_jobs
            and all(int(row[2] or 0) >= int(row[1]) for row in job_rows)
            and all(int(row[3]) == int(row[1]) for row in job_rows)
            and all(int(row[4]) == int(row[1]) for row in job_rows)
        )
        checks = {
            "terminal": state in TERMINAL_STATES if require_terminal else True,
            "planning": (
                scalars["spec_count"] == 1
                and scalars["budget_count"] == 1
                and scalars["plan_count"] == 1
                and scalars["semantic_call_count"] > 0
                if require_planning
                else True
            ),
            "search": scalars["search_response_count"] > 0 and scalars["candidate_count"] > 0,
            "corpus": (
                scalars["extraction_count"] > 0
                and bool(blob_digests)
                and bool(document_ids)
                and bool(chunk_ids)
                if require_corpus
                else True
            ),
            "blob_integrity": blob_integrity["complete"] if require_corpus else True,
            "worker_complete": worker_complete if require_corpus else True,
            "qdrant_coverage": projection["coverage"] == 1.0 if require_corpus else True,
        }
        return {
            "external_run_id": external_run_id,
            "run_id": str(run_id),
            "state": state,
            **scalars,
            "snapshot_count": len(blob_digests),
            "document_count": len(document_ids),
            "chunk_count": len(chunk_ids),
            "blob_integrity": blob_integrity,
            "index_job_counts": job_counts,
            "projection": projection,
            "checks": checks,
            "pass": all(checks.values()),
        }

    def wait_for_worker(
        self,
        external_run_id: str,
        *,
        require_planning: bool,
        require_corpus: bool,
        require_terminal: bool,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        last: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            last = self.run_metrics(
                external_run_id,
                require_planning=require_planning,
                require_corpus=require_corpus,
                require_terminal=require_terminal,
            )
            if last["pass"]:
                return last
            time.sleep(0.5)
        return last or self.run_metrics(
            external_run_id,
            require_planning=require_planning,
            require_corpus=require_corpus,
            require_terminal=require_terminal,
        )


class Campaign:
    """Run bounded persistent-service smoke/fault cases through public CLIs only."""

    def __init__(
        self,
        args: argparse.Namespace,
        *,
        inspector: AuthoritativeInspector | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        real_cli: str | None = None,
        work_root: Path | None = None,
    ) -> None:
        self.args = args
        self.campaign_id = args.run_id or now_stamp()
        self.runner = runner
        self.inspector = inspector or AuthoritativeInspector(
            args.database_url,
            qdrant_url=args.qdrant_url,
            qdrant_api_key=args.qdrant_api_key,
            blob_root=args.blob_root,
        )
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        if work_root is None:
            self._temporary = tempfile.TemporaryDirectory(prefix="firecrawl-live-validation-")
            work_root = Path(self._temporary.name)
        self.work_root = Path(work_root)
        self.monitored_tmp = self.work_root / "tmp"
        self.proxy_dir = self.work_root / "proxy"
        self.monitored_tmp.mkdir(parents=True, exist_ok=True)
        self.proxy_dir.mkdir(parents=True, exist_ok=True)
        self.real_cli = real_cli or shutil.which("firecrawl")
        self.counter = self.work_root / "operations.json"
        self.counter.write_text(
            json.dumps({"count": 0, "max": args.max_operations, "calls": []}),
            encoding="utf-8",
        )
        self.cases: list[dict[str, Any]] = []
        self.owned_runs: dict[str, dict[str, Any]] = {}
        self.preexisting_run_ids: set[str] = set()
        self.run_baseline_captured = False
        self.discovery_objectives: dict[str, str] = {}
        self.implementation_head: str | None = None
        self.started = time.monotonic()
        self._write_proxy()
        self.env = os.environ.copy()
        self.env.update(
            {
                "PATH": f"{self.proxy_dir}{os.pathsep}{self.env.get('PATH', '')}",
                "REAL_FIRECRAWL": self.real_cli or "",
                "FC_OPERATION_COUNTER": str(self.counter),
                "FC_OPERATION_MAX": str(args.max_operations),
                "FIRECRAWL_API_URL": args.api_url.rstrip("/"),
                "DATABASE_URL": args.database_url,
                "BLOB_ROOT": str(args.blob_root),
                "TMPDIR": str(self.monitored_tmp),
                "PYTHONDONTWRITEBYTECODE": "1",
                "FIRECRAWL_RESEARCH_AUTO_ENV": "0",
            }
        )
        if args.qdrant_url:
            self.env["QDRANT_URL"] = args.qdrant_url
        if args.qdrant_api_key is not None:
            self.env["QDRANT_API_KEY"] = args.qdrant_api_key

    def close(self) -> None:
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None

    def _write_proxy(self) -> None:
        proxy = self.proxy_dir / "firecrawl"
        proxy.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import fcntl
                import json
                import os
                import sys

                counter_path = os.environ["FC_OPERATION_COUNTER"]
                maximum = int(os.environ["FC_OPERATION_MAX"])
                with open(counter_path, "r+", encoding="utf-8") as handle:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                    data = json.load(handle)
                    if int(data.get("count", 0)) >= maximum:
                        print(f"Firecrawl operation cap exhausted ({maximum})", file=sys.stderr)
                        raise SystemExit(78)
                    data["count"] = int(data.get("count", 0)) + 1
                    data.setdefault("calls", []).append(sys.argv[1:])
                    handle.seek(0)
                    handle.truncate()
                    json.dump(data, handle, sort_keys=True)
                    handle.flush()
                    os.fsync(handle.fileno())
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                real = os.environ.get("REAL_FIRECRAWL", "")
                if not real:
                    print("REAL_FIRECRAWL is unset", file=sys.stderr)
                    raise SystemExit(127)
                os.execv(real, [real, *sys.argv[1:]])
                """
            ),
            encoding="utf-8",
        )
        proxy.chmod(0o700)

    def operation_data(self) -> dict[str, Any]:
        return json.loads(self.counter.read_text(encoding="utf-8"))

    def _temporary_entries(self) -> list[str]:
        return sorted(str(path.relative_to(self.monitored_tmp)) for path in self.monitored_tmp.rglob("*"))

    def _clear_temporary_entries(self) -> None:
        for path in sorted(self.monitored_tmp.rglob("*"), key=lambda item: len(item.parts), reverse=True):
            if path.is_dir():
                path.rmdir()
            else:
                path.unlink(missing_ok=True)

    def _record(
        self,
        name: str,
        *,
        category: str,
        contract_result: str,
        capability_result: str = "NOT_EVALUATED",
        observed_disposition: str = "not_evaluated",
        required_contract: bool = True,
        required_capability: bool = False,
        returncode: int = 0,
        seconds: float = 0.0,
        stdout: str = "",
        stderr: str = "",
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        clean = contract_result == "PASS" and (
            not required_capability or capability_result == "PASS"
        )
        status = "pass" if clean else ("not-run" if contract_result == "NOT_EVALUATED" else "fail")
        case = {
            "name": name,
            "category": category,
            "status": status,
            "required": required_contract or required_capability,
            "required_contract": required_contract,
            "required_capability": required_capability,
            "contract_result": contract_result,
            "capability_result": capability_result,
            "observed_disposition": observed_disposition,
            "returncode": returncode,
            "seconds": round(seconds, 2),
            "operations_after": self.operation_data()["count"],
            "stdout": bounded(stdout),
            "stderr": bounded(stderr),
            "details": details or {},
        }
        self.cases.append(case)
        print(
            f"[{status.upper()}] {name}: contract={contract_result} "
            f"capability={capability_result} disposition={observed_disposition}"
        )
        return case

    def run(
        self,
        name: str,
        command: list[str],
        *,
        category: str = "matrix",
        timeout: int = 900,
        env_changes: dict[str, str | None] | None = None,
        required_contract: bool = True,
        required_capability: bool = False,
        json_output: bool = False,
        expected_schema: str | None = None,
        expected_returncodes: tuple[int, ...] = (0,),
        capability_evaluator: Callable[[dict[str, Any] | None, int], bool] | None = None,
        require_no_provider_activity: bool = False,
    ) -> dict[str, Any]:
        env = self.env.copy()
        for key, value in (env_changes or {}).items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = str(value)
        started = time.monotonic()
        operations_before = int(self.operation_data()["count"])
        try:
            result = self.runner(
                command,
                text=True,
                capture_output=True,
                env=env,
                timeout=timeout,
                check=False,
            )
            returncode = int(result.returncode)
            stdout = result.stdout or ""
            stderr = result.stderr or ""
        except subprocess.TimeoutExpired as exc:
            returncode = 124
            stdout = str(exc.stdout or "")
            stderr = f"TIMEOUT after {timeout}s\n{exc.stderr or ''}"

        payload = _json_dict(stdout) if json_output else None
        operations_after = int(self.operation_data()["count"])
        operation_delta = operations_after - operations_before
        contract_ok = returncode in expected_returncodes
        if require_no_provider_activity and operation_delta != 0:
            contract_ok = False
            stderr = (
                f"{stderr}\nunexpected Firecrawl provider activity: {operation_delta} call(s)"
            ).strip()
        if json_output and payload is None:
            contract_ok = False
            stderr = f"{stderr}\ninvalid JSON output".strip()
        if expected_schema is not None and (
            payload is None or payload.get("schema_version") != expected_schema
        ):
            contract_ok = False
            stderr = (
                f"{stderr}\nunexpected schema_version: "
                f"{None if payload is None else payload.get('schema_version')!r}"
            ).strip()

        entries = self._temporary_entries()
        if entries:
            contract_ok = False
            stderr = f"{stderr}\nmonitored TMPDIR retained entries: {entries!r}".strip()
            self._clear_temporary_entries()

        capability_result = "NOT_EVALUATED"
        if required_capability:
            evaluator = capability_evaluator or (lambda _payload, rc: rc == 0)
            capability_result = "PASS" if contract_ok and evaluator(payload, returncode) else "FAIL"

        details: dict[str, Any] = {
            "command": command,
            "expected_returncodes": list(expected_returncodes),
            "provider_operations_before": operations_before,
            "provider_operations_after": operations_after,
            "provider_operation_delta": operation_delta,
        }
        if payload is not None:
            details["json"] = payload
        if entries:
            details["temporary_entries"] = entries

        return self._record(
            name,
            category=category,
            contract_result="PASS" if contract_ok else "FAIL",
            capability_result=capability_result,
            observed_disposition=_observed_disposition(payload, returncode),
            required_contract=required_contract,
            required_capability=required_capability,
            returncode=returncode,
            seconds=time.monotonic() - started,
            stdout=stdout,
            stderr=stderr,
            details=details,
        )

    def _track_run(
        self,
        name: str,
        run_id: str,
        objective: str,
        *,
        quality_required: bool = False,
        require_planning: bool = False,
        require_corpus: bool = False,
        require_terminal: bool = False,
    ) -> None:
        if run_id in self.preexisting_run_ids:
            raise RuntimeError(
                f"validator refused to claim pre-existing research run: {run_id}"
            )
        metadata = self.owned_runs.setdefault(
            run_id,
            {"case": name, "objective": objective},
        )
        metadata.update(
            {
                "quality_required": bool(
                    metadata.get("quality_required") or quality_required
                ),
                "require_planning": bool(
                    metadata.get("require_planning") or require_planning
                ),
                "require_corpus": bool(
                    metadata.get("require_corpus") or require_corpus
                ),
                "require_terminal": bool(
                    metadata.get("require_terminal") or require_terminal
                ),
            }
        )

    def _owned_objective(self, name: str, objective: str) -> str:
        value = (
            f"{objective} [live-validation:{self.campaign_id}:{name}]"
        )
        self.discovery_objectives[value] = name
        return value

    def _discover_owned_runs(self) -> None:
        if not self.run_baseline_captured:
            return
        for objective, name in sorted(self.discovery_objectives.items()):
            candidates = (
                self.inspector.run_ids_for_objective(objective)
                - self.preexisting_run_ids
            )
            untracked = sorted(candidates - set(self.owned_runs))
            if len(untracked) == 1:
                self._track_run(name, untracked[0], objective)
            elif len(untracked) > 1:
                self._record(
                    f"ambiguous_run_ownership_{name}",
                    category="plumbing",
                    contract_result="FAIL",
                    observed_disposition="ownership_ambiguous",
                    details={
                        "objective": objective,
                        "candidate_run_ids": untracked,
                    },
                    stderr=(
                        "multiple non-baseline runs share the validator-owned "
                        "objective; none were claimed for cleanup"
                    ),
                )

    def create_run(
        self,
        name: str,
        objective: str,
        *,
        prepare: bool = False,
    ) -> str | None:
        owned_objective = self._owned_objective(name, objective)
        case = self.run(
            f"create_run_{name}",
            [str(SCRIPT_DIR / "frun"), "start", owned_objective],
            category="plumbing",
            timeout=60,
        )
        if case["contract_result"] != "PASS":
            self._discover_owned_runs()
            return None
        run_id = case["stdout"].strip().splitlines()[-1]
        if not re.fullmatch(r"fr_[0-9a-f]{32}", run_id):
            case["contract_result"] = "FAIL"
            case["status"] = "fail"
            case["stderr"] = bounded(
                f"{case['stderr']}\ninvalid authoritative run ID: {run_id!r}"
            )
            self._discover_owned_runs()
            return None
        if run_id in self.preexisting_run_ids:
            case["contract_result"] = "FAIL"
            case["status"] = "fail"
            case["stderr"] = bounded(
                f"{case['stderr']}\nfrun returned pre-existing run ID: {run_id}"
            )
            return None
        owned_candidates = (
            self.inspector.run_ids_for_objective(owned_objective)
            - self.preexisting_run_ids
        )
        if owned_candidates != {run_id}:
            case["contract_result"] = "FAIL"
            case["status"] = "fail"
            case["stderr"] = bounded(
                f"{case['stderr']}\nrun ownership readback mismatch: "
                f"{sorted(owned_candidates)!r}"
            )
            self._discover_owned_runs()
            return None
        self._track_run(name, run_id, owned_objective)
        if prepare:
            prepared = self.run(
                f"prepare_run_{name}",
                [str(SCRIPT_DIR / "frun"), "prepare", run_id],
                category="plumbing",
                timeout=60,
            )
            if prepared["contract_result"] != "PASS":
                return None
        return run_id

    def _validate_implementation_identity(self) -> bool:
        repo_root = SCRIPT_DIR.parent
        head = self.runner(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        status = self.runner(
            ["git", "-C", str(repo_root), "status", "--porcelain", "--untracked-files=no"],
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        observed = (head.stdout or "").strip().lower()
        expected = (self.args.expected_head_sha or "").lower()
        clean = (
            head.returncode == 0
            and status.returncode == 0
            and not (status.stdout or "").strip()
            and bool(re.fullmatch(r"[0-9a-f]{40}", observed))
            and (not expected or observed == expected)
        )
        if clean:
            self.implementation_head = observed
        self._record(
            "implementation_identity",
            category="plumbing",
            contract_result="PASS" if clean else "FAIL",
            observed_disposition=observed or "unresolved",
            details={
                "expected_head_sha": expected or None,
                "observed_head_sha": observed or None,
                "tracked_worktree_clean": not bool((status.stdout or "").strip()),
            },
            stderr="" if clean else bounded((head.stderr or "") + "\n" + (status.stderr or "")),
        )
        return clean

    def preflight(self) -> bool:
        if not self._validate_implementation_identity():
            return False
        if not self.args.database_url:
            self._record(
                "authoritative_store",
                category="plumbing",
                contract_result="FAIL",
                stderr="DATABASE_URL is required",
            )
            return False
        ready = self.run(
            "authoritative_store",
            [str(SCRIPT_DIR / "research-db"), "ingest-ready"],
            category="plumbing",
            timeout=60,
        )
        if ready["contract_result"] != "PASS":
            return False
        try:
            self.preexisting_run_ids = self.inspector.list_run_ids()
            self.run_baseline_captured = True
        except Exception as exc:  # noqa: BLE001
            self._record(
                "research_run_baseline",
                category="plumbing",
                contract_result="FAIL",
                stderr=f"{type(exc).__name__}: {exc}",
            )
            return False
        self._record(
            "research_run_baseline",
            category="plumbing",
            contract_result="PASS",
            observed_disposition="captured",
            details={"preexisting_run_count": len(self.preexisting_run_ids)},
        )
        try:
            alias = self.inspector.probe_qdrant_alias()
        except Exception as exc:  # noqa: BLE001
            self._record(
                "qdrant_active_alias",
                category="plumbing",
                contract_result="FAIL",
                stderr=f"{type(exc).__name__}: {exc}",
            )
            return False
        self._record(
            "qdrant_active_alias",
            category="plumbing",
            contract_result="PASS",
            observed_disposition="compatible",
            details=alias,
        )
# __GHDEV_APPEND__
