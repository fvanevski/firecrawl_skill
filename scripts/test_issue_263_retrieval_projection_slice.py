"""Issue #263 retrieval/projection vertical-slice regressions."""

from __future__ import annotations

import ast
from pathlib import Path

from research_store import indexing as legacy_indexing
from research_store import qdrant as legacy_qdrant
from research_store import retrieval as canonical_retrieval
from research_store import retrieval_service as legacy_retrieval_service
from research_store.postgres_retrieval import (
    PostgresIndexJobRepository as LegacyPostgresIndexJobRepository,
)
from research_store.postgres_retrieval import (
    PostgresRetrievalRepository as LegacyPostgresRetrievalRepository,
)
from research_store.retrieval import service as canonical_retrieval_service
from research_store.retrieval.postgres import PostgresRetrievalRepository
from research_store.retrieval.projection import indexing as canonical_indexing
from research_store.retrieval.projection import qdrant as canonical_qdrant
from research_store.retrieval.projection.postgres_jobs import PostgresIndexJobRepository

ROOT = Path(__file__).resolve().parents[1]
STORE = ROOT / "scripts" / "research_store"


def _defined_symbols(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }


def test_retrieval_module_surface_is_now_the_capability_package() -> None:
    assert hasattr(canonical_retrieval, "__path__")
    assert callable(canonical_retrieval.reciprocal_rank_fusion)
    assert callable(canonical_retrieval.pack_context)
    assert canonical_retrieval.CohereCompatibleReranker.__module__.endswith(
        ".research_store.retrieval"
    )
    assert _defined_symbols(STORE / "retrieval.py") == set()


def test_legacy_retrieval_service_is_canonical_application_service() -> None:
    assert legacy_retrieval_service is canonical_retrieval_service
    assert canonical_retrieval_service.RetrievalService.__module__.endswith(
        ".research_store.retrieval.service"
    )
    assert _defined_symbols(STORE / "retrieval_service.py") == set()


def test_postgres_repositories_are_split_by_authority() -> None:
    assert LegacyPostgresRetrievalRepository is PostgresRetrievalRepository
    assert LegacyPostgresIndexJobRepository is PostgresIndexJobRepository
    assert PostgresRetrievalRepository.__module__.endswith(
        ".research_store.retrieval.postgres"
    )
    assert PostgresIndexJobRepository.__module__.endswith(
        ".research_store.retrieval.projection.postgres_jobs"
    )
    assert _defined_symbols(STORE / "postgres_retrieval.py") == set()


def test_legacy_qdrant_module_is_the_canonical_projection_module() -> None:
    assert legacy_qdrant is canonical_qdrant
    assert canonical_qdrant.QdrantIndex.__module__.endswith(
        ".research_store.retrieval.projection.qdrant"
    )
    assert _defined_symbols(STORE / "qdrant.py") == set()


def test_legacy_indexing_module_is_the_canonical_projection_module() -> None:
    assert legacy_indexing is canonical_indexing
    assert canonical_indexing.IndexWorker.__module__.endswith(
        ".research_store.retrieval.projection.indexing"
    )
    assert canonical_indexing.OpenAICompatibleEmbedder.__module__.endswith(
        ".research_store.retrieval.projection.indexing"
    )
    assert _defined_symbols(STORE / "indexing.py") == set()


def test_projection_boundary_declares_non_authoritative_qdrant_contract() -> None:
    source = (
        STORE / "retrieval" / "projection" / "__init__.py"
    ).read_text(encoding="utf-8")
    assert "Qdrant remains rebuildable and non-authoritative" in source
    assert "PostgreSQL retains durable" in source
