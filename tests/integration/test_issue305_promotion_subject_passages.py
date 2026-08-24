from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from firecrawl_skill.research_store.inspection_contract import PassageBounds
from firecrawl_skill.research_store.inspection_service import InspectionService
from firecrawl_skill.research_store.postgres import connect, migrate

TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL") or ""
pytestmark = pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)


class _BorrowedConnection:
    def __init__(self, connection):
        self.connection = connection

    def cursor(self):
        return self.connection.cursor()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _insert_retained_lineage(connection) -> dict[str, UUID]:
    ids = {
        "run": uuid4(),
        "candidate": uuid4(),
        "attempt": uuid4(),
        "source": uuid4(),
        "snapshot": uuid4(),
        "document": uuid4(),
        "chunk": uuid4(),
        "subject": UUID(int=0),
    }
    now = datetime.now(timezone.utc)
    url = f"https://issue305-review-{ids['run'].hex}.example.test/evidence"
    content = b"retained subject passage must remain authoritative"
    digest = hashlib.sha256(content).hexdigest()
    with connection.cursor() as cursor:
        cursor.execute(
            """INSERT INTO research_runs
               (id,objective,state,execution_mode,external_run_id)
               VALUES(%s,'issue 305 passage review','created','agent_led',%s)""",
            (ids["run"], f"fr_{ids['run'].hex}"),
        )
        cursor.execute(
            """INSERT INTO search_candidates
               (id,run_id,canonical_url,canonical_url_sha256,original_url,
                domain,backend)
               VALUES(%s,%s,%s,%s,%s,'example.test','firecrawl')""",
            (
                ids["candidate"],
                ids["run"],
                url,
                hashlib.sha256(url.encode()).hexdigest(),
                url,
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
            (ids["attempt"], ids["candidate"], ids["run"], now, now, digest),
        )
        cursor.execute(
            """INSERT INTO sources
               (id,canonical_url,registered_domain,source_type)
               VALUES(%s,%s,'example.test','web')""",
            (ids["source"], url),
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
                ids["attempt"],
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
               VALUES(%s,%s,%s,'retained','markdown','issue305-v1',
                      'issue305-v1',%s,'{}')""",
            (ids["document"], ids["snapshot"], ids["attempt"], digest),
        )
        cursor.execute(
            """INSERT INTO chunks
               (id,document_id,ordinal,text,token_count,content_sha256,
                chunker_name,chunker_version,tokenizer_name,metadata)
               VALUES(%s,%s,0,%s,10,%s,'hierarchical','issue305-v1',
                      'cl100k_base','{}')""",
            (ids["chunk"], ids["document"], content.decode(), digest),
        )
        cursor.execute(
            """INSERT INTO research_run_assets(run_id,snapshot_id,role,metadata)
               VALUES(%s,%s,'completion_evidence','{}')""",
            (ids["run"], ids["snapshot"]),
        )
        cursor.execute(
            """SELECT id FROM run_asset_promotion_subjects
                WHERE run_id=%s AND candidate_id=%s AND snapshot_id=%s""",
            (ids["run"], ids["candidate"], ids["snapshot"]),
        )
        ids["subject"] = UUID(str(cursor.fetchone()[0]))
    return ids


def test_promotion_subject_passages_bind_exact_retained_snapshot_with_multiple_attempts() -> (
    None
):
    migrate(TEST_DSN)
    with connect(TEST_DSN) as connection:
        retained = _insert_retained_lineage(connection)
        second_attempt = uuid4()
        second_snapshot = UUID(int=retained["snapshot"].int - 1)
        second_document = uuid4()
        second_chunk = uuid4()
        now = datetime.now(timezone.utc)
        content = b"unrelated retry snapshot that must not satisfy subject passages"
        digest = hashlib.sha256(content).hexdigest()
        with connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO extraction_attempts
                   (id,candidate_id,run_id,attempt_number,method,method_version,
                    requested_format,start_time,end_time,exit_status,http_status,
                    backend_status,raw_blob_sha256,failure_class,disposition,selected)
                   VALUES(%s,%s,%s,2,'firecrawl_main_content','issue305-v1',
                          'markdown',%s,%s,'succeeded',200,'complete',%s,'none',
                          'acceptable',false)""",
                (
                    second_attempt,
                    retained["candidate"],
                    retained["run"],
                    now,
                    now,
                    digest,
                ),
            )
            cursor.execute(
                """INSERT INTO asset_snapshots
                   (id,source_id,extraction_attempt_id,requested_url,final_url,
                    retrieved_at,mime_type,content_sha256,raw_blob_uri,
                    raw_byte_length,crawl_options)
                   SELECT %s,%s,%s,canonical_url,canonical_url,%s,
                          'text/markdown',%s,%s,%s,'{}'
                     FROM sources WHERE id=%s""",
                (
                    second_snapshot,
                    retained["source"],
                    second_attempt,
                    now,
                    digest,
                    f"blob://sha256/{digest}",
                    len(content),
                    retained["source"],
                ),
            )
            cursor.execute(
                """INSERT INTO documents
                   (id,snapshot_id,extraction_attempt_id,title,parser_name,
                    parser_version,normalization_version,document_sha256,metadata)
                   VALUES(%s,%s,%s,'retry','markdown','issue305-v1',
                          'issue305-v1',%s,'{}')""",
                (second_document, second_snapshot, second_attempt, digest),
            )
            cursor.execute(
                """INSERT INTO chunks
                   (id,document_id,ordinal,text,token_count,content_sha256,
                    chunker_name,chunker_version,tokenizer_name,metadata)
                   VALUES(%s,%s,0,%s,10,%s,'hierarchical','issue305-v1',
                          'cl100k_base','{}')""",
                (second_chunk, second_document, content.decode(), digest),
            )

        service = object.__new__(InspectionService)
        borrowed = _BorrowedConnection(connection)
        service.connection_factory = lambda: borrowed
        result = service.passages(
            retained["subject"],
            PassageBounds(limit=8, max_chars=20_000, max_tokens=4_000),
        )

        assert result["resolved_asset_id"] == str(retained["snapshot"])
        assert [item["id"] for item in result["items"]] == [str(retained["chunk"])]
        assert str(second_chunk) not in {item["id"] for item in result["items"]}
        connection.rollback()


def test_promotion_subject_pagination_remains_scoped_to_subject() -> None:
    migrate(TEST_DSN)
    with connect(TEST_DSN) as connection:
        retained = _insert_retained_lineage(connection)
        service = object.__new__(InspectionService)
        borrowed = _BorrowedConnection(connection)
        service.connection_factory = lambda: borrowed
        cursor = None
        parts: list[str] = []

        for _ in range(16):
            page = service.passages(
                retained["subject"],
                PassageBounds(
                    limit=1,
                    max_chars=7,
                    max_tokens=100,
                    cursor=cursor,
                ),
            )
            assert page["asset_id"] == str(retained["subject"])
            assert page["resolved_asset_id"] == str(retained["snapshot"])
            parts.append(page["items"][0]["text"])
            cursor = page["next_cursor"]
            if cursor is None:
                break

        assert cursor is None
        assert "".join(parts) == "retained subject passage must remain authoritative"
        connection.rollback()
