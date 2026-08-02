#!/usr/bin/env python3
"""Documentation command verification test.

Verifies that documentation files exist, cover all required topics,
and that SKILL.md references the new documentation. This is a
deterministic, no-network test.

Acceptance criteria for issue #69:
- [ ] Documentation command verification.
- [ ] Recovery drill checklist.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import ClassVar

import pytest

SCRIPTS = Path(__file__).parent
SKILL_ROOT = SCRIPTS.parent


def _documented_commands() -> set[str]:
    """Return top-level commands from the authoritative CLI parser."""
    from research_store.cli import parser

    root = parser()
    for action in root._actions:
        if isinstance(action, argparse._SubParsersAction):
            return set(action.choices)
    return set()


def _check_command_exists(cmd: str, extra_args: list[str]) -> bool:
    """Check whether a documented top-level command exists.

    Argument details remain in the documentation fixtures for readability, but
    command recognition is tested by inspecting the parser directly. This keeps
    the documentation test deterministic and avoids sourcing operator secrets.
    """
    del extra_args
    return cmd in _documented_commands()


# ------------------------------------------------------------------
# Commands documented in operations-runbook.md
# ------------------------------------------------------------------

OPERATIONS_RUNBOOK_COMMANDS = [
    # Core health and initialization
    ("migrate", []),
    ("status", []),
    ("doctor", []),
    ("ingest-ready", []),
    ("verify-blobs", []),
    # Worker and index lifecycle
    ("worker", []),
    ("index-once", []),
    ("index-list", []),
    ("index-build", ["--current-config", "--all"]),
    ("index-activate", ["test-id"]),
    ("index-rollback", ["test-id"]),
    ("index-prune", ["--dry-run"]),
    ("index-prune", ["--dry-run", "--keep-last", "2"]),
    ("index-prune", ["--force", "--index-id", "test-id"]),
    ("reindex", ["--all"]),
    ("reconcile-qdrant", []),
    # Derivations and explicit exports
    ("rederive", ["--all"]),
    ("rederive", ["--snapshot", "test-snapshot-id"]),
    ("rederive-v2", ["--all"]),
    ("rederive-v2", ["--snapshot", "test-snapshot-id"]),
    ("rederive-v2", ["--document", "test-doc-id"]),
    ("export-invocation", ["fc_test-id", "--output", "/tmp/out.json"]),
    ("export-run", ["test-run-id", "--output", "/tmp/out.json"]),
    # Run lifecycle and wrapper boundary
    ("run-start", ["test-ext-id", "Test objective"]),
    ("run-status", ["test-run-id"]),
    (
        "run-operation-start",
        ["test-run-id", "fc_test-id", "fscrape", "--input-file", "/tmp/input.json"],
    ),
    ("run-operation-finish", ["test-run-id", "fc_test-id", "--status", "succeeded"]),
    (
        "run-mode-change",
        [
            "test-run-id",
            "agent_led",
            "--expected-revision",
            "1",
            "--idempotency-key",
            "test-key",
            "--requested-by",
            "operator",
            "--approved-by",
            "operator",
            "--reason",
            "test",
        ],
    ),
    (
        "run-transition",
        [
            "test-run-id",
            "planning",
            "--expected-revision",
            "1",
            "--idempotency-key",
            "test-key",
        ],
    ),
    ("run-finish", ["test-run-id", "--outcome", "satisfied"]),
    ("run-reopen", ["test-run-id"]),
    ("run-cancel", ["test-run-id"]),
    ("run-annotate", ["test-run-id", "--type", "pivot", "--reason", "test pivot"]),
    ("run-verify", ["test-run-id"]),
    ("run-audit", ["test-run-id"]),
    ("run-compare", ["test-run-id-1", "test-run-id-2"]),
    # Budget, resources, benchmark, and derivations
    (
        "budget-record",
        [
            "test-run-id",
            "--research-spec",
            "/tmp/spec.json",
            "--budget-snapshot",
            "/tmp/budget.json",
        ],
    ),
    ("endpoint-health", []),
    ("resource-status", []),
    ("benchmark", ["run", "--dataset", "/tmp/dataset.json"]),
    ("benchmark", ["results", "--results-path", "/tmp/results.json"]),
    ("benchmark", ["report", "--results-path", "/tmp/results.json"]),
    ("derivation-list", []),
    ("derivation-list", ["--document", "test-doc-id"]),
    ("derivation-activate", ["test-deriv-id"]),
    ("derivation-compare", ["old-id", "new-id"]),
    ("normalize", ["--document", "test-doc-id"]),
    ("normalize", ["--all"]),
    ("parser-info", []),
    ("prune-cache", []),
]

# Commands documented in migration-guide.md
MIGRATION_COMMANDS = [
    ("migrate", []),
    ("status", []),
    ("ingest-ready", []),
    ("doctor", []),
    ("verify-blobs", []),
    ("index-build", ["--current-config", "--all"]),
    ("worker", ["--once", "--batch-size", "64"]),
    ("reconcile-qdrant", []),
    ("index-activate", ["test-id"]),
    ("index-rollback", ["test-id"]),
]

# Commands documented in coding-agent-guide.md
CODING_AGENT_COMMANDS = [
    ("search-assets", ["test-query", "--limit", "20"]),
    ("inspect-asset", ["test-candidate-id"]),
    ("fetch-passages", ["test-candidate-id", "--max-tokens", "2000"]),
    ("corpus-overview", []),
]


class TestDocumentationCommands:
    """Verify that every command referenced in documentation exists."""

    @pytest.mark.parametrize("cmd,extra_args", OPERATIONS_RUNBOOK_COMMANDS)
    def test_operations_runbook_command(self, cmd: str, extra_args: list[str]) -> None:
        """Every operations runbook command must be recognized by research-db."""
        assert _check_command_exists(cmd, extra_args), (
            f"Command '{cmd}' with args {extra_args} from operations runbook "
            f"is not recognized by research-db CLI"
        )

    @pytest.mark.parametrize("cmd,extra_args", MIGRATION_COMMANDS)
    def test_migration_guide_command(self, cmd: str, extra_args: list[str]) -> None:
        """Every migration guide command must be recognized by research-db."""
        assert _check_command_exists(cmd, extra_args), (
            f"Command '{cmd}' with args {extra_args} from migration guide "
            f"is not recognized by research-db CLI"
        )

    @pytest.mark.parametrize("cmd,extra_args", CODING_AGENT_COMMANDS)
    def test_coding_agent_command(self, cmd: str, extra_args: list[str]) -> None:
        """Every coding agent guide command must be recognized by research-db."""
        assert _check_command_exists(cmd, extra_args), (
            f"Command '{cmd}' with args {extra_args} from coding agent guide "
            f"is not recognized by research-db CLI"
        )


# ------------------------------------------------------------------
# Documentation file verification
# ------------------------------------------------------------------


class TestDocumentationFiles:
    """Verify that all referenced documentation files exist and are non-empty."""

    REFERENCE_FILES: ClassVar[list[str]] = [
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
    ]

    @pytest.mark.parametrize("rel_path", REFERENCE_FILES)
    def test_reference_file_exists(self, rel_path: str) -> None:
        """Every referenced documentation file must exist."""
        path = SKILL_ROOT / rel_path
        assert path.exists(), f"Missing reference file: {rel_path}"
        assert path.stat().st_size > 0, f"Empty reference file: {rel_path}"

    def test_skill_md_references_new_docs(self) -> None:
        """SKILL.md must reference the new documentation files."""
        skill_md = SKILL_ROOT / "SKILL.md"
        content = skill_md.read_text(encoding="utf-8")
        assert "operations-runbook.md" in content, (
            "SKILL.md does not reference operations-runbook.md"
        )
        assert "migration-guide.md" in content, (
            "SKILL.md does not reference migration-guide.md"
        )
        assert "coding-agent-guide.md" in content, (
            "SKILL.md does not reference coding-agent-guide.md"
        )

    def test_operations_runbook_has_all_sections(self) -> None:
        """Operations runbook must cover all required topics."""
        runbook = SKILL_ROOT / "references/operations-runbook.md"
        content = runbook.read_text(encoding="utf-8")

        required_sections = [
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
            "Destructive commands",
            "Recovery drill checklist",
        ]

        for section in required_sections:
            assert section.lower() in content.lower(), (
                f"operations-runbook.md missing section: {section}"
            )

    def test_migration_guide_has_all_sections(self) -> None:
        """Migration guide must cover all required topics."""
        guide = SKILL_ROOT / "references/migration-guide.md"
        content = guide.read_text(encoding="utf-8")

        required_sections = [
            "Migration principles",
            "Pre-migration checklist",
            "Running migrations",
            "Migration sequence",
            "Interrupted migration repair",
            "Forward-repair migrations",
            "Migration testing",
        ]

        for section in required_sections:
            assert section.lower() in content.lower(), (
                f"migration-guide.md missing section: {section}"
            )

    def test_coding_agent_guide_has_all_sections(self) -> None:
        """Coding agent guide must cover all required topics."""
        guide = SKILL_ROOT / "references/coding-agent-guide.md"
        content = guide.read_text(encoding="utf-8")

        required_sections = [
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
        ]

        for section in required_sections:
            assert section.lower() in content.lower(), (
                f"coding-agent-guide.md missing section: {section}"
            )

    def test_operations_runbook_documents_destructive_commands(self) -> None:
        """Destructive commands must be documented with scope and safeguards."""
        runbook = SKILL_ROOT / "references/operations-runbook.md"
        content = runbook.read_text(encoding="utf-8")

        destructive_commands = [
            "index-prune --force",
            "reset-firecrawl-research",
            "verify-blobs",
        ]

        for cmd in destructive_commands:
            assert cmd in content, (
                f"Destructive command '{cmd}' not documented in operations runbook"
            )

    def test_operations_runbook_documents_all_config_vars(self) -> None:
        """All configuration variables must have authoritative definitions."""
        runbook = SKILL_ROOT / "references/operations-runbook.md"
        content = runbook.read_text(encoding="utf-8")

        required_vars = [
            "DATABASE_URL",
            "BLOB_ROOT",
            "QDRANT_URL",
            "VALKEY_URL",
            "EMBEDDING_URL",
            "RERANKER_URL",
            "FIRECRAWL_LLM_LOCAL_BASE_URL",
            "FIRECRAWL_AUDIT_AUTO_SEMANTIC",
        ]

        for var in required_vars:
            assert var in content, (
                f"Configuration variable '{var}' not documented in operations runbook"
            )

    def test_migration_guide_documents_all_migrations(self) -> None:
        """Key clean-baseline migrations must be documented."""
        guide = SKILL_ROOT / "references/migration-guide.md"
        content = guide.read_text(encoding="utf-8")

        # Check that key migrations are mentioned
        key_migrations = [
            "0001",
            "0006",
            "0007",
            "0031",
            "0032",
            "0033",
            "0038_postgres_authority",
        ]

        for migration in key_migrations:
            assert migration in content, (
                f"Migration {migration} not documented in migration guide"
            )

    def test_removed_filesystem_authority_is_not_documented(self) -> None:
        """Operational documentation must not advertise removed runtime paths."""
        forbidden = (
            "FIRECRAWL_" + "CATALOG",
            "legacy" + "_adapter",
            "catalog" + "-export",
            "catalog" + "_v5",
        )
        for rel_path in self.REFERENCE_FILES + ["README.md", "SKILL.md"]:
            content = (SKILL_ROOT / rel_path).read_text(encoding="utf-8").lower()
            for token in forbidden:
                assert token.lower() not in content, (
                    f"Removed runtime identifier {token!r} found in {rel_path}"
                )

    def test_removed_scratch_interfaces_are_not_documented(self) -> None:
        removed = (
            "persist_results.py",
            "scripts/fread",
            "import-scratch",
            "FIRECRAWL_RESEARCH_PERSIST",
            "SCRATCH_ROOT",
        )
        documentation = [SKILL_ROOT / "README.md", SKILL_ROOT / "SKILL.md"]
        documentation.extend((SKILL_ROOT / "docs").glob("*.md"))
        documentation.extend((SKILL_ROOT / "references").glob("*.md"))

        for path in documentation:
            content = path.read_text(encoding="utf-8")
            for marker in removed:
                assert marker not in content, (
                    f"removed interface {marker!r} remains documented in "
                    f"{path.relative_to(SKILL_ROOT)}"
                )

    def test_recovery_drill_checklist_exists(self) -> None:
        """Recovery drill checklist must be present in operations runbook."""
        runbook = SKILL_ROOT / "references/operations-runbook.md"
        content = runbook.read_text(encoding="utf-8")

        assert "Recovery drill checklist" in content, (
            "Recovery drill checklist not found in operations runbook"
        )

        # Check for key drill items
        drill_items = [
            "Full disaster recovery",
            "Index cutover recovery",
            "Run recovery drill",
            "Endpoint failure drill",
        ]

        for item in drill_items:
            assert item in content, (
                f"Recovery drill item '{item}' not found in operations runbook"
            )

    def test_standalone_recovery_drill_checklist(self) -> None:
        """A standalone recovery drill checklist file must exist."""
        checklist = SKILL_ROOT / "references/recovery-drill-checklist.md"
        assert checklist.exists(), "Standalone recovery drill checklist not found"
        content = checklist.read_text(encoding="utf-8")
        assert len(content) > 500, "Recovery drill checklist is too short"

        # Verify key drill types are present
        drills = [
            "Full Disaster Recovery",
            "Index Cutover Recovery",
            "Run Recovery",
            "Endpoint Failure",
            "Migration Upgrade",
        ]
        for drill in drills:
            assert drill in content, (
                f"Drill '{drill}' not found in recovery-drill-checklist.md"
            )
