"""Final retrieval/projection ownership regressions after issue #269 cleanup."""

from __future__ import annotations

import ast
from pathlib import Path

from firecrawl_skill.research_store.retrieval import ranking, service
from firecrawl_skill.research_store.retrieval.postgres import (
    PostgresRetrievalRepository,
)
from firecrawl_skill.research_store.retrieval.projection import (
    authority,
    reconciliation,
)
from firecrawl_skill.research_store.retrieval.projection.checkpoint_indexing_stage import (
    CheckpointIndexingStage,
)
from firecrawl_skill.research_store.retrieval.projection.index_checkpoint_service import (
    IndexCheckpointService,
)
from firecrawl_skill.research_store.retrieval.projection.indexing import (
    IndexWorker,
    OpenAICompatibleEmbedder,
)
from firecrawl_skill.research_store.retrieval.projection.postgres_jobs import (
    PostgresIndexJobRepository,
)
from firecrawl_skill.research_store.retrieval.projection.qdrant import QdrantIndex

ROOT = Path(__file__).resolve().parents[2]
STORE = ROOT / "src" / "firecrawl_skill" / "research_store"
PROJECTION = STORE / "retrieval" / "projection"
WORKFLOW = ROOT / ".github" / "workflows" / "retrieval-projection-slice-review.yml"

_OBSOLETE_RETRIEVAL_PATHS = (
    "retrieval.py",
    "retrieval_core.py",
    "retrieval_service.py",
    "postgres_retrieval.py",
    "qdrant.py",
    "qdrant_authority.py",
    "projection_reconciliation.py",
    "indexing.py",
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


def test_final_retrieval_owners_are_canonical_package_modules() -> None:
    assert ranking.CohereCompatibleReranker.__module__.endswith(".retrieval.ranking")
    assert service.RetrievalService.__module__.endswith(".retrieval.service")
    assert PostgresRetrievalRepository.__module__.endswith(".retrieval.postgres")
    assert PostgresIndexJobRepository.__module__.endswith(
        ".retrieval.projection.postgres_jobs"
    )
    assert QdrantIndex.__module__.endswith(".retrieval.projection.qdrant")
    assert authority.evaluate_required_alias_state.__module__.endswith(
        ".retrieval.projection.authority"
    )
    assert reconciliation.reconcile_projection_compat.__module__.endswith(
        ".retrieval.projection.reconciliation"
    )


def test_indexing_and_checkpoint_implementations_live_in_projection() -> None:
    assert IndexWorker.__module__.endswith(".retrieval.projection.indexing")
    assert OpenAICompatibleEmbedder.__module__.endswith(".retrieval.projection.indexing")
    assert IndexCheckpointService.__module__.endswith(
        ".retrieval.projection.index_checkpoint_service"
    )
    assert CheckpointIndexingStage.__module__.endswith(
        ".retrieval.projection.checkpoint_indexing_stage"
    )
    assert {"IndexWorker", "OpenAICompatibleEmbedder"}.issubset(
        _defined_symbols(PROJECTION / "indexing.py")
    )
    assert "IndexCheckpointService" in _defined_symbols(
        PROJECTION / "index_checkpoint_service.py"
    )
    assert "CheckpointIndexingStage" in _defined_symbols(
        PROJECTION / "checkpoint_indexing_stage.py"
    )


def test_obsolete_retrieval_facades_are_physically_absent() -> None:
    remaining = [name for name in _OBSOLETE_RETRIEVAL_PATHS if (STORE / name).exists()]
    assert remaining == [], f"obsolete retrieval/projection facades remain: {remaining}"


def test_projection_boundary_declares_non_authoritative_qdrant_contract() -> None:
    source = (PROJECTION / "__init__.py").read_text(encoding="utf-8")
    assert "Qdrant remains rebuildable and non-authoritative" in source
    assert "PostgreSQL retains durable" in source


def test_slice_workflow_tracks_final_projection_owners() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert '"src/firecrawl_skill/research_store/retrieval/projection/**"' in workflow
    for staged_root in (
        '"src/firecrawl_skill/research_store/indexing.py"',
        '"src/firecrawl_skill/research_store/checkpoint_indexing_stage.py"',
        '"src/firecrawl_skill/research_store/index_checkpoint_*.py"',
    ):
        assert staged_root not in workflow
