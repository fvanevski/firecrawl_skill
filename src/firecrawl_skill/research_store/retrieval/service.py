from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

from firecrawl_skill.research_domain.models import MechanicalStatus, RetrievalExecution

from ..config import StoreConfig
from . import reciprocal_rank_fusion


class RetrievalService:
    """Retrieval/search capability shared by the canonical corpus service."""

    def __init__(
        self,
        config: StoreConfig,
        uow_factory: Callable,
        *,
        index=None,
        embedder=None,
        reranker=None,
    ) -> None:
        self.config = config
        self.uow_factory = uow_factory
        self.index = index
        self.embedder = embedder
        self.reranker = reranker

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
                uow.retrieval_events.record_retrieval_execution(run_id, execution)
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
            uow.retrieval_events.record_retrieval_execution(run_id, execution)
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
