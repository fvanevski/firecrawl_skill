"""Bounded live validation for PostgreSQL-authoritative Firecrawl workflows."""

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
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

SCRIPT_DIR = Path(__file__).resolve().parent

BENCHMARKS = {
    "simple": {
        "topic": "current Firecrawl CLI npm package and installation command",
        "facets": ("firecrawl", "npm", "install"),
        "min_domains": 2,
    },
    "academic": {
        "topic": "methodological naturalism cosmology burden of proof evidence objections",
        "facets": ("naturalism", "cosmology", "burden", "evidence", "objection"),
        "min_domains": 3,
    },
    "termux": {
        "topic": (
            "Android Termux Vulkan Turnip Mesa Zink acceleration "
            "compatibility and failure modes"
        ),
        "facets": ("termux", "vulkan", "turnip", "mesa", "zink", "failure"),
        "min_domains": 4,
    },
}
PROFILE_OPERATION_CAPS = {"focused": 40, "failure-path": 20, "full": 100}
_MAX_OUTPUT_CHARS = 4_000
_CHECKPOINT_EXIT = 75


def now_stamp() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y%m%d_%H%M%S_%f")


def jaccard(left: str, right: str) -> float:
    def tokenize(value: str) -> set[str]:
        return set(re.findall(r"[a-z0-9]+", value.lower()))

    first, second = tokenize(left), tokenize(right)
    return len(first & second) / len(first | second) if first | second else 1.0


def bounded(value: str | None, limit: int = _MAX_OUTPUT_CHARS) -> str:
    text = value or ""
    return text if len(text) <= limit else text[: limit - 15] + "...[truncated]"


