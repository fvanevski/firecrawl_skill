"""Test support for issue #218 authoritative completion provenance."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from uuid import UUID, uuid4

from research_store.asset_promotion_models import _canonical_sha256, _member_payload


@dataclass(frozen=True)
class SeededCompletionProvenance:
    source_manifest_sha256: str
    answer_sha256: str
    evidence_packet_revision: int
    draft_artifact_id: UUID
    citation_artifact_id: UUID


def _json_sha256(value):
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _validation_report_sha256(value):
    payload = json.dumps(value, sort_keys=True, ensure_ascii=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _authority_for_mode(mode: str) -> tuple[str, str, str]:
    if mode == "autonomous_local":
        return "local", "local-model", "test-local-model"
    if mode == "agent_led":
        return "host-agent", "host-agent", ""
    if mode == "deterministic_debug":
        return "deterministic-fixture", "deterministic-fixture", ""
    raise AssertionError(f"unsupported test execution mode: {mode}")


def _ensure_membership(uow, run_id: UUID):
    with uow.connection.cursor() as cursor:
        cursor.execute(
            """SELECT id,membership_sha256
                 FROM run_asset_membership_seals
                WHERE run_id=%s AND status='sealed'
                ORDER BY seal_revision DESC LIMIT 1""",
            (run_id,),
        )
        existing = cursor.fetchone()
        if existing is not None:
            seal_id = UUID(str(existing[0]))
            cursor.execute(
                """SELECT subject_id,snapshot_id,role,chunk_ids
                     FROM run_asset_membership_members
                    WHERE seal_id=%s AND run_id=%s
                    ORDER BY ordinal""",
                (seal_id, run_id),
            )
            members = [
                {
                    "subject_id": UUID(str(row[0])),
                    "snapshot_id": UUID(str(row[1])),
                    "role": str(row[2]),
                    "chunk_ids": tuple(UUID(str(value)) for value in row[3]),
                }
                for row in cursor.fetchall()
            ]
            assert members
            return {
                "seal_id": seal_id,
                "membership_sha256": str(existing[1]),
                "members": members,
                "chunk_ids": tuple(
                    sorted(
                        {chunk for member in members for chunk in member["chunk_ids"]},
                        key=str,
                    )
                ),
                "snapshot_ids": tuple(
                    sorted({member["snapshot_id"] for member in members}, key=str)
                ),
            }

        cursor.execute(
            """SELECT a.snapshot_id
                 FROM research_run_assets a
                WHERE a.run_id=%s
                ORDER BY a.snapshot_id
                LIMIT 1""",
            (run_id,),
        )
        row = cursor.fetchone()
        assert row is not None, "test run must have a persisted run asset"
        snapshot_id = UUID(str(row[0]))
        cursor.execute(
            """SELECT c.id
                 FROM chunks c
                 JOIN documents d ON d.id=c.document_id
                WHERE d.snapshot_id=%s
                ORDER BY c.ordinal,c.id""",
            (snapshot_id,),
        )
        chunk_ids = tuple(UUID(str(row[0])) for row in cursor.fetchall())
        assert chunk_ids, "test run asset must have persisted chunks"
        cursor.execute(
            "SELECT lifecycle_revision FROM research_runs WHERE id=%s", (run_id,)
        )
        lifecycle_revision = int(cursor.fetchone()[0])

        subject_id = uuid4()
        role = "completion_evidence"
        cursor.execute(
            """INSERT INTO run_asset_promotion_subjects(
                   id,run_id,snapshot_id,role,current_stage,stage_revision,
                   provenance,actor_type,policy_version,lifecycle_revision,
                   reason_code,reason)
                 VALUES(%s,%s,%s,%s,'completion_critical',0,'direct_retention',
                        'integration-test','completion-membership-v1',%s,
                        'issue_218_test_seed',
                        'authoritative completion test fixture')""",
            (subject_id, run_id, snapshot_id, role, lifecycle_revision),
        )
        member_payload = _member_payload(subject_id, snapshot_id, role, chunk_ids)
        member_hash = _canonical_sha256(member_payload)
        membership_hash = _canonical_sha256([member_payload])
        cursor.execute(
            "SELECT COALESCE(MAX(seal_revision),0)+1 "
            "FROM run_asset_membership_seals WHERE run_id=%s",
            (run_id,),
        )
        seal_revision = int(cursor.fetchone()[0])
        seal_id = uuid4()
        cursor.execute(
            """INSERT INTO run_asset_membership_seals(
                   id,run_id,seal_revision,lifecycle_revision,status,
                   membership_sha256,expected_asset_count,expected_chunk_count,
                   actor_type,policy_version,reason_code,reason)
                 VALUES(%s,%s,%s,%s,'sealed',%s,1,%s,
                        'integration-test','completion-membership-v1',
                        'issue_218_test_seed',
                        'authoritative completion test fixture')""",
            (
                seal_id,
                run_id,
                seal_revision,
                lifecycle_revision,
                membership_hash,
                len(chunk_ids),
            ),
        )
        cursor.execute(
            """INSERT INTO run_asset_membership_members(
                   seal_id,run_id,subject_id,snapshot_id,role,ordinal,
                   chunk_ids,chunk_count,member_sha256)
                 VALUES(%s,%s,%s,%s,%s,0,%s,%s,%s)""",
            (
                seal_id,
                run_id,
                subject_id,
                snapshot_id,
                role,
                list(chunk_ids),
                len(chunk_ids),
                member_hash,
            ),
        )
        return {
            "seal_id": seal_id,
            "membership_sha256": membership_hash,
            "members": [
                {
                    "subject_id": subject_id,
                    "snapshot_id": snapshot_id,
                    "role": role,
                    "chunk_ids": chunk_ids,
                }
            ],
            "chunk_ids": chunk_ids,
            "snapshot_ids": (snapshot_id,),
        }


def seed_authoritative_completion_provenance(
    uow_factory, run_id: UUID
) -> SeededCompletionProvenance:
    """Persist one exact, reproducible PostgreSQL authority chain for a run."""
    with uow_factory() as uow:
        membership = _ensure_membership(uow, run_id)
        chunk_id = membership["chunk_ids"][0]
        with uow.connection.cursor() as cursor:
            cursor.execute(
                "SELECT execution_mode FROM research_runs WHERE id=%s", (run_id,)
            )
            execution_mode = cursor.fetchone()[0]
            cursor.execute(
                """SELECT c.id,d.snapshot_id,c.text,s.canonical_url
                     FROM chunks c
                     JOIN documents d ON d.id=c.document_id
                     JOIN asset_snapshots a ON a.id=d.snapshot_id
                     JOIN sources s ON s.id=a.source_id
                    WHERE c.id=ANY(%s)
                    ORDER BY c.id""",
                (list(membership["chunk_ids"]),),
            )
            passage_rows = cursor.fetchall()
            assert len(passage_rows) == len(membership["chunk_ids"])
            passage_by_id = {UUID(str(row[0])): row for row in passage_rows}
            _, snapshot_id_raw, passage_text, source_url = passage_by_id[chunk_id]
            snapshot_id = UUID(str(snapshot_id_raw))
            provider, authority, model = _authority_for_mode(str(execution_mode))

            cursor.execute(
                "SELECT COALESCE(MAX(packet_revision),0)+1 "
                "FROM evidence_packets WHERE run_id=%s",
                (run_id,),
            )
            packet_revision = int(cursor.fetchone()[0])
            claim_id = uuid4()
            statement = (
                "Persisted PostgreSQL evidence supports authoritative completion."
            )
            cursor.execute(
                """INSERT INTO research_claims(
                       id,run_id,claim_id,statement,semantic_status,
                       evidence_packet_revision)
                     VALUES(%s,%s,%s,%s,'supported',%s)""",
                (uuid4(), run_id, claim_id, statement, packet_revision),
            )
            cursor.execute(
                """INSERT INTO claim_evidence_links(
                       id,run_id,claim_id,passage_id,snapshot_id,relationship,
                       confidence,source_url)
                     VALUES(%s,%s,%s,%s,%s,'supports',1.0,%s)""",
                (uuid4(), run_id, claim_id, chunk_id, snapshot_id, source_url),
            )
            packet = {
                "schema_version": "evidence-packet-v1",
                "run_id": str(run_id),
                "claims": [
                    {
                        "claim_id": str(claim_id),
                        "statement": statement,
                        "semantic_status": "supported",
                        "uncertainty": None,
                    }
                ],
                "passages": [
                    {
                        "passage_id": str(UUID(str(row[0]))),
                        "snapshot_id": str(UUID(str(row[1]))),
                        "text": row[2],
                        "source_url": row[3],
                    }
                    for row in passage_rows
                ],
                "omitted_passages": [],
                "claim_evidence_bindings": [
                    {
                        "claim_id": str(claim_id),
                        "passage_ids": [str(chunk_id)],
                        "relationship": "supports",
                        "confidence": 1.0,
                    }
                ],
                "limitations": [],
            }
            packet_id = uuid4()
            cursor.execute(
                """INSERT INTO evidence_packets(
                       id,run_id,research_spec_id,coverage_revision,
                       packet_revision,payload)
                     VALUES(%s,%s,%s,0,%s,%s::jsonb)""",
                (packet_id, run_id, uuid4(), packet_revision, json.dumps(packet)),
            )

            packet_ref = f"packet-{run_id}-r{packet_revision}"
            prompt_version = "synthesis-v1"
            draft_payload = {
                "schema_version": "synthesis-draft-v1",
                "run_id": str(run_id),
                "evidence_packet_revision": packet_revision,
                "report_sections": [
                    {
                        "section_id": "findings",
                        "title": "Findings",
                        "body": statement,
                        "claim_references": [
                            {
                                "claim_id": str(claim_id),
                                "passage_ids": [str(chunk_id)],
                                "relationship": "supports",
                            }
                        ],
                    }
                ],
                "unsupported_claims": [],
                "limitations": [],
            }
            citation_payload = {
                "schema_version": "synthesis-citation-pass-v1",
                "run_id": str(run_id),
                "evidence_packet_revision": packet_revision,
                "draft_revision": 1,
                "pass_status": "passed",
                "validation_results": [
                    {
                        "section_id": "findings",
                        "claim_id": str(claim_id),
                        "passage_ids": [str(chunk_id)],
                        "status": "valid",
                        "issue": "",
                    }
                ],
                "invented_citations": [],
                "unsupported_claims": [],
                "entailment_mismatches": [],
            }

            semantic = {}
            for stage_name, schema_name, payload in (
                ("draft", "synthesis-draft-v1", draft_payload),
                ("citation_pass", "synthesis-citation-pass-v1", citation_payload),
            ):
                request = {
                    "authority": authority,
                    "schema_name": schema_name,
                    "schema_version": 1,
                    "input_artifact_ids": [packet_ref],
                    "policy_version": "issue-218-test-v1",
                }
                call_id = uuid4()
                cursor.execute(
                    """INSERT INTO semantic_calls(
                           id,run_id,stage,provider,model,model_revision,
                           prompt_version,input_sha256,request,response_metadata,
                           status,idempotency_key,started_at,completed_at)
                         VALUES(%s,%s,%s,%s,%s,'',%s,%s,%s::jsonb,'{}'::jsonb,
                                'complete',%s,now(),now())""",
                    (
                        call_id,
                        run_id,
                        stage_name,
                        provider,
                        model,
                        prompt_version,
                        _json_sha256(request),
                        json.dumps(request),
                        f"issue-218:{run_id}:{packet_revision}:{stage_name}:call",
                    ),
                )
                artifact_id = uuid4()
                content_hash = _json_sha256(payload)
                cursor.execute(
                    """INSERT INTO semantic_artifacts(
                           id,run_id,semantic_call_id,artifact_type,schema_name,
                           schema_version,payload,content_sha256,validation_status,
                           validation_errors,idempotency_key)
                         VALUES(%s,%s,%s,%s,%s,1,%s::jsonb,%s,'valid',
                                '[]'::jsonb,%s)""",
                    (
                        artifact_id,
                        run_id,
                        call_id,
                        stage_name,
                        schema_name,
                        json.dumps(payload),
                        content_hash,
                        f"issue-218:{run_id}:{packet_revision}:{stage_name}:artifact",
                    ),
                )
                semantic[stage_name] = (call_id, artifact_id, payload, content_hash)

            report_hash = _validation_report_sha256(citation_payload)
            validation_artifact = {
                "report_hash": report_hash,
                "current_packet_revision": packet_revision,
                "stale_packet": False,
                "validation_status": "valid",
                "is_complete": True,
                "claim_manifest": [
                    {
                        "claim_id": str(claim_id),
                        "statement": statement,
                        "resolution": "supported",
                        "cited_passage_ids": [str(chunk_id)],
                        "binding_relationship": "supports",
                        "issues": [],
                    }
                ],
                "validation_errors_count": 0,
                "validation_warnings_count": 0,
                "summary": "All claims supported.",
            }
            stage_values = {
                "outline": (None, None, {"outline_sections": []}),
                "binding": (None, None, {"new_packet_revision": packet_revision}),
                "draft": semantic["draft"][:3],
                "citation_pass": semantic["citation_pass"][:3],
                "validation": (None, None, validation_artifact),
            }
            for stage_name, (call_id, artifact_id, artifact) in stage_values.items():
                cursor.execute(
                    """INSERT INTO synthesis_stages(
                           id,run_id,stage_name,stage_status,semantic_call_id,
                           semantic_artifact_id,evidence_packet_revision,model_name,
                           prompt_version,schema_version,artifact,error,attempts,
                           created_at,updated_at)
                         VALUES(%s,%s,%s,'completed',%s,%s,%s,%s,%s,1,
                                %s::jsonb,NULL,1,now(),now())
                         ON CONFLICT(run_id,stage_name) DO UPDATE SET
                           stage_status=EXCLUDED.stage_status,
                           semantic_call_id=EXCLUDED.semantic_call_id,
                           semantic_artifact_id=EXCLUDED.semantic_artifact_id,
                           evidence_packet_revision=EXCLUDED.evidence_packet_revision,
                           model_name=EXCLUDED.model_name,
                           prompt_version=EXCLUDED.prompt_version,
                           schema_version=EXCLUDED.schema_version,
                           artifact=EXCLUDED.artifact,error=NULL,updated_at=now()""",
                    (
                        uuid4(),
                        run_id,
                        stage_name,
                        call_id,
                        artifact_id,
                        packet_revision,
                        model,
                        prompt_version,
                        json.dumps(artifact),
                    ),
                )

    return SeededCompletionProvenance(
        source_manifest_sha256=membership["membership_sha256"],
        answer_sha256=semantic["draft"][3],
        evidence_packet_revision=packet_revision,
        draft_artifact_id=semantic["draft"][1],
        citation_artifact_id=semantic["citation_pass"][1],
    )
