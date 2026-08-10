"""Opt-in PostgreSQL + Qdrant integration coverage for issue #222.

The authoritative integration suite supplies the disposable database fixture.
Qdrant mutation is separately authorized: a disposable PostgreSQL DSN never
implies that the configured Qdrant endpoint is safe to reset.
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

from qdrant_test_support import delete_alias, require_disposable_qdrant_url
from research_store.asset_promotion_models import _canonical_sha256, _member_payload
from research_store.cli import _index_build, _index_reconcile
from research_store.config import StoreConfig
from research_store.index_checkpoint_models import _membership_digest
from research_store.postgres import connect
from research_store.qdrant import PAYLOAD_INDEX_SCHEMAS, QdrantIndex
from research_store.reconciliation import reconcile_run
from test_research_store_integration import prepared_database  # noqa: F401

TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL")
TEST_QDRANT_URL = os.environ.get("RESEARCH_STORE_TEST_QDRANT_URL")
TEST_QDRANT_ALLOW_RESET = os.environ.get("RESEARCH_STORE_TEST_QDRANT_ALLOW_RESET")
pytestmark = [
    pytest.mark.skipif(
        not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
    ),
    pytest.mark.skipif(
        not TEST_QDRANT_URL
        or (TEST_QDRANT_ALLOW_RESET or "").rstrip("/")
        != (TEST_QDRANT_URL or "").rstrip("/"),
        reason="requires explicitly authorized disposable Qdrant endpoint",
    ),
    pytest.mark.usefixtures("prepared_database"),
]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _seed_reconciliation_run(tmp_path: Path, *, count: int = 3):
    token = uuid4().hex
    alias = f"reconcile_{token[:12]}"
    suffix = token[:8]
    config = replace(
        StoreConfig.from_env(),
        database_url=TEST_DSN,
        blob_root=tmp_path / "blobs",
        qdrant_url=require_disposable_qdrant_url(),
        qdrant_alias=alias,
        embedding_model=f"reconcile-{token}",
        embedding_revision="issue-222",
        embedding_dimension=4,
        parser_version=f"markdown-v1-{suffix}",
        normalization_version=f"cleanup-v1-{suffix}",
        chunker_name="structural",
        chunker_version=f"structural-v1-{suffix}",
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
                "parser_version": config.parser_version,
                "normalization_version": config.normalization_version,
                "chunker_version": config.chunker_version,
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


def _delete_definition_records(definition_id) -> None:
    """Delete PostgreSQL projection records for one exact test-owned definition."""
    definition_id = str(definition_id)
    with connect(TEST_DSN) as conn, conn.cursor() as cur:
        cur.execute(
            """DELETE FROM index_activation_journal
               WHERE target_definition_id=%s OR previous_definition_id=%s""",
            (definition_id, definition_id),
        )
        cur.execute(
            "DELETE FROM index_point_counts WHERE index_definition_id=%s",
            (definition_id,),
        )
        cur.execute(
            "DELETE FROM index_jobs WHERE index_definition_id=%s",
            (definition_id,),
        )
        cur.execute(
            "DELETE FROM embedding_manifests WHERE index_definition_id=%s",
            (definition_id,),
        )
        cur.execute("DELETE FROM index_definitions WHERE id=%s", (definition_id,))


def _cleanup(seed) -> None:
    """Delete only resources owned by one reconciliation seed."""
    require_disposable_qdrant_url()
    with suppress(Exception):
        delete_alias(seed["index"], seed["config"].qdrant_alias)
    with suppress(Exception):
        if seed["index"].inspect_schema().get("exists"):
            seed["index"].delete_collection()
    _delete_definition_records(seed["definition"]["id"])


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
            delete_alias(other, seed["config"].qdrant_alias)
        with suppress(Exception):
            if other.inspect_schema().get("exists"):
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
            delete_alias(other, seed["config"].qdrant_alias)
        with suppress(Exception):
            if other.inspect_schema().get("exists"):
                other.delete_collection()
        _cleanup(seed)


# ---------------------------------------------------------------------------
# ARC-17 regression: CLI _index_reconcile() fails closed for every nonhealthy
# cross-store activation state.
# ---------------------------------------------------------------------------


def test_reconcile_fails_closed_when_no_pg_active_definition(tmp_path):
    seed = _seed_reconciliation_run(tmp_path, count=1)
    try:
        result = _index_reconcile(seed["config"])
        assert result["ok"] is False
        assert any("activation drift" in item for item in result["discrepancies"])
        assert any(
            "no PostgreSQL-active definition" in item
            for item in result["discrepancies"]
        )
    finally:
        _cleanup(seed)


def test_reconcile_fails_closed_on_multiple_pg_active_definitions(tmp_path):
    seed = _seed_reconciliation_run(tmp_path, count=1)
    config2 = replace(
        seed["config"],
        embedding_model=f"second-{uuid4().hex[:8]}",
        qdrant_collection=f"arc17_second_{uuid4().hex[:8]}",
    )
    build2 = _index_build(config2)
    def2_id = str(build2["index_definition"]["id"])
    col2 = build2["index_definition"]["physical_collection"]
    idx2 = QdrantIndex(
        config2.qdrant_url,
        config2.qdrant_api_key,
        col2,
        build2["index_definition"]["dimension"],
        build2["index_definition"]["distance_metric"],
    )
    try:
        with connect(TEST_DSN) as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE embedding_manifests SET index_status='complete',indexed_at=now()
                   WHERE index_definition_id=%s""",
                (def2_id,),
            )
            cur.execute(
                """UPDATE index_jobs SET status='complete',completed_at=now()
                   WHERE index_definition_id=%s""",
                (def2_id,),
            )
            cur.execute(
                """UPDATE index_definitions SET lifecycle_status='active'
                   WHERE id IN (%s,%s)""",
                (str(seed["definition"]["id"]), def2_id),
            )

        idx2.switch_alias(seed["config"].qdrant_alias, col2)
        result = _index_reconcile(seed["config"])
        assert result["ok"] is False
        assert any("activation drift" in item for item in result["discrepancies"])
        assert any(
            "active definitions" in item and "exactly one" in item
            for item in result["discrepancies"]
        )
    finally:
        with suppress(Exception):
            delete_alias(idx2, seed["config"].qdrant_alias)
        with suppress(Exception):
            if idx2.inspect_schema().get("exists"):
                idx2.delete_collection()
        _delete_definition_records(def2_id)
        _cleanup(seed)


