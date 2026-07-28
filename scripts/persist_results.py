#!/usr/bin/env python3
"""Persist scratch results into the PostgreSQL research store.

Reads a ``_meta.json`` manifest written by ``fsearch`` or ``fscrape`` and
persists each candidate/source through the authoritative corpus ingestion
service.  Writes back corpus identities to ``--output``.

Usage::

    persist_results.py <_meta.json> --output <_corpus.json> [--research-run-id <ID>]

Manifest types
--------------

``fsearch`` — top-level ``candidates`` array::

    {
      "invocation_id": "...",
      "operation": "search",
      "query": "...",
      "candidates": [
        {
          "rank": 1,
          "url": "https://example.com",
          "title": "Example",
          "snippet": "...",
          "scratch_file": "/tmp/.../result_000.md",
          "scrape_status": "ok",
          "word_count": 420
        }
      ]
    }

``fscrape`` — top-level ``results`` array::

    {
      "invocation_id": "...",
      "operation": "scrape",
      "results": [
        {
          "index": 0,
          "url": "https://example.com",
          "title": "Example",
          "scratch_file": "/tmp/.../url_000.md",
          "status": "ok",
          "word_count": 420
        }
      ]
    }

The legacy ``url`` key (single-scraper manifest) is no longer supported.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

logger = logging.getLogger("persist_results")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="persist-results",
        description=(
            "Persist scratch results into the research store "
            "through the authoritative corpus ingestion service."
        ),
    )
    result.add_argument(
        "manifest",
        help="Path to the _meta.json manifest file.",
    )
    result.add_argument(
        "--output",
        default=None,
        help=(
            "Path to write the corpus identities JSON "
            "(default: <manifest>_corpus.json)."
        ),
    )
    result.add_argument(
        "--research-run-id",
        default=None,
        help=(
            "Research run external ID (``fr_<hex>``) or internal UUID "
            "to associate with the persisted results."
        ),
    )
    return result


# ---------------------------------------------------------------------------
# MIME type mapping
# ---------------------------------------------------------------------------


def _format_to_mime_type(format_str: str) -> str:
    """Map the fscrape ``format`` field to a MIME type.

    Args:
        format_str: The format string from the manifest (e.g. "markdown",
            "html", "json", "links", "images", "summary").

    Returns:
        A MIME type string suitable for ``IngestRequest.mime_type``.
    """
    fmt = format_str.lower().strip()
    mapping: dict[str, str] = {
        "markdown": "text/markdown",
        "md": "text/markdown",
        "html": "text/html",
        "rawhtml": "text/html",
        "json": "application/json",
        "links": "text/html",
        "images": "text/html",
        "summary": "text/markdown",
    }
    return mapping.get(fmt, "text/markdown")


# ---------------------------------------------------------------------------
# Manifest parsing
# ---------------------------------------------------------------------------


def _load_manifest(path: str) -> dict[str, Any]:
    """Load and validate the _meta.json manifest."""
    meta_path = Path(path)
    if not meta_path.is_file():
        raise FileNotFoundError(f"manifest not found: {meta_path}")
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TypeError(f"manifest is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise TypeError("manifest top-level value must be a JSON object")
    return data


def _detect_manifest_type(manifest: dict[str, Any]) -> str:
    """Return ``'search'``, ``'scrape'``, or ``'unknown'``."""
    if manifest.get("operation") == "search" or "candidates" in manifest:
        return "search"
    if manifest.get("operation") == "scrape" or "results" in manifest:
        return "scrape"
    return "unknown"


# ---------------------------------------------------------------------------
# Run ID resolution
# ---------------------------------------------------------------------------


def _resolve_run_id(
    run_id: str | None,
    uow_factory,
) -> UUID | None:
    """Resolve an external run ID to an internal run UUID.

    Handles both raw UUID literals and the ``fr_<hex>`` prefix used by
    ``frun`` and other wrappers.  When the external ID cannot be found
    in the database the function raises ``ValueError`` so the caller can
    decide whether to fail or fall back to scratch-only.

    Args:
        run_id: External run ID string or ``None``.
        uow_factory: Callable returning a ``PostgresUnitOfWork``.

    Returns:
        The internal run UUID, or ``None`` when *run_id* is ``None``.

    Raises:
        ValueError: When the external ID is not found in the database.
    """
    if run_id is None:
        return None

    cleaned = run_id.removeprefix("fr_")

    # Try to resolve as an internal UUID first.
    try:
        internal_uuid = UUID(cleaned)
        with uow_factory() as uow:
            status = uow.runs.get_run_status(run_id=internal_uuid)
            return UUID(status["id"])
    except (ValueError, KeyError):
        pass

    # Try resolving by external ID — use the full value (with prefix) first,
    # then the stripped suffix as a fallback for legacy callers.
    try:
        with uow_factory() as uow:
            status = uow.runs.get_run_status(external_id=run_id)
            return UUID(status["id"])
    except KeyError:
        pass

    try:
        with uow_factory() as uow:
            status = uow.runs.get_run_status(external_id=cleaned)
            return UUID(status["id"])
    except KeyError:
        raise ValueError(f"research run {run_id!r} not found in the database") from None


# ---------------------------------------------------------------------------
# Authoritative ingestion
# ---------------------------------------------------------------------------


def _build_ingest_request(
    candidate: dict[str, Any],
    scratch_root: Path,
    mime_type: str = "text/markdown",
) -> tuple[Any, str | None]:
    """Build an ``IngestRequest`` from a manifest candidate entry.

    Returns:
        A tuple of ``(ingest_request, error)``.  On success *error* is
        ``None``; on failure *ingest_request* is ``None`` and *error*
        describes why the candidate could not be ingested.
    """
    url = candidate.get("url", "")
    if not url:
        return None, "missing URL"

    # Only ingest candidates that were actually scraped.
    scrape_status = candidate.get("scrape_status", "")
    if scrape_status != "ok":
        return None, f"scrape_status={scrape_status} — skipping unscripted candidate"

    scratch_file = candidate.get("scratch_file", "")
    if not scratch_file:
        return None, "missing scratch_file"

    scratch_path = Path(scratch_file)
    if not scratch_path.is_file():
        return None, f"scratch file not found: {scratch_file}"

    try:
        content = scratch_path.read_bytes()
    except OSError as exc:
        return None, f"cannot read scratch file: {exc}"

    if not content:
        return None, "scratch file is empty"

    from research_store.domain import IngestRequest

    title = candidate.get("title") or None
    snippet = candidate.get("snippet", "") or None
    metadata: dict[str, Any] = {
        "rank": candidate.get("rank"),
        "scrape_status": candidate.get("scrape_status"),
        "word_count": candidate.get("word_count"),
    }
    if snippet:
        metadata["snippet"] = snippet

    ingest_request = IngestRequest(
        requested_url=url,
        final_url=url,
        content=content,
        mime_type=mime_type,
        title=title,
        metadata=metadata,
    )
    return ingest_request, None


def _build_scrape_ingest_request(
    result: dict[str, Any],
    scratch_root: Path,
    mime_type: str = "text/markdown",
) -> tuple[Any, str | None]:
    """Build an ``IngestRequest`` from an fscrape result entry.

    Returns:
        A tuple of ``(ingest_request, error)``.
    """
    url = result.get("url", "")
    if not url:
        return None, "missing URL"

    # Only ingest results that were actually scraped successfully.
    status = result.get("status", "")
    if status != "ok":
        return None, f"status={status} — skipping unscripted result"

    scratch_file = result.get("scratch_file", "")
    if not scratch_file:
        return None, "missing scratch_file"

    scratch_path = Path(scratch_file)
    if not scratch_path.is_file():
        return None, f"scratch file not found: {scratch_file}"

    try:
        content = scratch_path.read_bytes()
    except OSError as exc:
        return None, f"cannot read scratch file: {exc}"

    if not content:
        return None, "scratch file is empty"

    from research_store.domain import IngestRequest

    title = result.get("title") or None
    metadata: dict[str, Any] = {
        "format": result.get("format"),
        "status": result.get("status"),
        "word_count": result.get("word_count"),
    }

    ingest_request = IngestRequest(
        requested_url=url,
        final_url=url,
        content=content,
        mime_type=mime_type,
        title=title,
        metadata=metadata,
    )
    return ingest_request, None


# ---------------------------------------------------------------------------
# Persistence entry point
# ---------------------------------------------------------------------------


def _persist_search_manifest(
    manifest: dict[str, Any],
    run_id: str | None,
    uow_factory,
) -> list[dict[str, Any]]:
    """Persist candidates from an fsearch manifest through the corpus service."""
    from research_store.blob import ContentAddressedBlobStore
    from research_store.config import StoreConfig
    from research_store.service import CorpusService

    candidates = manifest.get("candidates", [])
    records: list[dict[str, Any]] = []

    # When no database is configured, return scratch-only records.
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        logger.info("no DATABASE_URL — returning scratch-only identities")
        for idx, cand in enumerate(candidates, start=1):
            records.append(
                {
                    "index": idx,
                    "url": cand.get("url", ""),
                    "title": cand.get("title", ""),
                    "status": "ok",
                    "persisted": False,
                    "scratch_file": cand.get("scratch_file", ""),
                }
            )
        return records

    config = StoreConfig.from_env()
    config.require_database()

    # Build a real blob store so ingest does not fail with AttributeError.
    blob_store = ContentAddressedBlobStore(config.blob_root)

    # Build the service with a parser registry so non-Markdown MIME types
    # are handled correctly.
    from research_store.parsing import get_registry

    parser_registry = get_registry()

    service = CorpusService(
        config,
        uow_factory,
        blob_store=blob_store,
        parser_registry=parser_registry,
    )

    # Use the batch ingestion contract for proper provenance tracking.
    invocation_id = manifest.get(
        "invocation_id", f"legacy-{manifest.get('invocation_id', str(UUID(int=0)))}"
    )
    operation = "fsearch"

    # Build ingest requests for successfully scraped candidates only.
    ingest_items: list[Any] = []
    for idx, cand in enumerate(candidates, start=1):
        url = cand.get("url", "")
        if not url:
            records.append(
                {
                    "index": idx,
                    "url": "",
                    "title": cand.get("title", ""),
                    "status": "error",
                    "error": "missing URL",
                    "persisted": False,
                }
            )
            continue

        scrape_status = cand.get("scrape_status", "")
        if scrape_status != "ok":
            # Unscripted candidate — record as non-error but skip ingestion.
            records.append(
                {
                    "index": idx,
                    "url": url,
                    "title": cand.get("title", ""),
                    "status": "ok",
                    "persisted": False,
                    "scratch_file": cand.get("scratch_file", ""),
                    "reason": "not_scraped",
                }
            )
            continue

        ingest_request, error = _build_ingest_request(cand, config.scratch_root)
        if ingest_request is None:
            records.append(
                {
                    "index": idx,
                    "url": url,
                    "title": cand.get("title", ""),
                    "status": "error",
                    "error": error,
                    "persisted": False,
                }
            )
            continue

        ingest_items.append(ingest_request)

    # Batch ingest all successfully scraped candidates.
    if ingest_items:
        try:
            manifest_result = service.ingest_batch(
                invocation_id,
                operation,
                ingest_items,
                research_run_external_id=run_id,
                metadata={
                    "invocation_id": invocation_id,
                    "operation": operation,
                    "source": "persist_results",
                },
            )
            # Map batch results back to records.
            for asset in manifest_result.get("assets", []):
                ordinal = asset.get("ordinal", 0)
                record_idx = ordinal + 1  # 1-based index
                for rec in records:
                    if (
                        rec.get("index") == record_idx
                        and rec.get("status") == "ok"
                        and rec.get("persisted") is False
                    ):
                        if asset["status"] == "complete":
                            rec["persisted"] = True
                            rec["status"] = "ok"
                            rec["source_id"] = str(asset["source_id"])
                            rec["snapshot_id"] = str(asset["snapshot_id"])
                            rec["document_id"] = str(asset["document_id"])
                            rec["chunk_ids"] = [str(cid) for cid in asset["chunk_ids"]]
                            rec["content_sha256"] = asset["content_sha256"]
                        elif asset["status"] == "failed":
                            rec["persisted"] = False
                            rec["status"] = "error"
                            rec["error"] = asset.get("error", "unknown")
                        break
        except Exception as exc:  # noqa: BLE001
            logger.error("batch ingestion failed: %s", exc)
            # Mark all items as failed.
            for rec in records:
                if rec.get("persisted") is False and rec.get("status") == "ok":
                    rec["persisted"] = False
                    rec["status"] = "error"
                    rec["error"] = str(exc)

    return records


def _persist_scrape_manifest(
    manifest: dict[str, Any],
    run_id: str | None,
    uow_factory,
) -> list[dict[str, Any]]:
    """Persist results from an fscrape manifest through the corpus service."""
    from research_store.blob import ContentAddressedBlobStore
    from research_store.config import StoreConfig
    from research_store.service import CorpusService

    results = manifest.get("results", [])
    records: list[dict[str, Any]] = []

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        logger.info("no DATABASE_URL — returning scratch-only identities")
        for idx, res in enumerate(results, start=1):
            records.append(
                {
                    "index": idx,
                    "url": res.get("url", ""),
                    "title": res.get("title", ""),
                    "status": "ok",
                    "persisted": False,
                    "scratch_file": res.get("scratch_file", ""),
                }
            )
        return records

    config = StoreConfig.from_env()
    config.require_database()

    # Build a real blob store so ingest does not fail with AttributeError.
    blob_store = ContentAddressedBlobStore(config.blob_root)

    # Build the service with a parser registry so non-Markdown MIME types
    # are handled correctly.
    from research_store.parsing import get_registry

    parser_registry = get_registry()

    service = CorpusService(
        config,
        uow_factory,
        blob_store=blob_store,
        parser_registry=parser_registry,
    )

    # Use the batch ingestion contract for proper provenance tracking.
    invocation_id = manifest.get(
        "invocation_id", f"legacy-{manifest.get('invocation_id', str(UUID(int=0)))}"
    )
    operation = "fscrape"

    # Determine MIME type from the manifest format field.
    format_str = manifest.get("format", "markdown")
    mime_type = _format_to_mime_type(format_str)

    # Build ingest requests for successfully scraped results only.
    ingest_items: list[Any] = []
    for idx, res in enumerate(results, start=1):
        url = res.get("url", "")
        if not url:
            records.append(
                {
                    "index": idx,
                    "url": "",
                    "title": res.get("title", ""),
                    "status": "error",
                    "error": "missing URL",
                    "persisted": False,
                }
            )
            continue

        status = res.get("status", "")
        if status != "ok":
            # Unscripted result — record as non-error but skip ingestion.
            records.append(
                {
                    "index": idx,
                    "url": url,
                    "title": res.get("title", ""),
                    "status": "ok",
                    "persisted": False,
                    "scratch_file": res.get("scratch_file", ""),
                    "reason": "not_ok",
                }
            )
            continue

        ingest_request, error = _build_scrape_ingest_request(
            res, config.scratch_root, mime_type=mime_type
        )
        if ingest_request is None:
            records.append(
                {
                    "index": idx,
                    "url": url,
                    "title": res.get("title", ""),
                    "status": "error",
                    "error": error,
                    "persisted": False,
                }
            )
            continue

        ingest_items.append(ingest_request)

    # Batch ingest all successfully scraped results.
    if ingest_items:
        try:
            manifest_result = service.ingest_batch(
                invocation_id,
                operation,
                ingest_items,
                research_run_external_id=run_id,
                metadata={
                    "invocation_id": invocation_id,
                    "operation": operation,
                    "source": "persist_results",
                },
            )
            # Map batch results back to records.
            for asset in manifest_result.get("assets", []):
                ordinal = asset.get("ordinal", 0)
                record_idx = ordinal + 1  # 1-based index
                for rec in records:
                    if (
                        rec.get("index") == record_idx
                        and rec.get("status") == "ok"
                        and rec.get("persisted") is False
                    ):
                        if asset["status"] == "complete":
                            rec["persisted"] = True
                            rec["status"] = "ok"
                            rec["source_id"] = str(asset["source_id"])
                            rec["snapshot_id"] = str(asset["snapshot_id"])
                            rec["document_id"] = str(asset["document_id"])
                            rec["chunk_ids"] = [str(cid) for cid in asset["chunk_ids"]]
                            rec["content_sha256"] = asset["content_sha256"]
                        elif asset["status"] == "failed":
                            rec["persisted"] = False
                            rec["status"] = "error"
                            rec["error"] = asset.get("error", "unknown")
                        break
        except Exception as exc:  # noqa: BLE001
            logger.error("batch ingestion failed: %s", exc)
            # Mark all items as failed.
            for rec in records:
                if rec.get("persisted") is False and rec.get("status") == "ok":
                    rec["persisted"] = False
                    rec["status"] = "error"
                    rec["error"] = str(exc)

    return records


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)

    try:
        manifest = _load_manifest(args.manifest)
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    # Determine output path
    output_path = args.output or str(
        Path(args.manifest).with_suffix(Path(args.manifest).suffix + "_corpus.json")
    )

    # Determine manifest type
    manifest_type = _detect_manifest_type(manifest)
    if manifest_type == "unknown":
        records: list[dict[str, Any]] = [{"status": "ok", "persisted": False}]
        Path(output_path).write_text(json.dumps(records, indent=2), encoding="utf-8")
        return 0

    # Build a minimal uow_factory for run-ID resolution.
    # When no database is configured we skip resolution entirely.
    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        from functools import partial

        from research_store.config import StoreConfig
        from research_store.postgres import PostgresUnitOfWork

        config = StoreConfig.from_env()
        config.require_database()
        uow_factory = partial(
            PostgresUnitOfWork,
            config.database_url,
            config.physical_collection,
            config.embedding_model,
            config.embedding_revision,
            config.embedding_dimension,
            config.parser_version,
            config.normalization_version,
            config.chunker_version,
        )
    else:
        uow_factory = None

    # Dispatch to the correct persistence path.
    if manifest_type == "search":
        records = _persist_search_manifest(manifest, args.research_run_id, uow_factory)
    else:
        records = _persist_scrape_manifest(manifest, args.research_run_id, uow_factory)

    Path(output_path).write_text(json.dumps(records, indent=2), encoding="utf-8")

    # Exit nonzero when any requested authoritative operation failed.
    if database_url and any(rec.get("status") == "error" for rec in records):
        error_count = sum(1 for rec in records if rec.get("status") == "error")
        logger.error("%d of %d items failed to persist", error_count, len(records))
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
