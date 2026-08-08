"""Issue #219 regressions for run-level blob verification semantics."""

from __future__ import annotations

import hashlib
import json
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Self
from uuid import UUID

from research_store import cli as store_cli
from research_store.blob import ContentAddressedBlobStore
from research_store.run_service import ResearchRunService

RUN_ID = UUID(int=219)
INVOCATION_ID = UUID(int=1)
EXTERNAL_RUN_ID = "fr_issue_219_verifier"


class _Runs:
    def __init__(self, results: list[dict[str, Any]]) -> None:
        self.results = results

    def list_invocations(self, run_id: UUID) -> list[dict[str, Any]]:
        assert run_id == RUN_ID
        if not self.results:
            return []
        return [
            {
                "id": INVOCATION_ID,
                "output": {"results": self.results},
            }
        ]


class _UnitOfWork:
    def __init__(self, results: list[dict[str, Any]]) -> None:
        self.runs = _Runs(results)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def _service(
    store: ContentAddressedBlobStore,
    results: list[dict[str, Any]],
) -> ResearchRunService:
    return ResearchRunService(
        lambda: _UnitOfWork(results),
        blob_store=store,
    )


def _artifact(store: ContentAddressedBlobStore, digest: str) -> dict[str, str]:
    return {
        "path": str(store.path_for(digest)),
        "sha256": digest,
    }


def _assert_partition(report: dict[str, Any]) -> None:
    assert report["total"] == (
        report["available"] + report["missing"] + report["hash_mismatch"]
    )


def test_zero_eligible_objects_are_inconclusive(tmp_path: Path) -> None:
    report = _service(ContentAddressedBlobStore(tmp_path), []).verify(RUN_ID)

    assert report["status"] == "inconclusive"
    assert report["total"] == 0
    assert report["available"] == 0
    assert report["missing"] == 0
    assert report["hash_mismatch"] == 0
    assert report["file_based_unverified"] == 0
    _assert_partition(report)


def test_all_valid_objects_pass(tmp_path: Path) -> None:
    store = ContentAddressedBlobStore(tmp_path)
    blob = store.put(BytesIO(b"issue-219 valid payload"))
    report = _service(
        store,
        [{"snapshot": _artifact(store, blob.sha256)}],
    ).verify(RUN_ID)

    assert report["status"] == "passed"
    assert report["total"] == 1
    assert report["available"] == 1
    assert report["missing"] == 0
    assert report["hash_mismatch"] == 0
    assert report["artifacts"][0]["state"] == "available"
    _assert_partition(report)


def test_referenced_absent_object_is_missing_and_failed(tmp_path: Path) -> None:
    store = ContentAddressedBlobStore(tmp_path)
    missing_digest = hashlib.sha256(b"issue-219 absent payload").hexdigest()
    assert not store.exists(missing_digest)

    report = _service(
        store,
        [{"snapshot": _artifact(store, missing_digest)}],
    ).verify(RUN_ID)

    assert report["status"] == "failed"
    assert report["total"] == 1
    assert report["available"] == 0
    assert report["missing"] == 1
    assert report["hash_mismatch"] == 0
    assert report["artifacts"][0]["state"] == "missing"
    _assert_partition(report)


def test_present_corrupt_object_is_hash_mismatch_and_failed(tmp_path: Path) -> None:
    store = ContentAddressedBlobStore(tmp_path)
    expected_digest = hashlib.sha256(b"issue-219 expected payload").hexdigest()
    target = store.path_for(expected_digest)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"issue-219 tampered payload")
    assert store.exists(expected_digest)
    assert not store.verify(expected_digest)

    report = _service(
        store,
        [{"snapshot": _artifact(store, expected_digest)}],
    ).verify(RUN_ID)

    assert report["status"] == "failed"
    assert report["total"] == 1
    assert report["available"] == 0
    assert report["missing"] == 0
    assert report["hash_mismatch"] == 1
    assert report["artifacts"][0]["state"] == "hash_mismatch"
    _assert_partition(report)


def test_mixed_objects_preserve_disjoint_totals(tmp_path: Path) -> None:
    store = ContentAddressedBlobStore(tmp_path)
    valid = store.put(BytesIO(b"issue-219 mixed valid"))
    missing_digest = hashlib.sha256(b"issue-219 mixed missing").hexdigest()
    corrupt_digest = hashlib.sha256(b"issue-219 mixed expected").hexdigest()
    corrupt_path = store.path_for(corrupt_digest)
    corrupt_path.parent.mkdir(parents=True, exist_ok=True)
    corrupt_path.write_bytes(b"issue-219 mixed corrupt")

    report = _service(
        store,
        [
            {"snapshot": _artifact(store, valid.sha256)},
            {
                "artifacts": [
                    _artifact(store, missing_digest),
                    _artifact(store, corrupt_digest),
                ]
            },
        ],
    ).verify(RUN_ID)

    assert report["status"] == "failed"
    assert report["total"] == 3
    assert report["available"] == 1
    assert report["missing"] == 1
    assert report["hash_mismatch"] == 1
    assert [artifact["state"] for artifact in report["artifacts"]] == [
        "available",
        "missing",
        "hash_mismatch",
    ]
    _assert_partition(report)


def test_file_only_reference_does_not_create_integrity_proof(tmp_path: Path) -> None:
    store = ContentAddressedBlobStore(tmp_path)
    report = _service(
        store,
        [{"snapshot": {"path": "/legacy/snapshot.json"}}],
    ).verify(RUN_ID)

    assert report["status"] == "inconclusive"
    assert report["total"] == 0
    assert report["file_based_unverified"] == 1
    assert report["artifacts"][0]["state"] == "file_based_unverified"
    _assert_partition(report)


class _CliRunService:
    def __init__(self, report: dict[str, Any]) -> None:
        self.report = report

    def status(self, *, external_id: str) -> SimpleNamespace:
        assert external_id == EXTERNAL_RUN_ID
        return SimpleNamespace(id=RUN_ID)

    def verify(self, run_id: UUID) -> dict[str, Any]:
        assert run_id == RUN_ID
        return dict(self.report)


def test_cli_exit_codes_are_stable_and_json_status_is_authoritative(
    monkeypatch,
    capsys,
) -> None:
    def run(report_status: str, *extra: str) -> tuple[int, dict[str, Any]]:
        report = {
            "target": str(RUN_ID),
            "verified_at": "2026-08-08T00:00:00+00:00",
            "status": report_status,
            "total": 0 if report_status == "inconclusive" else 1,
            "available": 1 if report_status == "passed" else 0,
            "missing": 1 if report_status == "failed" else 0,
            "hash_mismatch": 0,
            "file_based_unverified": 0,
            "artifacts": [],
        }
        monkeypatch.setattr(
            store_cli,
            "build_run_service",
            lambda _config: _CliRunService(report),
        )
        return_code = store_cli.main(["run-verify", EXTERNAL_RUN_ID, *extra])
        payload = json.loads(capsys.readouterr().out)
        return return_code, payload

    return_code, payload = run("inconclusive")
    assert return_code == 1
    assert payload["status"] == "inconclusive"

    return_code, payload = run("inconclusive", "--allow-empty")
    assert return_code == 0
    assert payload["status"] == "inconclusive"

    return_code, payload = run("passed")
    assert return_code == 0
    assert payload["status"] == "passed"

    return_code, payload = run("failed")
    assert return_code == 0
    assert payload["status"] == "failed"
