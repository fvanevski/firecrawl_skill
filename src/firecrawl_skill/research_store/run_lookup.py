"""Authoritative run-id lookup helpers shared by thin entrypoints."""

from __future__ import annotations

from .store_runtime import database


def resolve_run_id(config, external_id, *, database_fn=database):
    if not external_id:
        return None
    with database_fn(config) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id,state FROM research_runs WHERE external_run_id=%s",
            (external_id,),
        )
        row = cur.fetchone()
    if not row:
        raise SystemExit(f"research run not found: {external_id}")
    if row[1] in {"completed", "partial", "failed", "cancelled"}:
        raise SystemExit(f"research run is finished; reopen it first: {external_id}")
    return row[0]


def resolve_any_run_id(config, external_id, *, database_fn=database):
    if not external_id:
        return None
    with database_fn(config) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM research_runs WHERE external_run_id=%s",
            (external_id,),
        )
        row = cur.fetchone()
    if not row:
        raise SystemExit(f"research run not found: {external_id}")
    return row[0]
