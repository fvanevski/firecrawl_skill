from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from test_issue305_identity_resolution import TEST_DSN, _insert_lineage

from firecrawl_skill.research_store.inspection_contract import PassageBounds
from firecrawl_skill.research_store.inspection_service import InspectionService
from firecrawl_skill.research_store.postgres import connect, migrate

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


def test_promotion_subject_passages_bind_exact_retained_snapshot_with_multiple_attempts() -> None:
    migrate(TEST_DSN)
    with connect(TEST_DSN) as connection:
        retained = _insert_lineage(connection, "retained-multi-attempt")
        second_attempt = uuid4()
        second_snapshot = UUID(int=1)
        second_document = UUID(int=2)
        second_chunk = UUID(int=3)
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
                    retained["search_candidate"],
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
            retained["promotion_subject"],
            PassageBounds(limit=8, max_chars=20_000, max_tokens=4_000),
        )

        assert result["resolved_asset_id"] == str(retained["snapshot"])
        assert [item["id"] for item in result["items"]] == [str(retained["chunk"])]
        assert str(second_chunk) not in {item["id"] for item in result["items"]}
        connection.rollback()
