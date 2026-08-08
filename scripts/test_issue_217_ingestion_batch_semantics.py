"""Authoritative regression matrix for issue #217 (RC-11/RC-12/RC-13)."""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

import pytest
from asset_promotion_test_support import (
    _advance_to_indexing,
    _insert_candidate,
    _request,
)
from research_store.asset_promotion_service import AssetPromotionService
from research_store.bounded_orchestrator import BoundedExtractionStage
from research_store.config import StoreConfig
from research_store.container import (
    build_extraction_service,
    build_run_service,
    build_service,
    build_workflow_operation_service,
)
from research_store.domain import IngestRequest
from research_store.ingestion_batch_semantics import _finish_ingestion_batch
from research_store.postgres import PostgresUnitOfWork, connect, migrate

TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL")
requires_db = pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)
UTC = timezone.utc


def _config(tmp_path: Path, database_url: str | None = None) -> StoreConfig:
    return replace(
        StoreConfig.from_env(),
        database_url=database_url or TEST_DSN,
        blob_root=tmp_path / f"blobs-{uuid4().hex}",
        qdrant_collection=f"issue217_{uuid4().hex}",
        embedding_dimension=4,
    )


def _dsn_for_database(dsn: str, database: str) -> str:
    parsed = urlsplit(dsn)
    return urlunsplit(
        (parsed.scheme, parsed.netloc, f"/{database}", parsed.query, parsed.fragment)
    )


def test_issue_217_contract_is_installed_on_canonical_uow():
    assert PostgresUnitOfWork.finish_ingestion_batch is _finish_ingestion_batch
    assert PostgresUnitOfWork.finish_ingestion_batch.__module__ == (
        "research_store.ingestion_batch_semantics"
    )


@requires_db
def test_direct_batch_persists_exact_constituent_min_max_and_member_ids(tmp_path: Path):
    migrate(TEST_DSN)
    service = build_service(_config(tmp_path))
    invocation_id = f"fc_issue217_direct_{uuid4().hex}"
    t0 = datetime(2026, 8, 8, 1, 0, tzinfo=UTC)
    t1 = t0 + timedelta(minutes=2)
    t2 = t0 + timedelta(minutes=7)
    t3 = t0 + timedelta(minutes=11)

    with service.uow_factory() as uow:
        batch_id = uow.start_ingestion_batch(invocation_id, "issue217_direct")
        uow.record_batch_asset(
            batch_id,
            0,
            "https://example.test/direct/0",
            "complete",
            constituent_started_at=t1,
            constituent_completed_at=t2,
        )
        uow.record_batch_asset(
            batch_id,
            1,
            "https://example.test/direct/1",
            "complete",
            constituent_started_at=t0,
            constituent_completed_at=t3,
        )

    manifest = service.finalize_ingestion_batch(str(batch_id), "complete")
    member_ids = [str(item["batch_asset_id"]) for item in manifest["assets"]]
    summary = manifest["outcome_summary"]

    assert manifest["started_at"] == t0
    assert manifest["completed_at"] == t3
    assert manifest["sealed_at"] is not None
    assert summary["schema_version"] == "ingestion-outcome-summary-v2"
    assert summary["member_count"] == 2
    assert summary["succeeded"] == 2
    assert summary["succeeded_ids"] == member_ids
    assert summary["failed_ids"] == []
    assert summary["cancelled_ids"] == []

    with service.uow_factory() as uow:
        canonical = uow.export_invocation(invocation_id)
    assert canonical["batch_id"] == manifest["batch_id"]
    assert canonical["sealed_at"] == manifest["sealed_at"]
    assert canonical["outcome_summary"] == summary
    assert [str(item["batch_asset_id"]) for item in canonical["assets"]] == member_ids


