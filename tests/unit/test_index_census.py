from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Self
from uuid import UUID

import pytest

from firecrawl_skill.research_store.index_census import (
    CENSUS_CLASSES,
    census_index_jobs,
)

SNAPSHOT = datetime(2026, 8, 4, 20, 0, tzinfo=timezone.utc)
FINGERPRINT = "census-test-fingerprint"
DEFINITION_ID = UUID(int=50_000)


class _Cursor:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, query: str, params: tuple[Any, ...]) -> None:
        self.connection.executions.append((query, params))

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self.connection.rows)


class _Connection:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.rows = rows
        self.executions: list[tuple[str, tuple[Any, ...]]] = []

    def cursor(self) -> _Cursor:
        return _Cursor(self)


def _job(
    entity_id: UUID,
    manifest_id: UUID,
    status: str,
    *,
    attempt_count: int = 0,
    lease_expires_at: datetime | None = None,
    lease_owner: str | None = None,
    heartbeat_at: datetime | None = None,
    available_at: datetime | None = None,
    completed_at: datetime | None = None,
) -> dict[str, Any]:
    running = status == "running"
    return {
        "job_id": str(UUID(int=entity_id.int + 20_000)),
        "manifest_id": str(manifest_id),
        "index_definition_id": str(DEFINITION_ID),
        "entity_type": "chunk",
        "entity_id": str(entity_id),
        "operation": "upsert",
        "index_name": "research_chunks_active",
        "status": status,
        "attempt_count": attempt_count,
        "available_at": (available_at or SNAPSHOT).isoformat(),
        "lease_token": str(UUID(int=entity_id.int + 30_000)) if running else None,
        "lease_owner": lease_owner if running else None,
        "lease_expires_at": lease_expires_at.isoformat()
        if lease_expires_at is not None
        else None,
        "completed_at": completed_at.isoformat() if completed_at is not None else None,
        "error": None,
        "heartbeat_at": heartbeat_at.isoformat() if heartbeat_at is not None else None,
    }


def _manifest(
    entity_id: UUID,
    status: str,
    jobs: list[dict[str, Any]],
    *,
    manifest_id: UUID | None = None,
    point_id: UUID | None = None,
) -> dict[str, Any]:
    manifest_id = manifest_id or UUID(int=entity_id.int + 10_000)
    return {
        "manifest_id": str(manifest_id),
        "chunk_id": str(entity_id),
        "index_definition_id": str(DEFINITION_ID),
        "index_status": status,
        "qdrant_point_id": str(point_id or entity_id),
        "qdrant_collection": "research_chunks_active",
        "physical_collection": "research_chunks_active",
        "indexed_at": SNAPSHOT.isoformat() if status == "complete" else None,
        "error": None,
        "jobs": jobs,
    }


def _row(
    ordinal: int,
    manifests: list[dict[str, Any]],
    *,
    other_fingerprints: list[str] | None = None,
    active_definition_count: int = 1,
) -> tuple[Any, ...]:
    return (
        SNAPSHOT,
        UUID(int=ordinal),
        ordinal,
        active_definition_count,
        manifests,
        other_fingerprints or [],
    )


