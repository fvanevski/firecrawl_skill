#!/usr/bin/env python3
"""Replay recorded Firecrawl CLI responses without network access."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys


def load_fixture(path: str | Path) -> dict:
    fixture = json.loads(Path(path).read_text(encoding="utf-8"))
    if fixture.get("schema_version") != "firecrawl-legacy-replay-v1":
        raise ValueError("unsupported replay fixture schema")
    return fixture


def _output_path(args: list[str]) -> Path:
    if "-o" not in args:
        raise ValueError("replay command requires -o")
    return Path(args[args.index("-o") + 1])


def _search(fixture: dict, args: list[str], output: Path) -> int:
    query = args[1]
    expected = fixture["search"]["query"]
    if query != expected and not os.environ.get("FIRECRAWL_REPLAY_ALLOW_ANY_QUERY"):
        print(f"fixture query mismatch: {query!r} != {expected!r}", file=sys.stderr)
        return 3
    failure = fixture.get("failures", {}).get("search")
    if failure:
        print(failure["message"], file=sys.stderr)
        return int(failure.get("exit_code", 1))
    output.write_text(
        json.dumps(fixture["search"]["response"], sort_keys=True),
        encoding="utf-8",
    )
    return 0


def _scrape(fixture: dict, args: list[str], output: Path) -> int:
    url = args[1]
    recorded = {item["url"]: item for item in fixture.get("scrapes", [])}
    item = recorded.get(url)
    if item is None:
        print(f"no recorded scrape for {url}", file=sys.stderr)
        return 4
    if item.get("error"):
        print(item["error"], file=sys.stderr)
        return int(item.get("exit_code", 1))
    output.write_text(item["content"], encoding="utf-8")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"--version", "version"}:
        print("fixture-replay-v1")
        return 0
    fixture_path = os.environ.get("FIRECRAWL_REPLAY_FIXTURE")
    if not fixture_path:
        print("FIRECRAWL_REPLAY_FIXTURE is required", file=sys.stderr)
        return 2
    fixture = load_fixture(fixture_path)
    try:
        output = _output_path(args)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    output.parent.mkdir(parents=True, exist_ok=True)
    if args[0] == "search":
        return _search(fixture, args, output)
    if args[0] == "scrape":
        return _scrape(fixture, args, output)
    print(f"unsupported replay command: {args[0]}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
