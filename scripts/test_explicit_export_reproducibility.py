from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from research_store import cli as store_cli
from research_store.config import StoreConfig
from research_store.container import build_run_service, build_service
from research_store.domain import IngestRequest
from research_store.index_census import CENSUS_CLASSES, census_index_jobs
from research_store.postgres import connect, migrate, require_disposable_database_reset
from research_store.run_integrity_export import SECTION_ITEM_LIMIT

TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL")
requires_postgres = pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)


@pytest.fixture(scope="module")
def prepared_export_database():
    if not TEST_DSN:
        pytest.skip("requires explicit disposable PostgreSQL test DSN")
    require_disposable_database_reset(
        TEST_DSN, os.environ.get("RESEARCH_STORE_TEST_ALLOW_RESET", "")
    )
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute("DROP SCHEMA public CASCADE")
        cursor.execute("CREATE SCHEMA public")
    assert migrate(TEST_DSN) >= 44


def _config(tmp_path: Path) -> StoreConfig:
    return replace(
        StoreConfig.from_env(),
        database_url=TEST_DSN,
        blob_root=tmp_path / "blobs",
        qdrant_collection=f"export_test_{uuid4().hex[:12]}",
        embedding_dimension=4,
    )


def _assert_canonical_file(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    assert text.endswith("\n")
    payload = json.loads(text)
    assert list(payload) == sorted(payload)
    return payload


def _configure_env(monkeypatch, config: StoreConfig) -> None:
    monkeypatch.setenv("DATABASE_URL", TEST_DSN)
    monkeypatch.setenv("BLOB_ROOT", str(config.blob_root))


def _create_run_with_asset(config: StoreConfig, label: str):
    external_id = f"fr_{label}_{uuid4().hex}"
    run = build_run_service(config).create(
        f"{label} run",
        external_id,
        execution_mode="autonomous_local",
    )
    invocation_id = f"fc_{uuid4().hex}"
    build_service(config).ingest_batch(
        invocation_id,
        "scrape",
        [IngestRequest(f"https://{label}.example/item", f"# {label}\n\nEvidence.".encode())],
        research_run_external_id=external_id,
    )
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT c.id,d.fingerprint,j.id,m.id
                 FROM research_run_assets a
                 JOIN documents doc ON doc.snapshot_id=a.snapshot_id
                 JOIN chunks c ON c.document_id=doc.id
                 JOIN embedding_manifests m ON m.chunk_id=c.id
                 JOIN index_definitions d ON d.id=m.index_definition_id
                 JOIN index_jobs j ON j.manifest_id=m.id
                WHERE a.run_id=%s ORDER BY c.id""",
            (run.id,),
        )
        rows = cursor.fetchall()
    assert rows
    return run, external_id, rows


def _install_checkpoint(run_id: UUID, rows) -> tuple[list[UUID], str]:
    entity_ids = [UUID(str(row[0])) for row in rows]
    fingerprint = str(rows[0][1])
    digest = hashlib.sha256(
        "\n".join(sorted(str(item) for item in entity_ids)).encode("utf-8")
    ).hexdigest()
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT lifecycle_revision FROM research_runs WHERE id=%s", (run_id,)
        )
        revision = int(cursor.fetchone()[0])
        cursor.execute(
            """INSERT INTO indexing_checkpoints(
                   run_id,lifecycle_revision,fingerprint,entity_ids,
                   expected_membership_sha256,expected_count,idempotency_key)
                 VALUES(%s,%s,%s,%s,%s,%s,%s)""",
            (
                run_id,
                revision,
                fingerprint,
                entity_ids,
                digest,
                len(entity_ids),
                f"integrity-test:{uuid4()}",
            ),
        )
    return entity_ids, fingerprint


def test_explicit_export_serializer_is_canonical_and_atomic(tmp_path):
    target = tmp_path / "export.json"
    payload = {
        "z": UUID("00000000-0000-0000-0000-000000000002"),
        "a": {"when": datetime(2026, 8, 3, tzinfo=timezone.utc)},
    }
    store_cli._export_json(target, payload)
    parsed = _assert_canonical_file(target)
    assert list(parsed["a"]) == ["when"]
    assert parsed["z"] == "00000000-0000-0000-0000-000000000002"
    assert list(tmp_path.glob(".export.json.*.tmp")) == []


def test_explicit_export_cleans_temporary_file_after_replace_failure(tmp_path, monkeypatch):
    target = tmp_path / "export.json"

    def fail_replace(*_args):
        raise OSError("simulated atomic replace failure")

    monkeypatch.setattr(store_cli.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated atomic replace failure"):
        store_cli._export_json(target, {"value": 1})
    assert not target.exists()
    assert list(tmp_path.glob(".export.json.*.tmp")) == []


@requires_postgres
def test_export_invocation_is_byte_reproducible_and_read_only(
    tmp_path, monkeypatch, capsys, prepared_export_database
):
    config = _config(tmp_path)
    external_run_id = f"fr_export_invocation_{uuid4().hex}"
    run = build_run_service(config).create(
        "Explicit invocation export reproducibility",
        external_run_id,
        execution_mode="autonomous_local",
    )
    invocation_id = f"fc_{uuid4().hex}"
    build_service(config).ingest_batch(
        invocation_id,
        "scrape",
        [
            IngestRequest("https://export.example/second", b"# Second\n\nSecond."),
            IngestRequest("https://export.example/first", b"# First\n\nFirst."),
        ],
        research_run_external_id=external_run_id,
    )
    _configure_env(monkeypatch, config)
    first, second = tmp_path / "invocation-one.json", tmp_path / "invocation-two.json"
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT b.status,b.completed_at,count(a.id),r.lifecycle_revision
                 FROM ingestion_batches b JOIN research_runs r ON r.id=b.research_run_id
                 LEFT JOIN ingestion_batch_assets a ON a.batch_id=b.id
                WHERE b.invocation_id=%s
                GROUP BY b.status,b.completed_at,r.lifecycle_revision""",
            (invocation_id,),
        )
        before = cursor.fetchone()
    assert store_cli.main(["export-invocation", invocation_id, "--output", str(first)]) == 0
    capsys.readouterr()
    assert store_cli.main(["export-invocation", invocation_id, "--output", str(second)]) == 0
    capsys.readouterr()
    assert first.read_bytes() == second.read_bytes()
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT b.status,b.completed_at,count(a.id),r.lifecycle_revision
                 FROM ingestion_batches b JOIN research_runs r ON r.id=b.research_run_id
                 LEFT JOIN ingestion_batch_assets a ON a.batch_id=b.id
                WHERE b.invocation_id=%s
                GROUP BY b.status,b.completed_at,r.lifecycle_revision""",
            (invocation_id,),
        )
        assert cursor.fetchone() == before
    assert run.lifecycle_revision == before[3]


@requires_postgres
def test_export_run_v1_remains_byte_reproducible(tmp_path, monkeypatch, prepared_export_database):
    config = _config(tmp_path)
    external_id = f"fr_export_v1_{uuid4().hex}"
    run = build_run_service(config).create("v1 compatibility", external_id, execution_mode="autonomous_local")
    same_time = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """INSERT INTO retrieval_events(id,run_id,stage,query,created_at)
                 VALUES (%s,%s,'semantic','second',%s),(%s,%s,'lexical','first',%s)""",
            (UUID(int=2), run.id, same_time, UUID(int=1), run.id, same_time),
        )
    _configure_env(monkeypatch, config)
    first, second = tmp_path / "v1-a.json", tmp_path / "v1-b.json"
    args = ["export-run", external_id, "--schema-version", "export-run-v1"]
    assert store_cli.main([*args, "--output", str(first)]) == 0
    assert store_cli.main([*args, "--output", str(second)]) == 0
    assert first.read_bytes() == second.read_bytes()
    payload = _assert_canonical_file(first)
    assert payload["schema_version"] == "export-run-v1"
    assert [event["id"] for event in payload["retrieval_events"]] == [str(UUID(int=1)), str(UUID(int=2))]


@requires_postgres
def test_export_run_v2_is_bounded_snapshot_read_only_and_rejects_fake_versions(
    tmp_path, monkeypatch, prepared_export_database
):
    config = _config(tmp_path)
    external_id = f"fr_export_v2_{uuid4().hex}"
    run = build_run_service(config).create("v2 export", external_id, execution_mode="autonomous_local")
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        for index in range(SECTION_ITEM_LIMIT + 5):
            cursor.execute(
                "INSERT INTO retrieval_events(run_id,stage,query) VALUES(%s,'lexical',%s)",
                (run.id, f"q-{index}"),
            )
    _configure_env(monkeypatch, config)
    first, second = tmp_path / "v2-a.json", tmp_path / "v2-b.json"
    assert store_cli.main(["export-run", external_id, "--output", str(first)]) == 0
    assert store_cli.main(["export-run", external_id, "--output", str(second)]) == 0
    payload = _assert_canonical_file(first)
    assert payload["schema_version"] == "export-run-v2"
    assert payload["snapshot_transaction"] == {"isolation": "repeatable read", "read_only": True}
    section = payload["sections"]["retrieval_events"]
    assert section["exact_count"] == SECTION_ITEM_LIMIT + 5
    assert len(section["items"]) == SECTION_ITEM_LIMIT
    assert section["truncated"] is True
    assert len(section["sha256"]) == 64
    assert first.read_bytes() == second.read_bytes()
    with pytest.raises(SystemExit) as exc:
        store_cli.main([
            "export-run", external_id, "--output", str(tmp_path / "bad.json"),
            "--schema-version", "export-run-custom",
        ])
    assert exc.value.code == 2


@requires_postgres
def test_integrity_is_run_scoped_and_ignores_other_run_leases(
    tmp_path, monkeypatch, prepared_export_database
):
    config = _config(tmp_path)
    target, target_external, target_rows = _create_run_with_asset(config, "target")
    other, _other_external, other_rows = _create_run_with_asset(config, "other")
    target_ids, _fingerprint = _install_checkpoint(target.id, target_rows)
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        other_job = other_rows[0][2]
        other_manifest = other_rows[0][3]
        cursor.execute(
            """UPDATE index_jobs SET status='running',attempt_count=1,started_at=now(),
                      lease_token=gen_random_uuid(),lease_owner='other-worker',
                      lease_expires_at=now()+interval '10 minutes' WHERE id=%s""",
            (other_job,),
        )
        cursor.execute("UPDATE embedding_manifests SET index_status='pending' WHERE id=%s", (other_manifest,))
        cursor.execute(
            "INSERT INTO index_worker_heartbeats(worker_id,heartbeat_at) VALUES('other-worker',now()) ON CONFLICT(worker_id) DO UPDATE SET heartbeat_at=excluded.heartbeat_at"
        )
    _configure_env(monkeypatch, config)
    output = tmp_path / "scoped.json"
    assert store_cli.main(["integrity", target_external, "--output", str(output)]) == 0
    payload = _assert_canonical_file(output)
    assert payload["index_job_census"]["expected"] == len(target_ids)
    assert payload["sections"]["index_jobs"]["exact_count"] == len(target_ids)
    assert payload["sections"]["active_leases"]["exact_count"] == 0
    assert all(item.get("worker_id") != "other-worker" for item in payload["sections"]["relevant_worker_heartbeats"]["items"])
    assert payload["qdrant_projection_reconciliation"]["authoritative_for_completion"] is False
    assert other.id != target.id


@requires_postgres
def test_integrity_redacts_entire_artifact_and_stdout(tmp_path, monkeypatch, capsys, prepared_export_database):
    config = _config(tmp_path)
    run, external_id, rows = _create_run_with_asset(config, "redact")
    _install_checkpoint(run.id, rows)
    access_key = "AKIAIOSFODNN7EXAMPLE"
    signature = "super-secret-signature"
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """UPDATE asset_snapshots SET raw_blob_uri=%s
                 WHERE id IN (SELECT snapshot_id FROM research_run_assets WHERE run_id=%s)""",
            (f"s3://bucket/blob?AWSAccessKeyId={access_key}&Signature={signature}", run.id),
        )
        cursor.execute(
            "UPDATE research_runs SET metadata=metadata || %s::jsonb WHERE id=%s",
            (json.dumps({"authorization": "Bearer top-secret-token"}), run.id),
        )
    _configure_env(monkeypatch, config)
    output = tmp_path / "redacted.json"
    assert store_cli.main(["integrity", external_id, "--output", str(output)]) == 0
    stdout = capsys.readouterr().out
    text = output.read_text(encoding="utf-8")
    for secret in (access_key, signature, "top-secret-token"):
        assert secret not in text
        assert secret not in stdout
    assert "***REDACTED***" in text
    assert '"status":"written"' in stdout.replace(" ", "")


@requires_postgres
def test_integrity_reports_running_live_at_terminal_then_later_completion(
    tmp_path, monkeypatch, prepared_export_database
):
    config = _config(tmp_path)
    run, external_id, rows = _create_run_with_asset(config, "late")
    entity_ids, fingerprint = _install_checkpoint(run.id, rows)
    job_id, manifest_id = rows[0][2], rows[0][3]
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """UPDATE index_jobs SET status='running',attempt_count=1,
                      started_at=now()-interval '1 minute',lease_token=gen_random_uuid(),
                      lease_owner='late-worker',lease_expires_at=now()+interval '10 minutes',
                      completed_at=NULL,error=NULL WHERE id=%s""",
            (job_id,),
        )
        cursor.execute("UPDATE embedding_manifests SET index_status='pending',indexed_at=NULL,error=NULL WHERE id=%s", (manifest_id,))
        cursor.execute(
            "INSERT INTO index_worker_heartbeats(worker_id,heartbeat_at) VALUES('late-worker',now()) ON CONFLICT(worker_id) DO UPDATE SET heartbeat_at=excluded.heartbeat_at"
        )
        census = census_index_jobs(connection, entity_ids, fingerprint)
    assert census["running_live"] == 1
    service = build_run_service(config)
    planning = service.transition(
        run.id, "planning", expected_revision=run.lifecycle_revision,
        idempotency_key=f"planning:{uuid4()}", actor_type="test",
    )
    service.fail(
        run.id,
        expected_revision=planning.lifecycle_revision,
        idempotency_key=f"failed:{uuid4()}",
        actor_type="test",
        reason="audited live job terminalization",
        completion={"state_census": census},
    )
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT created_at FROM terminal_decisions WHERE run_id=%s ORDER BY created_at DESC LIMIT 1", (run.id,))
        decision_at = cursor.fetchone()[0]
        finished_at = decision_at + timedelta(seconds=5)
        cursor.execute(
            """UPDATE index_jobs SET status='complete',lease_token=NULL,lease_owner=NULL,
                      lease_expires_at=NULL,completed_at=%s,error=NULL WHERE id=%s""",
            (finished_at, job_id),
        )
        cursor.execute(
            "UPDATE embedding_manifests SET index_status='complete',indexed_at=%s,error=NULL WHERE id=%s",
            (finished_at, manifest_id),
        )
    _configure_env(monkeypatch, config)
    output = tmp_path / "late.json"
    assert store_cli.main(["integrity", external_id, "--output", str(output)]) == 0
    payload = _assert_canonical_file(output)
    timing = payload["terminal_index_timing"]
    assert timing["terminal_census_running_live_count"] == 1
    assert timing["completed_after_decision"]["exact_count"] == 1
    assert timing["spanning_terminal_decision_exact_count"] == 1
    assert timing["historical_identity_correlation"]["status"] == "inconclusive"
    assert payload["index_job_census"]["complete"] == 1


@requires_postgres
def test_integrity_golden_preserves_audited_1344_complete_plus_32_live_census(
    tmp_path, monkeypatch, prepared_export_database
):
    config = _config(tmp_path)
    external_id = f"fr_golden_{uuid4().hex}"
    run = build_run_service(config).create("audited 1344+32 state", external_id, execution_mode="autonomous_local")
    counts = {name: 0 for name in CENSUS_CLASSES}
    counts.update({"complete": 1344, "running_live": 32})
    terminal_census = {
        "schema_version": "index-job-census-v1",
        "available": True,
        "expected": 1376,
        "counts": counts,
        **counts,
    }
    service = build_run_service(config)
    planning = service.transition(
        run.id, "planning", expected_revision=run.lifecycle_revision,
        idempotency_key=f"planning:{uuid4()}", actor_type="test",
    )
    service.fail(
        run.id,
        expected_revision=planning.lifecycle_revision,
        idempotency_key=f"failed:{uuid4()}", actor_type="test",
        reason="audited late-32 fixture",
        completion={"state_census": terminal_census},
    )
    _configure_env(monkeypatch, config)
    output = tmp_path / "golden.json"
    assert store_cli.main(["integrity", external_id, "--output", str(output)]) == 0
    payload = _assert_canonical_file(output)
    persisted = payload["terminal_index_timing"]["terminal_decision"]["state_census"]
    assert persisted["expected"] == 1376
    assert persisted["counts"]["complete"] == 1344
    assert persisted["counts"]["running_live"] == 32
    assert persisted["counts"]["claimable"] == 0
    assert payload["diagnostics"]["domains"]["terminal_decision"]["status"] == "failure"


@requires_postgres
def test_integrity_exports_execution_mode_history(tmp_path, monkeypatch, prepared_export_database):
    config = _config(tmp_path)
    external_id = f"fr_mode_{uuid4().hex}"
    service = build_run_service(config)
    run = service.create("mode history", external_id, execution_mode="autonomous_local")
    service.change_execution_mode(
        run.id,
        "agent_led",
        expected_revision=run.lifecycle_revision,
        idempotency_key=f"mode:{uuid4()}",
        requested_by="review-test",
        approved_by="review-test",
        reason="exercise persisted mode history",
        actor_type="test",
    )
    _configure_env(monkeypatch, config)
    output = tmp_path / "mode.json"
    assert store_cli.main(["integrity", external_id, "--output", str(output)]) == 0
    payload = _assert_canonical_file(output)
    history = payload["sections"]["run_mode_history"]
    assert history["exact_count"] == 1
    event = history["items"][0]
    assert event["event_type"] == "run.execution_mode_changed"
    assert event["payload"]["next_mode"] == "agent_led"
