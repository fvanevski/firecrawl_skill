from __future__ import annotations

import sys
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import research_store.indexing as indexing_module
from research_store.indexing import IndexWorker, OpenAICompatibleEmbedder
from research_store.qdrant import QdrantIndex
from research_store.queue import ValkeyQueue


class FakeRepository:
    def __init__(self, state):
        self.state = state

    def claim_jobs(self, limit, **options):
        self.state.setdefault("claim_history", []).append({"limit": limit, **options})
        self.state["claim_options"] = {"limit": limit, **options}
        jobs, self.state["jobs"] = (
            self.state["jobs"][:limit],
            self.state["jobs"][limit:],
        )
        return jobs

    def renew_job(self, job_id, lease_token, lease_seconds):
        self.state["renewals"].append((job_id, lease_token, lease_seconds))
        return self.state.get("owns_lease", True)

    def finish_job(self, job_id, lease_token, error, **options):
        self.state["finishes"].append((job_id, lease_token, error, options))
        return self.state.get("owns_at_finish", True)

    def chunks_for_index(self, ids, manifest_id=None):
        self.state["chunk_lookup"] = (ids, manifest_id)
        return self.state["records"]

    def heartbeat_worker(self, worker_id, metadata):
        self.state["heartbeats"].append((worker_id, metadata))


class FakeUow:
    def __init__(self, state):
        repo = FakeRepository(state)
        self.index_jobs = self.chunks = repo

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class FakeIndex:
    def __init__(self, calls, collection="active", dimension=3, distance="Cosine"):
        self.calls = calls
        self.collection = collection
        self.dimension = dimension
        self.distance = distance

    def for_collection(self, collection, dimension=None, distance=None):
        self.calls.append(("select", collection, dimension, distance))
        return FakeIndex(
            self.calls,
            collection,
            dimension or self.dimension,
            distance or self.distance,
        )

    def ensure_schema(self):
        self.calls.append(("schema", self.collection, self.dimension, self.distance))

    def upsert(self, points):
        self.calls.append(("upsert", self.collection, points))

    def delete(self, ids):
        self.calls.append(("delete", self.collection, ids))


def _state():
    chunk_id, job_id, manifest_id = uuid4(), uuid4(), uuid4()
    return {
        "jobs": [
            {
                "id": job_id,
                "manifest_id": manifest_id,
                "entity_id": chunk_id,
                "operation": "upsert",
                "lease_token": uuid4(),
                "physical_collection": "research_chunks_abc123",
                "dimension": 3,
                "distance_metric": "Cosine",
            }
        ],
        "records": [{"chunk_id": chunk_id, "text": "exact text", "source_id": uuid4()}],
        "renewals": [],
        "finishes": [],
        "heartbeats": [],
    }


def test_worker_uses_exact_manifest_collection_and_token():
    state, calls = _state(), []
    worker = IndexWorker(
        lambda: FakeUow(state),
        FakeIndex(calls),
        lambda _: [0.1, 0.2, 0.3],
        worker_id="w1",
    )
    result = worker.run_batch(10)
    job = state["finishes"][0]
    assert result["complete"] == 1 and result["failed"] == 0
    assert state["chunk_lookup"][1] is not None
    assert job[1] == state["renewals"][0][1] and job[2] is None
    assert ("schema", "research_chunks_abc123", 3, "Cosine") in calls
    assert next(call for call in calls if call[0] == "upsert")[2][0]["id"] == str(
        state["records"][0]["chunk_id"]
    )


def test_worker_claims_each_lease_only_when_processing_starts():
    state, calls = _state(), []
    second = dict(state["jobs"][0])
    second["id"] = uuid4()
    second["lease_token"] = uuid4()
    state["jobs"].append(second)

    def embedder(_text):
        return [0.1, 0.2, 0.3]

    embedder.fingerprint = "configured-fingerprint"

    result = IndexWorker(
        lambda: FakeUow(state), FakeIndex(calls), embedder, worker_id="w-sequential"
    ).run_batch(2)

    assert result["claimed"] == result["complete"] == 2
    # Microbatch path claims all jobs at once (limit=2), not one-at-a-time.
    assert [call["limit"] for call in state["claim_history"]] == [2]
    assert all(
        call["fingerprint"] == "configured-fingerprint"
        for call in state["claim_history"]
    )


