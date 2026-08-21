"""Issue #217 authoritative ingestion-batch contract.

The repository already uses explicit package-level production extension points
for release-candidate corrections (see ``research_store.__init__``). This
module installs the RC-11/RC-12/RC-13 contract on those canonical classes while
keeping PostgreSQL as the only lifecycle, provenance, timing, and exact
membership authority.

New v43 batches are strict:

* every batch member has an exact PostgreSQL constituent timing source;
* extraction-backed members use the exact linked extraction attempt;
* direct-ingestion members persist their own start/completion timestamps;
* finalization fails closed when any constituent lacks authoritative terminal
  evidence;
* ``started_at``/``completed_at`` are exact MIN/MAX constituent timestamps;
* sealing and membership mutation serialize on the same batch row lock; and
* the persisted outcome summary contains deterministic counts and stable member
  IDs, including cancellation and failure-class membership.

Pre-v43 schemas retain their legacy timing semantics without ever referencing
v43-only columns, which keeps rolling code deployment compatible with revision
0042. Historical promotion rows remain unknown unless PostgreSQL contains an
explicit promotion event.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import replace
from typing import Any
from uuid import UUID, uuid4

_TERMINAL_EXTRACTION_STATES = frozenset({"succeeded", "partial", "failed", "cancelled"})

_ORIGINAL_PROMOTION_LIST_ASSETS = None


def _has_constituent_timing_columns(connection) -> bool:
    """Return whether the complete v43 per-member timing contract is present."""
    with connection.cursor() as cur:
        cur.execute(
            """SELECT count(*)
                 FROM information_schema.columns
                WHERE table_name='ingestion_batch_assets'
                  AND column_name IN (
                    'constituent_started_at','constituent_completed_at'
                  )"""
        )
        return int(cur.fetchone()[0]) == 2


def _start_ingestion_batch(
    self, invocation_id, operation, research_run_external_id=None, metadata=None
):
    """Start/reopen a batch without referencing columns absent on v42."""
    has_v43 = self._has_sealed_at_column(self.connection)
    with self.connection.cursor() as cur:
        cur.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            (invocation_id,),
        )
        research_run_id = None
        if research_run_external_id is not None:
            cur.execute(
                """SELECT id,state FROM research_runs
                   WHERE external_run_id=%s FOR SHARE""",
                (research_run_external_id,),
            )
            run = cur.fetchone()
            if run is None:
                raise KeyError(research_run_external_id)
            if run[1] in {"completed", "partial", "failed", "cancelled"}:
                raise ValueError("ingestion batches require a nonterminal research run")
            research_run_id = run[0]

        cur.execute(
            """SELECT b.id,b.operation,r.external_run_id,b.status
                 FROM ingestion_batches b
                 LEFT JOIN research_runs r ON r.id=b.research_run_id
                WHERE b.invocation_id=%s FOR UPDATE OF b""",
            (invocation_id,),
        )
        existing = cur.fetchone()
        if existing:
            if existing[1] != operation or existing[2] != research_run_external_id:
                raise ValueError(
                    "invocation ID reuse requires the original operation and research run"
                )
            if existing[3] == "running":
                raise ValueError(
                    "invocation ID is already running; retry only after it is terminal"
                )
            batch_id = existing[0]
            if has_v43:
                cur.execute(
                    """UPDATE ingestion_batches
                          SET status='running',completed_at=NULL,error=NULL,
                              sealed_at=NULL,outcome_summary='{}'::jsonb,
                              metadata=metadata || %s
                        WHERE id=%s""",
                    (json.dumps(metadata or {}), batch_id),
                )
            else:
                cur.execute(
                    """UPDATE ingestion_batches
                          SET status='running',completed_at=NULL,error=NULL,
                              metadata=metadata || %s
                        WHERE id=%s""",
                    (json.dumps(metadata or {}), batch_id),
                )
        else:
            cur.execute(
                """INSERT INTO ingestion_batches(
                       invocation_id,operation,research_run_id,metadata)
                   VALUES(%s,%s,%s,%s)
                   RETURNING id""",
                (
                    invocation_id,
                    operation,
                    research_run_id,
                    json.dumps(metadata or {}),
                ),
            )
            batch_id = cur.fetchone()[0]

        # Invocation retries replace the reconstructable member ledger in the
        # same transaction. A rollback therefore restores the prior terminal
        # ledger and summary.
        cur.execute("DELETE FROM ingestion_batch_assets WHERE batch_id=%s", (batch_id,))
        return batch_id


def _record_batch_asset(
    self,
    batch_id,
    ordinal,
    requested_url,
    status,
    result=None,
    error=None,
    metadata=None,
    extraction_attempt_id=None,
    constituent_started_at=None,
    constituent_completed_at=None,
):
    """Persist one exact batch member while holding the batch membership lock."""
    has_v43 = self._has_sealed_at_column(self.connection)
    if has_v43:
        with self.connection.cursor() as cur:
            cur.execute(
                "SELECT sealed_at FROM ingestion_batches WHERE id=%s FOR UPDATE",
                (batch_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise KeyError(batch_id)
            if row[0] is not None:
                raise ValueError(
                    f"batch {batch_id} is sealed; no new assets may be recorded"
                )

    with self.connection.cursor() as cur:
        has_attempt = self._has_extraction_attempt_id_column(self.connection)
        has_timing = _has_constituent_timing_columns(self.connection)
        if has_v43 and not has_timing:
            raise RuntimeError(
                "ingestion batch schema is incomplete: revision 0043 requires "
                "constituent timing columns"
            )
        if has_attempt and has_timing:
            # A raw repository-level direct member has no separate extraction
            # attempt. In that narrow case, recording the authoritative member
            # is itself the observed constituent event, so a zero-duration
            # interval at the recording instant is truthful. CorpusService
            # supplies the richer prepare/persist interval for ordinary direct
            # ingestion; extraction-backed members always derive timing from
            # their exact extraction attempt instead.
            if extraction_attempt_id is None and (
                constituent_started_at is None or constituent_completed_at is None
            ):
                from .domain import utcnow

                observed_at = utcnow()
                constituent_started_at = constituent_started_at or observed_at
                constituent_completed_at = constituent_completed_at or observed_at
            cur.execute(
                """INSERT INTO ingestion_batch_assets(
                       batch_id,ordinal,requested_url,status,source_id,snapshot_id,
                       document_id,chunk_ids,error,metadata,extraction_attempt_id,
                       constituent_started_at,constituent_completed_at)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(batch_id,ordinal) DO UPDATE SET
                     requested_url=excluded.requested_url,
                     status=excluded.status,
                     source_id=excluded.source_id,
                     snapshot_id=excluded.snapshot_id,
                     document_id=excluded.document_id,
                     chunk_ids=excluded.chunk_ids,
                     error=excluded.error,
                     metadata=excluded.metadata,
                     extraction_attempt_id=COALESCE(
                       excluded.extraction_attempt_id,
                       ingestion_batch_assets.extraction_attempt_id
                     ),
                     constituent_started_at=COALESCE(
                       excluded.constituent_started_at,
                       ingestion_batch_assets.constituent_started_at
                     ),
                     constituent_completed_at=COALESCE(
                       excluded.constituent_completed_at,
                       ingestion_batch_assets.constituent_completed_at
                     )""",
                (
                    batch_id,
                    ordinal,
                    requested_url,
                    status,
                    result.source_id if result else None,
                    result.snapshot_id if result else None,
                    result.document_id if result else None,
                    list(result.chunk_ids) if result else [],
                    error,
                    json.dumps(metadata or {}),
                    str(extraction_attempt_id) if extraction_attempt_id else None,
                    constituent_started_at,
                    constituent_completed_at,
                ),
            )
        else:
            # Exact v42 compatibility path: do not mention any v43-only column.
            cur.execute(
                """INSERT INTO ingestion_batch_assets(
                       batch_id,ordinal,requested_url,status,source_id,snapshot_id,
                       document_id,chunk_ids,error,metadata)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(batch_id,ordinal) DO UPDATE SET
                     requested_url=excluded.requested_url,status=excluded.status,
                     source_id=excluded.source_id,snapshot_id=excluded.snapshot_id,
                     document_id=excluded.document_id,chunk_ids=excluded.chunk_ids,
                     error=excluded.error,metadata=excluded.metadata""",
                (
                    batch_id,
                    ordinal,
                    requested_url,
                    status,
                    result.source_id if result else None,
                    result.snapshot_id if result else None,
                    result.document_id if result else None,
                    list(result.chunk_ids) if result else [],
                    error,
                    json.dumps(metadata or {}),
                ),
            )


def _finish_ingestion_batch(self, batch_id, status, error=None):
    """Seal a batch from exact persisted constituent evidence.

    On v43 the method has no wall-clock completion fallback. Every member must
    expose a start and terminal timestamp before the batch can seal. Extraction
    members derive those timestamps and outcome classification from their exact
    ``extraction_attempt_id``; direct members use their persisted per-member
    timing columns.
    """
    from .domain import utcnow

    has_v43 = self._has_sealed_at_column(self.connection)
    if not has_v43:
        # Rolling deployment compatibility. Revision 0042 did not have the
        # constituent contract, so retain its documented statement-time finish
        # behavior without mentioning v43-only columns.
        with self.connection.cursor() as cur:
            cur.execute(
                """UPDATE ingestion_batches
                      SET status=%s,error=%s,completed_at=now()
                    WHERE id=%s""",
                (status, error, batch_id),
            )
            if cur.rowcount != 1:
                raise KeyError(batch_id)
        return

    if not _has_constituent_timing_columns(self.connection):
        raise RuntimeError(
            "ingestion batch schema is incomplete: revision 0043 requires "
            "constituent timing columns"
        )

    with self.connection.cursor() as cur:
        # The same row lock is taken by record_batch_asset(), so sealing and
        # membership mutation have one deterministic serial order.
        cur.execute(
            "SELECT 1 FROM ingestion_batches WHERE id=%s FOR UPDATE",
            (batch_id,),
        )
        if cur.fetchone() is None:
            raise KeyError(batch_id)

        cur.execute(
            """SELECT iba.id,iba.ordinal,iba.status,iba.extraction_attempt_id,
                      CASE WHEN iba.extraction_attempt_id IS NOT NULL
                           THEN ea.start_time
                           ELSE iba.constituent_started_at END AS member_started_at,
                      CASE WHEN iba.extraction_attempt_id IS NOT NULL
                           THEN ea.end_time
                           ELSE iba.constituent_completed_at END AS member_completed_at,
                      ea.exit_status,ea.failure_class
                 FROM ingestion_batch_assets iba
                 LEFT JOIN extraction_attempts ea
                   ON ea.id=iba.extraction_attempt_id
                WHERE iba.batch_id=%s
                ORDER BY iba.ordinal,iba.id""",
            (batch_id,),
        )
        members = cur.fetchall()
        if not members:
            raise ValueError(
                f"batch {batch_id} cannot be finalized without constituent members"
            )

        starts = []
        completions = []
        succeeded_ids: list[str] = []
        failed_ids: list[str] = []
        cancelled_ids: list[str] = []
        succeeded_attempt_ids: list[str] = []
        failed_attempt_ids: list[str] = []
        cancelled_attempt_ids: list[str] = []
        failure_ids: dict[str, list[str]] = defaultdict(list)
        member_records: list[dict[str, Any]] = []

        for (
            member_id,
            ordinal,
            asset_status,
            attempt_id,
            member_started_at,
            member_completed_at,
            exit_status,
            failure_class,
        ) in members:
            member_key = str(member_id)
            attempt_key = str(attempt_id) if attempt_id is not None else None

            if member_started_at is None:
                raise ValueError(
                    f"batch {batch_id} member {member_key} lacks authoritative start time"
                )
            if member_completed_at is None:
                raise ValueError(
                    f"batch {batch_id} member {member_key} lacks authoritative terminal time"
                )
            if member_completed_at < member_started_at:
                raise ValueError(
                    f"batch {batch_id} member {member_key} completes before it starts"
                )

            starts.append(member_started_at)
            completions.append(member_completed_at)

            if attempt_id is not None:
                if exit_status not in _TERMINAL_EXTRACTION_STATES:
                    raise ValueError(
                        f"batch {batch_id} member {member_key} extraction attempt is nonterminal"
                    )
                if exit_status == "succeeded":
                    outcome = "succeeded"
                    if asset_status != "complete":
                        raise ValueError(
                            f"batch {batch_id} member {member_key} has successful "
                            "attempt but failed ingestion status"
                        )
                elif exit_status == "cancelled":
                    outcome = "cancelled"
                    if asset_status != "failed":
                        raise ValueError(
                            f"batch {batch_id} cancelled member {member_key} must "
                            "have failed ingestion status"
                        )
                else:
                    outcome = "failed"
                    if asset_status != "failed":
                        raise ValueError(
                            f"batch {batch_id} failed member {member_key} must "
                            "have failed ingestion status"
                        )
            else:
                outcome = "succeeded" if asset_status == "complete" else "failed"

            normalized_failure_class = None
            if outcome in {"failed", "cancelled"}:
                if attempt_id is not None and failure_class not in (None, "", "none"):
                    normalized_failure_class = str(failure_class)
                elif attempt_id is not None:
                    normalized_failure_class = "unclassified"
                else:
                    normalized_failure_class = "ingestion_error"
                failure_ids[normalized_failure_class].append(member_key)

            if outcome == "succeeded":
                succeeded_ids.append(member_key)
                if attempt_key:
                    succeeded_attempt_ids.append(attempt_key)
            elif outcome == "cancelled":
                cancelled_ids.append(member_key)
                if attempt_key:
                    cancelled_attempt_ids.append(attempt_key)
            else:
                failed_ids.append(member_key)
                if attempt_key:
                    failed_attempt_ids.append(attempt_key)

            member_records.append(
                {
                    "id": member_key,
                    "ordinal": int(ordinal),
                    "extraction_attempt_id": attempt_key,
                    "outcome": outcome,
                    "failure_class": normalized_failure_class,
                }
            )

        derived_status = (
            "complete"
            if len(succeeded_ids) == len(members)
            else "failed"
            if not succeeded_ids
            else "partial"
        )
        if status != derived_status:
            raise ValueError(
                f"batch {batch_id} status {status!r} disagrees with exact "
                f"constituent outcome {derived_status!r}"
            )

        started_at = min(starts)
        completed_at = max(completions)
        failure_classes = {
            classification: {
                "count": len(ids),
                "ids": ids,
            }
            for classification, ids in sorted(failure_ids.items())
        }
        outcome_summary = {
            "schema_version": "ingestion-outcome-summary-v2",
            "id_type": "ingestion_batch_asset_id",
            "member_count": len(members),
            "succeeded": len(succeeded_ids),
            "succeeded_ids": succeeded_ids,
            "succeeded_extraction_attempt_ids": succeeded_attempt_ids,
            "failed": len(failed_ids),
            "failed_ids": failed_ids,
            "failed_extraction_attempt_ids": failed_attempt_ids,
            "cancelled": len(cancelled_ids),
            "cancelled_ids": cancelled_ids,
            "cancelled_extraction_attempt_ids": cancelled_attempt_ids,
            "failure_classes": failure_classes,
            "members": member_records,
        }
        sealed_at = utcnow()
        cur.execute(
            """UPDATE ingestion_batches
                  SET status=%s,error=%s,started_at=%s,completed_at=%s,
                      sealed_at=%s,outcome_summary=%s::jsonb
                WHERE id=%s""",
            (
                derived_status,
                error,
                started_at,
                completed_at,
                sealed_at,
                json.dumps(outcome_summary, sort_keys=True),
                batch_id,
            ),
        )


def _asset_export_rows(cur, batch_id, *, v43: bool) -> list[dict[str, Any]]:
    if v43:
        cur.execute(
            """SELECT a.id,a.ordinal,a.requested_url,a.status,a.source_id,
                      a.snapshot_id,a.document_id,a.chunk_ids,a.error,a.metadata,
                      d.document_sha256,a.extraction_attempt_id,
                      a.constituent_started_at,a.constituent_completed_at
                 FROM ingestion_batch_assets a
                 LEFT JOIN documents d ON d.id=a.document_id
                WHERE a.batch_id=%s ORDER BY a.ordinal,a.id""",
            (batch_id,),
        )
        keys = (
            "batch_asset_id",
            "ordinal",
            "requested_url",
            "status",
            "source_id",
            "snapshot_id",
            "document_id",
            "chunk_ids",
            "error",
            "metadata",
            "content_sha256",
            "extraction_attempt_id",
            "constituent_started_at",
            "constituent_completed_at",
        )
    else:
        cur.execute(
            """SELECT a.id,a.ordinal,a.requested_url,a.status,a.source_id,
                      a.snapshot_id,a.document_id,a.chunk_ids,a.error,a.metadata,
                      d.document_sha256
                 FROM ingestion_batch_assets a
                 LEFT JOIN documents d ON d.id=a.document_id
                WHERE a.batch_id=%s ORDER BY a.ordinal,a.id""",
            (batch_id,),
        )
        keys = (
            "batch_asset_id",
            "ordinal",
            "requested_url",
            "status",
            "source_id",
            "snapshot_id",
            "document_id",
            "chunk_ids",
            "error",
            "metadata",
            "content_sha256",
        )
    return [dict(zip(keys, row, strict=True)) for row in cur.fetchall()]


def _export_invocation(self, invocation_id):
    """Export one invocation with exact v42/v43 positional schemas."""
    v43 = self._has_sealed_at_column(self.connection)
    with self.connection.cursor() as cur:
        if v43:
            cur.execute(
                """SELECT b.id,b.invocation_id,b.operation,b.status,b.started_at,
                          b.completed_at,b.error,b.metadata,b.sealed_at,
                          b.outcome_summary,r.external_run_id
                     FROM ingestion_batches b
                     LEFT JOIN research_runs r ON r.id=b.research_run_id
                    WHERE b.invocation_id=%s""",
                (invocation_id,),
            )
            keys = (
                "batch_id",
                "invocation_id",
                "operation",
                "status",
                "started_at",
                "completed_at",
                "error",
                "metadata",
                "sealed_at",
                "outcome_summary",
                "research_run_id",
            )
        else:
            cur.execute(
                """SELECT b.id,b.invocation_id,b.operation,b.status,b.started_at,
                          b.completed_at,b.error,b.metadata,r.external_run_id
                     FROM ingestion_batches b
                     LEFT JOIN research_runs r ON r.id=b.research_run_id
                    WHERE b.invocation_id=%s""",
                (invocation_id,),
            )
            keys = (
                "batch_id",
                "invocation_id",
                "operation",
                "status",
                "started_at",
                "completed_at",
                "error",
                "metadata",
                "research_run_id",
            )
        row = cur.fetchone()
        if not row:
            raise KeyError(invocation_id)
        result = dict(zip(keys, row, strict=True))
        # Preserve a stable API shape while making unsupported v43 values
        # explicitly unknown rather than positionally corrupting the run ID.
        result.setdefault("sealed_at", None)
        result.setdefault("outcome_summary", None)
        result["research_run_external_id"] = result.get("research_run_id")
        result["assets"] = _asset_export_rows(cur, result["batch_id"], v43=v43)
        return result


def _export_invocation_by_batch(self, batch_id):
    """Export one invocation by batch ID with the same canonical shape."""
    v43 = self._has_sealed_at_column(self.connection)
    with self.connection.cursor() as cur:
        if v43:
            cur.execute(
                """SELECT b.id,b.invocation_id,b.operation,b.status,b.started_at,
                          b.completed_at,b.error,b.metadata,b.sealed_at,
                          b.outcome_summary,r.external_run_id
                     FROM ingestion_batches b
                     LEFT JOIN research_runs r ON r.id=b.research_run_id
                    WHERE b.id=%s""",
                (batch_id,),
            )
            keys = (
                "batch_id",
                "invocation_id",
                "operation",
                "status",
                "started_at",
                "completed_at",
                "error",
                "metadata",
                "sealed_at",
                "outcome_summary",
                "research_run_id",
            )
        else:
            cur.execute(
                """SELECT b.id,b.invocation_id,b.operation,b.status,b.started_at,
                          b.completed_at,b.error,b.metadata,r.external_run_id
                     FROM ingestion_batches b
                     LEFT JOIN research_runs r ON r.id=b.research_run_id
                    WHERE b.id=%s""",
                (batch_id,),
            )
            keys = (
                "batch_id",
                "invocation_id",
                "operation",
                "status",
                "started_at",
                "completed_at",
                "error",
                "metadata",
                "research_run_id",
            )
        row = cur.fetchone()
        if not row:
            raise KeyError(batch_id)
        result = dict(zip(keys, row, strict=True))
        result.setdefault("sealed_at", None)
        result.setdefault("outcome_summary", None)
        result["research_run_external_id"] = result.get("research_run_id")
        result["assets"] = _asset_export_rows(cur, batch_id, v43=v43)
        return result


def _get_trace(self, execution_id):
    """Expose retrieval selection with an explicit stage-specific name.

    ``selected`` is retained only as the pre-existing compatibility field;
    callers should consume ``selected_for_retrieval``.
    """
    fields = (
        "stage",
        "query",
        "filters",
        "retriever",
        "candidate_type",
        "candidate_id",
        "raw_score",
        "normalized_score",
        "rank",
        "reranker_score",
        "selected",
        "rejection_reason",
    )
    with self.connection.cursor() as cur:
        cur.execute(
            f"""SELECT {",".join(fields)}
                  FROM retrieval_events
                 WHERE retrieval_execution_id=%s
                 ORDER BY created_at,
                          CASE stage
                            WHEN 'lexical' THEN 1
                            WHEN 'semantic' THEN 2
                            WHEN 'fused' THEN 3
                            WHEN 'reranked' THEN 4
                            ELSE 5
                          END,
                          rank""",
            (execution_id,),
        )
        rows = []
        for row in cur.fetchall():
            item = dict(zip(fields, row, strict=True))
            item["selected_for_retrieval"] = bool(item["selected"])
            rows.append(item)
        return rows


def _corpus_ingest_batch(
    self,
    invocation_id: str,
    operation: str,
    requests: list,
    *,
    research_run_external_id: str | None = None,
    metadata: dict | None = None,
) -> dict:
    """Persist exact batch membership and direct constituent timing evidence."""
    import logging

    from .domain import IngestRequest, utcnow

    failures = 0
    with self.uow_factory() as uow:
        batch_id = uow.start_ingestion_batch(
            invocation_id, operation, research_run_external_id, metadata
        )
        seen_ordinals = set()
        for fallback_ordinal, item in enumerate(requests):
            ordinal = fallback_ordinal
            item_mapping = item if isinstance(item, dict) else None
            if item_mapping is not None:
                result_index = (
                    item_mapping.get("metadata", {})
                    .get("firecrawl", {})
                    .get("result_index")
                )
                if isinstance(result_index, int) and result_index >= 0:
                    ordinal = result_index
            if ordinal in seen_ordinals:
                raise ValueError(f"duplicate ingestion result ordinal: {ordinal}")
            seen_ordinals.add(ordinal)

            request = item if isinstance(item, IngestRequest) else item.get("request")
            requested_url = (
                request.requested_url
                if request is not None
                else item.get("requested_url") or item.get("url") or "unknown:"
            )
            item_metadata = item.get("metadata") if isinstance(item, dict) else None
            explicit_attempt_id = (
                item.get("extraction_attempt_id") if isinstance(item, dict) else None
            )
            attempt_id = (
                request.extraction_attempt_id
                if request is not None and request.extraction_attempt_id is not None
                else explicit_attempt_id
            )
            constituent_started_at = utcnow()
            try:
                if request is None:
                    raise RuntimeError(item.get("error") or "acquisition failed")
                prepared = self._prepare_ingest(request)
                with uow.savepoint():
                    result = uow.persist_ingest(*prepared.persist_args())
                    constituent_completed_at = utcnow()
                    uow.record_batch_asset(
                        batch_id,
                        ordinal,
                        requested_url,
                        "complete",
                        result,
                        metadata=item_metadata,
                        extraction_attempt_id=attempt_id,
                        constituent_started_at=constituent_started_at,
                        constituent_completed_at=constituent_completed_at,
                    )
                    if research_run_external_id:
                        try:
                            uow.snapshots.link_run_asset(
                                research_run_external_id,
                                result.snapshot_id,
                                "acquired",
                            )
                        except KeyError:
                            logging.getLogger(__name__).warning(
                                "link_run_asset failed for batch %s, ordinal %s: "
                                "run %s not found or not running",
                                batch_id,
                                ordinal,
                                research_run_external_id,
                            )
            except Exception as exc:  # noqa: BLE001
                failures += 1
                constituent_completed_at = utcnow()
                uow.record_batch_asset(
                    batch_id,
                    ordinal,
                    requested_url,
                    "failed",
                    error=f"{type(exc).__name__}: {exc}",
                    metadata=item_metadata,
                    extraction_attempt_id=attempt_id,
                    constituent_started_at=constituent_started_at,
                    constituent_completed_at=constituent_completed_at,
                )
        manifest = uow.export_invocation(invocation_id)
    manifest["failure_count"] = failures
    return manifest


def _corpus_finalize_ingestion_batch(
    self, batch_id: str, status: str, error: str | None = None
) -> dict:
    """Finalize and return the canonical exact-member outcome manifest."""
    with self.uow_factory() as uow:
        uow.finish_ingestion_batch(batch_id, status, error=error)
        manifest = uow.export_invocation_by_batch(batch_id)
    self._notify(
        chunk_id
        for asset in manifest.get("assets", [])
        if asset["status"] == "complete"
        for chunk_id in asset["chunk_ids"]
    )
    summary = manifest.get("outcome_summary") or {}
    # Normalize batch identity to the canonical UUID representation so that
    # downstream consumers (export, assertions, callers) always see the same
    # type PostgreSQL returns from export_invocation / export_invocation_by_batch.
    manifest["batch_id"] = UUID(str(batch_id))
    manifest["status"] = manifest.get("status", status)
    if summary:
        manifest["failure_count"] = int(summary.get("failed", 0)) + int(
            summary.get("cancelled", 0)
        )
    else:
        manifest["failure_count"] = sum(
            1 for asset in manifest.get("assets", []) if asset.get("status") == "failed"
        )
    return manifest


def _manifest_ordinal(raw_ordinal: int, metadata: Mapping[str, Any]) -> int:
    firecrawl = metadata.get("firecrawl")
    result_index = (
        firecrawl.get("result_index") if isinstance(firecrawl, Mapping) else None
    )
    return (
        result_index
        if isinstance(result_index, int) and result_index >= 0
        else raw_ordinal
    )


def _bounded_extraction_execute(
    self,
    run_id: UUID,
    run_revision: int,
    coverage_revision: int | None,
    run_state: str,
    context: dict[str, Any],
):
    """Bounded extraction with complete wave membership, including preflight terminals."""
    from . import bounded_orchestrator as bounded
    from .domain import IngestRequest, SearchAdapterResult, utcnow
    from .provider_preflight import (
        CandidatePreflightResult,
        extract_markdown,
        extract_response_metadata,
        redact_error_text,
    )
    from .run_service import RunStateError, StaleRunRevisionError
    from .stages import ContextKeys, StageResult

    if run_state not in ("extracting", "coverage_review"):
        return StageResult.failed(
            "extraction",
            f"extraction stage requires extracting/coverage_review state, got {run_state}",
        )
    raw_requests = list(context.get("raw_ingest_requests") or [])
    if not self.corpus_service or not self.extraction_service:
        return StageResult.failed(
            "extraction", "authoritative corpus/extraction services unavailable"
        )
    if not raw_requests:
        return StageResult.failed(
            "extraction", "candidates contain no authoritative acquisition records"
        )

    scrape_adapter = context.get("_candidate_scrape_adapter") or self.scrape_adapter
    attempt_by_manifest_ordinal: dict[int, dict[str, Any]] = {}
    batch_requests: list[dict[str, Any]] = []
    terminal_count = 0
    cancelled_count = 0
    wave_count = context.get(ContextKeys.WAVE_COUNT, 0)
    targets = context.get("candidate_coverage_items", {})

    for raw_ordinal, original_item in enumerate(raw_requests):
        # Work on a shallow copy so the durable batch representation can carry
        # exact attempt identity without mutating acquisition history in-place.
        item = dict(original_item)
        metadata = dict(item.get("metadata", {}))
        candidate_raw = metadata.get("candidate_id")
        if not candidate_raw:
            return StageResult.failed(
                "extraction", "extraction request is missing candidate provenance"
            )
        candidate_id = UUID(str(candidate_raw))
        requested_url = item.get("requested_url") or "unknown:"
        request = item.get("request")
        if request is not None:
            requested_url = request.requested_url
        outcome = bounded._metadata_preflight(metadata)
        provider_result: SearchAdapterResult | None = None
        attempt_started_at = utcnow()
        attempt_id = self.extraction_service.create_attempt(
            candidate_id=candidate_id,
            run_id=run_id,
            method="firecrawl_main_content",
            method_version="cli-1.19.27",
            requested_format="markdown",
            start_time=attempt_started_at,
        )
        manifest_ordinal = _manifest_ordinal(raw_ordinal, metadata)
        item["extraction_attempt_id"] = attempt_id
        item["requested_url"] = str(requested_url)

        if request is None and (outcome is None or not outcome.terminal):
            provider_result = scrape_adapter.scrape_url(str(requested_url))
            raw_preflight = provider_result.transport_metadata.get("preflight")
            if isinstance(raw_preflight, Mapping):
                outcome = CandidatePreflightResult.from_metadata(raw_preflight)
            else:
                outcome = self.preflight_checker.check(provider_result)
            bounded._apply_preflight_metadata(metadata, outcome)
            item["metadata"] = metadata

            if not outcome.terminal:
                provider_data = provider_result.raw_payload
                markdown = extract_markdown(json.loads(provider_data))
                if not isinstance(markdown, str) or not markdown.strip():
                    outcome = CandidatePreflightResult(
                        classification="empty_content",
                        reason_code="missing_usable_content",
                        reason="bounded provider result had no usable markdown",
                        failure_stage="content_suitability",
                        http_status=provider_result.http_status,
                        elapsed_seconds=bounded._safe_float(
                            provider_result.transport_metadata.get("elapsed_seconds")
                        ),
                        cancelled=True,
                        terminal=True,
                    )
                    bounded._apply_preflight_metadata(metadata, outcome)
                else:
                    provider_metadata = extract_response_metadata(
                        json.loads(provider_data)
                    )
                    request = IngestRequest(
                        requested_url=str(requested_url),
                        final_url=provider_metadata.get("url")
                        or provider_metadata.get("sourceURL")
                        or str(requested_url),
                        content=markdown.encode("utf-8"),
                        normalized_content=markdown.encode("utf-8"),
                        mime_type="text/markdown",
                        title=item.get("title") or provider_metadata.get("title"),
                        http_status=provider_result.http_status,
                        firecrawl_version="cli-1.19.27",
                        crawl_options={
                            "operation": "bounded candidate scrape",
                            "formats": ["markdown"],
                        },
                        metadata=metadata,
                    )
                    item["request"] = request

        if outcome is None and request is not None:
            synthetic = SearchAdapterResult(
                raw_payload=(
                    b'{"markdown": '
                    + json.dumps(
                        request.content.decode("utf-8", errors="replace")
                    ).encode()
                    + b"}"
                ),
                http_status=request.http_status,
            )
            outcome = self.preflight_checker.check(synthetic)
            bounded._apply_preflight_metadata(metadata, outcome)
            item["metadata"] = metadata

        terminalized = False
        raw_blob = normalized_blob = None
        if outcome is not None and outcome.terminal:
            failure_class = bounded._failure_class(outcome.classification)
            self.extraction_service.complete_attempt(
                attempt_id=attempt_id,
                exit_status="cancelled" if outcome.cancelled else "failed",
                failure_class=failure_class,
                http_status=outcome.http_status,
                backend_status=(
                    f"preflight:{outcome.failure_stage}:{outcome.reason_code}"
                )[:500],
                error_message=bounded._audit_message(outcome),
                end_time=(
                    provider_result.responded_at
                    if provider_result is not None
                    else None
                ),
            )
            terminalized = True
            terminal_count += 1
            cancelled_count += int(outcome.cancelled)
            item.pop("request", None)
            item["error"] = bounded._audit_message(outcome)
            self._record_coverage_failure(
                run_id,
                candidate_id,
                str(requested_url),
                targets,
                wave_count,
            )
        elif request is None:
            fallback = CandidatePreflightResult(
                classification="malformed",
                reason_code="missing_ingest_request",
                reason="candidate passed no authoritative content to ingestion",
                failure_stage="candidate_preflight",
                cancelled=False,
                terminal=True,
            )
            self.extraction_service.complete_attempt(
                attempt_id=attempt_id,
                exit_status="failed",
                failure_class="malformed",
                backend_status="preflight:candidate_preflight:missing_ingest_request",
                error_message=bounded._audit_message(fallback),
            )
            terminalized = True
            terminal_count += 1
            item.pop("request", None)
            item["error"] = bounded._audit_message(fallback)
            self._record_coverage_failure(
                run_id,
                candidate_id,
                str(requested_url),
                targets,
                wave_count,
            )
        else:
            raw_blob = self.extraction_service.store_raw_blob(request.content)
            normalized = request.normalized_content or request.content
            normalized_blob = self.extraction_service.store_normalized_blob(normalized)
            item["request"] = replace(request, extraction_attempt_id=attempt_id)

        attempt_by_manifest_ordinal[manifest_ordinal] = {
            "attempt_id": attempt_id,
            "candidate_id": candidate_id,
            "raw_blob": raw_blob,
            "normalized_blob": normalized_blob,
            "metadata": metadata,
            "terminalized": terminalized,
        }
        batch_requests.append(item)

    invocation_id = f"extract:{run_id}:w{wave_count}"
    run_status = self.run_service.status(run_id=run_id)
    if not run_status.external_id:
        return StageResult.failed(
            "extraction", "research run has no external ID for asset linkage"
        )

    try:
        manifest = self.corpus_service.bounded_ingest_batch(
            invocation_id=invocation_id,
            operation="orchestration_extract",
            requests=batch_requests,
            research_run_external_id=run_status.external_id,
            metadata={
                "run_id": str(run_id),
                "authority": "firecrawl-cli-1.19.27",
                "bounded_preflight": True,
            },
        )
    except Exception as exc:  # noqa: BLE001
        safe_error = redact_error_text(exc)
        for attempt in attempt_by_manifest_ordinal.values():
            if attempt["terminalized"]:
                continue
            self.extraction_service.complete_attempt(
                attempt_id=attempt["attempt_id"],
                exit_status="failed",
                raw_blob=attempt["raw_blob"],
                normalized_blob=attempt["normalized_blob"],
                failure_class="internal",
                backend_status="corpus_ingestion_failed",
                error_message=safe_error,
            )
        return StageResult.failed(
            "extraction", f"authoritative corpus ingestion failed: {safe_error}"
        )

    completed_assets: list[dict[str, Any]] = []
    for asset in manifest.get("assets", []):
        ordinal = int(asset["ordinal"])
        attempt = attempt_by_manifest_ordinal.get(ordinal)
        if attempt is None:
            return StageResult.failed(
                "extraction",
                f"corpus manifest ordinal {ordinal} has no extraction attempt",
            )
        if attempt["terminalized"]:
            if asset.get("status") != "failed":
                return StageResult.failed(
                    "extraction",
                    f"terminal preflight member {ordinal} was not persisted as failed",
                )
            continue

        succeeded = asset.get("status") == "complete"
        self.extraction_service.complete_attempt(
            attempt_id=attempt["attempt_id"],
            exit_status="succeeded" if succeeded else "failed",
            raw_blob=attempt["raw_blob"],
            normalized_blob=attempt["normalized_blob"],
            parser_used=self.config.parser_version if succeeded else None,
            failure_class=(
                "none"
                if succeeded
                else bounded._extraction_failure_class(asset.get("error"))
            ),
            http_status=attempt["metadata"].get("firecrawl", {}).get("status_code"),
            backend_status=asset.get("status"),
            error_message=(
                redact_error_text(asset.get("error")) if asset.get("error") else None
            ),
        )
        candidate_id = attempt["candidate_id"]
        source_url = asset.get("requested_url")
        for item_id in targets.get(str(candidate_id), []):
            self.coverage_service.apply_extraction_attempted(
                run_id,
                UUID(item_id),
                source_url=source_url,
                extraction_status="success" if succeeded else "failed",
                idempotency_key=(
                    f"extract:{run_id}:w{wave_count}:{candidate_id}:{item_id}"
                ),
            )
        if not succeeded:
            continue
        if not asset.get("snapshot_id") or not asset.get("chunk_ids"):
            return StageResult.failed(
                "extraction", "complete corpus asset lacks snapshot/chunk identity"
            )
        self.extraction_service.select_final_attempt(
            candidate_id=candidate_id,
            attempt_id=attempt["attempt_id"],
            selection_reason="bounded authoritative Firecrawl markdown persisted",
        )
        authoritative_asset = {
            **asset,
            "candidate_id": str(candidate_id),
            "extraction_attempt_id": str(attempt["attempt_id"]),
        }
        completed_assets.append(authoritative_asset)
        for item_id in targets.get(str(candidate_id), []):
            self.coverage_service.apply_asset_acquired(
                run_id,
                UUID(item_id),
                source_url=source_url,
                idempotency_key=(
                    f"acquired:{run_id}:w{wave_count}:{candidate_id}:{item_id}"
                ),
            )

    success_count = sum(
        1 for asset in manifest.get("assets", []) if asset.get("status") == "complete"
    )
    failure_count = len(manifest.get("assets", [])) - success_count
    batch_status = (
        "complete"
        if failure_count == 0
        else "failed"
        if success_count == 0
        else "partial"
    )
    try:
        manifest = self.corpus_service.finalize_ingestion_batch(
            manifest["batch_id"], batch_status
        )
    except Exception as exc:  # noqa: BLE001
        return StageResult.failed(
            "extraction", f"authoritative batch finalization failed: {exc}"
        )

    extraction_success_count = len(completed_assets)
    context.setdefault("extracted_assets", []).extend(completed_assets)
    context[ContextKeys.EXTRACTION_SUCCESS_COUNT] = extraction_success_count
    context[ContextKeys.EXTRACTION_ATTEMPTS] = len(raw_requests)
    context["cancelled_extraction_count"] = cancelled_count
    context["preflight_terminal_count"] = terminal_count
    context["successful_extraction_count"] = (
        int(context.get("successful_extraction_count", 0)) + extraction_success_count
    )

    if extraction_success_count > 0:
        try:
            self.run_service.transition(
                run_id,
                "indexing",
                expected_revision=run_revision,
                idempotency_key=f"stage:extraction_done:{run_id}:{uuid4()}",
                actor_type="orchestrator",
                actor_identifier="BoundedExtractionStage",
                triggering_event="run.indexing",
                reason=f"extraction succeeded for {extraction_success_count} sources",
            )
        except (RunStateError, StaleRunRevisionError) as exc:
            return StageResult.failed("extraction", str(exc))
    else:
        try:
            self.run_service.transition(
                run_id,
                "coverage_review",
                expected_revision=run_revision,
                idempotency_key=f"stage:extraction_empty:{run_id}:{uuid4()}",
                actor_type="orchestrator",
                actor_identifier="BoundedExtractionStage",
                triggering_event="run.coverage_review",
                reason="no successful bounded extractions, reviewing coverage",
            )
        except (RunStateError, StaleRunRevisionError) as exc:
            return StageResult.failed("extraction", str(exc))

    return StageResult.ok(
        "extraction",
        f"{extraction_success_count} successful extractions",
        details={
            ContextKeys.EXTRACTION_SUCCESS_COUNT: extraction_success_count,
            ContextKeys.EXTRACTION_ATTEMPTS: len(raw_requests),
            "cancelled_extraction_count": cancelled_count,
            "preflight_terminal_count": terminal_count,
            "batch_id": manifest["batch_id"],
            "outcome_summary": manifest.get("outcome_summary"),
            "extracted_assets": completed_assets,
        },
    )


def _promotion_list_assets(self, run_id: UUID) -> list[dict[str, Any]]:
    """Expose explicit ARC-05 stage flags without inferring historical intent."""
    items = _ORIGINAL_PROMOTION_LIST_ASSETS(self, run_id)
    events = self.list_events(run_id)
    reached: dict[str, set[str]] = defaultdict(set)
    for event in events:
        subject_id = event.get("subject_id")
        to_stage = event.get("to_stage")
        if subject_id is not None and to_stage:
            reached[str(subject_id)].add(str(to_stage))

    for item in items:
        item["selection_semantics_version"] = "asset-promotion-stage-flags-v1"
        if item.get("id") is None or item.get("current_stage") == "unknown":
            # Unknown remains unknown. False would incorrectly assert that the
            # historical asset never passed the stage.
            for field in (
                "selected_for_extraction",
                "extraction_succeeded",
                "retained",
                "evidence_eligible",
                "completion_critical",
            ):
                item[field] = None
            continue

        subject_key = str(item["id"])
        stages = set(reached.get(subject_key, set()))
        current_stage = str(item.get("current_stage") or "")
        if current_stage:
            stages.add(current_stage)
        item["selected_for_extraction"] = "selected_for_extraction" in stages
        item["extraction_succeeded"] = "extracted" in stages
        item["retained"] = "retained" in stages
        item["evidence_eligible"] = "evidence_eligible" in stages
        item["completion_critical"] = "completion_critical" in stages
    return items


def install_issue_217_contract(postgres_module, service_module, bounded_module) -> None:
    """Install the issue #217 contract on the canonical production classes."""
    global _ORIGINAL_PROMOTION_LIST_ASSETS

    uow = postgres_module.PostgresUnitOfWork
    uow._has_constituent_timing_columns = staticmethod(_has_constituent_timing_columns)
    uow.start_ingestion_batch = _start_ingestion_batch
    uow.record_batch_asset = _record_batch_asset
    uow.finish_ingestion_batch = _finish_ingestion_batch
    uow.export_invocation = _export_invocation
    uow.export_invocation_by_batch = _export_invocation_by_batch
    uow.get_trace = _get_trace

    service_module.CorpusService.ingest_batch = _corpus_ingest_batch
    service_module.CorpusService.finalize_ingestion_batch = (
        _corpus_finalize_ingestion_batch
    )
    bounded_module.BoundedExtractionStage.execute = _bounded_extraction_execute

    from .asset_promotion_service import AssetPromotionService

    if _ORIGINAL_PROMOTION_LIST_ASSETS is None:
        _ORIGINAL_PROMOTION_LIST_ASSETS = AssetPromotionService.list_assets
    AssetPromotionService.list_assets = _promotion_list_assets
