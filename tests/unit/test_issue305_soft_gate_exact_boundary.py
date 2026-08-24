from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from firecrawl_skill.research_store.asset_promotion_models import AssetPromotionError
from firecrawl_skill.research_store.candidate_budget_outcomes import (
    CandidateBudgetOverrideRequired,
    classify_persisted_completion_admission,
)
from firecrawl_skill.research_store.retrieval.projection import (
    checkpoint_indexing_stage as stage_module,
)
from firecrawl_skill.research_store.retrieval.projection.checkpoint_indexing_stage import (
    CheckpointIndexingStage,
)


class _Policy:
    def __init__(self, checks):
        self.checks = checks

    def list_checks(self, _run_id: UUID):
        return self.checks


def _check(check_id: UUID, *, soft: bool) -> dict:
    return {
        "id": str(check_id),
        "phase": "completion_admission",
        "lifecycle_revision": 7,
        "scope": {"subject_ids": [str(uuid4())]},
        "content_sha256": check_id.hex * 2,
        "hard_violations": [],
        "soft_violations": ([{"limit_name": "max_generic_page_share"}] if soft else []),
        "overridden_limits": [],
    }


def test_exact_check_id_prevents_stale_soft_check_reclassification() -> None:
    stale_id = uuid4()
    accepted_id = uuid4()
    policy = _Policy([_check(stale_id, soft=True), _check(accepted_id, soft=False)])

    assert isinstance(
        classify_persisted_completion_admission(policy, uuid4(), 7, check_id=stale_id),
        CandidateBudgetOverrideRequired,
    )
    assert (
        classify_persisted_completion_admission(
            policy, uuid4(), 7, check_id=accepted_id
        )
        is None
    )


def test_unrelated_asset_promotion_error_is_not_masked_by_stale_soft_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Checkpoints:
        def __init__(self, *_args, **_kwargs):
            pass

        def ensure(self, *_args, **_kwargs):
            raise AssetPromotionError("unrelated compatibility failure")

    monkeypatch.setattr(stage_module, "IndexCheckpointService", _Checkpoints)
    corpus = SimpleNamespace(
        index=object(),
        embedder=SimpleNamespace(fingerprint="fp"),
    )
    stage = CheckpointIndexingStage(
        SimpleNamespace(uow_factory=object()),
        SimpleNamespace(
            max_index_attempts=5,
            job_lease_seconds=60,
            embedding_batch_size=8,
        ),
        corpus,
    )

    result = stage.execute(uuid4(), 7, None, "indexing", {})

    assert (
        result.error
        == "index checkpoint creation failed: unrelated compatibility failure"
    )
    assert result.outcome.value == "terminal"