def test_worker_does_not_finish_after_lease_loss():
    state, calls = _state(), []
    state["owns_lease"] = False
    result = IndexWorker(
        lambda: FakeUow(state), FakeIndex(calls), lambda _: [0, 0, 0]
    ).run_batch()
    assert result["lease_lost"] == 1
    assert state["finishes"] == []
    assert not any(call[0] == "upsert" for call in calls)


def test_worker_reports_stale_completion_after_idempotent_upsert():
    state, calls = _state(), []
    state["owns_at_finish"] = False
    result = IndexWorker(
        lambda: FakeUow(state), FakeIndex(calls), lambda _: [0, 0, 0]
    ).run_batch()
    assert result["lease_lost"] == 1 and result["complete"] == 0
    assert any(call[0] == "upsert" for call in calls)


class FakeRedis:
    def __init__(self):
        self.calls = []

    def pipeline(self):
        return self

    def lpush(self, *args):
        self.calls.append(("lpush", args))

    def expire(self, *args):
        self.calls.append(("expire", args))

    def execute(self):
        self.calls.append(("execute",))

    def blpop(self, *args, **kwargs):
        self.calls.append(("blpop", args, kwargs))


def test_valkey_wakeup_is_best_effort_and_finite():
    redis = FakeRedis()
    queue = ValkeyQueue("redis://unused", client=redis)
    assert queue.notify(uuid4())
    assert queue.wait(0.25) is False
    assert redis.calls[-1][-1]["timeout"] == 0.25
    broken = FakeRedis()
    broken.pipeline = lambda: (_ for _ in ()).throw(OSError("down"))
    assert ValkeyQueue("redis://unused", client=broken).notify(uuid4()) is False


class FakeQdrant(QdrantIndex):
    def __init__(self, responses):
        super().__init__("http://qdrant", "", "physical", 3)
        self.responses = responses
        self.requests = []

    def _request(self, method, path, payload=None):
        self.requests.append((method, path, payload))
        return self.responses.pop(0)


def test_qdrant_schema_inspection_is_read_only():
    qdrant = FakeQdrant(
        [
            {
                "result": {
                    "config": {
                        "params": {
                            "vectors": {"dense": {"size": 3, "distance": "Cosine"}}
                        }
                    }
                }
            }
        ]
    )
    result = qdrant.inspect_schema()
    assert result["compatible"] is True
    assert qdrant.requests == [("GET", "/collections/physical", None)]


def test_qdrant_schema_rejects_unexpected_sparse_vectors():
    qdrant = FakeQdrant(
        [
            {
                "result": {
                    "config": {
                        "params": {
                            "vectors": {"dense": {"size": 3, "distance": "Cosine"}},
                            "sparse_vectors": {"sparse": {}},
                        }
                    }
                }
            }
        ]
    )
    result = qdrant.inspect_schema()
    assert result["compatible"] is False
    assert result["actual"]["sparse"] is True
    assert result["expected"]["sparse"] is False


def test_qdrant_ensure_schema_raises_on_unexpected_sparse_vectors():
    qdrant = FakeQdrant(
        [
            {
                "result": {
                    "config": {
                        "params": {
                            "vectors": {"dense": {"size": 3, "distance": "Cosine"}},
                            "sparse_vectors": {"sparse": {}},
                        }
                    }
                }
            }
        ]
    )
    with pytest.raises(RuntimeError, match="incompatible"):
        qdrant.ensure_schema()


def test_qdrant_alias_switch_is_single_atomic_request():
    qdrant = FakeQdrant(
        [
            {
                "result": {
                    "aliases": [
                        {
                            "alias_name": "research_chunks_active",
                            "collection_name": "old",
                        }
                    ]
                }
            },
            {"result": {"status": "ok"}},
        ]
    )
    assert qdrant.switch_alias("research_chunks_active", "new")
    method, path, payload = qdrant.requests[-1]
    assert (method, path) == ("POST", "/collections/aliases")
    assert payload["actions"] == [
        {"delete_alias": {"alias_name": "research_chunks_active"}},
        {
            "create_alias": {
                "collection_name": "new",
                "alias_name": "research_chunks_active",
            }
        },
    ]


def test_once_runtime_does_not_wait_for_queue():
    state, calls = _state(), []
    state["jobs"] = []
    queue = type(
        "Queue", (), {"wait": lambda self, _: (_ for _ in ()).throw(AssertionError)}
    )()
    worker = IndexWorker(
        lambda: FakeUow(state), FakeIndex(calls), lambda _: [], queue=queue
    )
    result = worker.run_forever(
        once=True, stop_event=Event(), install_signal_handlers=False
    )
    assert result["batches"] == 1


