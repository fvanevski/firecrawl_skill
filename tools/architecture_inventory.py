#!/usr/bin/env python3
"""Generate the deterministic structural-refactor Python module baseline."""

from __future__ import annotations

import argparse
import ast
import difflib
import json
import re
from collections import Counter
from pathlib import Path

SCHEMA_VERSION = "architecture-inventory-v1"
SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
MODULE_COLUMNS = [
    "path",
    "module",
    "physical_loc",
    "architectural_category",
    "top_level_symbols",
    "local_imports",
    "fan_out",
    "fan_in",
]


def _is_in_scope(path: Path, scripts_root: Path) -> bool:
    rel = path.relative_to(scripts_root)
    if path.suffix != ".py":
        return False
    if path.name == "conftest.py" or path.name.startswith("test_"):
        return False
    return not (path.name.endswith("_test_support.py") or "fixtures" in rel.parts)


def _module_name(path: Path, scripts_root: Path) -> tuple[str, bool]:
    rel = path.relative_to(scripts_root)
    parts = list(rel.with_suffix("").parts)
    is_package = parts[-1] == "__init__"
    if is_package:
        parts.pop()
    return ".".join(parts), is_package


def _category(rel_path: str) -> str:
    path = rel_path.casefold()
    stem = Path(rel_path).stem.casefold()
    parts = Path(rel_path).parts
    if "alembic" in parts:
        return "migration"
    if rel_path.startswith("scripts/research_domain/") or stem in {"domain", "models"}:
        return "domain-contract"
    if "/cli/" in path or "cli" in stem or stem.endswith("command"):
        return "entrypoint-cli"
    if any(
        token in stem for token in ("repository", "store", "uow", "blob", "persistence")
    ):
        return "persistence"
    if any(
        token in stem
        for token in ("retrieval", "index", "qdrant", "embedding", "rerank")
    ):
        return "retrieval-indexing"
    if any(token in stem for token in ("evidence", "claim", "report", "audit")):
        return "evidence-report-audit"
    if any(token in stem for token in ("release", "benchmark", "campaign")):
        return "release-benchmark"
    if any(
        token in stem
        for token in ("acquisition", "extract", "scrape", "firecrawl", "candidate")
    ):
        return "acquisition-extraction"
    if any(
        token in stem
        for token in (
            "workflow",
            "orchestr",
            "coverage",
            "policy",
            "semantic",
            "planning",
        )
    ):
        return "application-orchestration"
    if path.startswith("scripts/research_store/"):
        return "application-service"
    return "tooling"


def _top_level_symbols(tree: ast.Module) -> list[list[str]]:
    symbols: list[list[str]] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            symbols.append(["class", node.name])
        elif isinstance(node, ast.AsyncFunctionDef):
            symbols.append(["async_function", node.name])
        elif isinstance(node, ast.FunctionDef):
            symbols.append(["function", node.name])
    return symbols


def _match_local_module(candidate: str, module_names: set[str]) -> str | None:
    if candidate in module_names:
        return candidate
    parts = candidate.split(".")
    for end in range(len(parts) - 1, 0, -1):
        prefix = ".".join(parts[:end])
        if prefix in module_names:
            return prefix
    return None


def _resolve_from_base(
    *, module_name: str, is_package: bool, level: int, imported_module: str | None
) -> str:
    if level == 0:
        return imported_module or ""
    package = module_name if is_package else module_name.rpartition(".")[0]
    package_parts = [part for part in package.split(".") if part]
    parent_hops = level - 1
    if parent_hops > len(package_parts):
        return ""
    base_parts = package_parts[: len(package_parts) - parent_hops]
    if imported_module:
        base_parts.extend(imported_module.split("."))
    return ".".join(base_parts)


def _local_imports(
    tree: ast.Module,
    *,
    module_name: str,
    is_package: bool,
    resolvable_module_names: set[str],
) -> list[str]:
    dependencies: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                matched = _match_local_module(alias.name, resolvable_module_names)
                if matched and matched != module_name:
                    dependencies.add(matched)
        elif isinstance(node, ast.ImportFrom):
            base = _resolve_from_base(
                module_name=module_name,
                is_package=is_package,
                level=node.level,
                imported_module=node.module,
            )
            alias_match_found = False
            for alias in node.names:
                candidate = f"{base}.{alias.name}" if base else alias.name
                matched = _match_local_module(candidate, resolvable_module_names)
                if matched and matched != module_name:
                    dependencies.add(matched)
                    alias_match_found = True
            if alias_match_found:
                continue
            matched = (
                _match_local_module(base, resolvable_module_names) if base else None
            )
            if matched and matched != module_name:
                dependencies.add(matched)
    return sorted(dependencies)


