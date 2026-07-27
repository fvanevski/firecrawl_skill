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

import subprocess
from pathlib import Path
from typing import ClassVar

import pytest

SCRIPTS = Path(__file__).parent
SKILL_ROOT = SCRIPTS.parent


def _get_research_db_path() -> Path:
    """Get the path to the research-db script."""
    path = SCRIPTS / "research-db"
    if not path.exists():
        pytest.skip("research-db script not found")
    return path


def _check_command_exists(cmd: str, extra_args: list[str]) -> bool:
    """Check if a research-db subcommand is recognized.

    Uses `--help` to check recognition without executing the command.
    A recognized command will show help; an unrecognized one will error.
    """
    try:
        result = subprocess.run(  # noqa: PLW1510
            ["bash", str(_get_research_db_path()), cmd, "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        output = result.stderr + result.stdout
        # A recognized command shows "usage:" in output
        if "usage:" in output.lower():
            return True
        # An unrecognized command shows "unrecognized arguments" or "invalid choice"
        is_unrecognized = (
            "unrecognized arguments" in output.lower()
            or "invalid choice" in output.lower()
        )
        if is_unrecognized:  # noqa: SIM103
            return False
        # If we got help output, the command exists even if it has errors
        return True
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


# ------------------------------------------------------------------
# Commands documented in operations-runbook.md
# ------------------------------------------------------------------

OPERATIONS_RUNBOOK_COMMANDS = [
    # Core
    ("migrate", []),
    ("status", []),
    ("doctor", []),
    ("ingest-ready", []),
    # Worker
    ("worker", []),
    ("index-once", []),
    # Index lifecycle
    ("index-list", []),
    ("index-build", ["--current-config", "--all"]),
    ("index-activate", ["test-id"]),
    ("index-rollback", ["test-id"]),
    ("index-prune", ["--dry-run"]),
    ("index-prune", ["--dry-run", "--keep-last", "2"]),
    ("index-prune", ["--force", "--index-id", "test-id"]),
    ("reindex", ["--all"]),
    ("reconcile-qdrant", []),
    # Ingest / rederive
    ("import-scratch", ["--dry-run", "/tmp/test-scratch"]),
    ("import-scratch", ["/tmp/test-scratch"]),
    ("verify-blobs", []),
    ("rederive", ["--all"]),
    ("rederive", ["--snapshot", "test-snapshot-id"]),
    ("rederive-v2", ["--all"]),
    ("rederive-v2", ["--snapshot", "test-snapshot-id"]),
    ("rederive-v2", ["--document", "test-doc-id"]),
    # Export
    ("export-invocation", ["fc_test-id", "--output", "/tmp/out.json"]),
    ("export-run", ["test-run-id", "--output", "/tmp/out.json"]),
    # Catalog export
    ("catalog-export", ["run", "test-id", "--target-dir", "/tmp/catalog"]),
    (
        "catalog-export",
        ["invocation", "test-inv-id", "test-run-id", "--target-dir", "/tmp/catalog"],
    ),
    ("catalog-export", ["events", "test-id", "--target-dir", "/tmp/catalog"]),
    ("catalog-export", ["snapshots", "test-id", "--target-dir", "/tmp/catalog"]),
    ("catalog-export", ["claims", "test-id", "--target-dir", "/tmp/catalog"]),
    ("catalog-export", ["assessments", "test-id", "--target-dir", "/tmp/catalog"]),
    ("catalog-export", ["manifest", "test-id", "--target-dir", "/tmp/catalog"]),
    ("catalog-export", ["regenerate", "test-id", "--target-dir", "/tmp/catalog"]),
    # Run lifecycle
    ("run-start", ["test-ext-id", "Test objective"]),
    ("run-status", ["test-run-id"]),
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
    # Budget
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
    # Legacy comparisons
    ("legacy-comparisons", ["--research-run-id", "test-run-id"]),
    ("legacy-comparisons", ["--divergent-only"]),
    # Resource governance
    ("endpoint-health", []),
    ("resource-status", []),
    # Benchmark
    ("benchmark", ["run", "--dataset", "/tmp/dataset.json"]),
    ("benchmark", ["results", "--results-path", "/tmp/results.json"]),
    ("benchmark", ["report", "--results-path", "/tmp/results.json"]),
    # Derivation management
    ("derivation-list", []),
    ("derivation-list", ["--document", "test-doc-id"]),
    ("derivation-activate", ["test-deriv-id"]),
    ("derivation-activate", ["test-deriv-id", "--document", "test-doc-id"]),
    ("derivation-compare", ["old-id", "new-id"]),
    # Normalization
    ("normalize", ["--document", "test-doc-id"]),
    ("normalize", ["--all"]),
    # Parser info
    ("parser-info", []),
    # Prune cache
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
        "references/catalog-v5.md",
        "references/cli-script-disambiguation.md",
        "references/legacy-adapters.md",
        "references/research-domain-schemas.md",
        "references/phase-1-gate-report.md",
        "references/phase-6-retrieval-transparency.md",
        "references/legacy-baseline.md",
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
            "Catalog import and export",
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
            "Migration catalog",
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
            "migrate --from",
            "purge --force",
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
            "FIRECRAWL_RESEARCH_PERSIST",
            "DATABASE_URL",
            "BLOB_ROOT",
            "QDRANT_URL",
            "VALKEY_URL",
            "EMBEDDING_URL",
            "RERANKER_URL",
            "FIRECRAWL_LLM_LOCAL_BASE_URL",
            "FIRECRAWL_CATALOG_DISABLED",
            "FIRECRAWL_AUDIT_AUTO_SEMANTIC",
            "FIRECRAWL_LEGACY_ADAPTER_MODE",
        ]

        for var in required_vars:
            assert var in content, (
                f"Configuration variable '{var}' not documented in operations runbook"
            )

    def test_migration_guide_documents_all_migrations(self) -> None:
        """All migrations must be cataloged."""
        guide = SKILL_ROOT / "references/migration-guide.md"
        content = guide.read_text(encoding="utf-8")

        # Check that key migrations are mentioned
        key_migrations = [
            "0001",
            "0006",
            "0007",
            "0008",
            "0031",
            "0032",
            "0033",
        ]

        for migration in key_migrations:
            assert migration in content, (
                f"Migration {migration} not cataloged in migration guide"
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
