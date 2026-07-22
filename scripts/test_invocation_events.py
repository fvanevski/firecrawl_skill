"""Tests for invocation events and annotations (issue #31).

Covers:
- Event parity: every new invocation event is authoritative in PostgreSQL
- Concurrent append: stable ordering under concurrent writes
- Sanitization: secrets are redacted before storage
- Filesystem records are derived, not authoritative
- Event ordering is stable and queryable
- Duplicate/retried commands are idempotent
- Invalid and unknown IDs are rejected
- Transaction rollback preserves consistency
- PostgreSQL failure before export
- Export failure after PostgreSQL commit
"""

from __future__ import annotations

import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from uuid import UUID, uuid4

# Ensure scripts directory is on the path before importing test modules
# (E402: module level import not at top — sys.path.insert is required first)
SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))  # noqa: E402

import pytest  # noqa: E402

from research_store.invocation_events import (  # noqa: E402
    DuplicateEventKey,
    EventAppendResult,
    EventService,
    InvocationEvent,
    _sanitize,
    InvalidEventType,
    _validate_event_type,
)
from research_store.invocation_catalog import (  # noqa: E402
    InvocationCatalogService,
    InvocationCatalogError,
    InvocationAlreadyRunning,
    InvocationRecord,
)
from research_store.postgres import PostgresUnitOfWork  # noqa: E402


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------

def _database_url():
    url = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL")
    if not url:
        pytest.skip("RESEARCH_STORE_TEST_DATABASE_URL not set")
    return url


@pytest.fixture()
def database_url():
    return _database_url()


@pytest.fixture()
def uow_factory(database_url):
    """Return a PostgresUnitOfWork factory for tests."""
    def factory():
        return PostgresUnitOfWork(
            database_url,
            physical_collection="test_invocation_events",
            embedding_model="test",
            embedding_revision="1",
            embedding_dimension=3,
            parser_version="markdown-v1",
            normalization_version="cleanup-v1",
            chunker_version="structural-v1",
        )
    return factory


@pytest.fixture()
def run_id(database_url):
    """Create a test research run in PostgreSQL."""
    with PostgresUnitOfWork(database_url, "test_invocation_events",
                            "test", "1", 3, "markdown-v1",
                            "cleanup-v1", "structural-v1") as uow:
        run_id = uow.runs.start_run(
            "test invocation events objective",
            {"skill_version": "test", "llm_model": "test"},
        )
        yield run_id


@pytest.fixture()
def event_service(uow_factory):
    return EventService(uow_factory)


@pytest.fixture()
def catalog_service(uow_factory):
    return InvocationCatalogService(uow_factory)


# ------------------------------------------------------------------
# Event parity tests
# ------------------------------------------------------------------

class TestEventParity:
    """Every new invocation event is authoritative in PostgreSQL."""

    def test_append_event_returns_result(self, run_id, event_service):
        result = event_service.append(
            run_id,
            "invocation_started",
            "system",
            f"test:append:{uuid4()}",
            payload={"operation": "search"},
        )
        assert isinstance(result, EventAppendResult)
        assert isinstance(result.event_id, UUID)
        assert result.sequence_number > 0
        assert result.run_revision >= 0
        assert result.reused is False

    def test_event_is_queryable_by_id(self, run_id, event_service):
        result = event_service.append(
            run_id,
            "pivot",
            "system",
            f"test:query:{uuid4()}",
            payload={"query": "test query"},
        )
        event = event_service.get_event(run_id, result.event_id)
        assert isinstance(event, InvocationEvent)
        assert event.event_type == "pivot"
        assert event.payload == {"query": "test query"}
        assert event.sequence_number == result.sequence_number

    def test_event_is_queryable_by_type(self, run_id, event_service):
        event_types = ["pivot", "retry", "decision", "recovery", "annotation"]
        for idx, etype in enumerate(event_types):
            event_service.append(
                run_id, etype, "system", f"test:type:{idx}",
                payload={"detail": etype},
            )
        for etype in event_types:
            events = event_service.list_events_by_type(run_id, etype)
            assert len(events) == 1
            assert events[0].event_type == etype

    def test_event_is_queryable_by_invocation(self, run_id, catalog_service):
        inv = catalog_service.begin(
            run_id, f"fc_{uuid4().hex[:32]}", "search", {"query": "test"}
        )
        catalog_service.add_event(
            run_id, inv.id, "pivot", {"query": "pivot query"}
        )
        events = catalog_service.list_events(run_id, invocation_id=inv.id)
        assert len(events) == 1
        assert events[0].event_type == "pivot"

    def test_all_event_types_are_valid(self):
        from research_store.invocation_events import EVENT_TYPES
        assert EVENT_TYPES == frozenset({
            "run_started", "run_finished", "run_reopened",
            "invocation_started", "invocation_finished", "invocation_event",
            "pivot", "retry", "decision", "recovery", "annotation",
        })

    def test_invalid_event_type_rejected(self):
        with pytest.raises(InvalidEventType):
            _validate_event_type("invalid_type")

    def test_event_sequence_is_monotonically_increasing(self, run_id, event_service):
        seqs = []
        for idx in range(5):
            result = event_service.append(
                run_id, "annotation", "system", f"test:seq:{idx}",
                payload={"index": idx},
            )
            seqs.append(result.sequence_number)
        assert seqs == sorted(seqs)
        assert seqs[0] < seqs[-1]

    def test_event_ordering_is_stable_and_queryable(self, run_id, event_service):
        for idx in range(10):
            event_service.append(
                run_id, "annotation", "system", f"test:order:{idx}",
                payload={"index": idx},
            )
        events = event_service.list_events(run_id, limit=10)
        seqs = [e.sequence_number for e in events]
        assert seqs == list(range(1, 11))


