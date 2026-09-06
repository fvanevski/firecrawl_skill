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
from typing import Any, LiteralString
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
    "academic": (
        "Research methodological naturalism in cosmology. Explain what methodological "
        "naturalism means in cosmological inquiry, how burden-of-proof standards apply, "
        "what evidence is commonly cited, and what major objections are raised. Use "
        "scholarly sources and apply no publication-date restriction."
    ),
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


def _research_schema_contract(payload: dict[str, Any] | None) -> bool:
    if payload is None:
        return False
    schema_version = str(payload.get("schema_version") or "")
    schema_filename = {
        "workflow-directive-v2": "workflow-directive-v2.json",
        "research-result-v3": "research-result-v3.json",
    }.get(schema_version)
    if schema_filename is None:
        return False
    schema_root = SCRIPT_DIR.parent / "schemas" / "research-workflow"
    try:
        from jsonschema import Draft202012Validator

        schema = json.loads((schema_root / schema_filename).read_text(encoding="utf-8"))
        if schema_version == "research-result-v3":
            handoff = json.loads(
                (schema_root / "research-handoff-v1.json").read_text(encoding="utf-8")
            )
            schema["properties"]["handoff"]["anyOf"][1] = handoff
        Draft202012Validator.check_schema(schema)
        return Draft202012Validator(schema).is_valid(payload)
    except Exception:  # noqa: BLE001
        return False


def _fscrape_result_contract(payload: dict[str, Any] | None) -> bool:
    if payload is None or payload.get("schema_version") != "authoritative-fscrape-v1":
        return False
    required_types = {
        "status": str,
        "run_id": str,
        "research_run_id": str,
        "batch_id": str,
        "invocation_id": str,
        "replayed": bool,
        "items": list,
        "item_count": int,
        "items_truncated": bool,
        "corpus_ids": dict,
    }
    if any(
        not isinstance(payload.get(name), expected)
        for name, expected in required_types.items()
    ):
        return False
    if payload.get("status") not in {"complete", "partial", "failed"}:
        return False
    research_run_id = str(payload.get("research_run_id") or "")
    if not re.fullmatch(
        r"fr_(?:[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
        r"[89ab][0-9a-f]{3}-[0-9a-f]{12})",
        research_run_id,
    ):
        return False
    item_count = int(payload["item_count"])
    items = payload["items"]
    if item_count < 0 or item_count < len(items):
        return False
    return bool(payload["items_truncated"]) == (item_count > len(items))


def _fscrape_error_contract(payload: dict[str, Any] | None, returncode: int) -> bool:
    if (
        payload is None
        or payload.get("schema_version") != "authoritative-fscrape-error-v1"
    ):
        return False
    if payload.get("status") != "failed" or not isinstance(payload.get("error"), str):
        return False
    stage = str(payload.get("failure_stage") or "")
    expected_returncode = {
        "preflight": 2,
        "extraction": 5,
        "ingestion": 6,
        "indexing": 7,
    }.get(stage)
    if expected_returncode is None or returncode != expected_returncode:
        return False
    nested = payload.get("result")
    return nested is None or (
        isinstance(nested, dict) and _fscrape_result_contract(nested)
    )


def _fscrape_extraction_failure_contract(
    payload: dict[str, Any] | None, returncode: int
) -> bool:
    if returncode != 5 or payload is None:
        return False
    if payload.get("schema_version") == "authoritative-fscrape-error-v1":
        return _fscrape_error_contract(payload, returncode) and (
            payload.get("failure_stage") == "extraction"
        )
    if not _fscrape_result_contract(payload) or payload.get("status") != "failed":
        return False
    items = payload.get("items")
    return bool(
        payload.get("item_count") == 1
        and isinstance(items, list)
        and len(items) == 1
        and isinstance(items[0], dict)
        and items[0].get("status") == "failed"
        and (items[0].get("error") or items[0].get("diagnostic"))
    )


