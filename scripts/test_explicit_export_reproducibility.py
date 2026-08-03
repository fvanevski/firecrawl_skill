from __future__ import annotations

import json
import os
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from research_store import cli as store_cli
from research_store.config import StoreConfig
from research_store.container import build_run_service, build_service
from research_store.domain import IngestRequest
from research_store.postgres import connect, migrate, require_disposable_database_reset

TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL")
requires_postgres = pytest.mark.skipif(
    not TEST_DSN, reason="requires explicit disposable PostgreSQL test DSN"
)


@pytest.fixture(scope="module")
def prepared_export_database():
    if not TEST_DSN:
        pytest.skip("requires explicit disposable PostgreSQL test DSN")
    require_disposable_database_reset(
        TEST_DSN, os.environ.get("RESEARCH_STORE_TEST_ALLOW_RESET", "")
    )
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute("DROP SCHEMA public CASCADE")
        cursor.execute("CREATE SCHEMA public")
    assert migrate(TEST_DSN) >= 38


def _config(tmp_path: Path) -> StoreConfig:
    return replace(
        StoreConfig.from_env(),
        database_url=TEST_DSN,
        blob_root=tmp_path / "blobs",
        qdrant_collection=f"export_test_{uuid4().hex[:12]}",
        embedding_dimension=4,
    )


def _assert_canonical_file(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    assert text.endswith("\n")
    payload = json.loads(text)
    assert list(payload) == sorted(payload)
    return payload


def test_explicit_export_serializer_is_canonical_and_atomic(tmp_path):
    target = tmp_path / "export.json"
    payload = {
        "z": UUID("00000000-0000-0000-0000-000000000002"),
        "a": {"when": datetime(2026, 8, 3, tzinfo=timezone.utc)},
    }

    store_cli._export_json(target, payload)

    parsed = _assert_canonical_file(target)
    assert list(parsed["a"]) == ["when"]
    assert parsed["z"] == "00000000-0000-0000-0000-000000000002"
    assert list(tmp_path.glob(".export.json.*.tmp")) == []


def test_explicit_export_cleans_temporary_file_after_replace_failure(
    tmp_path, monkeypatch
):
    target = tmp_path / "export.json"

    def fail_replace(*_args):
        raise OSError("simulated atomic replace failure")

    monkeypatch.setattr(store_cli.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated atomic replace failure"):
        store_cli._export_json(target, {"value": 1})

    assert not target.exists()
    assert list(tmp_path.glob(".export.json.*.tmp")) == []


@requires_postgres
def test_export_invocation_is_byte_reproducible_and_read_only(
    tmp_path,
    monkeypatch,
    capsys,
    prepared_export_database,
):
    config = _config(tmp_path)
    external_run_id = f"fr_export_invocation_{uuid4().hex}"
    run = build_run_service(config).create(
        "Explicit invocation export reproducibility",
        external_run_id,
        execution_mode="autonomous_local",
    )
    invocation_id = f"fc_{uuid4().hex}"
    build_service(config).ingest_batch(
        invocation_id,
        "scrape",
        [
            IngestRequest(
                "https://export.example/second",
                b"# Second\n\nAuthoritative second asset.",
            ),
            IngestRequest(
                "https://export.example/first",
                b"# First\n\nAuthoritative first asset.",
            ),
        ],
        research_run_external_id=external_run_id,
    )

    monkeypatch.setenv("DATABASE_URL", TEST_DSN)
    monkeypatch.setenv("BLOB_ROOT", str(config.blob_root))
    first = tmp_path / "invocation-one.json"
    second = tmp_path / "invocation-two.json"

    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT b.status,b.completed_at,count(a.id),r.lifecycle_revision
     FROM ingestion_batches b
     JOIN research_runs r ON r.id=b.research_run_id
     LEFT JOIN ingestion_batch_assets a ON a.batch_id=b.id
     WHERE b.invocation_id=%s
     GROUP BY b.status,b.completed_at,r.lifecycle_revision""",
            (invocation_id,),
        )
        before = cursor.fetchone()

    assert (
        store_cli.main(["export-invocation", invocation_id, "--output", str(first)])
        == 0
    )
    capsys.readouterr()
    assert (
        store_cli.main(["export-invocation", invocation_id, "--output", str(second)])
        == 0
    )
    capsys.readouterr()

    assert first.read_bytes() == second.read_bytes()
    payload = _assert_canonical_file(first)
    assert [item["ordinal"] for item in payload["assets"]] == [0, 1]
    assert payload["research_run_id"] == external_run_id

    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """SELECT b.status,b.completed_at,count(a.id),r.lifecycle_revision
     FROM ingestion_batches b
     JOIN research_runs r ON r.id=b.research_run_id
     LEFT JOIN ingestion_batch_assets a ON a.batch_id=b.id
     WHERE b.invocation_id=%s
     GROUP BY b.status,b.completed_at,r.lifecycle_revision""",
            (invocation_id,),
        )
        after = cursor.fetchone()
    assert after == before
    assert run.lifecycle_revision == before[3]


@requires_postgres
def test_export_run_totally_orders_equal_timestamp_events_and_is_read_only(
    tmp_path,
    monkeypatch,
    prepared_export_database,
):
    config = _config(tmp_path)
    external_run_id = f"fr_export_run_{uuid4().hex}"
    run = build_run_service(config).create(
        "Explicit run export reproducibility",
        external_run_id,
        execution_mode="autonomous_local",
    )
    first_event_id = UUID("00000000-0000-0000-0000-000000000001")
    second_event_id = UUID("00000000-0000-0000-0000-000000000002")
    same_time = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)

    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            """INSERT INTO retrieval_events(id,run_id,stage,query,created_at)
     VALUES (%s,%s,'semantic','second',%s),
            (%s,%s,'lexical','first',%s)""",
            (
                second_event_id,
                run.id,
                same_time,
                first_event_id,
                run.id,
                same_time,
            ),
        )
        cursor.execute(
            "SELECT state,lifecycle_revision FROM research_runs WHERE id=%s",
            (run.id,),
        )
        run_before = cursor.fetchone()
        cursor.execute(
            "SELECT count(*) FROM retrieval_events WHERE run_id=%s", (run.id,)
        )
        event_count_before = cursor.fetchone()[0]

    monkeypatch.setenv("DATABASE_URL", TEST_DSN)
    monkeypatch.setenv("BLOB_ROOT", str(config.blob_root))
    first = tmp_path / "run-one.json"
    second = tmp_path / "run-two.json"

    assert store_cli.main(["export-run", external_run_id, "--output", str(first)]) == 0
    assert store_cli.main(["export-run", external_run_id, "--output", str(second)]) == 0

    assert first.read_bytes() == second.read_bytes()
    payload = _assert_canonical_file(first)
    assert [event["id"] for event in payload["retrieval_events"]] == [
        str(first_event_id),
        str(second_event_id),
    ]

    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT state,lifecycle_revision FROM research_runs WHERE id=%s",
            (run.id,),
        )
        assert cursor.fetchone() == run_before
        cursor.execute(
            "SELECT count(*) FROM retrieval_events WHERE run_id=%s", (run.id,)
        )
        assert cursor.fetchone()[0] == event_count_before
