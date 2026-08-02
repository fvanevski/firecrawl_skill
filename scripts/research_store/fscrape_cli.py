"""Command-line interface for PostgreSQL-authoritative ``fscrape``."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .acquisition_authority import AcquisitionPreflightError
from .direct_scrape_service import (
    DirectScrapeError,
    DirectScrapePersistenceError,
)
from .fscrape_contract import (
    SUPPORTED_FORMATS,
    FScrapeArgumentError,
    FScrapeError,
    FScrapeRequest,
    FScrapeResult,
    bounded_text,
    validate_schema,
)
from .fscrape_service import FScrapeService, build_fscrape_service


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise FScrapeArgumentError(message)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="fscrape",
        description=(
            "Scrape URLs through PostgreSQL-authoritative direct acquisition "
            "without filesystem staging."
        ),
    )
    parser.add_argument("urls", nargs="+")
    parser.add_argument("--format", choices=SUPPORTED_FORMATS, default="markdown")
    parser.add_argument("--summary", "-S", action="store_true")
    parser.add_argument("--schema")
    parser.add_argument("--schema-file")
    parser.add_argument(
        "--research-run-id",
        default=os.environ.get("FIRECRAWL_RESEARCH_RUN_ID"),
    )
    parser.add_argument("--idempotency-key")
    parser.add_argument(
        "--invocation-id",
        default=os.environ.get("FIRECRAWL_INVOCATION_ID"),
    )
    parser.add_argument("--json", action="store_true", dest="json_output")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    service_factory: Callable[[], FScrapeService] = build_fscrape_service,
) -> int:
    raw = list(argv if argv is not None else sys.argv[1:])
    json_requested = "--json" in raw
    if any(
        token == "--output-dir" or token.startswith("--output-dir=") for token in raw
    ):
        return emit_error(
            "preflight",
            "--output-dir was removed; fscrape no longer writes acquisition "
            "artifacts. Use database-native export tooling for explicit exports.",
            json_requested,
        )
    try:
        args = build_parser().parse_args(raw)
        if not args.research_run_id:
            raise FScrapeArgumentError(
                "--research-run-id or FIRECRAWL_RESEARCH_RUN_ID is required"
            )
        require_persistence_mode()
        request = FScrapeRequest(
            urls=tuple(args.urls),
            research_run_id=args.research_run_id,
            format=args.format,
            summary=args.summary,
            schema=load_schema(args.schema, args.schema_file),
            idempotency_key=args.idempotency_key,
            external_invocation_id=args.invocation_id,
        )
        try:
            service = service_factory()
        except RuntimeError as exc:
            raise AcquisitionPreflightError(str(exc)) from exc
        result = service.execute(request)
    except (
        FScrapeArgumentError,
        ValueError,
        OSError,
        AcquisitionPreflightError,
    ) as exc:
        return emit_error("preflight", str(exc), json_requested)
    except FScrapeError as exc:
        return emit_fscrape_error(exc, json_requested)
    except Exception as exc:  # noqa: BLE001
        return emit_error(exception_stage(exc), str(exc), json_requested)
    emit_result(result, json_requested)
    return 0 if result.status == "complete" else exit_code("extraction")


def load_schema(
    schema_text: str | None,
    schema_file: str | None,
) -> Mapping[str, Any] | None:
    if schema_text and schema_file:
        raise FScrapeArgumentError("--schema and --schema-file are mutually exclusive")
    if schema_file:
        schema_text = Path(schema_file).read_text(encoding="utf-8")
    if not schema_text:
        return None
    try:
        schema = json.loads(schema_text)
    except json.JSONDecodeError as exc:
        raise FScrapeArgumentError(f"invalid JSON schema: {exc}") from exc
    if not isinstance(schema, dict):
        raise FScrapeArgumentError("JSON schema must be an object")
    validate_schema(schema)
    return schema


def require_persistence_mode() -> None:
    mode = os.environ.get("FIRECRAWL_RESEARCH_PERSIST", "on").strip().lower()
    if mode == "off":
        raise FScrapeError(
            "preflight",
            "FIRECRAWL_RESEARCH_PERSIST=off was removed; fscrape now requires "
            "PostgreSQL-authoritative persistence and a valid fr_<uuid> run binding.",
        )
    if mode not in {"", "auto", "on"}:
        raise FScrapeError(
            "preflight",
            "FIRECRAWL_RESEARCH_PERSIST must be auto or on; off is unsupported",
        )


def emit_result(result: FScrapeResult, json_output: bool) -> None:
    payload = result.to_dict()
    if json_output:
        print(json.dumps(payload, sort_keys=True))
        return
    for key in (
        "status",
        "run_id",
        "research_run_id",
        "batch_id",
        "external_invocation_id",
        "replayed",
    ):
        print(f"{key}: {payload[key]}")
    print("items: " + json.dumps(payload["items"], sort_keys=True))
    print("corpus_ids: " + json.dumps(payload["corpus_ids"], sort_keys=True))


def emit_fscrape_error(error: FScrapeError, json_output: bool) -> int:
    if json_output:
        print(json.dumps(error.to_dict(), sort_keys=True))
    else:
        print(f"ERROR [{error.stage}]: {bounded_text(str(error))}", file=sys.stderr)
        if error.result is not None:
            print(
                "authoritative_result: "
                + json.dumps(error.result.to_dict(), sort_keys=True),
                file=sys.stderr,
            )
    return exit_code(error.stage)


def emit_error(stage: str, message: str, json_output: bool) -> int:
    return emit_fscrape_error(FScrapeError(stage, message), json_output)


def exit_code(stage: str) -> int:
    return {"preflight": 2, "extraction": 5, "ingestion": 6, "indexing": 7}[stage]


def exception_stage(exc: Exception) -> str:
    if isinstance(exc, AcquisitionPreflightError):
        return "preflight"
    if isinstance(exc, DirectScrapePersistenceError):
        return exc.stage
    if isinstance(exc, DirectScrapeError):
        return "extraction"
    return "ingestion"


if __name__ == "__main__":
    raise SystemExit(main())
