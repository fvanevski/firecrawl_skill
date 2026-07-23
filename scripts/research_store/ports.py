from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any, BinaryIO, Protocol
from uuid import UUID

from .domain import (
    BlobReference,
    ExtractionQualityMetrics,
    IngestRequest,
    IngestResult,
    SearchAdapterResult,
)
from .parsing import ParseResult, SelectionRecord


class SourceRepository(Protocol):
    def upsert_source(self, canonical_url: str, metadata: dict[str, Any]) -> UUID: ...


class SnapshotRepository(Protocol):
    def persist_ingest(
        self,
        request: IngestRequest,
        canonical_url: str,
        blob: BlobReference,
        normalized_text: str,
        blocks: list[Any],
        chunks: list[Any],
        parser_version: str,
        chunker_version: str,
        normalization_version: str,
    ) -> IngestResult: ...


class DocumentRepository(Protocol):
    def inspect_asset(self, candidate_id: UUID) -> dict[str, Any]: ...
    def fetch_passages(
        self,
        candidate_ids: list[UUID],
        max_tokens: int,
        max_passages: int,
        include_neighbors: bool,
    ) -> list[dict[str, Any]]: ...


class ChunkRepository(Protocol):
    def chunks_for_index(
        self, chunk_ids: list[UUID] | None = None
    ) -> list[dict[str, Any]]: ...


class SemanticCallRepository(Protocol):
    def record_semantic_call(
        self,
        run_id: UUID,
        stage: str,
        provider: str,
        model: str,
        prompt_version: str,
        request: dict[str, Any],
        idempotency_key: str,
        **metadata: Any,
    ) -> UUID: ...
    def finalize_semantic_call(
        self,
        run_id: UUID,
        call_id: UUID,
        status: str,
        response_metadata: dict[str, Any],
        error: str | None,
    ) -> UUID: ...
    def annotate_semantic_call(
        self, run_id: UUID, call_id: UUID, metadata: dict[str, Any]
    ) -> UUID: ...
    def get_semantic_call(self, run_id: UUID, call_id: UUID) -> dict[str, Any]: ...
    def record_semantic_artifact(
        self,
        run_id: UUID,
        semantic_call_id: UUID,
        artifact_type: str,
        schema_name: str,
        schema_version: int,
        payload: dict[str, Any],
        idempotency_key: str,
        **metadata: Any,
    ) -> UUID: ...


class SearchResponseRepository(Protocol):
    def record_search_response(
        self,
        run_id: UUID,
        query_text: str,
        backend: str,
        raw_payload: bytes | str,
        idempotency_key: str,
        blob_store: BlobStore,
        *,
        plan_id: UUID | None = None,
        plan_query_id: UUID | None = None,
        provider_request_id: str | None = None,
        parser_version: str = "firecrawl-search-v1",
        http_status: int | None = None,
        error_message: str | None = None,
        requested_at: Any | None = None,
        responded_at: Any | None = None,
        transport_metadata: dict[str, Any] | None = None,
        **metadata: Any,
    ) -> dict[str, Any]: ...
    def get_search_response(
        self, response_id: UUID, run_id: UUID | None = None
    ) -> dict[str, Any]: ...
    def list_search_responses(
        self,
        run_id: UUID,
        *,
        plan_id: UUID | None = None,
        plan_query_id: UUID | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]: ...
    def open_raw_search_response_blob(
        self, response_id: UUID, blob_store: BlobStore, run_id: UUID | None = None
    ) -> BinaryIO: ...


class CandidateRepository(Protocol):
    def record_response_candidates(
        self,
        run_id: UUID,
        search_response_id: UUID,
        blob_store: BlobStore,
        *,
        plan_id: UUID | None = None,
        plan_query_id: UUID | None = None,
    ) -> list[dict[str, Any]]: ...
    def get_candidate(
        self, candidate_id: UUID, run_id: UUID | None = None
    ) -> dict[str, Any]: ...
    def list_candidates(
        self,
        run_id: UUID,
        *,
        domain: str | None = None,
        min_recurrence: int | None = None,
        duplicate_group_id: UUID | None = None,
    ) -> list[dict[str, Any]]: ...
    def list_candidates_paginated(
        self,
        run_id: UUID,
        *,
        plan_id: UUID | None = None,
        plan_query_id: UUID | None = None,
        query_text: str | None = None,
        domain: str | None = None,
        min_recurrence: int | None = None,
        duplicate_group_id: UUID | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]: ...
    def list_candidate_occurrences(
        self, candidate_id: UUID, run_id: UUID | None = None
    ) -> list[dict[str, Any]]: ...
    def assign_duplicate_group(
        self,
        candidate_ids: list[UUID],
        group_id: UUID | None = None,
        run_id: UUID | None = None,
    ) -> UUID: ...