@requires_db
def test_v43_finalization_fails_closed_without_extraction_terminal_evidence(
    tmp_path: Path,
):
    migrate(TEST_DSN)
    config = _config(tmp_path)
    runs = build_run_service(config)
    extraction = build_extraction_service(config)
    status = runs.create(
        "issue 217 missing terminal evidence",
        f"fr_issue217_missing_{uuid4().hex}",
        execution_mode="autonomous_local",
    )
    candidate_id = _insert_candidate(status.id, "missing-terminal")
    attempt_id = extraction.create_attempt(
        candidate_id,
        status.id,
        start_time=datetime(2026, 8, 8, 2, 0, tzinfo=UTC),
    )

    with runs.uow_factory() as uow:
        batch_id = uow.start_ingestion_batch(
            f"fc_issue217_missing_{uuid4().hex}", "issue217_missing"
        )
        uow.record_batch_asset(
            batch_id,
            0,
            "https://example.test/missing-terminal",
            "complete",
            extraction_attempt_id=attempt_id,
        )

    with (
        runs.uow_factory() as uow,
        pytest.raises(ValueError, match="lacks authoritative terminal time"),
    ):
        uow.finish_ingestion_batch(batch_id, "complete")

    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT status,sealed_at,completed_at FROM ingestion_batches WHERE id=%s",
            (batch_id,),
        )
        batch_status, sealed_at, completed_at = cursor.fetchone()
    assert batch_status == "running"
    assert sealed_at is None
    assert completed_at is None


@requires_db
def test_outcome_summary_uses_exact_attempt_outcomes_ids_and_failure_classes(
    tmp_path: Path,
):
    migrate(TEST_DSN)
    config = _config(tmp_path)
    runs = build_run_service(config)
    extraction = build_extraction_service(config)
    status = runs.create(
        "issue 217 exact outcome summary",
        f"fr_issue217_outcomes_{uuid4().hex}",
        execution_mode="autonomous_local",
    )
    candidates = [
        _insert_candidate(status.id, label)
        for label in ("success", "failure", "cancelled")
    ]
    start_times = [
        datetime(2026, 8, 8, 3, 0, tzinfo=UTC),
        datetime(2026, 8, 8, 3, 1, tzinfo=UTC),
        datetime(2026, 8, 8, 3, 2, tzinfo=UTC),
    ]
    end_times = [
        datetime(2026, 8, 8, 3, 6, tzinfo=UTC),
        datetime(2026, 8, 8, 3, 9, tzinfo=UTC),
        datetime(2026, 8, 8, 3, 12, tzinfo=UTC),
    ]
    attempt_ids = [
        extraction.create_attempt(candidate, status.id, start_time=start)
        for candidate, start in zip(candidates, start_times, strict=True)
    ]
    extraction.complete_attempt(attempt_ids[0], "succeeded", end_time=end_times[0])
    extraction.complete_attempt(
        attempt_ids[1],
        "failed",
        failure_class="http_error",
        end_time=end_times[1],
        error_message="classified test failure",
    )
    extraction.complete_attempt(
        attempt_ids[2],
        "cancelled",
        failure_class="timeout",
        end_time=end_times[2],
        error_message="classified test cancellation",
    )

    with runs.uow_factory() as uow:
        batch_id = uow.start_ingestion_batch(
            f"fc_issue217_outcomes_{uuid4().hex}", "issue217_outcomes"
        )
        uow.record_batch_asset(
            batch_id,
            0,
            "https://example.test/outcome/success",
            "complete",
            extraction_attempt_id=attempt_ids[0],
        )
        uow.record_batch_asset(
            batch_id,
            1,
            "https://example.test/outcome/failure",
            "failed",
            error="classified test failure",
            extraction_attempt_id=attempt_ids[1],
        )
        uow.record_batch_asset(
            batch_id,
            2,
            "https://example.test/outcome/cancelled",
            "failed",
            error="classified test cancellation",
            extraction_attempt_id=attempt_ids[2],
        )

    service = build_service(config)
    manifest = service.finalize_ingestion_batch(str(batch_id), "partial")
    by_ordinal = {item["ordinal"]: item for item in manifest["assets"]}
    summary = manifest["outcome_summary"]

    assert manifest["started_at"] == min(start_times)
    assert manifest["completed_at"] == max(end_times)
    assert summary["succeeded"] == 1
    assert summary["failed"] == 1
    assert summary["cancelled"] == 1
    assert summary["succeeded_ids"] == [str(by_ordinal[0]["batch_asset_id"])]
    assert summary["failed_ids"] == [str(by_ordinal[1]["batch_asset_id"])]
    assert summary["cancelled_ids"] == [str(by_ordinal[2]["batch_asset_id"])]
    assert summary["succeeded_extraction_attempt_ids"] == [str(attempt_ids[0])]
    assert summary["failed_extraction_attempt_ids"] == [str(attempt_ids[1])]
    assert summary["cancelled_extraction_attempt_ids"] == [str(attempt_ids[2])]
    assert summary["failure_classes"] == {
        "http_error": {
            "count": 1,
            "ids": [str(by_ordinal[1]["batch_asset_id"])],
        },
        "timeout": {
            "count": 1,
            "ids": [str(by_ordinal[2]["batch_asset_id"])],
        },
    }


