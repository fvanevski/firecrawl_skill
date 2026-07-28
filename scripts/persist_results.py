#!/usr/bin/env python3
"""Persist scratch results into the PostgreSQL research store.

Reads a ``_meta.json`` manifest written by ``fsearch`` or ``fscrape`` and
persists each candidate/source into the research store.  Writes back corpus
IDs to ``--output``.

Usage::

    persist_results.py <_meta.json> --output <_corpus.json> [--research-run-id <UUID>]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

logger = logging.getLogger("persist_results")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="persist-results",
        description="Persist scratch results into the research store.",
    )
    result.add_argument(
        "manifest",
        help="Path to the _meta.json manifest file.",
    )
    result.add_argument(
        "--output",
        default=None,
        help="Path to write the corpus IDs JSON (default: <manifest>_corpus.json).",
    )
    result.add_argument(
        "--research-run-id",
        default=None,
        help="Research run UUID to associate with the persisted results.",
    )
    return result


def _load_manifest(path: str) -> dict[str, Any]:
    """Load and validate the _meta.json manifest."""
    meta_path = Path(path)
    if not meta_path.is_file():
        raise FileNotFoundError(f"manifest not found: {meta_path}")
    return json.loads(meta_path.read_text(encoding="utf-8"))


def _canonicalize_url(url: str) -> str:
    """Normalize a URL for deduplication."""
    return url.rstrip("/")


def _url_sha256(url: str) -> str:
    """SHA-256 hex digest of a URL for the canonical_url_sha256 column."""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _extract_domain(url: str) -> str:
    """Extract the domain from a URL."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    return parsed.netloc or ""


def _resolve_run_uuid(run_id: str | None) -> UUID | None:
    """Convert an external run ID to a UUID.

    Handles both raw UUID literals (``<32hex>``) and the
    ``fr_<32hex>`` prefix used by ``frun`` and other wrappers.
    """
    if run_id is None:
        return None
    cleaned = run_id.removeprefix("fr_")
    return UUID(cleaned)


def _persist_search_manifest(
    manifest: dict[str, Any],
    run_id: str | None,
    database_url: str | None,
) -> list[dict[str, Any]]:
    """Persist candidates from a search manifest into PostgreSQL."""
    candidates = manifest.get("candidates", [])
    records = []

    if not database_url:
        logger.info("no DATABASE_URL — skipping DB persistence (scratch remains valid)")
        for idx, cand in enumerate(candidates, start=1):
            records.append(
                {
                    "index": idx,
                    "url": cand.get("url", ""),
                    "title": cand.get("title", ""),
                    "status": "ok",
                    "persisted": False,
                }
            )
        return records

    try:
        import psycopg
    except ImportError:
        logger.warning("psycopg not available — skipping DB persistence")
        for idx, cand in enumerate(candidates, start=1):
            records.append(
                {
                    "index": idx,
                    "url": cand.get("url", ""),
                    "title": cand.get("title", ""),
                    "status": "ok",
                    "persisted": False,
                }
            )
        return records

    run_uuid = _resolve_run_uuid(run_id)

    try:
        conn = psycopg.connect(database_url)
        cur = conn.cursor()

        for idx, cand in enumerate(candidates, start=1):
            url = cand.get("url", "")
            if not url:
                logger.warning("candidate %d missing URL, skipping", idx)
                records.append(
                    {
                        "index": idx,
                        "url": "",
                        "status": "error",
                        "error": "missing URL",
                        "persisted": False,
                    }
                )
                continue

            canonical = _canonicalize_url(url)
            domain = _extract_domain(url)
            title = cand.get("title", "")
            snippet = cand.get("snippet", "")
            backend = cand.get("backend", "firecrawl")
            original_url = url

            try:
                cur.execute(
                    """INSERT INTO search_candidates
                       (id, run_id, canonical_url, canonical_url_sha256,
                        original_url, title, snippet, domain, backend,
                        backend_metadata, recurrence_count)
                     VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 1)
                     ON CONFLICT (run_id, canonical_url_sha256) DO UPDATE
                     SET last_seen_at = now(), title = EXCLUDED.title,
                         snippet = EXCLUDED.snippet""",
                    (
                        str(uuid4()),
                        str(run_uuid) if run_uuid else None,
                        canonical,
                        _url_sha256(canonical),
                        original_url,
                        title,
                        snippet,
                        domain,
                        backend,
                        json.dumps(cand.get("metadata", {})),
                    ),
                )
                conn.commit()
                records.append(
                    {
                        "index": idx,
                        "url": url,
                        "title": title,
                        "status": "ok",
                        "persisted": True,
                    }
                )
            except Exception as exc:  # noqa: BLE001
                conn.rollback()
                logger.warning("failed to persist candidate %d (%s): %s", idx, url, exc)
                records.append(
                    {
                        "index": idx,
                        "url": url,
                        "title": title,
                        "status": "error",
                        "error": str(exc),
                        "persisted": False,
                    }
                )

        cur.close()
        conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.error("database persistence failed: %s", exc)
        for idx, cand in enumerate(candidates, start=1):
            records.append(
                {
                    "index": idx,
                    "url": cand.get("url", ""),
                    "title": cand.get("title", ""),
                    "status": "error",
                    "error": str(exc),
                    "persisted": False,
                }
            )

    return records


