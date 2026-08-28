"""PostgreSQL-backed issue #314 host-handoff completion authority."""

from __future__ import annotations

import json
import os
import re
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from firecrawl_skill.research_store.acquisition.adapters.bounded_firecrawl import (
    BoundedFirecrawlSearchAdapter,
)
from firecrawl_skill.research_store.blob import ContentAddressedBlobStore
from firecrawl_skill.research_store.composition import (
    build_evidence_service,
    build_invocation_service,
    build_production_resumable_orchestrator,
    build_run_service,
    build_semantic_service,
    build_uow_factory,
)
from firecrawl_skill.research_store.config import StoreConfig
from firecrawl_skill.research_store.corpus_service import CorpusService
from firecrawl_skill.research_store.coverage_seed_service import CompleteCoverageService
from firecrawl_skill.research_store.domain import IngestRequest
from firecrawl_skill.research_store.parsing import get_registry
from firecrawl_skill.research_store.postgres import (
    connect,
    migrate,
    require_disposable_database_reset,
)
from firecrawl_skill.research_store.research_controller import ResearchWorkflowController
from firecrawl_skill.research_store.research_controller_contract import (
    DISPOSITION_COMPLETED,
)

TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL") or ""
pytestmark = pytest.mark.skipif(
    not TEST_DSN,
    reason="requires repository-sanctioned disposable PostgreSQL",
)
OBJECTIVE = "issue314 retained host handoff completion authority"


@pytest.fixture(scope="module", autouse=True)
def prepared_database() -> None:
    require_disposable_database_reset(
        TEST_DSN,
        os.environ.get("RESEARCH_STORE_TEST_ALLOW_RESET", ""),
    )
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute("DROP SCHEMA public CASCADE")
        cursor.execute("CREATE SCHEMA public")
    migrate(TEST_DSN)


@pytest.fixture
def controller(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[ResearchWorkflowController, CorpusService, list[str]]:
    monkeypatch.setenv("FIRECRAWL_RELEASE_DETERMINISTIC_FIXTURES", "1")
    config = replace(
        StoreConfig.from_env(),
        database_url=TEST_DSN,
        blob_root=tmp_path / "blobs",
        embedding_model=f"issue314-{uuid4().hex[:8]}",
        embedding_revision="test",
        embedding_dimension=4,
    )
    run_service = build_run_service(config)
    corpus = CorpusService(
        config,
        build_uow_factory(config),
        ContentAddressedBlobStore(config.blob_root),
        parser_registry=get_registry(),
    )
    provider_calls: list[str] = []

    def forbidden_provider_search(
        self: Any,
        query_text: str,
        **_kwargs: Any,
    ) -> Any:
        provider_calls.append(query_text)
        raise AssertionError("retained host handoff invoked Firecrawl provider")

    monkeypatch.setattr(
        BoundedFirecrawlSearchAdapter,
        "search",
        forbidden_provider_search,
    )

    workflow = ResearchWorkflowController(
        config=config,
        run_service=run_service,
        invocation_service=build_invocation_service(config),
        corpus_service=corpus,
        coverage_service=CompleteCoverageService(run_service.uow_factory),
        evidence_service=build_evidence_service(config),
        semantic_service=build_semantic_service(config),
        orchestrator_factory=lambda orchestrator_config: (
            build_production_resumable_orchestrator(
                config,
                orchestrator_config=orchestrator_config,
            )
        ),
        controller_config=None,
    )
    return workflow, corpus, provider_calls


def _seed_retained(corpus: CorpusService) -> None:
    corpus.ingest(
        IngestRequest(
            requested_url="https://issue314.example/retained",
            content=(
                b"Issue314 retained host handoff completion authority is established "
                b"by this durable retained corpus evidence."
            ),
            title="Issue314 retained host handoff authority",
        )
    )


def test_host_handoff_completion_binds_packet_snapshot_and_public_provenance(
    controller: tuple[ResearchWorkflowController, CorpusService, list[str]],
) -> None:
    workflow, corpus, provider_calls = controller
    _seed_retained(corpus)

    result = workflow.run(OBJECTIVE, execution_mode="deterministic_debug")

    assert result.disposition == DISPOSITION_COMPLETED
    assert result.result_ready is True
    assert result.handoff_ready is True
    assert provider_calls == []

    public = result.to_dict()
    assert public["delivery_mode"] == "host_handoff"
    handoff = public["handoff"]
    assert isinstance(handoff, dict)
    authority = handoff["authority"]
    assert authority["completion_schema_version"] == "completion-provenance-v2"
    handoff_authority_sha256 = authority["handoff_authority_sha256"]
    assert re.fullmatch(r"[0-9a-f]{64}", handoff_authority_sha256)

    serialized_handoff = json.dumps(handoff, sort_keys=True)
    for forbidden in (
        "research_spec_id",
        "coverage_item_id",
        "claim_id",
        "passage_id",
        "evidence_packet_id",
        "membership_seal_id",
        "source_membership_sha256",
    ):
        assert forbidden not in serialized_handoff

    status = workflow.run_service.status(external_id=result.run_id)
    with workflow.run_service.uow_factory() as uow:
        packet_record = uow.evidence_packets.get_evidence_packet(status.id)
        assert packet_record is not None
        packet_payload = packet_record.to_dict()["payload"]
        packet_coverage_revision = int(packet_payload["coverage_revision"])
        packet_snapshot = uow.coverage.get_snapshot(
            status.id,
            packet_coverage_revision,
        )
    assert packet_snapshot is not None
    assert packet_snapshot["coverage_revision"] == packet_coverage_revision
    assert packet_snapshot["ledger"]["run_id"] == str(status.id)
    assert handoff["coverage"]["coverage_revision"] == packet_coverage_revision

    with workflow.run_service.uow_factory() as uow, uow.connection.cursor() as cur:
        cur.execute(
            """SELECT count(*) FROM synthesis_stages
               WHERE run_id=%s AND stage_name IN ('draft','citation_pass')""",
            (status.id,),
        )
        synthesis_count = cur.fetchone()[0]
        cur.execute(
            """SELECT source_manifest_sha256,answer_sha256
               FROM research_runs WHERE id=%s""",
            (status.id,),
        )
        run_hashes = cur.fetchone()
        cur.execute(
            """SELECT validation_result FROM research_run_transitions
               WHERE run_id=%s AND next_state='completed'""",
            (status.id,),
        )
        completion_row = cur.fetchone()

    assert synthesis_count == 0
    assert run_hashes is not None
    assert run_hashes[1] == handoff_authority_sha256
    assert completion_row is not None
    completion = completion_row[0]["completion"]["completion_provenance"]
    assert completion["schema_version"] == "completion-provenance-v2"
    assert completion["delivery_mode"] == "host_handoff"
    assert completion["handoff_authority_sha256"] == handoff_authority_sha256
