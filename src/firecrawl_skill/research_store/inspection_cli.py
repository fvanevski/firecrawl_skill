"""Public CLI for database-native history, replay, and corpus inspection."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .composition import build_inspection_service
from .inspection_contract import InspectionError, PageRequest, PassageBounds


class InspectionArgumentError(ValueError):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise InspectionArgumentError(message)


def parser() -> argparse.ArgumentParser:
    root = _Parser(prog="finspect", description="Database-native research inspection")
    root.add_argument("--pretty", action="store_true", help="indent JSON output")
    sub = root.add_subparsers(dest="command", required=True)

    def page(command: argparse.ArgumentParser) -> None:
        command.add_argument("--limit", type=int, default=20)
        command.add_argument("--cursor")

    runs = sub.add_parser("runs", help="list authoritative research runs")
    page(runs)

    invocations = sub.add_parser("invocations", help="list run invocations")
    invocations.add_argument("--run", required=True)
    page(invocations)

    operations = sub.add_parser(
        "operations",
        help="list unified run provider operations without merging persistence identity",
    )
    operations.add_argument("--run", required=True)
    page(operations)

    responses = sub.add_parser(
        "search-responses", help="list authoritative search responses"
    )
    responses.add_argument("--run", required=True)
    page(responses)

    replay = sub.add_parser(
        "replay-search", help="replay one retained search response by stable ID"
    )
    replay.add_argument("search_response_id")
    replay.add_argument("--max-bytes", type=int, default=1_048_576)

    scrape = sub.add_parser(
        "scrape-candidates", help="scrape stable candidate IDs authoritatively"
    )
    scrape.add_argument("candidate_ids", nargs="+")
    scrape.add_argument(
        "--format",
        default="markdown",
        choices=("markdown", "html", "rawHtml", "json", "links", "images", "summary"),
    )
    scrape.add_argument("--idempotency-key")

    retry = sub.add_parser(
        "retry-candidates",
        help="retry failed items from a prior candidate acquisition",
    )
    retry.add_argument("prior_invocation_id")
    retry.add_argument("--idempotency-key", required=True)

    attempts = sub.add_parser(
        "attempts", help="list extraction attempts and corpus IDs"
    )
    scope = attempts.add_mutually_exclusive_group(required=True)
    scope.add_argument("--run")
    scope.add_argument("--candidate")
    page(attempts)

    inspect = sub.add_parser("inspect", help="inspect an authoritative asset ID")
    inspect.add_argument("asset_id")

    passages = sub.add_parser("passages", help="fetch bounded passages for an asset")
    passages.add_argument("asset_id")
    _passage_bounds(passages)

    lexical = sub.add_parser(
        "lexical-search", help="PostgreSQL lexical search over authoritative chunks"
    )
    lexical.add_argument("query")
    lexical.add_argument("--run")
    _passage_bounds(lexical)

    pattern = sub.add_parser(
        "pattern-search",
        help="bounded case-insensitive literal or regular-expression search",
    )
    pattern.add_argument("pattern")
    pattern.add_argument("--mode", choices=("literal", "regex"), default="literal")
    pattern.add_argument("--run")
    _passage_bounds(pattern)
    return root


def _passage_bounds(command: argparse.ArgumentParser) -> None:
    command.add_argument("--limit", type=int, default=20)
    command.add_argument("--cursor")
    command.add_argument("--max-chars", type=int, default=20_000)
    command.add_argument("--max-tokens", type=int, default=4_000)


def _page(args: argparse.Namespace) -> PageRequest:
    return PageRequest(limit=args.limit, cursor=args.cursor)


def _bounds(args: argparse.Namespace) -> PassageBounds:
    return PassageBounds(
        limit=args.limit,
        cursor=args.cursor,
        max_chars=args.max_chars,
        max_tokens=args.max_tokens,
    )


def execute(args: argparse.Namespace, service: Any) -> dict[str, Any]:
    if args.command == "runs":
        return service.list_runs(_page(args))
    if args.command == "invocations":
        return service.list_invocations(args.run, _page(args))
    if args.command == "operations":
        return service.list_operations(args.run, _page(args))
    if args.command == "search-responses":
        return service.list_search_responses(args.run, _page(args))
    if args.command == "replay-search":
        return service.replay_search(args.search_response_id, max_bytes=args.max_bytes)
    if args.command == "scrape-candidates":
        return service.scrape_candidates(
            args.candidate_ids,
            format=args.format,
            idempotency_key=args.idempotency_key,
        )
    if args.command == "retry-candidates":
        return service.retry_candidates(
            args.prior_invocation_id,
            idempotency_key=args.idempotency_key,
        )
    if args.command == "attempts":
        return service.list_extraction_attempts(
            run=args.run,
            candidate_id=args.candidate,
            page=_page(args),
        )
    if args.command == "inspect":
        return service.inspect_asset(args.asset_id)
    if args.command == "passages":
        return service.passages(args.asset_id, _bounds(args))
    if args.command == "lexical-search":
        return service.lexical_search(args.query, run=args.run, bounds=_bounds(args))
    if args.command == "pattern-search":
        return service.pattern_search(
            args.pattern,
            mode=args.mode,
            run=args.run,
            bounds=_bounds(args),
        )
    raise AssertionError(f"unhandled command: {args.command}")


def main(argv: list[str] | None = None) -> int:
    try:
        args = parser().parse_args(argv)
        result = execute(args, build_inspection_service())
    except (InspectionArgumentError, ValueError) as exc:
        payload = {
            "schema_version": "database-native-inspection-error-v1",
            "status": "failed",
            "failure_stage": "arguments",
            "error": str(exc),
        }
        print(json.dumps(payload, sort_keys=True), file=sys.stderr)
        return 2
    except InspectionError as exc:
        payload = {
            "schema_version": "database-native-inspection-error-v1",
            "status": "failed",
            "failure_stage": "inspection",
            "error": str(exc),
        }
        print(json.dumps(payload, sort_keys=True), file=sys.stderr)
        return 3
    except Exception as exc:  # noqa: BLE001
        payload = {
            "schema_version": "database-native-inspection-error-v1",
            "status": "failed",
            "failure_stage": "persistence",
            "error": f"{type(exc).__name__}: {exc}",
        }
        print(json.dumps(payload, sort_keys=True), file=sys.stderr)
        return 4
    print(json.dumps(result, indent=2 if args.pretty else None, sort_keys=True))
    if result.get("kind") in {"candidate_scrape", "candidate_retry"}:
        return 0 if result.get("status") == "complete" else 5
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