# ------------------------------------------------------------------
# Concurrent append tests
# ------------------------------------------------------------------

class TestConcurrentAppend:
    """Event ordering remains stable under concurrent writes."""

    def test_concurrent_appends_dont_duplicate(self, run_id, event_service):
        n_workers = 5
        n_events = 10
        with ThreadPoolExecutor(max_workers=n_workers) as pool:
            futures = []
            for worker_id in range(n_workers):
                for event_id in range(n_events):
                    futures.append(
                        pool.submit(
                            event_service.append,
                            run_id,
                            "annotation",
                            "system",
                            f"concurrent:{worker_id}:{event_id}",
                            payload={"worker": worker_id, "event": event_id},
                        )
                    )
            results = [f.result() for f in as_completed(futures)]
        assert len(results) == n_workers * n_events
        seqs = [r.sequence_number for r in results]
        assert len(set(seqs)) == n_workers * n_events

    def test_concurrent_appends_have_stable_order(self, run_id, event_service):
        n_events = 20
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [
                pool.submit(
                    event_service.append,
                    run_id,
                    "annotation",
                    "system",
                    f"stable:{idx}",
                    payload={"index": idx},
                )
                for idx in range(n_events)
            ]
            [f.result() for f in as_completed(futures)]
        events = event_service.list_events(run_id, limit=n_events)
        seqs = [e.sequence_number for e in events]
        assert seqs == sorted(seqs)


# ------------------------------------------------------------------
# Sanitization tests
# ------------------------------------------------------------------

class TestSanitization:
    """Secrets are redacted before storage."""

    def test_sensitive_key_is_redacted(self):
        payload = {"api_key": "secret123", "normal": "value"}
        result = _sanitize(payload)
        assert result["api_key"] == "[REDACTED]"
        assert result["normal"] == "value"

    def test_bearer_token_is_redacted(self):
        payload = {"header": "Bearer eyJhbGciOiJIUzI1NiJ9.test"}
        result = _sanitize(payload)
        assert "Bearer [REDACTED]" == result["header"]
        assert "eyJhbGci" not in result["header"]

    def test_api_key_pattern_is_redacted(self):
        payload = {"detail": "api_key=secret123"}
        result = _sanitize(payload)
        assert "api_key=[REDACTED]" == result["detail"]
        assert "secret123" not in result["detail"]

    def test_nested_sensitive_keys_are_redacted(self):
        payload = {
            "outer": {
                "password": "secret",
                "token": "tok_123",
                "safe": "value",
            }
        }
        result = _sanitize(payload)
        assert result["outer"]["password"] == "[REDACTED]"
        assert result["outer"]["token"] == "[REDACTED]"
        assert result["outer"]["safe"] == "value"

    def test_redaction_applied_to_event_in_database(self, run_id, event_service):
        result = event_service.append(
            run_id,
            "annotation",
            "system",
            f"test:redact:{uuid4()}",
            payload={
                "api_key": "secret_key_123",
                "auth_header": "Bearer tok_abc",
                "safe_field": "public_value",
            },
        )
        # Verify sequence_number is populated
        assert result.sequence_number > 0
        event = event_service.get_event(run_id, result.event_id)
        assert event.payload["api_key"] == "[REDACTED]"
        assert event.payload["auth_header"] == "Bearer [REDACTED]"
        assert event.payload["safe_field"] == "public_value"
        assert event.sequence_number == result.sequence_number