def _persist_scrape_manifest(
    manifest: dict[str, Any],
    run_id: str | None,
    database_url: str | None,
) -> list[dict[str, Any]]:
    """Persist a scrape result from the manifest into PostgreSQL."""
    url = manifest.get("url", "")
    title = manifest.get("title", "")

    if not url:
        return [{"status": "error", "error": "missing URL", "persisted": False}]

    if not database_url:
        logger.info("no DATABASE_URL — skipping DB persistence (scratch remains valid)")
        return [
            {
                "index": 1,
                "url": url,
                "title": title,
                "status": "ok",
                "persisted": False,
            }
        ]

    records = []
    try:
        import psycopg
    except ImportError:
        logger.warning("psycopg not available — skipping DB persistence")
        return [
            {
                "index": 1,
                "url": url,
                "title": title,
                "status": "ok",
                "persisted": False,
            }
        ]

    run_uuid = _resolve_run_uuid(run_id)

    try:
        conn = psycopg.connect(database_url)
        cur = conn.cursor()

        canonical = _canonicalize_url(url)
        domain = _extract_domain(url)

        try:
            cur.execute(
                """INSERT INTO search_candidates
                   (id, run_id, canonical_url, canonical_url_sha256,
                    original_url, title, snippet, domain, backend,
                    backend_metadata, recurrence_count)
                 VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 1)
                 ON CONFLICT (run_id, canonical_url_sha256) DO UPDATE
                 SET last_seen_at = now(), title = EXCLUDED.title""",
                (
                    str(uuid4()),
                    str(run_uuid) if run_uuid else None,
                    canonical,
                    _url_sha256(canonical),
                    url,
                    title,
                    manifest.get("snippet", ""),
                    domain,
                    "firecrawl",
                    json.dumps({}),
                ),
            )
            conn.commit()
            records.append(
                {
                    "index": 1,
                    "url": url,
                    "title": title,
                    "status": "ok",
                    "persisted": True,
                }
            )
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            logger.warning("failed to persist scrape (%s): %s", url, exc)
            records.append(
                {
                    "index": 1,
                    "url": url,
                    "title": title,
                    "status": "error",
                    "error": str(exc),
                    "persisted": False,
                }
            )

        cur.close()
        conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.error("database persistence failed: %s", exc)
        records.append(
            {
                "index": 1,
                "url": url,
                "title": title,
                "status": "error",
                "error": str(exc),
                "persisted": False,
            }
        )

    return records


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)

    try:
        manifest = _load_manifest(args.manifest)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    # Determine output path
    output_path = args.output or str(
        Path(args.manifest).with_suffix(Path(args.manifest).suffix + "_corpus.json")
    )

    database_url = os.environ.get("DATABASE_URL")

    # Determine whether this is a search or scrape manifest
    if manifest.get("candidates"):
        records = _persist_search_manifest(manifest, args.research_run_id, database_url)
    elif manifest.get("results"):
        # fscrape _meta.json: URLs stored in the results array
        records = _persist_search_manifest(manifest, args.research_run_id, database_url)
    elif manifest.get("url"):
        records = _persist_scrape_manifest(manifest, args.research_run_id, database_url)
    else:
        records = [{"status": "ok", "persisted": False}]

    Path(output_path).write_text(json.dumps(records, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
