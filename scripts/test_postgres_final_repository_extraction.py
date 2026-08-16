"""Issue #259 regressions for the final PostgreSQL repository extraction."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from research_store.postgres import PostgresUnitOfWork, connect, migrate

TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL")
INTEGRATION = pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)


class _FakeConnection:
    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass

    def transaction(self):
        raise AssertionError("unit repository binding must not open a savepoint")


FINAL_ROLES = {
    "retrieval_events": "log_retrieval",
    "index_jobs": "claim_jobs",
    "claims": "upsert_claim",
    "evidence_packets": "persist_evidence_packet",
    "audits": "create_audit_assessment",
    "semantic_calls": "record_semantic_call",
    "semantic_cache": "get_cache_entry_by_key",
    "model_endpoints": "upsert_health",
    "synthesis_stages": "get_synthesis_stage",
}

DOMAIN_METHODS = {
    "persist_ingest",
    "start_run",
    "record_search_response",
    "assign_duplicate_group",
    "record_semantic_call",
    "log_retrieval",
    "claim_jobs",
    "upsert_claim",
    "persist_evidence_packet",
    "create_audit_assessment",
    "get_cache_entry_by_key",
    "get_synthesis_stage",
    "start_ingestion_batch",
    "record_batch_asset",
    "finish_ingestion_batch",
    "export_invocation",
    "get_trace",
}


def test_final_roles_have_canonical_connection_bound_owners(monkeypatch):
    fake_connection = _FakeConnection()
    monkeypatch.setattr(
        "research_store.postgres.connect", lambda _database_url: fake_connection
    )

    with PostgresUnitOfWork("postgresql://test.invalid/db", "test-index") as uow:
        for role, operation in FINAL_ROLES.items():
            repository = getattr(uow, role)
            assert callable(getattr(repository, operation))
            assert repository.connection_identity == id(fake_connection)
            for lifecycle in (
                "connection",
                "commit",
                "rollback",
                "savepoint",
                "close",
                "execute",
                "fetchone",
            ):
                assert not hasattr(repository, lifecycle)


def test_uow_class_is_transaction_and_composition_only():
    assert DOMAIN_METHODS.isdisjoint(PostgresUnitOfWork.__dict__)
    actual_public = {
        name
        for name, value in PostgresUnitOfWork.__dict__.items()
        if callable(value) and not name.startswith("_")
    }
    assert actual_public == {"commit", "rollback", "savepoint", "execute", "fetchone"}


def _start_run(uow, suffix):
    return uow.runs.start_run(
        f"issue-259 {suffix}",
        {
            "external_run_id": f"issue-259-{suffix}-{uuid4()}",
            "execution_mode": "agent_led",
            "metadata": {"test": "issue-259"},
        },
    )


def _insert_cache_entry(uow, suffix):
    key_hash = f"{uuid4().hex}{uuid4().hex}"
    uow.semantic_cache.insert_cache_entry(
        {
            "id": uuid4(),
            "key_hash": key_hash,
            "stage": "issue-259",
            "model_fingerprint": "test-model",
            "input_hash": "a" * 64,
            "prompt_hash": "b" * 64,
            "prompt_version": "test-v1",
            "schema_version": "test-v1",
            "policy_version": None,
            "configuration_hash": None,
            "artifact": {"suffix": suffix},
            "provenance": {"test": "issue-259"},
            "status": "valid",
            "ttl_seconds": 3600,
            "created_at": time.time(),
        }
    )
    return key_hash


@INTEGRATION
def test_final_repositories_share_one_outer_rollback():
    migrate(TEST_DSN)
    run_id = None
    claim_id = uuid4()
    key_hash = None

    with (
        pytest.raises(RuntimeError, match="rollback final repositories"),
        PostgresUnitOfWork(TEST_DSN, "test-index") as uow,
    ):
        assert uow.claims.connection_identity == uow.semantic_cache.connection_identity
        assert uow.claims.connection_identity == id(uow.connection)
        run_id = _start_run(uow, "final-outer-rollback")
        uow.claims.upsert_claim(run_id, claim_id, "final repository rollback claim")
        key_hash = _insert_cache_entry(uow, "outer-rollback")
        raise RuntimeError("rollback final repositories")

    assert run_id is not None
    assert key_hash is not None
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM research_claims WHERE run_id=%s AND claim_id=%s",
            (run_id, claim_id),
        )
        assert cursor.fetchone()[0] == 0
        cursor.execute(
            "SELECT count(*) FROM semantic_cache WHERE key_hash=%s", (key_hash,)
        )
        assert cursor.fetchone()[0] == 0


@INTEGRATION
def test_final_repositories_share_uow_savepoint_behavior():
    migrate(TEST_DSN)
    claim_id = uuid4()
    key_hash = None

    with PostgresUnitOfWork(TEST_DSN, "test-index") as uow:
        run_id = _start_run(uow, "final-savepoint")
        try:
            with uow.savepoint():
                uow.claims.upsert_claim(
                    run_id, claim_id, "final repository savepoint claim"
                )
                key_hash = _insert_cache_entry(uow, "savepoint")
                raise ValueError("rollback final savepoint")
        except ValueError as exc:
            assert str(exc) == "rollback final savepoint"

        assert uow.claims.connection_identity == uow.semantic_cache.connection_identity
        assert uow.claims.connection_identity == id(uow.connection)
        assert not hasattr(uow.claims, "rollback")
        assert not hasattr(uow.semantic_cache, "savepoint")

    assert key_hash is not None
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM research_runs WHERE id=%s", (run_id,))
        assert cursor.fetchone()[0] == 1
        cursor.execute(
            "SELECT count(*) FROM research_claims WHERE run_id=%s AND claim_id=%s",
            (run_id, claim_id),
        )
        assert cursor.fetchone()[0] == 0
        cursor.execute(
            "SELECT count(*) FROM semantic_cache WHERE key_hash=%s", (key_hash,)
        )
        assert cursor.fetchone()[0] == 0
        cursor.execute("DELETE FROM research_runs WHERE id=%s", (run_id,))
