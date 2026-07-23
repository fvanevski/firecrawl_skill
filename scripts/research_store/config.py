from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path


def _integer(name: str, default: int) -> int:
    value = int(os.environ.get(name, default))
    if value <= 0:
        raise ValueError(f"{name} must be positive")
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
    scratch_root: Path
    embedding_model: str
    embedding_url: str
    embedding_api_key: str
    embedding_revision: str
    embedding_dimension: int
    reranker_url: str
    reranker_model: str
    reranker_api_key: str
    reranker_candidate_limit: int
    chunker_version: str
    parser_version: str
    normalization_version: str
    parser_registry_version: str
    max_index_attempts: int
    job_lease_seconds: int
    worker_poll_seconds: int

    @classmethod
    def from_env(cls) -> "StoreConfig":
        temp = Path(os.environ.get("TMPDIR", "/tmp"))
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
            scratch_root=Path(
                os.environ.get("SCRATCH_ROOT", temp / "firecrawl_scratch")
            ),
            embedding_model=os.environ.get("EMBEDDING_MODEL", "embed"),
            embedding_url=os.environ.get("EMBEDDING_URL", ""),
            embedding_api_key=os.environ.get("EMBEDDING_API_KEY", ""),
            embedding_revision=os.environ.get("EMBEDDING_REVISION", "main"),
            embedding_dimension=_integer("EMBEDDING_DIMENSION", 1024),
            reranker_url=os.environ.get("RERANKER_URL", ""),
            reranker_model=os.environ.get("RERANKER_MODEL", "rerank"),
            reranker_api_key=os.environ.get("RERANKER_API_KEY", ""),
            reranker_candidate_limit=_integer("RERANKER_CANDIDATE_LIMIT", 40),
            chunker_version=os.environ.get("CHUNKER_VERSION", "structural-v1"),
            parser_version=os.environ.get("PARSER_VERSION", "markdown-v1"),
            normalization_version=os.environ.get("NORMALIZATION_VERSION", "cleanup-v1"),
            parser_registry_version=os.environ.get(
                "PARSER_REGISTRY_VERSION", "canonical-v1"
            ),
            max_index_attempts=_integer("MAX_INDEX_ATTEMPTS", 5),
            job_lease_seconds=_integer("INDEX_JOB_LEASE_SECONDS", 300),
            worker_poll_seconds=_integer("INDEX_WORKER_POLL_SECONDS", 5),
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
