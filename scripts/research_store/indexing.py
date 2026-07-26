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
        if limit <= 1:
            # Original single-job path for backward compatibility.
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
                except (
                    Exception  # noqa: BLE001
                ) as exc:  # keep the durable worker alive per job
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
        else:
            # Microbatch path: claim N jobs, group by fingerprint, batch embed.
            with self.uow_factory() as uow:
                jobs = uow.index_jobs.claim_jobs(
                    limit,
                    lease_seconds=self.lease_seconds,
                    worker_id=self.worker_id,
                    max_attempts=self.max_attempts,
                    fingerprint=getattr(self.embedder, "fingerprint", None),
                )
            if not jobs:
                self._heartbeat(result)
                return result
            result["claimed"] = len(jobs)
            self._heartbeat({**result, "busy": True})
            try:
                batch_result = self._process_microbatch(jobs)
                result["complete"] += batch_result["complete"]
                result["failed"] += batch_result["failed"]
                result["lease_lost"] += batch_result["lease_lost"]
            except LeaseLost:
                result["lease_lost"] += 1
            self._heartbeat({**result, "busy": True})
        self._heartbeat(result)
        return result

    def _process_microbatch(self, jobs: list[dict]) -> dict:
        """Process a batch of jobs with shared fingerprint.

        Groups jobs by their output-affecting index parameters, batches
        embedding calls, validates each vector, batches Qdrant upserts, and
        completes or fails each job independently.

        Returns a dict with ``complete``, ``failed``, and ``lease_lost`` counts.
        """
        from collections import defaultdict

        groups: dict[str, list[dict]] = defaultdict(list)
        for job in jobs:
            key = job.get("fingerprint", "")
            groups[key].append(job)

        result = {"complete": 0, "failed": 0, "lease_lost": 0}

        for group in groups.values():
            self._process_microbatch_group(group, result)

        return result

    def _process_microbatch_group(self, group: list[dict], result: dict) -> None:
        """Process one fingerprint group of jobs.

        Raises ``LeaseLost`` when the lease cannot be renewed, allowing the
        caller to skip remaining groups.
        """
        # Renew all leases before processing.
        for job in group:
            self._renew(job)

        # Resolve chunk texts for this group.
        entity_ids = [job["entity_id"] for job in group]
        with self.uow_factory() as uow:
            records = uow.chunks.chunks_for_index(
                entity_ids, manifest_id=group[0]["manifest_id"]
            )

        # Build a map from entity_id to chunk record.
        record_map: dict[str, dict] = {}
        for rec in records:
            record_map[str(rec["chunk_id"])] = rec

        # Validate that every job resolved to a chunk.
        texts: list[str | None] = []
        for job in group:
            eid = str(job["entity_id"])
            if eid not in record_map:
                # Cannot embed without text; mark job failed.
                texts.append(None)  # sentinel
            else:
                texts.append(record_map[eid].get("text", ""))

        # Batch-embed non-null texts; keep track of indices.
        valid_indices = [i for i, t in enumerate(texts) if t is not None]
        vectors: list[list[float] | None] = [None] * len(group)
        try:
            embedder = (
                self.embedder.for_job(group[0])
                if hasattr(self.embedder, "for_job")
                else self.embedder
            )
        except ValueError as exc:
            # Fingerprint mismatch — fail all jobs in this group.
            for job in group:
                self._fail_job(job, f"embedder config: {exc}")
            result["failed"] += len(group)
            return

        # Track indices already failed during embedding so the post-completion
        # loop does not double-count them.
        already_failed: set[int] = set()
        valid_vectors: list[tuple[dict, list[float]]] = []

        if valid_indices:
            valid_texts = [texts[i] for i in valid_indices]
            if hasattr(embedder, "batch"):
                try:
                    batch_vectors = embedder.batch(valid_texts)
                except ValueError:
                    # Batch endpoint failed — fall back to per-text embedding
                    # so that partial failures only affect the offending job.
                    for idx in valid_indices:
                        try:
                            vec = embedder(texts[idx])
                            vectors[idx] = vec
                        except ValueError as inner_exc:
                            job = group[idx]
                            self._fail_job(job, f"embed: {inner_exc}")
                            result["failed"] += 1
                            already_failed.add(idx)
                else:
                    # Batch succeeded — assign vectors to their indices.
                    for idx, vec in zip(valid_indices, batch_vectors):
                        vectors[idx] = vec
            else:
                # Fallback: embed each text individually so that partial
                # failures only affect the offending job.
                for idx in valid_indices:
                    try:
                        vec = embedder(texts[idx])
                        vectors[idx] = vec
                    except ValueError as exc:
                        already_failed.add(idx)
                        job = group[idx]
                        self._fail_job(job, f"embed: {exc}")
                        result["failed"] += 1

        # Validate dimensions BEFORE upsert — reject incompatible vectors
        # so they never reach Qdrant.
        for i, job in enumerate(group):
            if vectors[i] is None:
                continue
            expected_dimension = job.get("dimension")
            if expected_dimension is not None and len(vectors[i]) != expected_dimension:
                already_failed.add(i)
                self._fail_job(
                    job,
                    f"embedding dimension {len(vectors[i])} does not match "
                    f"index definition dimension {expected_dimension}",
                )
                result["failed"] += 1
                continue
            valid_vectors.append((job, vectors[i]))

        # Build upsert points from validated vectors only.
        upsert_points: list[dict] = []
        for job, vector in valid_vectors:
            point = self._build_point(
                job, vector, record_map.get(str(job["entity_id"]))
            )
            if point:
                upsert_points.append(point)

        # Renew leases before upsert.
        for job in group:
            self._renew(job)

        if upsert_points:
            try:
                collection = _required(group[0], "physical_collection")
                dimension = group[0].get("dimension")
                distance = group[0].get("distance_metric", "Cosine")
                index = self.index.for_collection(collection, dimension, distance)
                index.ensure_schema()
                index.upsert(upsert_points)
            except Exception as exc:  # noqa: BLE001
                # Upsert failed — mark all jobs in this group as failed.
                for job, _ in valid_vectors:
                    self._fail_job(job, f"qdrant upsert: {exc}")
                result["failed"] += len(valid_vectors)
                # Also fail any jobs that had embedding errors.
                for i, job in enumerate(group):
                    if vectors[i] is None:
                        self._fail_job(job, "embedding failed")
                        result["failed"] += 1
                return

        # Complete each successfully upserted job.
        for job, _ in valid_vectors:
            self._renew(job)
            with self.uow_factory() as uow:
                owned = uow.index_jobs.finish_job(
                    job["id"],
                    job["lease_token"],
                    None,
                    max_attempts=self.max_attempts,
                )
            if not owned:
                result["lease_lost"] += 1
            else:
                result["complete"] += 1

        # Fail any jobs that had missing chunks or embedding errors.
        # These jobs were skipped during upsert and completion — they must
        # be explicitly marked as failed so they do not remain in claimed
        # state indefinitely.  Skip indices already failed during embedding
        # or dimension validation to avoid double-counting.
        for i, job in enumerate(group):
            if vectors[i] is None and i not in already_failed:
                self._fail_job(job, "embedding failed")
                result["failed"] += 1

    def _build_point(
        self, job: dict, vector: list[float], record: dict | None
    ) -> dict | None:
        """Build a Qdrant point for a single job."""
        if record is None:
            return None
        return {
            "id": str(job["entity_id"]),
            "vector": {"dense": vector},
            "payload": {
                key: _json_value(value)
                for key, value in record.items()
                if key != "text"
            },
        }

    def _fail_job(self, job: dict, error: str) -> None:
        """Mark a job as failed in PostgreSQL."""
        with self.uow_factory() as uow:
            uow.index_jobs.finish_job(
                job["id"],
                job["lease_token"],
                error,
                max_attempts=self.max_attempts,
            )
        # Count doesn't matter for the result — caller aggregates.

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
        embedder = (
            self.embedder.for_job(job)
            if hasattr(self.embedder, "for_job")
            else self.embedder
        )
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
        except Exception:  # noqa: BLE001
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
                except (
                    ValueError
                ):  # signal handlers can only be installed in main thread
                    break
        totals = {
            "batches": 0,
            "claimed": 0,
            "complete": 0,
            "failed": 0,
            "lease_lost": 0,
        }
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
    """Embedding client with single-text and microbatch support.

    Single-text calls use ``__call__``.  Microbatch calls use ``batch`` and
    return a list of vectors, one per input text, validated individually.
    """

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
        # Throughput metrics
        self._batch_count: int = 0
        self._total_texts: int = 0
        self._total_time: float = 0.0

    @property
    def throughput(self) -> dict:
        """Return throughput metrics.

        Returns:
            A dict with keys ``batch_count``, ``total_texts``,
            ``total_time``, and ``texts_per_second``.
        """
        tps = 0.0
        if self._total_time > 0:
            tps = round(self._total_texts / self._total_time, 3)
        return {
            "batch_count": self._batch_count,
            "total_texts": self._total_texts,
            "total_time": round(self._total_time, 3),
            "texts_per_second": tps,
        }

    def reset_throughput(self) -> None:
        """Reset throughput counters."""
        self._batch_count = 0
        self._total_texts = 0
        self._total_time = 0.0

    def for_job(self, job: dict) -> OpenAICompatibleEmbedder:
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
        """Embed a single text, returning a normalised vector."""
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

    def batch(self, texts: list[str]) -> list[list[float]]:
        """Embed multiple texts in a single request.

        Args:
            texts: A non-empty list of input strings.

        Returns:
            A list of normalised vectors, one per input text, validated
            individually.  Each vector is checked for dimension match and
            zero-norm; a failed vector raises ``ValueError`` and does not
            silently succeed.

        Raises:
            ValueError: If ``texts`` is empty, a vector has the wrong
                dimension, or the endpoint returns a zero vector.
        """
        if not texts:
            raise ValueError("batch requires at least one text")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = Request(
            self.url,
            data=json.dumps({"model": self.model, "input": texts}).encode(),
            headers=headers,
            method="POST",
        )
        start = monotonic()
        with urlopen(request, timeout=120) as response:
            payload = json.load(response)
        results = payload.get("data", [])
        if len(results) != len(texts):
            raise ValueError(
                f"embedding endpoint returned {len(results)} vectors "
                f"for {len(texts)} texts"
            )
        vectors: list[list[float]] = []
        for item in results:
            vector = [float(v) for v in item["embedding"]]
            if self.dimension is not None and len(vector) != self.dimension:
                raise ValueError(
                    f"embedding dimension {len(vector)} does not match "
                    f"configured {self.dimension}"
                )
            norm = math.sqrt(sum(v * v for v in vector))
            if norm == 0:
                raise ValueError("embedding endpoint returned a zero vector")
            vectors.append([v / norm for v in vector])
        elapsed = monotonic() - start
        self._batch_count += 1
        self._total_texts += len(texts)
        self._total_time += elapsed
        return vectors


def _endpoint(value: str, suffix: str) -> str:
    value = value.rstrip("/")
    return value if value.endswith(suffix) else value + suffix
