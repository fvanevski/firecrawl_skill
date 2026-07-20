from __future__ import annotations

from functools import partial

from .blob import ContentAddressedBlobStore
from .config import StoreConfig
from .postgres import PostgresUnitOfWork
from .indexing import OpenAICompatibleEmbedder
from .qdrant import QdrantIndex
from .retrieval import CohereCompatibleReranker
from .service import CorpusService


def build_service(config: StoreConfig | None = None) -> CorpusService:
    config = config or StoreConfig.from_env()
    config.require_database()
    embedder = (
        OpenAICompatibleEmbedder(
            config.embedding_url,
            config.embedding_model,
            config.embedding_api_key,
            config.embedding_dimension,
        )
        if config.embedding_url
        else None
    )
    index = QdrantIndex(
        config.qdrant_url,
        config.qdrant_api_key,
        config.qdrant_collection,
        config.embedding_dimension,
    )
    reranker = (
        CohereCompatibleReranker(
            config.reranker_url, config.reranker_model, config.reranker_api_key
        )
        if config.reranker_url
        else None
    )
    return CorpusService(
        config,
        partial(
            PostgresUnitOfWork,
            config.database_url,
            config.qdrant_collection,
            config.embedding_model,
            config.embedding_revision,
            config.embedding_dimension,
        ),
        ContentAddressedBlobStore(config.blob_root),
        index=index,
        embedder=embedder,
        reranker=reranker,
    )
