from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from firecrawl_skill.research_store.identity_resolver import resolve_corpus_identity
from firecrawl_skill.research_store.postgres import connect, migrate
from firecrawl_skill.research_store.retrieval_admin import fetch_passages

TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL") or ""


def _insert_lineage(connection, label: str) -> dict[str, UUID]:
    ids = {
        "run": uuid4(),
        "promotion_subject": UUID(int=0),
        "search_candidate": uuid4(),
        "extraction_attempt": uuid4(),
        "source": uuid4(),
        "snapshot": uuid4(),
        "document": uuid4(),
        "derivation": uuid4(),
        "chunk": uuid4(),
    }
    now = datetime.now(timezone.utc)
    host = f"issue305-{label}-{ids['run'].hex}.example.test"
    url = f"https://{host}/evidence"
    content = f"authoritative identity evidence for {label}".encode()
    digest = hashlib.sha256(content).hexdigest()
    with connection.cursor() as cursor:
        cursor.execute(
            """INSERT INTO research_runs
               (id,objective,state,execution_mode,external_run_id)
               VALUES(%s,%s,'created','agent_led',%s)""",
            (ids["run"], f"issue 305 identity {label}", f"fr_{ids['run'].hex}"),
        )
        cursor.execute(
            """INSERT INTO search_candidates
               (id,run_id,canonical_url,canonical_url_sha256,original_url,
                domain,backend)
               VALUES(%s,%s,%s,%s,%s,%s,'firecrawl')""",
            (
                ids["search_candidate"],
                ids["run"],
                url,
                hashlib.sha256(url.encode()).hexdigest(),
                url,
                host,
            ),
        )
        cursor.execute(
            """INSERT INTO extraction_attempts
               (id,candidate_id,run_id,attempt_number,method,method_version,
                requested_format,start_time,end_time,exit_status,http_status,
                backend_status,raw_blob_sha256,failure_class,disposition,selected)
               VALUES(%s,%s,%s,1,'firecrawl_main_content','issue305-v1',
                      'markdown',%s,%s,'succeeded',200,'complete',%s,'none',
                      'acceptable',true)""",
            (
                ids["extraction_attempt"],
                ids["search_candidate"],
                ids["run"],
                now,
                now,
                digest,
            ),
        )
        cursor.execute(
            """INSERT INTO sources
               (id,canonical_url,registered_domain,source_type)
               VALUES(%s,%s,%s,'web')""",
            (ids["source"], url, host),
        )
        cursor.execute(
            """INSERT INTO asset_snapshots
               (id,source_id,extraction_attempt_id,requested_url,final_url,
                retrieved_at,mime_type,content_sha256,raw_blob_uri,
                raw_byte_length,crawl_options)
               VALUES(%s,%s,%s,%s,%s,%s,'text/markdown',%s,%s,%s,'{}')""",
            (
                ids["snapshot"],
                ids["source"],
                ids["extraction_attempt"],
                url,
                url,
                now,
                digest,
                f"blob://sha256/{digest}",
                len(content),
            ),
        )
        cursor.execute(
            """INSERT INTO documents
               (id,snapshot_id,extraction_attempt_id,title,parser_name,
                parser_version,normalization_version,document_sha256,metadata)
               VALUES(%s,%s,%s,%s,'markdown','issue305-v1','issue305-v1',%s,'{}')""",
            (
                ids["document"],
                ids["snapshot"],
                ids["extraction_attempt"],
                f"Issue 305 {label}",
                digest,
            ),
        )
        cursor.execute(
            """INSERT INTO chunks
               (id,document_id,ordinal,text,token_count,content_sha256,
                chunker_name,chunker_version,tokenizer_name,metadata)
               VALUES(%s,%s,0,%s,%s,%s,'hierarchical','issue305-v1',
                      'cl100k_base','{}')""",
            (
                ids["chunk"],
                ids["document"],
                content.decode(),
                max(1, len(content) // 4),
                digest,
            ),
        )
        cursor.execute(
            """INSERT INTO document_derivations
               (id,document_id,snapshot_id,status,parser_version,
                normalization_version,chunker_name,chunker_version,
                tokenizer_name,chunk_count,block_count,configuration_sha256)
               VALUES(%s,%s,%s,'active','issue305-v1','issue305-v1',
                      'hierarchical','issue305-v1','cl100k_base',1,0,%s)""",
            (
                ids["derivation"],
                ids["document"],
                ids["snapshot"],
                hashlib.sha256(f"issue305-{label}".encode()).hexdigest(),
            ),
        )
        cursor.execute(
            """INSERT INTO research_run_assets(run_id,snapshot_id,role,metadata)
               VALUES(%s,%s,'completion_evidence','{}')""",
            (ids["run"], ids["snapshot"]),
        )
        cursor.execute(
            """SELECT id,current_stage,snapshot_id,role
                 FROM run_asset_promotion_subjects
                WHERE run_id=%s AND candidate_id=%s
                ORDER BY created_at,id""",
            (ids["run"], ids["search_candidate"]),
        )
        subjects = cursor.fetchall()
        assert len(subjects) == 1
        subject_id, stage, snapshot_id, role = subjects[0]
        assert stage == "retained"
        assert UUID(str(snapshot_id)) == ids["snapshot"]
        assert role == "completion_evidence"
        ids["promotion_subject"] = UUID(str(subject_id))
    return ids


def _assert_complete_lineage(resolved, expected: dict[str, UUID]) -> None:
    assert resolved.run_ids == (expected["run"],)
    assert resolved.promotion_subject_ids == (expected["promotion_subject"],)
    assert resolved.search_candidate_ids == (expected["search_candidate"],)
    assert resolved.extraction_attempt_ids == (expected["extraction_attempt"],)
    assert resolved.source_ids == (expected["source"],)
    assert resolved.snapshot_ids == (expected["snapshot"],)
    assert resolved.document_ids == (expected["document"],)
    assert resolved.derivation_ids == (expected["derivation"],)
    assert resolved.chunk_ids == (expected["chunk"],)


@pytest.mark.skipif(not TEST_DSN, reason="requires disposable PostgreSQL test DSN")
def test_identity_resolver_crosswalks_complete_lineage_without_cross_run_leakage() -> None:
    migrate(TEST_DSN)
    with connect(TEST_DSN) as connection:
        first = _insert_lineage(connection, "first")
        second = _insert_lineage(connection, "second")

        for identity_type in (
            "promotion_subject",
            "search_candidate",
            "extraction_attempt",
            "source",
            "snapshot",
            "document",
            "derivation",
            "chunk",
        ):
            resolved = resolve_corpus_identity(connection, first[identity_type])
            assert resolved.identity_type == identity_type
            _assert_complete_lineage(resolved, first)
            assert second["run"] not in resolved.run_ids
            assert second["promotion_subject"] not in resolved.promotion_subject_ids
            assert second["search_candidate"] not in resolved.search_candidate_ids
            assert second["extraction_attempt"] not in resolved.extraction_attempt_ids
            assert second["snapshot"] not in resolved.snapshot_ids
            assert second["document"] not in resolved.document_ids
            assert second["derivation"] not in resolved.derivation_ids
            assert second["chunk"] not in resolved.chunk_ids

        class _NoFetchService:
            called = False

            def fetch_passages(self, *_args, **_kwargs):
                self.called = True
                raise AssertionError("wrong identity must be rejected before passage fetch")

        class _BorrowedUow:
            def __init__(self, borrowed_connection):
                self.connection = borrowed_connection

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        def borrowed_uow_factory(_config):
            return lambda: _BorrowedUow(connection)

        args = SimpleNamespace(
            ids=[str(first["search_candidate"])],
            max_tokens=2000,
            max_passages=8,
            research_run_id=None,
        )
        service = _NoFetchService()
        with pytest.raises(ValueError) as exc_info:
            fetch_passages(
                service,
                object(),
                args,
                resolve_run_id=lambda _config, _run: None,
                uow_factory=borrowed_uow_factory,
            )
        diagnostic = json.loads(str(exc_info.value))
        assert diagnostic["code"] == "wrong_identity_type"
        assert diagnostic["expected_identity_type"] == "chunk"
        assert diagnostic["detected_identity_type"] == "search_candidate"
        assert diagnostic["provided_id"] == str(first["search_candidate"])
        assert "finspect passages" in diagnostic["guidance"]
        assert service.called is False

        connection.rollback()


def test_fetch_passages_contract_names_chunk_identity_in_diagnostic() -> None:
    import inspect

    source = inspect.getsource(fetch_passages)
    assert '"expected_identity_type": "chunk"' in source
    assert '"wrong_identity_type"' in source
    assert "finspect passages" in source
