"""Report and enforce the three authoritative release-campaign step outcomes."""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path

_OUTCOME_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_LABELS = {
    "execute": "Campaign execution",
    "verify": "Campaign verification",
    "upload": "Campaign artifact upload",
}


def evaluate_outcomes(outcomes: Mapping[str, str]) -> list[str]:
    """Return invalid or non-success outcome keys in deterministic order."""
    failed: list[str] = []
    for key in _LABELS:
        value = outcomes.get(key, "")
        if not _OUTCOME_RE.fullmatch(value) or value != "success":
            failed.append(key)
    return failed


def render_summary(outcomes: Mapping[str, str]) -> str:
    lines = [
        "## Authoritative gate outcomes",
        "",
        "| Step | Outcome |",
        "|---|---|",
    ]
    lines.extend(f"| {key} | {outcomes.get(key, '')} |" for key in _LABELS)
    return "\n".join(lines) + "\n"


def enforce(outcomes: Mapping[str, str], *, summary_file: Path | None) -> int:
    """Emit all diagnostics before returning a process-compatible gate status."""
    summary = render_summary(outcomes)
    if summary_file is not None:
        summary_file.parent.mkdir(parents=True, exist_ok=True)
        with summary_file.open("a", encoding="utf-8") as handle:
            handle.write(summary)

    failed = evaluate_outcomes(outcomes)
    for key, label in _LABELS.items():
        value = outcomes.get(key, "")
        print(f"{key}={value}")
        print(f"::notice title={label} outcome::{value}")
    for key in failed:
        label = _LABELS[key]
        value = outcomes.get(key, "")
        if not _OUTCOME_RE.fullmatch(value):
            print(
                f"::error title={label} outcome is invalid::outcome={value!r}",
                file=sys.stderr,
            )
        else:
            print(
                f"::error title={label} failed::outcome={value}",
                file=sys.stderr,
            )
    return 1 if failed else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", required=True)
    parser.add_argument("--verify", required=True)
    parser.add_argument("--upload", required=True)
    parser.add_argument(
        "--summary-file",
        type=Path,
        default=(
            Path(os.environ["GITHUB_STEP_SUMMARY"])
            if os.environ.get("GITHUB_STEP_SUMMARY")
            else None
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return enforce(
        {"execute": args.execute, "verify": args.verify, "upload": args.upload},
        summary_file=args.summary_file,
    )


if __name__ == "__main__":
    raise SystemExit(main())
