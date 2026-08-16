"""PostgreSQL schema probes required by the issue #217 compatibility facade."""

from __future__ import annotations

from typing import Any


def _has_sealed_at_column(connection: Any) -> bool:
    """Return whether the v43 ingestion-batch sealing column is present."""
    with connection.cursor() as cur:
        cur.execute(
            """SELECT 1 FROM information_schema.columns
            WHERE table_name='ingestion_batches'
              AND column_name='sealed_at'
            LIMIT 1"""
        )
        return cur.fetchone() is not None


def _has_extraction_attempt_id_column(connection: Any) -> bool:
    """Return whether batch members can reference extraction attempts."""
    with connection.cursor() as cur:
        cur.execute(
            """SELECT 1 FROM information_schema.columns
            WHERE table_name='ingestion_batch_assets'
              AND column_name='extraction_attempt_id'
            LIMIT 1"""
        )
        return cur.fetchone() is not None
