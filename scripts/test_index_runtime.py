from __future__ import annotations

# ruff: noqa: E402 - load the sibling script package without installing it.

from pathlib import Path
import sys
from threading import Event
from uuid import uuid4

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from research_store.indexing import IndexWorker, OpenAICompatibleEmbedder
import research_store.indexing as indexing_module
from research_store.qdrant import QdrantIndex
from research_store.queue import ValkeyQueue


class FakeRepository:
    def __init__(self, state):
        self.state = state

    def claim_jobs(self, limit, **options):
        self.state.setdefault("claim_history", []).append({"limit": limit, **options})
        self.state["claim_options"] = {"limit": limit, **options}
        jobs, self.state["jobs"] = self.state["jobs"][:limit], self.state["jobs"][limit:]
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
        return FakeIndex(self.calls, collection, dimension or self.dimension, distance or self.distance)

    def ensure_schema(self):
        self.calls.append(("schema", self.collection, self.dimension, self.distance))

    def upsert(self, points):
        self.calls.append(("upsert", self.collection, points))

    def delete(self, ids):
        self.calls.append(("delete", self.collection, ids))


def _state():
    chunk_id, job_id, manifest_id = uuid4(), uuid4(), uuid4()
    return {
        "jobs": [{
            "id": job_id,
            "manifest_id": manifest_id,
            "entity_id": chunk_id,
            "operation": "upsert",
            "lease_token": uuid4(),
            "physical_collection": "research_chunks_abc123",
            "dimension": 3,
            "distance_metric": "Cosine",
        }],
        "records": [{"chunk_id": chunk_id, "text": "exact text", "source_id": uuid4()}],
        "renewals": [],
        "finishes": [],
        "heartbeats": [],
    }


def test_worker_uses_exact_manifest_collection_and_token():
    state, calls = _state(), []
    worker = IndexWorker(lambda: FakeUow(state), FakeIndex(calls), lambda _: [0.1, 0.2, 0.3], worker_id="w1")
    result = worker.run_batch(10)
    job = state["finishes"][0]
    assert result["complete"] == 1 and result["failed"] == 0
    assert state["chunk_lookup"][1] is not None
    assert job[1] == state["renewals"][0][1] and job[2] is None
    assert ("schema", "research_chunks_abc123", 3, "Cosine") in calls
    assert [call for call in calls if call[0] == "upsert"][0][2][0]["id"] == str(state["records"][0]["chunk_id"])


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
    assert [call["limit"] for call in state["claim_history"]] == [1, 1]
    assert all(
        call["fingerprint"] == "configured-fingerprint"
        for call in state["claim_history"]
    )


def test_worker_does_not_finish_after_lease_loss():
    state, calls = _state(), []
    state["owns_lease"] = False
    result = IndexWorker(lambda: FakeUow(state), FakeIndex(calls), lambda _: [0, 0, 0]).run_batch()
    assert result["lease_lost"] == 1
    assert state["finishes"] == []
    assert not any(call[0] == "upsert" for call in calls)


def test_worker_reports_stale_completion_after_idempotent_upsert():
    state, calls = _state(), []
    state["owns_at_finish"] = False
    result = IndexWorker(lambda: FakeUow(state), FakeIndex(calls), lambda _: [0, 0, 0]).run_batch()
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
        return None


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
    qdrant = FakeQdrant([{"result": {"config": {"params": {"vectors": {"dense": {"size": 3, "distance": "Cosine"}}}}}}])
    result = qdrant.inspect_schema()
    assert result["compatible"] is True
    assert qdrant.requests == [("GET", "/collections/physical", None)]


def test_qdrant_alias_switch_is_single_atomic_request():
    qdrant = FakeQdrant([
        {"result": {"aliases": [{"alias_name": "research_chunks_active", "collection_name": "old"}]}},
        {"result": {"status": "ok"}},
    ])
    assert qdrant.switch_alias("research_chunks_active", "new")
    method, path, payload = qdrant.requests[-1]
    assert (method, path) == ("POST", "/collections/aliases")
    assert payload["actions"] == [
        {"delete_alias": {"alias_name": "research_chunks_active"}},
        {"create_alias": {"collection_name": "new", "alias_name": "research_chunks_active"}},
    ]


def test_once_runtime_does_not_wait_for_queue():
    state, calls = _state(), []
    state["jobs"] = []
    queue = type("Queue", (), {"wait": lambda self, _: (_ for _ in ()).throw(AssertionError)})()
    worker = IndexWorker(lambda: FakeUow(state), FakeIndex(calls), lambda _: [], queue=queue)
    result = worker.run_forever(once=True, stop_event=Event(), install_signal_handlers=False)
    assert result["batches"] == 1


def test_embedder_enforces_declared_unit_length_and_job_model(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"data":[{"embedding":[3,4,0]}]}'

    monkeypatch.setattr(indexing_module, "urlopen", lambda *_args, **_kwargs: Response())
    configured = OpenAICompatibleEmbedder("http://embedding/v1", "current", dimension=3)
    job = configured.for_job({"model_name": "immutable-job-model", "dimension": 3})
    assert job.model == "immutable-job-model"
    assert job("text") == [0.6, 0.8, 0.0]
