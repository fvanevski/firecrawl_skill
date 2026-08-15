"""Regression tests for the structural-refactor architecture inventory."""

from __future__ import annotations

import importlib.util
import io
import subprocess
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BASELINE_SHA = "c730b562343e10193fecaf4684925dcee0dc1403"

_SPEC = importlib.util.spec_from_file_location(
    "architecture_inventory",
    REPO_ROOT / "tools" / "architecture_inventory.py",
)
assert _SPEC is not None and _SPEC.loader is not None
architecture_inventory = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(architecture_inventory)


def _rows_by_path(inventory):
    columns = inventory["module_columns"]
    return {
        row[0]: dict(zip(columns, row, strict=True)) for row in inventory["modules"]
    }


def _materialize_baseline_scripts(tmp_path: Path) -> None:
    # Reproduce the immutable baseline from its exact historical source tree rather
    # than comparing it to the legitimately evolving current working tree.
    result = subprocess.run(
        [
            "git",
            "-C",
            str(REPO_ROOT),
            "archive",
            "--format=zip",
            BASELINE_SHA,
            "scripts",
        ],
        check=True,
        stdout=subprocess.PIPE,
    )
    with zipfile.ZipFile(io.BytesIO(result.stdout)) as archive:
        archive.extractall(tmp_path)


def test_checked_in_architecture_baseline_matches_exact_source_tree(tmp_path):
    _materialize_baseline_scripts(tmp_path)
    generated = architecture_inventory.render_inventory(
        architecture_inventory.build_inventory(tmp_path, BASELINE_SHA)
    )
    checked_in = (REPO_ROOT / "references" / "architecture-baseline.json").read_text(
        encoding="utf-8"
    )

    assert checked_in == generated


def test_architecture_inventory_generator_is_deterministic_for_current_tree():
    source_sha = "f" * 40
    first = architecture_inventory.render_inventory(
        architecture_inventory.build_inventory(REPO_ROOT, source_sha)
    )
    second = architecture_inventory.render_inventory(
        architecture_inventory.build_inventory(REPO_ROOT, source_sha)
    )

    assert first == second


def test_fan_in_counts_resolved_edges_from_ambiguous_source_paths(tmp_path):
    scripts = tmp_path / "scripts"
    package = scripts / "collision"
    package.mkdir(parents=True)

    (scripts / "collision.py").write_text("import target\n", encoding="utf-8")
    (package / "__init__.py").write_text("import target\n", encoding="utf-8")
    (scripts / "consumer.py").write_text("import collision\n", encoding="utf-8")
    (scripts / "target.py").write_text("VALUE = 1\n", encoding="utf-8")

    inventory = architecture_inventory.build_inventory(tmp_path, "a" * 40)
    rows = _rows_by_path(inventory)

    assert inventory["ambiguous_module_names"] == ["collision"]
    assert rows["scripts/collision.py"]["local_imports"] == ["target"]
    assert rows["scripts/collision/__init__.py"]["local_imports"] == ["target"]
    assert rows["scripts/target.py"]["fan_in"] == 2
    assert rows["scripts/consumer.py"]["local_imports"] == []
    assert rows["scripts/collision.py"]["fan_in"] is None
    assert rows["scripts/collision/__init__.py"]["fan_in"] is None