@requires_db
def test_v43_retry_replaces_exact_member_summary_without_stale_accumulation(
    tmp_path: Path,
):
    migrate(TEST_DSN)
    service = build_service(_config(tmp_path))
    invocation_id = f"fc_issue217_retry_{uuid4().hex}"
    requests = [
        _request("retry-success"),
        {
            "requested_url": "https://example.test/retry-failure",
            "error": "synthetic retry failure",
        },
    ]

    first = service.ingest_batch(invocation_id, "issue217_retry", requests)
    first = service.finalize_ingestion_batch(first["batch_id"], "partial")
    first_summary = first["outcome_summary"]
    first_ids = {str(item["batch_asset_id"]) for item in first["assets"]}
    assert first_summary["member_count"] == 2
    assert first_summary["succeeded"] == 1
    assert first_summary["failed"] == 1
    assert (
        set(first_summary["succeeded_ids"] + first_summary["failed_ids"]) == first_ids
    )

    second = service.ingest_batch(invocation_id, "issue217_retry", requests)
    second = service.finalize_ingestion_batch(second["batch_id"], "partial")
    second_summary = second["outcome_summary"]
    second_ids = {str(item["batch_asset_id"]) for item in second["assets"]}

    assert second["batch_id"] == first["batch_id"]
    assert second_summary["member_count"] == 2
    assert second_summary["succeeded"] == 1
    assert second_summary["failed"] == 1
    assert second_summary["cancelled"] == 0
    assert (
        set(second_summary["succeeded_ids"] + second_summary["failed_ids"])
        == second_ids
    )
    assert first_ids.isdisjoint(second_ids)
    assert second_summary["failure_classes"] == {
        "ingestion_error": {
            "count": 1,
            "ids": second_summary["failed_ids"],
        }
    }

    with service.uow_factory() as uow:
        canonical = uow.export_invocation(invocation_id)
    assert canonical["outcome_summary"] == second_summary
    assert {str(item["batch_asset_id"]) for item in canonical["assets"]} == second_ids


@requires_db
def test_concurrent_insert_and_seal_produce_exact_serializable_membership(
    tmp_path: Path,
):
    migrate(TEST_DSN)
    service = build_service(_config(tmp_path))
    invocation_id = f"fc_issue217_race_{uuid4().hex}"
    initial = service.ingest_batch(
        invocation_id,
        "issue217_race",
        [_request("race-initial")],
    )
    batch_id = initial["batch_id"]
    barrier = threading.Barrier(2)

    def record_late() -> str:
        barrier.wait(timeout=5)
        try:
            with service.uow_factory() as uow:
                uow.record_batch_asset(
                    batch_id,
                    99,
                    "https://example.test/race/late",
                    "complete",
                )
        except ValueError as exc:
            assert "sealed" in str(exc)
            return "rejected"
        return "recorded"

    def seal() -> str:
        barrier.wait(timeout=5)
        service.finalize_ingestion_batch(batch_id, "complete")
        return "sealed"

    with ThreadPoolExecutor(max_workers=2) as executor:
        writer = executor.submit(record_late)
        sealer = executor.submit(seal)
        outcomes = [writer.result(timeout=10), sealer.result(timeout=10)]

    assert "sealed" in outcomes
    assert outcomes.count("recorded") + outcomes.count("rejected") == 1

    with service.uow_factory() as uow:
        manifest = uow.export_invocation_by_batch(batch_id)
    summary = manifest["outcome_summary"]
    expected_members = 2 if "recorded" in outcomes else 1
    assert summary["member_count"] == expected_members
    assert summary["succeeded"] == expected_members
    assert len(summary["succeeded_ids"]) == expected_members
    assert len(manifest["assets"]) == expected_members