def build_inventory(root: Path, source_sha: str) -> dict[str, object]:
    if not SHA_RE.fullmatch(source_sha):
        raise ValueError(
            "source_sha must be an exact 40-character hexadecimal commit SHA"
        )
    root = root.resolve()
    scripts_root = root / "scripts"
    if not scripts_root.is_dir():
        raise ValueError(f"missing scripts directory under {root}")

    paths = sorted(
        path for path in scripts_root.rglob("*.py") if _is_in_scope(path, scripts_root)
    )
    module_meta: dict[Path, tuple[str, bool]] = {
        path: _module_name(path, scripts_root) for path in paths
    }
    module_name_counts = Counter(name for name, _ in module_meta.values() if name)
    ambiguous_module_names = sorted(
        name for name, count in module_name_counts.items() if count > 1
    )
    resolvable_module_names = {
        name for name, count in module_name_counts.items() if count == 1
    }

    records: list[dict[str, object]] = []
    imports_by_module: dict[str, list[str]] = {}
    for path in paths:
        rel_path = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text, filename=rel_path)
        module_name, is_package = module_meta[path]
        local_imports = _local_imports(
            tree,
            module_name=module_name,
            is_package=is_package,
            resolvable_module_names=resolvable_module_names,
        )
        if module_name in resolvable_module_names:
            imports_by_module[module_name] = local_imports
        records.append(
            {
                "path": rel_path,
                "module": module_name,
                "physical_loc": len(text.splitlines()),
                "architectural_category": _category(rel_path),
                "top_level_symbols": _top_level_symbols(tree),
                "local_imports": local_imports,
                "fan_out": len(local_imports),
            }
        )

    fan_in = Counter(
        dependency
        for dependencies in imports_by_module.values()
        for dependency in dependencies
    )
    module_rows = [
        [
            record["path"],
            record["module"],
            record["physical_loc"],
            record["architectural_category"],
            record["top_level_symbols"],
            record["local_imports"],
            record["fan_out"],
            fan_in.get(str(record["module"]), 0)
            if record["module"] in resolvable_module_names
            else None,
        ]
        for record in records
    ]
    categories = Counter(str(record["architectural_category"]) for record in records)
    return {
        "schema_version": SCHEMA_VERSION,
        "source_sha": source_sha.lower(),
        "module_columns": MODULE_COLUMNS,
        "ambiguous_module_names": ambiguous_module_names,
        "scope": {
            "included": "scripts/**/*.py production and maintenance modules",
            "excluded": [
                "scripts/test_*.py and nested test_*.py",
                "conftest.py",
                "*_test_support.py",
                "fixtures/",
            ],
            "physical_loc_definition": "len(file_text.splitlines())",
            "symbol_definition": "[kind, name] for module-level class/function/async-function definitions",
            "import_graph_definition": (
                "AST-resolved imports between uniquely named in-scope modules only; "
                "ambiguous module targets are excluded"
            ),
        },
        "summary": {
            "module_count": len(records),
            "physical_loc_total": sum(
                int(record["physical_loc"]) for record in records
            ),
            "categories": dict(sorted(categories.items())),
        },
        "modules": module_rows,
    }


def render_inventory(inventory: dict[str, object]) -> str:
    return json.dumps(inventory, sort_keys=True, separators=(",", ":")) + "\n"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--source-sha", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--output", type=Path)
    mode.add_argument("--check", type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    rendered = render_inventory(build_inventory(args.root, args.source_sha))
    if args.check:
        actual = args.check.read_text(encoding="utf-8") if args.check.exists() else ""
        if actual == rendered:
            return 0
        diff = difflib.unified_diff(
            actual.splitlines(),
            rendered.splitlines(),
            fromfile=str(args.check),
            tofile="generated-baseline",
            lineterm="",
        )
        print("\n".join(diff))
        return 1
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
        return 0
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
