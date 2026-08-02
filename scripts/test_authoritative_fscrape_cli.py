from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock
from uuid import UUID

import pytest

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from research_store.direct_scrape_service import (
    DirectScrapeBatchResult,
    DirectScrapeItemResult,
)
from research_store.fscrape_cli import main
from research_store.fscrape_contract import FScrapeResult

RUN_UUID = UUID("11111111-1111-4111-8111-111111111111")
RUN_ID = f"fr_{RUN_UUID.hex}"
INVOCATION_UUID = UUID("22222222-2222-4222-8222-222222222222")
EXTERNAL_INVOCATION_ID = "fc_33333333333343338333333333333333"


def _item(index: int, *, status: str = "succeeded", error: str | None = None):
    suffix = index + 10
    return DirectScrapeItemResult(
        index=index,
        item_key=f"item-{index}",
        status=status,
        requested_url=f"https://example.com/{index}",
        canonical_url=f"https://example.com/{index}",
        candidate_id=UUID(f"00000000-0000-4000-8000-{suffix:012d}"),
        invocation_id=INVOCATION_UUID,
        format="markdown",
        mime_type="text/markdown",
        extraction_attempt_id=(
            UUID(f"10000000-0000-4000-8000-{suffix:012d}")
            if status == "succeeded"
            else None
        ),
        source_id=(
            UUID(f"20000000-0000-4000-8000-{suffix:012d}")
            if status == "succeeded"
            else None
        ),
        snapshot_id=(
            UUID(f"30000000-0000-4000-8000-{suffix:012d}")
            if status == "succeeded"
            else None
        ),
        document_id=(
            UUID(f"40000000-0000-4000-8000-{suffix:012d}")
            if status == "succeeded"
            else None
        ),
        derivation_id=(
            UUID(f"50000000-0000-4000-8000-{suffix:012d}")
            if status == "succeeded"
            else None
        ),
        content_sha256="a" * 64 if status == "succeeded" else None,
        raw_blob_sha256="b" * 64 if status == "succeeded" else None,
        error=error,
        diagnostic=error,
        failure_class=None if status == "succeeded" else "http_error",
    )


def _batch(items, *, status="complete"):
    return DirectScrapeBatchResult(
        run_id=RUN_UUID,
        invocation_id=INVOCATION_UUID,
        idempotency_key="fscrape:test",
        status=status,
        items=tuple(items),
    )


class _CompletedCLIService:
    def __init__(self, *, status="complete"):
        self.status = status
        self.requests = []

    def execute(self, request):
        self.requests.append(request)
        items = (
            _item(0),
            _item(1, status="failed", error="blocked"),
        )
        if self.status == "complete":
            items = items[:1]
        return FScrapeResult(
            research_run_id=request.research_run_id,
            external_invocation_id=(
                request.external_invocation_id or EXTERNAL_INVOCATION_ID
            ),
            batch=_batch(items, status=self.status),
            index_job_ids_by_chunk={},
        )


def test_cli_requires_run_before_constructing_service(capsys):
    factory = mock.Mock()

    code = main(["https://example.com", "--json"], service_factory=factory)

    assert code == 2
    factory.assert_not_called()
    payload = json.loads(capsys.readouterr().out)
    assert payload["failure_stage"] == "preflight"
    assert "research-run-id" in payload["error"]


def test_cli_missing_database_fails_before_firecrawl(capsys):
    factory = mock.Mock(side_effect=RuntimeError("DATABASE_URL is required"))

    code = main(
        [
            "https://example.com",
            "--research-run-id",
            RUN_ID,
            "--json",
        ],
        service_factory=factory,
    )

    assert code == 2
    factory.assert_called_once_with()
    payload = json.loads(capsys.readouterr().out)
    assert "DATABASE_URL" in payload["error"]


@pytest.mark.parametrize(
    "schema_text",
    [
        "{not-json}",
        "[]",
        '{"type": 7}',
    ],
)
def test_invalid_schema_fails_before_service_or_network(schema_text, capsys):
    factory = mock.Mock()

    code = main(
        [
            "https://example.com",
            "--research-run-id",
            RUN_ID,
            "--schema",
            schema_text,
            "--json",
        ],
        service_factory=factory,
    )

    assert code == 2
    factory.assert_not_called()
    payload = json.loads(capsys.readouterr().out)
    assert "schema" in payload["error"].lower()


def test_schema_file_is_read_only_as_explicit_input(tmp_path: Path):
    schema_file = tmp_path / "schema.json"
    schema_file.write_text(
        json.dumps({"type": "object", "required": ["title"]}),
        encoding="utf-8",
    )
    service = _CompletedCLIService()

    code = main(
        [
            "https://example.com",
            "--research-run-id",
            RUN_ID,
            "--schema-file",
            str(schema_file),
            "--json",
        ],
        service_factory=lambda: service,
    )

    assert code == 0
    assert service.requests[0].schema == {
        "type": "object",
        "required": ["title"],
    }


def test_removed_output_dir_has_migration_message(capsys):
    factory = mock.Mock()

    code = main(
        [
            "https://example.com",
            "--research-run-id",
            RUN_ID,
            "--output-dir",
            "/tmp/legacy",
            "--json",
        ],
        service_factory=factory,
    )

    assert code == 2
    factory.assert_not_called()
    payload = json.loads(capsys.readouterr().out)
    assert "--output-dir was removed" in payload["error"]
    assert "database-native export" in payload["error"]


def test_cli_does_not_create_acquisition_artifacts_under_tmpdir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    service = _CompletedCLIService()

    code = main(
        [
            "https://example.com",
            "--research-run-id",
            RUN_ID,
            "--invocation-id",
            EXTERNAL_INVOCATION_ID,
            "--json",
        ],
        service_factory=lambda: service,
    )

    assert code == 0
    assert list(tmp_path.iterdir()) == []
    payload = json.loads(capsys.readouterr().out)
    assert payload["items"][0]["source_id"] is not None


def test_partial_cli_result_is_authoritative_and_nonzero(capsys):
    service = _CompletedCLIService(status="partial")

    code = main(
        [
            "https://example.com/0",
            "https://example.com/1",
            "--research-run-id",
            RUN_ID,
            "--json",
        ],
        service_factory=lambda: service,
    )

    assert code == 5
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "partial"
    assert [item["status"] for item in payload["items"]] == [
        "succeeded",
        "failed",
    ]


def test_persistence_off_is_rejected(monkeypatch: pytest.MonkeyPatch, capsys):
    monkeypatch.setenv("FIRECRAWL_RESEARCH_PERSIST", "off")
    factory = mock.Mock()

    code = main(
        [
            "https://example.com",
            "--research-run-id",
            RUN_ID,
            "--json",
        ],
        service_factory=factory,
    )

    assert code == 2
    factory.assert_not_called()
    payload = json.loads(capsys.readouterr().out)
    assert "PostgreSQL-authoritative" in payload["error"]


def test_public_launcher_is_thin_and_delegates_to_service_module():
    launcher = (SCRIPTS / "fscrape").read_text(encoding="utf-8")

    assert "research_store.fscrape_cli" in launcher
    assert 'exec "$research_python"' in launcher
    assert "firecrawl scrape" not in launcher
    assert "mkdir" not in launcher
    assert " -o " not in launcher
    assert "python3 - <<" not in launcher
