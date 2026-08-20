#!/usr/bin/env python3
"""Compare final Phase-5 structural metrics with the immutable Phase-1 baseline.

This tool is evidence-only. It intentionally does not impose LOC or module-count
thresholds and never rewrites ``references/architecture-baseline.json``.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "phase5-architecture-comparison-v1"
SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
SIZE_BANDS = (
    ("under_200", 0, 199),
    ("200_449", 200, 449),
    ("450_599", 450, 599),
    ("600_699", 600, 699),
    ("700_plus", 700, None),
)


def _is_python_source(path: Path) -> bool:
    if path.suffix == ".py":
        return True
    if path.suffix:
        return False
    try:
        first_line = path.read_bytes().splitlines()[0].lower()
    except (OSError, IndexError):
        return False
    return first_line.startswith(b"#!") and b"python" in first_line


def _script_is_in_scope(path: Path, scripts_root: Path) -> bool:
    rel = path.relative_to(scripts_root)
    if not _is_python_source(path):
        return False
    if path.name == "conftest.py" or path.name.startswith("test_"):
        return False
    if path.name.endswith("_test_support.py") or "fixtures" in rel.parts:
        return False
    return True


def _current_paths(root: Path) -> list[Path]:
    src_root = root / "src" / "firecrawl_skill"
    scripts_root = root / "scripts"
    if not src_root.is_dir():
        raise ValueError(f"missing canonical source directory: {src_root}")
    if not scripts_root.is_dir():
        raise ValueError(f"missing operator tooling directory: {scripts_root}")

    src_paths = [path for path in src_root.rglob("*.py") if path.is_file()]
    script_paths = [
        path
        for path in scripts_root.rglob("*")
        if path.is_file() and _script_is_in_scope(path, scripts_root)
    ]
    return sorted({*src_paths, *script_paths})


def _symbol_count(path: Path, text: str) -> int:
    tree = ast.parse(text, filename=str(path))
    return sum(
        isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        for node in tree.body
    )


def _size_bands(loc_values: list[int]) -> dict[str, int]:
    result: dict[str, int] = {}
    for name, lower, upper in SIZE_BANDS:
        result[name] = sum(
            loc >= lower and (upper is None or loc <= upper) for loc in loc_values
        )
    return result


def _summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    loc_values = [int(record["physical_loc"]) for record in records]
    largest = sorted(
        records,
        key=lambda record: (-int(record["physical_loc"]), str(record["path"])),
    )[:20]
    return {
        "module_count": len(records),
        "physical_loc_total": sum(loc_values),
        "top_level_symbol_count": sum(
            int(record["top_level_symbol_count"]) for record in records
        ),
        "size_bands": _size_bands(loc_values),
        "largest_modules": [
            {
                "path": str(record["path"]),
                "physical_loc": int(record["physical_loc"]),
                "top_level_symbol_count": int(record["top_level_symbol_count"]),
            }
            for record in largest
        ],
    }


def _baseline_records(data: dict[str, Any]) -> list[dict[str, Any]]:
    if data.get("schema_version") != "architecture-inventory-v1":
        raise ValueError("unsupported Phase-1 architecture baseline schema")
    columns = data.get("module_columns")
    modules = data.get("modules")
    if not isinstance(columns, list) or not isinstance(modules, list):
        raise ValueError("malformed Phase-1 architecture baseline")
    index = {str(name): offset for offset, name in enumerate(columns)}
    required = {"path", "physical_loc", "top_level_symbols"}
    if not required.issubset(index):
        raise ValueError("Phase-1 baseline is missing required columns")

    records: list[dict[str, Any]] = []
    for row in modules:
        if not isinstance(row, list):
            raise ValueError("malformed Phase-1 module row")
        symbols = row[index["top_level_symbols"]]
        records.append(
            {
                "path": str(row[index["path"]]),
                "physical_loc": int(row[index["physical_loc"]]),
                "top_level_symbol_count": (
                    len(symbols) if isinstance(symbols, list) else 0
                ),
            }
        )
    return records


def _current_records(root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in _current_paths(root):
        text = path.read_text(encoding="utf-8")
        records.append(
            {
                "path": path.relative_to(root).as_posix(),
                "physical_loc": len(text.splitlines()),
                "top_level_symbol_count": _symbol_count(path, text),
            }
        )
    return records


def build_comparison(
    root: Path,
    *,
    baseline_path: Path,
    current_source_sha: str,
) -> dict[str, Any]:
    if not SHA_RE.fullmatch(current_source_sha):
        raise ValueError(
            "current_source_sha must be an exact 40-character hexadecimal commit SHA"
        )

    root = root.resolve()
    baseline_file = (
        baseline_path if baseline_path.is_absolute() else root / baseline_path
    )
    baseline = json.loads(baseline_file.read_text(encoding="utf-8"))
    baseline_sha = str(baseline.get("source_sha") or "")
    if not SHA_RE.fullmatch(baseline_sha):
        raise ValueError("Phase-1 baseline source_sha is not an exact commit SHA")

    baseline_summary = _summary(_baseline_records(baseline))
    current_summary = _summary(_current_records(root))
    delta = {
        "module_count": current_summary["module_count"]
        - baseline_summary["module_count"],
        "physical_loc_total": current_summary["physical_loc_total"]
        - baseline_summary["physical_loc_total"],
        "top_level_symbol_count": current_summary["top_level_symbol_count"]
        - baseline_summary["top_level_symbol_count"],
    }

    return {
        "schema_version": SCHEMA_VERSION,
        "evidence_policy": (
            "review evidence only; module counts, LOC, symbol counts, and size bands "
            "are not acceptance thresholds"
        ),
        "baseline": {
            "source_sha": baseline_sha.lower(),
            "scope": baseline.get("scope"),
            "summary": baseline_summary,
        },
        "current": {
            "source_sha": current_source_sha.lower(),
            "scope": {
                "included": [
                    "src/firecrawl_skill/**/*.py canonical production package",
                    (
                        "scripts/ Python operator/tooling sources, including "
                        "extensionless Python entrypoints"
                    ),
                ],
                "excluded": [
                    "tests/",
                    "scripts/test_*.py and nested test_*.py",
                    "scripts/conftest.py",
                    "scripts/*_test_support.py and nested *_test_support.py",
                    "scripts/fixtures/",
                ],
                "physical_loc_definition": "len(file_text.splitlines())",
                "top_level_symbol_definition": (
                    "module-level class/function/async-function definitions"
                ),
            },
            "summary": current_summary,
        },
        "delta_current_minus_baseline": delta,
        "interpretation": {
            "scope_transition": (
                "Phase 1 measured production/maintenance Python under scripts/. "
                "Phase 5 measures the canonical src/firecrawl_skill package plus "
                "remaining Python operator/tooling entrypoints under scripts/."
            ),
            "required_review": (
                "Use the comparison to discuss semantic locality and large-module "
                "review triggers; do not infer structural correctness from LOC alone."
            ),
        },
    }


def render_comparison(comparison: dict[str, Any]) -> str:
    return json.dumps(comparison, indent=2, sort_keys=True) + "\n"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument(
        "--baseline",
        type=Path,
        default=Path("references/architecture-baseline.json"),
    )
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--output", type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    rendered = render_comparison(
        build_comparison(
            args.root,
            baseline_path=args.baseline,
            current_source_sha=args.source_sha,
        )
    )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
