"""Opt-in PostgreSQL integration tests.

Set RESEARCH_STORE_TEST_DATABASE_URL to a disposable PostgreSQL database whose
name contains a standalone ``test`` segment, and set
RESEARCH_STORE_TEST_ALLOW_RESET to that exact database name. The suite never
guesses or reuses DATABASE_URL because its session setup drops the public schema.
"""

from __future__ import annotations

# ruff: noqa: E402 - load the sibling script package without installing it.

from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import sys
from uuid import uuid4

import pytest

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from research_store.config import StoreConfig
from research_store import cli as store_cli
from research_store.container import build_service
from research_store.domain import IngestRequest
from research_store.postgres import connect, migrate, require_disposable_database_reset


TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)


@pytest.fixture
def service(tmp_path, prepared_database):
    migrate(TEST_DSN)
    config = replace(
        StoreConfig.from_env(),
        database_url=TEST_DSN,
        blob_root=tmp_path / "blobs",
        qdrant_collection="research_integration_test",
        embedding_dimension=4,
    )
    return build_service(config)


@pytest.fixture(scope="session")
def prepared_database():
    """Prove a populated, multi-index v1 database upgrades without data loss."""
    require_disposable_database_reset(
        TEST_DSN, os.environ.get("RESEARCH_STORE_TEST_ALLOW_RESET", "")
    )
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute("DROP SCHEMA public CASCADE")
        cursor.execute("CREATE SCHEMA public")
    assert migrate(TEST_DSN, "0001_research_store") == 1

    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO sources(canonical_url) VALUES (%s) RETURNING id",
            ("https://integration.example/legacy-multi-index",),
        )
        source_id = cursor.fetchone()[0]
        cursor.execute(
            """INSERT INTO asset_snapshots(
                source_id,requested_url,retrieved_at,content_sha256
            ) VALUES (%s,%s,now(),%s) RETURNING id""",
            (source_id, "https://integration.example/legacy-multi-index", "a" * 64),
        )
        snapshot_id = cursor.fetchone()[0]
        cursor.execute(
            """INSERT INTO documents(
                snapshot_id,normalized_text,parser_name,parser_version,
                normalization_version,document_sha256
            ) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id""",
            (snapshot_id, "legacy evidence", "legacy", "1", "1", "b" * 64),
        )
        document_id = cursor.fetchone()[0]
        cursor.execute(
            """INSERT INTO chunks(
                document_id,ordinal,text,content_sha256,chunker_name,chunker_version
            ) VALUES (%s,0,%s,%s,%s,%s) RETURNING id""",
            (document_id, "legacy evidence", "c" * 64, "legacy", "1"),
        )
        chunk_id = cursor.fetchone()[0]
        manifest_ids = []
        for model_name, dimension, index_name in (
            ("legacy-a", 4, "legacy-a-index"),
            ("legacy-b", 8, "legacy-b-index"),
        ):
            cursor.execute(
                """INSERT INTO embedding_manifests(
                    chunk_id,model_name,dimension,distance_metric,index_status
                ) VALUES (%s,%s,%s,'Cosine','complete') RETURNING id""",
                (chunk_id, model_name, dimension),
            )
            manifest_ids.append(cursor.fetchone()[0])
            cursor.execute(
                """INSERT INTO index_jobs(
                    entity_type,entity_id,index_name,operation,status
                ) VALUES ('chunk',%s,%s,'upsert','complete')""",
                (chunk_id, index_name),
            )

    assert migrate(TEST_DSN) == 5
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT count(*),count(DISTINCT index_definition_id)
            FROM embedding_manifests WHERE chunk_id=%s""",
            (chunk_id,),
        )
        assert cursor.fetchone() == (2, 2)
        cursor.execute(
            """SELECT count(*),count(DISTINCT manifest_id),count(DISTINCT index_definition_id)
            FROM index_jobs WHERE manifest_id=ANY(%s)""",
            (manifest_ids,),
        )
        assert cursor.fetchone() == (2, 2, 2)
        cursor.execute(
            """INSERT INTO index_definitions(
                fingerprint,physical_collection,model_name,model_revision,
                dimension,distance_metric,normalization,instruction_template_hash
            ) VALUES (%s,%s,'legacy-a','',16,'Cosine','unit-length','')
            RETURNING id""",
            ("d" * 64, "legacy-dimension-variant"),
        )
        definition_id = cursor.fetchone()[0]
        cursor.execute(
            """INSERT INTO embedding_manifests(
                chunk_id,model_name,model_revision,dimension,distance_metric,
                normalization,instruction_template_hash,index_status,index_definition_id
            ) VALUES (%s,'legacy-a','',16,'Cosine','unit-length','','pending',%s)""",
            (chunk_id, definition_id),
        )
        cursor.execute(
            """SELECT conname FROM pg_constraint
            WHERE conrelid='embedding_manifests'::regclass AND contype='u'
            ORDER BY conname"""
        )
        assert [row[0] for row in cursor.fetchall()] == [
            "embedding_manifests_definition_key",
            "embedding_manifests_id_definition_key",
        ]


def test_firecrawl_result_versioning_and_transactional_index_jobs(service):
    url = f"https://integration.example/{uuid4()}"
    first = service.ingest(
        IngestRequest(
            url, b"# V1\n\nRaw first.", normalized_content=b"# V1\n\nNormalized first."
        )
    )
    unchanged = service.ingest(
        IngestRequest(
            url, b"# V1\n\nRaw first.", normalized_content=b"# V1\n\nNormalized first."
        )
    )
    changed = service.ingest(IngestRequest(url, b"# V2\n\nRaw changed."))
    assert unchanged.reused_snapshot and unchanged.snapshot_id == first.snapshot_id
    assert changed.snapshot_id != first.snapshot_id
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT parent_snapshot_id FROM asset_snapshots WHERE id=%s",
            (changed.snapshot_id,),
        )
        assert cursor.fetchone()[0] == first.snapshot_id
        cursor.execute(
            "SELECT count(*) FROM index_jobs WHERE entity_id=ANY(%s)",
            (list(changed.chunk_ids),),
        )
        assert cursor.fetchone()[0] == len(changed.chunk_ids)


def test_bounded_targeted_passage_retrieval(service):
    result = service.ingest(
        IngestRequest(
            f"https://integration.example/{uuid4()}",
            b"# Evidence\n\nCitation-ready text.",
        )
    )
    passages = service.fetch_passages(
        list(result.chunk_ids), max_tokens=100, max_passages=1
    )
    assert len(passages) == 1
    assert passages[0]["snapshot_id"] == result.snapshot_id
    assert passages[0]["source_id"] == result.source_id


def test_concurrent_same_source_ingest_has_stable_identity(service):
    url = f"https://integration.example/concurrent/{uuid4()}"
    request = IngestRequest(url, b"# Concurrent identity\n\nStable corpus object.")
    with ThreadPoolExecutor(max_workers=2) as executor:
        first, second = list(executor.map(service.ingest, (request, request)))

    assert first.snapshot_id == second.snapshot_id
    assert first.document_id == second.document_id
    assert first.chunk_ids == second.chunk_ids
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT count(DISTINCT a.id),count(DISTINCT d.id),count(DISTINCT c.id)
            FROM sources s JOIN asset_snapshots a ON a.source_id=s.id
            JOIN documents d ON d.snapshot_id=a.id
            JOIN chunks c ON c.document_id=d.id WHERE s.canonical_url=%s""",
            (url,),
        )
        assert cursor.fetchone() == (1, 1, len(first.chunk_ids))