def _state_row(ordinal: int, state: str) -> tuple[Any, ...]:
    entity_id = UUID(int=ordinal)
    manifest_id = UUID(int=entity_id.int + 10_000)
    if state == "missing_job":
        return _row(
            ordinal,
            [_manifest(entity_id, "pending", [], manifest_id=manifest_id)],
        )
    if state == "wrong_fingerprint":
        return _row(ordinal, [], other_fingerprints=["obsolete-fingerprint"])
    if state == "manifest_inconsistent":
        job = _job(
            entity_id,
            manifest_id,
            "complete",
            completed_at=SNAPSHOT - timedelta(minutes=1),
        )
        return _row(
            ordinal,
            [_manifest(entity_id, "pending", [job], manifest_id=manifest_id)],
        )
    if state == "complete":
        job = _job(
            entity_id,
            manifest_id,
            "complete",
            completed_at=SNAPSHOT - timedelta(minutes=1),
        )
        return _row(
            ordinal,
            [_manifest(entity_id, "complete", [job], manifest_id=manifest_id)],
        )
    if state == "claimable":
        job = _job(entity_id, manifest_id, "pending")
        return _row(
            ordinal,
            [_manifest(entity_id, "pending", [job], manifest_id=manifest_id)],
        )
    if state == "running_live":
        job = _job(
            entity_id,
            manifest_id,
            "running",
            attempt_count=1,
            lease_expires_at=SNAPSHOT + timedelta(minutes=5),
            lease_owner="worker-live",
            heartbeat_at=SNAPSHOT - timedelta(seconds=10),
        )
        return _row(
            ordinal,
            [_manifest(entity_id, "pending", [job], manifest_id=manifest_id)],
        )
    if state == "running_expired":
        job = _job(
            entity_id,
            manifest_id,
            "running",
            attempt_count=2,
            lease_expires_at=SNAPSHOT - timedelta(minutes=2),
            lease_owner="worker-expired",
            heartbeat_at=SNAPSHOT - timedelta(minutes=3),
        )
        return _row(
            ordinal,
            [_manifest(entity_id, "failed", [job], manifest_id=manifest_id)],
        )
    if state == "retryable_failed":
        job = _job(
            entity_id,
            manifest_id,
            "failed",
            attempt_count=2,
            available_at=SNAPSHOT + timedelta(minutes=1),
        )
        return _row(
            ordinal,
            [_manifest(entity_id, "failed", [job], manifest_id=manifest_id)],
        )
    if state == "dead":
        job = _job(entity_id, manifest_id, "dead", attempt_count=5)
        return _row(
            ordinal,
            [_manifest(entity_id, "failed", [job], manifest_id=manifest_id)],
        )
    raise AssertionError(state)


def test_census_classifies_every_state_once_from_one_postgresql_statement() -> None:
    rows = [_state_row(index, state) for index, state in enumerate(CENSUS_CLASSES, 1)]
    connection = _Connection(rows)
    entity_ids = [UUID(int=index) for index in range(1, len(rows) + 1)]

    census = census_index_jobs(
        connection,
        entity_ids,
        FINGERPRINT,
        max_attempts=5,
        representative_limit=1,
    )

    assert census["expected"] == len(CENSUS_CLASSES)
    assert census["count_conserved"] is True
    assert sum(census[state] for state in CENSUS_CLASSES) == census["expected"]
    assert all(census[state] == 1 for state in CENSUS_CLASSES)
    assert census["complete_manifests"] == census["complete"] == 1
    assert all(
        len(census["representative_entity_ids"][state]) == 1
        for state in CENSUS_CLASSES
        if state != "complete"
    )

    assert census["decision_evidence"]["wait"]["count"] == 1
    assert census["decision_evidence"]["reclaim"]["count"] == 1
    assert census["decision_evidence"]["retry"]["count"] == 2
    assert census["decision_evidence"]["fail"]["count"] == 4
    assert census["latest_relevant_worker_heartbeat"] == {
        "worker_id": "worker-live",
        "heartbeat_at": (SNAPSHOT - timedelta(seconds=10)).isoformat(),
        "authoritative_for_counts": False,
    }
    assert census["lease_expiration_bounds"] == {
        "earliest": (SNAPSHOT - timedelta(minutes=2)).isoformat(),
        "latest": (SNAPSHOT + timedelta(minutes=5)).isoformat(),
    }

    assert len(connection.executions) == 1
    query, params = connection.executions[0]
    normalized = " ".join(query.lower().split())
    for required in (
        "unnest(%s::uuid[])",
        "index_definitions",
        "embedding_manifests",
        "index_jobs",
        "index_worker_heartbeats",
        "statement_timestamp()",
    ):
        assert required in normalized
    assert params == (entity_ids, FINGERPRINT, FINGERPRINT)


