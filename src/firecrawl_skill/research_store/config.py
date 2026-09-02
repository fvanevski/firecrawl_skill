from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _integer(name: str, default: int) -> int:
    value = int(os.environ.get(name, default))
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(
            f"{name} must be set explicitly or loaded from the repository .env"
        )
    return value


def _required_integer(name: str) -> int:
    value = int(_required(name))
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _non_negative_integer(name: str, default: int) -> int:
    """Read an integer env var that may be zero (zero means "unlimited")."""
    value = int(os.environ.get(name, default))
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value


@dataclass(frozen=True)
class StoreConfig:
    database_url: str
    qdrant_url: str
    qdrant_api_key: str
    qdrant_collection: str
    qdrant_alias: str
    valkey_url: str
    blob_root: Path
    embedding_model: str
    embedding_url: str
    embedding_api_key: str
    embedding_revision: str
    embedding_dimension: int
    reranker_url: str
    reranker_model: str
    reranker_api_key: str
    reranker_candidate_limit: int
    chunker_name: str
    chunker_version: str
    chunker_max_tokens: int
    tokenizer_name: str
    parser_version: str
    normalization_version: str
    parser_registry_version: str
    max_index_attempts: int
    job_lease_seconds: int
    worker_poll_seconds: int
    embedding_batch_size: int
    # Resource governance (P7-06)
    generative_url: str
    generative_model: str
    generative_api_key: str
    generative_max_concurrent: int
    generative_max_input_tokens: int
    generative_max_batch_size: int
    generative_health_check_interval: int
    generative_backpressure_threshold: int
    generative_token_cap: int
    embedding_max_concurrent: int
    embedding_max_batch_size: int
    embedding_health_check_interval: int
    embedding_backpressure_threshold: int
    reranker_max_concurrent: int
    reranker_max_batch_size: int
    reranker_health_check_interval: int
    reranker_backpressure_threshold: int
    # Host artifact supplier for agent-led benchmark runs (issue #170).
    host_artifact_supplier: Any = None

    @classmethod
    def from_env(cls) -> StoreConfig:
        return cls(
            database_url=os.environ.get("DATABASE_URL", ""),
            qdrant_url=os.environ.get("QDRANT_URL", "http://localhost:6333"),
            qdrant_api_key=os.environ.get("QDRANT_API_KEY", ""),
            qdrant_collection=os.environ.get("QDRANT_COLLECTION", "research_chunks_v1"),
            qdrant_alias=os.environ.get("QDRANT_ALIAS", "research_chunks_active"),
            valkey_url=os.environ.get("VALKEY_URL", "redis://localhost:6379/0"),
            blob_root=Path(
                os.environ.get(
                    "BLOB_ROOT", Path.home() / ".local/share/firecrawl/blobs"
                )
            ),
            embedding_model=_required("EMBEDDING_MODEL"),
            embedding_url=os.environ.get("EMBEDDING_URL", ""),
            embedding_api_key=os.environ.get("EMBEDDING_API_KEY", ""),
            embedding_revision=_required("EMBEDDING_REVISION"),
            embedding_dimension=_required_integer("EMBEDDING_DIMENSION"),
            reranker_url=os.environ.get("RERANKER_URL", ""),
            reranker_model=os.environ.get("RERANKER_MODEL", "rerank"),
            reranker_api_key=os.environ.get("RERANKER_API_KEY", ""),
            reranker_candidate_limit=_integer("RERANKER_CANDIDATE_LIMIT", 40),
            chunker_name=os.environ.get("CHUNKER_NAME", "hierarchical"),
            chunker_version=os.environ.get("CHUNKER_VERSION", "structural-v1"),
            chunker_max_tokens=_integer("CHUNKER_MAX_TOKENS", 1000),
            tokenizer_name=os.environ.get("TOKENIZER_NAME", "cl100k_base"),
            parser_version=os.environ.get("PARSER_VERSION", "markdown-v1"),
            normalization_version=os.environ.get("NORMALIZATION_VERSION", "cleanup-v1"),
            parser_registry_version=os.environ.get(
                "PARSER_REGISTRY_VERSION", "canonical-v1"
            ),
            max_index_attempts=_integer("MAX_INDEX_ATTEMPTS", 5),
            job_lease_seconds=_integer("INDEX_JOB_LEASE_SECONDS", 300),
            worker_poll_seconds=_integer("INDEX_WORKER_POLL_SECONDS", 5),
            embedding_batch_size=_integer("EMBEDDING_BATCH_SIZE", 32),
            # Resource governance (P7-06)
            generative_url=os.environ.get("GENERATIVE_URL", ""),
            generative_model=_required("GENERATIVE_MODEL"),
            generative_api_key=os.environ.get("GENERATIVE_API_KEY", ""),
            generative_max_concurrent=_integer("GENERATIVE_MAX_CONCURRENT", 1),
            generative_max_input_tokens=_non_negative_integer(
                "GENERATIVE_MAX_INPUT_TOKENS", 0
            ),
            generative_max_batch_size=_non_negative_integer(
                "GENERATIVE_MAX_BATCH_SIZE", 1
            ),
            generative_health_check_interval=_non_negative_integer(
                "GENERATIVE_HEALTH_CHECK_INTERVAL", 30
            ),
            generative_backpressure_threshold=_non_negative_integer(
                "GENERATIVE_BACKPRESSURE_THRESHOLD", 0
            ),
            generative_token_cap=_non_negative_integer("GENERATIVE_TOKEN_CAP", 0),
            embedding_max_concurrent=_integer("EMBEDDING_MAX_CONCURRENT", 4),
            embedding_max_batch_size=_non_negative_integer(
                "EMBEDDING_MAX_BATCH_SIZE", 0
            ),
            embedding_health_check_interval=_non_negative_integer(
                "EMBEDDING_HEALTH_CHECK_INTERVAL", 30
            ),
            embedding_backpressure_threshold=_non_negative_integer(
                "EMBEDDING_BACKPRESSURE_THRESHOLD", 0
            ),
            reranker_max_concurrent=_integer("RERANKER_MAX_CONCURRENT", 2),
            reranker_max_batch_size=_non_negative_integer("RERANKER_MAX_BATCH_SIZE", 0),
            reranker_health_check_interval=_non_negative_integer(
                "RERANKER_HEALTH_CHECK_INTERVAL", 30
            ),
            reranker_backpressure_threshold=_non_negative_integer(
                "RERANKER_BACKPRESSURE_THRESHOLD", 0
            ),
        )

    @property
    def embedding_fingerprint(self) -> str:
        payload = {
            "model": self.embedding_model,
            "revision": self.embedding_revision,
            "dimension": self.embedding_dimension,
            "distance": "Cosine",
            "normalization": "unit-length",
            "instruction_template_hash": "",
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    @property
    def physical_collection(self) -> str:
        return f"research_chunks_{self.embedding_fingerprint[:12]}"

    def require_database(self) -> None:
        if not self.database_url:
            raise RuntimeError(
                "DATABASE_URL is required for the authoritative research store"
            )
        if not self.database_url.startswith(("postgresql://", "postgres://")):
            raise ValueError("DATABASE_URL must identify PostgreSQL")
