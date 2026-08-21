"""Production-seam coverage for terminal completion provenance immutability."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from firecrawl_skill.research_store.composition import build_run_service
from firecrawl_skill.research_store.config import StoreConfig
from firecrawl_skill.research_store.lifecycle_guard import GuardedResearchRunService
from firecrawl_skill.research_store.postgres import connect, migrate
from tests.integration.test_completion_provenance_integration import _ready

TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL") or ""
pytestmark = pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)

_TERMINAL_GUARD = "terminal research run provenance is immutable"


@pytest.fixture
def terminal_guard_config(tmp_path: Path) -> StoreConfig:
    migrate(TEST_DSN)
    return replace(
        StoreConfig.from_env(),
        database_url=TEST_DSN,
        blob_root=tmp_path / "blobs",
        qdrant_collection=f"terminal_guard_{uuid4().hex}",
        embedding_dimension=4,
    )


def _packet_record(run_id):
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT research_spec_id,coverage_revision,packet_revision,payload
                 FROM evidence_packets
                WHERE run_id=%s
                ORDER BY packet_revision DESC
                LIMIT 1""",
            (run_id,),
        )
        row = cursor.fetchone()
    assert row is not None
    return row


def _persist_next_packet(uow, run_id, packet):
    research_spec_id, coverage_revision, packet_revision, payload = packet
    return uow.evidence_packets.persist_evidence_packet(
        run_id,
        research_spec_id,
        int(coverage_revision),
        int(packet_revision) + 1,
        payload,
    )


def _assert_terminal_guard(action):
    with pytest.raises(Exception, match=_TERMINAL_GUARD):
        action()


def test_container_wires_guarded_run_service(
    terminal_guard_config: StoreConfig,
):
    assert isinstance(
        build_run_service(terminal_guard_config),
        GuardedResearchRunService,
    )