def test_audited_census_reports_1344_complete_and_32_live() -> None:
    rows = [_state_row(index, "complete") for index in range(1, 1_345)] + [
        _state_row(index, "running_live") for index in range(1_345, 1_377)
    ]
    entity_ids = [UUID(int=index) for index in range(1, 1_377)]

    census = census_index_jobs(_Connection(rows), entity_ids, FINGERPRINT)

    assert census["expected"] == 1_376
    assert census["complete"] == 1_344
    assert census["running_live"] == 32
    assert census["claimable"] == 0
    assert all(
        census[state] == 0
        for state in CENSUS_CLASSES
        if state not in {"complete", "running_live"}
    )
    assert sum(census[state] for state in CENSUS_CLASSES) == 1_376


@pytest.mark.parametrize(
    "mutator",
    [
        lambda entity, manifest: manifest.update(qdrant_point_id=str(UUID(int=999))),
        lambda entity, manifest: manifest["jobs"][0].update(
            entity_id=str(UUID(int=998))
        ),
        lambda entity, manifest: manifest["jobs"][0].update(operation="delete"),
        lambda entity, manifest: manifest["jobs"][0].update(index_name="wrong"),
        lambda entity, manifest: manifest.update(qdrant_collection="wrong"),
        lambda entity, manifest: manifest["jobs"][0].update(lease_token=None),
        lambda entity, manifest: manifest.update(index_status="complete"),
    ],
)
def test_manifest_and_job_inconsistencies_fail_closed(mutator) -> None:
    entity_id = UUID(int=1)
    manifest_id = UUID(int=10_001)
    job = _job(
        entity_id,
        manifest_id,
        "running",
        lease_expires_at=SNAPSHOT + timedelta(minutes=5),
        lease_owner="worker",
        heartbeat_at=SNAPSHOT,
    )
    manifest = _manifest(entity_id, "pending", [job], manifest_id=manifest_id)
    mutator(entity_id, manifest)

    census = census_index_jobs(
        _Connection([_row(1, [manifest])]),
        [entity_id],
        FINGERPRINT,
    )

    assert census["manifest_inconsistent"] == 1
    assert census["expected"] == 1


def test_multiple_active_manifests_are_inconsistent() -> None:
    entity_id = UUID(int=1)
    first_id = UUID(int=10_001)
    second_id = UUID(int=10_002)
    first = _manifest(
        entity_id,
        "pending",
        [_job(entity_id, first_id, "pending")],
        manifest_id=first_id,
    )
    second = _manifest(
        entity_id,
        "pending",
        [_job(entity_id, second_id, "pending")],
        manifest_id=second_id,
    )

    census = census_index_jobs(
        _Connection([_row(1, [first, second])]),
        [entity_id],
        FINGERPRINT,
    )

    assert census["manifest_inconsistent"] == 1


def test_failed_job_is_claimable_only_after_backoff_expires() -> None:
    entity_id = UUID(int=1)
    manifest_id = UUID(int=10_001)

    available = _job(
        entity_id,
        manifest_id,
        "failed",
        attempt_count=2,
        available_at=SNAPSHOT,
    )
    delayed = _job(
        entity_id,
        manifest_id,
        "failed",
        attempt_count=2,
        available_at=SNAPSHOT + timedelta(seconds=1),
    )

    available_census = census_index_jobs(
        _Connection(
            [
                _row(
                    1,
                    [
                        _manifest(
                            entity_id,
                            "failed",
                            [available],
                            manifest_id=manifest_id,
                        )
                    ],
                )
            ]
        ),
        [entity_id],
        FINGERPRINT,
    )
    delayed_census = census_index_jobs(
        _Connection(
            [
                _row(
                    1,
                    [
                        _manifest(
                            entity_id,
                            "failed",
                            [delayed],
                            manifest_id=manifest_id,
                        )
                    ],
                )
            ]
        ),
        [entity_id],
        FINGERPRINT,
    )

    assert available_census["claimable"] == 1
    assert delayed_census["retryable_failed"] == 1


