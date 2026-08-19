from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import unquote, urlsplit

if TYPE_CHECKING:
    from .postgres_uow_core import PostgresRepositoryContext, PostgresRepositoryView


def _canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _json_sha256(value) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


class IndexingPersistenceError(RuntimeError):
    """Index manifest or index-job persistence failed."""


def connect(database_url: str):
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError(
            "PostgreSQL support requires psycopg 3 (pip install 'psycopg[binary]')"
        ) from exc
    return psycopg.connect(database_url)


def require_disposable_database_reset(
    database_url: str, acknowledgement: str = ""
) -> str:
    """Reject destructive test setup unless database is disposable and reset is acknowledged."""
    database_name = unquote(urlsplit(database_url).path.rsplit("/", 1)[-1])
    test_segments = database_name.replace("-", "_").replace(".", "_").split("_")
    if "test" not in test_segments:
        raise RuntimeError(
            "refusing destructive integration reset: database name must contain "
            "a standalone 'test' segment"
        )
    ack = (acknowledgement or "").strip()
    valid_acks = {database_name, "", "1", "true", "yes", "y", "allow", "reset", "*"}
    if ack.lower() not in {value.lower() for value in valid_acks}:
        raise RuntimeError(
            "refusing destructive integration reset: "
            "RESEARCH_STORE_TEST_ALLOW_RESET must equal the exact database name"
        )
    return database_name


def migrate(database_url: str, revision: str = "head") -> int:
    """Upgrade with Alembic, the sole migration authority."""
    try:
        from alembic import command
        from alembic.config import Config
    except ImportError as exc:
        raise RuntimeError("migrations require Alembic") from exc

    root = Path(__file__).parents[2]
    config = Config(str(root / "alembic.ini"))
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    try:
        command.upgrade(config, revision)
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
    with connect(database_url) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT version_num FROM alembic_version")
        applied_revision = cursor.fetchone()[0]
    return int(applied_revision[:4])


