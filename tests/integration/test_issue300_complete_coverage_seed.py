"""Regression for multi-item coverage seeding discovered during issue #300 audit."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from firecrawl_skill.research_store.composition import build_run_service
from firecrawl_skill.research_store.config import StoreConfig
from firecrawl_skill.research_store.coverage_seed_service import CompleteCoverageService
from firecrawl_skill.research_store.postgres import migrate

TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL") or ""
pytestmark = pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)


@pytest.fixture
def coverage_config(tmp_path: Path) -> StoreConfig:
    migrate(TEST_DSN)
    return replace(
        StoreConfig.from_env(),
        database_url=TEST_DSN,
        blob_root=tmp_path / "blobs",
        qdrant_collection=f"issue300_coverage_{uuid4().hex}",
        embedding_dimension=4,
    )


def test_multi_item_spec_seeds_every_item_and_replay_preserves_identities(
    coverage_config: StoreConfig,
) -> None:
    runs = build_run_service(coverage_config)
    external_id = f"fr_issue300_coverage_{uuid4().hex}"
    status = runs.create("issue 300 coverage seed", external_id)
    service = CompleteCoverageService(runs.uow_factory)
    identifiers = [uuid4() for _ in range(6)]
    spec = {
        "questions": [{"question_id": str(identifiers[0]), "text": "Question"}],
        "claims_to_validate": [{"claim_id": str(identifiers[1]), "statement": "Claim"}],
        "freshness_requirements": [
            {
                "requirement_id": str(identifiers[2]),
                "description": "Fresh within five days",
                "max_age_days": 5,
            },
            {
                "requirement_id": str(identifiers[3]),
                "description": "Fresh within thirty days",
                "max_age_days": 30,
            },
        ],
        "required_source_classes": [
            {
                "requirement_id": str(identifiers[4]),
                "source_class": "primary",
                "minimum_count": 1,
            }
        ],
        "corroboration_requirements": [
            {
                "requirement_id": str(identifiers[5]),
                "description": "Corroborate",
                "required_independent_source_count": 1,
            }
        ],
        "contradiction_requirements": [],
    }

    first = service.create_items_from_spec(
        status.id, spec, execution_mode=status.execution_mode
    )
    replay = service.create_items_from_spec(
        status.id, spec, execution_mode=status.execution_mode
    )

    assert len(first) == 6
    assert [item.coverage_item_id for item in replay] == [
        item.coverage_item_id for item in first
    ]
    assert [item.subject_id for item in first] == [str(value) for value in identifiers]
    freshness_items = [
        item for item in first if item.item_type.value == "freshness_requirement"
    ]
    assert len(freshness_items) == 2
    assert {item.freshness_status.value for item in freshness_items} == {"uncertain"}
    projection = service.rebuild_projection(
        status.id, idempotency_key=f"issue300:coverage:projection:{status.id}"
    )
    projected_freshness = {
        item.subject_id: item.freshness_status.value
        for item in projection.items
        if item.item_type.value == "freshness_requirement"
    }
    assert projected_freshness == {
        str(identifiers[2]): "uncertain",
        str(identifiers[3]): "uncertain",
    }
    with runs.uow_factory() as uow, uow.connection.cursor() as cursor:
        cursor.execute(
            """SELECT count(*),count(DISTINCT item_id)
                 FROM coverage_events
                WHERE run_id=%s AND event_type='item_created'""",
            (status.id,),
        )
        assert cursor.fetchone() == (6, 6)
