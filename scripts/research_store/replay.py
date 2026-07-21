from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any
from uuid import UUID

from .ports import BlobStore, SearchResponseRepository


@dataclass(frozen=True)
class SearchResponseReplay:
    id: UUID
    run_id: UUID
    query_text: str
    backend: str
    status: str
    parser_version: str
    raw_blob_sha256: str
    content_sha256: str
    raw_bytes: bytes
    result_count: int
    idempotency_key: str
    plan_id: UUID | None = None
    plan_query_id: UUID | None = None
    provider_request_id: str | None = None
    http_status: int | None = None
    error_message: str | None = None
    transport_metadata: dict[str, Any] | None = None
    payload_summary: dict[str, Any] | None = None
    parsed_json: dict[str, Any] | list[Any] | None = None

    def verify_integrity(self) -> bool:
        return hashlib.sha256(self.raw_bytes).hexdigest() == self.content_sha256


class SearchResponseReplayReader:
    """Reader for replaying raw search responses from content-addressed blob storage."""

    def __init__(self, repository: SearchResponseRepository, blob_store: BlobStore):
        self.repository = repository
        self.blob_store = blob_store

    def replay_search_response(
        self, response_id: UUID, run_id: UUID | None = None
    ) -> SearchResponseReplay:
        record = self.repository.get_search_response(response_id, run_id=run_id)
        blob_sha = record["raw_blob_sha256"]
        if not self.blob_store.exists(blob_sha):
            raise FileNotFoundError(f"raw blob {blob_sha} not found in blob store")

        with self.blob_store.open(blob_sha) as handle:
            raw_bytes = handle.read()

        actual_sha = hashlib.sha256(raw_bytes).hexdigest()
        if actual_sha != record["content_sha256"]:
            raise ValueError(
                f"content SHA-256 mismatch for response {response_id}: expected {record['content_sha256']}, got {actual_sha}"
            )

        parsed_json = None
        try:
            parsed_json = json.loads(raw_bytes.decode("utf-8"))
        except Exception:
            parsed_json = None

        return SearchResponseReplay(
            id=record["id"],
            run_id=record["run_id"],
            plan_id=record.get("plan_id"),
            plan_query_id=record.get("plan_query_id"),
            query_text=record["query_text"],
            backend=record["backend"],
            provider_request_id=record.get("provider_request_id"),
            status=record["status"],
            http_status=record.get("http_status"),
            parser_version=record["parser_version"],
            raw_blob_sha256=record["raw_blob_sha256"],
            content_sha256=record["content_sha256"],
            raw_bytes=raw_bytes,
            result_count=record["result_count"],
            error_message=record.get("error_message"),
            transport_metadata=record.get("transport_metadata") or {},
            payload_summary=record.get("payload_summary") or {},
            idempotency_key=record["idempotency_key"],
            parsed_json=parsed_json,
        )

    def replay_responses_for_query(
        self, run_id: UUID, plan_query_id: UUID
    ) -> list[SearchResponseReplay]:
        records = self.repository.list_search_responses(
            run_id, plan_query_id=plan_query_id
        )
        return [self.replay_search_response(rec["id"], run_id=run_id) for rec in records]
