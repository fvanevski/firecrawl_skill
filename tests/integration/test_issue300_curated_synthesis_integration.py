"""Issue #300 AC4 PostgreSQL integration for the curated synthesis boundary."""

from __future__ import annotations

import json
from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from asset_promotion_test_support import TEST_DSN, _mark_run_index_complete

from firecrawl_skill.model_gateway import StructuredResult
from firecrawl_skill.research_store.acquisition.models import ScrapeTransportResult
from firecrawl_skill.research_store.composition import (
    build_curated_run_service,
    build_fscrape_service,
)
from firecrawl_skill.research_store.curated_synthesis_service import CuratedSynthesisError
from firecrawl_skill.research_store.fscrape_contract import FScrapeRequest
from firecrawl_skill.research_store.postgres import connect

pytest_plugins = ("asset_promotion_test_support",)


class _OneAssetAdapter:
    def scrape(self, url: str, **_kwargs) -> ScrapeTransportResult:
        return ScrapeTransportResult(
            raw_payload=(
                b"# Authoritative finding\n\n"
                b"The persisted source explicitly states the issue 300 integration fact."
            ),
            http_status=200,
            final_url=url,
            title="Issue 300 authoritative finding",
            provider_request_id=f"issue300-{uuid4().hex}",
            metadata={"test": True},
        )


class _SchemaValidHostSupplier:
    """Return bounded host-authored artifacts for each production semantic stage."""

    def supply(self, *, semantic_context, schema, **call_kwargs):
        del schema
        stage = str(semantic_context["stage"])
        data = json.loads(call_kwargs["user_prompt"])
        if stage == "claim_extraction":
            payload = {
                "claims": [
                    {
                        "coverage_item_id": item["coverage_item_id"],
                        "source_passage_id": item["source_passage_id"],
                        "statement": (
                            "The authoritative source explicitly states the "
                            "issue 300 integration fact."
                        ),
                        "authority_class": "unclassified",
                        "freshness_status": "not_applicable",
                    }
                    for item in data["coverage_items"]
                ]
            }
        elif stage == "claim_binding":
            fallback = data["passages"][0]["passage_id"]
            payload = {
                "evaluations": [
                    {
                        "claim_id": claim["claim_id"],
                        "semantic_status": "supported",
                        "bindings": [
                            {
                                "passage_ids": claim.get("required_passage_ids")
                                or [fallback],
                                "relationship": "supports",
                                "confidence": 1.0,
                                "uncertainty": "",
                            }
                        ],
                    }
                    for claim in data["claims"]
                ]
            }
        elif stage == "outline":
            binding_by_claim = {
                item["claim_id"]: item for item in data.get("bindings", [])
            }
            payload = {
                "schema_version": "synthesis-outline-v1",
                "run_id": data["run_id"],
                "evidence_packet_revision": data["evidence_packet_revision"],
                "outline_sections": [
                    {
                        "section_id": "findings",
                        "title": "Findings",
                        "claims": [
                            {
                                "claim_id": claim["claim_id"],
                                "statement": claim["statement"],
                                "section_role": "primary",
                                "required_passage_ids": binding_by_claim[
                                    claim["claim_id"]
                                ]["passage_ids"],
                            }
                            for claim in data["claims"]
                        ],
                    }
                ],
                "unsupported_claims": [],
            }
        elif stage == "draft":
            binding_by_claim = {
                item["claim_id"]: item for item in data.get("bindings", [])
            }
            payload = {
                "schema_version": "synthesis-draft-v1",
                "run_id": data["run_id"],
                "evidence_packet_revision": data["evidence_packet_revision"],
                "report_sections": [
                    {
                        "section_id": "findings",
                        "title": "Findings",
                        "body": "The authoritative source states the integration fact.",
                        "claim_references": [
                            {
                                "claim_id": claim["claim_id"],
                                "passage_ids": binding_by_claim[claim["claim_id"]][
                                    "passage_ids"
                                ],
                                "relationship": binding_by_claim[claim["claim_id"]][
                                    "relationship"
                                ],
                            }
                            for claim in data["claims"]
                        ],
                    }
                ],
                "unsupported_claims": [],
                "limitations": [],
            }
        elif stage == "citation_pass":
            validation_results = [
                {
                    "section_id": section["section_id"],
                    "claim_id": reference["claim_id"],
                    "passage_ids": reference["passage_ids"],
                    "status": "valid",
                    "issue": "",
                }
                for section in data["draft_sections"]
                for reference in section.get("claim_references", [])
            ]
            payload = {
                "schema_version": "synthesis-citation-pass-v1",
                "run_id": data["run_id"],
                "evidence_packet_revision": data["evidence_packet_revision"],
                "draft_revision": 1,
                "pass_status": "passed",
                "validation_results": validation_results,
                "invented_citations": [],
                "unsupported_claims": [],
                "entailment_mismatches": [],
            }
        else:  # validation is deterministic and never invokes the supplier.
            raise AssertionError(f"unexpected host semantic stage: {stage}")
        return StructuredResult(
            payload,
            {"authority": "integration-host", "usage": {}},
            (),
        )