class AuthoritativeInspector:
    """Read validation evidence from PostgreSQL, BLOB_ROOT, and Qdrant."""

    def __init__(
        self,
        database_url: str,
        *,
        qdrant_url: str | None = None,
        qdrant_api_key: str | None = None,
    ) -> None:
        self.database_url = database_url
        self.qdrant_url = qdrant_url
        self.qdrant_api_key = qdrant_api_key

    def _connect(self):
        import psycopg

        return psycopg.connect(self.database_url)

    def table_counts(self) -> dict[str, int]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """SELECT
                     (SELECT count(*) FROM research_runs),
                     (SELECT count(*) FROM research_invocations),
                     (SELECT count(*) FROM research_specs),
                     (SELECT count(*) FROM research_budget_snapshots),
                     (SELECT count(*) FROM search_plans),
                     (SELECT count(*) FROM semantic_calls),
                     (SELECT count(*) FROM search_responses),
                     (SELECT count(*) FROM search_candidates),
                     (SELECT count(*) FROM extraction_attempts),
                     (SELECT count(*) FROM asset_snapshots),
                     (SELECT count(*) FROM documents),
                     (SELECT count(*) FROM chunks),
                     (SELECT count(*) FROM research_events),
                     (SELECT count(*) FROM index_jobs)"""
            )
            row = cursor.fetchone()
        names = (
            "research_runs",
            "research_invocations",
            "research_specs",
            "research_budget_snapshots",
            "search_plans",
            "semantic_calls",
            "search_responses",
            "search_candidates",
            "extraction_attempts",
            "asset_snapshots",
            "documents",
            "chunks",
            "research_events",
            "index_jobs",
        )
        return {name: int(value) for name, value in zip(names, row, strict=True)}

    def probe_qdrant_alias(self) -> dict[str, Any]:
        from research_store.config import StoreConfig
        from research_store.qdrant import QdrantIndex

        config = StoreConfig.from_env()
        url = self.qdrant_url or config.qdrant_url
        api_key = (
            self.qdrant_api_key
            if self.qdrant_api_key is not None
            else config.qdrant_api_key
        )
        if not url:
            raise RuntimeError("QDRANT_URL is required")
        alias_index = QdrantIndex(
            url,
            api_key,
            config.qdrant_alias,
            config.embedding_dimension,
        )
        aliases = alias_index.list_aliases()
        target = aliases.get(config.qdrant_alias)
        if target != config.physical_collection:
            raise RuntimeError(
                f"active alias {config.qdrant_alias!r} targets {target!r}, "
                f"expected {config.physical_collection!r}"
            )
        physical = alias_index.for_collection(
            target,
            config.embedding_dimension,
            "Cosine",
        )
        schema = physical.inspect_schema()
        if not schema.get("exists") or not schema.get("compatible"):
            raise RuntimeError(f"active Qdrant schema is incompatible: {schema!r}")
        return {
            "alias": config.qdrant_alias,
            "collection": target,
            "dimension": config.embedding_dimension,
            "compatible": True,
        }

    def _run_row(self, external_run_id: str) -> tuple[UUID, str, Any]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """SELECT r.id,r.state,coalesce(p.payload,r.query_plan)
                   FROM research_runs r
                   LEFT JOIN search_plans p ON p.id=r.search_plan_id
                   WHERE r.external_run_id=%s""",
                (external_run_id,),
            )
            row = cursor.fetchone()
        if row is None:
            raise RuntimeError(f"research run not found: {external_run_id}")
        return UUID(str(row[0])), str(row[1]), row[2]

    def _projection_metrics(self, chunk_ids: list[UUID]) -> dict[str, Any]:
        alias = self.probe_qdrant_alias()
        if not chunk_ids:
            return {
                **alias,
                "expected_points": 0,
                "returned_points": 0,
                "coverage": 0.0,
                "empty": True,
            }

        from research_store.qdrant import QdrantIndex

        index = QdrantIndex(
            self.qdrant_url or os.environ.get("QDRANT_URL", "http://localhost:6333"),
            (
                self.qdrant_api_key
                if self.qdrant_api_key is not None
                else os.environ.get("QDRANT_API_KEY", "")
            ),
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
            "empty": False,
        }

    def _blob_integrity(self, digests: list[str]) -> dict[str, Any]:
        from research_store.blob import ContentAddressedBlobStore
        from research_store.config import StoreConfig

        store = ContentAddressedBlobStore(StoreConfig.from_env().blob_root)
        unique = sorted({digest for digest in digests if digest})
        verified = [digest for digest in unique if store.verify(digest)]
        return {
            "expected": len(unique),
            "verified": len(verified),
            "missing_or_invalid": sorted(set(unique) - set(verified)),
            "complete": bool(unique) and len(verified) == len(unique),
        }

    def run_metrics(
        self,
        external_run_id: str,
        benchmark: dict[str, Any] | None,
        *,
        require_corpus: bool,
        require_resume_history: bool = False,
    ) -> dict[str, Any]:
        run_id, state, raw_plan = self._run_row(external_run_id)
        query_plan = json.loads(raw_plan) if isinstance(raw_plan, str) else raw_plan
        plan_entries = (
            query_plan.get("queries", []) if isinstance(query_plan, dict) else []
        )
        planned_queries = [
            str(item.get("query", ""))
            for item in plan_entries
            if isinstance(item, dict) and item.get("query")
        ]

        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """SELECT id,query_text,status,result_count
                   FROM search_responses
                   WHERE run_id=%s AND backend <> 'orchestrator'
                   ORDER BY created_at,id""",
                (run_id,),
            )
            responses = list(cursor.fetchall())
            response_queries = [str(row[1]) for row in responses if row[1]]
            queries = planned_queries or response_queries

            cursor.execute(
                """SELECT c.id,c.canonical_url,c.domain,
                          coalesce(c.title,''),coalesce(c.snippet,'')
                   FROM search_candidates c
                   WHERE c.run_id=%s ORDER BY c.id""",
                (run_id,),
            )
            candidates = list(cursor.fetchall())

            scalar_queries = {
                "invocation_count": (
                    "SELECT count(*) FROM research_invocations WHERE run_id=%s"
                ),
                "event_count": "SELECT count(*) FROM research_events WHERE run_id=%s",
                "coverage_event_count": (
                    "SELECT count(*) FROM coverage_events WHERE run_id=%s"
                ),
                "spec_count": "SELECT count(*) FROM research_specs WHERE run_id=%s",
                "budget_count": (
                    "SELECT count(*) FROM research_budget_snapshots WHERE run_id=%s"
                ),
                "plan_count": "SELECT count(*) FROM search_plans WHERE run_id=%s",
                "semantic_call_count": (
                    "SELECT count(*) FROM semantic_calls WHERE run_id=%s"
                ),
                "extraction_count": (
                    "SELECT count(*) FROM extraction_attempts WHERE run_id=%s"
                ),
            }
            scalars = {}
            for name, statement in scalar_queries.items():
                cursor.execute(statement, (run_id,))
                scalars[name] = int(cursor.fetchone()[0])

            cursor.execute(
                """SELECT DISTINCT s.id,s.content_sha256
                   FROM asset_snapshots s
                   JOIN extraction_attempts ea ON ea.id=s.extraction_attempt_id
                   WHERE ea.run_id=%s""",
                (run_id,),
            )
            snapshot_rows = list(cursor.fetchall())
            snapshot_count = len(snapshot_rows)
            blob_digests = [str(row[1]) for row in snapshot_rows]

            cursor.execute(
                """SELECT count(DISTINCT d.id)
                   FROM documents d
                   LEFT JOIN asset_snapshots s ON s.id=d.snapshot_id
                   LEFT JOIN extraction_attempts ea
                     ON ea.id=coalesce(d.extraction_attempt_id,s.extraction_attempt_id)
                   WHERE ea.run_id=%s""",
                (run_id,),
            )
            document_count = int(cursor.fetchone()[0])

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
            else:
                job_rows = []

            cursor.execute(
                """SELECT prior_state,next_state
                   FROM research_run_transitions
                   WHERE run_id=%s ORDER BY lifecycle_revision,id""",
                (run_id,),
            )
            transition_rows = list(cursor.fetchall())

        domains = {
            str(row[2] or urlsplit(str(row[1] or "")).netloc)
            for row in candidates
            if row[1] or row[2]
        }
        candidate_text = " ".join(
            " ".join(str(value or "") for value in row[1:]).lower()
            for row in candidates
        )
        pairwise = [
            jaccard(queries[first], queries[second])
            for first in range(len(queries))
            for second in range(first + 1, len(queries))
        ]
        facet_coverage = 1.0
        minimum_domains = 1
        if benchmark:
            facets = tuple(benchmark["facets"])
            facet_coverage = sum(
                facet.lower() in candidate_text for facet in facets
            ) / len(facets)
            minimum_domains = int(benchmark["min_domains"])

        job_counts = {str(row[0]): int(row[1]) for row in job_rows}
        total_jobs = sum(job_counts.values())
        completed_jobs = job_counts.get("complete", 0)
        worker_evidence = (
            bool(chunk_ids)
            and total_jobs > 0
            and completed_jobs == total_jobs
            and all(int(row[2] or 0) >= int(row[1]) for row in job_rows)
            and all(int(row[3]) == int(row[1]) for row in job_rows)
            and all(int(row[4]) == int(row[1]) for row in job_rows)
        )
        projection = self._projection_metrics(chunk_ids)
        blob_integrity = self._blob_integrity(blob_digests) if blob_digests else {
            "expected": 0,
            "verified": 0,
            "missing_or_invalid": [],
            "complete": False,
        }
        state_history = [str(row[1]) for row in transition_rows]
        terminal = state in {"completed", "partial", "failed", "cancelled"}
        corpus_complete = (
            scalars["extraction_count"] > 0
            and snapshot_count > 0
            and document_count > 0
            and bool(chunk_ids)
            and blob_integrity["complete"]
        )

        checks = {
            "persisted_run": bool(run_id),
            "terminal_run": terminal,
            "authoritative_spec": scalars["spec_count"] == 1,
            "authoritative_budget": scalars["budget_count"] == 1,
            "authoritative_plan": scalars["plan_count"] == 1,
            "authoritative_semantic_provenance": scalars["semantic_call_count"] > 0,
            "authoritative_invocations": scalars["invocation_count"] > 0,
            "authoritative_search_responses": len(responses) > 0,
            "authoritative_candidates": len(candidates) > 0,
            "authoritative_events": (
                scalars["event_count"] + scalars["coverage_event_count"] > 0
            ),
            "unique_queries": bool(queries)
            and len(queries)
            == len({re.sub(r"\W+", " ", query.lower()).strip() for query in queries}),
            "broad_first": bool(queries) and "site:" not in queries[0].lower(),
            "max_query_similarity": max(pairwise, default=0.0) <= 0.80,
            "domain_diversity": len(domains) >= minimum_domains,
            "facet_coverage": facet_coverage >= 0.70,
            "corpus_persisted": corpus_complete if require_corpus else True,
            "blob_integrity": blob_integrity["complete"] if require_corpus else True,
            "worker_completed": worker_evidence if require_corpus else True,
            "qdrant_alias_compatible": bool(projection["compatible"]),
            "qdrant_coverage": (
                projection["coverage"] == 1.0 if require_corpus else True
            ),
            "resume_checkpoint_observed": (
                "extracting" in state_history if require_resume_history else True
            ),
        }
        return {
            "external_run_id": external_run_id,
            "run_id": str(run_id),
            "state": state,
            "state_history": state_history,
            "query_count": len(queries),
            "search_response_count": len(responses),
            "candidate_count": len(candidates),
            "domain_count": len(domains),
            **scalars,
            "snapshot_count": snapshot_count,
            "document_count": document_count,
            "chunk_count": len(chunk_ids),
            "blob_integrity": blob_integrity,
            "index_job_counts": job_counts,
            "max_query_similarity": round(max(pairwise, default=0.0), 3),
            "facet_coverage": round(facet_coverage, 3),
            "projection": projection,
            "checks": checks,
            "pass": all(checks.values()),
        }

    def wait_for_worker(
        self,
        external_run_id: str,
        benchmark: dict[str, Any] | None,
        *,
        require_corpus: bool,
        require_resume_history: bool = False,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        last = None
        while time.monotonic() < deadline:
            last = self.run_metrics(
                external_run_id,
                benchmark,
                require_corpus=require_corpus,
                require_resume_history=require_resume_history,
            )
            if last["checks"]["worker_completed"] and last["checks"]["qdrant_coverage"]:
                return last
            time.sleep(0.5)
        return last or self.run_metrics(
            external_run_id,
            benchmark,
            require_corpus=require_corpus,
            require_resume_history=require_resume_history,
        )


class Campaign:
    """Execute bounded public workflows and verify authoritative outcomes."""

    def __init__(
        self,
        args: argparse.Namespace,
        *,
        inspector: AuthoritativeInspector | None = None,
        runner=subprocess.run,
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
        )
        self._temporary = None
        if work_root is None:
            self._temporary = tempfile.TemporaryDirectory(
                prefix="firecrawl-live-validation-"
            )
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
        self.runs: dict[str, dict[str, Any]] = {}
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
                        print(
                            f"Firecrawl operation cap exhausted ({maximum})",
                            file=sys.stderr,
                        )
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
        return sorted(
            str(path.relative_to(self.monitored_tmp))
            for path in self.monitored_tmp.rglob("*")
        )

    def _clear_temporary_entries(self) -> None:
        for path in sorted(
            self.monitored_tmp.rglob("*"), key=lambda item: len(item.parts), reverse=True
        ):
            if path.is_dir():
                path.rmdir()
            else:
                path.unlink(missing_ok=True)

    def _record(
        self,
        name: str,
        status: str,
        *,
        required: bool,
        seconds: float = 0.0,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        case = {
            "name": name,
            "status": status,
            "required": required,
            "returncode": returncode,
            "seconds": round(seconds, 2),
            "operations_after": self.operation_data()["count"],
            "stdout": bounded(stdout),
            "stderr": bounded(stderr),
            "details": details or {},
        }
        self.cases.append(case)
        print(
            f"[{status.upper()}] {name} "
            f"({case['seconds']}s, operations={case['operations_after']})"
        )
        return case

    def run(
        self,
        name: str,
        command: list[str],
        *,
        timeout: int = 900,
        env_changes: dict[str, str | None] | None = None,
        required: bool = True,
        json_output: bool = False,
        expected_returncodes: tuple[int, ...] = (0,),
    ) -> dict[str, Any]:
        env = self.env.copy()
        for key, value in (env_changes or {}).items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = str(value)
        started = time.monotonic()
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
            status = "pass" if returncode in expected_returncodes else "fail"
        except subprocess.TimeoutExpired as exc:
            returncode = 124
            stdout = str(exc.stdout or "")
            stderr = f"TIMEOUT after {timeout}s\n{exc.stderr or ''}"
            status = "fail"

        details: dict[str, Any] = {
            "command": command,
            "expected_returncodes": list(expected_returncodes),
        }
        if json_output and status == "pass":
            try:
                details["json"] = json.loads(stdout)
            except json.JSONDecodeError as exc:
                status = "fail"
                stderr = f"{stderr}\ninvalid JSON output: {exc}".strip()

        entries = self._temporary_entries()
        if entries:
            status = "fail"
            details["temporary_entries"] = entries
            stderr = (
                f"{stderr}\nmonitored TMPDIR retained entries: {entries!r}"
            ).strip()
            self._clear_temporary_entries()

        return self._record(
            name,
            status,
            required=required,
            seconds=time.monotonic() - started,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            details=details,
        )

    def preflight(self) -> bool:
        if not self.args.database_url:
            self._record(
                "authoritative_store",
                "fail",
                required=True,
                stderr="DATABASE_URL is required",
            )
            return False
        ready = self.run(
            "authoritative_store",
            [str(SCRIPT_DIR / "research-db"), "ingest-ready"],
            timeout=60,
        )
        if ready["status"] != "pass":
            return False
        try:
            alias = self.inspector.probe_qdrant_alias()
        except Exception as exc:  # noqa: BLE001
            self._record(
                "qdrant_active_alias",
                "fail",
                required=True,
                stderr=f"{type(exc).__name__}: {exc}",
            )
            return False
        self._record(
            "qdrant_active_alias", "pass", required=True, details=alias
        )
        if not self.real_cli:
            self._record(
                "firecrawl_cli",
                "fail",
                required=True,
                stderr="firecrawl executable not found",
            )
            return False
        version = self.run(
            "firecrawl_cli", [self.real_cli, "--version"], timeout=30
        )
        return version["status"] == "pass"

    def create_run(self, name: str, objective: str) -> str | None:
        case = self.run(
            f"create_run_{name}",
            [str(SCRIPT_DIR / "frun"), "start", objective],
            timeout=60,
        )
        if case["status"] != "pass":
            return None
        external_id = case["stdout"].strip().splitlines()[-1]
        if not re.fullmatch(r"fr_[0-9a-f]{32}", external_id):
            case["status"] = "fail"
            case["stderr"] = (
                case["stderr"] + f"\ninvalid authoritative run ID: {external_id!r}"
            ).strip()
            return None
        self.runs[name] = {
            "external_run_id": external_id,
            "objective": objective,
        }
        return external_id

    def validate_dry_run(self) -> None:
        before_counts = self.inspector.table_counts()
        before_operations = self.operation_data()["count"]
        case = self.run(
            "smart_dry_run",
            [
                str(SCRIPT_DIR / "fsearch_smart"),
                BENCHMARKS["academic"]["topic"],
                "--dry-run",
                "--invocation-id",
                "fc_" + "0" * 32,
            ],
            timeout=60,
            env_changes={"DATABASE_URL": None},
            json_output=True,
        )
        after_counts = self.inspector.table_counts()
        after_operations = self.operation_data()["count"]
        payload = case["details"].get("json", {})
        purity = {
            "database_unchanged": before_counts == after_counts,
            "firecrawl_operations_unchanged": before_operations == after_operations,
            "structured_stdout": (
                payload.get("schema_version") == "authoritative-smart-search-plan-v1"
                and payload.get("mode") == "dry_run"
            ),
        }
        self._record(
            "smart_dry_run_purity",
            "pass" if all(purity.values()) else "fail",
            required=True,
            details={**purity, "before_counts": before_counts, "after_counts": after_counts},
        )

    def run_smart(
        self,
        name: str,
        benchmark_key: str,
        *,
        resume: bool = False,
    ) -> str | None:
        benchmark = BENCHMARKS[benchmark_key]
        external_id = self.create_run(name, benchmark["topic"])
        if external_id is None:
            return None
        command = [
            str(SCRIPT_DIR / "fsearch_smart"),
            benchmark["topic"],
            "--research-run-id",
            external_id,
            "--max-adaptive-cycles",
            str(self.args.max_adaptive_cycles),
        ]
        if resume:
            checkpoint = self.run(
                f"smart_{name}_checkpoint",
                [*command, "--stop-after-state", "extracting"],
                timeout=self.args.case_timeout,
                expected_returncodes=(_CHECKPOINT_EXIT,),
            )
            if checkpoint["status"] == "pass":
                self.run(
                    f"smart_{name}_resume",
                    command,
                    timeout=self.args.case_timeout,
                )
        else:
            self.run(f"smart_{name}", command, timeout=self.args.case_timeout)
        self.runs[name]["benchmark_key"] = benchmark_key
        self.runs[name]["require_corpus"] = True
        self.runs[name]["require_resume_history"] = resume
        return external_id

    def run_fscrape_valkey_loss(self) -> str | None:
        name = "fscrape_valkey_loss"
        external_id = self.create_run(
            name, "Valkey-loss authoritative direct scrape validation"
        )
        if external_id is None:
            return None
        self.run(
            name,
            [
                str(SCRIPT_DIR / "fscrape"),
                "https://example.com",
                "--research-run-id",
                external_id,
                "--json",
            ],
            timeout=self.args.case_timeout,
            env_changes={"VALKEY_URL": "redis://127.0.0.1:1/0"},
            json_output=True,
        )
        self.runs[name]["benchmark_key"] = None
        self.runs[name]["require_corpus"] = True
        self.runs[name]["valkey_loss"] = True
        return external_id

    def run_full_cases(self) -> None:
        search_name = "fsearch_public"
        search_run = self.create_run(
            search_name, "Public authoritative fsearch validation"
        )
        if search_run:
            self.run(
                search_name,
                [
                    str(SCRIPT_DIR / "fsearch"),
                    BENCHMARKS["simple"]["topic"],
                    "--research-run-id",
                    search_run,
                    "--limit",
                    "5",
                    "--scrape-limit",
                    "2",
                    "--json",
                ],
                timeout=self.args.case_timeout,
                json_output=True,
            )
            self.runs[search_name]["benchmark_key"] = "simple"
            self.runs[search_name]["require_corpus"] = True

        scrape_name = "fscrape_structured"
        scrape_run = self.create_run(
            scrape_name, "Public authoritative structured fscrape validation"
        )
        if scrape_run:
            schema = json.dumps(
                {
                    "type": "object",
                    "properties": {"title": {"type": ["string", "null"]}},
                }
            )
            self.run(
                scrape_name,
                [
                    str(SCRIPT_DIR / "fscrape"),
                    "https://example.com",
                    "--research-run-id",
                    scrape_run,
                    "--schema",
                    schema,
                    "--json",
                ],
                timeout=self.args.case_timeout,
                json_output=True,
            )
            self.runs[scrape_name]["benchmark_key"] = None
            self.runs[scrape_name]["require_corpus"] = True

    def collect_metrics(self) -> list[dict[str, Any]]:
        metrics = []
        for name, metadata in self.runs.items():
            benchmark_key = metadata.get("benchmark_key")
            benchmark = BENCHMARKS.get(benchmark_key) if benchmark_key else None
            try:
                item = self.inspector.wait_for_worker(
                    metadata["external_run_id"],
                    benchmark,
                    require_corpus=bool(metadata.get("require_corpus")),
                    require_resume_history=bool(
                        metadata.get("require_resume_history")
                    ),
                    timeout_seconds=self.args.worker_timeout,
                )
            except Exception as exc:  # noqa: BLE001
                item = {
                    "external_run_id": metadata["external_run_id"],
                    "checks": {"metrics_available": False},
                    "pass": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            item["case"] = name
            if metadata.get("valkey_loss"):
                item["checks"]["valkey_loss_tolerated"] = item.get("pass", False)
                item["pass"] = all(item["checks"].values())
            metrics.append(item)
        return metrics

    def execute(self) -> int:
        if not self.preflight():
            return self.finish(exit_override=2)
        self.validate_dry_run()
        if self.args.profile == "failure-path":
            self.run_fscrape_valkey_loss()
            return self.finish()
        self.run_smart("academic", "academic", resume=True)
        self.run_fscrape_valkey_loss()
        if self.args.profile == "full":
            self.run_smart("simple", "simple")
            self.run_smart("termux", "termux")
            self.run_full_cases()
        return self.finish()

    def _report_markdown(self, manifest: dict[str, Any]) -> str:
        lines = [
            f"# Firecrawl Authoritative Live Validation: {self.campaign_id}",
            "",
            f"- Profile: `{self.args.profile}`",
            (
                f"- Operations: `{manifest['operations']['count']}/"
                f"{manifest['operations']['max']}`"
            ),
            f"- Required cases: `{'PASS' if manifest['required_cases_pass'] else 'FAIL'}`",
            f"- Authoritative metrics: `{'PASS' if manifest['quality_pass'] else 'FAIL'}`",
            "",
            "## Cases",
            "",
            "| Case | Status | Seconds | Operations after |",
            "|---|---:|---:|---:|",
        ]
        lines.extend(
            f"| {case['name']} | {case['status']} | {case['seconds']} | "
            f"{case['operations_after']} |"
            for case in manifest["cases"]
        )
        lines.extend(
            [
                "",
                "## Authoritative run metrics",
                "",
                "| Case | Responses | Candidates | Snapshots | Documents | Chunks | Blobs | Qdrant | Status |",
                "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for item in manifest["quality_metrics"]:
            projection = item.get("projection", {})
            blobs = item.get("blob_integrity", {})
            lines.append(
                f"| {item.get('case')} | {item.get('search_response_count', 0)} | "
                f"{item.get('candidate_count', 0)} | {item.get('snapshot_count', 0)} | "
                f"{item.get('document_count', 0)} | {item.get('chunk_count', 0)} | "
                f"{blobs.get('verified', 0)}/{blobs.get('expected', 0)} | "
                f"{projection.get('coverage', 0):.0%} | "
                f"{'PASS' if item.get('pass') else 'FAIL'} |"
            )
        return "\n".join(lines) + "\n"

    def finish(self, *, exit_override: int | None = None) -> int:
        metrics = self.collect_metrics() if self.runs else []
        operations = self.operation_data()
        required_cases_pass = all(
            case["status"] == "pass" for case in self.cases if case.get("required")
        )
        quality_pass = bool(metrics) and all(item.get("pass") for item in metrics)
        manifest = {
            "schema_version": "authoritative-live-validation-v1",
            "campaign_id": self.campaign_id,
            "profile": self.args.profile,
            "duration_seconds": round(time.monotonic() - self.started, 2),
            "operations": operations,
            "cases": self.cases,
            "quality_metrics": metrics,
            "required_cases_pass": required_cases_pass,
            "quality_pass": quality_pass,
            "monitored_tmp_clean": not self._temporary_entries(),
        }
        if self.args.artifact_root:
            destination = Path(self.args.artifact_root) / self.campaign_id
            destination.mkdir(parents=True, exist_ok=False)
            (destination / "manifest.json").write_text(
                json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
            )
            (destination / "report.md").write_text(
                self._report_markdown(manifest), encoding="utf-8"
            )
            print(f"Artifacts: {destination}")
        else:
            print(json.dumps(manifest, indent=2, sort_keys=True))
        if exit_override is not None:
            return exit_override
        return 0 if required_cases_pass and quality_pass else 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--api-url",
        default=os.environ.get("FIRECRAWL_API_URL", "http://garion.us:3002"),
    )
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL", ""))
    parser.add_argument("--qdrant-url", default=os.environ.get("QDRANT_URL"))
    parser.add_argument("--qdrant-api-key", default=os.environ.get("QDRANT_API_KEY"))
    parser.add_argument("--max-operations", type=int, default=40)
    parser.add_argument("--max-adaptive-cycles", type=int, default=2)
    parser.add_argument("--case-timeout", type=int, default=1800)
    parser.add_argument("--worker-timeout", type=float, default=90.0)
    parser.add_argument("--artifact-root")
    parser.add_argument("--run-id")
    parser.add_argument(
        "--profile", choices=tuple(PROFILE_OPERATION_CAPS), default="focused"
    )
    args = parser.parse_args(argv)
    profile_cap = PROFILE_OPERATION_CAPS[args.profile]
    if not 1 <= args.max_operations <= profile_cap:
        parser.error(
            f"--max-operations must be between 1 and {profile_cap} "
            f"for profile {args.profile}"
        )
    if not 1 <= args.max_adaptive_cycles <= 10:
        parser.error("--max-adaptive-cycles must be between 1 and 10")
    if args.case_timeout < 1:
        parser.error("--case-timeout must be positive")
    if args.worker_timeout < 0:
        parser.error("--worker-timeout must be non-negative")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    campaign = Campaign(args)
    try:
        return campaign.execute()
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    finally:
        campaign.close()


if __name__ == "__main__":
    raise SystemExit(main())