class ResearchRunRepository(
    SemanticCallRepository, SearchResponseRepository, CandidateRepository, Protocol
):
    def start_run(self, original_request: str, metadata: dict[str, Any]) -> UUID: ...
    def get_run_status(
        self, *, run_id: UUID | None = None, external_id: str | None = None
    ) -> dict[str, Any]: ...
    def apply_run_transition(
        self,
        run_id: UUID,
        next_state: str,
        expected_revision: int,
        idempotency_key: str,
        actor_type: str,
        policy_version: str,
        **metadata: Any,
    ) -> dict[str, Any]: ...
    def revise_execution_mode(
        self,
        run_id: UUID,
        next_mode: str,
        expected_revision: int,
        idempotency_key: str,
        actor_type: str,
        policy_version: str,
        **metadata: Any,
    ) -> dict[str, Any]: ...
    def append_run_transition(
        self,
        run_id: UUID,
        lifecycle_revision: int,
        prior_state: str,
        next_state: str,
        idempotency_key: str,
        actor_type: str,
        policy_version: str,
        **metadata: Any,
    ) -> dict[str, Any]: ...
    def record_invocation(
        self, run_id: UUID, operation: str, idempotency_key: str, **metadata: Any
    ) -> UUID: ...
    def _bump_lifecycle_revision(self, run_id: UUID, new_revision: int) -> int: ...
    def append_event(
        self,
        run_id: UUID,
        event_type: str,
        actor_type: str,
        idempotency_key: str,
        **metadata: Any,
    ) -> UUID: ...
    def record_research_spec(
        self,
        run_id: UUID,
        spec_revision: int,
        schema_name: str,
        schema_version: int,
        payload: dict[str, Any],
        idempotency_key: str,
        **metadata: Any,
    ) -> UUID: ...
    def record_budget_snapshot(
        self,
        run_id: UUID,
        research_spec_id: UUID,
        spec_revision: int,
        run_revision: int,
        policy_version: str,
        policy_config_sha256: str,
        snapshot: dict[str, Any],
        idempotency_key: str,
    ) -> UUID: ...
    def record_search_plan(
        self,
        run_id: UUID,
        research_spec_id: UUID,
        revision: int,
        search_plan: dict[str, Any],
        idempotency_key: str,
        **metadata: Any,
    ) -> UUID: ...
    def get_search_plan(
        self, run_id: UUID, plan_id: UUID | None = None, revision: int | None = None
    ) -> dict[str, Any]: ...
    def list_search_plans(self, run_id: UUID) -> list[dict[str, Any]]: ...
    def get_plan_query(
        self, query_id: UUID, run_id: UUID | None = None
    ) -> dict[str, Any]: ...
    def list_plan_queries(self, plan_id: UUID) -> list[dict[str, Any]]: ...
    def record_compatibility_export(
        self,
        run_id: UUID,
        export_type: str,
        export_schema_version: int,
        source_state_sha256: str,
        status: str,
        idempotency_key: str,
        **metadata: Any,
    ) -> UUID: ...
    def record_legacy_adapter_comparison(
        self,
        entry_point: str,
        adapter_mode: str,
        legacy_decision: dict[str, Any],
        service_proposal: dict[str, Any],
        legacy_sha256: str,
        proposal_sha256: str,
        divergent: bool,
        divergence_reasons: list[str],
        idempotency_key: str,
        **metadata: Any,
    ) -> UUID: ...
    def list_legacy_adapter_comparisons(
        self, **filters: Any
    ) -> list[dict[str, Any]]: ...
    def link_run_asset(
        self, external_run_id: str, snapshot_id: UUID, role: str = "acquired"
    ) -> None: ...


class RetrievalEventRepository(Protocol):
    def log_retrieval(self, run_id: UUID, event: dict[str, Any]) -> None: ...


class IndexJobRepository(Protocol):
    def claim_jobs(
        self,
        limit: int,
        lease_seconds: int = 300,
        worker_id: str = "compat",
        max_attempts: int = 5,
        fingerprint: str | None = None,
    ) -> list[dict[str, Any]]: ...
    def renew_job(
        self, job_id: UUID, lease_token: UUID, lease_seconds: int = 300
    ) -> bool: ...
    def finish_job(
        self,
        job_id: UUID,
        lease_token: UUID,
        error: str | None = None,
        max_attempts: int = 5,
    ) -> bool: ...