def test_expired_final_attempt_is_dead_not_reclaimable() -> None:
    entity_id = UUID(int=1)
    manifest_id = UUID(int=10_001)
    job = _job(
        entity_id,
        manifest_id,
        "running",
        attempt_count=5,
        lease_expires_at=SNAPSHOT - timedelta(seconds=1),
        lease_owner="worker",
        heartbeat_at=SNAPSHOT - timedelta(seconds=2),
    )

    census = census_index_jobs(
        _Connection(
            [
                _row(
                    1,
                    [
                        _manifest(
                            entity_id,
                            "failed",
                            [job],
                            manifest_id=manifest_id,
                        )
                    ],
                )
            ]
        ),
        [entity_id],
        FINGERPRINT,
        max_attempts=5,
    )

    assert census["dead"] == 1
    assert census["running_expired"] == 0


def test_unknown_active_fingerprint_fails_as_wrong_fingerprint() -> None:
    entity_id = UUID(int=1)
    connection = _Connection(
        [
            (
                SNAPSHOT,
                entity_id,
                1,
                0,
                [],
                [],
            )
        ]
    )

    census = census_index_jobs(connection, [entity_id], FINGERPRINT)

    assert census["wrong_fingerprint"] == 1
    assert census["missing_job"] == 0


def test_census_rejects_duplicate_sealed_membership_before_query() -> None:
    entity_id = UUID(int=1)
    connection = _Connection([])

    with pytest.raises(ValueError, match="duplicates"):
        census_index_jobs(connection, [entity_id, entity_id], FINGERPRINT)

    assert connection.executions == []


class _WorkerRepository:
    def __init__(self, entity_ids: list[UUID]) -> None:
        self.entity_ids = entity_ids
        self.claim_options: dict[str, Any] | None = None
        self.census_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def claim_jobs(self, limit: int, **options: Any) -> list[dict[str, Any]]:
        assert limit == 64
        self.claim_options = options
        return []

    def heartbeat_worker(self, _worker_id: str, _metadata: dict[str, Any]) -> None:
        return None

    def census_index_jobs(self, *args: Any, **kwargs: Any) -> dict[str, int]:
        self.census_calls.append((args, kwargs))
        return {
            "expected": len(self.entity_ids),
            "complete": len(self.entity_ids) - 1,
            "claimable": 0,
            "running_live": 1,
            "running_expired": 0,
            "retryable_failed": 0,
            "dead": 0,
            "missing_job": 0,
            "wrong_fingerprint": 0,
            "manifest_inconsistent": 0,
        }


class _WorkerUow:
    def __init__(self, repository: Any) -> None:
        self.index_jobs = repository

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def test_worker_attaches_exact_census_after_zero_claim_batch() -> None:
    from types import SimpleNamespace

    from firecrawl_skill.research_store.indexing import IndexWorker

    entity_ids = [UUID(int=1), UUID(int=2)]
    repository = _WorkerRepository(entity_ids)
    worker = IndexWorker(
        uow_factory=lambda: _WorkerUow(repository),
        index=SimpleNamespace(),
        embedder=SimpleNamespace(fingerprint=FINGERPRINT),
        worker_id="census-worker",
    )

    result = worker.run_batch(limit=64, entity_ids=entity_ids)

    assert result["claimed"] == 0
    assert result["complete"] == 0
    assert result["census"]["expected"] == 2
    assert result["census"]["complete"] == 1
    assert result["complete_manifests"] == 1
    assert result["running_live"] == 1
    assert repository.claim_options is not None
    assert repository.claim_options["entity_ids"] == entity_ids
    assert repository.claim_options["fingerprint"] == FINGERPRINT
    assert repository.census_calls == [
        (
            (entity_ids, FINGERPRINT),
            {"max_attempts": worker.max_attempts},
        )
    ]


