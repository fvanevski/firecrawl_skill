from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from typing import Any
from uuid import UUID, uuid4

from research_domain.models import MechanicalStatus, RetrievalExecution

from .config import StoreConfig
from .domain import IngestRequest, IngestResult
from .hierarchical_chunker import hierarchical_chunks
from .invocation_events import _sanitize
from .parsing import structural_blocks
from .parsing.interfaces import ParserSelectionError, UnsupportedFormatError
from .retrieval import reciprocal_rank_fusion
from .url import canonicalize_url


@dataclass(frozen=True)
class ParsedContent:
    """Parsed blocks plus the exact parser identity that produced them."""

    blocks: tuple[Any, ...]
    parser_name: str
    parser_version: str


@dataclass(frozen=True)
class PreparedIngest:
    """Named, stable boundary between parsing and PostgreSQL persistence."""

    request: IngestRequest
    canonical_url: str
    blob: Any
    normalized_text: str
    blocks: tuple[Any, ...]
    chunks: tuple[Any, ...]
    parser_name: str
    parser_version: str
    parser_implementation_version: str
    chunker_version: str
    normalization_version: str
    chunker_name: str

    def persist_args(self) -> tuple[Any, ...]:
        return (
            self.request,
            self.canonical_url,
            self.blob,
            self.normalized_text,
            self.blocks,
            self.chunks,
            self.parser_version,
            self.chunker_version,
            self.normalization_version,
            self.chunker_name,
            f"{self.parser_name}@{self.parser_implementation_version}",
        )


