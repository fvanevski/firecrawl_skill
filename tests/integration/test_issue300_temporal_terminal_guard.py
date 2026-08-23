"""PostgreSQL regressions for item-bound temporal terminal admission."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from completion_provenance_test_support import seed_authoritative_completion_provenance

from firecrawl_skill.research_store.completion_provenance import (
    load_authoritative_completion_provenance,
)
from firecrawl_skill.research_store.composition import build_run_service, build_service
from firecrawl_skill.research_store.config import StoreConfig
from firecrawl_skill.research_store.coverage_seed_service import CompleteCoverageService
from firecrawl_skill.research_store.domain import IngestRequest
from firecrawl_skill.research_store.postgres import migrate
from firecrawl_skill.research_store.temporal_provenance import (
    TemporalEvidenceError,
    assert_temporal_evidence_satisfied,
)
from firecrawl_skill.research_store.terminal_decision_service import (
    TerminalDecisionError,
)

TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL") or ""
pytestmark = pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)


@pytest.fixture
def temporal_guard_config(tmp_path: Path) -> StoreConfig:
    migrate(TEST_DSN)
    return replace(
        StoreConfig.from_env(),
        database_url=TEST_DSN,
        blob_root=tmp_path / "blobs",
        qdrant_collection=f"issue300_terminal_{uuid4().hex}",
        embedding_dimension=4,
    )


def _freshness_spec(status, *, count: int = 1, max_age_days: int = 5) -> dict:
    return {
        "schema_version": "research-spec-v1",
        "research_spec_id": str(uuid4()),
        "objective": "verify item-bound temporal terminal admission",
        "research_archetype": "fact_finding",
        "risk_level": "low",
        "execution_mode": status.execution_mode,
        "questions": [
            {
                "question_id": str(uuid4()),
                "text": "Does the evidence satisfy every temporal obligation?",
            }
        ],
        "claims_to_validate": [],
        "entities": [],
        "jurisdictions": [],
        "time_window": {
            "start": None,
            "end": None,
            "description": "Freshness-only issue 300 regression.",
            "uncertainty": "",
        },
        "freshness_requirements": [
            {
                "requirement_id": str(uuid4()),
                "description": f"freshness obligation {index}",
                "max_age_days": max_age_days,
            }
            for index in range(count)
        ],
        "required_source_classes": [],
        "corroboration_requirements": [],
        "contradiction_requirements": [],
        "excluded_interpretations": [],
        "structured_data_requirements": [],
        "completion_criteria": [
            {
                "criterion_id": str(uuid4()),
                "description": "Every bounded temporal obligation must qualify.",
                "mandatory": True,
            }
        ],
        "user_constraints": [],
        "ambiguities": [],
        "assumptions": [],
    }


def _ingest_chunks(config: StoreConfig, ages_days: tuple[int, ...]):
    runs = build_run_service(config)
    corpus = build_service(config)
    external_id = f"fr_issue300_terminal_{uuid4().hex}"
    status = runs.create("issue 300 temporal terminal guard", external_id)
    now = datetime.now(timezone.utc)
    requests: list[IngestRequest | dict[str, Any]] = [
        IngestRequest(
            f"https://temporal.example/{index}",
            f"# Evidence {index}\n\nTemporal evidence {index}.".encode(),
            published_at=now - timedelta(days=age),
        )
        for index, age in enumerate(ages_days)
    ]
    manifest = corpus.ingest_batch(
        f"fc_issue300_terminal_{uuid4().hex}",
        "scrape",
        requests,
        research_run_external_id=external_id,
    )
    assert manifest["failure_count"] == 0
    with runs.uow_factory() as uow, uow.connection.cursor() as cursor:
        cursor.execute(
            """SELECT c.id,d.published_at
                 FROM research_run_assets rra
                 JOIN documents d ON d.snapshot_id=rra.snapshot_id
                 JOIN chunks c ON c.document_id=d.id
                WHERE rra.run_id=%s
                ORDER BY d.published_at DESC,c.id""",
            (status.id,),
        )
        rows = cursor.fetchall()
    assert len(rows) >= len(ages_days)
    return runs, status, [UUID(str(row[0])) for row in rows[: len(ages_days)]]


def _seed_item_bound_packet(runs, status, chunks: list[UUID], spec: dict) -> None:
    spec_id = runs.record_research_spec(status.id, spec, revision=1)
    coverage = CompleteCoverageService(runs.uow_factory)
    items = coverage.create_items_from_spec(
        status.id, spec, execution_mode=status.execution_mode
    )
    freshness_items = {
        item.subject_id: item.coverage_item_id
        for item in items
        if item.item_type.value == "freshness_requirement"
    }
    requirements = spec["freshness_requirements"]
    assert len(requirements) == len(chunks)
    for requirement, passage_id in zip(requirements, chunks, strict=True):
        item_id = freshness_items[requirement["requirement_id"]]
        coverage.apply_freshness_observed(
            status.id,
            item_id,
            freshness_status="satisfied",
            idempotency_key=f"issue300:freshness:{item_id}",
        )
        coverage.apply_event(
            status.id,
            "item_status_changed",
            item_id=item_id,
            new_status="satisfied",
            payload={"passage_ids": [str(passage_id)], "remaining_gap": ""},
            idempotency_key=f"issue300:freshness-status:{item_id}",
        )

    claims = [uuid4() for _ in chunks]
    packet = {
        "schema_version": "evidence-packet-v1",
        "run_id": str(status.id),
        "claims": [
            {
                "claim_id": str(claim_id),
                "statement": f"temporal claim {index}",
                "semantic_status": "supported",
            }
            for index, claim_id in enumerate(claims)
        ],
        "passages": [
            {
                "passage_id": str(chunk_id),
                "chunk_id": str(chunk_id),
                "text": f"temporal passage {index}",
                "source_url": f"https://temporal.example/{index}",
            }
            for index, chunk_id in enumerate(chunks)
        ],
        "omitted_passages": [],
        "claim_evidence_bindings": [
            {
                "claim_id": str(claim_id),
                "passage_ids": [str(chunk_id)],
                "relationship": "supports",
            }
            for claim_id, chunk_id in zip(claims, chunks, strict=True)
        ],
    }
    with runs.uow_factory() as uow:
        uow.evidence_packets.persist_evidence_packet(
            status.id,
            UUID(str(spec_id)),
            coverage.get_current_revision(status.id),
            1,
            packet,
        )


def _bind_seeded_packet_research_spec(runs, status, spec: dict) -> UUID:
    """Bind a real spec to the fixture packet's original immutable spec UUID."""
    with runs.uow_factory() as uow, uow.connection.cursor() as cursor:
        cursor.execute(
            """SELECT research_spec_id,
                      (payload->'claim_evidence_bindings'->0->'passage_ids'->>0)::uuid
                 FROM evidence_packets WHERE run_id=%s
                 ORDER BY packet_revision DESC LIMIT 1""",
            (status.id,),
        )
        row = cursor.fetchone()
        assert row is not None
        research_spec_id = UUID(str(row[0]))
        passage_id = UUID(str(row[1]))
        payload = json.dumps(
            spec, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        digest = hashlib.sha256(payload.encode()).hexdigest()
        cursor.execute(
            """INSERT INTO research_specs(
                   id,run_id,spec_revision,schema_name,schema_version,payload,
                   content_sha256,validation_status,validation_errors,idempotency_key)
                 VALUES(%s,%s,1,'research_spec',1,%s::jsonb,%s,
                        'valid','[]'::jsonb,%s)""",
            (
                research_spec_id,
                status.id,
                payload,
                digest,
                f"issue300:bound-seeded-spec:{status.id}",
            ),
        )
        cursor.execute(
            "UPDATE research_runs SET research_spec_id=%s WHERE id=%s",
            (research_spec_id, status.id),
        )
    return passage_id


def _set_bound_passage_temporal_authority(
    runs,
    passage_id: UUID,
    *,
    age_days: int | None,
    update_age_days: int | None = None,
) -> None:
    """Apply the scenario to the exact immutable packet-bound passage."""
    now = datetime.now(timezone.utc)
    published_at = None if age_days is None else now - timedelta(days=age_days)
    last_modified = (
        None if update_age_days is None else now - timedelta(days=update_age_days)
    )
    with runs.uow_factory() as uow, uow.connection.cursor() as cursor:
        cursor.execute(
            """UPDATE documents d
                  SET published_at=%s
                 FROM chunks c
                WHERE c.id=%s AND c.document_id=d.id
                RETURNING d.snapshot_id""",
            (published_at, passage_id),
        )
        row = cursor.fetchone()
        assert row is not None
        cursor.execute(
            "UPDATE asset_snapshots SET last_modified=%s WHERE id=%s",
            (last_modified, row[0]),
        )


def _mark_single_freshness_satisfied(
    runs, status, spec: dict, passage_id: UUID
) -> None:
    coverage = CompleteCoverageService(runs.uow_factory)
    requirement_id = spec["freshness_requirements"][0]["requirement_id"]
    item = next(
        value
        for value in coverage.create_items_from_spec(
            status.id, spec, execution_mode=status.execution_mode
        )
        if value.subject_id == requirement_id
    )
    coverage.apply_freshness_observed(
        status.id,
        item.coverage_item_id,
        freshness_status="satisfied",
        idempotency_key=f"issue300:terminal-freshness:{status.id}",
    )
    coverage.apply_event(
        status.id,
        "item_status_changed",
        item_id=item.coverage_item_id,
        new_status="satisfied",
        payload={"passage_ids": [str(passage_id)], "remaining_gap": ""},
        idempotency_key=f"issue300:terminal-status:{status.id}",
    )


def _advance_to_validating(runs, status):
    current = runs.status(run_id=status.id)
    order = (
        "planning",
        "corpus_review",
        "acquiring",
        "extracting",
        "indexing",
        "coverage_review",
        "synthesizing",
        "validating",
    )
    start = order.index(current.state) + 1 if current.state in order else 0
    for next_state in order[start:]:
        runs.transition(
            current.id,
            next_state,
            expected_revision=current.lifecycle_revision,
            idempotency_key=f"issue300:advance:{current.id}:{next_state}",
            actor_type="integration-test",
            reason="prepare terminal temporal regression",
        )
        current = runs.status(run_id=current.id)
    return current


def _terminal_case(
    config: StoreConfig,
    *,
    age_days: int,
    update_age_days: int | None = None,
):
    runs, status, _chunks = _ingest_chunks(config, (age_days,))
    spec = _freshness_spec(status)
    seed_authoritative_completion_provenance(runs.uow_factory, status.id)
    passage_id = _bind_seeded_packet_research_spec(runs, status, spec)
    _set_bound_passage_temporal_authority(
        runs,
        passage_id,
        age_days=age_days,
        update_age_days=update_age_days,
    )
    _mark_single_freshness_satisfied(runs, status, spec, passage_id)
    current = _advance_to_validating(runs, status)
    with runs.uow_factory() as uow:
        completion = load_authoritative_completion_provenance(
            uow, status.id, for_update=False
        ).completion_fields()
    return runs, status, current, completion


def _time_window_spec(status, *, days: int = 5) -> dict:
    spec = _freshness_spec(status, count=0)
    now = datetime.now(timezone.utc)
    spec["time_window"] = {
        "start": (now - timedelta(days=days)).isoformat(),
        "end": (now + timedelta(days=1)).isoformat(),
        "description": f"Publication must be within the last {days} days.",
        "uncertainty": "",
    }
    spec["freshness_requirements"] = []
    return spec


def _ingest_undated_chunk(config: StoreConfig):
    runs = build_run_service(config)
    corpus = build_service(config)
    external_id = f"fr_issue300_undated_{uuid4().hex}"
    status = runs.create("issue 300 retrieval-only temporal guard", external_id)
    manifest = corpus.ingest_batch(
        f"fc_issue300_undated_{uuid4().hex}",
        "scrape",
        [
            IngestRequest(
                "https://temporal.example/retrieval-only",
                b"# Retrieval only\n\nNo publication or update date is supplied.",
            )
        ],
        research_run_external_id=external_id,
    )
    assert manifest["failure_count"] == 0
    with runs.uow_factory() as uow, uow.connection.cursor() as cursor:
        cursor.execute(
            """SELECT c.id,d.published_at,a.last_modified,a.retrieved_at
                 FROM research_run_assets rra
                 JOIN documents d ON d.snapshot_id=rra.snapshot_id
                 JOIN asset_snapshots a ON a.id=d.snapshot_id
                 JOIN chunks c ON c.document_id=d.id
                WHERE rra.run_id=%s ORDER BY c.id LIMIT 1""",
            (status.id,),
        )
        row = cursor.fetchone()
    assert row is not None
    assert row[1] is None
    assert row[2] is None
    assert row[3] is not None
    return runs, status, UUID(str(row[0]))


def _window_terminal_case(config: StoreConfig, *, age_days: int | None):
    if age_days is None:
        runs, status, _chunk = _ingest_undated_chunk(config)
    else:
        runs, status, _chunks = _ingest_chunks(config, (age_days,))
    spec = _time_window_spec(status)
    seed_authoritative_completion_provenance(runs.uow_factory, status.id)
    passage_id = _bind_seeded_packet_research_spec(runs, status, spec)
    _set_bound_passage_temporal_authority(runs, passage_id, age_days=age_days)
    current = _advance_to_validating(runs, status)
    with runs.uow_factory() as uow:
        completion = load_authoritative_completion_provenance(
            uow, status.id, for_update=False
        ).completion_fields()
    return runs, status, current, completion


def test_one_fresh_passage_cannot_globally_satisfy_another_freshness_item(
    temporal_guard_config: StoreConfig,
) -> None:
    runs, status, chunks = _ingest_chunks(temporal_guard_config, (1, 90))
    spec = _freshness_spec(status, count=2)
    _seed_item_bound_packet(runs, status, chunks, spec)

    with (
        runs.uow_factory() as uow,
        pytest.raises(
            TemporalEvidenceError,
            match="has no qualifying publication or explicit update",
        ),
    ):
        assert_temporal_evidence_satisfied(uow, status.id)


def test_no_research_spec_remains_additively_not_applicable(
    temporal_guard_config: StoreConfig,
) -> None:
    runs = build_run_service(temporal_guard_config)
    status = runs.create(
        "mechanical completion fixture without temporal spec",
        f"fr_issue300_no_spec_{uuid4().hex}",
    )
    with runs.uow_factory() as uow:
        result = assert_temporal_evidence_satisfied(uow, status.id)
    assert result["status"] == "not_applicable"
    assert result["required"] is False


def test_stale_evidence_packet_spec_identity_fails_closed(
    temporal_guard_config: StoreConfig,
) -> None:
    runs, status, chunks = _ingest_chunks(temporal_guard_config, (1,))
    spec = _freshness_spec(status)
    _seed_item_bound_packet(runs, status, chunks, spec)
    runs.record_research_spec(
        status.id,
        _freshness_spec(status),
        revision=2,
        idempotency_key=f"issue300:replacement-spec:{status.id}",
    )
    with (
        runs.uow_factory() as uow,
        pytest.raises(TemporalEvidenceError, match="different ResearchSpec"),
    ):
        assert_temporal_evidence_satisfied(uow, status.id)


def _assert_terminal_rejection_is_transactional(
    runs, status, current, completion, *, key: str
) -> None:
    with pytest.raises(TerminalDecisionError, match="temporal evidence obligations"):
        runs.complete(
            status.id,
            expected_revision=current.lifecycle_revision,
            idempotency_key=key,
            actor_type="integration-test",
            completion=completion,
        )
    after = runs.status(run_id=status.id)
    assert after.state == "validating"
    assert after.lifecycle_revision == current.lifecycle_revision


def test_guarded_completed_rejection_is_transactional_and_leaves_run_validating(
    temporal_guard_config: StoreConfig,
) -> None:
    runs, status, current, completion = _terminal_case(
        temporal_guard_config, age_days=90
    )
    _assert_terminal_rejection_is_transactional(
        runs,
        status,
        current,
        completion,
        key=f"issue300:terminal-complete:{status.id}",
    )
    with runs.uow_factory() as uow, uow.connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM terminal_decisions WHERE run_id=%s", (status.id,)
        )
        row = cursor.fetchone()
        assert row is not None
        assert row[0] == 0


