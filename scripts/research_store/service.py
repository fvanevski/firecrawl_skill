from __future__ import annotations

from io import BytesIO
import json
from typing import Callable
from uuid import UUID

from .config import StoreConfig
from .domain import IngestRequest, IngestResult
from .parsing import deterministic_chunks, structural_blocks
from .retrieval import reciprocal_rank_fusion
from .url import canonicalize_url


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
    ):
        self.config = config
        self.uow_factory = uow_factory
        self.blob_store = blob_store
        self.index = index
        self.embedder = embedder
        self.reranker = reranker

    def ingest(self, request: IngestRequest) -> IngestResult:
        canonical = canonicalize_url(request.final_url or request.requested_url)
        blob = self.blob_store.put(BytesIO(request.content), request.mime_type)
        normalized = (
            request.normalized_content
            if request.normalized_content is not None
            else request.content
        )
        text = normalized.decode("utf-8", errors="replace").replace("\r\n", "\n")
        blocks = structural_blocks(text)
        if not blocks:
            raise ValueError("retrieved content produced no structural blocks")
        chunks = deterministic_chunks(blocks)
        with self.uow_factory() as uow:
            return uow.snapshots.persist_ingest(
                request,
                canonical,
                blob,
                text,
                blocks,
                chunks,
                self.config.parser_version,
                self.config.chunker_version,
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
    ) -> list[dict]:
        if not query.strip():
            raise ValueError("query is required")
        if not 1 <= candidate_limit <= 200:
            raise ValueError("candidate_limit must be 1..200")
        filters = filters or {}
        with self.uow_factory() as uow:
            lexical = uow.documents.search_lexical(query, candidate_limit * 2, filters)
            for item in lexical:
                item["candidate_id"] = str(item["candidate_id"])
                item["retriever"] = "postgres_fts"
            semantic = []
            if self.index and self.embedder:
                try:
                    points = self.index.search(
                        self.embedder(query),
                        _qdrant_filter(filters),
                        candidate_limit * 2,
                    )
                    semantic = [_semantic_candidate(point) for point in points]
                except Exception:
                    semantic = []
            candidates = reciprocal_rank_fusion([lexical, semantic])[
                : self.config.reranker_candidate_limit
            ]
            passages = uow.documents.fetch_passages(
                [UUID(str(item["candidate_id"])) for item in candidates],
                50000,
                len(candidates),
                False,
            )
            excerpts = {str(item["chunk_id"]): item["text"][:400] for item in passages}
            for item in candidates:
                item["excerpt"] = item.get("excerpt") or excerpts.get(
                    str(item["candidate_id"]), ""
                )
            if self.reranker:
                candidates = self.reranker(query, candidates)
            candidates = candidates[:candidate_limit]
            if run_id:
                for rank, candidate in enumerate(candidates, 1):
                    uow.retrieval_events.log_retrieval(
                        run_id,
                        {
                            "stage": "lexical",
                            "query": query,
                            "filters": filters,
                            "retriever": candidate.get("retriever", "hybrid_rrf"),
                            "candidate_type": "chunk",
                            "candidate_id": candidate["candidate_id"],
                            "raw_score": candidate.get("lexical_score")
                            or candidate.get("semantic_score"),
                            "normalized_score": candidate.get("fused_score"),
                            "reranker_score": candidate.get("reranker_score"),
                            "rank": rank,
                            "selected": True,
                        },
                    )
            return candidates

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


def _qdrant_filter(filters: dict) -> dict:
    must = []
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
