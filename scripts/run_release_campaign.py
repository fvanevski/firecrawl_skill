"""Run the authoritative two-mode release campaign and normalize its artifacts.

The underlying strict benchmark supports additional modes for non-release uses.
This entry point freezes the release contract to exactly two modes and rewrites
only the mode metadata emitted by the legacy serializer so the durable raw
artifacts describe the execution that actually occurred.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from research_store import strict_benchmark

AUTHORITATIVE_MODES = ("autonomous_local", "deterministic_debug")


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
    result = strict_benchmark.main(arguments, execution_modes=AUTHORITATIVE_MODES)
    normalize_mode_metadata(campaign_dir)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
