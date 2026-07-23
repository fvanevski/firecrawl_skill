from __future__ import annotations

import argparse
from datetime import datetime, timezone
from functools import partial
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any
from uuid import UUID, uuid4

from .blob import ContentAddressedBlobStore
from .catalog_export import EXPORT_SCHEMA_VERSION
from .config import StoreConfig
from .container import (
    build_audit_service,
    build_catalog_export_service,
    build_run_service,
    build_service,
)
from .domain import IngestRequest
from .indexing import IndexWorker, OpenAICompatibleEmbedder
from .normalization import NORMALIZATION_VERSION
from .postgres import PostgresUnitOfWork, connect
from .qdrant import QdrantIndex
from .queue import ValkeyQueue
from .retrieval import CohereCompatibleReranker
from .service import dumps


_KNOWN_PREFIXES = ("result_", "url_")


def _export_json(path: Path, payload: Any) -> None:
    """Write JSON atomically via temp-file rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    temporary.replace(path)


def _iter_scratch_assets(root: Path):
    for meta_path in sorted(root.rglob("_meta.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for item in meta.get("results", []):
            if item.get("status") != "ok" or not item.get("url"):
                continue
            raw = item.get("scratch_file", "")
            path = (
                Path(raw)
                if raw
                else meta_path.parent / f"result_{item.get('index', 0):03d}.md"
            )
            if not path.is_absolute():
                path = meta_path.parent / path
            try:
                path.resolve().relative_to(root.resolve())
            except ValueError:
                path = meta_path.parent / Path(raw).name
            if path.name.startswith(_KNOWN_PREFIXES) and path.is_file():
                yield (
                    path,
                    {
                        **item,
                        "invocation_id": meta.get("invocation_id"),
                        "operation": meta.get("operation"),
                    },
                )


def _import_scratch(root: Path, service, dry_run: bool = False) -> dict:
    report = {
        "version": 1,
        "root": str(root),
        "dry_run": dry_run,
        "scanned": 0,
        "imported": 0,
        "reused": 0,
        "failed": 0,
        "items": [],
    }
    for path, item in _iter_scratch_assets(root):
        report["scanned"] += 1
        entry = {
            "original_path": str(path),
            "url": item["url"],
            "status": "would_import" if dry_run else "pending",
        }
        try:
            content = path.read_bytes()
            entry["byte_length"] = len(content)
            if not dry_run:
                result = service.ingest(
                    IngestRequest(
                        requested_url=item["url"],
                        content=content,
                        mime_type="application/json"
                        if path.suffix == ".json"
                        else "text/markdown",
                        title=item.get("title"),
                        metadata={
                            "migration": {
                                "original_path": str(path),
                                "invocation_id": item.get("invocation_id"),
                                "operation": item.get("operation"),
                            }
                        },
                    )
                )
                entry.update(
                    {
                        "status": "reused" if result.reused_snapshot else "imported",
                        "source_id": str(result.source_id),
                        "snapshot_id": str(result.snapshot_id),
                        "document_id": str(result.document_id),
                        "content_sha256": result.content_sha256,
                    }
                )
                report[entry["status"]] += 1
        except Exception as exc:
            entry.update({"status": "failed", "error": f"{type(exc).__name__}: {exc}"})
            report["failed"] += 1
        report["items"].append(entry)
    report["completed_at"] = datetime.now(timezone.utc).isoformat()
    return report


def parser():
    root = argparse.ArgumentParser(
        prog="research-db", description="Authoritative research asset store"
    )
    sub = root.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate")
    sub.add_parser("status")
    sub.add_parser("doctor")
    sub.add_parser("ingest-ready")

    # ------------------------------------------------------------------
    # Parser info (issue #44)
    # ------------------------------------------------------------------
    sub.add_parser("parser-info", help="Show parser registry information")

    imp = sub.add_parser("import-scratch")
    imp.add_argument("path", nargs="?")
    imp.add_argument("--dry-run", action="store_true")
    imp.add_argument("--report")
    ingest = sub.add_parser("ingest-result")
    ingest.add_argument("--url", required=True)
    ingest.add_argument("--file", required=True)
    ingest.add_argument("--title")
    ingest.add_argument("--metadata-json", default="{}")
    sub.add_parser("verify-blobs")

    worker = sub.add_parser("worker")
    worker.add_argument("--batch-size", type=int, default=32)
    worker.add_argument("--poll-seconds", type=float)
    worker.add_argument("--lease-seconds", type=int)
    worker.add_argument("--max-attempts", type=int)
    worker.add_argument("--once", action="store_true")
    once = sub.add_parser("index-once")
    once.add_argument("--limit", type=int, default=64)

    sub.add_parser("index-list")
    build = sub.add_parser("index-build")
    build.add_argument("--current-config", action="store_true", required=True)
    selection = build.add_mutually_exclusive_group(required=True)
    selection.add_argument("--all", action="store_true")
    selection.add_argument("--document")
    reindex = sub.add_parser("reindex")
    legacy_selection = reindex.add_mutually_exclusive_group(required=True)
    legacy_selection.add_argument("--all", action="store_true")
    legacy_selection.add_argument("--document")
    activate = sub.add_parser("index-activate")
    activate.add_argument("id")
    rollback = sub.add_parser("index-rollback")
    rollback.add_argument("id")
    prune = sub.add_parser("index-prune")
    prune.add_argument("--dry-run", action="store_true")
    prune.add_argument("--force", action="store_true")
    prune.add_argument("--keep-last", type=int, default=2)
    prune.add_argument("--index-id")
    sub.add_parser("reconcile-qdrant")
    sub.add_parser("prune-cache")

    rederive = sub.add_parser("rederive")
    target = rederive.add_mutually_exclusive_group(required=True)
    target.add_argument("--all", action="store_true")
    target.add_argument("--snapshot")

    # ------------------------------------------------------------------
    # Normalization diagnostics (issue #45)
    # ------------------------------------------------------------------
    norm = sub.add_parser("normalize", help="Run normalization and show diagnostics")
    norm.add_argument("--document", help="Document UUID to normalize")
    norm.add_argument("--all", action="store_true", help="Normalize all documents")
    norm.add_argument(
        "--aggressive", action="store_true", help="Enable aggressive cleanup"
    )
    norm.add_argument(
        "--document-type",
        default="web",
        choices=["web", "academic", "legal", "documentation"],
    )

    export = sub.add_parser("export-invocation")
    export.add_argument("invocation_id")
    export.add_argument("--output", required=True)
    export_run = sub.add_parser("export-run")
    export_run.add_argument("id")
    export_run.add_argument("--output", required=True)

    # ------------------------------------------------------------------
    # Catalog v5 compatibility export (issue #35)
    # ------------------------------------------------------------------
    catalog_export = sub.add_parser("catalog-export")
    catalog_sub = catalog_export.add_subparsers(dest="catalog_command", required=True)

    catalog_run = catalog_sub.add_parser("run")
    catalog_run.add_argument("external_id")
    catalog_run.add_argument("--target-dir", required=True)

    catalog_invocation = catalog_sub.add_parser("invocation")
    catalog_invocation.add_argument("invocation_id")
    catalog_invocation.add_argument("run_id")
    catalog_invocation.add_argument("--target-dir", required=True)

    catalog_events = catalog_sub.add_parser("events")
    catalog_events.add_argument("external_id")
    catalog_events.add_argument("--target-dir", required=True)

    catalog_snapshots = catalog_sub.add_parser("snapshots")
    catalog_snapshots.add_argument("external_id")
    catalog_snapshots.add_argument("--target-dir", required=True)

    catalog_claims = catalog_sub.add_parser("claims")
    catalog_claims.add_argument("external_id")
    catalog_claims.add_argument("--target-dir", required=True)

    catalog_assessments = catalog_sub.add_parser("assessments")
    catalog_assessments.add_argument("external_id")
    catalog_assessments.add_argument("--target-dir", required=True)

    catalog_manifest = catalog_sub.add_parser("manifest")
    catalog_manifest.add_argument("external_id")
    catalog_manifest.add_argument("--target-dir", required=True)

    catalog_regenerate = catalog_sub.add_parser("regenerate")
    catalog_regenerate.add_argument("external_id")
    catalog_regenerate.add_argument("--target-dir", required=True)

    run_start = sub.add_parser("run-start")
    run_start.add_argument("external_id")
    run_start.add_argument("objective")
    run_start.add_argument("--catalog-pointer")
    run_start.add_argument(
        "--mode",
        choices=("agent_led", "autonomous_local", "deterministic_debug"),
        default="autonomous_local",
    )
    run_start.add_argument("--idempotency-key")
    run_start.add_argument("--actor", default="cli")
    run_status = sub.add_parser("run-status")
    run_status.add_argument("external_id")
    run_mode = sub.add_parser("run-mode-change")
    run_mode.add_argument("external_id")
    run_mode.add_argument(
        "mode", choices=("agent_led", "autonomous_local", "deterministic_debug")
    )
    run_mode.add_argument("--expected-revision", type=int, required=True)
    run_mode.add_argument("--idempotency-key", required=True)
    run_mode.add_argument("--requested-by", required=True)
    run_mode.add_argument("--approved-by", required=True)
    run_mode.add_argument("--reason", required=True)
    run_mode.add_argument("--actor", default="operator")
    run_mode.add_argument("--actor-identifier")
    run_transition = sub.add_parser("run-transition")
    run_transition.add_argument("external_id")
    run_transition.add_argument("next_state")
    run_transition.add_argument("--expected-revision", type=int, required=True)
    run_transition.add_argument("--idempotency-key", required=True)
    run_transition.add_argument("--actor", default="cli")
    run_transition.add_argument("--actor-identifier")
    run_transition.add_argument("--semantic-proposal-id")
    run_transition.add_argument("--reason")
    run_finish = sub.add_parser("run-finish")
    run_finish.add_argument("external_id")
    run_finish.add_argument("--outcome", required=True)
    run_finish.add_argument(
        "--status", choices=("complete", "failed"), default="complete"
    )
    run_finish.add_argument("--catalog-pointer")
    run_finish.add_argument("--source-manifest-sha256")
    run_finish.add_argument("--answer-sha256")
    run_finish.add_argument("--expected-revision", type=int)
    run_finish.add_argument("--idempotency-key")
    run_finish.add_argument("--actor", default="cli")
    run_reopen = sub.add_parser("run-reopen")
    run_reopen.add_argument("external_id")
    run_reopen.add_argument("--reason", default="legacy compatibility reopen")
    run_reopen.add_argument("--expected-revision", type=int)
    run_reopen.add_argument("--idempotency-key")
    run_reopen.add_argument("--actor", default="cli")
    run_cancel = sub.add_parser("run-cancel")
    run_cancel.add_argument("external_id")
    run_cancel.add_argument("--reason", default="cancelled by operator")
    run_cancel.add_argument("--expected-revision", type=int)
    run_cancel.add_argument("--idempotency-key")
    run_cancel.add_argument("--actor", default="cli")
    # ------------------------------------------------------------------
    # Compatibility commands routed through PostgreSQL (issue #36)
    # ------------------------------------------------------------------
    run_annotate = sub.add_parser("run-annotate")
    run_annotate.add_argument("external_id")
    run_annotate.add_argument(
        "--type", choices=("pivot", "retry", "decision"), required=True
    )
    run_annotate.add_argument("--reason", required=True)
    run_annotate.add_argument("--from-invocation")
    run_annotate.add_argument("--to-invocation")
    run_annotate.add_argument("--expected-revision", type=int)
    run_annotate.add_argument("--idempotency-key")
    run_annotate.add_argument("--actor", default="cli")
    run_verify = sub.add_parser("run-verify")
    run_verify.add_argument("external_id")
    run_verify.add_argument("--output", default="-")
    run_audit = sub.add_parser("run-audit")
    run_audit.add_argument("external_id")
    run_audit.add_argument("--target-hash")
    run_audit.add_argument(
        "--llm", choices=("local", "openai", "gemini"), default="local"
    )
    run_audit.add_argument("--model")
    run_audit.add_argument("--force", action="store_true")
    run_audit.add_argument("--stages")
    run_audit.add_argument("--max-calls", type=int)
    run_audit.add_argument("--max-input-tokens", type=int)
    run_audit.add_argument("--commercial-fallback", choices=("openai", "gemini"))
    run_audit.add_argument("--fallback-model")
    run_compare = sub.add_parser("run-compare")
    run_compare.add_argument("external_ids", nargs="+")
    budget_record = sub.add_parser("budget-record")
    budget_record.add_argument("external_id")
    budget_record.add_argument("--research-spec", required=True)
    budget_record.add_argument("--budget-snapshot", required=True)
    comparisons = sub.add_parser("legacy-comparisons")
    comparisons.add_argument("--research-run-id")
    comparisons.add_argument("--invocation-id")
    comparisons.add_argument(
        "--entry-point", choices=("frun", "fsearch_smart", "fsearch", "fscrape")
    )
    comparisons.add_argument("--divergent-only", action="store_true")
    comparisons.add_argument("--limit", type=int, default=100)

    search_plan_rec = sub.add_parser("search-plan-record")
    search_plan_rec.add_argument("external_id")
    search_plan_rec.add_argument("--research-spec-id", required=True)
    search_plan_rec.add_argument("--revision", type=int, required=True)
    search_plan_rec.add_argument("--search-plan", required=True)
    search_plan_rec.add_argument("--idempotency-key", required=True)

    search_plan_get = sub.add_parser("search-plan-get")
    search_plan_get.add_argument("external_id")
    search_plan_get.add_argument("--plan-id")
    search_plan_get.add_argument("--revision", type=int)

    plan_query_get = sub.add_parser("search-plan-query-get")
    plan_query_get.add_argument("query_id")

    search_resp_rec = sub.add_parser("search-response-record")
    search_resp_rec.add_argument("external_id")
    search_resp_rec.add_argument("--query-text", required=True)
    search_resp_rec.add_argument("--backend", default="firecrawl")
    search_resp_rec.add_argument(
        "--payload-file", help="Path to raw payload file (reads stdin if omitted)"
    )
    search_resp_rec.add_argument("--idempotency-key", required=True)
    search_resp_rec.add_argument("--plan-id")
    search_resp_rec.add_argument("--plan-query-id")
    search_resp_rec.add_argument("--provider-request-id")
    search_resp_rec.add_argument("--parser-version", default="firecrawl-search-v1")
    search_resp_rec.add_argument("--http-status", type=int)

    search_resp_get = sub.add_parser("search-response-get")
    search_resp_get.add_argument("response_id")

    search_resp_replay = sub.add_parser("search-response-replay")
    search_resp_replay.add_argument("response_id")

    cand_rec_resp = sub.add_parser("candidate-record-response")
    cand_rec_resp.add_argument("external_id")
    cand_rec_resp.add_argument("--search-response-id", required=True)

    cand_get = sub.add_parser("candidate-get")
    cand_get.add_argument("candidate_id")

    cand_list = sub.add_parser("candidate-list")
    cand_list.add_argument("external_id")
    cand_list.add_argument("--domain")
    cand_list.add_argument("--min-recurrence", type=int)
    cand_list.add_argument("--duplicate-group-id")

    cand_occ_list = sub.add_parser("candidate-occurrences-list")
    cand_occ_list.add_argument("candidate_id")

    cand_grp = sub.add_parser("candidate-assign-group")
    cand_grp.add_argument("candidate_ids", nargs="+")
    cand_grp.add_argument("--group-id")

    acq_search = sub.add_parser("acquisition-search")
    acq_search.add_argument("external_id")
    acq_search.add_argument("query_text")
    acq_search.add_argument("--backend", default="firecrawl")
    acq_search.add_argument("--limit", type=int, default=20)
    acq_search.add_argument("--sources", default="web")
    acq_search.add_argument("--tbs")
    acq_search.add_argument("--plan-id")
    acq_search.add_argument("--plan-query-id")
    acq_search.add_argument("--idempotency-key")
    acq_search.add_argument("--scratch-dir")

    acq_recon = sub.add_parser("acquisition-reconcile")
    acq_recon.add_argument("external_id")

    cand_list_pag = sub.add_parser("candidate-list-paginated")
    cand_list_pag.add_argument("external_id")
    cand_list_pag.add_argument("--plan-id")
    cand_list_pag.add_argument("--plan-query-id")
    cand_list_pag.add_argument("--query-text")
    cand_list_pag.add_argument("--domain")
    cand_list_pag.add_argument("--min-recurrence", type=int)
    cand_list_pag.add_argument("--duplicate-group-id")
    cand_list_pag.add_argument("--limit", type=int, default=20)
    cand_list_pag.add_argument("--offset", type=int, default=0)

    cand_card = sub.add_parser("candidate-card")
    cand_card.add_argument("candidate_id")
    cand_card.add_argument("--max-snippet-length", type=int, default=500)

    cand_triage = sub.add_parser("candidate-triage-input")
    cand_triage.add_argument("external_id")
    cand_triage.add_argument("--plan-id")
    cand_triage.add_argument("--plan-query-id")
    cand_triage.add_argument("--query-text")
    cand_triage.add_argument("--domain")
    cand_triage.add_argument("--min-recurrence", type=int)
    cand_triage.add_argument("--duplicate-group-id")
    cand_triage.add_argument("--limit", type=int, default=50)
    cand_triage.add_argument("--offset", type=int, default=0)
    cand_triage.add_argument("--max-snippet-length", type=int, default=500)

    cand_replay = sub.add_parser("candidate-replay")
    cand_replay.add_argument("external_id")
    cand_replay.add_argument("--plan-id")
    cand_replay.add_argument("--plan-query-id")
    cand_replay.add_argument("--domain")
    cand_replay.add_argument("--min-recurrence", type=int)
    cand_replay.add_argument("--limit", type=int, default=100)
    cand_replay.add_argument("--offset", type=int, default=0)

    exp_search_compat = sub.add_parser("export-search-compat")
    exp_search_compat.add_argument("external_id")
    exp_search_compat.add_argument("search_response_id")
    exp_search_compat.add_argument("--target-dir", required=True)
    exp_search_compat.add_argument("--idempotency-key")

    regen_search_exp = sub.add_parser("regenerate-search-exports")
    regen_search_exp.add_argument("external_id")
    regen_search_exp.add_argument("--target-dir", required=True)

    sub.add_parser("corpus-overview")
    search = sub.add_parser("search-assets")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--domain")
    search.add_argument("--source-type")
    search.add_argument("--date-from")
    search.add_argument("--date-to")
    search.add_argument("--research-run-id")
    inspect = sub.add_parser("inspect-asset")
    inspect.add_argument("id")
    fetch = sub.add_parser("fetch-passages")
    fetch.add_argument("ids", nargs="+")
    fetch.add_argument("--max-tokens", type=int, default=2000)
    fetch.add_argument("--max-passages", type=int, default=8)
    fetch.add_argument("--research-run-id")
    expand = sub.add_parser("expand-relationships")
    expand.add_argument("ids", nargs="+")
    expand.add_argument("--max-hops", type=int, default=1)
    expand.add_argument("--max-results", type=int, default=50)
    expand.add_argument("--max-tokens", type=int, default=2000)
    packet = sub.add_parser("build-evidence-packet")
    packet.add_argument("ids", nargs="+")
    packet.add_argument("--max-tokens", type=int, default=3000)

    # ------------------------------------------------------------------
    # Claim manifest commands (issue #32)
    # ------------------------------------------------------------------
    claim = sub.add_parser("claim-manifest")
    claim_sub = claim.add_subparsers(dest="claim_command", required=True)

    claim_import = claim_sub.add_parser("import")
    claim_import.add_argument("external_id")
    claim_import.add_argument("--file", required=True)
    claim_import.add_argument("--dry-run", action="store_true")

    claim_export = claim_sub.add_parser("export")
    claim_export.add_argument("external_id")
    claim_export.add_argument("--output", required=True)

    claim_list = claim_sub.add_parser("list")
    claim_list.add_argument("external_id")

    # ------------------------------------------------------------------
    # Audit subcommands (issue #33)
    # ------------------------------------------------------------------
    audit = sub.add_parser("audit")
    audit.add_argument("external_id")
    audit.add_argument("--target-hash", required=True)
    audit.add_argument("--evaluator-version", default="catalog-v5.0")
    audit.add_argument("--prompt-template-version", default="staged-research-audit-v1")
    audit.add_argument("--policy-version", default="audit-policy-v1")
    audit.add_argument("--stages", default="rubric,acquisition,evidence,synthesis")
    audit.add_argument(
        "--status", default="partial", choices=["completed", "partial", "failed"]
    )
    audit.add_argument("--provider", default="local")
    audit.add_argument("--model")
    audit.add_argument("--prompt-hash")
    audit.add_argument("--model-fingerprint", required=True)
    audit.add_argument("--elapsed-ms", type=int, default=0)
    audit.add_argument("--packet-manifest-file")

    audit_status = sub.add_parser("audit-status")
    audit_status.add_argument("external_id")
    audit_status.description = "Show the latest audit assessment for a research run"

    audit_query = sub.add_parser("audit-query")
    audit_query.add_argument("external_id")
    audit_query.add_argument("--status-filter")
    audit_query.add_argument("--limit", type=int, default=100)
    audit_query.add_argument("--offset", type=int, default=0)

    audit_export = sub.add_parser("audit-export")
    audit_export.add_argument("assessment_id")
    audit_export.add_argument("--output", default="-")

    audit_staleness = sub.add_parser("audit-staleness")
    audit_staleness.add_argument("external_id")
    audit_staleness.add_argument("--target-hash", required=True)

    # ------------------------------------------------------------------
    # Catalog import commands (issue #37)
    # ------------------------------------------------------------------
    catalog_import = sub.add_parser("catalog-import")
    catalog_import.add_argument(
        "catalog_root",
        help="Path to the Catalog v5 root directory to import",
    )
    catalog_import.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help=(
            "Apply the import (write to PostgreSQL). "
            "Default is dry-run (no writes). "
            "Exit codes: 0 = success, 1 = conflicts/omissions detected, "
            "2 = errors (e.g. all records malformed)."
        ),
    )
    catalog_import.add_argument(
        "--report",
        help="Path to write the import report as JSON",
    )

    catalog_reconcile = sub.add_parser("catalog-reconcile")
    catalog_reconcile.add_argument(
        "--report",
        help="Path to write the reconciliation report as JSON",
    )

    return root


def _db(config):
    config.require_database()
    return connect(config.database_url)


def _uow_factory(config):
    return partial(
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


def _cmd_normalize(config, args) -> int:
    """Run normalization diagnostics on document blocks (issue #45).

    Runs normalization on document blocks and persists the results to
    ``normalized_blocks`` and ``transformation_records`` tables.  The
    operation is idempotent — re-running normalizes the same blocks and
    upserts the results.

    Args:
        config: Store configuration.
        args: Parsed CLI arguments.

    Returns:
        Exit code (0 = success).
    """
    from uuid import UUID as _UUID

    from .normalization import NormalizationService

    config.require_database()
    with _db(config) as conn, conn.cursor() as cur:
        if args.all:
            cur.execute(
                """SELECT d.id, d.title, d.requested_url, d.content_sha256,
                   db.id AS block_id, db.ordinal, db.block_type,
                   db.char_start, db.char_end, db.text,
                   db.parser_version
                   FROM documents d
                   JOIN document_blocks db ON db.document_id = d.id
                   ORDER BY d.id, db.ordinal"""
            )
            rows = cur.fetchall()
        elif args.document:
            doc_uuid = _UUID(args.document)
            cur.execute(
                """SELECT d.id, d.title, d.requested_url, d.content_sha256,
                   db.id AS block_id, db.ordinal, db.block_type,
                   db.char_start, db.char_end, db.text,
                   db.parser_version
                   FROM documents d
                   JOIN document_blocks db ON db.document_id = d.id
                   WHERE d.id = %s
                   ORDER BY db.ordinal""",
                (doc_uuid,),
            )
            rows = cur.fetchall()
        else:
            print(dumps({"error": "specify --document <uuid> or --all"}))
            return 1

    if not rows:
        print(dumps({"message": "no blocks found", "normalized": 0}))
        return 0

    # Group rows by document
    docs: dict[tuple, list] = {}
    for row in rows:
        doc_key = (str(row[0]), row[1], row[2], row[3])
        docs.setdefault(doc_key, []).append(row)

    service = NormalizationService(
        aggressive=args.aggressive,
        document_type=args.document_type,
    )

    results = []
    upserted_blocks = 0
    upserted_transforms = 0

    for doc_key, doc_rows in docs.items():
        doc_id = _UUID(doc_key[0])
        title = doc_key[1]
        url = doc_key[2]
        sha = doc_key[3]

        block_ids = []
        for row in doc_rows:
            block_ids.append(_UUID(row[4]))

        # Build TypedBlock for normalization
        from research_store.parsing.interfaces import TypedBlock

        typed_blocks = []
        for row in doc_rows:
            typed_blocks.append(
                TypedBlock(
                    ordinal=int(row[5]),
                    block_type=row[6],
                    text=row[9] or "",
                    heading_path=(),
                    char_start=int(row[7]) if row[7] is not None else None,
                    char_end=int(row[8]) if row[8] is not None else None,
                    parser_version=row[10] or "canonical-v1",
                )
            )

        norm_result = service.normalize(
            blocks=typed_blocks,
            source_block_ids=block_ids,
            document_id=doc_id,
            document_type=args.document_type,
        )

        # Persist normalized blocks and transformation records
        with conn.cursor() as block_cur:
            for nb in (
                norm_result.blocks
                + norm_result.suppressed_blocks
                + norm_result.removed_blocks
            ):
                block_cur.execute(
                    """INSERT INTO normalized_blocks
                       (id, source_block_id, document_id, ordinal, block_type,
                        text, heading_path, char_start, char_end, disposition,
                        rule_version, transformation_reason, parser_version)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (source_block_id, rule_version) DO UPDATE SET
                         disposition = EXCLUDED.disposition,
                         transformation_reason = EXCLUDED.transformation_reason,
                         text = EXCLUDED.text,
                         char_start = EXCLUDED.char_start,
                         char_end = EXCLUDED.char_end,
                         parser_version = EXCLUDED.parser_version""",
                    (
                        str(nb.id),
                        str(nb.source_block_id),
                        str(nb.document_id) if nb.document_id else None,
                        nb.ordinal,
                        nb.block_type,
                        nb.text if nb.disposition != "remove" else "",
                        list(nb.heading_path) if nb.heading_path else None,
                        nb.char_start,
                        nb.char_end,
                        nb.disposition,
                        nb.rule_version,
                        nb.transformation_reason,
                        nb.parser_version,
                    ),
                )

        with conn.cursor() as transform_cur:
            for tr in norm_result.transformations:
                transform_cur.execute(
                    """INSERT INTO transformation_records
                       (id, normalized_block_id, rule_id, rule_version,
                        reason, before_text, after_text, confidence)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (normalized_block_id, rule_id) DO UPDATE SET
                         rule_id = EXCLUDED.rule_id,
                         reason = EXCLUDED.reason,
                         before_text = EXCLUDED.before_text,
                         after_text = EXCLUDED.after_text,
                         confidence = EXCLUDED.confidence""",
                    (
                        str(tr.id),
                        str(tr.normalized_block_id) if tr.normalized_block_id else None,
                        tr.rule_id,
                        tr.rule_version,
                        tr.reason,
                        tr.before_text,
                        tr.after_text,
                        tr.confidence,
                    ),
                )
            conn.commit()

        upserted_blocks += len(
            norm_result.blocks
            + norm_result.suppressed_blocks
            + norm_result.removed_blocks
        )
        upserted_transforms += len(norm_result.transformations)

        results.append(
            {
                "document_id": str(doc_id),
                "title": title,
                "url": url,
                "content_sha256": sha,
                "source_block_count": len(typed_blocks),
                "kept": len([b for b in norm_result.blocks if b.disposition == "keep"]),
                "altered": len(
                    [b for b in norm_result.blocks if b.disposition == "alter"]
                ),
                "suppressed": len(norm_result.suppressed_blocks),
                "removed": len(norm_result.removed_blocks),
                "transformations": len(norm_result.transformations),
                "diagnostics": norm_result.diagnostics(),
            }
        )

    output = {
        "rule_version": NORMALIZATION_VERSION,
        "aggressive": args.aggressive,
        "document_type": args.document_type,
        "documents_processed": len(results),
        "normalized_blocks_upserted": upserted_blocks,
        "transformation_records_upserted": upserted_transforms,
        "documents": results,
    }
    print(dumps(output))
    return 0


def _qdrant(config, collection=None, dimension=None, distance="Cosine"):
    return QdrantIndex(
        config.qdrant_url,
        config.qdrant_api_key,
        collection or config.qdrant_alias,
        dimension or config.embedding_dimension,
        distance,
    )


def _worker(config):
    if not config.embedding_url:
        raise RuntimeError("EMBEDDING_URL is required to process index jobs")
    return IndexWorker(
        _uow_factory(config),
        _qdrant(config),
        OpenAICompatibleEmbedder(
            config.embedding_url,
            config.embedding_model,
            config.embedding_api_key,
            config.embedding_dimension,
            config.embedding_fingerprint,
        ),
        queue=ValkeyQueue(config.valkey_url),
        lease_seconds=config.job_lease_seconds,
        max_attempts=config.max_index_attempts,
    )


def _schema_state(config):
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    ini = Path(__file__).parents[2] / "alembic.ini"
    head = ScriptDirectory.from_config(Config(str(ini))).get_current_head()
    with _db(config) as conn, conn.cursor() as cur:
        cur.execute("SELECT version_num FROM alembic_version")
        row = cur.fetchone()
        current = row[0] if row else None
    return {"current": current, "head": head, "at_head": current == head}


def _resolve_run_id(config, external_id):
    if not external_id:
        return None
    with _db(config) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id,status FROM research_runs WHERE external_run_id=%s",
            (external_id,),
        )
        row = cur.fetchone()
    if not row:
        raise SystemExit(f"research run not found: {external_id}")
    if row[1] != "running":
        raise SystemExit(f"research run is finished; reopen it first: {external_id}")
    return row[0]


def _resolve_any_run_id(config, external_id):
    """Resolve a run UUID from its external_run_id, accepting any lifecycle status.

    Used by read-only claim-manifest subcommands (export, list) where the run
    may already be finished.  Use :func:`_resolve_run_id` for write operations
    that require the run to be in ``running`` status.
    """
    if not external_id:
        return None
    with _db(config) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM research_runs WHERE external_run_id=%s",
            (external_id,),
        )
        row = cur.fetchone()
    if not row:
        raise SystemExit(f"research run not found: {external_id}")
    return row[0]


def _index_rows(config):
    with _db(config) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT d.id,d.fingerprint,d.physical_collection,d.model_name,
            d.model_revision,d.dimension,d.distance_metric,d.normalization,
            d.instruction_template_hash,d.lifecycle_status,d.created_at,d.activated_at,
            count(m.id),count(m.id) FILTER(WHERE m.index_status='complete')
            FROM index_definitions d
            LEFT JOIN embedding_manifests m ON m.index_definition_id=d.id
            GROUP BY d.id ORDER BY d.created_at DESC"""
        )
        keys = (
            "id",
            "fingerprint",
            "physical_collection",
            "model_name",
            "model_revision",
            "dimension",
            "distance_metric",
            "normalization",
            "instruction_template_hash",
            "lifecycle_status",
            "created_at",
            "activated_at",
            "manifest_count",
            "complete_count",
        )
        return [dict(zip(keys, row)) for row in cur.fetchall()]


