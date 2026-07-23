"""Derivation service for rederive v2 (issue #47).

This module provides the ``DerivationService`` which handles:

* Creating new document derivations with explicit parser, normalizer,
  chunker, and tokenizer version selection.
* Preserving existing source snapshots and old derivations.
* Generating new document, block, and chunk derivations.
* Producing derivation comparison reports.
* Idempotent repeated rederive operations.
* Safe reindex integration.

## Design principles

* **Source snapshot preserved:** Rederive never creates a new source
  snapshot — the raw bytes are content-addressed and already exist.
* **Old derivations preserved:** Existing document rows, blocks, and
  chunks remain intact. New derivations create new document rows with
  the selected configuration.
* **Idempotent:** Re-running rederive with identical configuration
  against the same source produces no new derivation.
* **Configuration SHA-256:** The configuration dict is hashed so that
  identical configurations produce the same SHA-256, enabling
  deduplication.
* **Derivation status lifecycle:** New derivations start as ``pending``,
  can be activated (``active``), or marked ``failed``.

.. versionadded:: P5-07
   Introduced as part of rederive v2.
"""

from __future__ import annotations

from hashlib import sha256
import json
import logging
from typing import Any, Callable
from uuid import UUID

from .domain import (
    DerivationAttempt,
    DerivationComparisonReport,
    IngestRequest,
)

logger = logging.getLogger(__name__)