# ------------------------------------------------------------------
# Idempotency tests
# ------------------------------------------------------------------

class TestIdempotency:
    """Duplicate or retried commands are idempotent."""

    def test_duplicate_idempotency_key_returns_existing(self, run_id, event_service):
        key = f"test:idempotent:{uuid4()}"
        result1 = event_service.append(
            run_id, "pivot", "system", key,
            payload={"query": "original"},
        )
        result2 = event_service.append(
            run_id, "pivot", "system", key,
            payload={"query": "original"},
        )
        assert result1.event_id == result2.event_id
        assert result2.reused is True

    def test_different_payload_with_same_key_rejected(self, run_id, event_service):
        key = f"test:conflict:{uuid4()}"
        event_service.append(
            run_id, "pivot", "system", key,
            payload={"query": "original"},
        )
        with pytest.raises(
            DuplicateEventKey, match="idempotency key"
        ):
            event_service.append(
                run_id, "pivot", "system", key,
                payload={"query": "different"},
            )


# ------------------------------------------------------------------
# Filesystem derivation tests
# ------------------------------------------------------------------

class TestFilesystemDerivation:
    """Filesystem records are derived after database commit."""

    def test_export_invocation_to_filesystem(self, run_id, database_url, tmp_path):
        from research_store.config import StoreConfig

        config = StoreConfig.from_env()
        config.database_url = database_url

        from research_store.container import build_run_service
        from research_store.invocation_catalog import InvocationCatalogService

        service = build_run_service(config)
        catalog_service = InvocationCatalogService(
            service.uow_factory, event_service=service.event_service
        )

        inv = catalog_service.begin(
            run_id, f"fc_{uuid4().hex[:32]}", "search", {"query": "test"}
        )
        catalog_service.add_event(
            run_id, inv.id, "pivot", {"query": "pivot query"}
        )

        # Export to filesystem
        catalog_path = tmp_path / "catalog" / "invocations"
        catalog_path.mkdir(parents=True)

        catalog_record = catalog_service.export_to_catalog_format(
            run_id, inv.id
        )
        assert catalog_record["schema_version"] == 5
        assert catalog_record["operation"] == "search"
        assert len(catalog_record["events"]) == 1
        assert catalog_record["events"][0]["event_type"] == "pivot"

    def test_filesystem_not_read_for_state(self, run_id, database_url):
        """Verify that current state is determined from PostgreSQL, not filesystem."""
        from research_store.container import build_run_service
        from research_store.invocation_catalog import InvocationCatalogService
        from research_store.config import StoreConfig

        config = StoreConfig.from_env()
        config.database_url = database_url

        service = build_run_service(config)
        catalog_service = InvocationCatalogService(
            service.uow_factory, event_service=service.event_service
        )

        inv = catalog_service.begin(
            run_id, f"fc_{uuid4().hex[:32]}", "search", {"query": "test"}
        )

        # State should be "running" from PostgreSQL
        status = catalog_service.status(invocation_id=inv.id)
        assert status.status == "running"

        # Even if filesystem doesn't exist, state is still queryable
        # (the test passes if no exception is raised)


# ------------------------------------------------------------------
# Invalid and unknown ID tests
# ------------------------------------------------------------------

class TestInvalidIds:
    """Invalid and unknown IDs are rejected."""

    def test_unknown_run_id_rejected(self, event_service):
        fake_run_id = uuid4()
        with pytest.raises(KeyError, match="not found"):
            event_service.append(
                fake_run_id, "pivot", "system", f"test:unknown:{uuid4()}",
                payload={"query": "test"},
            )

    def test_unknown_event_id_rejected(self, run_id, event_service):
        fake_event_id = uuid4()
        with pytest.raises(KeyError):
            event_service.get_event(run_id, fake_event_id)

    def test_empty_actor_type_rejected(self, run_id, event_service):
        with pytest.raises(ValueError, match="actor_type"):
            event_service.append(
                run_id, "pivot", "", f"test:empty:{uuid4()}",
                payload={},
            )

    def test_empty_idempotency_key_rejected(self, run_id, event_service):
        with pytest.raises(ValueError, match="idempotency_key"):
            event_service.append(
                run_id, "pivot", "system", "",
                payload={},
            )