def _active_chunk_ids(config, document_id=None):
    with _db(config) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT c.id FROM chunks c JOIN documents d ON d.id=c.document_id
            WHERE d.parser_version=%s AND d.normalization_version=%s
              AND c.chunker_version=%s
              AND (%s::uuid IS NULL OR c.document_id=%s::uuid)
            ORDER BY c.id""",
            (
                config.parser_version,
                config.normalization_version,
                config.chunker_version,
                document_id,
                document_id,
            ),
        )
        return {row[0] for row in cur.fetchall()}


def _derivation_filter(config):
    return {
        "must": [
            {"key": "parser_version", "match": {"value": config.parser_version}},
            {
                "key": "normalization_version",
                "match": {"value": config.normalization_version},
            },
            {"key": "chunker_version", "match": {"value": config.chunker_version}},
        ]
    }


def _index_build(config, document_id=None):
    with _uow_factory(config)() as uow:
        definition = uow.ensure_index_definition()
    index = _qdrant(
        config,
        definition["physical_collection"],
        definition["dimension"],
        definition["distance_metric"],
    )
    schema = index.ensure_schema()
    selected_chunk_ids = _active_chunk_ids(config, document_id)
    indexed_ids, offset = set(), None
    while True:
        page = index.point_ids(offset, filters=_derivation_filter(config))
        indexed_ids.update(UUID(str(item["id"])) for item in page.get("points", []))
        offset = page.get("next_page_offset")
        if not offset:
            break
    with _db(config) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO embedding_manifests(
            chunk_id,model_name,model_revision,dimension,distance_metric,
            normalization,instruction_template_hash,qdrant_collection,
            qdrant_point_id,index_status,index_definition_id)
            SELECT c.id,%s,%s,%s,%s,%s,%s,%s,c.id,'pending',%s
            FROM chunks c WHERE c.id=ANY(%s)
            ON CONFLICT(chunk_id,index_definition_id) DO UPDATE
            SET qdrant_collection=excluded.qdrant_collection
            RETURNING id,chunk_id,index_status""",
            (
                definition["model_name"],
                definition["model_revision"],
                definition["dimension"],
                definition["distance_metric"],
                definition["normalization"],
                definition["instruction_template_hash"],
                definition["physical_collection"],
                definition["id"],
                list(selected_chunk_ids),
            ),
        )
        manifests = cur.fetchall()
        manifest_ids = [row[0] for row in manifests]
        requeue_ids = [
            row[0]
            for row in manifests
            if row[1] not in indexed_ids or row[2] != "complete"
        ]
        cur.execute(
            """INSERT INTO index_jobs(
            entity_type,entity_id,index_name,operation,status,manifest_id,index_definition_id)
            SELECT 'chunk',m.chunk_id,%s,'upsert','pending',m.id,%s
            FROM embedding_manifests m WHERE m.id=ANY(%s)
            ON CONFLICT(manifest_id,operation) DO NOTHING""",
            (definition["physical_collection"], definition["id"], manifest_ids),
        )
        if requeue_ids:
            cur.execute(
                """UPDATE index_jobs SET status='pending',available_at=now(),
                started_at=NULL,completed_at=NULL,error=NULL,lease_token=NULL,
                lease_owner=NULL,lease_expires_at=NULL,updated_at=now()
                WHERE manifest_id=ANY(%s) AND operation='upsert'""",
                (requeue_ids,),
            )
            cur.execute(
                """UPDATE embedding_manifests SET index_status='pending',
                indexed_at=NULL,error=NULL WHERE id=ANY(%s)""",
                (requeue_ids,),
            )
    queue = ValkeyQueue(config.valkey_url)
    if manifest_ids:
        queue.notify(manifest_ids[0])
    return {
        "index_definition": definition,
        "selected_chunks": len(manifest_ids),
        "scheduled": len(requeue_ids),
        "qdrant_schema": schema,
    }


