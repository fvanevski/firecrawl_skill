from __future__ import annotations

import json
import math
import os
import signal
from threading import Event
from time import monotonic
from urllib.request import Request, urlopen
from uuid import UUID, uuid4


class LeaseLost(RuntimeError):
    """The job is no longer owned by this worker."""


class IndexWorker:
    """Lease-safe transactional-outbox consumer.

    PostgreSQL owns job state. Qdrant upserts are deliberately idempotent, so a
    worker that loses its lease after an upsert may safely leave the job for a
    later owner to replay.
    """

    def __init__(
        self,
        uow_factory,
        index,
        embedder,
        *,
        queue=None,
        worker_id: str | None = None,
        lease_seconds: int = 300,
        max_attempts: int = 5,
    ):
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        self.uow_factory = uow_factory
        self.index = index
        self.embedder = embedder
        self.queue = queue
        self.worker_id = worker_id or f"{os.uname().nodename}:{os.getpid()}:{uuid4()}"
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts

    def run_batch(self, limit: int = 64) -> dict:
        if limit <= 0:
            raise ValueError("limit must be positive")
        result = {
            "worker_id": self.worker_id,
            "claimed": 0,
            "complete": 0,
            "failed": 0,
            "lease_lost": 0,
        }
        for _ in range(limit):
            with self.uow_factory() as uow:
                jobs = uow.index_jobs.claim_jobs(
                    1,
                    lease_seconds=self.lease_seconds,
                    worker_id=self.worker_id,
                    max_attempts=self.max_attempts,
                    fingerprint=getattr(self.embedder, "fingerprint", None),
                )
            if not jobs:
                break
            job = jobs[0]
            result["claimed"] += 1
            self._heartbeat({**result, "busy": True})
            error = None
            try:
                self._process_job(job)
            except LeaseLost:
                result["lease_lost"] += 1
                continue
            except Exception as exc:  # keep the durable worker alive per job
                error = f"{type(exc).__name__}: {exc}"

            with self.uow_factory() as uow:
                owned = uow.index_jobs.finish_job(
                    job["id"],
                    job["lease_token"],
                    error,
                    max_attempts=self.max_attempts,
                )
            if not owned:
                result["lease_lost"] += 1
            elif error is None:
                result["complete"] += 1
            else:
                result["failed"] += 1
            self._heartbeat({**result, "busy": True})
        self._heartbeat(result)
        return result

    def _process_job(self, job: dict) -> None:
        self._renew(job)
        operation = job.get("operation", "upsert")
        collection = _required(job, "physical_collection")
        dimension = job.get("dimension")
        distance = job.get("distance_metric", "Cosine")
        index = self.index.for_collection(collection, dimension, distance)
        index.ensure_schema()

        entity_id = job.get("chunk_id", job.get("entity_id"))
        if entity_id is None:
            raise ValueError("claimed index job has no chunk/entity id")
        if operation == "delete":
            self._renew(job)
            index.delete([entity_id])
            return
        if operation != "upsert":
            raise ValueError(f"unsupported index operation: {operation}")

        with self.uow_factory() as uow:
            records = uow.chunks.chunks_for_index(
                [entity_id], manifest_id=job.get("manifest_id")
            )
        if len(records) != 1:
            raise RuntimeError(
                f"expected exactly one chunk for manifest {job.get('manifest_id')}, "
                f"found {len(records)}"
            )
        row = records[0]
        if str(row["chunk_id"]) != str(entity_id):
            raise RuntimeError("index job resolved to a different chunk")

        self._renew(job)
        embedder = self.embedder.for_job(job) if hasattr(self.embedder, "for_job") else self.embedder
        vector = embedder(row["text"])
        expected_dimension = dimension or index.dimension
        if expected_dimension is not None and len(vector) != expected_dimension:
            raise ValueError(
                f"embedding dimension {len(vector)} does not match index definition "
                f"dimension {expected_dimension}"
            )
        point = {
            "id": str(row["chunk_id"]),
            "vector": {"dense": vector},
            "payload": {
                key: _json_value(value) for key, value in row.items() if key != "text"
            },
        }
        self._renew(job)
        index.upsert([point])

    def _renew(self, job: dict) -> None:
        with self.uow_factory() as uow:
            owned = uow.index_jobs.renew_job(
                job["id"], job["lease_token"], self.lease_seconds
            )
        if not owned:
            raise LeaseLost(str(job["id"]))

    def _heartbeat(self, metadata: dict | None = None) -> None:
        try:
            with self.uow_factory() as uow:
                heartbeat = getattr(uow.index_jobs, "heartbeat_worker", None)
                if heartbeat:
                    heartbeat(self.worker_id, metadata or {})
        except Exception:
            # Observability must not change job correctness or daemon liveness.
            return

    def run_forever(
        self,
        *,
        batch_size: int = 32,
        poll_seconds: float = 5.0,
        stop_event: Event | None = None,
        once: bool = False,
        install_signal_handlers: bool = True,
    ) -> dict:
        """Drain jobs once or until stopped, always returning to PostgreSQL.

        Valkey is only a latency optimization. A finite blocking wait means a
        missing or consumed wakeup can never strand a durable PostgreSQL job.
        """
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        stop = stop_event or Event()
        previous = {}
        if install_signal_handlers:
            for signum in (signal.SIGTERM, signal.SIGINT):
                try:
                    previous[signum] = signal.signal(signum, lambda *_: stop.set())
                except ValueError:  # signal handlers can only be installed in main thread
                    break
        totals = {"batches": 0, "claimed": 0, "complete": 0, "failed": 0, "lease_lost": 0}
        started = monotonic()
        try:
            while not stop.is_set():
                batch = self.run_batch(batch_size)
                totals["batches"] += 1
                for key in ("claimed", "complete", "failed", "lease_lost"):
                    totals[key] += batch[key]
                if once:
                    break
                if batch["claimed"] >= batch_size:
                    continue
                self._heartbeat({**totals, "idle": True})
                if self.queue is not None:
                    self.queue.wait(poll_seconds)
                else:
                    stop.wait(poll_seconds)
            totals["worker_id"] = self.worker_id
            totals["runtime_seconds"] = round(monotonic() - started, 3)
            return totals
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)


