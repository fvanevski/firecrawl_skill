"""Regression coverage for issue #297 retrieval CLI stdout contract.

Every retrieval-family command must emit deterministic machine-readable JSON
on stdout on success, including explicit empty results.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from firecrawl_skill.research_store import export_serialization
from firecrawl_skill.research_store.cli import retrieval

RETRIEVAL_CLI = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "firecrawl_skill"
    / "research_store"
    / "cli"
    / "retrieval.py"
)
ASSET_ID = str(UUID("11111111-1111-1111-1111-111111111111"))

COMMANDS = (
    "build-evidence-packet",
    "corpus-overview",
    "expand-relationships",
    "fetch-passages",
    "inspect-asset",
    "search-assets",
)


def _execution() -> SimpleNamespace:
    return SimpleNamespace(
        requested_mode="hybrid",
        executed_mode="hybrid",
        mechanical_status="succeeded",
        component_health={"lexical": "healthy"},
        errors=(),
        warnings=(),
        stage_counts={"lexical": 1},
        index_fingerprint="fingerprint-1",
        skipped_stages=(),
        timing={"lexical": 0.001},
    )


POPULATED_RESULTS = {
    "corpus-overview": {
        "sources": 3,
        "snapshots": 4,
        "documents": 4,
        "chunks": 10,
        "retrieved_range": [None, None],
        "domains": {"example.com": 3},
        "source_types": {"web": 3},
        "indexes": [
            {
                "collection": "research_chunks",
                "model": "model",
                "revision": 1,
                "dimension": 384,
                "chunks": 10,
            }
        ],
        "research_runs": {},
    },
    "search-assets": (_execution(), [{"candidate_id": ASSET_ID, "score": 0.9}]),
    "inspect-asset": {
        "candidate_id": ASSET_ID,
        "title": "document title",
        "outline": [
            {"ordinal": 0, "type": "heading", "heading_path": ["h1"], "text": "t"}
        ],
    },
    "fetch-passages": [{"chunk_id": ASSET_ID, "text": "passage text"}],
    "expand-relationships": [
        {
            "chunk_id": ASSET_ID,
            "relation": "cites",
            "neighbor": "22222222-2222-2222-2222-222222222222",
        }
    ],
    "build-evidence-packet": {
        "packet_version": "research-store-v1",
        "passages": [{"chunk_id": ASSET_ID, "text": "passage text"}],
        "selection_rationale": "explicit candidate selection",
        "corroborating_groups": [],
        "contradicting_groups": [],
        "omitted_near_duplicates": [],
    },
}

EMPTY_RESULTS = {
    "corpus-overview": {
        "sources": 0,
        "snapshots": 0,
        "documents": 0,
        "chunks": 0,
        "retrieved_range": [None, None],
        "domains": {},
        "source_types": {},
        "indexes": [],
        "research_runs": {},
    },
    "search-assets": (_execution(), []),
    "inspect-asset": {
        "candidate_id": ASSET_ID,
        "title": None,
        "outline": [],
    },
    "fetch-passages": [],
    "expand-relationships": [],
    "build-evidence-packet": {
        "packet_version": "research-store-v1",
        "passages": [],
        "selection_rationale": "explicit candidate selection",
        "corroborating_groups": [],
        "contradicting_groups": [],
        "omitted_near_duplicates": [],
    },
}


class _FakeService:
    """Return-value based service mirroring the retrieval service API."""

    def __init__(self, results):
        self._results = results

    def corpus_overview(self):
        return self._results["corpus-overview"]

    def search_assets(
        self,
        query,
        *,
        filters=None,
        candidate_limit=20,
        run_id=None,
        requested_mode="hybrid",
    ):
        return self._results["search-assets"]

    def inspect_asset(self, candidate_id):
        return self._results["inspect-asset"]

    def fetch_passages(
        self,
        candidate_ids,
        *,
        max_tokens=2000,
        max_passages=8,
        include_neighboring_blocks=False,
    ):
        return self._results["fetch-passages"]

    def expand_relationships(
        self, candidate_ids, *, max_hops=1, max_results=50, max_tokens=2000
    ):
        return self._results["expand-relationships"]

    def build_evidence_packet(self, candidate_ids, *, max_tokens=3000):
        return self._results["build-evidence-packet"]


class _FakeUnitOfWork:
    """Minimal UoW for the fetch-passages identity-validation boundary."""

    connection = object()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _FakeDeps:
    """Mirror the cli module wiring: canonical dumps helper and run resolution."""

    def __init__(self, service):
        self._service = service
        self.dumps = export_serialization.dumps

    def build_service(self, config):
        return self._service

    def _resolve_run_id(self, config, external_id):
        return None

    def _uow_factory(self, config):
        return _FakeUnitOfWork


def _args(command):
    return SimpleNamespace(
        command=command,
        research_run_id=None,
        domain=None,
        source_type=None,
        date_from=None,
        date_to=None,
        query="retrieval query",
        limit=20,
        mode="hybrid",
        id=ASSET_ID,
        ids=[ASSET_ID],
        max_tokens=2000,
        max_passages=8,
        max_hops=1,
        max_results=50,
    )


def _chunk_resolution(_connection, identifier):
    return SimpleNamespace(
        identity_type="chunk",
        to_dict=lambda: {"identity_type": "chunk", "id": str(identifier)},
    )


def _invoke(command, results, capsys, monkeypatch):
    if command == "fetch-passages":
        monkeypatch.setattr(
            retrieval.retrieval_admin,
            "resolve_corpus_identity",
            _chunk_resolution,
        )
    exit_code = retrieval.run(
        _args(command), object(), _FakeDeps(_FakeService(results))
    )
    return exit_code, json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("command", COMMANDS)
def test_retrieval_command_emits_populated_result_to_stdout(
    command, capsys, monkeypatch
):
    exit_code, payload = _invoke(command, POPULATED_RESULTS, capsys, monkeypatch)

    assert exit_code == 0
    if command == "corpus-overview":
        assert payload["sources"] == 3
        assert payload["chunks"] == 10
    elif command == "search-assets":
        assert payload["candidates"] == [{"candidate_id": ASSET_ID, "score": 0.9}]
        assert payload["execution"]["executed_mode"] == "hybrid"
    elif command == "inspect-asset":
        assert payload["candidate_id"] == ASSET_ID
        assert payload["outline"]
    elif command == "fetch-passages":
        assert payload == [{"chunk_id": ASSET_ID, "text": "passage text"}]
    elif command == "expand-relationships":
        assert payload == [
            {
                "chunk_id": ASSET_ID,
                "relation": "cites",
                "neighbor": "22222222-2222-2222-2222-222222222222",
            }
        ]
    elif command == "build-evidence-packet":
        assert payload["packet_version"] == "research-store-v1"
        assert payload["passages"] == [{"chunk_id": ASSET_ID, "text": "passage text"}]


@pytest.mark.parametrize("command", COMMANDS)
def test_retrieval_command_emits_explicit_empty_result_to_stdout(
    command, capsys, monkeypatch
):
    exit_code, payload = _invoke(command, EMPTY_RESULTS, capsys, monkeypatch)

    assert exit_code == 0
    if command == "corpus-overview":
        assert payload["sources"] == 0
        assert payload["indexes"] == []
    elif command == "search-assets":
        assert payload["candidates"] == []
    elif command == "inspect-asset":
        assert payload["outline"] == []
    elif command in ("fetch-passages", "expand-relationships"):
        assert payload == []
    elif command == "build-evidence-packet":
        assert payload["passages"] == []


def test_retrieval_cli_prints_only_through_canonical_helper():
    source = RETRIEVAL_CLI.read_text(encoding="utf-8")
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("print("):
            assert stripped.startswith("print(deps.dumps("), (
                f"ad-hoc print in retrieval CLI: {stripped}"
            )
    assert source.count("print(deps.dumps(") >= len(COMMANDS)
    assert "def run(args, config, deps) -> int:" in source
