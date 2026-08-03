#!/usr/bin/env python3
"""Drain durable PostgreSQL index jobs through bounded worker batches."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run research-db worker --once repeatedly until PostgreSQL reports "
            "that no jobs were claimed."
        )
    )
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--max-batches", type=int, default=10_000)
    parser.add_argument(
        "--research-db",
        default=os.environ.get("FIRECRAWL_RESEARCH_DB_COMMAND"),
        help="Path to research-db; defaults to the sibling scripts/research-db.",
    )
    return parser


def _default_runner(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        list(argv),
        check=False,
        capture_output=True,
        text=True,
    )


def _nonnegative_int(payload: dict[str, Any], field: str) -> int:
    value = payload.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"worker result field {field!r} must be a non-negative integer")
    return value


def drain_index_jobs(
    research_db: Path,
    *,
    batch_size: int = 64,
    max_batches: int = 10_000,
    runner: Runner = _default_runner,
) -> int:
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    if max_batches <= 0:
        raise ValueError("max_batches must be positive")

    command = [
        str(research_db),
        "worker",
        "--once",
        "--batch-size",
        str(batch_size),
    ]
    for batch_number in range(1, max_batches + 1):
        completed = runner(command)
        if completed.stdout:
            print(completed.stdout.rstrip())
        if completed.stderr:
            print(completed.stderr.rstrip(), file=sys.stderr)

        try:
            payload = json.loads(completed.stdout)
            if not isinstance(payload, dict):
                raise ValueError("worker result must be a JSON object")
            claimed = _nonnegative_int(payload, "claimed")
            failed = _nonnegative_int(payload, "failed")
            lease_lost = _nonnegative_int(payload, "lease_lost")
        except (json.JSONDecodeError, ValueError) as exc:
            print(f"invalid worker result after batch {batch_number}: {exc}", file=sys.stderr)
            return 1

        if completed.returncode != 0:
            print(
                f"worker batch {batch_number} exited with {completed.returncode}",
                file=sys.stderr,
            )
            return completed.returncode or 1
        if failed or lease_lost:
            print(
                "worker drain stopped because the authoritative result reported "
                f"failed={failed}, lease_lost={lease_lost}",
                file=sys.stderr,
            )
            return 1
        if claimed == 0:
            return 0

    print(
        f"worker drain exceeded --max-batches={max_batches} before reaching claimed=0",
        file=sys.stderr,
    )
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    research_db = (
        Path(args.research_db)
        if args.research_db
        else Path(__file__).resolve().with_name("research-db")
    )
    return drain_index_jobs(
        research_db,
        batch_size=args.batch_size,
        max_batches=args.max_batches,
    )


if __name__ == "__main__":
    raise SystemExit(main())