@requires_db
def test_reused_snapshot_keeps_original_provenance_and_batch_uses_current_attempt(
    tmp_path: Path,
):
    migrate(TEST_DSN)
    config = _config(tmp_path)
    corpus = build_service(config)
    runs = build_run_service(config)
    extraction = build_extraction_service(config)
    status = runs.create(
        "issue 217 reused snapshot",
        f"fr_issue217_reuse_{uuid4().hex}",
        execution_mode="autonomous_local",
    )
    candidate_id = _insert_candidate(status.id, "reused-snapshot")
    request = _request("same-content")
    first_start = datetime(2026, 8, 8, 4, 0, tzinfo=UTC)
    first_end = first_start + timedelta(minutes=3)
    second_start = first_start + timedelta(hours=1)
    second_end = second_start + timedelta(minutes=8)

    first_attempt = extraction.create_attempt(
        candidate_id, status.id, start_time=first_start
    )
    first_manifest = corpus.ingest_batch(
        f"fc_issue217_reuse_first_{uuid4().hex}",
        "issue217_reuse",
        [replace(request, extraction_attempt_id=first_attempt)],
    )
    extraction.complete_attempt(first_attempt, "succeeded", end_time=first_end)
    first_manifest = corpus.finalize_ingestion_batch(
        first_manifest["batch_id"], "complete"
    )

    second_attempt = extraction.create_attempt(
        candidate_id, status.id, start_time=second_start
    )
    second_manifest = corpus.ingest_batch(
        f"fc_issue217_reuse_second_{uuid4().hex}",
        "issue217_reuse",
        [replace(request, extraction_attempt_id=second_attempt)],
    )
    extraction.complete_attempt(second_attempt, "succeeded", end_time=second_end)
    second_manifest = corpus.finalize_ingestion_batch(
        second_manifest["batch_id"], "complete"
    )

    first_snapshot = UUID(str(first_manifest["assets"][0]["snapshot_id"]))
    second_snapshot = UUID(str(second_manifest["assets"][0]["snapshot_id"]))
    assert second_snapshot == first_snapshot
    assert second_manifest["assets"][0]["extraction_attempt_id"] == second_attempt
    assert second_manifest["started_at"] == second_start
    assert second_manifest["completed_at"] == second_end
    assert second_manifest["outcome_summary"]["succeeded_extraction_attempt_ids"] == [
        str(second_attempt)
    ]

    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT extraction_attempt_id FROM asset_snapshots WHERE id=%s",
            (first_snapshot,),
        )
        snapshot_attempt = cursor.fetchone()[0]
    assert UUID(str(snapshot_attempt)) == first_attempt