class StrategyRevisionRepository(Protocol):
    """Persist strategy-revision proposals and deterministic authorization decisions."""

    def record_proposal(
        self,
        run_id: UUID,
        proposal_id: UUID,
        run_revision: int,
        coverage_revision: int,
        decision_type: str,
        target_coverage_item_ids: list[str],
        proposed_queries: list[dict[str, Any]],
        proposed_candidate_ids: list[str],
        proposed_retrieval_queries: list[str],
        expected_contribution: str,
        estimated_cost: dict[str, Any],
        rationale: str,
        confidence: float,
        idempotency_key: str,
        **metadata: Any,
    ) -> UUID: ...
    def get_proposal(
        self, run_id: UUID, proposal_id: UUID
    ) -> dict[str, Any] | None: ...
    def list_proposals(
        self,
        run_id: UUID,
        *,
        run_revision: int | None = None,
        coverage_revision: int | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]: ...
    def record_decision(
        self,
        run_id: UUID,
        decision_id: UUID,
        proposal_id: UUID,
        run_revision: int,
        coverage_revision: int,
        outcome: str,
        rejection_reasons: list[str],
        policy_version: str,
        scope_expansion_type: str | None,
        scope_expansion_rationale: str | None,
        scope_expansion_approved: bool | None,
        authorized_by: str,
        idempotency_key: str,
        **metadata: Any,
    ) -> UUID: ...
    def get_decision(
        self, run_id: UUID, decision_id: UUID
    ) -> dict[str, Any] | None: ...
    def list_decisions(
        self,
        run_id: UUID,
        *,
        proposal_id: UUID | None = None,
        outcome: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]: ...
    def proposal_exists(self, run_id: UUID, proposal_id: UUID) -> bool: ...
    def get_proposal_by_idempotency(
        self, run_id: UUID, idempotency_key: str
    ) -> dict[str, Any] | None: ...
    def decision_exists(self, run_id: UUID, decision_id: UUID) -> bool: ...
    def list_proposal_ids_for_run(self, run_id: UUID) -> list[str]: ...
    def list_decision_ids_for_proposal(
        self, run_id: UUID, proposal_id: UUID
    ) -> list[str]: ...


class BlobStore(Protocol):
    def put(self, stream: BinaryIO, mime_type: str | None = None) -> BlobReference: ...
    def open(self, digest: str) -> BinaryIO: ...
    def exists(self, digest: str) -> bool: ...
    def verify(self, digest: str) -> bool: ...


class ParserRepository(Protocol):
    """Protocol for parser selection and execution."""

    def select(
        self,
        raw: bytes,
        *,
        mime_type: str | None = None,
    ) -> tuple[SelectionRecord, ParseResult]: ...


class RetrievalIndex(Protocol):
    def ensure_schema(self) -> None: ...
    def upsert(self, points: list[dict[str, Any]]) -> None: ...
    def search(
        self, vector: list[float], filters: dict[str, Any], limit: int
    ) -> list[dict[str, Any]]: ...
    def delete(self, ids: list[UUID]) -> None: ...


class QueueBackend(Protocol):
    def notify(self, job_id: UUID, ttl_seconds: int = 3600) -> None: ...


class SearchAdapter(Protocol):
    def search(
        self,
        query_text: str,
        *,
        backend: str = "firecrawl",
        limit: int = 20,
        sources: str = "web",
        tbs: str | None = None,
        **kwargs: Any,
    ) -> SearchAdapterResult: ...


class ExtractionAttemptRepository(Protocol):
    """Persist and query extraction-attempt records."""

    def create_attempt(
        self,
        candidate_id: UUID,
        run_id: UUID,
        invocation_id: UUID | None,
        attempt_number: int,
        method: str,
        method_version: str,
        requested_format: str | None,
        start_time: Any,
        end_time: Any,
        exit_status: str,
        http_status: int | None,
        backend_status: str | None,
        raw_blob: BlobReference | None,
        normalized_blob: BlobReference | None,
        parser_used: str | None,
        quality_metrics: ExtractionQualityMetrics | None,
        failure_class: str,
        retry_parent_id: UUID | None,
        disposition: str,
        error_message: str | None,
        selection_reason: str | None,
    ) -> UUID: ...

    def complete_attempt(
        self,
        attempt_id: UUID,
        exit_status: str,
        raw_blob: BlobReference | None,
        normalized_blob: BlobReference | None,
        parser_used: str | None,
        quality_metrics: ExtractionQualityMetrics | None,
        failure_class: str,
        http_status: int | None,
        backend_status: str | None,
        end_time: Any,
        error_message: str | None,
    ) -> None: ...

    def update_disposition(self, attempt_id: UUID, disposition: str) -> None: ...

    def select_final_attempt(
        self,
        candidate_id: UUID,
        attempt_id: UUID,
        selection_reason: str,
    ) -> None: ...

    def get_attempt(
        self, attempt_id: UUID, run_id: UUID | None = None
    ) -> dict[str, Any]: ...

    def list_attempts_for_candidate(
        self,
        candidate_id: UUID,
        *,
        run_id: UUID | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]: ...

    def list_attempts_for_run(
        self,
        run_id: UUID,
        *,
        candidate_id: UUID | None = None,
        method: str | None = None,
        exit_status: str | None = None,
        disposition: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]: ...

    def get_selected_attempt(self, candidate_id: UUID) -> dict[str, Any] | None: ...

    def record_quality_metrics(
        self,
        attempt_id: UUID,
        quality_metrics: ExtractionQualityMetrics,
    ) -> None: ...


class UnitOfWork(AbstractContextManager, Protocol):
    sources: SourceRepository
    snapshots: SnapshotRepository
    documents: DocumentRepository
    chunks: ChunkRepository
    runs: ResearchRunRepository
    search_responses: SearchResponseRepository
    candidates: CandidateRepository
    retrieval_events: RetrievalEventRepository
    index_jobs: IndexJobRepository
    strategy_revisions: StrategyRevisionRepository
    extraction_attempts: ExtractionAttemptRepository

    def commit(self) -> None: ...
    def rollback(self) -> None: ...