def _recover_activation(config):
    aliases = _qdrant(config).list_aliases()
    active_collection = aliases.get(config.qdrant_alias)
    recovered = []
    with _db(config) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT j.id,j.target_definition_id,d.physical_collection
            FROM index_activation_journal j
            JOIN index_definitions d ON d.id=j.target_definition_id
            WHERE j.status IN ('prepared','switched') ORDER BY j.created_at"""
        )
        for journal_id, definition_id, collection in cur.fetchall():
            if active_collection == collection:
                cur.execute(
                    "UPDATE index_definitions SET lifecycle_status='inactive' WHERE lifecycle_status='active' AND id<>%s",
                    (definition_id,),
                )
                cur.execute(
                    "UPDATE index_definitions SET lifecycle_status='active',activated_at=now() WHERE id=%s",
                    (definition_id,),
                )
                cur.execute(
                    "UPDATE index_activation_journal SET status='complete',updated_at=now() WHERE id=%s",
                    (journal_id,),
                )
                recovered.append(str(journal_id))
            else:
                cur.execute(
                    """UPDATE index_activation_journal SET status='failed',updated_at=now(),
                    error='alias did not switch to prepared target' WHERE id=%s""",
                    (journal_id,),
                )
    return recovered


def _activate_index(config, identifier, action):
    recovered = _recover_activation(config)
    with _db(config) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT id,physical_collection,dimension,distance_metric
            FROM index_definitions WHERE id=%s""",
            (UUID(identifier),),
        )
        row = cur.fetchone()
        if not row:
            raise SystemExit("index definition not found")
        definition_id, collection, dimension, distance = row
        active_chunk_ids = _active_chunk_ids(config)
        total_chunks = len(active_chunk_ids)
        cur.execute(
            """SELECT count(*) FROM embedding_manifests m
            JOIN chunks c ON c.id=m.chunk_id JOIN documents d ON d.id=c.document_id
            WHERE m.index_definition_id=%s AND m.index_status='complete'
              AND d.parser_version=%s AND d.normalization_version=%s
              AND c.chunker_version=%s""",
            (
                definition_id,
                config.parser_version,
                config.normalization_version,
                config.chunker_version,
            ),
        )
        complete = cur.fetchone()[0]
        if complete != total_chunks:
            raise SystemExit(
                f"index coverage incomplete: {complete} complete manifests for {total_chunks} chunks"
            )
        cur.execute(
            "SELECT id FROM index_definitions WHERE lifecycle_status='active' LIMIT 1"
        )
        previous = cur.fetchone()
    index = _qdrant(config, collection, dimension, distance)
    schema = index.inspect_schema()
    if not schema["exists"] or not schema["compatible"]:
        raise SystemExit(f"target collection schema is not compatible: {schema}")
    point_ids, offset = set(), None
    while True:
        page = index.point_ids(offset, filters=_derivation_filter(config))
        point_ids.update(str(item["id"]) for item in page.get("points", []))
        offset = page.get("next_page_offset")
        if not offset:
            break
    chunk_ids = {str(value) for value in active_chunk_ids}
    if point_ids != chunk_ids:
        raise SystemExit(
            f"Qdrant coverage mismatch: missing={len(chunk_ids - point_ids)} orphaned={len(point_ids - chunk_ids)}"
        )
    if total_chunks:
        index.search([1.0] + [0.0] * (dimension - 1), {}, 1)
    with _db(config) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO index_activation_journal(
            target_definition_id,previous_definition_id,action)
            VALUES(%s,%s,%s) RETURNING id""",
            (definition_id, previous[0] if previous else None, action),
        )
        journal_id = cur.fetchone()[0]
    switched = index.switch_alias(config.qdrant_alias, collection)
    with _db(config) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE index_activation_journal SET status='switched',updated_at=now() WHERE id=%s",
            (journal_id,),
        )
        cur.execute(
            "UPDATE index_definitions SET lifecycle_status='inactive' WHERE lifecycle_status='active' AND id<>%s",
            (definition_id,),
        )
        cur.execute(
            "UPDATE index_definitions SET lifecycle_status='active',activated_at=now() WHERE id=%s",
            (definition_id,),
        )
        cur.execute(
            "UPDATE index_activation_journal SET status='complete',updated_at=now() WHERE id=%s",
            (journal_id,),
        )
    return {
        "action": action,
        "index_definition_id": definition_id,
        "collection": collection,
        "alias": config.qdrant_alias,
        "switched": switched,
        "recovered_journals": recovered,
        "coverage": total_chunks,
    }


def _blob_health(config):
    store = ContentAddressedBlobStore(config.blob_root)
    with _db(config) as conn, conn.cursor() as cur:
        cur.execute("SELECT id,content_sha256 FROM asset_snapshots")
        references = {digest: snapshot_id for snapshot_id, digest in cur.fetchall()}
    missing = [
        {"snapshot_id": references[digest], "sha256": digest}
        for digest in references
        if not store.verify(digest)
    ]
    disk_hashes = {
        path.name
        for path in config.blob_root.rglob("*")
        if path.is_file()
        and len(path.name) == 64
        and all(character in "0123456789abcdef" for character in path.name)
    }
    return {
        "ok": not missing and not (disk_hashes - references.keys()),
        "referenced": len(references),
        "missing_or_corrupt": missing,
        "unreferenced": sorted(disk_hashes - references.keys()),
    }


def _doctor(config):
    checks, failed = {}, False
    try:
        checks["schema"] = _schema_state(config)
        if not checks["schema"]["at_head"]:
            failed = True
        with _db(config) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT status,count(*) FROM index_jobs GROUP BY status ORDER BY status"
            )
            checks["index_jobs"] = dict(cur.fetchall())
            if checks["schema"]["at_head"]:
                cur.execute(
                    """SELECT count(*) FILTER(WHERE status IN ('partial','failed')),
                    min(started_at) FILTER(WHERE status='running') FROM ingestion_batches"""
                )
                bad, oldest_running = cur.fetchone()
                checks["ingestion_batches"] = {
                    "partial_or_failed": bad,
                    "oldest_running": oldest_running,
                }
        if checks["schema"]["at_head"]:
            with _uow_factory(config)() as uow:
                checks["worker"] = uow.worker_status()
            workers = checks["worker"]["workers"]
            threshold = max(90, config.worker_poll_seconds * 4)
            age = (
                (
                    datetime.now(timezone.utc) - workers[0]["heartbeat_at"]
                ).total_seconds()
                if workers
                else None
            )
            checks["worker"]["latest_heartbeat_age_seconds"] = (
                round(age, 3) if age is not None else None
            )
            checks["worker"]["heartbeat_freshness_threshold_seconds"] = threshold
            checks["worker"]["current_worker_available"] = (
                age is not None and age <= threshold
            ) or checks["worker"]["active_leases"] > 0
            if checks["worker"]["dead_jobs"] or checks["worker"]["stale_leases"]:
                failed = True
            if not checks["worker"]["current_worker_available"]:
                failed = True
        else:
            checks["worker"] = {"available": False, "reason": "migration required"}
    except Exception as exc:
        checks["postgres"] = {"ok": False, "error": str(exc)}
        failed = True

    try:
        if not config.blob_root.is_dir():
            raise RuntimeError(f"blob root is not a directory: {config.blob_root}")
        if not os.access(config.blob_root, os.R_OK | os.X_OK):
            raise RuntimeError("blob root is not readable")
        checks["blobs"] = _blob_health(config)
        failed |= not checks["blobs"]["ok"]
    except Exception as exc:
        checks["blobs"] = {"ok": False, "error": str(exc)}
        failed = True

    try:
        aliases = _qdrant(config).list_aliases()
        active = aliases.get(config.qdrant_alias)
        qdrant = {"ok": True, "alias": config.qdrant_alias, "collection": active}
        if active:
            if not checks.get("schema", {}).get("at_head"):
                qdrant["schema"] = _qdrant(config, active).inspect_schema()
                checks["qdrant"] = qdrant
                active = None
        if active:
            rows = [
                row
                for row in _index_rows(config)
                if row["physical_collection"] == active
            ]
            if not rows:
                raise RuntimeError("active alias is not backed by an index definition")
            row = rows[0]
            qdrant["query_embedding_compatible"] = (
                row["fingerprint"] == config.embedding_fingerprint
            )
            if not qdrant["query_embedding_compatible"]:
                failed = True
            qdrant["schema"] = _qdrant(
                config, active, row["dimension"], row["distance_metric"]
            ).inspect_schema()
            if not qdrant["schema"]["compatible"]:
                failed = True
            if checks.get("schema", {}).get("at_head"):
                point_ids, offset = set(), None
                active_index = _qdrant(
                    config, active, row["dimension"], row["distance_metric"]
                )
                while True:
                    page = active_index.point_ids(
                        offset, filters=_derivation_filter(config)
                    )
                    point_ids.update(str(item["id"]) for item in page.get("points", []))
                    offset = page.get("next_page_offset")
                    if not offset:
                        break
                chunk_ids = {str(value) for value in _active_chunk_ids(config)}
                qdrant["coverage"] = {
                    "missing": len(chunk_ids - point_ids),
                    "orphaned": len(point_ids - chunk_ids),
                }
                failed |= point_ids != chunk_ids
        checks["qdrant"] = qdrant
    except Exception as exc:
        checks["qdrant"] = {"ok": False, "error": str(exc)}
        failed = True

    try:
        import redis

        checks["valkey"] = {"ok": bool(redis.Redis.from_url(config.valkey_url).ping())}
    except Exception as exc:
        checks["valkey"] = {"ok": False, "error": str(exc)}
        failed = True

    for name, endpoint in (
        ("embedding", config.embedding_url),
        ("reranker", config.reranker_url),
    ):
        try:
            if not endpoint:
                raise RuntimeError(f"{name.upper()}_URL is not configured")
            if name == "embedding":
                vector = OpenAICompatibleEmbedder(
                    endpoint,
                    config.embedding_model,
                    config.embedding_api_key,
                    config.embedding_dimension,
                )("research-store-doctor")
                checks[name] = {"ok": True, "dimension": len(vector)}
            else:
                ranked = CohereCompatibleReranker(
                    endpoint, config.reranker_model, config.reranker_api_key
                )(
                    "research database",
                    [
                        {"candidate_id": "relevant", "excerpt": "research database"},
                        {"candidate_id": "other", "excerpt": "yellow bananas"},
                    ],
                )
                if not ranked or ranked[0]["candidate_id"] != "relevant":
                    raise RuntimeError("unexpected reranker ordering")
                checks[name] = {"ok": True}
        except Exception as exc:
            checks[name] = {"ok": False, "error": str(exc)}
            failed = True
    checks["configuration"] = {
        "embedding_fingerprint": config.embedding_fingerprint,
        "physical_collection": config.physical_collection,
        "normalization_version": config.normalization_version,
        "parser_version": config.parser_version,
        "chunker_version": config.chunker_version,
    }
    return checks, failed


def main(argv=None):
    args = parser().parse_args(argv)
    config = StoreConfig.from_env()

    if args.command == "migrate":
        config.require_database()
        try:
            from alembic import command
            from alembic.config import Config
        except ImportError as exc:
            raise RuntimeError(
                "migrations require dependencies from requirements-research-store.txt"
            ) from exc
        ini = Path(__file__).parents[2] / "alembic.ini"
        command.upgrade(Config(str(ini)), "head")
        print(dumps(_schema_state(config)))
        return 0
    if args.command == "status":
        schema = _schema_state(config)
        with _db(config) as conn, conn.cursor() as cur:
            cur.execute("SELECT status,count(*) FROM index_jobs GROUP BY status")
            jobs = dict(cur.fetchall())
            if schema["at_head"]:
                cur.execute(
                    "SELECT status,count(*) FROM ingestion_batches GROUP BY status"
                )
                batches = dict(cur.fetchall())
            else:
                batches = {"available": False, "reason": "migration required"}
        print(dumps({"schema": schema, "index_jobs": jobs, "batches": batches}))
        return 0 if schema["at_head"] else 1
    if args.command == "doctor":
        checks, failed = _doctor(config)
        print(dumps(checks))
        return 1 if failed else 0
    if args.command == "ingest-ready":
        schema = _schema_state(config)
        if not schema["at_head"]:
            raise SystemExit(
                f"research store migration required: {schema['current']} != {schema['head']}"
            )
        if not config.blob_root.is_dir():
            raise SystemExit(f"blob root is not writable: {config.blob_root}")
        with _db(config) as conn, conn.cursor() as cur:
            required_privileges = {
                "sources": ("SELECT", "INSERT", "UPDATE"),
                "asset_snapshots": ("SELECT", "INSERT"),
                "documents": ("SELECT", "INSERT"),
                "document_blocks": ("SELECT", "INSERT"),
                "chunks": ("SELECT", "INSERT"),
                "embedding_manifests": ("SELECT", "INSERT", "UPDATE"),
                "index_definitions": ("SELECT", "INSERT", "UPDATE"),
                "index_jobs": ("SELECT", "INSERT", "UPDATE"),
                "ingestion_batches": ("SELECT", "INSERT", "UPDATE"),
                "ingestion_batch_assets": (
                    "SELECT",
                    "INSERT",
                    "UPDATE",
                    "DELETE",
                ),
                "research_runs": ("SELECT", "INSERT", "UPDATE"),
                "research_run_assets": ("SELECT", "INSERT", "UPDATE"),
                "retrieval_events": ("SELECT", "INSERT"),
            }
            missing = []
            for table, privileges in required_privileges.items():
                for privilege in privileges:
                    cur.execute(
                        "SELECT has_table_privilege(current_user,%s,%s)",
                        (f"public.{table}", privilege),
                    )
                    if not cur.fetchone()[0]:
                        missing.append(f"{table}:{privilege}")
            if missing:
                raise SystemExit(
                    "database role lacks corpus privileges: " + ", ".join(missing)
                )
            probe_url = f"https://ingest-ready.invalid/{uuid4()}"
            cur.execute(
                """INSERT INTO sources(canonical_url,metadata)
                VALUES(%s,%s) ON CONFLICT(canonical_url) DO UPDATE
                SET metadata=sources.metadata || excluded.metadata""",
                (probe_url, json.dumps({"ingest_ready_probe": True})),
            )
            conn.rollback()
        probe_path = renamed_path = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=config.blob_root,
                prefix=".firecrawl-ingest-ready-",
                delete=False,
            ) as probe:
                probe.write(b"research-store-ingest-ready")
                probe.flush()
                os.fsync(probe.fileno())
                probe_path = Path(probe.name)
            renamed_path = probe_path.with_suffix(".verified")
            os.replace(probe_path, renamed_path)
            probe_path = None
        except OSError as exc:
            raise SystemExit(f"blob root write probe failed: {exc}") from exc
        finally:
            for path in (probe_path, renamed_path):
                if path is not None:
                    path.unlink(missing_ok=True)
        print(dumps({"ready": True, "schema": schema, "blob_root": config.blob_root}))
        return 0
    # ------------------------------------------------------------------
    # Parser info (issue #44)
    # ------------------------------------------------------------------
    if args.command == "parser-info":
        from .parsing import get_registry

        registry = get_registry()
        info = {
            "parser_registry_version": config.parser_registry_version,
            "parser_version": config.parser_version,
            "normalization_version": config.normalization_version,
            "chunker_version": config.chunker_version,
            "registered_parsers": registry.list_registered(),
        }
        print(dumps(info))
        return 0
    if args.command == "import-scratch":
        root = Path(args.path) if args.path else config.scratch_root
        report = _import_scratch(
            root, None if args.dry_run else build_service(config), args.dry_run
        )
        if args.report:
            _export_json(Path(args.report), report)
        print(dumps(report))
        return 1 if report["failed"] else 0
    if args.command == "ingest-result":
        path = Path(args.file)
        result = build_service(config).ingest(
            IngestRequest(
                requested_url=args.url,
                content=path.read_bytes(),
                mime_type="application/json"
                if path.suffix == ".json"
                else "text/markdown",
                title=args.title,
                metadata=json.loads(args.metadata_json),
            )
        )
        print(dumps(result.__dict__))
        return 0
    if args.command == "verify-blobs":
        health = _blob_health(config)
        print(dumps(health))
        return 0 if health["ok"] else 1
    if args.command in {"worker", "index-once"}:
        worker = _worker(config)
        if args.command == "index-once":
            result = worker.run_forever(batch_size=args.limit, once=True)
        else:
            worker.lease_seconds = args.lease_seconds or config.job_lease_seconds
            worker.max_attempts = args.max_attempts or config.max_index_attempts
            result = worker.run_forever(
                batch_size=args.batch_size,
                poll_seconds=args.poll_seconds or config.worker_poll_seconds,
                once=args.once,
            )
        print(dumps(result))
        return 1 if result["failed"] else 0
    if args.command == "index-list":
        print(
            dumps(
                {
                    "alias": config.qdrant_alias,
                    "aliases": _qdrant(config).list_aliases(),
                    "definitions": _index_rows(config),
                }
            )
        )
        return 0
    if args.command in {"index-build", "reindex"}:
        print(dumps(_index_build(config, args.document)))
        return 0
    if args.command == "index-activate":
        print(dumps(_activate_index(config, args.id, "activate")))
        return 0
    if args.command == "index-rollback":
        print(dumps(_activate_index(config, args.id, "rollback")))
        return 0
    if args.command == "index-prune":
        if args.dry_run and args.force:
            raise SystemExit("--dry-run and --force are mutually exclusive")
        if args.force and not args.index_id:
            raise SystemExit("--force requires --index-id for an exact prune target")
        if args.keep_last < 0:
            raise SystemExit("--keep-last must be non-negative")
        aliases = _qdrant(config).list_aliases()
        active = aliases.get(config.qdrant_alias)
        rows = _index_rows(config)
        if args.index_id:
            rows = [row for row in rows if str(row["id"]) == args.index_id]
            if not rows:
                raise SystemExit("index definition not found")
        else:
            rows = rows[args.keep_last :]
        candidates = [row for row in rows if row["physical_collection"] != active]
        result = {
            "action": "deleted" if args.force else "dry_run",
            "indexes": [
                {"id": row["id"], "collection": row["physical_collection"]}
                for row in candidates
            ],
        }
        if args.force:
            for row in candidates:
                _qdrant(
                    config,
                    row["physical_collection"],
                    row["dimension"],
                    row["distance_metric"],
                ).delete_collection()
        print(dumps(result))
        return 0
    if args.command == "reconcile-qdrant":
        aliases = _qdrant(config).list_aliases()
        collection = aliases.get(config.qdrant_alias)
        if not collection:
            raise SystemExit(f"Qdrant alias is not configured: {config.qdrant_alias}")
        rows = [
            row
            for row in _index_rows(config)
            if row["physical_collection"] == collection
        ]
        if not rows:
            raise SystemExit("active collection has no PostgreSQL index definition")
        index = _qdrant(
            config, collection, rows[0]["dimension"], rows[0]["distance_metric"]
        )
        qdrant_ids, offset = set(), None
        while True:
            page = index.point_ids(offset, filters=_derivation_filter(config))
            qdrant_ids.update(str(item["id"]) for item in page.get("points", []))
            offset = page.get("next_page_offset")
            if not offset:
                break
        postgres_ids = {str(value) for value in _active_chunk_ids(config)}
        print(
            dumps(
                {
                    "collection": collection,
                    "orphaned_qdrant": sorted(qdrant_ids - postgres_ids),
                    "missing_qdrant": sorted(postgres_ids - qdrant_ids),
                }
            )
        )
        return 0 if qdrant_ids == postgres_ids else 1
    if args.command == "prune-cache":
        print(dumps({"deleted": ValkeyQueue(config.valkey_url).prune_cache()}))
        return 0
    if args.command == "rederive":
        service = build_service(config)
        with _db(config) as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT a.id,a.requested_url,a.final_url,a.retrieved_at,a.http_status,
                a.etag,a.last_modified,a.mime_type,a.content_sha256,a.firecrawl_version,
                a.crawl_options,d.title,d.published_at,d.metadata
                FROM asset_snapshots a LEFT JOIN LATERAL(
                  SELECT title,published_at,metadata FROM documents
                  WHERE snapshot_id=a.id ORDER BY id DESC LIMIT 1
                ) d ON true WHERE (%s::uuid IS NULL OR a.id=%s::uuid)
                ORDER BY a.retrieved_at,a.id""",
                (args.snapshot, args.snapshot),
            )
            snapshots = cur.fetchall()
        store = ContentAddressedBlobStore(config.blob_root)
        results = []
        for row in snapshots:
            with store.open(row[8]) as handle:
                content = handle.read()
            result = service.ingest(
                IngestRequest(
                    requested_url=row[1],
                    final_url=row[2],
                    retrieved_at=row[3],
                    http_status=row[4],
                    etag=row[5],
                    last_modified=row[6],
                    mime_type=row[7] or "text/markdown",
                    content=content,
                    firecrawl_version=row[9],
                    crawl_options=row[10] or {},
                    title=row[11],
                    published_at=row[12],
                    metadata=row[13] or {},
                )
            )
            results.append(result.__dict__)
        print(dumps({"rederived": len(results), "assets": results}))
        return 0
    if args.command == "normalize":
        return _cmd_normalize(config, args)
    if args.command == "export-invocation":
        with _uow_factory(config)() as uow:
            result = uow.export_invocation(args.invocation_id)
        _export_json(Path(args.output), result)
        print(dumps(result))
        return 0
    if args.command == "export-run":
        with _db(config) as conn, conn.cursor() as cur:
            try:
                internal_id = UUID(args.id)
                cur.execute(
                    "SELECT row_to_json(r) FROM research_runs r WHERE id=%s",
                    (internal_id,),
                )
            except ValueError:
                cur.execute(
                    "SELECT row_to_json(r) FROM research_runs r WHERE external_run_id=%s",
                    (args.id,),
                )
            run = cur.fetchone()
            if not run:
                raise SystemExit("research run not found")
            internal_id = run[0]["id"]
            cur.execute(
                "SELECT row_to_json(e) FROM retrieval_events e WHERE run_id=%s ORDER BY created_at",
                (internal_id,),
            )
            events = [row[0] for row in cur.fetchall()]
        _export_json(Path(args.output), {"run": run[0], "retrieval_events": events})
        return 0

    # ------------------------------------------------------------------
    # Catalog v5 compatibility export (issue #35)
    # ------------------------------------------------------------------
    if args.command == "catalog-export":
        from .catalog_export import ExportTargetNotFound

        exporter = build_catalog_export_service(config)

        try:
            if args.catalog_command == "run":
                run_id = _resolve_any_run_id(config, args.external_id)
                result = exporter.export_run(
                    run_id,
                    args.target_dir,
                )
                print(
                    dumps(
                        {
                            "status": result.status,
                            "export_id": "ce_"
                            + sha256(
                                f"{result.source_state_sha256}:{EXPORT_SCHEMA_VERSION}".encode()
                            ).hexdigest()[:40],
                            "source_state_sha256": result.source_state_sha256,
                            "export_schema_version": result.export_schema_version,
                            "target_dir": str(result.target_dir),
                            "files_created": [str(f) for f in result.files_created],
                            "invocation_count": len(result.invocations),
                            "event_count": len(result.events),
                            "claim_count": len(result.claims),
                            "assessment_count": len(result.assessments),
                            "error": result.error,
                        }
                    )
                )
                return 0 if result.status == "complete" else 1

            elif args.catalog_command == "invocation":
                invocation_id = UUID(args.invocation_id)
                run_id = _resolve_any_run_id(config, args.run_id)
                result = exporter.export_invocation(
                    invocation_id,
                    run_id,
                    args.target_dir,
                )
                print(
                    dumps(
                        {
                            "status": result.status,
                            "export_id": result.export_id,
                            "source_state_sha256": result.source_state_sha256,
                            "export_schema_version": result.export_schema_version,
                            "target_dir": str(result.target_dir),
                            "files_created": [str(f) for f in result.files_created],
                            "error": result.error,
                        }
                    )
                )
                return 0 if result.status == "complete" else 1

            elif args.catalog_command == "events":
                run_id = _resolve_any_run_id(config, args.external_id)
                result = exporter.export_events(run_id, args.target_dir)
                print(
                    dumps(
                        {
                            "status": result.status,
                            "export_id": result.export_id,
                            "source_state_sha256": result.source_state_sha256,
                            "export_schema_version": result.export_schema_version,
                            "target_dir": str(result.target_dir),
                            "files_created": [str(f) for f in result.files_created],
                            "error": result.error,
                        }
                    )
                )
                return 0 if result.status == "complete" else 1

            elif args.catalog_command == "snapshots":
                run_id = _resolve_any_run_id(config, args.external_id)
                result = exporter.export_snapshots(run_id, args.target_dir)
                print(
                    dumps(
                        {
                            "status": result.status,
                            "export_id": result.export_id,
                            "source_state_sha256": result.source_state_sha256,
                            "export_schema_version": result.export_schema_version,
                            "target_dir": str(result.target_dir),
                            "files_created": [str(f) for f in result.files_created],
                            "error": result.error,
                        }
                    )
                )
                return 0 if result.status == "complete" else 1

            elif args.catalog_command == "claims":
                run_id = _resolve_any_run_id(config, args.external_id)
                result = exporter.export_claims(run_id, args.target_dir)
                print(
                    dumps(
                        {
                            "status": result.status,
                            "export_id": result.export_id,
                            "source_state_sha256": result.source_state_sha256,
                            "export_schema_version": result.export_schema_version,
                            "target_dir": str(result.target_dir),
                            "files_created": [str(f) for f in result.files_created],
                            "error": result.error,
                        }
                    )
                )
                return 0 if result.status == "complete" else 1

            elif args.catalog_command == "assessments":
                run_id = _resolve_any_run_id(config, args.external_id)
                result = exporter.export_assessments(run_id, args.target_dir)
                print(
                    dumps(
                        {
                            "status": result.status,
                            "export_id": result.export_id,
                            "source_state_sha256": result.source_state_sha256,
                            "export_schema_version": result.export_schema_version,
                            "target_dir": str(result.target_dir),
                            "files_created": [str(f) for f in result.files_created],
                            "error": result.error,
                        }
                    )
                )
                return 0 if result.status == "complete" else 1

            elif args.catalog_command == "manifest":
                run_id = _resolve_any_run_id(config, args.external_id)
                result = exporter.export_manifest(run_id, args.target_dir)
                print(
                    dumps(
                        {
                            "status": result.status,
                            "export_id": result.export_id,
                            "source_state_sha256": result.source_state_sha256,
                            "export_schema_version": result.export_schema_version,
                            "target_dir": str(result.target_dir),
                            "files_created": [str(f) for f in result.files_created],
                            "error": result.error,
                        }
                    )
                )
                return 0 if result.status == "complete" else 1

            elif args.catalog_command == "regenerate":
                run_id = _resolve_any_run_id(config, args.external_id)
                result = exporter.regenerate_run_export(run_id, args.target_dir)
                print(
                    dumps(
                        {
                            "status": result.status,
                            "source_state_sha256": result.source_state_sha256,
                            "export_schema_version": result.export_schema_version,
                            "target_dir": str(result.target_dir),
                            "files_created": [str(f) for f in result.files_created],
                            "error": result.error,
                        }
                    )
                )
                return 0 if result.status == "complete" else 1

        except ExportTargetNotFound as exc:
            raise SystemExit(str(exc)) from exc
        except Exception as exc:
            raise SystemExit(f"catalog export failed: {exc}") from exc

    if args.command == "run-start":
        status = build_run_service(config).create(
            args.objective,
            args.external_id,
            execution_mode=args.mode,
            idempotency_key=args.idempotency_key,
            actor_type=args.actor,
            catalog_pointer=args.catalog_pointer,
            skill_version="research-store-v3",
        )
        print(dumps(status.to_dict()))
        return 0
    if args.command in {
        "run-status",
        "run-mode-change",
        "run-transition",
        "run-finish",
        "run-reopen",
        "run-cancel",
    }:
        run_service = build_run_service(config)
        status = run_service.status(external_id=args.external_id)
        if args.command == "run-status":
            print(dumps(status.to_dict()))
            return 0
        expected_revision = (
            args.expected_revision
            if args.expected_revision is not None
            else status.lifecycle_revision
        )
    if args.command == "run-mode-change":
        result = run_service.change_execution_mode(
            status.id,
            args.mode,
            expected_revision=expected_revision,
            idempotency_key=args.idempotency_key,
            requested_by=args.requested_by,
            approved_by=args.approved_by,
            reason=args.reason,
            actor_type=args.actor,
            actor_identifier=args.actor_identifier,
        )
        print(dumps(result.to_dict()))
        return 0
    if args.command == "run-transition":
        result = run_service.transition(
            status.id,
            args.next_state,
            expected_revision=expected_revision,
            idempotency_key=args.idempotency_key,
            actor_type=args.actor,
            actor_identifier=args.actor_identifier,
            semantic_proposal_id=(
                UUID(args.semantic_proposal_id) if args.semantic_proposal_id else None
            ),
            reason=args.reason,
        )
        print(dumps(result.to_dict()))
        return 0
    if args.command == "run-finish":
        next_state = (
            "failed"
            if args.status == "failed"
            else "partial"
            if args.outcome == "partial"
            else "completed"
        )
        idempotency_key = args.idempotency_key or (
            f"run:finish:{args.status}:{args.outcome}:"
            f"{args.source_manifest_sha256 or ''}:{args.answer_sha256 or ''}"
        )
        result = run_service.transition(
            status.id,
            next_state,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            actor_type=args.actor,
            outcome=args.outcome,
            error=args.outcome if next_state == "failed" else None,
            completion={
                "catalog_pointer": args.catalog_pointer,
                "source_manifest_sha256": args.source_manifest_sha256,
                "answer_sha256": args.answer_sha256,
            },
        )
        print(dumps(result.to_dict()))
        return 0
    if args.command == "run-reopen":
        result = run_service.reopen(
            status.id,
            expected_revision=expected_revision,
            idempotency_key=args.idempotency_key
            or f"run:reopen:{args.external_id}:{args.reason}",
            actor_type=args.actor,
            reason=args.reason,
        )
        print(dumps(result.to_dict()))
        return 0
    if args.command == "run-cancel":
        result = run_service.cancel(
            status.id,
            expected_revision=expected_revision,
            idempotency_key=args.idempotency_key
            or f"run:cancel:{args.external_id}:{args.reason}",
            actor_type=args.actor,
            reason=args.reason,
        )
        print(dumps(result.to_dict()))
        return 0
    # ------------------------------------------------------------------
    # Compatibility commands routed through PostgreSQL (issue #36)
    # ------------------------------------------------------------------
    if args.command == "run-annotate":
        run_service = build_run_service(config)
        status = run_service.status(external_id=args.external_id)
        expected_revision = (
            args.expected_revision
            if args.expected_revision is not None
            else status.lifecycle_revision
        )
        result = run_service.annotate(
            status.id,
            event_type=args.type,
            reason=args.reason,
            from_invocation=args.from_invocation,
            to_invocation=args.to_invocation,
            expected_revision=expected_revision,
            idempotency_key=args.idempotency_key
            or f"run:annotate:{args.external_id}:{args.type}:{args.reason}",
            actor_type=args.actor,
        )
        print(dumps(result))
        return 0
    if args.command == "run-verify":
        run_service = build_run_service(config)
        status = run_service.status(external_id=args.external_id)
        result = run_service.verify(status.id)
        output_file = args.output
        if output_file == "-":
            print(dumps(result))
        else:
            import tempfile

            with tempfile.NamedTemporaryFile(
                "w", suffix=".json", delete=False, dir=str(Path(output_file).parent)
            ) as f:
                json.dump(result, f, indent=2, sort_keys=True)
                f.write("\n")
            print(dumps({"status": "written", "path": f.name}))
        return 0
    if args.command == "run-audit":
        run_service = build_run_service(config)
        status = run_service.status(external_id=args.external_id)

        target_hash = args.target_hash
        if not target_hash:
            from research_store.audit_packet import compute_audit_packet_hash_from_db

            target_hash = compute_audit_packet_hash_from_db(
                status.id, run_service.uow_factory
            )

        result = run_service.trigger_audit(
            status.id,
            target_hash=target_hash,
            provider=args.llm,
            model=args.model,
            force=args.force,
            stages=args.stages.split(",") if args.stages else None,
            max_calls=args.max_calls,
            max_input_tokens=args.max_input_tokens,
            fallback_provider=args.commercial_fallback,
            fallback_model=args.fallback_model,
        )
        print(dumps(result))
        return 0
    if args.command == "run-compare":
        run_service = build_run_service(config)
        results = []
        for external_id in args.external_ids:
            run_status = run_service.status(external_id=external_id)
            results.append(run_status.to_dict())
        print(dumps({"comparison": results, "count": len(results)}))
        return 0
    if args.command == "budget-record":
        from research_domain import load_model, serialize_model
        from research_domain.models import ResearchSpec

        spec_payload = json.loads(Path(args.research_spec).read_text(encoding="utf-8"))
        spec = load_model(spec_payload)
        if not isinstance(spec, ResearchSpec):
            raise SystemExit("--research-spec must contain research-spec-v1")
        snapshot = json.loads(Path(args.budget_snapshot).read_text(encoding="utf-8"))
        required = {
            "snapshot_version",
            "policy_version",
            "policy_config_sha256",
            "research_spec_id",
            "spec_revision",
            "run_revision",
            "effective_caps",
        }
        missing = sorted(required - set(snapshot))
        if missing:
            raise SystemExit(f"budget snapshot missing required fields: {missing}")
        if snapshot["research_spec_id"] != str(spec.research_spec_id):
            raise SystemExit("budget snapshot references another ResearchSpec")
        run_id = _resolve_run_id(config, args.external_id)
        with _uow_factory(config)() as uow:
            spec_id = uow.record_research_spec(
                run_id,
                snapshot["spec_revision"],
                "research-spec",
                1,
                serialize_model(spec),
                f"research-spec:{spec.research_spec_id}:r{snapshot['spec_revision']}",
            )
            budget_id = uow.record_budget_snapshot(
                run_id,
                spec_id,
                snapshot["spec_revision"],
                snapshot["run_revision"],
                snapshot["policy_version"],
                snapshot["policy_config_sha256"],
                snapshot,
                "budget:"
                f"{snapshot['policy_version']}:r{snapshot['run_revision']}:"
                f"{spec.research_spec_id}",
            )
        print(dumps({"id": budget_id, "external_run_id": args.external_id}))
        return 0
    if args.command == "legacy-comparisons":
        with _uow_factory(config)() as uow:
            rows = uow.runs.list_legacy_adapter_comparisons(
                external_run_id=args.research_run_id,
                external_invocation_id=args.invocation_id,
                entry_point=args.entry_point,
                divergent_only=args.divergent_only,
                limit=args.limit,
            )
        print(dumps({"comparisons": rows, "count": len(rows)}))
        return 0
    if args.command == "search-plan-record":
        run_svc = build_run_service(config)
        status = run_svc.status(external_id=args.external_id)
        with open(args.search_plan, "r", encoding="utf-8") as f:
            plan_payload = json.load(f)
        plan_id = run_svc.record_search_plan(
            status.id,
            UUID(args.research_spec_id),
            args.revision,
            plan_payload,
            args.idempotency_key,
        )
        print(dumps({"id": plan_id, "external_run_id": args.external_id}))
        return 0
    if args.command == "search-plan-get":
        run_svc = build_run_service(config)
        status = run_svc.status(external_id=args.external_id)
        plan_id = UUID(args.plan_id) if args.plan_id else None
        plan = run_svc.get_search_plan(
            status.id, plan_id=plan_id, revision=args.revision
        )
        print(dumps(plan))
        return 0
    if args.command == "search-plan-query-get":
        run_svc = build_run_service(config)
        query = run_svc.get_plan_query(UUID(args.query_id))
        print(dumps(query))
        return 0
    if args.command == "search-response-record":
        run_svc = build_run_service(config)
        status = run_svc.status(external_id=args.external_id)
        if args.payload_file:
            with open(args.payload_file, "rb") as f:
                raw_payload = f.read()
        else:
            raw_payload = sys.stdin.buffer.read()
        resp = run_svc.record_search_response(
            status.id,
            args.query_text,
            args.backend,
            raw_payload,
            args.idempotency_key,
            plan_id=UUID(args.plan_id) if args.plan_id else None,
            plan_query_id=UUID(args.plan_query_id) if args.plan_query_id else None,
            provider_request_id=args.provider_request_id,
            parser_version=args.parser_version,
            http_status=args.http_status,
        )
        print(dumps(resp))
        return 0
    if args.command == "search-response-get":
        run_svc = build_run_service(config)
        resp = run_svc.get_search_response(UUID(args.response_id))
        print(dumps(resp))
        return 0
    if args.command == "search-response-replay":
        run_svc = build_run_service(config)
        replay = run_svc.replay_search_response(UUID(args.response_id))
        out = {
            "id": str(replay.id),
            "run_id": str(replay.run_id),
            "query_text": replay.query_text,
            "backend": replay.backend,
            "status": replay.status,
            "parser_version": replay.parser_version,
            "raw_blob_sha256": replay.raw_blob_sha256,
            "content_sha256": replay.content_sha256,
            "raw_bytes_len": len(replay.raw_bytes),
            "integrity_verified": replay.verify_integrity(),
            "result_count": replay.result_count,
            "parsed_json": replay.parsed_json,
        }
        print(dumps(out))
        return 0
    if args.command == "candidate-record-response":
        run_svc = build_run_service(config)
        status = run_svc.status(external_id=args.external_id)
        occs = run_svc.record_response_candidates(
            status.id, UUID(args.search_response_id)
        )
        print(dumps(occs))
        return 0
    if args.command == "candidate-get":
        run_svc = build_run_service(config)
        cand = run_svc.get_candidate(UUID(args.candidate_id))
        print(dumps(cand))
        return 0
    if args.command == "candidate-list":
        run_svc = build_run_service(config)
        status = run_svc.status(external_id=args.external_id)
        cands = run_svc.list_candidates(
            status.id,
            domain=args.domain,
            min_recurrence=args.min_recurrence,
            duplicate_group_id=UUID(args.duplicate_group_id)
            if args.duplicate_group_id
            else None,
        )
        print(dumps(cands))
        return 0
    if args.command == "candidate-occurrences-list":
        run_svc = build_run_service(config)
        occs = run_svc.list_candidate_occurrences(UUID(args.candidate_id))
        print(dumps(occs))
        return 0
    if args.command == "candidate-assign-group":
        run_svc = build_run_service(config)
        group_id = UUID(args.group_id) if args.group_id else None
        res_group_id = run_svc.assign_duplicate_group(
            [UUID(cid) for cid in args.candidate_ids], group_id=group_id
        )
        print(dumps({"duplicate_group_id": res_group_id}))
        return 0
    if args.command == "acquisition-search":
        from .container import build_acquisition_service

        run_svc = build_run_service(config)
        status = run_svc.status(external_id=args.external_id)
        acq_svc = build_acquisition_service(config)
        result = acq_svc.execute_search(
            status.id,
            args.query_text,
            backend=args.backend,
            plan_id=UUID(args.plan_id) if args.plan_id else None,
            plan_query_id=UUID(args.plan_query_id) if args.plan_query_id else None,
            idempotency_key=args.idempotency_key,
            limit=args.limit,
            sources=args.sources,
            tbs=args.tbs,
            scratch_dir=args.scratch_dir,
            export_scratch=bool(args.scratch_dir),
        )
        print(
            dumps(
                {
                    "search_response_id": str(result.search_response_id),
                    "run_id": str(result.run_id),
                    "query_text": result.query_text,
                    "backend": result.backend,
                    "status": result.status,
                    "candidate_count": result.candidate_count,
                    "postgres_committed": result.postgres_committed,
                    "scratch_exported": result.scratch_exported,
                    "event_id": str(result.event_id) if result.event_id else None,
                    "scratch_error": result.scratch_error,
                }
            )
        )
        return 0
    if args.command == "acquisition-reconcile":
        from .container import build_acquisition_service

        run_svc = build_run_service(config)
        status = run_svc.status(external_id=args.external_id)
        acq_svc = build_acquisition_service(config)
        reconciled = acq_svc.reconcile_pending_searches(status.id)
        print(dumps(reconciled))
        return 0
    if args.command == "candidate-list-paginated":
        run_svc = build_run_service(config)
        status = run_svc.status(external_id=args.external_id)
        paginated = run_svc.list_candidates_paginated(
            status.id,
            plan_id=UUID(args.plan_id) if args.plan_id else None,
            plan_query_id=UUID(args.plan_query_id) if args.plan_query_id else None,
            query_text=args.query_text,
            domain=args.domain,
            min_recurrence=args.min_recurrence,
            duplicate_group_id=UUID(args.duplicate_group_id)
            if args.duplicate_group_id
            else None,
            limit=args.limit,
            offset=args.offset,
        )
        print(dumps(paginated))
        return 0
    if args.command == "candidate-card":
        run_svc = build_run_service(config)
        card = run_svc.get_candidate_card(
            UUID(args.candidate_id),
            max_snippet_length=args.max_snippet_length,
        )
        print(dumps(card))
        return 0
    if args.command == "candidate-triage-input":
        run_svc = build_run_service(config)
        status = run_svc.status(external_id=args.external_id)
        triage = run_svc.build_triage_input(
            status.id,
            plan_id=UUID(args.plan_id) if args.plan_id else None,
            plan_query_id=UUID(args.plan_query_id) if args.plan_query_id else None,
            query_text=args.query_text,
            domain=args.domain,
            min_recurrence=args.min_recurrence,
            duplicate_group_id=UUID(args.duplicate_group_id)
            if args.duplicate_group_id
            else None,
            limit=args.limit,
            offset=args.offset,
            max_snippet_length=args.max_snippet_length,
        )
        print(dumps(triage))
        return 0
    if args.command == "candidate-replay":
        run_svc = build_run_service(config)
        status = run_svc.status(external_id=args.external_id)
        replayed = run_svc.replay_candidates(
            status.id,
            plan_id=UUID(args.plan_id) if args.plan_id else None,
            plan_query_id=UUID(args.plan_query_id) if args.plan_query_id else None,
            domain=args.domain,
            min_recurrence=args.min_recurrence,
            limit=args.limit,
            offset=args.offset,
        )
        print(dumps(replayed))
        return 0
    if args.command == "export-search-compat":
        from .container import build_compatibility_export_service

        run_svc = build_run_service(config)
        status = run_svc.status(external_id=args.external_id)
        exporter = build_compatibility_export_service(config)
        res = exporter.export_search(
            status.id,
            UUID(args.search_response_id),
            args.target_dir,
            idempotency_key=args.idempotency_key,
        )
        print(
            dumps(
                {
                    "export_id": str(res.export_id) if res.export_id else None,
                    "run_id": str(res.run_id),
                    "search_response_id": str(res.search_response_id),
                    "target_dir": str(res.target_dir),
                    "source_state_sha256": res.source_state_sha256,
                    "status": res.status,
                    "files_created": [str(f) for f in res.files_created],
                    "error": res.error,
                }
            )
        )
        return 0
    if args.command == "regenerate-search-exports":
        from .container import build_compatibility_export_service

        run_svc = build_run_service(config)
        status = run_svc.status(external_id=args.external_id)
        exporter = build_compatibility_export_service(config)
        results = exporter.regenerate_search_exports(status.id, args.target_dir)
        print(
            dumps(
                [
                    {
                        "export_id": str(res.export_id) if res.export_id else None,
                        "run_id": str(res.run_id),
                        "search_response_id": str(res.search_response_id),
                        "target_dir": str(res.target_dir),
                        "source_state_sha256": res.source_state_sha256,
                        "status": res.status,
                        "files_created": [str(f) for f in res.files_created],
                        "error": res.error,
                    }
                    for res in results
                ]
            )
        )
        return 0

    service = build_service(config)
    if args.command == "corpus-overview":
        result = service.corpus_overview()
    elif args.command == "search-assets":
        filters = {
            key: value
            for key, value in {
                "domain": args.domain,
                "source_type": args.source_type,
                "date_from": args.date_from,
                "date_to": args.date_to,
            }.items()
            if value
        }
        result = service.search_assets(
            args.query,
            filters=filters,
            candidate_limit=args.limit,
            run_id=_resolve_run_id(config, args.research_run_id),
        )
    elif args.command == "inspect-asset":
        result = service.inspect_asset(UUID(args.id))
    elif args.command == "fetch-passages":
        ids = [UUID(value) for value in args.ids]
        result = service.fetch_passages(
            ids, max_tokens=args.max_tokens, max_passages=args.max_passages
        )
        run_id = _resolve_run_id(config, args.research_run_id)
        if run_id:
            with _uow_factory(config)() as uow:
                for rank, passage in enumerate(result, 1):
                    uow.log_retrieval(
                        run_id,
                        {
                            "stage": "passage_fetch",
                            "retriever": "explicit_selection",
                            "candidate_type": "chunk",
                            "candidate_id": passage["chunk_id"],
                            "rank": rank,
                            "selected": True,
                        },
                    )
    elif args.command == "expand-relationships":
        result = service.expand_relationships(
            [UUID(value) for value in args.ids],
            max_hops=args.max_hops,
            max_results=args.max_results,
            max_tokens=args.max_tokens,
        )
    elif args.command == "build-evidence-packet":
        result = service.build_evidence_packet(
            [UUID(value) for value in args.ids], max_tokens=args.max_tokens
        )

    # ------------------------------------------------------------------
    # Claim manifest commands (issue #32)
    # ------------------------------------------------------------------
    elif args.command == "claim-manifest":
        from .container import build_claim_service

        claim_svc = build_claim_service(config)
        if args.claim_command == "import":
            # import requires a running run (write operation)
            run_id = _resolve_run_id(config, args.external_id)
            import json as _json

            manifest_file = args.file
            manifest_path = Path(manifest_file)
            if not manifest_path.is_file():
                raise SystemExit(f"manifest file not found: {manifest_file}")
            with open(manifest_path, "r") as f:
                manifest = _json.load(f)
            result = claim_svc.import_manifest(
                run_id, manifest, dry_run=getattr(args, "dry_run", False)
            )
        elif args.claim_command == "export":
            # export is read-only; works on any run status
            run_id = _resolve_any_run_id(config, args.external_id)
            manifest = claim_svc.export_manifest(run_id)
            output = args.output
            if output == "-":
                print(dumps(manifest))
                return 0
            else:
                output_path = Path(output)
                output_path.parent.mkdir(parents=True, exist_ok=True)
                fd, tmp_path = tempfile.mkstemp(
                    dir=str(output_path.parent), suffix=".tmp"
                )
                try:
                    with os.fdopen(fd, "w") as f:
                        f.write(dumps(manifest))
                    os.replace(tmp_path, str(output_path))
                except BaseException:
                    os.unlink(tmp_path)
                    raise
                result = {
                    "exported_to": output,
                    "claim_count": manifest.get("claim_count", 0),
                    "link_count": manifest.get("link_count", 0),
                }
        elif args.claim_command == "list":
            # list is read-only; works on any run status
            run_id = _resolve_any_run_id(config, args.external_id)
            claims = claim_svc.list_claims(run_id)
            links = claim_svc.list_evidence_links(run_id)
            result = {
                "claims": claims,
                "links": links,
                "claim_count": len(claims),
                "link_count": len(links),
            }
        else:
            raise SystemExit(f"unknown claim-manifest command: {args.claim_command}")

    # ------------------------------------------------------------------
    # Audit commands (issue #33)
    # ------------------------------------------------------------------
    elif args.command == "audit":
        config.require_database()
        audit_svc = build_audit_service(config)
        run_id = _resolve_run_id(config, args.external_id)
        if run_id is None:
            raise SystemExit(
                f"research run not found or not running: {args.external_id}"
            )

        stage_set = [s.strip() for s in args.stages.split(",") if s.strip()]
        manifest = None
        if args.packet_manifest_file:
            with open(args.packet_manifest_file, "r") as f:
                manifest = json.load(f)

        assessment = audit_svc.assess_run(
            run_id=run_id,
            external_run_id=args.external_id,
            target_hash=args.target_hash,
            evaluator_version=args.evaluator_version,
            prompt_template_version=args.prompt_template_version,
            policy_version=args.policy_version,
            stage_set=stage_set,
            status=args.status,
            provider=args.provider,
            model=args.model,
            prompt_hash=args.prompt_hash,
            model_fingerprint=args.model_fingerprint,
            elapsed_ms=args.elapsed_ms,
            audit_packet_manifest=manifest,
        )
        print(dumps(assessment))

    elif args.command == "audit-status":
        config.require_database()
        audit_svc = build_audit_service(config)
        run_id = _resolve_any_run_id(config, args.external_id)
        if run_id is None:
            raise SystemExit(f"research run not found: {args.external_id}")

        # Return only the latest assessment
        assessments = audit_svc.list_assessments(
            run_id=run_id,
            limit=1,
            offset=0,
        )
        result = assessments[0] if assessments else None
        if result is None:
            raise SystemExit(f"no assessments found for run: {args.external_id}")
        print(dumps(result))

    elif args.command == "audit-query":
        config.require_database()
        audit_svc = build_audit_service(config)
        run_id = _resolve_any_run_id(config, args.external_id)
        if run_id is None:
            raise SystemExit(f"research run not found: {args.external_id}")

        assessments = audit_svc.list_assessments(
            run_id=run_id,
            status=args.status_filter,
            limit=args.limit,
            offset=args.offset,
        )
        result = {"run_id": str(run_id), "assessments": assessments}
        print(dumps(result))

    elif args.command == "audit-export":
        config.require_database()
        audit_svc = build_audit_service(config)
        export = audit_svc.export_assessment(UUID(args.assessment_id))
        if export is None:
            raise SystemExit(f"assessment not found: {args.assessment_id}")
        if args.output == "-":
            print(dumps(export))
        else:
            output_path = Path(args.output)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(dir=str(output_path.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(export, f, indent=2, default=json_default)
                os.replace(tmp_path, str(output_path))
            except BaseException:
                os.unlink(tmp_path)
                raise

    elif args.command == "audit-staleness":
        config.require_database()
        audit_svc = build_audit_service(config)
        run_id = _resolve_run_id(config, args.external_id)
        if run_id is None:
            raise SystemExit(
                f"research run not found or not running: {args.external_id}"
            )

        stale = audit_svc.detect_stale_assessments(
            run_id=run_id,
            target_type="run",
            target_id=run_id,
            current_hash=args.target_hash,
        )
        result = {"run_id": str(run_id), "stale_assessments": stale}
        print(dumps(result))

    # ------------------------------------------------------------------
    # Catalog import commands (issue #37)
    # ------------------------------------------------------------------
    elif args.command == "catalog-import":
        config.require_database()
        from .container import build_catalog_import_service

        catalog_root = Path(args.catalog_root)
        if not catalog_root.is_dir():
            raise SystemExit(f"Catalog root not found: {args.catalog_root}")

        import_svc = build_catalog_import_service(config)

        if args.apply:
            report_path = Path(args.report) if args.report else None
            report = import_svc.apply(
                catalog_root,
                report_file=report_path,
            )
        else:
            # Dry-run is the default
            report = import_svc.dry_run(catalog_root)

        report_dict = report.to_dict()
        if args.report:
            report_path = Path(args.report)
            report_path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(dir=str(report_path.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(report_dict, f, indent=2, default=str)
                os.replace(tmp_path, str(report_path))
                print(f"Report written to {report_path}")
            except BaseException:
                os.unlink(tmp_path)
                raise
        else:
            print(dumps(report_dict))

        # Exit code: 0 = success, 1 = conflicts/omissions, 2 = errors
        # Check conflicts/omissions (including pending records) before
        # general errors so that a warning about pending records does not
        # escalate to exit code 2.
        if report.records_conflicting > 0 or report.records_omitted > 0:
            raise SystemExit(1)
        elif report.errors:
            raise SystemExit(2)
        elif report.records_malformed > 0 and report.records_inserted == 0:
            raise SystemExit(2)
        return 0

    elif args.command == "catalog-reconcile":
        config.require_database()
        from .container import build_catalog_import_service

        import_svc = build_catalog_import_service(config)
        report = import_svc.reconcile()
        report_dict = {
            "total_imports": report.total_imports,
            "imports": report.imports,
            "conflict_summary": report.conflict_summary,
            "omission_summary": report.omission_summary,
        }
        if args.report:
            report_path = Path(args.report)
            report_path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(dir=str(report_path.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(report_dict, f, indent=2, default=str)
                os.replace(tmp_path, str(report_path))
                print(f"Report written to {report_path}")
            except BaseException:
                os.unlink(tmp_path)
                raise
        else:
            print(dumps(report_dict))
        return 0

    else:
        raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