@requires_db
def test_bounded_wave_persists_success_failed_and_cancelled_preflight_members(
    tmp_path: Path,
):
    migrate(TEST_DSN)
    config = _config(tmp_path)
    runs = build_run_service(config)
    external_id = f"fr_issue217_wave_{uuid4().hex}"
    runs.create(
        "issue 217 mixed bounded extraction wave",
        external_id,
        execution_mode="autonomous_local",
    )
    build_workflow_operation_service(config).prepare_run(external_id)
    status = runs.status(external_id=external_id)
    runs.transition(
        status.id,
        "extracting",
        expected_revision=status.lifecycle_revision,
        idempotency_key=f"issue217:extracting:{status.id}",
        actor_type="test",
        actor_identifier="test_issue_217",
        triggering_event="run.extracting",
        reason="exercise mixed authoritative extraction batch membership",
    )
    status = runs.status(run_id=status.id)
    candidates = [
        _insert_candidate(status.id, label)
        for label in ("wave-success", "wave-failed", "wave-cancelled")
    ]

    success_url = "https://example.test/wave/success"
    failed_url = "https://example.test/wave/failed"
    cancelled_url = "https://example.test/wave/cancelled"
    raw_requests = [
        {
            "requested_url": success_url,
            "request": IngestRequest(
                success_url,
                b"# authoritative bounded extraction evidence",
            ),
            "metadata": {
                "candidate_id": str(candidates[0]),
                "firecrawl": {"result_index": 0, "status_code": 200},
            },
        },
        {
            "requested_url": failed_url,
            "metadata": {
                "candidate_id": str(candidates[1]),
                "firecrawl": {"result_index": 1, "status_code": 503},
                "preflight_classification": "http_error",
                "preflight_terminal": True,
                "preflight_cancelled": False,
                "preflight_retryable": False,
                "preflight_reason_code": "http_503",
                "preflight_reason": "authoritative test HTTP failure",
                "preflight_failure_stage": "provider_operation",
                "preflight_http_status": 503,
            },
        },
        {
            "requested_url": cancelled_url,
            "metadata": {
                "candidate_id": str(candidates[2]),
                "firecrawl": {"result_index": 2},
                "preflight_classification": "timeout",
                "preflight_terminal": True,
                "preflight_cancelled": True,
                "preflight_retryable": False,
                "preflight_reason_code": "provider_operation_timeout",
                "preflight_reason": "authoritative test timeout",
                "preflight_failure_stage": "provider_operation",
                "preflight_elapsed_seconds": 1.25,
            },
        },
    ]
    stage = BoundedExtractionStage(
        run_service=runs,
        coverage_service=MagicMock(),
        config=config,
        corpus_service=build_service(config),
        extraction_service=build_extraction_service(config),
    )
    context = {
        "raw_ingest_requests": raw_requests,
        "candidate_coverage_items": {},
    }

    result = stage.execute(
        status.id,
        status.lifecycle_revision,
        None,
        "extracting",
        context,
    )
    assert result.error is None
    summary = result.details["outcome_summary"]
    assert summary["member_count"] == 3
    assert summary["succeeded"] == 1
    assert summary["failed"] == 1
    assert summary["cancelled"] == 1
    assert len(summary["succeeded_extraction_attempt_ids"]) == 1
    assert len(summary["failed_extraction_attempt_ids"]) == 1
    assert len(summary["cancelled_extraction_attempt_ids"]) == 1
    assert summary["failure_classes"]["http_error"]["count"] == 1
    assert summary["failure_classes"]["timeout"]["count"] == 1

    with runs.uow_factory() as uow:
        manifest = uow.export_invocation_by_batch(result.details["batch_id"])
    assert len(manifest["assets"]) == 3
    assert {item["ordinal"] for item in manifest["assets"]} == {0, 1, 2}
    assert all(item["extraction_attempt_id"] is not None for item in manifest["assets"])


@requires_db
def test_v42_retry_and_export_are_schema_compatible_and_positionally_safe(
    tmp_path: Path,
):
    from psycopg import sql

    database = f"firecrawl_issue217_v42_test_{uuid4().hex}"
    admin_dsn = _dsn_for_database(TEST_DSN, "postgres")
    isolated_dsn = _dsn_for_database(TEST_DSN, database)
    with connect(admin_dsn) as admin:
        admin.autocommit = True
        with admin.cursor() as cursor:
            cursor.execute(
                sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database))
            )

    try:
        assert migrate(isolated_dsn, "0042_candidate_ranking_budgets") == 42
        config = _config(tmp_path, isolated_dsn)
        runs = build_run_service(config)
        corpus = build_service(config)
        status = runs.create(
            "issue 217 v42 rolling compatibility",
            f"fr_issue217_v42_{uuid4().hex}",
            execution_mode="autonomous_local",
        )
        invocation_id = f"fc_issue217_v42_{uuid4().hex}"

        first = corpus.ingest_batch(
            invocation_id,
            "issue217_v42",
            [_request("v42-first")],
            research_run_external_id=status.external_id,
        )
        first = corpus.finalize_ingestion_batch(first["batch_id"], "complete")
        first_batch_id = first["batch_id"]
        assert first["sealed_at"] is None
        assert first["outcome_summary"] is None
        assert first["research_run_id"] == status.external_id
        assert first["research_run_external_id"] == status.external_id

        second = corpus.ingest_batch(
            invocation_id,
            "issue217_v42",
            [_request("v42-retry")],
            research_run_external_id=status.external_id,
        )
        second = corpus.finalize_ingestion_batch(second["batch_id"], "complete")
        assert second["batch_id"] == first_batch_id
        assert second["status"] == "complete"
        assert second["sealed_at"] is None
        assert second["outcome_summary"] is None
        assert second["research_run_external_id"] == status.external_id
    finally:
        with connect(admin_dsn) as admin:
            admin.autocommit = True
            with admin.cursor() as cursor:
                cursor.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                        sql.Identifier(database)
                    )
                )


