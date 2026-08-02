from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from research_store.blob import ContentAddressedBlobStore
from research_store.config import StoreConfig
from research_store.inspection_service import (
    InspectionService,
    PageRequest,
    PassageBounds,
)
from research_store.postgres import connect

DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DSN, reason="disposable PostgreSQL is not configured"
)


def _config(tmp_path: Path) -> StoreConfig:
    values = StoreConfig.from_env().__dict__ | {
        "database_url": DSN,
        "blob_root": tmp_path,
    }
    return StoreConfig(**values)


def test_database_native_pagination_replay_and_bounded_passages(tmp_path):
    run_id = uuid4()
    response_ids = [uuid4(), uuid4()]
    candidate_id = uuid4()
    source_id = uuid4()
    snapshot_id = uuid4()
    document_id = uuid4()
    chunk_ids = [uuid4(), uuid4()]
    payload = json.dumps(
        {"success": True, "data": {"web": [{"url": "https://example.com/a"}]}}
    ).encode()
    blob = ContentAddressedBlobStore(tmp_path).put(
        __import__("io").BytesIO(payload), "application/json"
    )
    now = datetime.now(timezone.utc)
    with connect(DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """INSERT INTO research_runs
               (id,objective,state,execution_mode,external_run_id)
               VALUES(%s,'inspection integration','created','agent_led',%s)""",
            (run_id, f"fr_{run_id.hex}"),
        )
        for index, response_id in enumerate(response_ids):
            cursor.execute(
                """INSERT INTO search_responses
                   (id,run_id,query_text,backend,status,http_status,parser_version,
                    raw_blob_sha256,raw_blob_bytes,mime_type,content_sha256,
                    result_count,idempotency_key,requested_at,responded_at,created_at)
                   VALUES(%s,%s,%s,'firecrawl','succeeded',200,'test-v1',
                          %s,%s,'application/json',%s,1,%s,%s,%s,%s)""",
                (
                    response_id,
                    run_id,
                    f"query-{index}",
                    blob.sha256,
                    blob.byte_length,
                    hashlib.sha256(payload).hexdigest(),
                    f"inspection:{response_id}",
                    now,
                    now,
                    now,
                ),
            )
        canonical = "https://example.com/a"
        cursor.execute(
            """INSERT INTO search_candidates
               (id,run_id,canonical_url,canonical_url_sha256,original_url,
                domain,backend)
               VALUES(%s,%s,%s,%s,%s,'example.com','firecrawl')""",
            (
                candidate_id,
                run_id,
                canonical,
                hashlib.sha256(canonical.encode()).hexdigest(),
                canonical,
            ),
        )
        cursor.execute(
            """INSERT INTO candidate_occurrences
               (candidate_id,run_id,search_response_id,rank,query_text,original_url)
               VALUES(%s,%s,%s,1,'query-0',%s)""",
            (candidate_id, run_id, response_ids[0], canonical),
        )
        cursor.execute(
            """INSERT INTO sources(id,canonical_url,registered_domain,source_type)
               VALUES(%s,%s,'example.com','web')""",
            (source_id, canonical),
        )
        cursor.execute(
            """INSERT INTO asset_snapshots
               (id,source_id,requested_url,final_url,retrieved_at,mime_type,
                content_sha256,raw_blob_uri,raw_byte_length,crawl_options)
               VALUES(%s,%s,%s,%s,%s,'text/markdown',%s,%s,%s,'{}')""",
            (
                snapshot_id,
                source_id,
                canonical,
                canonical,
                now,
                hashlib.sha256(b"body").hexdigest(),
                "blob://sha256/" + hashlib.sha256(b"body").hexdigest(),
                4,
            ),
        )
        cursor.execute(
            """INSERT INTO documents
               (id,snapshot_id,title,parser_name,parser_version,
                normalization_version,document_sha256,metadata)
               VALUES(%s,%s,'title','markdown','test-v1','test-v1',%s,'{}')""",
            (document_id, snapshot_id, hashlib.sha256(b"body").hexdigest()),
        )
        for ordinal, (chunk_id, text) in enumerate(
            zip(
                chunk_ids,
                ("alpha database passage", "beta database passage"),
                strict=True,
            )
        ):
            cursor.execute(
                """INSERT INTO chunks
                   (id,document_id,ordinal,text,token_count,content_sha256,
                    chunker_name,chunker_version,tokenizer_name,metadata)
                   VALUES(%s,%s,%s,%s,3,%s,'hierarchical','test-v1','test','{}')""",
                (
                    chunk_id,
                    document_id,
                    ordinal,
                    text,
                    hashlib.sha256(text.encode()).hexdigest(),
                ),
            )
        cursor.execute(
            """INSERT INTO research_run_assets(run_id,snapshot_id,role)
               VALUES(%s,%s,'acquired')""",
            (run_id, snapshot_id),
        )
        connection.commit()
    inspector = InspectionService(_config(tmp_path))
    first = inspector.list_search_responses(run_id, PageRequest(limit=1))
    assert first["item_count"] == 1
    assert first["truncated"] is True
    second = inspector.list_search_responses(
        run_id, PageRequest(limit=1, cursor=first["next_cursor"])
    )
    assert second["item_count"] == 1
    assert second["items"][0]["id"] != first["items"][0]["id"]
    replay = inspector.replay_search(response_ids[0])
    assert replay["payload_integrity"]["verified"] is True
    assert replay["candidates"][0]["candidate_id"] == str(candidate_id)
    passages = inspector.passages(
        document_id, PassageBounds(limit=1, max_chars=100, max_tokens=10)
    )
    assert passages["item_count"] == 1
    lexical = inspector.lexical_search(
        "database", run=run_id, bounds=PassageBounds(limit=2)
    )
    assert lexical["item_count"] == 2
