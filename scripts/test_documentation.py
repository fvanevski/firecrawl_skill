#!/usr/bin/env python3
"""Verify RC-9 documentation against authoritative parser and storage contracts."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import ClassVar

import pytest

SCRIPTS = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPTS.parent
RUN_ID = "fr_" + "a" * 32
INVOCATION_ID = "fc_" + "b" * 32

REFERENCE_FILES = [
    "references/operations-runbook.md",
    "references/migration-guide.md",
    "references/coding-agent-guide.md",
    "references/recovery-drill-checklist.md",
    "references/research-store-architecture.md",
    "references/research-store-operations.md",
    "references/workflow-state-schema.md",
    "references/budget-policy.md",
    "references/cli-script-disambiguation.md",
    "references/research-domain-schemas.md",
    "references/phase-1-gate-report.md",
    "references/phase-6-retrieval-transparency.md",
    "references/release-notes-rc9.md",
]

RUNTIME_DOCUMENTS = [
    "README.md",
    "SKILL.md",
    "references/operations-runbook.md",
    "references/research-store-operations.md",
    "references/research-store-architecture.md",
    "references/recovery-drill-checklist.md",
    "references/cli-script-disambiguation.md",
    "references/coding-agent-guide.md",
    "references/workflow-state-schema.md",
]

REMOVED_RUNTIME_MARKERS = (
    "/tmp/firecrawl_scratch",
    "firecrawl_scratch",
    "_corpus.json",
    "_search.json",
    "_meta.json",
    "_index.md",
    "_workflow_input.json",
    "persist_results.py",
    "scripts/fread",
    "import-scratch",
    "FIRECRAWL_RESEARCH_PERSIST",
    "FIRECRAWL_RESEARCH_ACTIVE",
    "FIRECRAWL_CAPTURE_RAW",
    "SCRATCH_ROOT",
    "scratch-only",
    "filesystem-only acquisition",
    "--reuse-search",
    "--scrape-ranks",
    "--output-dir",
)


def _subcommands(parser: argparse.ArgumentParser) -> set[str]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return set(action.choices)
    return set()


def _options(parser: argparse.ArgumentParser) -> set[str]:
    return {
        option
        for action in parser._actions
        for option in getattr(action, "option_strings", ())
    }


class TestDocumentationFiles:
    REFERENCE_FILES: ClassVar[list[str]] = REFERENCE_FILES

    @pytest.mark.parametrize("rel_path", REFERENCE_FILES)
    def test_reference_file_exists(self, rel_path: str) -> None:
        path = SKILL_ROOT / rel_path
        assert path.is_file(), f"missing reference file: {rel_path}"
        assert path.stat().st_size > 0, f"empty reference file: {rel_path}"

    @pytest.mark.parametrize("rel_path", RUNTIME_DOCUMENTS)
    def test_runtime_docs_do_not_advertise_removed_surfaces(
        self,
        rel_path: str,
    ) -> None:
        content = (SKILL_ROOT / rel_path).read_text(encoding="utf-8")
        for marker in REMOVED_RUNTIME_MARKERS:
            assert marker not in content, (
                f"removed runtime marker {marker!r} remains in {rel_path}"
            )

    def test_target_a_boundary_is_explicit(self) -> None:
        for rel_path in (
            "README.md",
            "references/research-store-architecture.md",
            "references/migration-guide.md",
            "references/release-notes-rc9.md",
        ):
            content = (SKILL_ROOT / rel_path).read_text(encoding="utf-8")
            assert "Target A" in content
            assert "BLOB_ROOT" in content
            assert "PostgreSQL" in content
            assert "Qdrant" in content
            assert "Valkey" in content

        architecture = (
            SKILL_ROOT / "references/research-store-architecture.md"
        ).read_text(encoding="utf-8")
        assert "Future PostgreSQL payload migration" in architecture
        assert "not implemented" in architecture

    def test_release_notes_define_breaking_boundary(self) -> None:
        content = (SKILL_ROOT / "references/release-notes-rc9.md").read_text(
            encoding="utf-8"
        )
        for required in (
            "82d3369c0be9bba381f38b598c3b05ed4b683ae6",
            "1aaa92f7c3a84ea1ed210947130b120cc814826e",
            "Breaking changes",
            "Parser and error compatibility",
            "Current equivalents",
            "Legacy acquisition-tree migration",
            "Migration effects",
            "Rollback",
            "No tag name is asserted",
            "future migration",
        ):
            assert required in content

    def test_migration_guide_defines_import_and_rollback(self) -> None:
        content = (SKILL_ROOT / "references/migration-guide.md").read_text(
            encoding="utf-8"
        )
        for required in (
            "82d3369c0be9bba381f38b598c3b05ed4b683ae6",
            "research-db import-scratch",
            "--dry-run",
            "Current equivalents",
            "Rollback boundary",
            "unsupported schema lineage",
        ):
            assert required in content

    def test_skill_uses_current_finspect_run_option(self) -> None:
        content = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        assert 'invocations --run "$RUN_ID"' in content
        assert 'search-responses --run "$RUN_ID"' in content
        assert "invocations --run-id" not in content
        assert "search-responses --run-id" not in content

    def test_required_run_binding_is_shown(self) -> None:
        for rel_path in (
            "README.md",
            "SKILL.md",
            "references/operations-runbook.md",
            "references/research-store-operations.md",
            "references/recovery-drill-checklist.md",
        ):
            content = (SKILL_ROOT / rel_path).read_text(encoding="utf-8")
            assert "--research-run-id" in content
            assert "frun" in content

    def test_operations_runbook_has_required_sections(self) -> None:
        content = (SKILL_ROOT / "references/operations-runbook.md").read_text(
            encoding="utf-8"
        )
        for section in (
            "Architecture overview",
            "Service boundaries",
            "Execution modes",
            "Deployment",
            "Configuration variables",
            "Backup and restore",
            "Qdrant rebuild",
            "Valkey loss handling",
            "Endpoint restart",
            "Interrupted-run recovery",
            "PostgreSQL workflow recovery",
            "Benchmarking",
            "Release evidence",
            "Destructive commands",
            "Recovery drill checklist",
        ):
            assert section.lower() in content.lower()

    def test_coding_agent_guide_has_required_sections(self) -> None:
        content = (SKILL_ROOT / "references/coding-agent-guide.md").read_text(
            encoding="utf-8"
        )
        for section in (
            "Architecture overview",
            "Authority boundaries",
            "Execution modes",
            "Budget policy",
            "Coverage-led research",
            "State machine",
            "Extraction and derivations",
            "Retrieval and evidence",
            "Report synthesis and validation",
            "Cache behavior",
            "Embedding microbatching",
            "Resource governance",
            "Configuration variables",
            "Coding conventions",
            "Testing guidance",
        ):
            assert section.lower() in content.lower()

    def test_migration_guide_has_required_sections(self) -> None:
        content = (SKILL_ROOT / "references/migration-guide.md").read_text(
            encoding="utf-8"
        )
        for section in (
            "Migration principles",
            "Pre-migration checklist",
            "Running migrations",
            "Migration sequence",
            "Interrupted migration repair",
            "Forward-repair migrations",
            "Rollback boundary",
            "Migration testing",
        ):
            assert section.lower() in content.lower()

    def test_operations_runbook_documents_required_config(self) -> None:
        content = (SKILL_ROOT / "references/operations-runbook.md").read_text(
            encoding="utf-8"
        )
        for variable in (
            "DATABASE_URL",
            "BLOB_ROOT",
            "QDRANT_URL",
            "VALKEY_URL",
            "EMBEDDING_URL",
            "RERANKER_URL",
            "FIRECRAWL_LLM_LOCAL_BASE_URL",
            "FIRECRAWL_AUDIT_AUTO_SEMANTIC",
        ):
            assert variable in content


class TestParserBackedExamples:
    def test_fsearch_examples_parse(self) -> None:
        from research_store.fsearch_service import build_parser

        parser = build_parser()
        args = parser.parse_args(
            [
                "bounded query",
                "--research-run-id",
                RUN_ID,
                "--limit",
                "20",
                "--scrape-limit",
                "5",
                "--sources",
                "web,news",
                "--tbs",
                "qdr:d",
                "--invocation-id",
                INVOCATION_ID,
                "--idempotency-key",
                "stable-key",
                "--json",
            ]
        )
        assert args.research_run_id == RUN_ID
        assert args.scrape_limit == 5

        options = _options(parser)
        assert "--dir" in options
        assert "--reuse-search" not in options
        assert "--scrape-ranks" not in options

    def test_fscrape_examples_parse(self) -> None:
        from research_store.fscrape_cli import build_parser

        parser = build_parser()
        args = parser.parse_args(
            [
                "https://example.com",
                "--research-run-id",
                RUN_ID,
                "--format",
                "markdown",
                "--invocation-id",
                INVOCATION_ID,
                "--idempotency-key",
                "stable-key",
                "--json",
            ]
        )
        assert args.research_run_id == RUN_ID
        assert args.format == "markdown"
        assert "--output-dir" not in _options(parser)

    @pytest.mark.parametrize(
        "argv",
        [
            ["runs", "--limit", "20"],
            ["invocations", "--run", RUN_ID, "--limit", "20"],
            ["search-responses", "--run", RUN_ID, "--limit", "20"],
            ["replay-search", "00000000-0000-0000-0000-000000000001"],
            [
                "scrape-candidates",
                "00000000-0000-0000-0000-000000000001",
                "--format",
                "markdown",
                "--idempotency-key",
                "stable-key",
            ],
            [
                "retry-candidates",
                "00000000-0000-0000-0000-000000000002",
                "--idempotency-key",
                "new-key",
            ],
            ["attempts", "--run", RUN_ID],
            ["inspect", "00000000-0000-0000-0000-000000000003"],
            [
                "passages",
                "00000000-0000-0000-0000-000000000003",
                "--limit",
                "20",
                "--max-chars",
                "20000",
                "--max-tokens",
                "4000",
            ],
            ["lexical-search", "terms", "--run", RUN_ID],
            ["pattern-search", "literal.identifier", "--mode", "literal"],
        ],
    )
    def test_finspect_examples_parse(self, argv: list[str]) -> None:
        from research_store.inspection_cli import parser

        parsed = parser().parse_args(argv)
        assert parsed.command

    @pytest.mark.parametrize(
        "argv",
        [
            ["migrate"],
            ["status"],
            ["ingest-ready"],
            ["doctor"],
            ["verify-blobs"],
            ["worker", "--once", "--batch-size", "64"],
            ["index-list"],
            ["index-build", "--current-config", "--all"],
            ["reconcile-qdrant"],
            ["index-activate", "index-id"],
            ["index-rollback", "prior-index-id"],
            ["index-prune", "--dry-run"],
            ["endpoint-health"],
            ["resource-status"],
            ["rederive", "--snapshot", "snapshot-id"],
            ["export-invocation", INVOCATION_ID, "--output", "invocation.json"],
            ["export-run", RUN_ID, "--output", "run.json"],
            ["corpus-overview"],
            ["search-assets", "query", "--limit", "20"],
            ["inspect-asset", "candidate-id"],
            ["fetch-passages", "candidate-id", "--max-tokens", "2000"],
            ["run-status", RUN_ID],
            [
                "benchmark",
                "run",
                "--dataset",
                "tests/fixtures/benchmark/benchmark-v2.json",
                "--output",
                "benchmark-results.json",
            ],
            [
                "benchmark",
                "results",
                "--results-path",
                "benchmark-results.json",
            ],
            [
                "benchmark",
                "report",
                "--results-path",
                "benchmark-results.json",
                "--output",
                "benchmark-report.md",
            ],
        ],
    )
    def test_research_db_examples_parse(self, argv: list[str]) -> None:
        from research_store.cli import parser

        parsed = parser().parse_args(argv)
        assert parsed.command

    def test_current_research_db_has_no_legacy_import_command(self) -> None:
        from research_store.cli import parser

        commands = _subcommands(parser())
        assert "import-scratch" not in commands


def test_authoritative_fsearch_documents_low_level_exit_contract() -> None:
    content = (SKILL_ROOT / "docs" / "authoritative-fsearch.md").read_text(
        encoding="utf-8"
    )
    assert "research-db acquisition-search" in content
    assert "before the Firecrawl adapter is constructed or invoked" in content
    assert "`0` for persisted `succeeded` or `empty`" in content
    assert "`2` for authoritative preflight failure" in content
    assert "`3`" in content
    assert "idempotency-key conflict" in content


def test_removed_flag_documentation_matches_parser_contract() -> None:
    content = (SKILL_ROOT / "docs" / "authoritative-fsearch.md").read_text(
        encoding="utf-8"
    )
    assert "`--dir` remains a hidden compatibility tombstone" in content
    assert "`--reuse-search` and `--scrape-ranks` are not registered" in content
    assert "standard `unrecognized arguments` diagnostic" in content

    fscrape = (SKILL_ROOT / "docs" / "authoritative-fscrape.md").read_text(
        encoding="utf-8"
    )
    assert "`--output-dir PATH` and `--output-dir=PATH` fail" in fscrape
    assert "before the Firecrawl adapter is" in fscrape
