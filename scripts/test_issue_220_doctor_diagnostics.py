from __future__ import annotations

import errno
import sys
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Self
from uuid import UUID

import pytest
from research_store import cli
from research_store import doctor_diagnostics as diagnostics
from research_store.blob import ContentAddressedBlobStore
from research_store.doctor_command import parser as doctor_parser


class _Cursor:
    def __init__(self, blob_rows: list[tuple[UUID, str]]) -> None:
        self.blob_rows = blob_rows
        self.query = ""

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, query: str, _params: object = None) -> None:
        self.query = " ".join(query.lower().split())

    def fetchall(self) -> list[tuple[Any, ...]]:
        if "from asset_snapshots" in self.query:
            return list(self.blob_rows)
        if "from index_jobs" in self.query:
            return []
        raise AssertionError(f"unexpected fetchall query: {self.query}")

    def fetchone(self) -> tuple[Any, ...]:
        if "from ingestion_batches" in self.query:
            return (0, None)
        raise AssertionError(f"unexpected fetchone query: {self.query}")


class _Connection:
    def __init__(self, blob_rows: list[tuple[UUID, str]]) -> None:
        self.blob_rows = blob_rows

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def cursor(self) -> _Cursor:
        return _Cursor(self.blob_rows)


class _WorkerUow:
    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def worker_status(self) -> dict[str, Any]:
        return {
            "workers": [{"heartbeat_at": datetime.now(timezone.utc)}],
            "active_leases": 0,
            "dead_jobs": 0,
            "stale_leases": 0,
        }


class _Qdrant:
    def __init__(self, point_id: UUID) -> None:
        self.point_id = point_id

    def list_aliases(self) -> dict[str, str]:
        return {"research_chunks_active": "collection-a"}

    def inspect_schema(self) -> dict[str, Any]:
        return {"exists": True, "compatible": True}

    def point_ids(self, _offset: object, **_kwargs: object) -> dict[str, Any]:
        return {
            "points": [{"id": str(self.point_id)}],
            "next_page_offset": None,
        }


class _RedisClient:
    def ping(self) -> bool:
        return True


class _Redis:
    @staticmethod
    def from_url(_url: str) -> _RedisClient:
        return _RedisClient()


def _config(blob_root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        blob_root=blob_root,
        qdrant_alias="research_chunks_active",
        embedding_fingerprint="fingerprint-a",
        physical_collection="collection-a",
        worker_poll_seconds=5,
        valkey_url="redis://127.0.0.1:6379/0",
        embedding_url="http://127.0.0.1:8000/v1",
        embedding_model="embedding-model",
        embedding_api_key="",
        embedding_dimension=2,
        reranker_url="http://127.0.0.1:8001/v1",
        reranker_model="reranker-model",
        reranker_api_key="",
        normalization_version="normalization-v1",
        parser_version="parser-v1",
        chunker_version="chunker-v1",
    )


def _install_healthy_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    blob_root: Path,
    *,
    index_fingerprint: str = "fingerprint-a",
) -> tuple[SimpleNamespace, UUID, str]:
    store = ContentAddressedBlobStore(blob_root)
    referenced = store.put(BytesIO(b"referenced payload"))
    orphan = store.put(BytesIO(b"unrelated orphan payload"))
    snapshot_id = UUID(int=220)
    point_id = UUID(int=221)
    config = _config(blob_root)

    monkeypatch.setattr(
        cli,
        "_schema_state",
        lambda _config: {"current": "head", "head": "head", "at_head": True},
    )
    monkeypatch.setattr(
        cli,
        "_db",
        lambda _config: _Connection([(snapshot_id, referenced.sha256)]),
    )
    monkeypatch.setattr(cli, "_uow_factory", lambda _config: _WorkerUow)
    qdrant = _Qdrant(point_id)
    monkeypatch.setattr(cli, "_qdrant", lambda *_args, **_kwargs: qdrant)
    monkeypatch.setattr(
        cli,
        "_index_rows",
        lambda _config: [
            {
                "physical_collection": "collection-a",
                "fingerprint": index_fingerprint,
                "dimension": 2,
                "distance_metric": "Cosine",
            }
        ],
    )
    monkeypatch.setattr(cli, "_active_chunk_ids", lambda _config: {point_id})
    monkeypatch.setattr(
        cli,
        "_index_reconcile",
        lambda _config, repair=False: {
            "ok": True,
            "total_active_chunks": 1,
            "definitions": [{}],
            "discrepancies": [],
        },
    )
    monkeypatch.setattr(
        diagnostics,
        "OpenAICompatibleEmbedder",
        lambda *_args, **_kwargs: lambda _text: [1.0, 0.0],
    )
    monkeypatch.setattr(
        diagnostics,
        "CohereCompatibleReranker",
        lambda *_args, **_kwargs: (
            lambda *_call_args, **_call_kwargs: [{"candidate_id": "relevant"}]
        ),
    )
    monkeypatch.setitem(sys.modules, "redis", SimpleNamespace(Redis=_Redis))
    return config, point_id, orphan.sha256


