"""Run the authoritative two-mode release campaign and normalize its artifacts.

The underlying strict benchmark supports additional modes for non-release uses.
This entry point freezes the release contract to exactly two modes, binds the
serialized evidence to authoritative PostgreSQL records, replaces
workload-dependent embedding throughput with a fixed release calibration, and
emits self-validating PostgreSQL-derived timing diagnostics.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from release_campaign_contract import (
    repair_campaign_contract,
    validate_campaign_contract,
)
from release_campaign_timing import (
    TIMING_DIAGNOSTICS_SCHEMA as _TIMING_DIAGNOSTICS_SCHEMA,
)
from release_campaign_timing import write_timing_diagnostics

from firecrawl_skill.research_store import strict_benchmark

AUTHORITATIVE_MODES = ("autonomous_local", "deterministic_debug")
TIMING_DIAGNOSTICS_SCHEMA = _TIMING_DIAGNOSTICS_SCHEMA


def _load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"expected JSON object: {path}")
    return value


def normalize_mode_metadata(campaign_dir: Path) -> None:
    """Bind raw manifest and environment metadata to the executed modes."""
    manifest_path = campaign_dir / "manifest.json"
    if not manifest_path.is_file():
        return

    manifest = _load_object(manifest_path)
    manifest["modes"] = list(AUTHORITATIVE_MODES)

    for key, label in (("campaign_a", "A"), ("campaign_b", "B")):
        entry = manifest.get(key)
        if not isinstance(entry, dict):
            continue
        result_dir = Path(str(entry.get("result_path") or ""))
        if not result_dir.is_dir():
            candidates = sorted((campaign_dir / label).glob("*/result.json"))
            if not candidates:
                continue
            result_dir = candidates[-1].parent
            entry["result_path"] = str(result_dir)

        environment_path = result_dir / "environment.json"
        if environment_path.is_file():
            environment = _load_object(environment_path)
            environment["execution_modes"] = list(AUTHORITATIVE_MODES)
            strict_benchmark._write_json_atomic(environment_path, environment)

    strict_benchmark._write_json_atomic(manifest_path, manifest)


def _campaign_dir_from_argv(argv: list[str]) -> Path:
    for index, item in enumerate(argv):
        if item == "--campaign-dir" and index + 1 < len(argv):
            return Path(argv[index + 1])
        if item.startswith("--campaign-dir="):
            return Path(item.split("=", 1)[1])
    return Path("/tmp/firecrawl_strict_campaign")


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    campaign_dir = _campaign_dir_from_argv(arguments)
    raw_result = strict_benchmark.main(
        arguments,
        execution_modes=AUTHORITATIVE_MODES,
    )
    normalize_mode_metadata(campaign_dir)

    # Preflight or campaign-construction failures do not produce a manifest and
    # must retain the underlying non-zero result. Completed campaigns are
    # normalized against authoritative state before the workflow decides the
    # execute-step outcome.
    if not (campaign_dir / "manifest.json").is_file():
        return raw_result

    try:
        reproducible = repair_campaign_contract(
            campaign_dir,
            os.environ.get("DATABASE_URL", ""),
        )
        normalize_mode_metadata(campaign_dir)
        timing = write_timing_diagnostics(
            campaign_dir,
            os.environ.get("DATABASE_URL", ""),
        )
        print(
            "Timing diagnostics: "
            f"{timing['run_count']} runs, "
            f"{len(timing['reproducibility_failures'])} reproducibility failure(s)"
        )
        errors = validate_campaign_contract(campaign_dir)
    except Exception as exc:  # noqa: BLE001
        print(
            f"ERROR: release evidence contract correction failed: {exc}",
            file=sys.stderr,
        )
        return 1

    for error in errors:
        print(f"ERROR: {error}", file=sys.stderr)
    if reproducible and not errors:
        print("Release evidence contract correction: PASS")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
