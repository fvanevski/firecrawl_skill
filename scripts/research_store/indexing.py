from __future__ import annotations

import json
from urllib.request import Request, urlopen
from uuid import UUID


class IndexWorker:
    """Transactional-outbox consumer. Corpus commits never depend on Qdrant."""

    def __init__(self, uow_factory, index, embedder, *, max_attempts: int = 5):
        self.uow_factory, self.index, self.embedder, self.max_attempts = (
            uow_factory,
            index,
            embedder,
            max_attempts,
        )

    def run_batch(self, limit: int = 64) -> dict:
        self.index.ensure_schema()
        with self.uow_factory() as uow:
            jobs = uow.index_jobs.claim_jobs(limit)
        complete = failed = 0
        for job in jobs:
            error = None
            try:
                with self.uow_factory() as uow:
                    records = uow.chunks.chunks_for_index([job["entity_id"]])
                points = [
                    {
                        "id": str(row["chunk_id"]),
                        "vector": {"dense": self.embedder(row["text"])},
                        "payload": {
                            key: _json_value(value)
                            for key, value in row.items()
                            if key != "text"
                        },
                    }
                    for row in records
                ]
                self.index.upsert(points)
                complete += 1
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                failed += 1
            with self.uow_factory() as uow:
                uow.index_jobs.finish_job(job["id"], error)
        return {"claimed": len(jobs), "complete": complete, "failed": failed}


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
        self, base_url: str, model: str, api_key: str = "", dimension: int | None = None
    ):
        self.url, self.model, self.api_key, self.dimension = (
            _endpoint(base_url, "/embeddings"),
            model,
            api_key,
            dimension,
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
        return vector


def _endpoint(value: str, suffix: str) -> str:
    value = value.rstrip("/")
    return value if value.endswith(suffix) else value + suffix
