"""Regression coverage for issue #251 thin CLI entrypoints."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from firecrawl_skill.research_store import cli, store_admin

ROOT = Path(__file__).resolve().parents[2]
CLI_PACKAGE = ROOT / "src" / "firecrawl_skill" / "research_store" / "cli"
LEGACY_CLI = ROOT / "src" / "firecrawl_skill" / "research_store" / "cli.py"


def _parser_commands() -> set[str]:
    root = cli.parser()
    subparser_action = next(
        action
        for action in root._actions
        if hasattr(action, "choices") and isinstance(action.choices, dict)
    )
    assert subparser_action.choices is not None
    return set(subparser_action.choices)


def test_command_families_are_disjoint_and_cover_all_parser_commands() -> None:
    claimed: dict[str, str] = {}
    for family in cli._FAMILIES:
        for command in family.COMMANDS:
            assert command not in claimed, (
                f"{command} claimed by both {claimed[command]} and {family.__name__}"
            )
            claimed[command] = family.__name__

    assert _parser_commands() == set(claimed)
    assert cli.compatibility.COMMANDS == {
        "export-run",
        "integrity",
        "index-reconcile",
        "reconcile-qdrant",
    }


def test_canonical_root_is_dispatch_only_and_does_not_exec_legacy_monolith() -> None:
    source = (CLI_PACKAGE / "__init__.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    assert "exec(" not in source
    assert "_artifact_main" not in source
    assert "_reconcile_main" not in source
    assert "_SPECIAL_COMMANDS" not in source
    assert len(source.splitlines()) < 280
    assert any(
        isinstance(node, ast.FunctionDef) and node.name == "main" for node in tree.body
    )
    assert len(LEGACY_CLI.read_text(encoding="utf-8").splitlines()) < 40


def test_dispatch_selects_one_family_without_application_logic(monkeypatch) -> None:
    sentinel_config = object()
    calls = []
    monkeypatch.setattr(cli.StoreConfig, "from_env", lambda: sentinel_config)
    monkeypatch.setattr(
        cli.admin,
        "run",
        lambda args, config, deps: calls.append((args.command, config, deps)) or 17,
    )

    assert cli.main(["status"]) == 17
    assert calls == [("status", sentinel_config, cli)]


def test_raw_overlay_dispatch_selects_compatibility_family(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        cli.compatibility,
        "run_argv",
        lambda command, argv, deps: calls.append((command, argv, deps)) or 19,
    )

    argv = ["fr_test", "--output", "/tmp/unused.json"]
    assert cli.main(["export-run", *argv]) == 19
    assert calls == [("export-run", argv, cli)]


def test_artifact_overlay_rejects_unknown_schema_version() -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(
            [
                "export-run",
                "fr_test",
                "--output",
                "/tmp/unused.json",
                "--schema-version",
                "export-run-custom",
            ]
        )
    assert exc.value.code == 2


def test_reconciliation_overlay_preserves_optional_run_and_repair(
    monkeypatch, capsys
) -> None:
    config = object()
    calls = []
    monkeypatch.setattr(cli.StoreConfig, "from_env", lambda: config)
    monkeypatch.setattr(
        cli,
        "reconcile_run",
        lambda actual_config, run, repair=False: (
            calls.append((actual_config, run, repair)) or {"ok": True, "scope": "run"}
        ),
    )

    assert cli.main(["reconcile-qdrant", "fr_test", "--repair"]) == 0
    assert calls == [(config, "fr_test", True)]
    assert '"ok": true' in capsys.readouterr().out.lower()


def test_legacy_connectivity_classifier_preserves_pre_refactor_reason_code() -> None:
    result = store_admin.classify_connectivity_failure(
        RuntimeError("connect ECONNREFUSED 127.0.0.1:5432")
    )

    assert result == {
        "status": "failure",
        "reason_code": "query_runtime_failure",
        "detail": "connect ECONNREFUSED 127.0.0.1:5432",
    }


def test_legacy_cli_file_is_a_delegating_facade() -> None:
    source = LEGACY_CLI.read_text(encoding="utf-8")
    assert "from firecrawl_skill.research_store.cli import main, parser" in source
    assert "ArgumentParser(" not in source
    assert "PostgresUnitOfWork" not in source