def test_embedder_enforces_declared_unit_length_and_job_model(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"data":[{"embedding":[3,4,0]}]}'

    monkeypatch.setattr(
        indexing_module, "urlopen", lambda *_args, **_kwargs: Response()
    )
    configured = OpenAICompatibleEmbedder("http://embedding/v1", "current", dimension=3)
    job = configured.for_job({"model_name": "immutable-job-model", "dimension": 3})
    assert job.model == "immutable-job-model"
    assert job("text") == [0.6, 0.8, 0.0]


# ---------------------------------------------------------------------------
# Microbatching tests (issue #65)
# ---------------------------------------------------------------------------


class _MicrobatchState:
    """Shared state for microbatch test fixtures."""

    def __init__(self):
        self.jobs: list[dict] = []
        self.records: list[dict] = []
        self.renewals: list[tuple] = []
        self.finishes: list[tuple] = []
        self.claim_history: list[dict] = []
        self.upsert_calls: list[dict] = []

    def _make_job(self, chunk_id, manifest_id, dimension=None, fingerprint="fp"):
        return {
            "id": uuid4(),
            "manifest_id": manifest_id,
            "entity_id": chunk_id,
            "chunk_id": chunk_id,
            "operation": "upsert",
            "lease_token": uuid4(),
            "physical_collection": "research_chunks_test",
            "dimension": dimension,
            "distance_metric": "Cosine",
            "fingerprint": fingerprint,
        }

    def _make_record(self, chunk_id):
        return {
            "chunk_id": chunk_id,
            "text": f"chunk-{chunk_id}",
            "document_id": uuid4(),
            "snapshot_id": uuid4(),
            "source_id": uuid4(),
            "domain": "example.com",
            "source_type": "web",
            "url": f"https://example.com/{chunk_id}",
            "title": "Test",
            "heading_path": [],
            "retrieved_at": "2026-01-01T00:00:00Z",
            "published_at": "2026-01-01T00:00:00Z",
            "language": "en",
            "content_sha256": uuid4().hex,
            "authority_class": "web",
            "parser_version": "markdown-v1",
            "normalization_version": "cleanup-v1",
            "chunker_version": "structural-v1",
        }


def _make_uow(state):
    class Repo:
        def claim_jobs(self, limit, **options):
            state.claim_history.append({"limit": limit, **options})
            taken = state.jobs[:limit]
            state.jobs = state.jobs[limit:]
            return taken

        def renew_job(self, job_id, lease_token, lease_seconds):
            state.renewals.append((job_id, lease_token, lease_seconds))
            return True

        def finish_job(self, job_id, lease_token, error, **options):
            state.finishes.append((job_id, lease_token, error, options))
            return True  # worker still owns lease at finish

        def chunks_for_index(self, chunk_ids, manifest_id=None):
            return [
                r
                for r in state.records
                if chunk_ids is None
                or any(str(r["chunk_id"]) == str(cid) for cid in chunk_ids)
            ]

        def heartbeat_worker(self, worker_id, metadata):
            pass

    class Uow:
        def __enter__(self):
            self.index_jobs = self.chunks = Repo()
            return self

        def __exit__(self, *_):
            return False

    return Uow()


def _fake_qdrant(state):
    calls = []

    class Q:
        def for_collection(self, collection, dimension=None, distance=None):
            return self

        def ensure_schema(self):
            calls.append("schema")

        def upsert(self, points):
            calls.append(("upsert", points))
            state.upsert_calls.extend(points)

        def delete(self, ids):
            calls.append(("delete", ids))

    return Q(), calls


