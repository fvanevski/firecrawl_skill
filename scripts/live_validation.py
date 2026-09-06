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
# __GHDEV_APPEND__