def test_lexical_search_selects_only_configured_derivation(service):
    marker = f"derivationmarker{uuid4().hex}"
    url = f"https://integration.example/derivation/{uuid4()}"
    request = IngestRequest(url, f"# Derivation\n\n{marker} retained evidence.".encode())
    active = service.ingest(request)
    alternate_config = replace(
        service.config,
        parser_version="markdown-integration-alternate",
        normalization_version="cleanup-integration-alternate",
        chunker_version="structural-integration-alternate",
    )
    alternate_service = build_service(alternate_config)
    alternate = alternate_service.ingest(request)

    assert alternate.snapshot_id == active.snapshot_id
    assert alternate.document_id != active.document_id
    assert alternate.chunk_ids != active.chunk_ids
    with service.uow_factory() as uow:
        active_hits = uow.search_lexical(marker, 10, {})
    with alternate_service.uow_factory() as uow:
        alternate_hits = uow.search_lexical(marker, 10, {})
    assert {row["candidate_id"] for row in active_hits} == set(active.chunk_ids)
    assert {row["candidate_id"] for row in alternate_hits} == set(
        alternate.chunk_ids
    )


def test_batch_records_acquisition_failure_without_losing_success(service):
    invocation_id = f"fc_integration_{uuid4().hex}"
    good_url = f"https://integration.example/batch/{uuid4()}"
    manifest = service.ingest_batch(
        invocation_id,
        "integration",
        [
            IngestRequest(good_url, b"# Good\n\nCommitted through the outer batch."),
            {
                "requested_url": "https://integration.example/unreachable",
                "error": "synthetic acquisition failure",
            },
        ],
    )

    assert manifest["status"] == "partial"
    assert manifest["failure_count"] == 1
    assert [asset["status"] for asset in manifest["assets"]] == [
        "complete",
        "failed",
    ]
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM sources WHERE canonical_url=%s", (good_url,)
        )
        assert cursor.fetchone()[0] == 1

    replacement = service.ingest_batch(
        invocation_id,
        "integration",
        [
            {
                "request": IngestRequest(
                    good_url, b"# Retry\n\nReplacement invocation ledger."
                ),
                "metadata": {"firecrawl": {"result_index": 7}},
            }
        ],
    )
    assert replacement["status"] == "complete"
    assert [(item["ordinal"], item["status"]) for item in replacement["assets"]] == [
        (7, "complete")
    ]
    with pytest.raises(ValueError, match="original operation and research run"):
        service.ingest_batch(invocation_id, "different-operation", [])
    external_run = f"fr_integration_{uuid4().hex}"
    with service.uow_factory() as uow:
        uow.start_run("other owner", {"external_run_id": external_run})
    with pytest.raises(ValueError, match="original operation and research run"):
        service.ingest_batch(
            invocation_id,
            "integration",
            [],
            research_run_external_id=external_run,
        )