def _research_spec(*, execution_mode: str) -> dict:
    spec_id = uuid4()
    return {
        "schema_version": "research-spec-v1",
        "research_spec_id": str(spec_id),
        "objective": "Verify the issue 300 curated synthesis path",
        "research_archetype": "fact_finding",
        "risk_level": "low",
        "execution_mode": execution_mode,
        "questions": [
            {
                "question_id": str(uuid4()),
                "text": "What fact does the authoritative source state?",
            }
        ],
        "claims_to_validate": [],
        "entities": [],
        "jurisdictions": [],
        "time_window": {
            "start": None,
            "end": None,
            "description": "No explicit temporal bound for this AC4 test.",
            "uncertainty": "",
        },
        "freshness_requirements": [],
        "required_source_classes": [],
        "corroboration_requirements": [],
        "contradiction_requirements": [],
        "excluded_interpretations": [],
        "structured_data_requirements": [],
        "completion_criteria": [
            {
                "criterion_id": str(uuid4()),
                "description": "Produce one source-grounded answer.",
                "mandatory": True,
            }
        ],
        "user_constraints": [],
        "ambiguities": [],
        "assumptions": [],
    }


def _prepare_sealed_coverage_review(config, *, execution_mode: str):
    curated = build_curated_run_service(config)
    external_id = f"fr_{uuid4().hex}"
    started = curated.start(
        "Issue 300 curated synthesis integration",
        external_id,
        run_mode="curated",
        execution_mode=execution_mode,
    )
    curated.prepare(external_id)
    scrape = build_fscrape_service(
        config, adapter_factory=lambda: _OneAssetAdapter()
    ).execute(
        FScrapeRequest(
            urls=(f"https://example.test/issue300/{uuid4().hex}",),
            research_run_id=external_id,
            external_invocation_id=f"fc_{uuid4().hex}",
        )
    )
    assert scrape.status == "complete"
    assets = curated.assets(external_id)["assets"]
    assert len(assets) == 1
    subject_id = UUID(str(assets[0]["id"]))
    curated.retain(external_id, subject_id)
    curated.seal_acquisition(external_id)
    _mark_run_index_complete(started.run.id)
    current = curated.run_service.status(run_id=started.run.id)
    assert current.state == "indexing"
    curated.run_service.transition(
        started.run.id,
        "coverage_review",
        expected_revision=current.lifecycle_revision,
        idempotency_key=f"issue300:index-complete:{external_id}",
        actor_type="integration-test",
        reason="all test index jobs are complete",
    )
    spec = _research_spec(execution_mode=execution_mode)
    curated.run_service.record_research_spec(
        started.run.id,
        spec,
        revision=1,
        idempotency_key=f"issue300:spec:{external_id}",
    )
    return curated, started, external_id


def test_curated_operator_path_builds_evidence_five_stages_then_finishes(
    promotion_config,
) -> None:
    supplier = _SchemaValidHostSupplier()
    config = replace(
        promotion_config,
        host_artifact_supplier=supplier,
        generative_model="host-model-placeholder",
    )
    curated, started, external_id = _prepare_sealed_coverage_review(
        config, execution_mode="agent_led"
    )

    result = curated.synthesize(external_id)
    assert result["state"] == "validating"
    assert result["finished"] is False
    assert result["evidence"]["mode"] == "prepared"
    assert result["synthesis"]["overall_status"] == "completed"

    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM evidence_packets WHERE run_id=%s",
            (started.run.id,),
        )
        assert cursor.fetchone()[0] >= 1
        cursor.execute(
            "SELECT count(*) FROM research_claims WHERE run_id=%s",
            (started.run.id,),
        )
        assert cursor.fetchone()[0] >= 1
        cursor.execute(
            "SELECT count(*) FROM claim_evidence_links WHERE run_id=%s",
            (started.run.id,),
        )
        assert cursor.fetchone()[0] >= 1
        cursor.execute(
            """SELECT stage_name,stage_status,semantic_call_id,semantic_artifact_id
                 FROM synthesis_stages WHERE run_id=%s ORDER BY stage_name""",
            (started.run.id,),
        )
        stages = cursor.fetchall()
        assert {row[0] for row in stages} == {
            "outline",
            "binding",
            "draft",
            "citation_pass",
            "validation",
        }
        assert all(row[1] == "completed" for row in stages)
        semantic_by_stage = {row[0]: (row[2], row[3]) for row in stages}
        assert all(semantic_by_stage[name][0] for name in ("draft", "citation_pass"))
        assert all(semantic_by_stage[name][1] for name in ("draft", "citation_pass"))

    finished = curated.finish(external_id, outcome="satisfied")
    assert finished.run.state == "completed"


def test_unavailable_local_endpoint_has_no_evidence_or_semantic_side_effects(
    promotion_config,
    monkeypatch,
) -> None:
    config = replace(
        promotion_config,
        host_artifact_supplier=None,
        generative_url="http://127.0.0.1:8002/v1",
        generative_model="chat",
    )
    curated, started, external_id = _prepare_sealed_coverage_review(
        config, execution_mode="autonomous_local"
    )
    monkeypatch.setattr(
        "firecrawl_skill.research_store.curated_synthesis_service.model_gateway.probe_local",
        lambda *_args, **_kwargs: {"status": "unavailable", "error": "offline"},
    )

    with pytest.raises(CuratedSynthesisError, match="unavailable before semantic work"):
        curated.synthesize(external_id)

    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        for table in ("evidence_packets", "semantic_calls", "synthesis_stages"):
            cursor.execute(f"SELECT count(*) FROM {table} WHERE run_id=%s", (started.run.id,))
            assert cursor.fetchone()[0] == 0
    assert curated.run_service.status(run_id=started.run.id).state == "coverage_review"
