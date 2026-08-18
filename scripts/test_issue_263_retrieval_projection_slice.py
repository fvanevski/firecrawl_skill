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
from research_store.retrieval.projection import authority as canonical_authority
from research_store.retrieval.projection import (
    checkpoint_indexing_stage as canonical_checkpoint_stage,
)
from research_store.retrieval.projection import (
    index_checkpoint_service as canonical_checkpoint_service,
)
from research_store.retrieval.projection import indexing as canonical_indexing
from research_store.retrieval.projection import qdrant as canonical_qdrant
from research_store.retrieval.projection import (
    reconciliation as canonical_reconciliation,
)
from research_store.retrieval.projection.postgres_jobs import PostgresIndexJobRepository

ROOT = Path(__file__).resolve().parents[1]
STORE = ROOT / "scripts" / "research_store"

_CHECKPOINT_FACADE_FILES = (
    "checkpoint_indexing_stage.py",
    "index_checkpoint_asset_membership.py",
    "index_checkpoint_core.py",
    "index_checkpoint_finalize.py",
    "index_checkpoint_models.py",
    "index_checkpoint_replay.py",
    "index_checkpoint_service.py",
    "index_checkpoint_store.py",
)


def _defined_symbols(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _is_research_store_module(module: str, suffix: str) -> bool:
    """Accept the supported source-test and canonical package import roots.

    ``scripts/conftest.py`` deliberately places ``scripts`` first during the
    historical pytest corpus, so modules may be loaded as ``research_store.*``.
    Canonical source/wheel imports load the same implementation as
    ``firecrawl_skill.research_store.*``. Ownership assertions care about the
    exact capability-relative module, not which supported package root loaded it.
    """
    target = f"research_store.{suffix}"
    return module == target or module.endswith(f".{target}")


def test_module_ownership_matcher_is_exact_about_research_store_boundary() -> None:
    assert _is_research_store_module(
        "research_store.retrieval.ranking", "retrieval.ranking"
    )
    assert _is_research_store_module(
        "firecrawl_skill.research_store.retrieval.ranking", "retrieval.ranking"
    )
    assert not _is_research_store_module(
        "not_research_store.retrieval.ranking", "retrieval.ranking"
    )


def test_retrieval_module_surface_is_now_the_capability_package() -> None:
    assert hasattr(canonical_retrieval, "__path__")
    assert callable(canonical_retrieval.reciprocal_rank_fusion)
    assert callable(canonical_retrieval.pack_context)
    assert _is_research_store_module(
        canonical_retrieval.CohereCompatibleReranker.__module__, "retrieval.ranking"
    )

    # These sibling files are migration-only source residue. They must never
    # regain domain implementation while #269 owns their eventual deletion.
    assert _defined_symbols(STORE / "retrieval.py") == set()
    assert _defined_symbols(STORE / "retrieval_core.py") == set()

    assert {"CohereCompatibleReranker", "reciprocal_rank_fusion"}.issubset(
        _defined_symbols(STORE / "retrieval" / "ranking.py")
    )


def test_legacy_retrieval_service_is_canonical_application_service() -> None:
    assert legacy_retrieval_service is canonical_retrieval_service
    assert _is_research_store_module(
        canonical_retrieval_service.RetrievalService.__module__, "retrieval.service"
    )
    assert _defined_symbols(STORE / "retrieval_service.py") == set()


def test_postgres_repositories_are_split_by_authority() -> None:
    assert LegacyPostgresRetrievalRepository is PostgresRetrievalRepository
    assert LegacyPostgresIndexJobRepository is PostgresIndexJobRepository
    assert _is_research_store_module(
        PostgresRetrievalRepository.__module__, "retrieval.postgres"
    )
    assert _is_research_store_module(
        PostgresIndexJobRepository.__module__, "retrieval.projection.postgres_jobs"
    )
    assert _defined_symbols(STORE / "postgres_retrieval.py") == set()


def test_legacy_qdrant_module_is_the_canonical_projection_module() -> None:
    assert legacy_qdrant is canonical_qdrant
    assert _is_research_store_module(
        canonical_qdrant.QdrantIndex.__module__, "retrieval.projection.qdrant"
    )
    assert _defined_symbols(STORE / "qdrant.py") == set()


def test_indexing_uses_baseline_stable_implementation_with_projection_namespace() -> (
    None
):
    assert canonical_indexing.IndexWorker is legacy_indexing.IndexWorker
    assert canonical_indexing.OpenAICompatibleEmbedder is (
        legacy_indexing.OpenAICompatibleEmbedder
    )
    assert _is_research_store_module(legacy_indexing.IndexWorker.__module__, "indexing")
    assert _is_research_store_module(
        legacy_indexing.OpenAICompatibleEmbedder.__module__, "indexing"
    )
    assert {"IndexWorker", "OpenAICompatibleEmbedder"}.issubset(
        _defined_symbols(STORE / "indexing.py")
    )
    assert _defined_symbols(STORE / "retrieval" / "projection" / "indexing.py") == set()


def test_qdrant_authority_and_reconciliation_are_projection_infrastructure() -> None:
    assert legacy_authority is canonical_authority
    assert legacy_reconciliation is canonical_reconciliation
    assert _is_research_store_module(
        canonical_authority.evaluate_required_alias_state.__module__,
        "retrieval.projection.authority",
    )
    assert _is_research_store_module(
        canonical_reconciliation.reconcile_projection_compat.__module__,
        "retrieval.projection.reconciliation",
    )
    assert _defined_symbols(STORE / "qdrant_authority.py") == set()
    assert _defined_symbols(STORE / "projection_reconciliation.py") == set()


def test_checkpoints_use_baseline_stable_implementation_with_projection_namespace() -> (
    None
):
    assert canonical_checkpoint_service.IndexCheckpointService is (
        legacy_checkpoint_service.IndexCheckpointService
    )
    assert canonical_checkpoint_stage.CheckpointIndexingStage is (
        legacy_checkpoint_stage.CheckpointIndexingStage
    )
    assert _is_research_store_module(
        legacy_checkpoint_service.IndexCheckpointService.__module__,
        "index_checkpoint_service",
    )
    assert _is_research_store_module(
        legacy_checkpoint_stage.CheckpointIndexingStage.__module__,
        "checkpoint_indexing_stage",
    )
    assert "IndexCheckpointService" in _defined_symbols(
        STORE / "index_checkpoint_service.py"
    )
    assert "CheckpointIndexingStage" in _defined_symbols(
        STORE / "checkpoint_indexing_stage.py"
    )

    # The complete staged projection facade family must remain zero-domain-logic.
    # Physical relocation is a #269 type-debt migration, not an implicit #263 move.
    projection = STORE / "retrieval" / "projection"
    for filename in _CHECKPOINT_FACADE_FILES:
        assert _defined_symbols(projection / filename) == set()


def test_projection_boundary_declares_non_authoritative_qdrant_contract() -> None:
    source = (STORE / "retrieval" / "projection" / "__init__.py").read_text(
        encoding="utf-8"
    )
    assert "Qdrant remains rebuildable and non-authoritative" in source
    assert "PostgreSQL retains durable" in source