def test_batch_rejects_invalid_run_and_active_reuse_before_ledger_mutation(service):
    missing_run = f"fr_missing_{uuid4().hex}"
    missing_invocation = f"fc_missing_{uuid4().hex}"
    with pytest.raises(KeyError, match=missing_run):
        service.ingest_batch(
            missing_invocation,
            "integration",
            [],
            research_run_external_id=missing_run,
        )

    finished_run = f"fr_finished_{uuid4().hex}"
    finished_invocation = f"fc_finished_{uuid4().hex}"
    with service.uow_factory() as uow:
        run_id = uow.start_run("finished owner", {"external_run_id": finished_run})
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """UPDATE research_runs SET status='complete',outcome='test-complete',
            completed_at=now() WHERE id=%s""",
            (run_id,),
        )
    with pytest.raises(ValueError, match="require a running research run"):
        service.ingest_batch(
            finished_invocation,
            "integration",
            [],
            research_run_external_id=finished_run,
        )

    active_invocation = f"fc_active_{uuid4().hex}"
    with service.uow_factory() as uow:
        uow.start_ingestion_batch(active_invocation, "integration")
    with pytest.raises(ValueError, match="already running"):
        service.ingest_batch(active_invocation, "integration", [])

    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT invocation_id FROM ingestion_batches
            WHERE invocation_id=ANY(%s) ORDER BY invocation_id""",
            ([missing_invocation, finished_invocation, active_invocation],),
        )
        assert cursor.fetchall() == [(active_invocation,)]


def test_finished_run_is_immutable_and_rejects_new_evidence(service):
    external_id = f"fr_integration_{uuid4().hex}"
    asset = service.ingest(
        IngestRequest(
            f"https://integration.example/run/{uuid4()}",
            b"# Run evidence\n\nImmutable after finish.",
        )
    )
    with service.uow_factory() as uow:
        run_id = uow.start_run(
            "original request", {"external_run_id": external_id}
        )
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """UPDATE research_runs SET status='complete',outcome='test-complete',
            completed_at=now() WHERE id=%s""",
            (run_id,),
        )
    with service.uow_factory() as uow:
        repeated = uow.start_run(
            "mutated request", {"external_run_id": external_id}
        )
        assert repeated == run_id
        with pytest.raises(KeyError):
            uow.link_run_asset(external_id, asset.snapshot_id)
        with pytest.raises(KeyError):
            uow.log_retrieval(
                run_id,
                {
                    "stage": "retriever",
                    "query": "late evidence",
                    "retriever": "lexical",
                    "candidate_type": "chunk",
                    "candidate_id": asset.chunk_ids[0],
                    "rank": 1,
                },
            )
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT original_request,status FROM research_runs WHERE id=%s", (run_id,)
        )
        assert cursor.fetchone() == ("original request", "complete")


def test_finish_reopen_refinish_clears_completion_state(service, monkeypatch):
    external_id = f"fr_reopen_{uuid4().hex}"
    with service.uow_factory() as uow:
        run_id = uow.start_run("reopen lifecycle", {"external_run_id": external_id})
    monkeypatch.setenv("DATABASE_URL", TEST_DSN)
    assert (
        store_cli.main(
            [
                "run-finish",
                external_id,
                "--outcome",
                "satisfied",
                "--source-manifest-sha256",
                "a" * 64,
                "--answer-sha256",
                "b" * 64,
            ]
        )
        == 0
    )
    assert store_cli.main(["run-reopen", external_id]) == 0
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT status,outcome,completed_at,source_manifest_sha256,answer_sha256
            FROM research_runs WHERE id=%s""",
            (run_id,),
        )
        assert cursor.fetchone() == ("running", None, None, None, None)
        with pytest.raises(Exception) as error:
            cursor.execute(
                "UPDATE research_runs SET status='unexpected' WHERE id=%s", (run_id,)
            )
        assert "research_runs_lifecycle_check" in str(error.value)
    assert (
        store_cli.main(
            ["run-finish", external_id, "--outcome", "satisfied"]
        )
        == 0
    )
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT status,outcome,source_manifest_sha256,answer_sha256
            FROM research_runs WHERE id=%s""",
            (run_id,),
        )
        assert cursor.fetchone() == ("complete", "satisfied", None, None)


def test_expired_final_attempt_becomes_dead_and_manifest_failed(service):
    result = service.ingest(
        IngestRequest(
            f"https://integration.example/exhausted/{uuid4()}",
            b"# Exhausted lease\n\nMust never remain running forever.",
        )
    )
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """UPDATE index_jobs SET status='running',attempt_count=5,
            lease_token=gen_random_uuid(),lease_owner='crashed-worker',
            lease_expires_at=now()-interval '1 minute'
            WHERE entity_id=%s RETURNING id,manifest_id""",
            (result.chunk_ids[0],),
        )
        job_id, manifest_id = cursor.fetchone()
    with service.uow_factory() as uow:
        uow.claim_jobs(1, max_attempts=5)
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT j.status,j.error,m.index_status,m.error
            FROM index_jobs j JOIN embedding_manifests m ON m.id=j.manifest_id
            WHERE j.id=%s AND m.id=%s""",
            (job_id, manifest_id),
        )
        status, error, manifest_status, manifest_error = cursor.fetchone()
    assert (status, manifest_status) == ("dead", "failed")
    assert error == manifest_error == "lease expired after final allowed attempt"