def _fresearch_contract(payload: dict[str, Any] | None, returncode: int) -> bool:
    if not _research_schema_contract(payload):
        return False
    assert payload is not None
    disposition = str(payload.get("disposition") or "")
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
            cursor.execute(
                "SELECT external_run_id FROM research_runs ORDER BY external_run_id"
            )
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
        if row is None:
            raise RuntimeError("authoritative table-count query returned no row")
        return {name: int(value) for name, value in zip(names, row, strict=True)}

    def probe_qdrant_alias(self) -> dict[str, Any]:
        from firecrawl_skill.research_store.config import StoreConfig
        from firecrawl_skill.research_store.retrieval.projection.qdrant import (
            QdrantIndex,
        )

        config = StoreConfig.from_env()
        url = self.qdrant_url or config.qdrant_url
        api_key = (
            self.qdrant_api_key
            if self.qdrant_api_key is not None
            else config.qdrant_api_key
        )
        if not url:
            raise RuntimeError("QDRANT_URL is required")
        index = QdrantIndex(
            url, api_key, config.qdrant_alias, config.embedding_dimension
        )
        aliases = index.list_aliases()
        target = aliases.get(config.qdrant_alias)
        if target is None:
            raise RuntimeError(f"active alias {config.qdrant_alias!r} is missing")
        if target != config.physical_collection:
            raise RuntimeError(
                f"active alias {config.qdrant_alias!r} targets {target!r}, "
                f"expected {config.physical_collection!r}"
            )
        schema = index.for_collection(
            target, config.embedding_dimension, "Cosine"
        ).inspect_schema()
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
        from firecrawl_skill.research_store.retrieval.projection.qdrant import (
            QdrantIndex,
        )

        index = QdrantIndex(
            self.qdrant_url or os.environ.get("QDRANT_URL", "http://localhost:6333"),
            self.qdrant_api_key
            if self.qdrant_api_key is not None
            else os.environ.get("QDRANT_API_KEY", ""),
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
            scalar_queries: dict[str, LiteralString] = {
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
                    raise RuntimeError(
                        f"authoritative metric query returned no row: {name}"
                    )
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
                   FROM documents d
                   LEFT JOIN asset_snapshots s ON s.id=d.snapshot_id
                   LEFT JOIN extraction_attempts ea
                     ON ea.id=coalesce(d.extraction_attempt_id,s.extraction_attempt_id)
                   WHERE ea.run_id=%s ORDER BY d.id""",
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
            else {
                "expected": 0,
                "verified": 0,
                "missing_or_invalid": [],
                "complete": False,
            }
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
            "search": scalars["search_response_count"] > 0
            and scalars["candidate_count"] > 0,
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
            "qdrant_coverage": projection["coverage"] == 1.0
            if require_corpus
            else True,
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
            self._temporary = tempfile.TemporaryDirectory(
                prefix="firecrawl-live-validation-"
            )
            work_root = Path(self._temporary.name)
        self.work_root = Path(work_root)
        self.monitored_tmp = self.work_root / "tmp"
        self.proxy_dir = self.work_root / "proxy"
        self.cache_dir = self.work_root / "cache"
        self.monitored_tmp.mkdir(parents=True, exist_ok=True)
        self.proxy_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
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
        self.ownership_discovery_failures: dict[str, dict[str, str]] = {}
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
                "TIKTOKEN_CACHE_DIR": str(self.cache_dir),
                "DATA_GYM_CACHE_DIR": str(self.cache_dir),
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
        return sorted(
            str(path.relative_to(self.monitored_tmp))
            for path in self.monitored_tmp.rglob("*")
        )

    def _clear_temporary_entries(self) -> None:
        for path in sorted(
            self.monitored_tmp.rglob("*"),
            key=lambda item: len(item.parts),
            reverse=True,
        ):
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
        status = (
            "pass"
            if clean
            else ("not-run" if contract_result == "NOT_EVALUATED" else "fail")
        )
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
        capability_evaluator: Callable[[dict[str, Any] | None, int], bool]
        | None = None,
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
        if expected_schema is not None:
            if expected_schema == "authoritative-fscrape-error-v1":
                schema_ok = _fscrape_error_contract(payload, returncode)
            elif expected_schema == "authoritative-fscrape-v1":
                schema_ok = _fscrape_result_contract(payload)
            else:
                schema_ok = bool(
                    payload is not None
                    and payload.get("schema_version") == expected_schema
                )
            if not schema_ok:
                contract_ok = False
                stderr = (
                    f"{stderr}\ninvalid {expected_schema} public contract"
                ).strip()

        entries = self._temporary_entries()
        if entries:
            contract_ok = False
            stderr = f"{stderr}\nmonitored TMPDIR retained entries: {entries!r}".strip()
            self._clear_temporary_entries()

        capability_result = "NOT_EVALUATED"
        if required_capability:
            evaluator = capability_evaluator or (lambda _payload, rc: rc == 0)
            capability_result = (
                "PASS" if contract_ok and evaluator(payload, returncode) else "FAIL"
            )

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
        value = f"{objective} [live-validation:{self.campaign_id}:{name}]"
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
            tracked = {
                run_id
                for run_id, metadata in self.owned_runs.items()
                if metadata.get("objective") == objective
            }
            untracked = sorted(candidates - set(self.owned_runs))
            if not tracked and len(untracked) == 1:
                self._track_run(name, untracked[0], objective)
                continue
            if not untracked:
                continue

            failure = {
                "run_id": "<ownership-discovery>",
                "error": (
                    "ambiguous non-baseline runs share validator objective "
                    f"{objective!r}: {untracked!r}"
                ),
            }
            if objective not in self.ownership_discovery_failures:
                self.ownership_discovery_failures[objective] = failure
                self._record(
                    f"ambiguous_run_ownership_{name}",
                    category="plumbing",
                    contract_result="FAIL",
                    observed_disposition="ownership_ambiguous",
                    details={
                        "objective": objective,
                        "tracked_run_ids": sorted(tracked),
                        "candidate_run_ids": sorted(candidates),
                        "unclaimed_run_ids": untracked,
                    },
                    stderr=(
                        "run ownership is ambiguous; unclaimed runs were not "
                        "mutated during cleanup"
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
            stderr=""
            if clean
            else bounded((head.stderr or "") + "\n" + (status.stderr or "")),
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
        if not self.real_cli:
            self._record(
                "firecrawl_cli",
                category="plumbing",
                contract_result="FAIL",
                stderr="firecrawl executable not found",
            )
            return False
        version = self.run(
            "firecrawl_cli",
            [self.real_cli, "--version"],
            category="plumbing",
            timeout=30,
        )
        return version["contract_result"] == "PASS"

    def validate_retired_smart_options(self) -> None:
        values = {
            "--dry-run": (),
            "--stop-after-state": ("extracting",),
            "--research-run-id": ("fr_" + "0" * 32,),
            "--max-adaptive-cycles": ("1",),
        }
        for option in RETIRED_SMART_OPTIONS:
            self.run(
                f"retired_smart_option_{option[2:].replace('-', '_')}",
                [
                    str(SCRIPT_DIR / "fsearch_smart"),
                    "retired compatibility assertion",
                    option,
                    *values[option],
                ],
                category="matrix",
                timeout=60,
                expected_returncodes=(2,),
                require_no_provider_activity=True,
            )

    def run_fresearch(self, name: str, objective: str) -> None:
        owned_objective = self._owned_objective(name, objective)
        case = self.run(
            name,
            [str(SCRIPT_DIR / "fresearch"), "run", owned_objective],
            timeout=self.args.case_timeout,
            json_output=True,
            expected_returncodes=(0, 1, 75),
            required_capability=True,
            capability_evaluator=lambda payload, rc: bool(
                rc == 0
                and payload
                and payload.get("schema_version") == "research-result-v3"
                and payload.get("disposition") == "terminal_completed"
                and payload.get("objective_satisfied") is True
            ),
        )
        payload = case["details"].get("json")
        if not _fresearch_contract(
            payload if isinstance(payload, dict) else None,
            int(case["returncode"]),
        ):
            case["contract_result"] = "FAIL"
            case["capability_result"] = "FAIL"
            case["status"] = "fail"
            case["stderr"] = bounded(
                f"{case['stderr']}\ninvalid fresearch schema/disposition/exit mapping"
            )

        owned_candidates = (
            self.inspector.run_ids_for_objective(owned_objective)
            - self.preexisting_run_ids
        )
        run_id = _run_id_from_payload(payload if isinstance(payload, dict) else None)
        if run_id and owned_candidates == {run_id}:
            self._track_run(
                name,
                run_id,
                owned_objective,
                quality_required=case["capability_result"] == "PASS",
                require_planning=case["capability_result"] == "PASS",
                require_corpus=case["capability_result"] == "PASS",
                require_terminal=case["capability_result"] == "PASS",
            )
        elif run_id:
            case["contract_result"] = "FAIL"
            case["capability_result"] = "FAIL"
            case["status"] = "fail"
            case["stderr"] = bounded(
                f"{case['stderr']}\nresult run ownership readback mismatch: "
                f"{sorted(owned_candidates)!r}"
            )
            self._discover_owned_runs()
        else:
            if case["contract_result"] == "PASS":
                case["contract_result"] = "FAIL"
                case["capability_result"] = "FAIL"
                case["status"] = "fail"
                case["stderr"] = bounded(f"{case['stderr']}\nmissing canonical run_id")
            self._discover_owned_runs()

    def run_unprepared_rejection(self) -> None:
        objective = "unprepared acquisition fail-closed validation"
        run_id = self.create_run("unprepared_fscrape", objective)
        if run_id is None:
            return
        self.run(
            "unprepared_fscrape_rejected",
            [
                str(SCRIPT_DIR / "fscrape"),
                "https://example.com",
                "--research-run-id",
                run_id,
                "--json",
            ],
            expected_returncodes=(2,),
            json_output=True,
            expected_schema="authoritative-fscrape-error-v1",
            require_no_provider_activity=True,
        )

    def run_provider_failure(self) -> None:
        objective = "provider failure typing validation"
        run_id = self.create_run("provider_failure", objective, prepare=True)
        if run_id is None:
            return
        case = self.run(
            "provider_failure_typed",
            [
                str(SCRIPT_DIR / "fscrape"),
                "https://example.com",
                "--research-run-id",
                run_id,
                "--json",
            ],
            env_changes={"FIRECRAWL_API_URL": "http://127.0.0.1:1"},
            expected_returncodes=(5,),
            json_output=True,
        )
        payload = case["details"].get("json")
        if not _fscrape_extraction_failure_contract(
            payload if isinstance(payload, dict) else None,
            int(case["returncode"]),
        ):
            case["contract_result"] = "FAIL"
            case["status"] = "fail"
            case["stderr"] = bounded(
                f"{case['stderr']}\ninvalid typed extraction-failure contract"
            )

    def run_valkey_loss_capability(self) -> None:
        objective = "Valkey-loss direct scrape validation"
        run_id = self.create_run("fscrape_valkey_loss", objective, prepare=True)
        if run_id is None:
            return
        case = self.run(
            "fscrape_valkey_loss",
            [
                str(SCRIPT_DIR / "fscrape"),
                "https://example.com",
                "--research-run-id",
                run_id,
                "--json",
            ],
            timeout=self.args.case_timeout,
            env_changes={"VALKEY_URL": "redis://127.0.0.1:1/0"},
            required_capability=True,
            json_output=True,
            expected_schema="authoritative-fscrape-v1",
            capability_evaluator=lambda payload, rc: bool(
                rc == 0 and payload and payload.get("status") == "complete"
            ),
        )
        if case["capability_result"] == "PASS":
            self._track_run(
                "fscrape_valkey_loss",
                run_id,
                objective,
                quality_required=True,
                require_corpus=True,
            )

    def run_public_fsearch(self) -> None:
        objective = "Public authoritative fsearch validation"
        run_id = self.create_run("fsearch_public", objective, prepare=True)
        if run_id is None:
            return
        case = self.run(
            "fsearch_public",
            [
                str(SCRIPT_DIR / "fsearch"),
                BENCHMARKS["simple"],
                "--research-run-id",
                run_id,
                "--limit",
                "5",
                "--scrape-limit",
                "2",
                "--json",
            ],
            timeout=self.args.case_timeout,
            required_capability=True,
            json_output=True,
            capability_evaluator=lambda _payload, rc: rc == 0,
        )
        if case["capability_result"] == "PASS":
            self._track_run(
                "fsearch_public",
                run_id,
                objective,
                quality_required=True,
                require_corpus=True,
            )

    def collect_quality_metrics(self) -> list[dict[str, Any]]:
        metrics: list[dict[str, Any]] = []
        for run_id, metadata in self.owned_runs.items():
            if not metadata.get("quality_required"):
                continue
            try:
                item = self.inspector.wait_for_worker(
                    run_id,
                    require_planning=bool(metadata.get("require_planning")),
                    require_corpus=bool(metadata.get("require_corpus")),
                    require_terminal=bool(metadata.get("require_terminal")),
                    timeout_seconds=self.args.worker_timeout,
                )
            except Exception as exc:  # noqa: BLE001
                item = {
                    "external_run_id": run_id,
                    "checks": {"metrics_available": False},
                    "pass": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            item["case"] = metadata["case"]
            metrics.append(item)
        return metrics

    def cleanup_runs(self) -> dict[str, Any]:
        retained: list[str] = []
        cancelled: list[str] = []
        already_terminal: list[str] = []
        failures: list[dict[str, str]] = []
        try:
            self._discover_owned_runs()
        except Exception as exc:  # noqa: BLE001
            failures.append(
                {
                    "run_id": "<ownership-discovery>",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
        failures.extend(self.ownership_discovery_failures.values())
        for run_id in sorted(self.owned_runs):
            if self.args.keep_runs:
                retained.append(run_id)
                continue
            try:
                state = self.inspector.run_state(run_id)
            except Exception as exc:  # noqa: BLE001
                failures.append(
                    {"run_id": run_id, "error": f"{type(exc).__name__}: {exc}"}
                )
                continue
            if state in TERMINAL_STATES:
                already_terminal.append(run_id)
                continue
            try:
                result = self.runner(
                    [
                        str(SCRIPT_DIR / "frun"),
                        "cancel",
                        run_id,
                        "--reason",
                        f"live validation cleanup {self.campaign_id}",
                    ],
                    text=True,
                    capture_output=True,
                    env=self.env,
                    timeout=60,
                    check=False,
                )
            except Exception as exc:  # noqa: BLE001
                failures.append(
                    {
                        "run_id": run_id,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                continue
            if int(result.returncode) != 0:
                failures.append(
                    {
                        "run_id": run_id,
                        "error": bounded(result.stderr or result.stdout)
                        or "cancel failed",
                    }
                )
                continue
            try:
                final_state = self.inspector.run_state(run_id)
            except Exception as exc:  # noqa: BLE001
                failures.append(
                    {"run_id": run_id, "error": f"{type(exc).__name__}: {exc}"}
                )
                continue
            if final_state != "cancelled":
                failures.append(
                    {
                        "run_id": run_id,
                        "error": f"unexpected cleanup state: {final_state}",
                    }
                )
            else:
                cancelled.append(run_id)

        result = (
            "NOT_RUN" if self.args.keep_runs else ("PASS" if not failures else "FAIL")
        )
        evidence = {
            "result": result,
            "owned_run_ids": sorted(self.owned_runs),
            "already_terminal": already_terminal,
            "cancelled": cancelled,
            "retained": retained,
            "failures": failures,
        }
        self._record(
            "validator_owned_run_cleanup",
            category="plumbing",
            contract_result=(
                "PASS"
                if result == "PASS"
                else ("NOT_EVALUATED" if result == "NOT_RUN" else "FAIL")
            ),
            observed_disposition=(
                "suppressed" if self.args.keep_runs else "terminalized"
            ),
            details=evidence,
        )
        return evidence

    def execute(self) -> int:
        exit_override: int | None = None
        try:
            if not self.preflight():
                exit_override = 2
            else:
                self.validate_retired_smart_options()
                self.run_unprepared_rejection()
                self.run_provider_failure()
                if self.args.profile == "failure-path":
                    self.run_valkey_loss_capability()
                else:
                    self.run_fresearch("fresearch_academic", BENCHMARKS["academic"])
                    if self.args.profile == "full":
                        self.run_fresearch("fresearch_simple", BENCHMARKS["simple"])
                        self.run_fresearch("fresearch_termux", BENCHMARKS["termux"])
                        self.run_valkey_loss_capability()
                        self.run_public_fsearch()
        except Exception as exc:  # noqa: BLE001
            self._record(
                "validator_execution_error",
                category="plumbing",
                contract_result="FAIL",
                observed_disposition="execution_failed",
                returncode=2,
                stderr=f"{type(exc).__name__}: {exc}",
            )
            exit_override = 2
        return self.finish(exit_override=exit_override)

    def _accounting(self) -> dict[str, int]:
        matrix = [case for case in self.cases if case["category"] == "matrix"]
        plumbing = [case for case in self.cases if case["category"] == "plumbing"]
        not_run = [
            case for case in self.cases if case["contract_result"] == "NOT_EVALUATED"
        ]
        return {
            "declared_matrix_cases": len(matrix),
            "executed_matrix_cases": sum(
                case["contract_result"] != "NOT_EVALUATED" for case in matrix
            ),
            "plumbing_operations": len(plumbing),
            "not_run_cases": len(not_run),
            "failed_cases": sum(case["status"] == "fail" for case in self.cases),
            "passed_contract_cases": sum(
                case["contract_result"] == "PASS" for case in self.cases
            ),
            "successful_capability_cases": sum(
                case["capability_result"] == "PASS" for case in self.cases
            ),
        }

    def _report_markdown(self, manifest: dict[str, Any]) -> str:
        lines = [
            f"# Firecrawl live validation: {self.campaign_id}",
            "",
            f"- Profile: `{self.args.profile}`",
            f"- Host evidence: `{manifest['host_evidence']}`",
            f"- Cleanup: `{manifest['cleanup']['result']}`",
            f"- Operations: `{manifest['operations']['count']}/{manifest['operations']['max']}`",
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
            "`host_evidence=PASS` means every required contract assertion passed, "
            "every designated positive capability actually succeeded, required run-scoped "
            "corpus/blob/index/Qdrant integrity passed, validator-owned nonterminal runs "
            "were terminalized, and monitored temporary storage is clean.",
        ]
        return "\n".join(lines) + "\n"

    def finish(self, *, exit_override: int | None = None) -> int:
        try:
            self._discover_owned_runs()
        except Exception as exc:  # noqa: BLE001
            self._record(
                "validator_run_discovery",
                category="plumbing",
                contract_result="FAIL",
                observed_disposition="discovery_failed",
                stderr=f"{type(exc).__name__}: {exc}",
            )
        quality_metrics = self.collect_quality_metrics()
        quality_required = any(
            metadata.get("quality_required") for metadata in self.owned_runs.values()
        )
        quality_pass = (
            bool(quality_metrics) and all(item.get("pass") for item in quality_metrics)
            if quality_required
            else True
        )
        cleanup = (
            self.cleanup_runs()
            if self.run_baseline_captured
            else {
                "result": "PASS",
                "owned_run_ids": [],
                "already_terminal": [],
                "cancelled": [],
                "retained": [],
                "failures": [],
            }
        )
        required_contracts = all(
            case["contract_result"] == "PASS"
            for case in self.cases
            if case["required_contract"]
        )
        required_capabilities = all(
            case["capability_result"] == "PASS"
            for case in self.cases
            if case["required_capability"]
        )
        tmp_clean = not self._temporary_entries()
        host_pass = (
            required_contracts
            and required_capabilities
            and quality_pass
            and cleanup["result"] == "PASS"
            and tmp_clean
        )
        manifest = {
            "schema_version": "live-validation-v2",
            "campaign_id": self.campaign_id,
            "profile": self.args.profile,
            "implementation_head_sha": self.implementation_head,
            "duration_seconds": round(time.monotonic() - self.started, 2),
            "operations": self.operation_data(),
            "retry_policy": {
                "max_attempts_per_case": 1,
                "automatic_retry": False,
                "note": "Retries require an explicit new validation invocation.",
            },
            "cases": self.cases,
            "accounting": self._accounting(),
            "quality_metrics": quality_metrics,
            "quality_result": "PASS" if quality_pass else "FAIL",
            "cleanup": cleanup,
            "monitored_tmp_clean": tmp_clean,
            "host_evidence": "PASS" if host_pass else "FAIL",
            "host_evidence_semantics": (
                "PASS requires required contract conformance, designated capability success, "
                "required corpus/quality integrity, validator-owned run cleanup, and monitored "
                "temporary-storage cleanliness."
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
        if exit_override is not None:
            return exit_override
        return 0 if host_pass else 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--api-url",
        default=os.environ.get("FIRECRAWL_API_URL", "http://garion.us:3002"),
    )
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL", ""))
    parser.add_argument("--qdrant-url", default=os.environ.get("QDRANT_URL"))
    parser.add_argument("--qdrant-api-key", default=os.environ.get("QDRANT_API_KEY"))
    parser.add_argument(
        "--blob-root", default=os.environ.get("BLOB_ROOT", "data/blobs")
    )
    parser.add_argument("--max-operations", type=int)
    parser.add_argument("--case-timeout", type=int, default=1800)
    parser.add_argument("--worker-timeout", type=float, default=90.0)
    parser.add_argument(
        "--expected-head-sha",
        default=os.environ.get("FIRECRAWL_VALIDATION_HEAD_SHA"),
    )
    parser.add_argument("--artifact-root")
    parser.add_argument("--run-id")
    parser.add_argument("--keep-runs", action="store_true")
    parser.add_argument(
        "--profile",
        choices=tuple(PROFILE_OPERATION_CAPS),
        default="focused",
    )
    parser.add_argument("--disposable-namespace", default="fc_live_fault")
    parser.add_argument("--disposable-pg-port", type=int, default=55436)
    parser.add_argument("--disposable-qdrant-port", type=int, default=55437)
    args = parser.parse_args(argv)
    cap = PROFILE_OPERATION_CAPS[args.profile]
    if args.max_operations is None:
        args.max_operations = cap
    if not 1 <= args.max_operations <= cap:
        parser.error(
            f"--max-operations must be between 1 and {cap} for profile {args.profile}"
        )
    if args.case_timeout < 1:
        parser.error("--case-timeout must be positive")
    if args.worker_timeout < 0:
        parser.error("--worker-timeout must be non-negative")
    if args.expected_head_sha and not re.fullmatch(
        r"[0-9a-fA-F]{40}", args.expected_head_sha
    ):
        parser.error("--expected-head-sha must be a 40-character Git SHA")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,47}", args.disposable_namespace):
        parser.error("--disposable-namespace has invalid format")
    for name in ("disposable_pg_port", "disposable_qdrant_port"):
        port = int(getattr(args, name))
        if not 1024 <= port <= 65535:
            parser.error(f"--{name.replace('_', '-')} must be between 1024 and 65535")
    if args.disposable_pg_port == args.disposable_qdrant_port:
        parser.error("disposable PostgreSQL and Qdrant ports must differ")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.profile == "destructive":
        from live_validation_destructive import DisposableDestructiveCampaign

        return DisposableDestructiveCampaign(args).execute()
    campaign = Campaign(args)
    try:
        return campaign.execute()
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    finally:
        campaign.close()


__all__ = [
    "AuthoritativeInspector",
    "Campaign",
    "PROFILE_OPERATION_CAPS",
    "RETIRED_SMART_OPTIONS",
    "main",
    "parse_args",
]


if __name__ == "__main__":
    raise SystemExit(main())