def test_doctor_reports_seven_independent_domains_and_orphan_warning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config, _point_id, orphan_sha = _install_healthy_dependencies(monkeypatch, tmp_path)

    checks, failed = diagnostics.doctor(config)

    assert checks["schema_version"] == diagnostics.DOCTOR_SCHEMA_VERSION
    assert set(diagnostics.DOCTOR_DOMAINS) <= checks.keys()
    assert checks["postgres_authority"]["status"] == "pass"
    assert checks["referenced_blob_integrity"]["status"] == "pass"
    assert checks["referenced_blob_integrity"]["missing_or_corrupt"] == []
    assert checks["unreferenced_blob_inventory"]["status"] == "warning"
    assert checks["unreferenced_blob_inventory"]["orphan_count"] == 1
    assert orphan_sha in checks["unreferenced_blob_inventory"]["unreferenced"]
    assert checks["index_job_health"]["status"] == "pass"
    assert checks["qdrant_projection"]["status"] == "pass"
    assert checks["worker_health"]["status"] == "pass"
    assert checks["environment_connectivity"]["status"] == "pass"
    assert failed is False, "global orphan inventory must not fail run-specific health"

    human = diagnostics.format_human(checks)
    for domain in diagnostics.DOCTOR_DOMAINS:
        assert f"{domain}: {checks[domain]['status']}" in human


def test_qdrant_coverage_success_cannot_erase_compatibility_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config, _point_id, _orphan_sha = _install_healthy_dependencies(
        monkeypatch,
        tmp_path,
        index_fingerprint="wrong-fingerprint",
    )

    checks, failed = diagnostics.doctor(config)

    projection = checks["qdrant_projection"]
    assert projection["coverage"] == {"missing": 0, "orphaned": 0}
    assert projection["status"] == "failure"
    assert any(
        issue["reason_code"] == "embedding_fingerprint_mismatch"
        for issue in projection["issues"]
    )
    assert failed is True


@pytest.mark.parametrize(
    ("exc", "component", "expected"),
    [
        (
            PermissionError(errno.EPERM, "Operation not permitted"),
            "embedding",
            "network_policy_denial",
        ),
        (
            RuntimeError("connect ECONNREFUSED 127.0.0.1:5432"),
            "postgres_authority",
            "server_unavailable",
        ),
        (
            RuntimeError("password authentication failed for user research"),
            "postgres_authority",
            "credential_failure",
        ),
        (
            OSError(errno.ENETUNREACH, "Network unreachable"),
            "embedding",
            "network_namespace_denial",
        ),
        (
            RuntimeError("permission denied for table index_jobs"),
            "postgres_authority",
            "database_rejection",
        ),
        (
            RuntimeError("unexpected query runtime error"),
            "embedding",
            "query_runtime_failure",
        ),
    ],
)
def test_connectivity_classes_are_distinct_and_actionable(
    exc: BaseException,
    component: str,
    expected: str,
) -> None:
    result = diagnostics.classify_connectivity_failure(exc, component=component)
    assert result["status"] == "failure"
    assert result["reason_code"] == expected
    assert result["remediation"]


def test_server_unavailable_compact_errno_is_not_captured_as_policy_denial() -> None:
    result = diagnostics.classify_connectivity_failure(
        RuntimeError("connect failed errno111"),
        component="embedding",
    )
    assert result["reason_code"] == "server_unavailable"


def test_doctor_distinguishes_sandbox_denial_from_database_rejection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config, _point_id, _orphan_sha = _install_healthy_dependencies(
        monkeypatch, tmp_path
    )

    monkeypatch.setattr(
        cli,
        "_schema_state",
        lambda _config: (_ for _ in ()).throw(
            PermissionError("Operation not permitted")
        ),
    )
    sandbox, _failed = diagnostics.doctor(config)
    assert sandbox["postgres_authority"]["reason_code"] == "network_policy_denial"

    monkeypatch.setattr(
        cli,
        "_schema_state",
        lambda _config: (_ for _ in ()).throw(
            RuntimeError("permission denied for table index_jobs")
        ),
    )
    rejected, _failed = diagnostics.doctor(config)
    assert rejected["postgres_authority"]["reason_code"] == "database_rejection"


def test_diagnostic_details_redact_credentials() -> None:
    result = diagnostics.classify_connectivity_failure(
        RuntimeError(
            "authentication failed password=supersecret api_key=abc123 "
            "Authorization: Bearer bearer-secret "
            "postgresql://research:urlsecret@db.invalid/research"
        ),
        component="postgres_authority",
    )
    detail = result["detail"]
    for secret in ("supersecret", "abc123", "bearer-secret", "urlsecret"):
        assert secret not in detail
    assert "[REDACTED]" in detail


def test_doctor_human_parser_and_shell_route_are_explicit() -> None:
    assert doctor_parser().parse_args(["--human"]).human is True
    shell = Path(__file__).with_name("research-db").read_text(encoding="utf-8")
    assert '"${1:-}" == "doctor"' in shell
    assert "research_store.doctor_command" in shell


def test_reset_clean_state_contract_uses_doctor_v1_domains() -> None:
    reset = (
        Path(__file__).with_name("reset-firecrawl-research").read_text(encoding="utf-8")
    )
    for expected in (
        '.schema_version == "doctor-diagnostics-v1"',
        ".postgres_authority.status",
        ".referenced_blob_integrity.status",
        ".unreferenced_blob_inventory.status",
        ".qdrant_projection.status",
        ".index_job_health.status",
        ".environment_connectivity.status",
        ".worker_health.status",
    ):
        assert expected in reset
    for stale in (
        ".blobs.ok",
        ".qdrant.ok",
        ".index_reconcile.ok",
        ".valkey.ok",
        ".worker.current_worker_available",
    ):
        assert stale not in reset