def _required(mapping: dict, key: str):
    value = mapping.get(key)
    if value in (None, ""):
        raise ValueError(f"claimed index job has no {key}")
    return value


def _json_value(value):
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    return value


class OpenAICompatibleEmbedder:
    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "",
        dimension: int | None = None,
        fingerprint: str | None = None,
    ):
        self.url, self.model, self.api_key, self.dimension, self.fingerprint = (
            _endpoint(base_url, "/embeddings"),
            model,
            api_key,
            dimension,
            fingerprint,
        )

    def for_job(self, job: dict) -> "OpenAICompatibleEmbedder":
        """Bind a claimed job to its immutable model definition."""
        if self.fingerprint and job.get("fingerprint") != self.fingerprint:
            raise ValueError(
                "worker embedding configuration does not match the claimed index definition"
            )
        return type(self)(
            self.url,
            job.get("model_name") or self.model,
            self.api_key,
            job.get("dimension") or self.dimension,
            self.fingerprint,
        )

    def __call__(self, text: str) -> list[float]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = Request(
            self.url,
            data=json.dumps({"model": self.model, "input": text}).encode(),
            headers=headers,
            method="POST",
        )
        with urlopen(request, timeout=60) as response:
            vector = json.load(response)["data"][0]["embedding"]
        vector = [float(value) for value in vector]
        if self.dimension is not None and len(vector) != self.dimension:
            raise ValueError(
                f"embedding dimension {len(vector)} does not match configured {self.dimension}"
            )
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0:
            raise ValueError("embedding endpoint returned a zero vector")
        return [value / norm for value in vector]


def _endpoint(value: str, suffix: str) -> str:
    value = value.rstrip("/")
    return value if value.endswith(suffix) else value + suffix