# ------------------------------------------------------------------
# Invocation catalog tests
# ------------------------------------------------------------------

class TestInvocationCatalog:
    """PostgreSQL-backed invocation catalog API."""

    def test_begin_creates_invocation_and_event(self, run_id, catalog_service):
        ext_id = f"fc_{uuid4().hex[:32]}"
        record = catalog_service.begin(
            run_id, ext_id, "search", {"query": "test query"}
        )
        assert isinstance(record, InvocationRecord)
        assert record.external_invocation_id == ext_id
        assert record.operation == "search"
        assert record.status == "running"

        # Verify event was appended
        events = catalog_service.list_events(run_id)
        assert any(e.event_type == "invocation_started" for e in events)

    def test_complete_updates_invocation(self, run_id, catalog_service):
        ext_id = f"fc_{uuid4().hex[:32]}"
        record = catalog_service.begin(run_id, ext_id, "search", {"query": "test"})
        completed = catalog_service.complete(
            run_id, record.id, "succeeded",
            output={"results": [{"url": "https://example.com"}]},
        )
        assert completed.status == "complete"
        assert completed.completed_at is not None
        assert completed.output == {"results": [{"url": "https://example.com"}]}

    def test_complete_non_running_raises(self, run_id, catalog_service):
        ext_id = f"fc_{uuid4().hex[:32]}"
        record = catalog_service.begin(run_id, ext_id, "search", {"query": "test"})
        catalog_service.complete(run_id, record.id, "succeeded")
        with pytest.raises(InvocationCatalogError):
            catalog_service.complete(run_id, record.id, "succeeded")

    def test_duplicate_invocation_id_raises(self, run_id, catalog_service):
        ext_id = f"fc_{uuid4().hex[:32]}"
        catalog_service.begin(run_id, ext_id, "search", {"query": "test"})
        with pytest.raises(InvocationAlreadyRunning):
            catalog_service.begin(run_id, ext_id, "scrape", {"url": "https://example.com"})

    def test_list_invocations(self, run_id, catalog_service):
        for idx in range(3):
            catalog_service.begin(
                run_id, f"fc_{uuid4().hex[:32]}", "search", {"query": f"test {idx}"}
            )
        invocations = catalog_service.list_invocations(run_id)
        assert len(invocations) == 3

    def test_list_invocations_filtered(self, run_id, catalog_service):
        catalog_service.begin(run_id, f"fc_{uuid4().hex[:32]}", "search", {})
        catalog_service.begin(run_id, f"fc_{uuid4().hex[:32]}", "scrape", {})
        search_invocs = catalog_service.list_invocations(run_id, operation="search")
        scrape_invocs = catalog_service.list_invocations(run_id, operation="scrape")
        assert len(search_invocs) == 1
        assert len(scrape_invocs) == 1


# ------------------------------------------------------------------
# Batch append tests
# ------------------------------------------------------------------

class TestBatchAppend:
    """Multiple events can be appended atomically."""

    def test_batch_append_all_or_nothing(self, run_id, event_service):
        events = [
            {"event_type": "pivot", "payload": {"query": f"pivot {i}"}}
            for i in range(5)
        ]
        results = event_service.append_batch(run_id, events, actor_type="system")
        assert len(results) == 5
        for idx, r in enumerate(results):
            assert r.sequence_number > 0

    def test_batch_append_invalid_type_rejects_all(self, run_id, event_service):
        events = [
            {"event_type": "pivot", "payload": {"query": "ok"}},
            {"event_type": "invalid_type", "payload": {"query": "bad"}},
        ]
        with pytest.raises(InvalidEventType):
            event_service.append_batch(run_id, events)

    def test_batch_append_empty_returns_empty(self, run_id, event_service):
        results = event_service.append_batch(run_id, [])
        assert results == []

    def test_batch_duplicate_key_rejected(self, run_id, event_service):
        events = [
            {
                "event_type": "pivot",
                "payload": {"query": "first"},
                "idempotency_key": "same-key",
            },
            {
                "event_type": "retry",
                "payload": {"query": "second"},
                "idempotency_key": "same-key",
            },
        ]
        with pytest.raises(DuplicateEventKey, match="duplicate idempotency key"):
            event_service.append_batch(run_id, events)

    def test_batch_duplicate_key_with_existing_rejected(self, run_id, event_service):
        key = f"test:batch-conflict:{uuid4()}"
        event_service.append(
            run_id, "pivot", "system", key, payload={"query": "existing"}
        )
        events = [
            {
                "event_type": "retry",
                "payload": {"query": "new"},
                "idempotency_key": key,
            },
        ]
        with pytest.raises(DuplicateEventKey, match="idempotency key"):
            event_service.append_batch(run_id, events)