@pytest.mark.parametrize(
    ("age_days", "update_age_days"),
    [(-1, None), (90, -1)],
)
def test_guarded_completed_rejects_future_publication_or_update(
    temporal_guard_config: StoreConfig,
    age_days: int,
    update_age_days: int | None,
) -> None:
    runs, status, current, completion = _terminal_case(
        temporal_guard_config,
        age_days=age_days,
        update_age_days=update_age_days,
    )
    _assert_terminal_rejection_is_transactional(
        runs,
        status,
        current,
        completion,
        key=f"issue300:future-temporal-reject:{status.id}",
    )


def test_guarded_completed_accepts_qualifying_item_bound_temporal_evidence(
    temporal_guard_config: StoreConfig,
) -> None:
    runs, status, current, completion = _terminal_case(
        temporal_guard_config, age_days=1
    )
    result = runs.complete(
        status.id,
        expected_revision=current.lifecycle_revision,
        idempotency_key=f"issue300:terminal-ok-complete:{status.id}",
        actor_type="integration-test",
        completion=completion,
    )
    assert result.next_state == "completed"
    assert runs.status(run_id=status.id).state == "completed"


@pytest.mark.parametrize("age_days", [90, None, -1])
def test_publication_window_rejects_out_of_window_retrieval_only_and_future_evidence(
    temporal_guard_config: StoreConfig, age_days: int | None
) -> None:
    runs, status, current, completion = _window_terminal_case(
        temporal_guard_config, age_days=age_days
    )
    _assert_terminal_rejection_is_transactional(
        runs,
        status,
        current,
        completion,
        key=f"issue300:window-reject:{status.id}",
    )


def test_publication_window_accepts_in_window_publication(
    temporal_guard_config: StoreConfig,
) -> None:
    runs, status, current, completion = _window_terminal_case(
        temporal_guard_config, age_days=1
    )
    result = runs.complete(
        status.id,
        expected_revision=current.lifecycle_revision,
        idempotency_key=f"issue300:window-complete:{status.id}",
        actor_type="integration-test",
        completion=completion,
    )
    assert result.next_state == "completed"
    assert runs.status(run_id=status.id).state == "completed"