class CorpusService:
    def __init__(
        self,
        config: StoreConfig,
        uow_factory: Callable,
        blob_store,
        *,
        index=None,
        embedder=None,
        reranker=None,
        queue=None,
        parser_registry=None,
    ):
        self.config = config
        self.uow_factory = uow_factory
        self.blob_store = blob_store
        self.index = index
        self.embedder = embedder
        self.reranker = reranker
        self.queue = queue
        self.parser_registry = parser_registry

    def ingest(
        self,
        request: IngestRequest,
        *,
        run_id: UUID | None = None,
        external_run_id: str | None = None,
    ) -> IngestResult:
        """Ingest content and optionally associate it with a research run.

        Args:
            request: The ingestion request containing content and metadata.
            run_id: Optional research run UUID to associate with the
                ingested content.  When provided a ``research_run_assets``
                row is created linking the source to the run.
            external_run_id: Optional external run ID (e.g. ``fr_<hex>``)
                to use when linking.  Takes precedence over *run_id* for
                the asset linkage.

        Returns:
            The ``IngestResult`` containing source, snapshot, document,
            and chunk identities.
        """
        prepared = self._prepare_ingest(request)
        with self.uow_factory() as uow:
            result = uow.snapshots.persist_ingest(*prepared.persist_args())

        # Associate with a research run when requested.
        if run_id is not None or external_run_id is not None:
            self._link_to_run(
                request,
                result,
                run_id,
                external_run_id=external_run_id,
            )

        self._notify(result.chunk_ids)
        return result

    def _notify(self, identifiers) -> None:
        if self.queue:
            for identifier in identifiers:
                self.queue.notify(identifier)
                break

    def _link_to_run(
        self,
        request: IngestRequest,
        result: IngestResult,
        run_id: UUID | None,
        *,
        external_run_id: str | None = None,
    ) -> None:
        """Create a research_run_assets row linking the ingested content to a run.

        The asset record links the snapshot (content) to the research run
        so that downstream queries can find all content associated with a
        specific run.

        Args:
            request: The original ingestion request.
            result: The result containing source/snapshot/document IDs.
            run_id: The internal research run UUID.
            external_run_id: Optional external run ID (e.g. ``fr_<hex>``)
                to use for the linkage.  Takes precedence over *run_id*.
        """
        # Determine the external run ID to use for linkage.
        link_external_id = external_run_id
        if link_external_id is None and run_id is not None:
            # Resolve internal UUID to external ID via the run service.
            try:
                with self.uow_factory() as uow:
                    status = uow.runs.get_run_status(run_id=run_id)
                    link_external_id = status.get("external_id")
            except KeyError:
                pass

        if link_external_id is None:
            return  # No external run ID available — skip linkage.

        try:
            self.uow_factory().runs.link_run_asset(
                external_run_id=link_external_id,
                snapshot_id=result.snapshot_id,
                role="acquired",
                metadata=request.metadata,
            )
        except KeyError:
            # Run not found or not in a running state — skip silently.
            pass

    def prepare_ingest(self, request: IngestRequest) -> PreparedIngest:
        """Prepare an ingestion using a typed, format-aware parser contract."""
        return self._prepare_ingest(request)

    def _prepare_ingest(self, request: IngestRequest) -> PreparedIngest:
        canonical = canonicalize_url(request.final_url or request.requested_url)
        blob = self.blob_store.put(BytesIO(request.content), request.mime_type)
        normalized = (
            request.normalized_content
            if request.normalized_content is not None
            else request.content
        )
        raw = normalized
        text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n")

        parsed = self._parse_content_with_identity(raw, request.mime_type)
        blocks = list(parsed.blocks)
        if not blocks:
            raise ValueError("retrieved content produced no structural blocks")
        chunks = hierarchical_chunks(
            blocks,
            max_tokens=self.config.chunker_max_tokens,
            tokenizer_name=self.config.tokenizer_name,
            chunker_version=self.config.chunker_version,
            chunker_name=self.config.chunker_name,
        )
        from .domain import Chunk

        legacy_chunks: list[Chunk] = []
        for hc in chunks:
            legacy_chunks.append(
                Chunk(
                    ordinal=hc.ordinal,
                    text=hc.text,
                    content_sha256=hc.content_sha256,
                    first_block_ordinal=hc.first_block_ordinal,
                    last_block_ordinal=hc.last_block_ordinal,
                    token_count=hc.token_count,
                    heading_path=hc.heading_path,
                    tokenizer_name=hc.tokenizer_name,
                    parent_block_ordinal=hc.parent_block_ordinal,
                )
            )
        return PreparedIngest(
            request=request,
            canonical_url=canonical,
            blob=blob,
            normalized_text=text,
            blocks=tuple(blocks),
            chunks=tuple(legacy_chunks),
            parser_name=parsed.parser_name,
            parser_version=self.config.parser_version,
            parser_implementation_version=parsed.parser_version,
            chunker_version=self.config.chunker_version,
            normalization_version=self.config.normalization_version,
            chunker_name=self.config.chunker_name,
        )

    def _parse_content(self, raw: bytes, mime_type: str | None) -> list:
        """Compatibility wrapper returning only parsed blocks."""
        return list(self._parse_content_with_identity(raw, mime_type).blocks)

    def _parse_content_with_identity(
        self, raw: bytes, mime_type: str | None
    ) -> ParsedContent:
        """Parse bytes and retain the exact parser type and version."""
        if self.parser_registry is not None:
            try:
                record = self.parser_registry.select(mime_type, raw=raw)
                from importlib import import_module

                module_name, class_name = record.selected_parser_type.rsplit(".", 1)
                mod = import_module(module_name)
                parser = getattr(mod, class_name)()
                parse_result = parser.parse(raw, mime_type=mime_type)
                if not parse_result.success:
                    raise UnsupportedFormatError(
                        mime_type=mime_type,
                        suggestion=parse_result.error or "Parser reported an error",
                    )
                return ParsedContent(
                    blocks=tuple(
                        block.to_legacy_block() for block in parse_result.blocks
                    ),
                    parser_name=record.selected_parser_type,
                    parser_version=parse_result.parser_version,
                )
            except (UnsupportedFormatError, ParserSelectionError):
                raise
            except (
                ValueError,
                KeyError,
                AttributeError,
                ImportError,
                json.JSONDecodeError,
            ) as exc:
                if self._is_html_content(mime_type, raw):
                    logging.getLogger(__name__).debug(
                        "Primary HTML parser failed (%s), trying normalized HTML fallback: %s",
                        type(exc).__name__,
                        exc,
                    )
                    normalized_result = self._try_normalized_html_with_identity(
                        raw, mime_type
                    )
                    if normalized_result is not None:
                        return normalized_result
                logging.getLogger(__name__).debug(
                    "Typed parser failed (%s), falling back to legacy: %s",
                    type(exc).__name__,
                    exc,
                )
            except Exception as exc:
                if self._is_html_content(mime_type, raw):
                    logging.getLogger(__name__).exception(
                        "Unexpected parser error, trying normalized HTML fallback: %s",
                        exc,  # noqa: TRY401
                    )
                    normalized_result = self._try_normalized_html_with_identity(
                        raw, mime_type
                    )
                    if normalized_result is not None:
                        return normalized_result
                logging.getLogger(__name__).exception(
                    "Unexpected parser error, falling back to legacy: %s",
                    exc,  # noqa: TRY401
                )

        if self._is_html_content(mime_type, raw):
            raise ValueError(
                "HTML parsing failed for both primary and fallback parsers"
            )

        text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n")
        return ParsedContent(
            blocks=tuple(structural_blocks(text)),
            parser_name=f"{structural_blocks.__module__}.{structural_blocks.__name__}",
            parser_version=self.config.parser_version,
        )

    @staticmethod
    def _is_html_content(mime_type: str | None, raw: bytes) -> bool:
        """Return ``True`` when *raw* appears to be HTML content."""
        if mime_type is not None:
            lower = mime_type.lower().split(";")[0].strip()
            if lower in ("text/html", "application/xhtml+xml"):
                return True
        # Content sniff: check for HTML markers in the first 512 bytes
        if raw:
            snippet = raw[:512].decode("utf-8", errors="replace").lower()
            html_markers = (
                "<!doctype",
                "<html",
                "<head",
                "<body",
                "<main",
                "<article",
            )
            return any(snippet.startswith(m) for m in html_markers)
        return False

    def _try_normalized_html(self, raw: bytes, mime_type: str | None) -> list | None:
        """Compatibility wrapper for callers that only need blocks."""
        parsed = self._try_normalized_html_with_identity(raw, mime_type)
        return list(parsed.blocks) if parsed is not None else None

    def _try_normalized_html_with_identity(
        self, raw: bytes, mime_type: str | None
    ) -> ParsedContent | None:
        """Try the normalized HTML parser while retaining its identity."""
        try:
            from .html_parser import HtmlNormalizedParser

            parser = HtmlNormalizedParser()
            parse_result = parser.parse(raw, mime_type=mime_type)
            if parse_result.success and parse_result.blocks:
                parser_name = f"{type(parser).__module__}.{type(parser).__name__}"
                return ParsedContent(
                    blocks=tuple(
                        block.to_legacy_block() for block in parse_result.blocks
                    ),
                    parser_name=parser_name,
                    parser_version=parse_result.parser_version,
                )
        except Exception:
            logging.getLogger(__name__).exception(
                "Normalized HTML fallback also failed"
            )
        return None

    def ingest_batch(
        self,
        invocation_id: str,
        operation: str,
        requests: list[IngestRequest | dict],
        *,
        research_run_external_id: str | None = None,
        metadata: dict | None = None,
    ) -> dict:
        """Persist a reconstructable invocation using one outer transaction.

        Asset failures roll back to savepoints while their failure records and
        every successful asset commit atomically with the batch manifest.
        """
        failures = 0
        with self.uow_factory() as uow:
            batch_id = uow.start_ingestion_batch(
                invocation_id, operation, research_run_external_id, metadata
            )
            seen_ordinals = set()
            for fallback_ordinal, item in enumerate(requests):
                ordinal = fallback_ordinal
                if isinstance(item, dict):
                    result_index = (
                        item.get("metadata", {})
                        .get("firecrawl", {})
                        .get("result_index")
                    )
                    if isinstance(result_index, int) and result_index >= 0:
                        ordinal = result_index
                if ordinal in seen_ordinals:
                    raise ValueError(f"duplicate ingestion result ordinal: {ordinal}")
                seen_ordinals.add(ordinal)
                request = (
                    item if isinstance(item, IngestRequest) else item.get("request")
                )
                requested_url = (
                    request.requested_url
                    if request is not None
                    else item.get("requested_url") or item.get("url") or "unknown:"
                )
                try:
                    if request is None:
                        raise RuntimeError(item.get("error") or "acquisition failed")
                    prepared = self._prepare_ingest(request)
                    with uow.savepoint():
                        result = uow.persist_ingest(*prepared.persist_args())
                        uow.record_batch_asset(
                            batch_id,
                            ordinal,
                            requested_url,
                            "complete",
                            result,
                            metadata=item.get("metadata")
                            if isinstance(item, dict)
                            else None,
                            extraction_attempt_id=(
                                request.extraction_attempt_id if request else None
                            ),
                        )
                        if research_run_external_id:
                            try:
                                uow.link_run_asset(
                                    research_run_external_id,
                                    result.snapshot_id,
                                    "acquired",
                                )
                            except KeyError:
                                # Run not found or not running — log a warning
                                # so provenance gaps are visible.
                                logging.getLogger(__name__).warning(
                                    "link_run_asset failed for batch %s, "
                                    "ordinal %s: run %s not found or not running",
                                    batch_id,
                                    ordinal,
                                    research_run_external_id,
                                )
                except Exception as exc:  # noqa: BLE001
                    failures += 1
                    uow.record_batch_asset(
                        batch_id,
                        ordinal,
                        requested_url,
                        "failed",
                        error=f"{type(exc).__name__}: {exc}",
                        metadata=item.get("metadata")
                        if isinstance(item, dict)
                        else None,
                        extraction_attempt_id=(
                            request.extraction_attempt_id if request else None
                        ),
                    )
            manifest = uow.export_invocation(invocation_id)
        # The caller is responsible for finalizing the batch after all
        # constituent outcomes are persisted.  Direct callers that do not have
        # separate constituent tracking finalize immediately; orchestrators
        # complete their extraction attempts first and then call
        # :meth:`finalize_ingestion_batch`.
        manifest["failure_count"] = failures
        return manifest

    def finalize_ingestion_batch(
        self, batch_id: str, status: str, error: str | None = None
    ) -> dict:
        """Finalize an ingestion batch after all constituent outcomes are known.

        Must be called after every constituent has a persisted terminal
        outcome so that batch timing and outcome summaries are derived from
        authoritative evidence rather than wall-clock guesses.
        """
        with self.uow_factory() as uow:
            uow.finish_ingestion_batch(batch_id, status, error=error)
            manifest = uow.export_invocation_by_batch(batch_id)
        self._notify(
            chunk_id
            for asset in manifest.get("assets", [])
            if asset["status"] == "complete"
            for chunk_id in asset["chunk_ids"]
        )
        # Normalize the internal batch identity representation while preserving
        # the manifest shape; all ingestion ordering changes remain bounded-stage
        # scoped.
        if manifest.get("batch_id") is not None:
            manifest["batch_id"] = UUID(str(manifest["batch_id"]))
        manifest["status"] = status
        manifest["failure_count"] = sum(
            1 for a in manifest.get("assets", []) if a.get("status") == "failed"
        )
        return manifest

    def bounded_ingest_batch(
        self,
        invocation_id: str,
        operation: str,
        requests: list,
        *,
        research_run_external_id: str | None = None,
        metadata: dict | None = None,
    ) -> dict:
        """Persist bounded extraction success and run linkage atomically.

        Unlike :meth:`ingest_batch`, this method completes each extraction
        attempt within the same UoW as the batch asset recording and run-asset
        linkage, ensuring atomicity of success terminalization, batch membership,
        and run-asset retention. Callers that subsequently invoke
        :meth:`~ExtractionService.complete_attempt` for the same attempts will
        hit the idempotency guard and return the existing record when the
        evidence matches.
        """
        from .domain import utcnow

        failures = 0
        with self.uow_factory() as uow:
            batch_id = uow.start_ingestion_batch(
                invocation_id, operation, research_run_external_id, metadata
            )
            seen_ordinals: set[int] = set()
            for fallback_ordinal, item in enumerate(requests):
                ordinal = fallback_ordinal
                item_mapping = item if isinstance(item, dict) else None
                if item_mapping is not None:
                    result_index = (
                        item_mapping.get("metadata", {})
                        .get("firecrawl", {})
                        .get("result_index")
                    )
                    if isinstance(result_index, int) and result_index >= 0:
                        ordinal = result_index
                if ordinal in seen_ordinals:
                    raise ValueError(f"duplicate ingestion result ordinal: {ordinal}")
                seen_ordinals.add(ordinal)

                request = (
                    item if isinstance(item, IngestRequest) else item.get("request")
                )
                requested_url = (
                    request.requested_url
                    if request is not None
                    else item.get("requested_url") or item.get("url") or "unknown:"
                )
                item_metadata = item.get("metadata") if isinstance(item, dict) else None
                explicit_attempt_id = (
                    item.get("extraction_attempt_id")
                    if isinstance(item, dict)
                    else None
                )
                attempt_id = (
                    request.extraction_attempt_id
                    if request is not None and request.extraction_attempt_id is not None
                    else explicit_attempt_id
                )
                constituent_started_at = utcnow()
                try:
                    if request is None:
                        raise RuntimeError(item.get("error") or "acquisition failed")
                    prepared = self._prepare_ingest(request)
                    with uow.savepoint():
                        result = uow.persist_ingest(*prepared.persist_args())
                        constituent_completed_at = utcnow()

                        # For the bounded path, extraction success, exact batch
                        # membership, and run-asset retention become authoritative
                        # in one PostgreSQL transaction. If any later write in
                        # this savepoint fails, the success completion rolls back
                        # too.
                        if attempt_id is not None:
                            raw_blob = self.blob_store.put(
                                BytesIO(request.content), None
                            )
                            normalized_blob = self.blob_store.put(
                                BytesIO(request.normalized_content or request.content),
                                None,
                            )
                            status_code = None
                            if isinstance(item_metadata, dict):
                                firecrawl = item_metadata.get("firecrawl")
                                if isinstance(firecrawl, dict):
                                    status_code = firecrawl.get("status_code")
                            if status_code is None:
                                status_code = request.http_status
                            uow.extraction_attempts.complete_attempt(
                                attempt_id=attempt_id,
                                exit_status="succeeded",
                                raw_blob=raw_blob,
                                normalized_blob=normalized_blob,
                                parser_used=self.config.parser_version,
                                quality_metrics=None,
                                failure_class="none",
                                http_status=status_code,
                                backend_status="complete",
                                end_time=constituent_completed_at,
                                error_message=None,
                            )

                        uow.record_batch_asset(
                            batch_id,
                            ordinal,
                            requested_url,
                            "complete",
                            result,
                            metadata=item_metadata,
                            extraction_attempt_id=attempt_id,
                            constituent_started_at=constituent_started_at,
                            constituent_completed_at=constituent_completed_at,
                        )
                        if research_run_external_id:
                            uow.link_run_asset(
                                research_run_external_id,
                                result.snapshot_id,
                                "acquired",
                            )
                except Exception as exc:  # noqa: BLE001
                    failures += 1
                    constituent_completed_at = utcnow()
                    uow.record_batch_asset(
                        batch_id,
                        ordinal,
                        requested_url,
                        "failed",
                        error=f"{type(exc).__name__}: {exc}",
                        metadata=item_metadata,
                        extraction_attempt_id=attempt_id,
                        constituent_started_at=constituent_started_at,
                        constituent_completed_at=constituent_completed_at,
                    )
            manifest = uow.export_invocation(invocation_id)
        manifest["failure_count"] = failures
        return manifest

    def persist_manifest_batch(
        self, metadata: dict, assets: list, research_run_external_id: str | None = None
    ) -> dict:
        """Wrapper-oriented adapter around :meth:`ingest_batch`.

        Each item may be an IngestRequest or a mapping containing ``request``.
        """
        return self.ingest_batch(
            metadata["invocation_id"],
            metadata["operation"],
            assets,
            research_run_external_id=research_run_external_id,
            metadata=metadata,
        )

    def corpus_overview(self) -> dict:
        with self.uow_factory() as uow:
            return uow.documents.corpus_overview()

    def search_assets(
        self,
        query: str,
        *,
        filters: dict | None = None,
        candidate_limit: int = 20,
        run_id: UUID | None = None,
        requested_mode: str = "hybrid",
    ) -> tuple[RetrievalExecution, list[dict]]:
        if not query.strip():
            raise ValueError("query is required")
        if not 1 <= candidate_limit <= 200:
            raise ValueError("candidate_limit must be 1..200")
        filters = filters or {}
        timing = {}
        t0 = time.time()

        with self.uow_factory() as uow:
            if requested_mode != "semantic":
                lexical = uow.documents.search_lexical(
                    query, candidate_limit * 2, filters
                )
                for item in lexical:
                    item["candidate_id"] = str(item["candidate_id"])
                    item["retriever"] = "postgres_fts"
                timing["lexical"] = time.time() - t0
            else:
                lexical = []
                timing["lexical"] = 0.0

            semantic = []
            component_health = {
                "lexical": "healthy",
                "embedding": "healthy",
                "qdrant": "healthy",
                "reranker": "healthy",
                "fusion": "healthy",
            }
            errors = []
            warnings = []
            skipped_stages = []
            executed_mode = requested_mode
            index_fingerprint = None

            if requested_mode == "lexical":
                skipped_stages.extend(["embedding", "qdrant", "reranker"])
            else:
                if self.index and self.embedder:
                    t1 = time.time()
                    try:
                        active = self.index.list_aliases().get(self.config.qdrant_alias)
                        index_fingerprint = active
                        if active == self.config.physical_collection:
                            points = self.index.search(
                                self.embedder(query),
                                _qdrant_filter(filters, self.config),
                                candidate_limit * 2,
                            )
                            semantic = [_semantic_candidate(point) for point in points]
                        else:
                            skipped_stages.extend(["embedding", "qdrant"])
                            executed_mode = "lexical"
                            warnings.append(
                                f"qdrant alias points to {active!r}, "
                                f"expected {self.config.physical_collection!r}"
                            )
                    except Exception as e:  # noqa: BLE001
                        component_health["qdrant"] = "failed"
                        component_health["embedding"] = "failed"
                        errors.append(f"qdrant/embedding error: {e!s}")
                        executed_mode = "lexical"
                        semantic = []
                        skipped_stages.extend(["embedding", "qdrant"])
                    timing["semantic"] = time.time() - t1
                else:
                    skipped_stages.extend(["embedding", "qdrant"])
                    executed_mode = "lexical"

            t2 = time.time()
            fused_candidates = reciprocal_rank_fusion([lexical, semantic])
            candidates = fused_candidates[: self.config.reranker_candidate_limit]
            timing["fusion"] = time.time() - t2

            t3 = time.time()
            passages = uow.documents.fetch_passages(
                [UUID(str(item["candidate_id"])) for item in candidates],
                50000,
                len(candidates),
                False,
            )
            timing["fetch_passages"] = time.time() - t3

            excerpts = {str(item["chunk_id"]): item["text"][:400] for item in passages}
            for item in candidates:
                item["excerpt"] = item.get("excerpt") or excerpts.get(
                    str(item["candidate_id"]), ""
                )

            reranked_candidates = None
            if requested_mode != "lexical" and self.reranker:
                t4 = time.time()
                try:
                    candidates = self.reranker(query, candidates)
                    reranked_candidates = candidates
                except Exception as e:  # noqa: BLE001
                    component_health["reranker"] = "failed"
                    errors.append(f"reranker error: {e!s}")
                timing["reranker"] = time.time() - t4
            elif requested_mode == "lexical":
                pass
            else:
                skipped_stages.append("reranker")

            final_candidates = candidates[:candidate_limit]
            candidates = final_candidates

            mechanical_status = MechanicalStatus.SUCCEEDED
            if requested_mode != executed_mode:
                if requested_mode == "semantic":
                    mechanical_status = MechanicalStatus.FAILED
                else:
                    mechanical_status = MechanicalStatus.DEGRADED
            elif errors:
                mechanical_status = MechanicalStatus.DEGRADED

            stage_counts = {
                "lexical": len(lexical),
                "semantic": len(semantic),
                "fused": len(fused_candidates),
            }

            execution = RetrievalExecution(
                execution_id=uuid4(),
                run_id=run_id or UUID(int=0),
                requested_mode=requested_mode,
                executed_mode=executed_mode,
                mechanical_status=mechanical_status,
                component_health=component_health,
                errors=tuple(errors),
                warnings=tuple(warnings),
                stage_counts=stage_counts,
                index_fingerprint=index_fingerprint,
                filters=filters,
                skipped_stages=tuple(skipped_stages),
                timing=timing,
                config_identity=self.config.embedding_fingerprint[:12],
            )

            if run_id:
                uow.record_retrieval_execution(run_id, execution)
                events_to_log = []

                def _add_stage_events(
                    stage_name, source_list, limit=None, final_stage=False
                ):
                    for rank, item in enumerate(source_list, 1):
                        selected = final_stage and limit is not None and rank <= limit
                        rejection_reason = None
                        if not selected and limit is not None and rank > limit:
                            rejection_reason = (
                                "below_candidate_limit"
                                if final_stage
                                else "below_reranker_limit"
                            )

                        raw_score = item.get("lexical_score")
                        if raw_score is None:
                            raw_score = item.get("semantic_score")

                        events_to_log.append(
                            {
                                "stage": stage_name,
                                "query": query,
                                "filters": filters,
                                "retriever": item.get("retriever", "hybrid_rrf"),
                                "candidate_type": "chunk",
                                "candidate_id": item["candidate_id"],
                                "raw_score": raw_score,
                                "normalized_score": item.get("fused_score"),
                                "reranker_score": item.get("reranker_score"),
                                "rank": rank,
                                "selected": selected,
                                "rejection_reason": rejection_reason,
                            }
                        )

                if lexical:
                    _add_stage_events("lexical", lexical)
                if semantic:
                    _add_stage_events("semantic", semantic)

                # Fused is final if reranker is not run or fails
                fused_is_final = reranked_candidates is None
                _add_stage_events(
                    "fused",
                    fused_candidates,
                    limit=candidate_limit
                    if fused_is_final
                    else self.config.reranker_candidate_limit,
                    final_stage=fused_is_final,
                )

                if reranked_candidates is not None:
                    _add_stage_events(
                        "reranked",
                        reranked_candidates,
                        limit=candidate_limit,
                        final_stage=True,
                    )

                uow.retrieval_events.log_retrieval_batch(
                    execution.execution_id, run_id, events_to_log
                )

            return execution, candidates

    def get_retrieval_trace(self, execution_id: UUID) -> list[dict[str, Any]]:
        with self.uow_factory() as uow:
            return uow.retrieval_events.get_trace(execution_id)

    def inspect_asset(self, candidate_id: UUID) -> dict:
        with self.uow_factory() as uow:
            return uow.documents.inspect_asset(candidate_id)

    def fetch_passages(
        self,
        candidate_ids: list[UUID],
        *,
        max_tokens: int = 2000,
        max_passages: int = 8,
        include_neighboring_blocks: bool = False,
    ) -> list[dict]:
        if max_tokens > 16000 or max_passages > 50:
            raise ValueError("passage request exceeds hard safety limits")
        with self.uow_factory() as uow:
            return uow.documents.fetch_passages(
                candidate_ids, max_tokens, max_passages, include_neighboring_blocks
            )

    def select_run_passages(
        self,
        run_id: UUID,
        chunk_ids: list[UUID],
        *,
        max_tokens: int = 3000,
        max_passages: int = 20,
    ) -> tuple[RetrievalExecution, list[dict[str, Any]]]:
        """Select and record passages from the exact run-scoped asset set."""
        if not chunk_ids:
            raise ValueError("chunk_ids must not be empty")
        if not 1 <= max_tokens <= 16000 or not 1 <= max_passages <= 50:
            raise ValueError("run-scoped passage request exceeds hard safety limits")

        started = time.time()
        with self.uow_factory() as uow:
            passages = uow.documents.fetch_run_passages(
                run_id, chunk_ids, max_tokens, max_passages
            )
            execution = RetrievalExecution(
                execution_id=uuid4(),
                run_id=run_id,
                requested_mode="run_scoped_extraction",
                executed_mode="run_scoped_extraction",
                mechanical_status=(
                    MechanicalStatus.SUCCEEDED if passages else MechanicalStatus.FAILED
                ),
                component_health={"postgres_run_scope": "healthy"},
                errors=() if passages else ("no run-scoped passages found",),
                warnings=(),
                stage_counts={"selected": len(passages)},
                index_fingerprint=None,
                filters={"run_id": str(run_id)},
                skipped_stages=("embedding", "qdrant", "reranker"),
                timing={"selection": time.time() - started},
                config_identity="run-scoped-extraction-v1",
            )
            uow.record_retrieval_execution(run_id, execution)
            uow.retrieval_events.log_retrieval_batch(
                execution.execution_id,
                run_id,
                [
                    {
                        "stage": "selected",
                        "query": "run-scoped extracted assets",
                        "filters": {"run_id": str(run_id)},
                        "retriever": "postgres_run_scope",
                        "candidate_type": "chunk",
                        "candidate_id": str(passage["chunk_id"]),
                        "raw_score": None,
                        "normalized_score": None,
                        "reranker_score": None,
                        "rank": rank,
                        "selected": True,
                        "rejection_reason": None,
                    }
                    for rank, passage in enumerate(passages, 1)
                ],
            )
        return execution, passages

    def build_evidence_packet(
        self, candidate_ids: list[UUID], *, max_tokens: int = 3000
    ) -> dict:
        passages = self.fetch_passages(candidate_ids, max_tokens=max_tokens)
        return {
            "packet_version": "research-store-v1",
            "passages": passages,
            "selection_rationale": "explicit candidate selection",
            "corroborating_groups": [],
            "contradicting_groups": [],
            "omitted_near_duplicates": [],
        }

    def expand_relationships(
        self,
        candidate_ids: list[UUID],
        *,
        max_hops: int = 1,
        max_results: int = 50,
        max_tokens: int = 2000,
    ) -> list[dict]:
        if (
            not 1 <= max_hops <= 3
            or not 1 <= max_results <= 200
            or not 1 <= max_tokens <= 8000
        ):
            raise ValueError("relationship expansion exceeds hard bounds")
        with self.uow_factory() as uow:
            relations = uow.documents.expand_relationships(
                candidate_ids, max_hops, max_results
            )
        result, used = [], 0
        for relation in relations:
            cost = max(1, len(str(relation)) // 4)
            if used + cost > max_tokens:
                break
            result.append(relation)
            used += cost
        return result


def _semantic_candidate(point: dict) -> dict:
    payload = point.get("payload") or {}
    return {
        "candidate_id": payload.get("chunk_id", point.get("id")),
        "title": payload.get("title"),
        "domain": payload.get("domain"),
        "date": payload.get("published_at") or payload.get("retrieved_at"),
        "heading_path": payload.get("heading_path") or [],
        "semantic_score": point.get("score"),
        "snapshot_id": payload.get("snapshot_id"),
        "source_id": payload.get("source_id"),
        "url": payload.get("url"),
        "retriever": "qdrant_dense",
    }


def _qdrant_filter(filters: dict, config: StoreConfig) -> dict:
    must = [
        {"key": "parser_version", "match": {"value": config.parser_version}},
        {
            "key": "normalization_version",
            "match": {"value": config.normalization_version},
        },
        {"key": "chunker_version", "match": {"value": config.chunker_version}},
    ]
    if filters.get("domain"):
        must.append({"key": "domain", "match": {"value": filters["domain"]}})
    if filters.get("source_type"):
        must.append({"key": "source_type", "match": {"value": filters["source_type"]}})
    date_range = {}
    if filters.get("date_from"):
        date_range["gte"] = filters["date_from"]
    if filters.get("date_to"):
        date_range["lte"] = filters["date_to"]
    if date_range:
        must.append({"key": "retrieved_at", "range": date_range})
    return {"must": must} if must else {}


def json_default(value):
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def dumps(value) -> str:
    return json.dumps(value, indent=2, default=json_default)


class ClaimManifestService:
    """Authoritative service for research claims and evidence links.

    Persists claims and claim-to-passage evidence links in PostgreSQL.
    Validates all references before accepting. Rejects URL-only source
    resolution — callers must provide stable passage and snapshot IDs.
    """

    VALID_RELATIONSHIPS = frozenset({"supports", "contradicts", "qualifies", "context"})
    VALID_SEMANTIC_STATUSES = frozenset(
        {
            "supported",
            "contradicted",
            "qualified",
            "unsupported",
            "uncertain",
            "unassessed",
        }
    )

    def __init__(self, uow_factory: Callable):
        self.uow_factory = uow_factory

    def create_claim(
        self,
        run_id: UUID,
        claim_id: UUID,
        statement: str,
        *,
        semantic_status: str = "unassessed",
        uncertainty: str | None = None,
        evidence_packet_revision: int = 1,
    ) -> UUID:
        """Insert or update a claim. Returns the row ``id``.

        Idempotent on ``(run_id, claim_id)``.
        """
        if not statement.strip():
            raise ValueError("claim statement must be non-empty")
        if semantic_status not in self.VALID_SEMANTIC_STATUSES:
            raise ValueError(f"invalid semantic_status: {semantic_status}")
        with self.uow_factory() as uow:
            row_id = uow.upsert_claim(
                run_id,
                claim_id,
                statement,
                semantic_status=semantic_status,
                uncertainty=uncertainty,
                evidence_packet_revision=evidence_packet_revision,
            )
        return row_id

    def create_evidence_link(
        self,
        run_id: UUID,
        claim_id: UUID,
        passage_id: UUID,
        snapshot_id: UUID,
        *,
        source_url: str = "",
        relationship: str = "supports",
        confidence: float = 1.0,
    ) -> UUID:
        """Insert a claim-evidence link. Returns the row ``id``.

        Validates that ``passage_id`` exists in ``chunks`` and
        ``snapshot_id`` exists in ``asset_snapshots`` before inserting.
        Rejects URL-only source references — the caller must provide
        stable ``passage_id`` and ``snapshot_id``.
        """
        if relationship not in self.VALID_RELATIONSHIPS:
            raise ValueError(f"invalid relationship: {relationship}")
        if not (0.0 <= confidence <= 1.0):
            raise ValueError(f"confidence must be in [0, 1], got {confidence}")
        with self.uow_factory() as uow:
            row_id = uow.insert_evidence_link(
                run_id,
                claim_id,
                passage_id,
                snapshot_id,
                source_url=source_url,
                relationship=relationship,
                confidence=confidence,
            )
        return row_id

    def list_claims(self, run_id: UUID) -> list[dict[str, Any]]:
        """Return all claims for a run."""
        with self.uow_factory() as uow:
            return uow.list_claims(run_id)

    def list_evidence_links(self, run_id: UUID) -> list[dict[str, Any]]:
        """Return all evidence links for a run."""
        with self.uow_factory() as uow:
            return uow.list_evidence_links(run_id)

    def export_manifest(self, run_id: UUID) -> dict[str, Any]:
        """Export all claims and links for a run as a JSON-compatible dict."""
        with self.uow_factory() as uow:
            return uow.export_claim_manifest(run_id)

    def import_manifest(
        self,
        run_id: UUID,
        manifest: dict[str, Any],
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Import claims and evidence links from a manifest dict.

        Dry-run-first: validates all references before committing.
        Idempotent — existing claims are upserted, links are appended.
        """
        claims = manifest.get("claims", [])
        links = manifest.get("links", [])

        # Dry-run phase: validate all references in a single UoW to avoid
        # opening O(n) connections (one per passage/snapshot check).
        unknown_passages = []
        unknown_snapshots = []
        malformed_claim_ids = []

        with self.uow_factory() as uow:
            for claim in claims:
                cid = claim.get("claim_id")
                if cid:
                    try:
                        UUID(str(cid))
                    except ValueError:
                        malformed_claim_ids.append(str(cid))

            for link in links:
                pid = link.get("passage_id")
                sid = link.get("snapshot_id")
                if pid:
                    try:
                        uid = UUID(str(pid))
                        if not uow.validate_passage_id(uid):
                            unknown_passages.append(str(pid))
                    except ValueError:
                        unknown_passages.append(str(pid))
                if sid:
                    try:
                        uid = UUID(str(sid))
                        if not uow.validate_snapshot_id(uid):
                            unknown_snapshots.append(str(sid))
                    except ValueError:
                        unknown_snapshots.append(str(sid))

        dry_run_result = {
            "dry_run": True,
            "run_id": str(run_id),
            "claims_count": len(claims),
            "links_count": len(links),
            "malformed_claim_ids": malformed_claim_ids,
            "unknown_passage_ids": unknown_passages,
            "unknown_snapshot_ids": unknown_snapshots,
            "valid": not malformed_claim_ids
            and not unknown_passages
            and not unknown_snapshots,
        }

        if dry_run or (malformed_claim_ids or unknown_passages or unknown_snapshots):
            return dry_run_result

        # Apply phase: commit claims and links
        failed_claims = []
        failed_links = []
        inserted_claims = 0
        with self.uow_factory() as uow:
            for claim in claims:
                try:
                    uow.upsert_claim(
                        run_id,
                        UUID(str(claim["claim_id"])),
                        claim["statement"],
                        semantic_status=claim.get("semantic_status", "unassessed"),
                        uncertainty=claim.get("uncertainty"),
                        evidence_packet_revision=claim.get(
                            "evidence_packet_revision", 1
                        ),
                    )
                    inserted_claims += 1
                except Exception as exc:  # noqa: BLE001
                    failed_claims.append(
                        {
                            "claim_id": str(claim.get("claim_id", "unknown")),
                            "error": str(exc),
                        }
                    )

            inserted_links = 0
            for link in links:
                try:
                    uow.insert_evidence_link(
                        run_id,
                        UUID(str(link["claim_id"])),
                        UUID(str(link["passage_id"])),
                        UUID(str(link["snapshot_id"])),
                        source_url=link.get("source_url", ""),
                        relationship=link.get("relationship", "supports"),
                        confidence=link.get("confidence", 1.0),
                    )
                    inserted_links += 1
                except Exception as exc:  # noqa: BLE001
                    exc_str = str(exc).lower()
                    if (
                        "unique constraint" in exc_str
                        or "uk_claim_evidence_links" in exc_str
                        or "duplicate key" in exc_str
                    ):
                        inserted_links += 1
                    else:
                        failed_links.append(
                            {
                                "claim_id": str(link.get("claim_id", "unknown")),
                                "passage_id": str(link.get("passage_id", "unknown")),
                                "error": str(exc),
                            }
                        )

        has_failures = bool(failed_claims) or bool(failed_links)
        return {
            "dry_run": False,
            "run_id": str(run_id),
            "claims_count": len(claims),
            "links_count": len(links),
            "inserted_claims": inserted_claims,
            "inserted_links": inserted_links,
            "malformed_claim_ids": malformed_claim_ids,
            "unknown_passage_ids": unknown_passages,
            "unknown_snapshot_ids": unknown_snapshots,
            "failed_claims": failed_claims,
            "failed_links": failed_links,
            "valid": not has_failures
            and not malformed_claim_ids
            and not unknown_passages
            and not unknown_snapshots,
        }

    def _passage_id_valid(self, passage_id: UUID) -> bool:
        """Check if passage_id exists in chunks."""
        with self.uow_factory() as uow:
            return uow.validate_passage_id(passage_id)

    def _snapshot_id_valid(self, snapshot_id: UUID) -> bool:
        """Check if snapshot_id exists in asset_snapshots."""
        with self.uow_factory() as uow:
            return uow.validate_snapshot_id(snapshot_id)


# ---------------------------------------------------------------------------
# Audit service (issue #33)
# ---------------------------------------------------------------------------


AUDIT_IDENTITY_VERSION = "audit-identity-v1"
AUDIT_MODEL_IMPLEMENTATION_VERSION = "audit-evaluator-v1"


def resolve_model_fingerprint(
    *,
    model_fingerprint: str | None,
    provider: str | None,
    model: str | None,
    evaluator_version: str,
    prompt_template_version: str,
) -> str:
    """Return a stable, non-empty evaluator/model fingerprint.

    Callers may supply a provider-issued fingerprint. Otherwise both provider
    and a fixed model identifier are required, and the service derives a
    deterministic fingerprint that also retains evaluator schema, prompt
    schema, and implementation versions.
    """
    if model_fingerprint is not None:
        fingerprint = model_fingerprint.strip()
        if not fingerprint:
            raise ValueError("model_fingerprint must be non-empty")
        return fingerprint
    if not provider or not provider.strip() or not model or not model.strip():
        raise ValueError(
            "model_fingerprint is required unless provider and model are both supplied"
        )
    identity = {
        "implementation_version": AUDIT_MODEL_IMPLEMENTATION_VERSION,
        "provider": provider.strip(),
        "model": model.strip(),
        "evaluator_version": evaluator_version,
        "prompt_template_version": prompt_template_version,
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return sha256(canonical.encode("utf-8")).hexdigest()


def compute_audit_identity_hash(
    *,
    target_hash: str,
    evaluator_version: str,
    prompt_template_version: str,
    policy_version: str,
    stage_set: list[str],
    model_fingerprint: str,
) -> str:
    """Compute the canonical SHA-256 identity for reusable audit output."""
    required = {
        "target_hash": target_hash,
        "evaluator_version": evaluator_version,
        "prompt_template_version": prompt_template_version,
        "policy_version": policy_version,
        "model_fingerprint": model_fingerprint,
    }
    empty = [name for name, value in required.items() if not value or not value.strip()]
    if empty:
        raise ValueError("audit identity fields must be non-empty: " + ", ".join(empty))
    normalized_stages = sorted(set(stage_set))
    if not normalized_stages or any(
        not stage or not stage.strip() for stage in normalized_stages
    ):
        raise ValueError("stage_set must contain non-empty stages")
    identity = {
        "identity_version": AUDIT_IDENTITY_VERSION,
        "evaluator_version": evaluator_version,
        "model_fingerprint": model_fingerprint,
        "policy_version": policy_version,
        "prompt_template_version": prompt_template_version,
        "stage_set": normalized_stages,
        "target_hash": target_hash,
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return sha256(canonical.encode("utf-8")).hexdigest()


def _extract_evidence_references(obj: Any) -> list[str]:
    """Recursively extract evidence reference IDs from a stage output dictionary/list structure."""
    refs: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            k_lower = str(k).lower()
            if k_lower in (
                "evidence_refs",
                "evidence_references",
                "claim_id",
                "claim_ids",
                "passage_id",
                "passage_ids",
                "snapshot_id",
                "snapshot_ids",
            ):
                if isinstance(v, (list, tuple, set)):
                    refs.extend([str(item) for item in v if item])
                elif v:
                    refs.append(str(v))
            else:
                refs.extend(_extract_evidence_references(v))
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            refs.extend(_extract_evidence_references(item))
    return refs


class AuditService:
    """Authoritative service for staged semantic audit persistence.

    Persists audit assessments and their individual stage outputs in
    PostgreSQL. Stage failures do not erase successful stages. Target
    hash changes make prior assessments stale but they remain as
    historical records.
    """

    def __init__(self, uow_factory: Callable):
        self.uow_factory = uow_factory

    def _identity(
        self,
        *,
        target_hash: str,
        evaluator_version: str,
        prompt_template_version: str,
        policy_version: str,
        stage_set: list[str],
        provider: str | None,
        model: str | None,
        model_fingerprint: str | None,
    ) -> tuple[str, str]:
        fingerprint = resolve_model_fingerprint(
            model_fingerprint=model_fingerprint,
            provider=provider,
            model=model,
            evaluator_version=evaluator_version,
            prompt_template_version=prompt_template_version,
        )
        identity_hash = compute_audit_identity_hash(
            target_hash=target_hash,
            evaluator_version=evaluator_version,
            prompt_template_version=prompt_template_version,
            policy_version=policy_version,
            stage_set=stage_set,
            model_fingerprint=fingerprint,
        )
        return fingerprint, identity_hash

    @staticmethod
    def _validate_target(uow, run_id: UUID, target_type: str, target_id: UUID) -> None:
        if target_type not in {"run", "invocation"}:
            raise ValueError(f"invalid audit target_type: {target_type}")
        if not uow.validate_audit_target(run_id, target_type, target_id):
            raise ValueError(
                f"audit target not found or not owned by run: "
                f"{run_id}/{target_type}/{target_id}"
            )

    def create_assessment(
        self,
        run_id: UUID,
        target_type: str,
        target_id: UUID,
        target_hash: str,
        evaluator_version: str,
        prompt_template_version: str,
        policy_version: str,
        stage_set: list[str],
        status: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        prompt_hash: str | None = None,
        model_fingerprint: str | None = None,
        elapsed_ms: int = 0,
        audit_packet_manifest: dict[str, Any] | None = None,
    ) -> UUID:
        """Append an assessment with an internally computed identity."""
        fingerprint, identity_hash = self._identity(
            target_hash=target_hash,
            evaluator_version=evaluator_version,
            prompt_template_version=prompt_template_version,
            policy_version=policy_version,
            stage_set=stage_set,
            provider=provider,
            model=model,
            model_fingerprint=model_fingerprint,
        )
        sanitized_manifest = (
            _sanitize(audit_packet_manifest) if audit_packet_manifest else None
        )
        with self.uow_factory() as uow:
            self._validate_target(uow, run_id, target_type, target_id)
            assessment_id = uow.insert_audit_assessment_if_absent(
                run_id=run_id,
                target_type=target_type,
                target_id=target_id,
                target_hash=target_hash,
                evaluator_version=evaluator_version,
                prompt_template_version=prompt_template_version,
                policy_version=policy_version,
                stage_set=stage_set,
                status=status,
                audit_identity_hash=identity_hash,
                provider=provider,
                model=model,
                prompt_hash=prompt_hash,
                model_fingerprint=fingerprint,
                elapsed_ms=elapsed_ms,
                audit_packet_manifest=sanitized_manifest,
            )
            if assessment_id is not None:
                return assessment_id
            existing = uow.lookup_equivalent_assessment(
                run_id, target_type, target_id, identity_hash
            )
            if existing is None:
                raise RuntimeError(
                    "completed audit conflict did not resolve to an assessment"
                )
            return UUID(existing["id"])

    def add_stage_output(
        self,
        assessment_id: UUID,
        stage: str,
        sequence_number: int,
        status: str,
        *,
        output: dict[str, Any] | None = None,
        error: str | None = None,
        error_details: dict[str, Any] | None = None,
        call_count: int = 0,
        used_fallback: bool = False,
    ) -> UUID:
        """Add a stage output to an assessment. Returns the stage ``id``.

        Stage failures are recorded individually; successful stages remain
        intact. Evidence references in ``output`` are validated against the database.
        """
        sanitized_output = _sanitize(output) if output else None
        sanitized_error_details = _sanitize(error_details) if error_details else None

        with self.uow_factory() as uow:
            if not uow.validate_assessment_exists(assessment_id):
                raise ValueError(f"assessment not found: {assessment_id}")

            if sanitized_output:
                extracted_refs = _extract_evidence_references(sanitized_output)
                if extracted_refs:
                    invalid_refs = uow.validate_evidence_references(extracted_refs)
                    if invalid_refs:
                        raise ValueError(
                            f"invalid evidence references in stage output: {sorted(set(invalid_refs))}"
                        )

            stage_id = uow.insert_audit_stage_output(
                assessment_id=assessment_id,
                stage=stage,
                sequence_number=sequence_number,
                status=status,
                output=sanitized_output,
                error=error,
                error_details=sanitized_error_details,
                call_count=call_count,
                used_fallback=used_fallback,
            )
        return stage_id

    def get_assessment(self, assessment_id: UUID) -> dict[str, Any] | None:
        """Fetch a single audit assessment by ID."""
        with self.uow_factory() as uow:
            return uow.get_audit_assessment(assessment_id)

    def list_assessments(
        self,
        run_id: UUID | None = None,
        target_id: UUID | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List audit assessments with optional filters."""
        with self.uow_factory() as uow:
            return uow.list_audit_assessments(
                run_id=run_id,
                target_id=target_id,
                status=status,
                limit=limit,
                offset=offset,
            )

    def get_stage_outputs(
        self,
        assessment_id: UUID,
        stage: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """List stage outputs for an assessment."""
        with self.uow_factory() as uow:
            return uow.list_audit_stage_outputs(
                assessment_id=assessment_id,
                stage=stage,
                status=status,
                limit=limit,
                offset=offset,
            )

    def detect_stale_assessments(
        self,
        run_id: UUID,
        target_type: str,
        target_id: UUID,
        current_hash: str,
    ) -> list[dict[str, Any]]:
        """Return assessments whose target_hash differs from current_hash.

        Stale assessments are retained as historical records.

        Validates that the target entity exists before querying.
        """
        if target_type == "run":
            with self.uow_factory() as uow:
                if not uow.run_exists(run_id):
                    raise ValueError(f"run not found: {run_id}")
        elif target_type == "invocation":
            with self.uow_factory() as uow:
                if not uow.invocation_exists(target_id):
                    raise ValueError(f"invocation not found: {target_id}")
        with self.uow_factory() as uow:
            return uow.detect_stale_assessments(
                run_id=run_id,
                target_type=target_type,
                target_id=target_id,
                current_hash=current_hash,
            )

    def find_equivalent_assessment(
        self,
        run_id: UUID,
        target_type: str,
        target_id: UUID,
        target_hash: str,
        evaluator_version: str,
        prompt_template_version: str,
        policy_version: str,
        stage_set: list[str],
        *,
        provider: str | None = None,
        model: str | None = None,
        model_fingerprint: str | None = None,
    ) -> dict[str, Any] | None:
        """Return the completed equivalent assessment for this exact target."""
        _fingerprint, identity_hash = self._identity(
            target_hash=target_hash,
            evaluator_version=evaluator_version,
            prompt_template_version=prompt_template_version,
            policy_version=policy_version,
            stage_set=stage_set,
            provider=provider,
            model=model,
            model_fingerprint=model_fingerprint,
        )
        with self.uow_factory() as uow:
            self._validate_target(uow, run_id, target_type, target_id)
            return uow.lookup_equivalent_assessment(
                run_id, target_type, target_id, identity_hash
            )

    def schedule_assessment(
        self,
        run_id: UUID,
        target_type: str,
        target_id: UUID,
        target_hash: str,
        evaluator_version: str,
        prompt_template_version: str,
        policy_version: str,
        stage_set: list[str],
        status: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        prompt_hash: str | None = None,
        model_fingerprint: str | None = None,
        elapsed_ms: int = 0,
        audit_packet_manifest: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Append an audit attempt or reuse one completed equivalent assessment.

        Only completed assessments are reusable. Partial and failed rows remain
        immutable historical attempts and do not prevent a completed retry.
        """
        fingerprint, identity_hash = self._identity(
            target_hash=target_hash,
            evaluator_version=evaluator_version,
            prompt_template_version=prompt_template_version,
            policy_version=policy_version,
            stage_set=stage_set,
            provider=provider,
            model=model,
            model_fingerprint=model_fingerprint,
        )
        sanitized_manifest = (
            _sanitize(audit_packet_manifest) if audit_packet_manifest else None
        )
        with self.uow_factory() as uow:
            self._validate_target(uow, run_id, target_type, target_id)
            existing = uow.lookup_equivalent_assessment(
                run_id, target_type, target_id, identity_hash
            )
            if existing is not None:
                assessment_id = UUID(existing["id"])
                action = "reuse"
            else:
                assessment_id = uow.insert_audit_assessment_if_absent(
                    run_id=run_id,
                    target_type=target_type,
                    target_id=target_id,
                    target_hash=target_hash,
                    evaluator_version=evaluator_version,
                    prompt_template_version=prompt_template_version,
                    policy_version=policy_version,
                    stage_set=stage_set,
                    status=status,
                    audit_identity_hash=identity_hash,
                    provider=provider,
                    model=model,
                    prompt_hash=prompt_hash,
                    model_fingerprint=fingerprint,
                    elapsed_ms=elapsed_ms,
                    audit_packet_manifest=sanitized_manifest,
                )
                if assessment_id is None:
                    existing = uow.lookup_equivalent_assessment(
                        run_id, target_type, target_id, identity_hash
                    )
                    if existing is None:
                        raise RuntimeError(
                            "completed audit conflict did not resolve to an assessment"
                        )
                    assessment_id = UUID(existing["id"])
                    action = "reuse"
                else:
                    action = "create"

        export = self.export_assessment(assessment_id) or {}
        export["existing"] = action == "reuse"
        return {
            "action": action,
            "assessment_id": str(assessment_id),
            "audit_identity_hash": identity_hash,
            "existing": action == "reuse",
            "assessment": export,
        }

    def export_assessment(self, assessment_id: UUID) -> dict[str, Any] | None:
        """Export a complete audit assessment with all stage outputs."""
        with self.uow_factory() as uow:
            return uow.export_audit_assessment(assessment_id)

    def assess_run(
        self,
        run_id: UUID,
        external_run_id: str,
        target_hash: str,
        evaluator_version: str,
        prompt_template_version: str,
        policy_version: str,
        stage_set: list[str],
        status: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        prompt_hash: str | None = None,
        model_fingerprint: str | None = None,
        elapsed_ms: int = 0,
        audit_packet_manifest: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Schedule a target-scoped run assessment idempotently."""
        result = self.schedule_assessment(
            run_id=run_id,
            target_type="run",
            target_id=run_id,
            target_hash=target_hash,
            evaluator_version=evaluator_version,
            prompt_template_version=prompt_template_version,
            policy_version=policy_version,
            stage_set=stage_set,
            status=status,
            provider=provider,
            model=model,
            prompt_hash=prompt_hash,
            model_fingerprint=model_fingerprint,
            elapsed_ms=elapsed_ms,
            audit_packet_manifest=audit_packet_manifest,
        )
        assessment = dict(result["assessment"])
        assessment["external_run_id"] = external_run_id
        assessment["action"] = result["action"]
        return assessment

    def assess_invocation(
        self,
        run_id: UUID,
        invocation_id: UUID,
        target_hash: str,
        evaluator_version: str,
        prompt_template_version: str,
        policy_version: str,
        stage_set: list[str],
        status: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        prompt_hash: str | None = None,
        model_fingerprint: str | None = None,
        elapsed_ms: int = 0,
        audit_packet_manifest: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Schedule a target-scoped invocation assessment idempotently."""
        result = self.schedule_assessment(
            run_id=run_id,
            target_type="invocation",
            target_id=invocation_id,
            target_hash=target_hash,
            evaluator_version=evaluator_version,
            prompt_template_version=prompt_template_version,
            policy_version=policy_version,
            stage_set=stage_set,
            status=status,
            provider=provider,
            model=model,
            prompt_hash=prompt_hash,
            model_fingerprint=model_fingerprint,
            elapsed_ms=elapsed_ms,
            audit_packet_manifest=audit_packet_manifest,
        )
        assessment = dict(result["assessment"])
        assessment["invocation_id"] = str(invocation_id)
        assessment["action"] = result["action"]
        return assessment
