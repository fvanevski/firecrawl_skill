"""Opt-in PostgreSQL + Qdrant integration coverage for issue #222.

The authoritative integration suite already supplies the disposable database
fixture. These tests import only that fixture so they share the same guarded
schema reset without re-running destructive setup.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import pytest

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from research_store.asset_promotion_models import _canonical_sha256, _member_payload
from research_store.cli import _index_build
from research_store.config import StoreConfig
from research_store.index_checkpoint_models import _membership_digest
from research_store.postgres import connect
from research_store.qdrant import PAYLOAD_INDEX_SCHEMAS, QdrantIndex
from research_store.reconciliation import reconcile_run
from test_research_store_integration import prepared_database  # noqa: F401

TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL")
pytestmark = [
    pytest.mark.skipif(
        not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
    ),
    pytest.mark.usefixtures("prepared_database"),
]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _seed_reconciliation_run(tmp_path: Path, *, count: int = 3):
    token = uuid4().hex
    alias = f"reconcile_{token[:12]}"
    config = replace(
        StoreConfig.from_env(),
        database_url=TEST_DSN,
        blob_root=tmp_path / "blobs",
        qdrant_alias=alias,
        embedding_model=f"reconcile-{token}",
        embedding_revision="issue-222",
        embedding_dimension=4,
        parser_version="markdown-v1",
        normalization_version="cleanup-v1",
        chunker_name="structural",
        chunker_version="structural-v1",
    )
    external_run_id = f"fr_reconcile_{token}"

    with connect(TEST_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO research_runs(
                   objective,external_run_id,state,execution_mode,lifecycle_revision)
                 VALUES(%s,%s,'indexing','autonomous_local',0) RETURNING id""",
            ("Issue 222 reconciliation integration", external_run_id),
        )
        run_id = UUID(str(cur.fetchone()[0]))
        url = f"https://reconcile-{token}.example/test"
        cur.execute(
            """INSERT INTO sources(canonical_url,registered_domain)
               VALUES(%s,%s) RETURNING id""",
            (url, f"reconcile-{token}.example"),
        )
        source_id = UUID(str(cur.fetchone()[0]))
        cur.execute(
            """INSERT INTO asset_snapshots(
                   source_id,requested_url,retrieved_at,content_sha256)
                 VALUES(%s,%s,now(),%s) RETURNING id""",
            (source_id, url, _sha(f"snapshot-{token}")),
        )
        snapshot_id = UUID(str(cur.fetchone()[0]))
        cur.execute(
            """INSERT INTO documents(
                   snapshot_id,normalized_text,parser_name,parser_version,
                   normalization_version,document_sha256,published_at)
                 VALUES(%s,%s,'markdown',%s,%s,%s,now()) RETURNING id""",
            (
                snapshot_id,
                f"reconciliation document {token}",
                config.parser_version,
                config.normalization_version,
                _sha(f"document-{token}"),
            ),
        )
        document_id = UUID(str(cur.fetchone()[0]))
        rows = [
            (
                document_id,
                ordinal,
                f"chunk {ordinal} {token}",
                _sha(f"chunk-{token}-{ordinal}"),
                config.chunker_name,
                config.chunker_version,
            )
            for ordinal in range(count)
        ]
        cur.executemany(
            """INSERT INTO chunks(
                   document_id,ordinal,text,content_sha256,chunker_name,chunker_version)
                 VALUES(%s,%s,%s,%s,%s,%s)""",
            rows,
        )
        cur.execute(
            "SELECT id FROM chunks WHERE document_id=%s ORDER BY id", (document_id,)
        )
        chunk_ids = tuple(UUID(str(row[0])) for row in cur.fetchall())
        cur.execute(
            """INSERT INTO research_run_assets(run_id,snapshot_id,role,metadata)
                 VALUES(%s,%s,'acquired','{}'::jsonb)""",
            (run_id, snapshot_id),
        )
        # The research_run_assets trigger establishes the authoritative
        # direct-retention subject. Reuse it rather than bypassing the promotion
        # contract with a second subject row.
        cur.execute(
            """SELECT id FROM run_asset_promotion_subjects
                 WHERE run_id=%s AND snapshot_id=%s AND role='acquired'""",
            (run_id, snapshot_id),
        )
        subject_id = UUID(str(cur.fetchone()[0]))
        cur.execute(
            """UPDATE run_asset_promotion_subjects
                   SET current_stage='evidence_eligible',reason_code='test_evidence'
                 WHERE id=%s""",
            (subject_id,),
        )
        cur.execute(
            """UPDATE run_asset_promotion_subjects
                   SET current_stage='completion_critical',reason_code='test_completion'
                 WHERE id=%s""",
            (subject_id,),
        )
        member_payload = _member_payload(
            subject_id,
            snapshot_id,
            "acquired",
            tuple(sorted(chunk_ids, key=str)),
        )
        member_sha = _canonical_sha256(member_payload)
        membership_sha = _canonical_sha256([member_payload])
        cur.execute(
            """INSERT INTO run_asset_membership_seals(
                   run_id,seal_revision,lifecycle_revision,membership_sha256,
                   expected_asset_count,expected_chunk_count,actor_type,
                   policy_version,reason_code)
                 VALUES(%s,1,0,%s,1,%s,'integration-test',
                        'asset-promotion-v1','test_seed')
                 RETURNING id""",
            (run_id, membership_sha, count),
        )
        seal_id = UUID(str(cur.fetchone()[0]))
        cur.execute(
            """INSERT INTO run_asset_membership_members(
                   seal_id,run_id,subject_id,snapshot_id,role,ordinal,
                   chunk_ids,chunk_count,member_sha256)
                 VALUES(%s,%s,%s,%s,'acquired',0,%s,%s,%s)""",
            (
                seal_id,
                run_id,
                subject_id,
                snapshot_id,
                sorted(chunk_ids, key=str),
                count,
                member_sha,
            ),
        )

    build = _index_build(config)
    definition = build["index_definition"]
    definition_id = UUID(str(definition["id"]))
    with connect(TEST_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE embedding_manifests SET index_status='complete',
                   indexed_at=now(),error=NULL
                WHERE index_definition_id=%s AND chunk_id=ANY(%s)""",
            (definition_id, list(chunk_ids)),
        )
        cur.execute(
            """UPDATE index_jobs j SET status='complete',completed_at=now(),error=NULL
                 FROM embedding_manifests m
                WHERE j.manifest_id=m.id AND m.index_definition_id=%s
                  AND m.chunk_id=ANY(%s)""",
            (definition_id, list(chunk_ids)),
        )
        checkpoint_digest = _membership_digest(tuple(sorted(chunk_ids, key=str)))
        cur.execute(
            """INSERT INTO indexing_checkpoints(
                   run_id,lifecycle_revision,fingerprint,entity_ids,
                   expected_membership_sha256,expected_count,complete_count,
                   manifest_count,census_counts,status,idempotency_key,completed_at,
                   asset_membership_seal_id,asset_membership_sha256,
                   asset_expected_count,asset_expected_chunk_count)
                 VALUES(%s,0,%s,%s,%s,%s,%s,%s,%s::jsonb,'completed',%s,
                        now(),%s,%s,1,%s)
                 RETURNING id""",
            (
                run_id,
                definition["fingerprint"],
                sorted(chunk_ids, key=str),
                checkpoint_digest,
                count,
                count,
                count,
                json.dumps({"complete": count}),
                f"reconcile-seed:{token}",
                seal_id,
                membership_sha,
                count,
            ),
        )
        checkpoint_id = UUID(str(cur.fetchone()[0]))
        cur.execute(
            """SELECT c.id,d.snapshot_id,d.id,s.id,s.registered_domain,d.published_at
                 FROM chunks c JOIN documents d ON d.id=c.document_id
                 JOIN asset_snapshots a ON a.id=d.snapshot_id
                 JOIN sources s ON s.id=a.source_id
                WHERE c.id=ANY(%s) ORDER BY c.id""",
            (list(chunk_ids),),
        )
        payload_rows = cur.fetchall()

    index = QdrantIndex(
        config.qdrant_url,
        config.qdrant_api_key,
        definition["physical_collection"],
        definition["dimension"],
        definition["distance_metric"],
    )
    points = [
        {
            "id": str(row[0]),
            "vector": {"dense": [1.0, 0.0, 0.0, 0.0]},
            "payload": {
                "snapshot_id": str(row[1]),
                "document_id": str(row[2]),
                "source_id": str(row[3]),
                "domain": row[4],
                "published_at": row[5].isoformat() if row[5] else None,
            },
        }
        for row in payload_rows
    ]
    for start in range(0, len(points), 256):
        index.upsert(points[start : start + 256])
    index.switch_alias(config.qdrant_alias, definition["physical_collection"])

    return {
        "config": config,
        "run_id": run_id,
        "external_run_id": external_run_id,
        "checkpoint_id": checkpoint_id,
        "chunk_ids": chunk_ids,
        "points": points,
        "index": index,
        "definition": definition,
    }


def _cleanup(seed):
    with suppress(Exception):
        seed["index"].delete_collection()


def test_reconcile_audited_1376_exact_membership(tmp_path):
    seed = _seed_reconciliation_run(tmp_path, count=1376)
    try:
        result = reconcile_run(seed["config"], seed["external_run_id"])
        assert result["ok"] is True
        assert result["scope"] == "run"
        assert result["checkpoint"]["expected"] == 1376
        assert result["qdrant"]["run_coverage"] == {
            "expected": 1376,
            "present": 1376,
            "missing": 0,
            "missing_ids": [],
        }
        assert result["qdrant"]["payload_scan"]["expected"] == 1376
        assert result["qdrant"]["payload_scan"]["retrieved"] == 1376
        assert result["qdrant"]["payload_scan"]["batches"] >= 6
        assert result["qdrant"]["payload_scan"]["mismatch_count"] == 0
    finally:
        _cleanup(seed)


def test_reconcile_missing_and_orphaned_points(tmp_path):
    seed = _seed_reconciliation_run(tmp_path, count=3)
    try:
        missing_id = str(seed["chunk_ids"][0])
        seed["index"].delete([missing_id])
        orphan_id = str(uuid4())
        seed["index"].upsert(
            [
                {
                    "id": orphan_id,
                    "vector": {"dense": [1.0, 0.0, 0.0, 0.0]},
                    "payload": {},
                }
            ]
        )
        result = reconcile_run(seed["config"], seed["run_id"])
        assert result["ok"] is False
        assert result["qdrant"]["run_coverage"]["missing_ids"] == [missing_id]
        assert orphan_id in result["qdrant"]["definition_coverage"]["orphaned_ids"]
    finally:
        _cleanup(seed)


def test_reconcile_detects_real_payload_drift_without_membership_gap(tmp_path):
    seed = _seed_reconciliation_run(tmp_path, count=2)
    try:
        point = dict(seed["points"][0])
        point["payload"] = dict(point["payload"])
        point["payload"]["source_id"] = str(uuid4())
        seed["index"].upsert([point])
        result = reconcile_run(seed["config"], seed["external_run_id"])
        assert result["qdrant"]["run_coverage"]["missing"] == 0
        scan = result["qdrant"]["payload_scan"]
        assert scan["mismatch_count"] == 1
        assert scan["mismatches"][0]["field"] == "source_id"
        assert scan["mismatches"][0]["point_id"] == point["id"]
    finally:
        _cleanup(seed)


def test_reconcile_reports_alias_and_vector_schema_mismatch(tmp_path):
    seed = _seed_reconciliation_run(tmp_path, count=1)
    other = QdrantIndex(
        seed["config"].qdrant_url,
        seed["config"].qdrant_api_key,
        f"wrong_{uuid4().hex[:12]}",
        3,
        "Cosine",
    )
    try:
        other.ensure_schema()
        other.switch_alias(seed["config"].qdrant_alias, other.collection)
        alias_result = reconcile_run(seed["config"], seed["external_run_id"])
        assert alias_result["ok"] is False
        assert alias_result["qdrant"]["alias_matches"] is False

        seed["index"].switch_alias(
            seed["config"].qdrant_alias, seed["definition"]["physical_collection"]
        )
        seed["index"].delete_collection()
        incompatible = QdrantIndex(
            seed["config"].qdrant_url,
            seed["config"].qdrant_api_key,
            seed["definition"]["physical_collection"],
            3,
            "Cosine",
        )
        incompatible.ensure_schema()
        vector_result = reconcile_run(seed["config"], seed["external_run_id"])
        assert vector_result["ok"] is False
        assert vector_result["qdrant"]["schema"]["compatible"] is False
    finally:
        with suppress(Exception):
            other.delete_collection()
        _cleanup(seed)


def test_payload_indexes_are_typed_and_idempotent_on_real_qdrant(tmp_path):
    seed = _seed_reconciliation_run(tmp_path, count=1)
    try:
        first = seed["index"].ensure_payload_indexes(PAYLOAD_INDEX_SCHEMAS)
        second = seed["index"].ensure_payload_indexes(PAYLOAD_INDEX_SCHEMAS)
        assert first == second == {field: True for field in PAYLOAD_INDEX_SCHEMAS}
        details = seed["index"].inspect_payload_indexes(PAYLOAD_INDEX_SCHEMAS)
        assert {
            field: detail["actual_type"] for field, detail in details.items()
        } == PAYLOAD_INDEX_SCHEMAS
    finally:
        _cleanup(seed)


def test_read_only_reconciliation_does_not_create_missing_payload_index(tmp_path):
    seed = _seed_reconciliation_run(tmp_path, count=1)
    field = "domain"
    path = (
        f"/collections/{seed['definition']['physical_collection']}"
        f"/index/{field}?wait=true"
    )
    try:
        seed["index"]._request("DELETE", path)
        before = seed["index"].inspect_payload_indexes({field: "keyword"})
        assert before[field]["present"] is False

        observed = reconcile_run(seed["config"], seed["external_run_id"], repair=False)
        after_observation = seed["index"].inspect_payload_indexes({field: "keyword"})
        assert observed["read_only"] is True
        assert observed["ok"] is False
        assert after_observation[field]["present"] is False

        repaired = reconcile_run(seed["config"], seed["external_run_id"], repair=True)
        after_repair = seed["index"].inspect_payload_indexes({field: "keyword"})
        assert repaired["read_only"] is False
        assert after_repair[field]["compatible"] is True
    finally:
        _cleanup(seed)


def test_repair_refuses_to_repoint_alias_outside_activation_lifecycle(tmp_path):
    seed = _seed_reconciliation_run(tmp_path, count=1)
    other = QdrantIndex(
        seed["config"].qdrant_url,
        seed["config"].qdrant_api_key,
        f"wrong_{uuid4().hex[:12]}",
        4,
        "Cosine",
    )
    try:
        other.ensure_schema()
        other.switch_alias(seed["config"].qdrant_alias, other.collection)
        result = reconcile_run(seed["config"], seed["external_run_id"], repair=True)
        assert result["post_repair"]["qdrant"]["alias_target"] == other.collection
        assert any("index-activate" in blocker for blocker in result["repair_blockers"])
    finally:
        with suppress(Exception):
            other.delete_collection()
        _cleanup(seed)