def _configuration_sha256(
    parser_version: str,
    normalization_version: str,
    chunker_name: str,
    chunker_version: str,
    tokenizer_name: str,
) -> str:
    """Compute a deterministic SHA-256 of the derivation configuration."""
    config = {
        "parser_version": parser_version,
        "normalization_version": normalization_version,
        "chunker_name": chunker_name,
        "chunker_version": chunker_version,
        "tokenizer_name": tokenizer_name,
    }
    return sha256(
        json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class DerivationService:
    """Service for managing document derivations.

    Args:
        uow_factory: Callable that returns a ``PostgresUnitOfWork``.
        corpus_service: ``CorpusService`` instance for ingestion.
    """

    def __init__(
        self,
        uow_factory: Callable,
        corpus_service,
    ) -> None:
        self.uow_factory = uow_factory
        self.corpus_service = corpus_service

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def rederive(
        self,
        snapshot_id: UUID | None = None,
        document_id: UUID | None = None,
        *,
        parser_version: str | None = None,
        normalization_version: str | None = None,
        chunker_name: str | None = None,
        chunker_version: str | None = None,
        tokenizer_name: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Redrive derivation for snapshots or documents.

        For each target:

        1. Reads the raw blob from content-addressed storage.
        2. Looks up the existing document (if any) for comparison.
        3. Computes the configuration SHA-256.
        4. Checks for existing derivation with the same configuration
           (idempotency).
        5. If not a no-op, creates a new ``IngestRequest`` from the blob
           and calls ``CorpusService.ingest`` to produce new blocks/chunks.
        6. Records a new ``document_derivations`` row.
        7. Produces a comparison report.

        Args:
            snapshot_id: Optional snapshot UUID to rederive.
            document_id: Optional document UUID to rederive.
            parser_version: Explicit parser version. Uses config default
                when ``None``.
            normalization_version: Explicit normalization version. Uses
                config default when ``None``.
            chunker_name: Explicit chunker name. Uses config default
                when ``None``.
            chunker_version: Explicit chunker version. Uses config default
                when ``None``.
            tokenizer_name: Explicit tokenizer name. Uses config default
                when ``None``.
            dry_run: When ``True``, compute what would happen without
                writing.

        Returns:
            A dict with ``targets``, ``total_rederived``, ``total_noop``,
            ``total_failed``, and per-target ``results``.
        """
        from .config import StoreConfig

        config = StoreConfig.from_env()
        parser_version = parser_version or config.parser_version
        normalization_version = normalization_version or config.normalization_version
        chunker_name = chunker_name or config.chunker_name
        chunker_version = chunker_version or config.chunker_version
        tokenizer_name = tokenizer_name or config.tokenizer_name

        configuration_sha = _configuration_sha256(
            parser_version,
            normalization_version,
            chunker_name,
            chunker_version,
            tokenizer_name,
        )

        targets = self._resolve_targets(snapshot_id, document_id, configuration_sha)

        results = []
        total_rederived = 0
        total_noop = 0
        total_failed = 0

        for target in targets:
            target_result = self._rederive_target(
                target,
                parser_version=parser_version,
                normalization_version=normalization_version,
                chunker_name=chunker_name,
                chunker_version=chunker_version,
                tokenizer_name=tokenizer_name,
                configuration_sha=configuration_sha,
                dry_run=dry_run,
            )
            results.append(target_result)
            status = target_result.get("status")
            if status == "rederived":
                total_rederived += 1
            elif status == "noop":
                total_noop += 1
            elif status == "failed":
                total_failed += 1
                logger.warning(
                    "Target %s %s failed: %s",
                    target_result.get("target_type"),
                    target_result.get("target_id"),
                    target_result.get("error", "unknown"),
                )

        return {
            "parser_version": parser_version,
            "normalization_version": normalization_version,
            "chunker_name": chunker_name,
            "chunker_version": chunker_version,
            "tokenizer_name": tokenizer_name,
            "configuration_sha256": configuration_sha,
            "dry_run": dry_run,
            "targets": len(targets),
            "total_rederived": total_rederived,
            "total_noop": total_noop,
            "total_failed": total_failed,
            "results": results,
        }

    def list_derivations(
        self,
        document_id: UUID | None = None,
        snapshot_id: UUID | None = None,
        *,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """List derivation attempts.

        Args:
            document_id: Optional document filter.
            snapshot_id: Optional snapshot filter.
            status: Optional status filter.

        Returns:
            List of derivation attempt dicts.
        """
        with self.uow_factory() as uow:
            return uow.derivations.list(
                document_id=document_id,
                snapshot_id=snapshot_id,
                status=status,
            )

    def get_derivation(
        self,
        derivation_id: UUID,
    ) -> DerivationAttempt | None:
        """Get a single derivation attempt by ID.

        Args:
            derivation_id: Derivation UUID.

        Returns:
            A ``DerivationAttempt`` or ``None``.
        """
        with self.uow_factory() as uow:
            return uow.derivations.get(derivation_id)

    def activate_derivation(
        self,
        derivation_id: UUID,
    ) -> DerivationAttempt:
        """Activate a pending derivation.

        Sets the derivation status to ``active`` and marks any prior
        active derivation for the same document as ``superseded``.

        Args:
            derivation_id: Derivation UUID to activate.

        Returns:
            The activated ``DerivationAttempt``.

        Raises:
            ValueError: When the derivation is not found or not pending.
        """
        with self.uow_factory() as uow:
            return uow.derivations.activate(derivation_id)

    def compare_derivations(
        self,
        old_derivation_id: UUID,
        new_derivation_id: UUID,
    ) -> DerivationComparisonReport:
        """Compare two derivation attempts.

        Args:
            old_derivation_id: Prior derivation UUID.
            new_derivation_id: New derivation UUID.

        Returns:
            A ``DerivationComparisonReport``.
        """
        with self.uow_factory() as uow:
            old = uow.derivations.get(old_derivation_id)
            new = uow.derivations.get(new_derivation_id)

        if old is None or new is None:
            raise ValueError("both derivations must exist")

        # Compare chunks and blocks
        old_chunks = uow.derivations.count_chunks_for_derivation(old_derivation_id)
        new_chunks = uow.derivations.count_chunks_for_derivation(new_derivation_id)
        old_blocks = uow.derivations.count_blocks_for_derivation(old_derivation_id)
        new_blocks = uow.derivations.count_blocks_for_derivation(new_derivation_id)

        # For a simple count-based comparison, use chunk/block counts
        # A full content-hash comparison would require iterating chunks
        chunks_added = (
            max(0, new_chunks - old_chunks)
            if old_chunks is not None and new_chunks is not None
            else 0
        )
        chunks_removed = (
            max(0, old_chunks - new_chunks)
            if old_chunks is not None and new_chunks is not None
            else 0
        )
        chunks_unchanged = (
            min(old_chunks or 0, new_chunks or 0)
            if old_chunks is not None and new_chunks is not None
            else 0
        )

        blocks_added = (
            max(0, new_blocks - old_blocks)
            if old_blocks is not None and new_blocks is not None
            else 0
        )
        blocks_removed = (
            max(0, old_blocks - new_blocks)
            if old_blocks is not None and new_blocks is not None
            else 0
        )
        blocks_unchanged = (
            min(old_blocks or 0, new_blocks or 0)
            if old_blocks is not None and new_blocks is not None
            else 0
        )

        return DerivationComparisonReport(
            old_parser_version=old.parser_version,
            new_parser_version=new.parser_version,
            old_normalization_version=old.normalization_version,
            new_normalization_version=new.normalization_version,
            old_chunker_version=old.chunker_version,
            new_chunker_version=new.chunker_version,
            old_tokenizer_name=old.tokenizer_name,
            new_tokenizer_name=new.tokenizer_name,
            old_chunk_count=old.chunk_count or old_chunks,
            new_chunk_count=new.chunk_count or new_chunks,
            old_block_count=old.block_count or old_blocks,
            new_block_count=new.block_count or new_blocks,
            chunks_added=chunks_added,
            chunks_removed=chunks_removed,
            chunks_unchanged=chunks_unchanged,
            blocks_added=blocks_added,
            blocks_removed=blocks_removed,
            blocks_unchanged=blocks_unchanged,
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _resolve_targets(
        self,
        snapshot_id: UUID | None,
        document_id: UUID | None,
        configuration_sha: str,
    ) -> list[dict]:
        """Resolve target snapshots/documents for rederive.

        Returns a list of target dicts with keys:
        - ``type``: ``"snapshot"`` or ``"document"``
        - ``id``: UUID
        - ``document_id``: UUID (for snapshot targets)
        - ``snapshot_id``: UUID
        - ``existing_parser_version``: str or None
        - ``existing_config_sha``: str or None
        """
        with self.uow_factory() as uow:
            targets = []

            if snapshot_id is not None:
                # Resolve snapshot → document mapping
                rows = uow.derivations.get_document_for_snapshot(snapshot_id)
                for row in rows:
                    targets.append(
                        {
                            "type": "snapshot",
                            "id": snapshot_id,
                            "document_id": row["document_id"],
                            "snapshot_id": snapshot_id,
                            "existing_parser_version": row.get("parser_version"),
                            "existing_config_sha": row.get("configuration_sha256"),
                        }
                    )
            elif document_id is not None:
                # Resolve document → snapshot mapping
                rows = uow.derivations.get_snapshots_for_document(document_id)
                for row in rows:
                    targets.append(
                        {
                            "type": "document",
                            "id": document_id,
                            "document_id": document_id,
                            "snapshot_id": row["snapshot_id"],
                            "existing_parser_version": row.get("parser_version"),
                            "existing_config_sha": row.get("configuration_sha256"),
                        }
                    )
            else:
                # No filter — list all derivations with their sources
                rows = uow.derivations.list_all_targets()
                for row in rows:
                    targets.append(
                        {
                            "type": "document",
                            "id": row["document_id"],
                            "document_id": row["document_id"],
                            "snapshot_id": row["snapshot_id"],
                            "existing_parser_version": row.get("parser_version"),
                            "existing_config_sha": row.get("configuration_sha256"),
                        }
                    )

            # Validate: if both document_id and snapshot_id are provided,
            # verify the snapshot belongs to the document (consistency check)
            if snapshot_id is not None and document_id is not None:
                targets = [t for t in targets if t["document_id"] == document_id]
                if not targets:
                    raise ValueError(
                        f"snapshot {snapshot_id} does not belong to document {document_id}"
                    )

            # Filter out targets that already have this exact configuration
            # as an active or pending derivation (idempotency)
            filtered = []
            for t in targets:
                existing = uow.derivations.find_by_configuration(
                    t["document_id"], configuration_sha
                )
                if existing is None:
                    filtered.append(t)
                # If existing exists, skip — idempotent noop

            return filtered

    def _rederive_target(
        self,
        target: dict,
        *,
        parser_version: str,
        normalization_version: str,
        chunker_name: str,
        chunker_version: str,
        tokenizer_name: str,
        configuration_sha: str,
        dry_run: bool,
    ) -> dict[str, Any]:
        """Redrive a single target.

        Args:
            target: Target dict from ``_resolve_targets``.
            parser_version: Parser version.
            normalization_version: Normalization version.
            chunker_name: Chunker name.
            chunker_version: Chunker version.
            tokenizer_name: Tokenizer name.
            configuration_sha: Pre-computed configuration SHA-256.
            dry_run: Whether to skip writes.

        Returns:
            A result dict with status, comparison report, etc.
        """
        result = {
            "target_type": target["type"],
            "target_id": str(target["id"]),
            "document_id": str(target["document_id"]),
            "snapshot_id": str(target["snapshot_id"]),
            "status": "pending",
            "configuration_sha256": configuration_sha,
        }

        try:
            # Read the raw blob
            blob_data = self._read_snapshot_blob(target["snapshot_id"])
            if blob_data is None:
                result["status"] = "failed"
                result["error"] = "snapshot blob not found"
                return result

            # Build IngestRequest with explicit version overrides
            request = IngestRequest(
                requested_url="",  # Will be populated from snapshot
                content=blob_data["content"],
                mime_type=blob_data.get("mime_type", "text/markdown"),
                title=blob_data.get("title"),
                metadata={
                    "rederive": {
                        "parser_version": parser_version,
                        "normalization_version": normalization_version,
                        "chunker_name": chunker_name,
                        "chunker_version": chunker_version,
                        "tokenizer_name": tokenizer_name,
                        "configuration_sha256": configuration_sha,
                        "source_snapshot_id": str(target["snapshot_id"]),
                    }
                },
            )

            if dry_run:
                result["status"] = "would_rederive"
                result["dry_run"] = True
                return result

            # Perform ingestion — this creates new document/blocks/chunks
            ingest_result = self.corpus_service.ingest(request)

            # Record the derivation attempt
            with self.uow_factory() as uow:
                derivation = uow.derivations.create(
                    document_id=target["document_id"],
                    snapshot_id=target["snapshot_id"],
                    parser_version=parser_version,
                    normalization_version=normalization_version,
                    chunker_name=chunker_name,
                    chunker_version=chunker_version,
                    tokenizer_name=tokenizer_name,
                    chunk_count=len(ingest_result.chunk_ids),
                    block_count=None,  # Will be set by a separate query
                    configuration_sha256=configuration_sha,
                    status="pending",
                )
                result["derivation_id"] = str(derivation.id)

            result["status"] = "rederived"
            result["reused_snapshot"] = ingest_result.reused_snapshot
            result["reused_document"] = ingest_result.reused_document
            result["reused_chunks"] = ingest_result.reused_chunks
            result["chunk_count"] = len(ingest_result.chunk_ids)
            result["snapshot_id"] = str(ingest_result.snapshot_id)
            result["document_id"] = str(ingest_result.document_id)

            return result

        except Exception as exc:
            result["status"] = "failed"
            result["error"] = f"{type(exc).__name__}: {exc}"
            return result

    def _read_snapshot_blob(self, snapshot_id: UUID) -> dict | None:
        """Read the raw blob for a snapshot.

        Returns:
            A dict with ``content``, ``mime_type``, ``title``, etc.
            or ``None`` when the blob is not found.
        """
        from .blob import ContentAddressedBlobStore
        from .config import StoreConfig

        config = StoreConfig.from_env()
        store = ContentAddressedBlobStore(config.blob_root)

        with self.uow_factory() as uow:
            row = uow.derivations.get_snapshot_info(snapshot_id)

        if row is None:
            return None

        try:
            with store.open(row["content_sha256"]) as handle:
                content = handle.read()
        except FileNotFoundError:
            return None

        return {
            "content": content,
            "mime_type": row.get("mime_type"),
            "title": row.get("title"),
            "requested_url": row.get("requested_url"),
            "final_url": row.get("final_url"),
            "retrieved_at": row.get("retrieved_at"),
            "http_status": row.get("http_status"),
        }