def test_reconcile_fails_closed_when_required_alias_missing(tmp_path):
    seed = _seed_reconciliation_run(tmp_path, count=1)
    try:
        def_id = str(seed["definition"]["id"])
        with connect(TEST_DSN) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE index_definitions SET lifecycle_status='active' WHERE id=%s",
                (def_id,),
            )
        delete_alias(seed["index"], seed["config"].qdrant_alias)

        result = _index_reconcile(seed["config"])
        assert result["ok"] is False
        assert any("activation drift" in item for item in result["discrepancies"])
        assert any(
            "missing" in item or "absent" in item
            for item in result["discrepancies"]
        )
    finally:
        _cleanup(seed)


def test_reconcile_fails_closed_when_required_alias_targets_wrong_collection(
    tmp_path,
):
    seed = _seed_reconciliation_run(tmp_path, count=1)
    wrong_name = f"arc17_wrong_{uuid4().hex[:8]}"
    wrong_idx = QdrantIndex(
        seed["config"].qdrant_url,
        seed["config"].qdrant_api_key,
        wrong_name,
        seed["config"].embedding_dimension,
        "Cosine",
    )
    try:
        with connect(TEST_DSN) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE index_definitions SET lifecycle_status='active' WHERE id=%s",
                (str(seed["definition"]["id"]),),
            )
        wrong_idx.ensure_schema()
        wrong_idx.switch_alias(seed["config"].qdrant_alias, wrong_name)

        result = _index_reconcile(seed["config"])
        assert result["ok"] is False
        assert any("activation drift" in item for item in result["discrepancies"])
        assert any(
            "activation drift" in item and "expected" in item
            for item in result["discrepancies"]
        )
    finally:
        with suppress(Exception):
            delete_alias(wrong_idx, seed["config"].qdrant_alias)
        with suppress(Exception):
            if wrong_idx.inspect_schema().get("exists"):
                wrong_idx.delete_collection()
        _cleanup(seed)


def test_reconcile_passes_when_all_invariants_hold(tmp_path):
    seed = _seed_reconciliation_run(tmp_path, count=1)
    try:
        with connect(TEST_DSN) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE index_definitions SET lifecycle_status='active' WHERE id=%s",
                (str(seed["definition"]["id"]),),
            )

        result = _index_reconcile(seed["config"])
        assert result["ok"] is True
        assert result["discrepancies"] == []
    finally:
        _cleanup(seed)