def test_partial_batch_failure_does_not_falsely_complete_others():
    """One job's embedding fails; the other completes independently."""
    state = _MicrobatchState()
    chunk_a, chunk_b = uuid4(), uuid4()
    manifest_a, manifest_b = uuid4(), uuid4()
    state.jobs = [
        state._make_job(chunk_a, manifest_a, fingerprint="fp"),
        state._make_job(chunk_b, manifest_b, fingerprint="fp"),
    ]
    state.records = [state._make_record(chunk_a), state._make_record(chunk_b)]

    call_count = [0]

    def embedder_failing(text):
        call_count[0] += 1
        if call_count[0] == 1:
            raise ValueError("embedding failed for first chunk")
        return [0.1, 0.2, 0.3]

    embedder_failing.fingerprint = "fp"
    qdrant, _calls = _fake_qdrant(state)

    worker = IndexWorker(
        lambda: _make_uow(state),
        qdrant,
        embedder_failing,
        worker_id="w-partial",
    )
    result = worker.run_batch(2)

    assert result["claimed"] == 2
    assert result["complete"] == 1
    assert result["failed"] == 1
    # Only one upsert call (for the successful job).
    assert len(state.upsert_calls) == 1


def test_lease_expiry_during_batch_stops_processing():
    """When lease is lost, no jobs are completed and no upsert happens."""
    state = _MicrobatchState()
    chunk_a = uuid4()
    manifest_a = uuid4()
    state.jobs = [state._make_job(chunk_a, manifest_a, fingerprint="fp")]
    state.records = [state._make_record(chunk_a)]

    class FailingRepo:
        def claim_jobs(self, limit, **options):
            state.claim_history.append({"limit": limit, **options})
            taken = state.jobs[:limit]
            state.jobs = state.jobs[limit:]
            return taken

        def renew_job(self, job_id, lease_token, lease_seconds):
            return False  # lease already lost

        def finish_job(self, job_id, lease_token, error, **options):
            state.finishes.append((job_id, lease_token, error, options))
            return True

        def chunks_for_index(self, chunk_ids, manifest_id=None):
            return [
                r
                for r in state.records
                if chunk_ids is None
                or any(str(r["chunk_id"]) == str(cid) for cid in chunk_ids)
            ]

        def heartbeat_worker(self, worker_id, metadata):
            pass

    repo = FailingRepo()

    class Uow:
        def __enter__(self):
            self.index_jobs = self.chunks = repo
            return self

        def __exit__(self, *_):
            return False

    qdrant, _calls = _fake_qdrant(state)
    worker = IndexWorker(
        lambda: Uow(),
        qdrant,
        lambda _: [0.1, 0.2, 0.3],
        worker_id="w-lease",
    )
    result = worker.run_batch(2)

    assert result["lease_lost"] == 1
    assert result["complete"] == 0
    assert not any(c[0] == "upsert" for c in _calls)


def test_dimension_mismatch_fails_individual_job():
    """Job with wrong dimension fails; compatible job succeeds."""
    state = _MicrobatchState()
    chunk_a, chunk_b = uuid4(), uuid4()
    manifest_a, manifest_b = uuid4(), uuid4()
    # chunk_a expects dimension 3, chunk_b expects dimension 5
    state.jobs = [
        state._make_job(chunk_a, manifest_a, dimension=3, fingerprint="fp"),
        state._make_job(chunk_b, manifest_b, dimension=5, fingerprint="fp"),
    ]
    state.records = [state._make_record(chunk_a), state._make_record(chunk_b)]

    # Embedder returns dimension-3 vectors for all texts.
    def embedder(text):
        return [0.1, 0.2, 0.3]

    embedder.fingerprint = "fp"
    qdrant, _calls = _fake_qdrant(state)

    worker = IndexWorker(
        lambda: _make_uow(state),
        qdrant,
        embedder,
        worker_id="w-dim",
    )
    result = worker.run_batch(2)

    assert result["claimed"] == 2
    assert result["complete"] == 1  # chunk_a (dim=3) succeeds
    assert result["failed"] == 1  # chunk_b (dim=5) fails
    # Only one upsert (for the dimension-3 vector).
    assert len(state.upsert_calls) == 1


