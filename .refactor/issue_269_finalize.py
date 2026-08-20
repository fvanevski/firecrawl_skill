#!/usr/bin/env python3
"""Deterministic finalizer for issue #269.

Central owns the mappings in this file.  The local agent is expected only to
execute it on the exact reviewed head, inspect its fail-closed output, and then
run the prescribed validation authorities.  The script performs no Git commit,
no baseline regeneration, and no test/config weakening.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "firecrawl_skill"
STORE = SRC / "research_store"
SCRIPTS = ROOT / "scripts"
TESTS = ROOT / "tests"
WORKFLOWS = ROOT / ".github" / "workflows"
BASELINE = ROOT / "pyrefly-baseline.json"
SELF = Path(__file__).resolve()

BARE_MODULE_MAP = {
    "budget_policy": "firecrawl_skill.research_store.budget_policy",
    "candidate_ranking": "firecrawl_skill.research_store.acquisition.candidate_ranking",
    "classifier": "firecrawl_skill.research_store.acquisition.classifier",
    "model_gateway": "firecrawl_skill.model_gateway",
}

MODULE_MAP = {
    "firecrawl_skill.research_store.container": "firecrawl_skill.research_store.composition",
    "firecrawl_skill.research_store.acquisition_authority": "firecrawl_skill.research_store.acquisition.authority",
    "firecrawl_skill.research_store.bounded_acquisition": "firecrawl_skill.research_store.acquisition.adapters.bounded_firecrawl",
    "firecrawl_skill.research_store.coverage_service": "firecrawl_skill.research_store.assessment.coverage",
    "firecrawl_skill.research_store.quality_service": "firecrawl_skill.research_store.assessment.quality",
    "firecrawl_skill.research_store.duplicate_service": "firecrawl_skill.research_store.assessment.duplicates",
    "firecrawl_skill.research_store.evidence_grouping": "firecrawl_skill.research_store.assessment.grouping",
    "firecrawl_skill.research_store.audit_packet": "firecrawl_skill.research_store.assessment.audit_packet",
    "firecrawl_skill.research_store.evidence": "firecrawl_skill.research_store.assessment.evidence",
    "firecrawl_skill.research_store.claim_binding_service": "firecrawl_skill.research_store.assessment.binding",
    "firecrawl_skill.research_store.packet_validator": "firecrawl_skill.research_store.assessment.validation",
    "firecrawl_skill.research_store.report_service": "firecrawl_skill.research_store.reporting.construction",
    "firecrawl_skill.research_store.report_validator": "firecrawl_skill.research_store.reporting.validation",
    "firecrawl_skill.research_store.report_artifact_service": "firecrawl_skill.research_store.reporting.artifacts",
    "firecrawl_skill.research_store.benchmark_admin": "firecrawl_skill.research_store.release.admin",
    "firecrawl_skill.research_store.preflight": "firecrawl_skill.research_store.release.preflight",
    "firecrawl_skill.research_store.release_benchmark": "firecrawl_skill.research_store.release.benchmark",
    "firecrawl_skill.research_store.release_evidence": "firecrawl_skill.research_store.release.evidence",
    "firecrawl_skill.research_store.strict_benchmark": "firecrawl_skill.research_store.release.strict",
    "firecrawl_skill.research_store.workflow_benchmark": "firecrawl_skill.research_store.release.workflow",
    "firecrawl_skill.research_store.orchestration.composition": "firecrawl_skill.research_store.composition",
    "firecrawl_skill.research_store.retrieval_service": "firecrawl_skill.research_store.retrieval.service",
    "firecrawl_skill.research_store.postgres_retrieval": "firecrawl_skill.research_store.retrieval.postgres",
    "firecrawl_skill.research_store.qdrant": "firecrawl_skill.research_store.retrieval.projection.qdrant",
    "firecrawl_skill.research_store.qdrant_authority": "firecrawl_skill.research_store.retrieval.projection.qdrant_authority",
    "firecrawl_skill.research_store.projection_reconciliation": "firecrawl_skill.research_store.retrieval.projection.reconciliation",
    "firecrawl_skill.research_store.indexing": "firecrawl_skill.research_store.retrieval.projection.indexing",
    "firecrawl_skill.research_store.checkpoint_indexing_stage": "firecrawl_skill.research_store.retrieval.projection.checkpoint_indexing_stage",
    "firecrawl_skill.research_store.index_checkpoint_asset_membership": "firecrawl_skill.research_store.retrieval.projection.index_checkpoint_asset_membership",
    "firecrawl_skill.research_store.index_checkpoint_core": "firecrawl_skill.research_store.retrieval.projection.index_checkpoint_core",
    "firecrawl_skill.research_store.index_checkpoint_finalize": "firecrawl_skill.research_store.retrieval.projection.index_checkpoint_finalize",
    "firecrawl_skill.research_store.index_checkpoint_models": "firecrawl_skill.research_store.retrieval.projection.index_checkpoint_models",
    "firecrawl_skill.research_store.index_checkpoint_replay": "firecrawl_skill.research_store.retrieval.projection.index_checkpoint_replay",
    "firecrawl_skill.research_store.index_checkpoint_service": "firecrawl_skill.research_store.retrieval.projection.index_checkpoint_service",
    "firecrawl_skill.research_store.index_checkpoint_store": "firecrawl_skill.research_store.retrieval.projection.index_checkpoint_store",
}

# Facades whose symbols moved to more than one final owner.
SYMBOL_MAP = {
    # generic service.py aggregation
    ("firecrawl_skill.research_store.service", "AuditService"): ("firecrawl_skill.research_store.assessment.audit", "AuditService"),
    ("firecrawl_skill.research_store.service", "compute_audit_identity_hash"): ("firecrawl_skill.research_store.assessment.audit", "compute_audit_identity_hash"),
    ("firecrawl_skill.research_store.service", "resolve_model_fingerprint"): ("firecrawl_skill.research_store.assessment.audit", "resolve_model_fingerprint"),
    ("firecrawl_skill.research_store.service", "ClaimManifestService"): ("firecrawl_skill.research_store.assessment.claims", "ClaimManifestService"),
    ("firecrawl_skill.research_store.service", "CorpusService"): ("firecrawl_skill.research_store.corpus_service", "CorpusService"),
    ("firecrawl_skill.research_store.service", "ParsedContent"): ("firecrawl_skill.research_store.corpus_service", "ParsedContent"),
    ("firecrawl_skill.research_store.service", "PreparedIngest"): ("firecrawl_skill.research_store.corpus_service", "PreparedIngest"),
    ("firecrawl_skill.research_store.service", "IngestRequest"): ("firecrawl_skill.research_store.domain", "IngestRequest"),
    ("firecrawl_skill.research_store.service", "IngestResult"): ("firecrawl_skill.research_store.domain", "IngestResult"),
    ("firecrawl_skill.research_store.service", "dumps"): ("firecrawl_skill.research_store.export_serialization", "dumps"),
    ("firecrawl_skill.research_store.service", "json_default"): ("firecrawl_skill.research_store.export_serialization", "json_default"),
    # acquisition_service.py
    ("firecrawl_skill.research_store.acquisition_service", "FirecrawlSearchAdapter"): ("firecrawl_skill.research_store.acquisition.adapters.bounded_firecrawl", "BoundedFirecrawlSearchAdapter"),
    # direct_scrape_service.py and acquisition.direct_scrape
    ("firecrawl_skill.research_store.direct_scrape_service", "FirecrawlDirectScrapeAdapter"): ("firecrawl_skill.research_store.acquisition.adapters.firecrawl_scrape", "FirecrawlDirectScrapeAdapter"),
    ("firecrawl_skill.research_store.direct_scrape_service", "build_direct_scrape_service"): ("firecrawl_skill.research_store.composition", "build_direct_scrape_service"),
    ("firecrawl_skill.research_store.acquisition.direct_scrape", "build_direct_scrape_service"): ("firecrawl_skill.research_store.composition", "build_direct_scrape_service"),
    # root compatibility aliases
    ("firecrawl_skill.research_store", "FirecrawlSearchAdapter"): ("firecrawl_skill.research_store.acquisition.adapters.bounded_firecrawl", "BoundedFirecrawlSearchAdapter"),
    ("firecrawl_skill.research_store.ports", "SearchAdapter"): ("firecrawl_skill.research_store.acquisition.ports", "SearchAdapter"),
}

for legacy in (
    "firecrawl_skill.research_store.direct_scrape_service",
    "firecrawl_skill.research_store.acquisition.direct_scrape",
):
    for symbol in (
        "DIRECT_SCRAPE_TABLE_PRIVILEGES",
        "DirectScrapeError",
        "DirectScrapePersistenceError",
        "DirectScrapeService",
        "_ResolvedTarget",
        "require_direct_scrape_persistence",
    ):
        SYMBOL_MAP[(legacy, symbol)] = (
            "firecrawl_skill.research_store.acquisition.direct_scrape_application",
            symbol,
        )
    for symbol in (
        "DirectScrapeBatchResult",
        "DirectScrapeItemResult",
        "DirectScrapeRequest",
        "ScrapeTransportResult",
    ):
        SYMBOL_MAP[(legacy, symbol)] = (
            "firecrawl_skill.research_store.acquisition.models",
            symbol,
        )

for symbol in (
    "AcquisitionAuthorityChangedError",
    "AcquisitionConcurrencyError",
    "AcquisitionIdempotencyConflictError",
    "AcquisitionResult",
    "AcquisitionService",
    "SearchProvenanceError",
):
    SYMBOL_MAP[("firecrawl_skill.research_store.acquisition_service", symbol)] = (
        "firecrawl_skill.research_store.acquisition.service",
        symbol,
    )

FORBIDDEN_PATHS = (
    SCRIPTS / "budget_policy.py",
    SCRIPTS / "candidate_ranking.py",
    SCRIPTS / "classifier.py",
    SCRIPTS / "model_gateway.py",
    STORE / "service.py",
    STORE / "container.py",
    STORE / "coverage_service.py",
    STORE / "quality_service.py",
    STORE / "duplicate_service.py",
    STORE / "evidence_grouping.py",
    STORE / "audit_packet.py",
    STORE / "evidence.py",
    STORE / "claim_binding_service.py",
    STORE / "packet_validator.py",
    STORE / "report_service.py",
    STORE / "report_validator.py",
    STORE / "report_artifact_service.py",
    STORE / "acquisition_authority.py",
    STORE / "acquisition_service.py",
    STORE / "bounded_acquisition.py",
    STORE / "direct_scrape_service.py",
    STORE / "acquisition" / "direct_scrape.py",
    STORE / "benchmark_admin.py",
    STORE / "preflight.py",
    STORE / "release_benchmark.py",
    STORE / "release_evidence.py",
    STORE / "strict_benchmark.py",
    STORE / "workflow_benchmark.py",
    STORE / "orchestration" / "composition.py",
    STORE / "cli.py",
    STORE / "retrieval.py",
    STORE / "retrieval_core.py",
    STORE / "retrieval_service.py",
    STORE / "postgres_retrieval.py",
    STORE / "qdrant.py",
    STORE / "qdrant_authority.py",
    STORE / "projection_reconciliation.py",
    STORE / "indexing.py",
    STORE / "checkpoint_indexing_stage.py",
    STORE / "index_checkpoint_asset_membership.py",
    STORE / "index_checkpoint_core.py",
    STORE / "index_checkpoint_finalize.py",
    STORE / "index_checkpoint_models.py",
    STORE / "index_checkpoint_replay.py",
    STORE / "index_checkpoint_service.py",
    STORE / "index_checkpoint_store.py",
)

FORBIDDEN_MODULES = set(BARE_MODULE_MAP) | set(MODULE_MAP) | {
    "firecrawl_skill.research_store.service",
    "firecrawl_skill.research_store.acquisition_service",
    "firecrawl_skill.research_store.direct_scrape_service",
    "firecrawl_skill.research_store.acquisition.direct_scrape",
}

PATH_MAP = {
    "src/firecrawl_skill/research_store/container.py": "src/firecrawl_skill/research_store/composition.py",
    "src/firecrawl_skill/research_store/coverage_service.py": "src/firecrawl_skill/research_store/assessment/coverage.py",
    "src/firecrawl_skill/research_store/quality_service.py": "src/firecrawl_skill/research_store/assessment/quality.py",
    "src/firecrawl_skill/research_store/duplicate_service.py": "src/firecrawl_skill/research_store/assessment/duplicates.py",
    "src/firecrawl_skill/research_store/evidence_grouping.py": "src/firecrawl_skill/research_store/assessment/grouping.py",
    "src/firecrawl_skill/research_store/audit_packet.py": "src/firecrawl_skill/research_store/assessment/audit_packet.py",
    "src/firecrawl_skill/research_store/evidence.py": "src/firecrawl_skill/research_store/assessment/evidence.py",
    "src/firecrawl_skill/research_store/claim_binding_service.py": "src/firecrawl_skill/research_store/assessment/binding.py",
    "src/firecrawl_skill/research_store/packet_validator.py": "src/firecrawl_skill/research_store/assessment/validation.py",
    "src/firecrawl_skill/research_store/report_service.py": "src/firecrawl_skill/research_store/reporting/construction.py",
    "src/firecrawl_skill/research_store/report_validator.py": "src/firecrawl_skill/research_store/reporting/validation.py",
    "src/firecrawl_skill/research_store/report_artifact_service.py": "src/firecrawl_skill/research_store/reporting/artifacts.py",
    "src/firecrawl_skill/research_store/acquisition_authority.py": "src/firecrawl_skill/research_store/acquisition/authority.py",
    "src/firecrawl_skill/research_store/acquisition_service.py": "src/firecrawl_skill/research_store/acquisition/service.py",
    "src/firecrawl_skill/research_store/bounded_acquisition.py": "src/firecrawl_skill/research_store/acquisition/adapters/bounded_firecrawl.py",
    "src/firecrawl_skill/research_store/direct_scrape_service.py": "src/firecrawl_skill/research_store/acquisition/direct_scrape_application.py",
    "src/firecrawl_skill/research_store/acquisition/direct_scrape.py": "src/firecrawl_skill/research_store/acquisition/direct_scrape_application.py",
    "src/firecrawl_skill/research_store/benchmark_admin.py": "src/firecrawl_skill/research_store/release/admin.py",
    "src/firecrawl_skill/research_store/preflight.py": "src/firecrawl_skill/research_store/release/preflight.py",
    "src/firecrawl_skill/research_store/release_benchmark.py": "src/firecrawl_skill/research_store/release/benchmark.py",
    "src/firecrawl_skill/research_store/release_evidence.py": "src/firecrawl_skill/research_store/release/evidence.py",
    "src/firecrawl_skill/research_store/strict_benchmark.py": "src/firecrawl_skill/research_store/release/strict.py",
    "src/firecrawl_skill/research_store/workflow_benchmark.py": "src/firecrawl_skill/research_store/release/workflow.py",
    "src/firecrawl_skill/research_store/orchestration/composition.py": "src/firecrawl_skill/research_store/composition.py",
    "src/firecrawl_skill/research_store/cli.py": "src/firecrawl_skill/research_store/cli/__init__.py",
    "src/firecrawl_skill/research_store/retrieval_service.py": "src/firecrawl_skill/research_store/retrieval/service.py",
    "src/firecrawl_skill/research_store/postgres_retrieval.py": "src/firecrawl_skill/research_store/retrieval/postgres.py",
    "src/firecrawl_skill/research_store/qdrant.py": "src/firecrawl_skill/research_store/retrieval/projection/qdrant.py",
    "src/firecrawl_skill/research_store/qdrant_authority.py": "src/firecrawl_skill/research_store/retrieval/projection/qdrant_authority.py",
    "src/firecrawl_skill/research_store/projection_reconciliation.py": "src/firecrawl_skill/research_store/retrieval/projection/reconciliation.py",
    "src/firecrawl_skill/research_store/indexing.py": "src/firecrawl_skill/research_store/retrieval/projection/indexing.py",
    "src/firecrawl_skill/research_store/checkpoint_indexing_stage.py": "src/firecrawl_skill/research_store/retrieval/projection/checkpoint_indexing_stage.py",
    "scripts/model_gateway.py": "src/firecrawl_skill/model_gateway.py",
}
for stem in (
    "index_checkpoint_asset_membership",
    "index_checkpoint_core",
    "index_checkpoint_finalize",
    "index_checkpoint_models",
    "index_checkpoint_replay",
    "index_checkpoint_service",
    "index_checkpoint_store",
):
    PATH_MAP[f"src/firecrawl_skill/research_store/{stem}.py"] = (
        f"src/firecrawl_skill/research_store/retrieval/projection/{stem}.py"
    )


class Edit(NamedTuple):
    start: int
    end: int
    replacement: str


def _git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def _require_clean_exact_head(expected: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", expected):
        raise SystemExit("--expected-head must be a 40-character lowercase SHA")
    actual = _git("rev-parse", "HEAD")
    if actual != expected:
        raise SystemExit(f"HEAD mismatch: {actual} != {expected}")
    status = _git("status", "--porcelain")
    if status:
        raise SystemExit("worktree must be clean before issue #269 finalization")


def _module_for_path(path: Path) -> tuple[str, str] | None:
    try:
        relative = path.relative_to(ROOT / "src")
    except ValueError:
        return None
    if path.suffix != ".py":
        return None
    parts = list(relative.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
        module = ".".join(parts)
        package = module
    else:
        module = ".".join(parts)
        package = ".".join(parts[:-1])
    return module, package


def _resolve_from(path: Path, node: ast.ImportFrom) -> str | None:
    if node.level == 0:
        return node.module
    info = _module_for_path(path)
    if info is None:
        return None
    _module, package = info
    parts = package.split(".") if package else []
    ascend = node.level - 1
    if ascend > len(parts):
        return None
    base = parts[: len(parts) - ascend]
    if node.module:
        base.extend(node.module.split("."))
    return ".".join(base)


def _mapped_module(name: str) -> str:
    return BARE_MODULE_MAP.get(name, MODULE_MAP.get(name, name))


def _alias_text(name: str, bound_name: str | None = None) -> str:
    return f"{name} as {bound_name}" if bound_name else name


def _rewrite_import_node(path: Path, node: ast.Import | ast.ImportFrom) -> str | None:
    if isinstance(node, ast.Import):
        changed = False
        aliases: list[str] = []
        for alias in node.names:
            target = _mapped_module(alias.name)
            changed |= target != alias.name
            if target != alias.name and alias.asname is None:
                aliases.append(_alias_text(target, alias.name.split(".")[0]))
            else:
                aliases.append(_alias_text(target, alias.asname))
        return "import " + ", ".join(aliases) if changed else None

    absolute = _resolve_from(path, node)
    if not absolute:
        return None

    # If the imported name itself identifies a migrated module (for example
    # `from firecrawl_skill.research_store import container`), preserve the
    # caller's local binding with a direct module import.
    direct_imports: list[str] = []
    grouped: dict[str, list[str]] = defaultdict(list)
    changed = False

    for alias in node.names:
        full = f"{absolute}.{alias.name}" if absolute else alias.name
        if full in MODULE_MAP:
            target = MODULE_MAP[full]
            binding = alias.asname or alias.name
            direct_imports.append(f"import {target} as {binding}")
            changed = True
            continue

        symbol_target = SYMBOL_MAP.get((absolute, alias.name))
        if symbol_target is not None:
            target_module, target_symbol = symbol_target
            binding = alias.asname or alias.name
            rendered = target_symbol
            if binding != target_symbol:
                rendered += f" as {binding}"
            grouped[target_module].append(rendered)
            changed = True
            continue

        target_module = _mapped_module(absolute)
        if target_module != absolute:
            grouped[target_module].append(_alias_text(alias.name, alias.asname))
            changed = True
        else:
            grouped[absolute].append(_alias_text(alias.name, alias.asname))

    if not changed:
        return None

    statements = list(direct_imports)
    for module, aliases in grouped.items():
        statements.append(f"from {module} import {', '.join(aliases)}")
    return "\n".join(statements)


def _apply_import_rewrites(path: Path) -> bool:
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return False
    lines = source.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    edits: list[Edit] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        replacement = _rewrite_import_node(path, node)
        if replacement is None:
            continue
        start = offsets[node.lineno - 1] + node.col_offset
        end = offsets[node.end_lineno - 1] + node.end_col_offset
        indent = lines[node.lineno - 1][: node.col_offset]
        replacement = replacement.replace("\n", "\n" + indent)
        edits.append(Edit(start, end, replacement))
    if not edits:
        return False
    for edit in sorted(edits, key=lambda item: item.start, reverse=True):
        source = source[: edit.start] + edit.replacement + source[edit.end :]
    path.write_text(source, encoding="utf-8")
    return True


def _rewrite_string_targets(path: Path) -> bool:
    source = path.read_text(encoding="utf-8")
    original = source
    # Symbol-specific dynamic targets first.
    for (legacy_module, legacy_symbol), (target_module, target_symbol) in sorted(
        SYMBOL_MAP.items(), key=lambda item: len(item[0][0]), reverse=True
    ):
        source = source.replace(
            f"{legacy_module}.{legacy_symbol}", f"{target_module}.{target_symbol}"
        )
    for legacy, target in sorted(MODULE_MAP.items(), key=lambda item: len(item[0]), reverse=True):
        source = source.replace(legacy, target)
    for legacy, target in BARE_MODULE_MAP.items():
        # Restrict bare-name text replacement to importlib/module target strings;
        # ordinary prose/identifiers are left for the AST import rewrite.
        source = source.replace(f'"{legacy}.', f'"{target}.')
        source = source.replace(f"'{legacy}.", f"'{target}.")
        source = source.replace(f'"{legacy}"', f'"{target}"')
        source = source.replace(f"'{legacy}'", f"'{target}'")
    if source == original:
        return False
    path.write_text(source, encoding="utf-8")
    return True


def _python_targets() -> list[Path]:
    result = [*SRC.rglob("*.py"), *TESTS.rglob("*.py"), *SCRIPTS.rglob("*.py")]
    return sorted(path for path in result if path.resolve() != SELF)


def _rewrite_domain_codec() -> None:
    path = SRC / "research_domain" / "assessment.py"
    source = path.read_text(encoding="utf-8")
    old = (
        "        from firecrawl_skill.research_store.assessment.evidence import _to_dict\n\n"
        "        return _to_dict(self)"
    )
    if old not in source:
        # The import rewrite may not have run if the old facade was already gone.
        old = (
            "        from firecrawl_skill.research_store.evidence import _to_dict\n\n"
            "        return _to_dict(self)"
        )
    if old not in source:
        raise RuntimeError("BenchmarkResult.to_dict store dependency not found exactly")
    source = source.replace(
        old,
        "        from .codec import to_dict\n\n        return to_dict(self)",
        1,
    )
    path.write_text(source, encoding="utf-8")


def _move_report_construction() -> None:
    source_path = STORE / "report_service.py"
    target_path = STORE / "reporting" / "construction.py"
    if not source_path.exists():
        raise RuntimeError("report_service.py is required for the prescribed physical move")
    if target_path.exists():
        raise RuntimeError("reporting/construction.py already exists; refusing ambiguous move")
    source = source_path.read_text(encoding="utf-8")
    replacements = {
        "from .authorized_semantic import": "from firecrawl_skill.research_store.authorized_semantic import",
        "from .completion_provenance import": "from firecrawl_skill.research_store.completion_provenance import",
        "from .config import": "from firecrawl_skill.research_store.config import",
        "from .domain import": "from firecrawl_skill.research_store.domain import",
        "from .semantic_cache import": "from firecrawl_skill.research_store.semantic_cache import",
        "from .semantic_service import": "from firecrawl_skill.research_store.semantic_service import",
        "from .assessment.binding import": "from firecrawl_skill.research_store.assessment.binding import",
        "from .reporting.artifacts import": "from firecrawl_skill.research_store.reporting.artifacts import",
        "from .telemetry_service import": "from firecrawl_skill.research_store.telemetry_service import",
    }
    for old, new in replacements.items():
        source = source.replace(old, new)
    old_schema_root = "pathlib.Path(__file__).parent.parent.parent.parent"
    if old_schema_root not in source:
        raise RuntimeError("report schema-root expression was not found exactly")
    source = source.replace(
        old_schema_root, "pathlib.Path(__file__).resolve().parents[4]", 1
    )
    target_path.write_text(source, encoding="utf-8")
    source_path.unlink()


def _remove_package_aliases() -> None:
    init_path = STORE / "__init__.py"
    source = init_path.read_text(encoding="utf-8")
    source = source.replace(
        "from .acquisition.adapters.bounded_firecrawl import BoundedFirecrawlSearchAdapter\n",
        "",
        1,
    )
    alias_block = (
        "# Preserve the root provider alias without mutating acquisition internals at\n"
        "# import time. Production adapter selection is explicit in composition roots.\n"
        "FirecrawlSearchAdapter = BoundedFirecrawlSearchAdapter\n\n"
    )
    if alias_block not in source:
        raise RuntimeError("root FirecrawlSearchAdapter compatibility block not found")
    source = source.replace(alias_block, "", 1)
    source = source.replace('    "FirecrawlSearchAdapter",\n', "", 1)
    init_path.write_text(source, encoding="utf-8")

    ports_path = STORE / "ports.py"
    ports = ports_path.read_text(encoding="utf-8")
    legacy = "from .acquisition.ports import SearchAdapter  # noqa: F401\n"
    if legacy not in ports:
        raise RuntimeError("research_store.ports SearchAdapter compatibility alias not found")
    ports_path.write_text(ports.replace(legacy, "", 1), encoding="utf-8")


def _clean_workflow_paths() -> None:
    for path in sorted(WORKFLOWS.glob("*.yml")):
        source = path.read_text(encoding="utf-8")
        original = source
        for old, new in sorted(PATH_MAP.items(), key=lambda item: len(item[0]), reverse=True):
            source = source.replace(old, new)
        # Generic service.py has no one-to-one workflow owner. Canonical
        # assessment/reporting globs already cover those slices.
        source = source.replace(
            '      - "src/firecrawl_skill/research_store/service.py"\n', ""
        )
        # Collapse exact duplicate path-filter lines introduced by many-to-one
        # migration mappings without otherwise reformatting YAML.
        output: list[str] = []
        seen_path_lines: set[str] = set()
        for line in source.splitlines(keepends=True):
            stripped = line.strip()
            if stripped.startswith('- "') and stripped.endswith('"'):
                if line in seen_path_lines:
                    continue
                seen_path_lines.add(line)
            output.append(line)
        source = "".join(output)
        if source != original:
            path.write_text(source, encoding="utf-8")


def _delete_obsolete_paths() -> list[str]:
    deleted: list[str] = []
    # report_service.py was already moved; all other entries are direct deletes.
    for path in FORBIDDEN_PATHS:
        if not path.exists():
            continue
        path.unlink()
        deleted.append(path.relative_to(ROOT).as_posix())
    return deleted


def _prune_deleted_baseline_paths() -> int:
    data = json.loads(BASELINE.read_text(encoding="utf-8"))
    errors = data.get("errors")
    if not isinstance(errors, list):
        raise RuntimeError("unexpected pyrefly baseline schema")
    kept = []
    removed = 0
    for item in errors:
        rel = item.get("path") if isinstance(item, dict) else None
        if isinstance(rel, str) and not (ROOT / rel).exists():
            removed += 1
            continue
        kept.append(item)
    data["errors"] = kept
    BASELINE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return removed


def _resolved_import_modules(path: Path, tree: ast.AST) -> list[str]:
    result: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            absolute = _resolve_from(path, node)
            if absolute:
                result.append(absolute)
                result.extend(f"{absolute}.{alias.name}" for alias in node.names)
    return result


def _verify_final_state() -> list[str]:
    violations: list[str] = []
    for path in FORBIDDEN_PATHS:
        if path.exists():
            violations.append(f"obsolete path remains: {path.relative_to(ROOT)}")

    for path in _python_targets():
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            violations.append(f"syntax error after rewrite: {path.relative_to(ROOT)}: {exc}")
            continue
        for module in _resolved_import_modules(path, tree):
            if any(module == old or module.startswith(old + ".") for old in FORBIDDEN_MODULES):
                violations.append(f"legacy import: {path.relative_to(ROOT)} -> {module}")
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            value = node.value
            if any(value == old or value.startswith(old + ".") for old in FORBIDDEN_MODULES):
                violations.append(f"legacy dynamic target: {path.relative_to(ROOT)} -> {value}")

    init_source = (STORE / "__init__.py").read_text(encoding="utf-8")
    if "FirecrawlSearchAdapter" in init_source:
        violations.append("root FirecrawlSearchAdapter compatibility alias remains")
    ports_source = (STORE / "ports.py").read_text(encoding="utf-8")
    if "from .acquisition.ports import SearchAdapter" in ports_source:
        violations.append("research_store.ports SearchAdapter compatibility alias remains")

    diff_check = subprocess.run(
        ["git", "diff", "--check"], cwd=ROOT, text=True, capture_output=True
    )
    if diff_check.returncode:
        violations.append("git diff --check failed:\n" + diff_check.stdout + diff_check.stderr)
    return violations


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-head", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    _require_clean_exact_head(args.expected_head)
    if not args.apply:
        print(
            json.dumps(
                {
                    "status": "ready",
                    "head": args.expected_head,
                    "forbidden_paths": len(FORBIDDEN_PATHS),
                    "message": "rerun with --apply to execute the Central-owned migration",
                },
                indent=2,
            )
        )
        return 0

    changed_import_files = 0
    changed_target_files = 0
    for path in _python_targets():
        changed_import_files += int(_apply_import_rewrites(path))
    for path in _python_targets():
        changed_target_files += int(_rewrite_string_targets(path))

    _rewrite_domain_codec()
    _move_report_construction()
    _remove_package_aliases()
    _clean_workflow_paths()
    deleted = _delete_obsolete_paths()
    baseline_removed = _prune_deleted_baseline_paths()

    violations = _verify_final_state()
    summary = {
        "status": "failed" if violations else "finalized",
        "exact_head_before_mutation": args.expected_head,
        "import_rewrite_files": changed_import_files,
        "dynamic_target_rewrite_files": changed_target_files,
        "deleted_paths": deleted,
        "deleted_pyrefly_baseline_records": baseline_removed,
        "violations": violations,
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    if violations:
        return 1

    # This migration helper is itself temporary scaffolding.  Delete it only
    # after the final-state verification succeeds so `git add -A` records its
    # retirement in the same local mechanical commit.
    SELF.unlink()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