@pytest.mark.parametrize("limit", [1, 64])
@pytest.mark.parametrize("fingerprint", [None, "", "   ", 42])
def test_scoped_worker_requires_fingerprint_before_any_side_effect(
    limit: int,
    fingerprint: object,
) -> None:
    from types import SimpleNamespace

    from firecrawl_skill.research_store.indexing import IndexWorker

    side_effects: list[str] = []

    def forbidden_uow() -> None:
        side_effects.append("unit_of_work")
        raise AssertionError("scoped validation must run before any unit of work")

    worker = IndexWorker(
        uow_factory=forbidden_uow,
        index=SimpleNamespace(),
        embedder=SimpleNamespace(fingerprint=fingerprint),
        worker_id="fail-closed-census-worker",
    )

    with pytest.raises(ValueError, match="active index fingerprint"):
        worker.run_batch(limit=limit, entity_ids=[UUID(int=1)])

    assert side_effects == []


class _SequencedWorkerRepository:
    def __init__(self, entity_ids: list[UUID]) -> None:
        self.entity_ids = entity_ids
        self.claim_batches = [
            [{"id": UUID(int=101)}, {"id": UUID(int=102)}],
            [{"id": UUID(int=103)}],
            [],
        ]
        self.census_totals = [2, 3, 3]
        self.claim_calls: list[tuple[int, dict[str, Any]]] = []
        self.census_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def claim_jobs(self, limit: int, **options: Any) -> list[dict[str, Any]]:
        self.claim_calls.append((limit, options))
        return self.claim_batches.pop(0)

    def heartbeat_worker(self, _worker_id: str, _metadata: dict[str, Any]) -> None:
        return None

    def census_index_jobs(self, *args: Any, **kwargs: Any) -> dict[str, int]:
        observation = len(self.census_calls)
        self.census_calls.append((args, kwargs))
        complete = self.census_totals[observation]
        return {
            "expected": len(self.entity_ids),
            "complete": complete,
            "complete_manifests": complete,
            "claimable": 0,
            "running_live": len(self.entity_ids) - complete,
            "running_expired": 0,
            "retryable_failed": 0,
            "dead": 0,
            "missing_job": 0,
            "wrong_fingerprint": 0,
            "manifest_inconsistent": 0,
        }


def test_worker_preserves_batch_completion_deltas_across_census_observations() -> None:
    from types import SimpleNamespace

    from firecrawl_skill.research_store.indexing import IndexWorker

    class _BatchDeltaWorker(IndexWorker):
        def _process_microbatch(self, jobs: list[dict]) -> dict[str, Any]:
            return {
                "complete": len(jobs),
                "failed": 0,
                "lease_lost": 0,
                "embedding_batches": 1,
                "embedding_texts": len(jobs),
                "embedding_elapsed_seconds": 0.25,
            }

    entity_ids = [UUID(int=1), UUID(int=2), UUID(int=3)]
    repository = _SequencedWorkerRepository(entity_ids)
    worker = _BatchDeltaWorker(
        uow_factory=lambda: _WorkerUow(repository),
        index=SimpleNamespace(),
        embedder=SimpleNamespace(fingerprint=FINGERPRINT),
        worker_id="delta-preserving-census-worker",
    )

    observations = [worker.run_batch(limit=2, entity_ids=entity_ids) for _ in range(3)]

    assert [item["claimed"] for item in observations] == [2, 1, 0]
    assert [item["complete"] for item in observations] == [2, 1, 0]
    assert [item["complete_manifests"] for item in observations] == [2, 3, 3]
    assert [item["census"]["complete"] for item in observations] == [2, 3, 3]

    aggregate = {
        "complete": 0,
        "embedding_batches": 0,
        "embedding_texts": 0,
        "embedding_elapsed_seconds": 0.0,
    }
    for observation in observations:
        for key in aggregate:
            aggregate[key] += observation[key]

    assert aggregate == {
        "complete": 3,
        "embedding_batches": 2,
        "embedding_texts": 3,
        "embedding_elapsed_seconds": 0.5,
    }