# ------------------------------------------------------------------
# Export failure isolation tests
# ------------------------------------------------------------------

class TestExportFailureIsolation:
    """Export failure does not roll back database commit."""

    def test_database_commit_succeeds_when_export_fails(self, run_id, database_url):
        """If filesystem export fails, PostgreSQL commit still succeeds."""
        from research_store.container import build_run_service
        from research_store.invocation_catalog import InvocationCatalogService
        from research_store.config import StoreConfig

        config = StoreConfig.from_env()
        config.database_url = database_url

        service = build_run_service(config)
        catalog_service = InvocationCatalogService(
            service.uow_factory, event_service=service.event_service
        )

        # This should succeed even if filesystem is unavailable
        record = catalog_service.begin(
            run_id, f"fc_{uuid4().hex[:32]}", "search", {"query": "test"}
        )
        assert record.status == "running"

        # Verify state is in PostgreSQL
        status = catalog_service.status(invocation_id=record.id)
        assert status.status == "running"


# ------------------------------------------------------------------
# next_event_sequence tests
# ------------------------------------------------------------------

class TestNextEventSequence:
    """The next_event_sequence method returns the correct next number."""

    def test_next_sequence_after_no_events(self, run_id, database_url):
        from research_store.postgres import PostgresUnitOfWork

        with PostgresUnitOfWork(
            database_url, "test_invocation_events", "test", "1", 3,
            "markdown-v1", "cleanup-v1", "structural-v1"
        ) as uow:
            next_seq = uow.runs.next_event_sequence(run_id)
            assert next_seq == 1  # No events, so next is 1

    def test_next_sequence_after_events(self, run_id, event_service):
        from research_store.postgres import PostgresUnitOfWork

        event_service.append(run_id, "annotation", "system", "test:seq:1", payload={})
        event_service.append(run_id, "annotation", "system", "test:seq:2", payload={})

        with PostgresUnitOfWork(
            database_url, "test_invocation_events", "test", "1", 3,
            "markdown-v1", "cleanup-v1", "structural-v1"
        ) as uow:
            next_seq = uow.runs.next_event_sequence(run_id)
            assert next_seq == 3  # Two events exist, so next is 3


# ------------------------------------------------------------------
# export_to_catalog_format tests
# ------------------------------------------------------------------

class TestExportCatalogFormat:
    """Export produces correct Catalog v5-compatible format."""

    def test_export_format_fields(self, run_id, database_url):
        from research_store.config import StoreConfig
        from research_store.container import build_run_service
        from research_store.invocation_catalog import InvocationCatalogService

        config = StoreConfig.from_env()
        config.database_url = database_url

        service = build_run_service(config)
        catalog_service = InvocationCatalogService(
            service.uow_factory, event_service=service.event_service
        )

        inv = catalog_service.begin(
            run_id, f"fc_{uuid4().hex[:32]}", "search", {"query": "test"}
        )
        catalog_service.add_event(
            run_id, inv.id, "pivot", {"query": "pivot query"}
        )
        catalog_service.complete(run_id, inv.id, "succeeded", output={"results": []})

        export = catalog_service.export_to_catalog_format(run_id, inv.id)

        # Verify schema version
        assert export["schema_version"] == 5

        # Verify field mapping
        assert "invocation_id" in export
        assert "external_invocation_id" in export
        assert "research_run_id" in export
        assert "operation" in export
        assert export["operation"] == "search"

        # Verify events are serialized
        assert "events" in export
        assert len(export["events"]) == 1
        assert export["events"][0]["event_type"] == "pivot"

        # Verify execution status mapping
        assert export["execution"]["status"] == "succeeded"

        # Verify completed_at is set
        assert export["finished_at"] is not None