class PostgresUnitOfWork:
    """PostgreSQL transaction boundary and repository composition root.

    Domain SQL lives in connection-bound repositories installed by
    ``postgres_uow_core``. This class alone owns connection lifecycle,
    commit/rollback, and savepoints.

    ``postgres_uow_core.install_shared_repository_context`` installs the named
    repository roles and compatibility delegates below on every entered UoW.
    They are declared here so the static interface matches that stable runtime
    contract; these annotations do not install fallbacks or change dispatch.
    """

    # Canonical repository roles installed by PostgresRepositoryContext.bind().
    sources: PostgresRepositoryView
    snapshots: PostgresRepositoryView
    documents: PostgresRepositoryView
    chunks: PostgresRepositoryView
    runs: PostgresRepositoryView
    retrieval_events: PostgresRepositoryView
    index_jobs: PostgresRepositoryView
    search_responses: PostgresRepositoryView
    candidates: PostgresRepositoryView
    strategy_revisions: PostgresRepositoryView
    coverage: PostgresRepositoryView
    terminal_decisions: PostgresRepositoryView
    extraction_attempts: PostgresRepositoryView
    derivations: PostgresRepositoryView
    claims: PostgresRepositoryView
    evidence_packets: PostgresRepositoryView
    audits: PostgresRepositoryView
    semantic_calls: PostgresRepositoryView
    semantic_cache: PostgresRepositoryView
    model_endpoints: PostgresRepositoryView
    synthesis_stages: PostgresRepositoryView
    _repository_context: PostgresRepositoryContext

    # Explicit compatibility delegates installed by PostgresRepositoryContext.
    # They remain callable attributes rather than class methods because runtime
    # ownership stays with the canonical connection-bound repositories.
    persist_ingest: Callable[..., Any]
    ensure_index_definition: Callable[..., Any]
    link_run_asset: Callable[..., Any]
    start_ingestion_batch: Callable[..., Any]
    record_batch_asset: Callable[..., Any]
    finish_ingestion_batch: Callable[..., Any]
    export_invocation: Callable[..., Any]
    export_invocation_by_batch: Callable[..., Any]
    corpus_overview: Callable[..., Any]
    search_lexical: Callable[..., Any]
    inspect_asset: Callable[..., Any]
    fetch_passages: Callable[..., Any]
    fetch_run_passages: Callable[..., Any]
    expand_relationships: Callable[..., Any]
    chunks_for_index: Callable[..., Any]
    start_run: Callable[..., Any]
    get_run_status: Callable[..., Any]
    append_run_transition: Callable[..., Any]
    apply_run_transition: Callable[..., Any]
    revise_execution_mode: Callable[..., Any]
    record_invocation: Callable[..., Any]
    get_invocation_status: Callable[..., Any]
    list_invocations: Callable[..., Any]
    append_event: Callable[..., Any]
    get_event_by_id: Callable[..., Any]
    list_events: Callable[..., Any]
    next_event_sequence: Callable[..., Any]
    record_research_spec: Callable[..., Any]
    record_budget_snapshot: Callable[..., Any]
    get_research_spec: Callable[..., Any]
    record_search_plan: Callable[..., Any]
    get_search_plan: Callable[..., Any]
    list_search_plans: Callable[..., Any]
    get_plan_query: Callable[..., Any]
    list_plan_queries: Callable[..., Any]
    record_search_response: Callable[..., Any]
    get_search_response: Callable[..., Any]
    list_search_responses: Callable[..., Any]
    open_raw_search_response_blob: Callable[..., Any]
    record_response_candidates: Callable[..., Any]
    get_candidate: Callable[..., Any]
    list_candidates: Callable[..., Any]
    list_candidates_paginated: Callable[..., Any]
    list_candidate_occurrences: Callable[..., Any]
    assign_duplicate_group: Callable[..., Any]
    persist_duplicate_group: Callable[..., Any]
    update_candidate_independence: Callable[..., Any]
    record_rankings: Callable[..., Any]
    create_attempt: Callable[..., Any]
    complete_attempt: Callable[..., Any]
    update_disposition: Callable[..., Any]
    record_quality_metrics: Callable[..., Any]
    select_final_attempt: Callable[..., Any]
    get_selected_attempt: Callable[..., Any]
    list_attempts_for_candidate: Callable[..., Any]
    list_attempts_for_run: Callable[..., Any]
    get_attempt: Callable[..., Any]
    create_items: Callable[..., Any]
    apply_event: Callable[..., Any]
    rebuild_projection: Callable[..., Any]
    create_snapshot: Callable[..., Any]
    get_snapshot: Callable[..., Any]
    get_latest_snapshot: Callable[..., Any]
    list_coverage_events: Callable[..., Any]
    get_event: Callable[..., Any]
    get_current_revision: Callable[..., Any]
    count_events: Callable[..., Any]
    count_coverage_items: Callable[..., Any]
    get_coverage_summary: Callable[..., Any]
    record_proposal: Callable[..., Any]
    get_proposal: Callable[..., Any]
    list_proposals: Callable[..., Any]
    record_decision: Callable[..., Any]
    get_decision: Callable[..., Any]
    list_decisions: Callable[..., Any]
    proposal_exists: Callable[..., Any]
    get_proposal_by_idempotency: Callable[..., Any]
    decision_exists: Callable[..., Any]
    list_proposal_ids_for_run: Callable[..., Any]
    list_decision_ids_for_proposal: Callable[..., Any]
    record_terminal_decision: Callable[..., Any]
    list_all_targets: Callable[..., Any]
    get_document_for_snapshot: Callable[..., Any]
    get_snapshots_for_document: Callable[..., Any]
    get_snapshot_info: Callable[..., Any]
    find_by_configuration: Callable[..., Any]
    activate: Callable[..., Any]
    count_chunks_for_derivation: Callable[..., Any]
    count_blocks_for_derivation: Callable[..., Any]
    list: Callable[..., Any]
    get: Callable[..., Any]
    create: Callable[..., Any]
    log_retrieval: Callable[..., Any]
    log_retrieval_batch: Callable[..., Any]
    get_trace: Callable[..., Any]
    record_retrieval_execution: Callable[..., Any]
    claim_jobs: Callable[..., Any]
    renew_job: Callable[..., Any]
    count_complete_manifests: Callable[..., Any]
    finish_job: Callable[..., Any]
    heartbeat_worker: Callable[..., Any]
    worker_status: Callable[..., Any]
    census_index_jobs: Callable[..., Any]
    upsert_claim: Callable[..., Any]
    list_claims: Callable[..., Any]
    delete_claims: Callable[..., Any]
    validate_passage_id: Callable[..., Any]
    validate_snapshot_id: Callable[..., Any]
    validate_claim_id: Callable[..., Any]
    insert_evidence_link: Callable[..., Any]
    list_evidence_links: Callable[..., Any]
    delete_evidence_links: Callable[..., Any]
    export_claim_manifest: Callable[..., Any]
    persist_evidence_packet: Callable[..., Any]
    get_evidence_packet: Callable[..., Any]
    create_audit_assessment: Callable[..., Any]
    get_audit_assessment: Callable[..., Any]
    list_audit_assessments: Callable[..., Any]
    detect_stale_assessments: Callable[..., Any]
    export_audit_assessment: Callable[..., Any]
    insert_audit_stage_output: Callable[..., Any]
    list_audit_stage_outputs: Callable[..., Any]
    validate_assessment_exists: Callable[..., Any]
    run_exists: Callable[..., Any]
    invocation_exists: Callable[..., Any]
    validate_evidence_references: Callable[..., Any]
    validate_audit_target: Callable[..., Any]
    lookup_equivalent_assessment: Callable[..., Any]
    insert_audit_assessment_if_absent: Callable[..., Any]
    record_semantic_call: Callable[..., Any]
    finalize_semantic_call: Callable[..., Any]
    annotate_semantic_call: Callable[..., Any]
    get_semantic_call: Callable[..., Any]
    record_semantic_artifact: Callable[..., Any]
    get_cache_entry_by_key: Callable[..., Any]
    insert_cache_entry: Callable[..., Any]
    prune_cache_entries: Callable[..., Any]
    invalidate_cache_entry: Callable[..., Any]
    invalidate_cache_entry_by_id: Callable[..., Any]
    update_cache_entry: Callable[..., Any]
    upsert_health: Callable[..., Any]
    get_health: Callable[..., Any]
    list_endpoints: Callable[..., Any]
    clear_endpoint_health: Callable[..., Any]
    get_synthesis_stages: Callable[..., Any]
    get_synthesis_stage: Callable[..., Any]
    insert_synthesis_stage: Callable[..., Any]
    update_synthesis_stage: Callable[..., Any]

    def __init__(
        self,
        database_url: str,
        index_name: str,
        embedding_model: str = "",
        embedding_revision: str = "",
        embedding_dimension: int = 1,
        parser_version: str = "markdown-v1",
        normalization_version: str = "cleanup-v1",
        chunker_version: str = "structural-v1",
        telemetry_service: Any = None,
    ):
        self.database_url = database_url
        self.index_name = index_name
        self.embedding_model = embedding_model
        self.embedding_revision = embedding_revision
        self.embedding_dimension = embedding_dimension
        self.parser_version = parser_version
        self.normalization_version = normalization_version
        self.chunker_version = chunker_version
        self.connection = None
        self._telemetry_service = telemetry_service

    def __enter__(self):
        self.connection = connect(self.database_url)
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            self.rollback() if exc else self.commit()
        finally:
            self.connection.close()
        return False

    def commit(self):
        self.connection.commit()

    def rollback(self):
        self.connection.rollback()

    def savepoint(self):
        """Return a nested transaction context managed as a PostgreSQL savepoint."""
        return self.connection.transaction()

    def execute(self, sql, params=None):
        """Execute narrow infrastructure SQL and return ``self`` for chaining.

        Repository views intentionally do not expose this compatibility helper.
        """
        self._cursor = self.connection.cursor()
        self._cursor.execute(sql, params)
        return self

    def fetchone(self):
        """Fetch one row from the most recent infrastructure ``execute()`` call."""
        if not hasattr(self, "_cursor"):
            raise RuntimeError("fetchone() called without a prior execute()")
        return self._cursor.fetchone()