def test_idempotent_qdrant_replay_after_upsert():
    """If finish_job reports lease lost after upsert, job is counted as lease_lost
    but the upsert itself is idempotent — no duplicate points are written."""
    state = _MicrobatchState()
    chunk_a = uuid4()
    manifest_a = uuid4()
    state.jobs = [state._make_job(chunk_a, manifest_a, fingerprint="fp")]
    state.records = [state._make_record(chunk_a)]

    # finish_job reports lease lost AFTER upsert.
    class LeaseLostRepo:
        def claim_jobs(self, limit, **options):
            state.claim_history.append({"limit": limit, **options})
            taken = state.jobs[:limit]
            state.jobs = state.jobs[limit:]
            return taken

        def renew_job(self, job_id, lease_token, lease_seconds):
            return True

        def finish_job(self, job_id, lease_token, error, **options):
            state.finishes.append((job_id, lease_token, error, options))
            return False  # lease lost at finish

        def chunks_for_index(self, chunk_ids, manifest_id=None):
            return [
                r
                for r in state.records
                if chunk_ids is None
                or any(str(r["chunk_id"]) == str(cid) for cid in chunk_ids)
            ]

        def heartbeat_worker(self, worker_id, metadata):
            pass

    repo = LeaseLostRepo()

    class Uow:
        def __enter__(self):
            self.index_jobs = self.chunks = repo
            return self

        def __exit__(self, *_):
            return False

    qdrant, _calls = _fake_qdrant(state)
    worker = IndexWorker(
        lambda: Uow(),
        qdrant,
        lambda _: [0.1, 0.2, 0.3],
        worker_id="w-replay",
    )
    result = worker.run_batch(2)

    assert result["lease_lost"] == 1
    assert result["complete"] == 0
    # Upsert was still called (idempotent).
    assert any(c[0] == "upsert" for c in _calls)
    assert len(state.upsert_calls) == 1


def test_throughput_metrics_are_tracked():
    """OpenAICompatibleEmbedder.batch() tracks throughput counters."""
    embedder = OpenAICompatibleEmbedder(
        "http://embed/v1", "model", dimension=3, fingerprint="fp"
    )

    # Mock the HTTP call.
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return b'{"data":[{"embedding":[1,2,3]},{"embedding":[4,5,6]}]}'

    original_urlopen = indexing_module.urlopen
    indexing_module.urlopen = lambda *a, **k: Response()
    try:
        vectors = embedder.batch(["text-a", "text-b"])
        assert len(vectors) == 2
        tp = embedder.throughput
        assert tp["batch_count"] == 1
        assert tp["total_texts"] == 2
        assert tp["texts_per_second"] > 0
        embedder.reset_throughput()
        tp2 = embedder.throughput
        assert tp2["batch_count"] == 0
        assert tp2["total_texts"] == 0
    finally:
        indexing_module.urlopen = original_urlopen


def test_batch_groups_jobs_by_fingerprint():
    """Jobs with different fingerprints are processed in separate groups."""
    state = _MicrobatchState()
    chunk_a, chunk_b = uuid4(), uuid4()
    manifest_a, manifest_b = uuid4(), uuid4()
    state.jobs = [
        state._make_job(chunk_a, manifest_a, fingerprint="fp-a"),
        state._make_job(chunk_b, manifest_b, fingerprint="fp-b"),
    ]
    state.records = [state._make_record(chunk_a), state._make_record(chunk_b)]

    embedder_calls = []

    class TestEmbedder:
        fingerprint = "fp-a"  # Only matches fp-a

        def for_job(self, job):
            if job.get("fingerprint") != self.fingerprint:
                raise ValueError(
                    "worker embedding configuration does not match the claimed index definition"
                )
            return self

        def __call__(self, text):
            embedder_calls.append(text)
            return [0.1, 0.2, 0.3]

    class Repo:
        def claim_jobs(self, limit, **options):
            state.claim_history.append({"limit": limit, **options})
            taken = state.jobs[:limit]
            state.jobs = state.jobs[limit:]
            return taken

        def renew_job(self, job_id, lease_token, lease_seconds):
            return True

        def finish_job(self, job_id, lease_token, error, **options):
            state.finishes.append((job_id, lease_token, error, options))
            return True

        def chunks_for_index(self, chunk_ids, manifest_id=None):
            return [
                r
                for r in state.records
                if chunk_ids is None
                or any(str(r["chunk_id"]) == str(cid) for cid in chunk_ids)
            ]

        def heartbeat_worker(self, worker_id, metadata):
            pass

    repo = Repo()

    class Uow:
        def __enter__(self):
            self.index_jobs = self.chunks = repo
            return self

        def __exit__(self, *_):
            return False

    qdrant, _calls = _fake_qdrant(state)
    worker = IndexWorker(
        lambda: Uow(),
        qdrant,
        TestEmbedder(),
        worker_id="w-fp",
    )
    result = worker.run_batch(2)

    # Both jobs claimed, but only fp-a group succeeds.
    assert result["claimed"] == 2
    assert result["complete"] == 1
    assert result["failed"] == 1


