from __future__ import annotations

import json
import math
import os
import signal
import time
from collections import defaultdict
from threading import Event
from time import monotonic
from typing import Any
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

    def run_batch(self, limit: int = 64, entity_ids: list[UUID] | None = None) -> dict:
        if limit <= 0:
            raise ValueError("limit must be positive")
        fingerprint = getattr(self.embedder, "fingerprint", None)
        if entity_ids is not None:
            if not isinstance(fingerprint, str) or not fingerprint.strip():
                raise ValueError(
                    "sealed index-job census requires the active index fingerprint"
                )
            fingerprint = fingerprint.strip()
        result = {
            "worker_id": self.worker_id,
            "claimed": 0,
            "complete": 0,
            "failed": 0,
            "lease_lost": 0,
            "embedding_batches": 0,
            "embedding_texts": 0,
            "embedding_elapsed_seconds": 0.0,
        }
        claim_options = {
            "lease_seconds": self.lease_seconds,
            "worker_id": self.worker_id,
            "max_attempts": self.max_attempts,
            "fingerprint": fingerprint,
        }
        if entity_ids is not None:
            claim_options["entity_ids"] = entity_ids
        if limit <= 1:
            for _ in range(limit):
                with self.uow_factory() as uow:
                    jobs = uow.index_jobs.claim_jobs(1, **claim_options)
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
                except Exception as exc:  # noqa: BLE001
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
            with self.uow_factory() as uow:
                jobs = uow.index_jobs.claim_jobs(limit, **claim_options)
            if not jobs:
                self._heartbeat(result)
                return self._attach_census(
                    result,
                    entity_ids=entity_ids,
                    fingerprint=claim_options["fingerprint"],
                )
            result["claimed"] = len(jobs)
            self._heartbeat({**result, "busy": True})
            try:
                batch_result = self._process_microbatch(jobs)
                for field in (
                    "complete",
                    "failed",
                    "lease_lost",
                    "embedding_batches",
                    "embedding_texts",
                ):
                    result[field] += batch_result[field]
                result["embedding_elapsed_seconds"] += batch_result[
                    "embedding_elapsed_seconds"
                ]
            except LeaseLost:
                result["lease_lost"] += 1
            except Exception as exc:  # noqa: BLE001
                error = f"microbatch: {type(exc).__name__}: {exc}"
                for job in jobs:
                    with self.uow_factory() as uow:
                        owned = uow.index_jobs.finish_job(
                            job["id"],
                            job["lease_token"],
                            error,
                            max_attempts=self.max_attempts,
                        )
                    if owned:
                        result["failed"] += 1
                    else:
                        result["lease_lost"] += 1
            self._heartbeat({**result, "busy": True})
        self._heartbeat(result)
        return self._attach_census(
            result, entity_ids=entity_ids, fingerprint=claim_options["fingerprint"]
        )

    def _attach_census(
        self, result: dict, *, entity_ids: list[UUID] | None, fingerprint: str | None
    ) -> dict:
        if entity_ids is None:
            return result
        if not isinstance(fingerprint, str) or not fingerprint.strip():
            raise ValueError(
                "sealed index-job census requires the active index fingerprint"
            )
        fingerprint = fingerprint.strip()
        with self.uow_factory() as uow:
            repository_census = getattr(uow.index_jobs, "census_index_jobs", None)
            if repository_census is not None:
                census = repository_census(
                    entity_ids, fingerprint, max_attempts=self.max_attempts
                )
            else:
                from ...index_census import census_index_jobs

                census = census_index_jobs(
                    uow.connection,
                    entity_ids,
                    fingerprint,
                    max_attempts=self.max_attempts,
                )
        result["census"] = census
        for key in (
            "expected",
            "claimable",
            "running_live",
            "running_expired",
            "retryable_failed",
            "dead",
            "missing_job",
            "wrong_fingerprint",
            "manifest_inconsistent",
        ):
            result[key] = census[key]
        result["complete_manifests"] = census.get(
            "complete_manifests", census["complete"]
        )
        return result

    def _process_microbatch(self, jobs: list[dict]) -> dict:
        groups: dict[str, list[dict]] = defaultdict(list)
        for job in jobs:
            groups[job.get("fingerprint", "")].append(job)
        result = {
            "complete": 0,
            "failed": 0,
            "lease_lost": 0,
            "embedding_batches": 0,
            "embedding_texts": 0,
            "embedding_elapsed_seconds": 0.0,
        }
        for group in groups.values():
            self._process_microbatch_group(group, result)
        return result

    def _process_microbatch_group(self, group: list[dict], result: dict) -> None:
        for job in group:
            self._renew(job)
        entity_ids = [job["entity_id"] for job in group]
        with self.uow_factory() as uow:
            records = uow.chunks.chunks_for_index(
                entity_ids,
                manifest_id=group[0]["manifest_id"] if len(group) == 1 else None,
            )
        record_map: dict[str, dict] = {str(rec["chunk_id"]): rec for rec in records}
        texts: list[str | None] = []
        for job in group:
            eid = str(job["entity_id"])
            texts.append(
                None if eid not in record_map else record_map[eid].get("text", "")
            )
        valid_indices = [i for i, text in enumerate(texts) if text is not None]
        vectors: list[list[float] | None] = [None] * len(group)
        try:
            embedder = (
                self.embedder.for_job(group[0])
                if hasattr(self.embedder, "for_job")
                else self.embedder
            )
        except ValueError as exc:
            for job in group:
                self._fail_job(job, f"embedder config: {exc}")
            result["failed"] += len(group)
            return
        already_failed: set[int] = set()
        valid_vectors: list[tuple[dict, list[float]]] = []
        if valid_indices:
            valid_texts = [texts[i] for i in valid_indices]
            if hasattr(embedder, "batch"):
                try:
                    embedding_started = time.monotonic()
                    batch_vectors = embedder.batch(valid_texts)
                except ValueError:
                    for idx in valid_indices:
                        try:
                            embedding_started = time.monotonic()
                            vector = embedder(texts[idx])
                            result["embedding_elapsed_seconds"] += (
                                time.monotonic() - embedding_started
                            )
                            result["embedding_batches"] += 1
                            result["embedding_texts"] += 1
                            vectors[idx] = vector
                        except ValueError as inner_exc:
                            self._fail_job(group[idx], f"embed: {inner_exc}")
                            result["failed"] += 1
                            already_failed.add(idx)
                else:
                    result["embedding_elapsed_seconds"] += (
                        time.monotonic() - embedding_started
                    )
                    result["embedding_batches"] += 1
                    result["embedding_texts"] += len(valid_texts)
                    for idx, vector in zip(valid_indices, batch_vectors):
                        vectors[idx] = vector
            else:
                for idx in valid_indices:
                    try:
                        embedding_started = time.monotonic()
                        vector = embedder(texts[idx])
                        result["embedding_elapsed_seconds"] += (
                            time.monotonic() - embedding_started
                        )
                        result["embedding_batches"] += 1
                        result["embedding_texts"] += 1
                        vectors[idx] = vector
                    except ValueError as exc:
                        already_failed.add(idx)
                        self._fail_job(group[idx], f"embed: {exc}")
                        result["failed"] += 1
        for i, job in enumerate(group):
            vector = vectors[i]
            if vector is None:
                continue
            expected_dimension = job.get("dimension")
            if expected_dimension is not None and len(vector) != expected_dimension:
                already_failed.add(i)
                self._fail_job(
                    job,
                    f"embedding dimension {len(vector)} does not match index definition dimension {expected_dimension}",
                )
                result["failed"] += 1
                continue
            valid_vectors.append((job, vector))
        upsert_points: list[dict] = []
        for job, vector in valid_vectors:
            point = self._build_point(
                job, vector, record_map.get(str(job["entity_id"]))
            )
            if point:
                upsert_points.append(point)
        for i, job in enumerate(group):
            if i not in already_failed:
                self._renew(job)
        if upsert_points:
            try:
                collection = _required(group[0], "physical_collection")
                dimension = group[0].get("dimension")
                distance = group[0].get("distance_metric", "Cosine")
                index = self.index.for_collection(collection, dimension, distance)
                index.require_compatible_schema()
                index.upsert(upsert_points)
            except Exception as exc:  # noqa: BLE001
                for job, _ in valid_vectors:
                    self._fail_job(job, f"qdrant upsert: {exc}")
                result["failed"] += len(valid_vectors)
                for i, job in enumerate(group):
                    if vectors[i] is None:
                        self._fail_job(job, "embedding failed")
                        result["failed"] += 1
                return
        for job, _ in valid_vectors:
            self._renew(job)
            with self.uow_factory() as uow:
                owned = uow.index_jobs.finish_job(
                    job["id"], job["lease_token"], None, max_attempts=self.max_attempts
                )
            if not owned:
                result["lease_lost"] += 1
            else:
                result["complete"] += 1
        for i, job in enumerate(group):
            if vectors[i] is None and i not in already_failed:
                self._fail_job(job, "embedding failed")
                result["failed"] += 1

    def _build_point(
        self, job: dict, vector: list[float], record: dict | None
    ) -> dict | None:
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
        with self.uow_factory() as uow:
            uow.index_jobs.finish_job(
                job["id"], job["lease_token"], error, max_attempts=self.max_attempts
            )

    def _process_job(self, job: dict) -> None:
        self._renew(job)
        operation = job.get("operation", "upsert")
        collection = _required(job, "physical_collection")
        dimension = job.get("dimension")
        distance = job.get("distance_metric", "Cosine")
        index = self.index.for_collection(collection, dimension, distance)
        index.require_compatible_schema()
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
                f"expected exactly one chunk for manifest {job.get('manifest_id')}, found {len(records)}"
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
                f"embedding dimension {len(vector)} does not match index definition dimension {expected_dimension}"
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
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        stop = stop_event or Event()
        previous = {}
        if install_signal_handlers:
            for signum in (signal.SIGTERM, signal.SIGINT):
                try:
                    previous[signum] = signal.signal(signum, lambda *_: stop.set())
                except ValueError:
                    break
        totals: dict[str, Any] = {
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
    """Embedding client with single-text and microbatch support."""

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
        self._batch_count = 0
        self._total_texts = 0
        self._total_time = 0.0

    @property
    def throughput(self) -> dict:
        tps = (
            0.0
            if self._total_time <= 0
            else round(self._total_texts / self._total_time, 3)
        )
        return {
            "batch_count": self._batch_count,
            "total_texts": self._total_texts,
            "total_time": round(self._total_time, 3),
            "texts_per_second": tps,
        }

    def reset_throughput(self) -> None:
        self._batch_count = 0
        self._total_texts = 0
        self._total_time = 0.0

    def for_job(self, job: dict) -> OpenAICompatibleEmbedder:
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

    def batch(self, texts: list[str]) -> list[list[float]]:
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
                f"embedding endpoint returned {len(results)} vectors for {len(texts)} texts"
            )
        vectors: list[list[float]] = []
        for item in results:
            vector = [float(v) for v in item["embedding"]]
            if self.dimension is not None and len(vector) != self.dimension:
                raise ValueError(
                    f"embedding dimension {len(vector)} does not match configured {self.dimension}"
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