def test_migration_installs_terminal_provenance_guards(
    terminal_guard_config: StoreConfig,
):
    expected_tables = {
        "evidence_packets",
        "research_claims",
        "claim_evidence_links",
        "synthesis_stages",
        "semantic_calls",
        "semantic_artifacts",
        "run_asset_membership_seals",
        "run_asset_membership_members",
    }
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT c.relname
                 FROM pg_trigger t
                 JOIN pg_class c ON c.oid=t.tgrelid
                WHERE NOT t.tgisinternal
                  AND t.tgname LIKE %s
                ORDER BY c.relname""",
            ("%_terminal_provenance_guard",),
        )
        guarded = {str(row[0]) for row in cursor.fetchall()}
    assert guarded == expected_tables


def test_terminal_run_rejects_public_provenance_mutation_families(
    terminal_guard_config: StoreConfig,
):
    runs, status, _provenance, workflow = _ready(terminal_guard_config)
    assert status.external_id is not None
    finished = workflow.finish_run(status.external_id, outcome="satisfied")
    assert finished.state == "completed"

    with runs.uow_factory() as uow:
        packet = uow.evidence_packets.get_evidence_packet(status.id)
        claims = uow.claims.list_claims(status.id)
        links = uow.claims.list_evidence_links(status.id)
        draft = uow.synthesis_stages.get_synthesis_stage(status.id, "draft")
        semantic = uow.semantic_calls.get_semantic_call(
            status.id, draft["semantic_call_id"]
        )

    assert packet is not None
    assert claims
    assert links
    assert semantic["artifacts"]
    claim = claims[0]
    link = links[0]
    artifact = semantic["artifacts"][0]

    def persist_packet():
        with runs.uow_factory() as uow:
            _persist_next_packet(
                uow,
                status.id,
                (
                    packet.research_spec_id,
                    packet.coverage_revision,
                    packet.packet_revision,
                    packet.payload,
                ),
            )

    def mutate_claim():
        with runs.uow_factory() as uow:
            uow.claims.upsert_claim(
                status.id,
                UUID(str(claim["claim_id"])),
                claim["statement"],
                semantic_status=claim["semantic_status"],
                uncertainty=claim["uncertainty"],
                evidence_packet_revision=int(claim["evidence_packet_revision"]),
            )

    def delete_claims():
        with runs.uow_factory() as uow:
            uow.claims.delete_claims(status.id)

    def append_evidence_link():
        with runs.uow_factory() as uow:
            uow.claims.insert_evidence_link(
                status.id,
                UUID(str(link["claim_id"])),
                UUID(str(link["passage_id"])),
                UUID(str(link["snapshot_id"])),
                source_url=link["source_url"],
                relationship=link["relationship"],
                confidence=float(link["confidence"]),
            )

    def delete_evidence_links():
        with runs.uow_factory() as uow:
            uow.claims.delete_evidence_links(status.id)

    def mutate_synthesis_stage():
        with runs.uow_factory() as uow:
            record = dict(draft)
            record["attempts"] = int(record["attempts"]) + 1
            uow.synthesis_stages.update_synthesis_stage(record)

    def append_semantic_call():
        with runs.uow_factory() as uow:
            uow.semantic_calls.record_semantic_call(
                status.id,
                semantic["stage"],
                semantic["provider"],
                semantic["model"],
                semantic["prompt_version"],
                semantic["request"],
                f"terminal-guard:{uuid4()}",
                model_revision=semantic["model_revision"],
                status="pending",
            )

    def mutate_semantic_call():
        with runs.uow_factory() as uow:
            uow.semantic_calls.annotate_semantic_call(
                status.id,
                semantic["id"],
                {"late_terminal_annotation": True},
            )

    def append_semantic_artifact():
        with runs.uow_factory() as uow:
            uow.semantic_calls.record_semantic_artifact(
                status.id,
                semantic["id"],
                artifact["artifact_type"],
                artifact["schema_name"],
                int(artifact["schema_version"]),
                artifact["payload"],
                f"terminal-guard:{uuid4()}",
                validation_status="valid",
                validation_errors=[],
            )

    for action in (
        persist_packet,
        mutate_claim,
        delete_claims,
        append_evidence_link,
        delete_evidence_links,
        mutate_synthesis_stage,
        append_semantic_call,
        mutate_semantic_call,
        append_semantic_artifact,
    ):
        _assert_terminal_guard(action)

    with (
        pytest.raises(Exception, match=_TERMINAL_GUARD),
        connect(TEST_DSN) as connection,
        connection.cursor() as cursor,
    ):
        cursor.execute(
            """INSERT INTO run_asset_membership_seals(
                   run_id,seal_revision,lifecycle_revision,status,
                   membership_sha256,expected_asset_count,expected_chunk_count,
                   actor_type,actor_identifier,policy_version,reason_code,reason)
                 SELECT run_id,seal_revision+1,lifecycle_revision,'sealed',
                        membership_sha256,expected_asset_count,
                        expected_chunk_count,actor_type,actor_identifier,
                        policy_version,reason_code,reason
                   FROM run_asset_membership_seals
                  WHERE run_id=%s AND status='sealed'""",
            (status.id,),
        )


def test_reopen_is_the_only_path_that_reenables_provenance_writes(
    terminal_guard_config: StoreConfig,
):
    runs, status, _provenance, workflow = _ready(terminal_guard_config)
    assert status.external_id is not None
    finished = workflow.finish_run(status.external_id, outcome="satisfied")
    assert finished.state == "completed"
    packet = _packet_record(status.id)

    with (
        pytest.raises(Exception, match=_TERMINAL_GUARD),
        runs.uow_factory() as uow,
    ):
        _persist_next_packet(uow, status.id, packet)

    current = runs.status(run_id=status.id)
    reopened = runs.reopen(
        status.id,
        expected_revision=current.lifecycle_revision,
        idempotency_key=f"terminal-guard:reopen:{status.id}",
        actor_type="integration-test",
        reason="explicitly reopen before revising authoritative provenance",
    )
    assert reopened.next_state == "created"
    assert runs.status(run_id=status.id).state == "created"

    with runs.uow_factory() as uow:
        new_packet_id = _persist_next_packet(uow, status.id, packet)
    assert new_packet_id is not None


def test_writer_waits_for_terminal_commit_then_rejects(
    terminal_guard_config: StoreConfig,
    monkeypatch,
):
    runs, status, _provenance, workflow = _ready(terminal_guard_config)
    packet = _packet_record(status.id)

    from firecrawl_skill.research_store import lifecycle_guard

    original_loader = lifecycle_guard.load_authoritative_completion_provenance
    terminal_locked = threading.Event()
    release_terminal = threading.Event()
    completion_done = threading.Event()
    writer_started = threading.Event()
    writer_done = threading.Event()
    result: dict[str, object] = {}

    def paused_loader(uow, run_id, *, for_update=False):
        if for_update and run_id == status.id:
            terminal_locked.set()
            if not release_terminal.wait(5):
                raise RuntimeError("timed out waiting to release terminal transaction")
        return original_loader(uow, run_id, for_update=for_update)

    monkeypatch.setattr(
        lifecycle_guard,
        "load_authoritative_completion_provenance",
        paused_loader,
    )

    def complete():
        try:
            assert status.external_id is not None
            result["completion"] = workflow.finish_run(
                status.external_id,
                outcome="satisfied",
            )
        except Exception as exc:  # noqa: BLE001 - asserted below
            result["completion_error"] = exc
        finally:
            completion_done.set()

    def writer():
        writer_started.set()
        try:
            with runs.uow_factory() as uow:
                _persist_next_packet(uow, status.id, packet)
        except Exception as exc:  # noqa: BLE001 - asserted below
            result["writer_error"] = exc
        finally:
            writer_done.set()

    completion_thread = threading.Thread(target=complete, daemon=True)
    completion_thread.start()
    assert terminal_locked.wait(5)

    writer_thread = threading.Thread(target=writer, daemon=True)
    writer_thread.start()
    assert writer_started.wait(2)
    assert not writer_done.wait(0.2)

    release_terminal.set()
    completion_thread.join(timeout=5)
    writer_thread.join(timeout=5)
    assert not completion_thread.is_alive()
    assert not writer_thread.is_alive()
    assert "completion_error" not in result
    assert cast(Any, result["completion"]).state == "completed"
    assert _TERMINAL_GUARD in str(result.get("writer_error", ""))

    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT max(packet_revision) FROM evidence_packets WHERE run_id=%s",
            (status.id,),
        )
        row = cursor.fetchone()
        assert row is not None
        assert int(row[0]) == int(packet[2])


def test_update_writer_waits_for_fully_locked_terminal_snapshot_then_rejects(
    terminal_guard_config: StoreConfig,
    monkeypatch,
):
    runs, status, _provenance, workflow = _ready(terminal_guard_config)
    with runs.uow_factory() as uow:
        draft = uow.synthesis_stages.get_synthesis_stage(status.id, "draft")
    original_attempts = int(draft["attempts"])

    from firecrawl_skill.research_store import lifecycle_guard

    original_loader = lifecycle_guard.load_authoritative_completion_provenance
    provenance_locked = threading.Event()
    release_terminal = threading.Event()
    writer_started = threading.Event()
    writer_done = threading.Event()
    result: dict[str, object] = {}

    def paused_after_locking(uow, run_id, *, for_update=False):
        authoritative = original_loader(uow, run_id, for_update=for_update)
        if for_update and run_id == status.id:
            provenance_locked.set()
            if not release_terminal.wait(5):
                raise RuntimeError("timed out waiting to release terminal transaction")
        return authoritative

    monkeypatch.setattr(
        lifecycle_guard,
        "load_authoritative_completion_provenance",
        paused_after_locking,
    )

    def complete():
        try:
            assert status.external_id is not None
            result["completion"] = workflow.finish_run(
                status.external_id,
                outcome="satisfied",
            )
        except Exception as exc:  # noqa: BLE001 - asserted below
            result["completion_error"] = exc

    def writer():
        writer_started.set()
        try:
            with connect(TEST_DSN) as connection, connection.cursor() as cursor:
                cursor.execute(
                    """UPDATE synthesis_stages
                          SET attempts=attempts+1
                        WHERE run_id=%s AND stage_name='draft'""",
                    (status.id,),
                )
        except Exception as exc:  # noqa: BLE001 - asserted below
            result["writer_error"] = exc
        finally:
            writer_done.set()

    completion_thread = threading.Thread(target=complete, daemon=True)
    completion_thread.start()
    assert provenance_locked.wait(5)

    writer_thread = threading.Thread(target=writer, daemon=True)
    writer_thread.start()
    assert writer_started.wait(2)
    assert not writer_done.wait(0.2)

    release_terminal.set()
    completion_thread.join(timeout=5)
    writer_thread.join(timeout=5)
    assert not completion_thread.is_alive()
    assert not writer_thread.is_alive()
    assert "completion_error" not in result
    assert cast(Any, result["completion"]).state == "completed"
    assert _TERMINAL_GUARD in str(result.get("writer_error", ""))

    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT attempts FROM synthesis_stages
                WHERE run_id=%s AND stage_name='draft'""",
            (status.id,),
        )
        row = cursor.fetchone()
        assert row is not None
        assert int(row[0]) == original_attempts