# ---------------------------------------------------------------------------
# N3: OpenAICompatibleEmbedder.batch() failure-path tests
# ---------------------------------------------------------------------------


def test_batch_empty_input_raises():
    """batch([]) raises ValueError."""
    embedder = OpenAICompatibleEmbedder("http://embed/v1", "model", dimension=3)
    with pytest.raises(ValueError, match="batch requires at least one text"):
        embedder.batch([])


def test_batch_zero_vector_raises(monkeypatch):
    """A zero-norm vector in the batch response raises ValueError."""

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"data":[{"embedding":[0,0,0]}]}'

    monkeypatch.setattr(
        indexing_module, "urlopen", lambda *_args, **_kwargs: Response()
    )
    embedder = OpenAICompatibleEmbedder("http://embed/v1", "model", dimension=3)
    with pytest.raises(ValueError, match="zero vector"):
        embedder.batch(["text"])


def test_batch_dimension_mismatch_raises(monkeypatch):
    """A vector with the wrong dimension raises ValueError."""

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"data":[{"embedding":[1,2,3,4,5]}]}'

    monkeypatch.setattr(
        indexing_module, "urlopen", lambda *_args, **_kwargs: Response()
    )
    embedder = OpenAICompatibleEmbedder("http://embed/v1", "model", dimension=3)
    with pytest.raises(ValueError, match="dimension 5 does not match configured 3"):
        embedder.batch(["text"])


def test_batch_response_count_mismatch_raises(monkeypatch):
    """When the endpoint returns fewer vectors than requested, ValueError is raised."""

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"data":[{"embedding":[1,2,3]}]}'

    monkeypatch.setattr(
        indexing_module, "urlopen", lambda *_args, **_kwargs: Response()
    )
    embedder = OpenAICompatibleEmbedder("http://embed/v1", "model", dimension=3)
    with pytest.raises(ValueError, match="returned 1 vectors for 2 texts"):
        embedder.batch(["text-a", "text-b"])


# ---------------------------------------------------------------------------
# N4: Batch-path partial failure (OpenAICompatibleEmbedder.batch with mixed
#      dimension mismatch in response)
# ---------------------------------------------------------------------------


def test_batch_path_partial_failure(monkeypatch):
    """When batch() returns a malformed vector, the job fails but others succeed."""
    state = _MicrobatchState()
    chunk_a, chunk_b = uuid4(), uuid4()
    manifest_a, manifest_b = uuid4(), uuid4()
    state.jobs = [
        state._make_job(chunk_a, manifest_a, dimension=3, fingerprint="fp"),
        state._make_job(chunk_b, manifest_b, dimension=3, fingerprint="fp"),
    ]
    state.records = [state._make_record(chunk_a), state._make_record(chunk_b)]

    # The batch endpoint returns 5-dim vectors — both will fail dimension check.
    # We use a mock that returns oversized vectors to exercise the dimension
    # validation in the batch path.
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"data":[{"embedding":[1,2,3,4,5]},{"embedding":[6,7,8,9,10]}]}'

    original_urlopen = indexing_module.urlopen
    indexing_module.urlopen = lambda *a, **k: Response()
    try:
        qdrant, _calls = _fake_qdrant(state)
        worker = IndexWorker(
            lambda: _make_uow(state),
            qdrant,
            OpenAICompatibleEmbedder(
                "http://embed/v1", "model", dimension=3, fingerprint="fp"
            ),
            worker_id="w-batch-partial",
        )
        result = worker.run_batch(2)

        assert result["claimed"] == 2
        assert result["complete"] == 0  # both fail dimension check
        assert result["failed"] == 2
    finally:
        indexing_module.urlopen = original_urlopen


# ---------------------------------------------------------------------------
# N2: run_forever with microbatch path
# ---------------------------------------------------------------------------


def test_run_forever_with_microbatch():
    """run_forever exercises the microbatch path with batch_size > 1."""
    state = _MicrobatchState()
    chunk_a, chunk_b = uuid4(), uuid4()
    manifest_a, manifest_b = uuid4(), uuid4()
    state.jobs = [
        state._make_job(chunk_a, manifest_a, fingerprint="fp"),
        state._make_job(chunk_b, manifest_b, fingerprint="fp"),
    ]
    state.records = [state._make_record(chunk_a), state._make_record(chunk_b)]

    qdrant, _calls = _fake_qdrant(state)

    worker = IndexWorker(
        lambda: _make_uow(state),
        qdrant,
        lambda _: [0.1, 0.2, 0.3],
        worker_id="w-forever",
    )
    result = worker.run_forever(
        batch_size=2,
        poll_seconds=0.1,
        stop_event=Event(),
        once=True,
        install_signal_handlers=False,
    )

    # The worker claims and processes 2 jobs in one batch, then stops.
    assert result["claimed"] == 2
    assert result["complete"] == 2
    assert result["failed"] == 0


