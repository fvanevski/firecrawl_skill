"""Issue #263 retrieval/projection vertical-slice regressions."""

from __future__ import annotations

import ast
from pathlib import Path

from research_store import checkpoint_indexing_stage as legacy_checkpoint_stage
from research_store import index_checkpoint_service as legacy_checkpoint_service
from research_store import indexing as legacy_indexing
from research_store import projection_reconciliation as legacy_reconciliation
from research_store import qdrant as legacy_qdrant
from research_store import qdrant_authority as legacy_authority
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
from research_store.retrieval.projection import (
    authority as canonical_authority,
    checkpoint_indexing_stage as canonical_checkpoint_stage,
    index_checkpoint_service as canonical_checkpoint_service,
    indexing as canonical_indexing,
    qdrant as canonical_qdrant,
    reconciliation as canonical_reconciliation,
)
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
        ".research_store.retrieval.ranking"
    )
    assert _defined_symbols(STORE / "retrieval.py") == set()
    assert {"CohereCompatibleReranker", "reciprocal_rank_fusion"}.issubset(
        _defined_symbols(STORE / "retrieval" / "ranking.py")
    )


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


def test_indexing_uses_baseline_stable_implementation_with_projection_namespace() -> None:
    assert canonical_indexing.IndexWorker is legacy_indexing.IndexWorker
    assert canonical_indexing.OpenAICompatibleEmbedder is (
        legacy_indexing.OpenAICompatibleEmbedder
    )
    assert legacy_indexing.IndexWorker.__module__.endswith(".research_store.indexing")
    assert legacy_indexing.OpenAICompatibleEmbedder.__module__.endswith(
        ".research_store.indexing"
    )
    assert {"IndexWorker", "OpenAICompatibleEmbedder"}.issubset(
        _defined_symbols(STORE / "indexing.py")
    )
    assert (
        _defined_symbols(STORE / "retrieval" / "projection" / "indexing.py") == set()
    )


def test_qdrant_authority_and_reconciliation_are_projection_infrastructure() -> None:
    assert legacy_authority is canonical_authority
    assert legacy_reconciliation is canonical_reconciliation
    assert canonical_authority.evaluate_required_alias_state.__module__.endswith(
        ".research_store.retrieval.projection.authority"
    )
    assert canonical_reconciliation.reconcile_projection_compat.__module__.endswith(
        ".research_store.retrieval.projection.reconciliation"
    )
    assert _defined_symbols(STORE / "qdrant_authority.py") == set()
    assert _defined_symbols(STORE / "projection_reconciliation.py") == set()


def test_checkpoints_use_baseline_stable_implementation_with_projection_namespace() -> None:
    assert canonical_checkpoint_service.IndexCheckpointService is (
        legacy_checkpoint_service.IndexCheckpointService
    )
    assert canonical_checkpoint_stage.CheckpointIndexingStage is (
        legacy_checkpoint_stage.CheckpointIndexingStage
    )
    assert legacy_checkpoint_service.IndexCheckpointService.__module__.endswith(
        ".research_store.index_checkpoint_service"
    )
    assert legacy_checkpoint_stage.CheckpointIndexingStage.__module__.endswith(
        ".research_store.checkpoint_indexing_stage"
    )
    assert "IndexCheckpointService" in _defined_symbols(
        STORE / "index_checkpoint_service.py"
    )
    assert "CheckpointIndexingStage" in _defined_symbols(
        STORE / "checkpoint_indexing_stage.py"
    )
    assert (
        _defined_symbols(
            STORE / "retrieval" / "projection" / "index_checkpoint_service.py"
        )
        == set()
    )
    assert (
        _defined_symbols(
            STORE / "retrieval" / "projection" / "checkpoint_indexing_stage.py"
        )
        == set()
    )


def test_projection_boundary_declares_non_authoritative_qdrant_contract() -> None:
    source = (
        STORE / "retrieval" / "projection" / "__init__.py"
    ).read_text(encoding="utf-8")
    assert "Qdrant remains rebuildable and non-authoritative" in source
    assert "PostgreSQL retains durable" in source