def test_job_completion_requires_exact_lease_token(service):
    result = service.ingest(
        IngestRequest(
            f"https://integration.example/token/{uuid4()}",
            b"# Lease token\n\nOnly the owning worker may complete.",
        )
    )
    lease_token = uuid4()
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """UPDATE index_jobs SET status='running',attempt_count=1,
            lease_token=%s,lease_owner='integration',lease_expires_at=now()+interval '5 minutes'
            WHERE entity_id=%s RETURNING id""",
            (lease_token, result.chunk_ids[0]),
        )
        job_id = cursor.fetchone()[0]
    with service.uow_factory() as uow:
        with pytest.raises(TypeError):
            uow.finish_job(job_id, None)
        assert uow.finish_job(job_id, uuid4()) is False
        assert uow.finish_job(job_id, lease_token) is True


def test_job_manifest_definition_mismatch_is_rejected(service):
    request = IngestRequest(
        f"https://integration.example/definition/{uuid4()}",
        b"# Definition binding\n\nA job cannot escape its manifest definition.",
    )
    first = service.ingest(request)
    alternate = build_service(
        replace(service.config, embedding_revision=f"alternate-{uuid4().hex}")
    )
    alternate.ingest(request)
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT m.id,m.index_definition_id
            FROM embedding_manifests m WHERE m.chunk_id=%s
            ORDER BY m.id LIMIT 1""",
            (first.chunk_ids[0],),
        )
        manifest_id, original_definition = cursor.fetchone()
        cursor.execute(
            """SELECT index_definition_id FROM embedding_manifests
            WHERE chunk_id=%s AND index_definition_id<>%s LIMIT 1""",
            (first.chunk_ids[0], original_definition),
        )
        other_definition = cursor.fetchone()[0]
        with pytest.raises(Exception) as error:
            cursor.execute(
                """UPDATE index_jobs SET index_definition_id=%s
                WHERE manifest_id=%s""",
                (other_definition, manifest_id),
            )
        assert "index_jobs_manifest_definition_fk" in str(error.value)