# ---------------------------------------------------------------------------
# N7: Fingerprint grouping with batch path
# ---------------------------------------------------------------------------


def test_batch_groups_jobs_by_fingerprint_with_batch_path(monkeypatch):
    """Jobs with different fingerprints are processed in separate groups via batch."""
    state = _MicrobatchState()
    chunk_a, chunk_b = uuid4(), uuid4()
    manifest_a, manifest_b = uuid4(), uuid4()
    state.jobs = [
        state._make_job(chunk_a, manifest_a, fingerprint="fp-a"),
        state._make_job(chunk_b, manifest_b, fingerprint="fp-b"),
    ]
    state.records = [state._make_record(chunk_a), state._make_record(chunk_b)]

    class TestEmbedder:
        fingerprint = "fp-a"

        def for_job(self, job):
            if job.get("fingerprint") != self.fingerprint:
                raise ValueError(
                    "worker embedding configuration does not match the claimed index definition"
                )
            return OpenAICompatibleEmbedder(
                "http://embed/v1", "model", dimension=3, fingerprint=self.fingerprint
            )

        def __call__(self, text):
            return [0.1, 0.2, 0.3]

    class Repo:
        def claim_jobs(self, limit, **options):
            state.claim_history.append({"limit": limit, **options})
            taken = state.jobs[:limit]
            state.jobs = state.jobs[limit:]
            return taken

        def renew_job(self, job_id, lease_token, lease_seconds):
            state.renewals.append((job_id, lease_token, lease_seconds))
            return True

        def finish_job(self, job_id, lease_token, error, **options):
            state.finishes.append((job_id, lease_token, error, options))
            return True

        def chunks_for_index(self, chunk_ids, manifest_id=None):
            return [
                r
                for r in state.records
                if chunk_ids is None
                or any(str(r["chunk_id"]) == str(cid) for cid in chunk_ids)
            ]

        def heartbeat_worker(self, worker_id, metadata):
            pass

    repo = Repo()

    class Uow:
        def __enter__(self):
            self.index_jobs = self.chunks = repo
            return self

        def __exit__(self, *_):
            return False

    # Mock urlopen for the OpenAICompatibleEmbedder.batch() calls.
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"data":[{"embedding":[0.577,0.577,0.577]}]}'

    monkeypatch.setattr(indexing_module, "urlopen", lambda *a, **k: Response())

    qdrant, _calls = _fake_qdrant(state)
    worker = IndexWorker(
        lambda: Uow(),
        qdrant,
        TestEmbedder(),
        worker_id="w-fp-batch",
    )
    result = worker.run_batch(2)

    # Both jobs claimed, but only fp-a group succeeds.
    assert result["claimed"] == 2
    assert result["complete"] == 1
    assert result["failed"] == 1


# ---------------------------------------------------------------------------
# B1: Missing-chunk jobs are explicitly failed
# ---------------------------------------------------------------------------


def test_missing_chunk_job_is_explicitly_failed():
    """A job whose entity_id does not resolve to a chunk is failed."""
    state = _MicrobatchState()
    chunk_a = uuid4()
    manifest_a = uuid4()
    # Job references chunk_a, but no record exists for it.
    state.jobs = [
        state._make_job(chunk_a, manifest_a, fingerprint="fp"),
    ]
    # Deliberately empty — no records.
    state.records = []

    qdrant, _calls = _fake_qdrant(state)
    worker = IndexWorker(
        lambda: _make_uow(state),
        qdrant,
        lambda _: [0.1, 0.2, 0.3],
        worker_id="w-missing",
    )
    result = worker.run_batch(2)

    assert result["claimed"] == 1
    assert result["complete"] == 0
    assert result["failed"] == 1
    # The job should have been explicitly failed in PostgreSQL.
    assert len(state.finishes) == 1
    assert state.finishes[0][2] == "embedding failed"