@requires_db
def test_arc05_canonical_read_exposes_stage_specific_selection_semantics(
    tmp_path: Path,
):
    migrate(TEST_DSN)
    config = _config(tmp_path)
    runs = build_run_service(config)
    corpus = build_service(config)
    extraction = build_extraction_service(config)
    status = runs.create(
        "issue 217 stage-specific semantics",
        f"fr_issue217_stages_{uuid4().hex}",
        execution_mode="autonomous_local",
    )
    candidate_id = _insert_candidate(status.id, "stage-flags")
    request = _request("stage-flags")
    attempt_id = extraction.create_attempt(candidate_id, status.id)
    promotions = AssetPromotionService(runs.uow_factory)

    selected = promotions.list_assets(status.id)[0]
    assert selected["current_stage"] == "selected_for_extraction"
    assert selected["selected_for_extraction"] is True
    assert selected["extraction_succeeded"] is False
    assert selected["retained"] is False

    raw_blob = extraction.store_raw_blob(request.content)
    normalized_blob = extraction.store_normalized_blob(
        request.normalized_content or request.content
    )
    extraction.complete_attempt(
        attempt_id,
        "succeeded",
        raw_blob=raw_blob,
        normalized_blob=normalized_blob,
        parser_used=config.parser_version,
    )
    extracted = promotions.list_assets(status.id)[0]
    assert extracted["selected_for_extraction"] is True
    assert extracted["extraction_succeeded"] is True
    assert extracted["retained"] is False

    ingest = corpus.ingest(replace(request, extraction_attempt_id=attempt_id))
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """INSERT INTO research_run_assets(run_id,snapshot_id,role,metadata)
                 VALUES(%s,%s,'acquired','{}')""",
            (status.id, ingest.snapshot_id),
        )
    retained = promotions.list_assets(status.id)[0]
    assert retained["retained"] is True
    assert retained["evidence_eligible"] is False
    assert retained["completion_critical"] is False

    current = _advance_to_indexing(runs, status)
    promotions.prepare_for_indexing(
        status.id, lifecycle_revision=current.lifecycle_revision
    )
    final = promotions.list_assets(status.id)[0]
    assert final["selected_for_extraction"] is True
    assert final["extraction_succeeded"] is True
    assert final["retained"] is True
    assert final["evidence_eligible"] is True
    assert final["completion_critical"] is True


@requires_db
def test_arc05_failed_and_rejected_subjects_keep_stage_specific_truth(tmp_path: Path):
    migrate(TEST_DSN)
    config = _config(tmp_path)
    runs = build_run_service(config)
    extraction = build_extraction_service(config)
    status = runs.create(
        "issue 217 failed and rejected stage semantics",
        f"fr_issue217_rejected_{uuid4().hex}",
        execution_mode="autonomous_local",
    )
    candidate_id = _insert_candidate(status.id, "rejected-stage-flags")
    attempt_id = extraction.create_attempt(candidate_id, status.id)
    extraction.complete_attempt(
        attempt_id,
        "failed",
        failure_class="http_error",
        error_message="authoritative test failure",
    )
    promotions = AssetPromotionService(runs.uow_factory)

    failed = promotions.list_assets(status.id)[0]
    assert failed["current_stage"] == "selected_for_extraction"
    assert failed["selected_for_extraction"] is True
    assert failed["extraction_succeeded"] is False
    assert failed["retained"] is False
    assert failed["evidence_eligible"] is False
    assert failed["completion_critical"] is False

    current = runs.status(run_id=status.id)
    promotions.reject(
        UUID(str(failed["id"])),
        expected_lifecycle_revision=current.lifecycle_revision,
        expected_run_id=status.id,
        actor_type="test",
        actor_identifier="test_issue_217",
        policy_version="issue217-test-v1",
        reason_code="test_rejection",
        reason="verify rejected stage-specific semantics",
    )
    rejected = promotions.list_assets(status.id)[0]
    assert rejected["current_stage"] == "rejected"
    assert rejected["selected_for_extraction"] is True
    assert rejected["extraction_succeeded"] is False
    assert rejected["retained"] is False
    assert rejected["evidence_eligible"] is False
    assert rejected["completion_critical"] is False