def test_writer_that_commits_first_forces_terminal_revalidation_to_fail(
    terminal_guard_config: StoreConfig,
):
    runs, status, _provenance, workflow = _ready(terminal_guard_config)
    packet = _packet_record(status.id)
    writer = connect(TEST_DSN)
    completion_started = threading.Event()
    completion_done = threading.Event()
    result: dict[str, object] = {}

    try:
        with writer.cursor() as cursor:
            cursor.execute(
                """INSERT INTO evidence_packets(
                       id,run_id,research_spec_id,coverage_revision,
                       packet_revision,payload)
                     VALUES(%s,%s,%s,%s,%s,%s::jsonb)""",
                (
                    uuid4(),
                    status.id,
                    packet[0],
                    int(packet[1]),
                    int(packet[2]) + 1,
                    json.dumps(packet[3]),
                ),
            )

        def complete():
            completion_started.set()
            try:
                assert status.external_id is not None
                result["completion"] = workflow.finish_run(
                    status.external_id,
                    outcome="satisfied",
                )
            except Exception as exc:  # noqa: BLE001 - asserted below
                result["error"] = exc
            finally:
                completion_done.set()

        thread = threading.Thread(target=complete, daemon=True)
        thread.start()
        assert completion_started.wait(2)
        assert not completion_done.wait(0.2)

        writer.commit()
        thread.join(timeout=5)
        assert not thread.is_alive()
        error = str(result.get("error", ""))
        assert "failed revalidation" in error
        assert "current persisted claim provenance" in error or "stale" in error.lower()
        assert runs.status(run_id=status.id).state == "validating"
    finally:
        writer.rollback()
        writer.close()
