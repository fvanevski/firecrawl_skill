"""Checkpoint-backed indexing stage for the coverage-led orchestrator."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event
from typing import Any
from uuid import UUID

from ...asset_promotion_service import AssetPromotionError
from ...candidate_budget_outcomes import CandidateBudgetAdmissionBoundaryError
from ...stages import ContextKeys, StageResult
from .drain import drain_index_jobs_result
from .index_checkpoint_service import IndexCheckpointService
from .indexing import IndexWorker

INDEX_CHECKPOINT_PENDING_PREFIX = "index_checkpoint_pending:"


class IndexCheckpointPending(RuntimeError):
    """A bounded indexing attempt stopped with recoverable persisted work."""


def _raise_pending(details: dict[str, Any], reason: str) -> None:
    payload = {"reason": reason, **details}
    raise IndexCheckpointPending(
        INDEX_CHECKPOINT_PENDING_PREFIX
        + json.dumps(payload, sort_keys=True, default=str)
    )


class _PersistingCheckpointRunner:
    def __init__(
        self,
        *,
        worker: IndexWorker,
        checkpoint_service: IndexCheckpointService,
        checkpoint: Any,
        deadline_at: datetime,
    ) -> None:
        self.worker = worker
        self.checkpoint_service = checkpoint_service
        self.checkpoint = checkpoint
        self.deadline_at = deadline_at
        self.aggregate: dict[str, int | float] = {
            "claimed": 0,
            "complete": 0,
            "failed": 0,
            "lease_lost": 0,
            "embedding_batches": 0,
            "embedding_texts": 0,
            "embedding_elapsed_seconds": 0.0,
        }

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        command = list(argv)
        try:
            batch_flag = command.index("--batch-size")
            limit = int(command[batch_flag + 1])
            result = self.worker.run_batch(
                limit=limit, entity_ids=list(self.checkpoint.entity_ids)
            )
            census = result.get("census")
            if not isinstance(census, dict):
                raise TypeError("scoped indexing result omitted its census")
            self.checkpoint = self.checkpoint_service.observe(
                self.checkpoint.id, census, deadline_at=self.deadline_at
            )
            for field in self.aggregate:
                value = result.get(field, 0)
                if field == "embedding_elapsed_seconds":
                    self.aggregate[field] += float(value)
                else:
                    self.aggregate[field] += int(value)
            result["checkpoint"] = self.checkpoint.to_dict()
            return subprocess.CompletedProcess(
                command, 0, json.dumps(result, sort_keys=True, default=str), ""
            )
        except Exception as exc:  # noqa: BLE001
            return subprocess.CompletedProcess(
                command,
                1,
                json.dumps(
                    {
                        "claimed": 0,
                        "failed": 0,
                        "lease_lost": 0,
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                    sort_keys=True,
                ),
                str(exc),
            )


class CheckpointIndexingStage:
    """Persist progress and advance only from a fresh exact PostgreSQL census."""

    def __init__(
        self, run_service: Any, config: Any, corpus_service: Any | None = None
    ) -> None:
        self.run_service = run_service
        self.config = config
        self.corpus_service = corpus_service

    def execute(
        self,
        run_id: UUID,
        run_revision: int,
        coverage_revision: int | None,
        run_state: str,
        context: dict[str, Any],
    ) -> StageResult:
        del coverage_revision
        if run_state != "indexing":
            return StageResult.failed(
                "indexing", f"indexing stage requires indexing state, got {run_state}"
            )
        corpus_service = self.corpus_service
        if not (
            corpus_service
            and getattr(corpus_service, "index", None)
            and getattr(corpus_service, "embedder", None)
        ):
            return StageResult.failed(
                "indexing", "vector index and embedding services are required"
            )

        embedder = corpus_service.embedder
        fingerprint = getattr(embedder, "fingerprint", None)
        if not isinstance(fingerprint, str) or not fingerprint.strip():
            return StageResult.failed(
                "indexing", "configured embedder lacks an immutable fingerprint"
            )
        fingerprint = fingerprint.strip()

        deadline_seconds = float(context.get("index_checkpoint_deadline_seconds", 30.0))
        if deadline_seconds <= 0:
            return StageResult.failed(
                "indexing", "index checkpoint deadline must be positive"
            )
        max_batches = int(context.get("index_checkpoint_max_batches", 10_000))
        if max_batches <= 0:
            return StageResult.failed(
                "indexing", "index checkpoint max batches must be positive"
            )
        deadline_at = datetime.now(timezone.utc) + timedelta(seconds=deadline_seconds)

        cancellation = context.get("_cancellation_event")
        if cancellation is None or not all(
            callable(getattr(cancellation, name, None)) for name in ("is_set", "wait")
        ):
            cancellation = Event()

        max_attempts = int(getattr(self.config, "max_index_attempts", 5))
        lease_seconds = int(getattr(self.config, "job_lease_seconds", 60))
        embedding_batch_size = int(getattr(self.config, "embedding_batch_size", 64))
        checkpoints = IndexCheckpointService(
            self.run_service.uow_factory, max_attempts=max_attempts
        )
        try:
            checkpoint = checkpoints.ensure(
                run_id,
                lifecycle_revision=run_revision,
                fingerprint=fingerprint,
                deadline_at=deadline_at,
                idempotency_key=f"orchestrator:index-checkpoint:{run_id}:r{run_revision}",
            )
        except CandidateBudgetAdmissionBoundaryError:
            # The exact budget decision that failed raised this typed boundary.
            # Preserve it to the resume layer; never reinterpret another error by
            # looking at an older persisted check.
            raise
        except AssetPromotionError as exc:
            return StageResult.failed(
                "indexing", f"index checkpoint creation failed: {exc}"
            )
        except Exception as exc:  # noqa: BLE001
            return StageResult.failed(
                "indexing", f"index checkpoint creation failed: {exc}"
            )
        if checkpoint.status != "active":
            return StageResult.failed(
                "indexing",
                "index checkpoint is not resumable: "
                f"{checkpoint.invalidation_reason or checkpoint.status}",
                details={"checkpoint": checkpoint.to_dict()},
            )

        worker = IndexWorker(
            uow_factory=corpus_service.uow_factory,
            index=corpus_service.index,
            embedder=embedder,
            queue=getattr(corpus_service, "queue", None),
            lease_seconds=lease_seconds,
            max_attempts=max_attempts,
        )
        runner = _PersistingCheckpointRunner(
            worker=worker,
            checkpoint_service=checkpoints,
            checkpoint=checkpoint,
            deadline_at=deadline_at,
        )
        drain = drain_index_jobs_result(
            Path("research-db"),
            batch_size=embedding_batch_size,
            max_batches=max_batches,
            deadline_seconds=deadline_seconds,
            initial_backoff_seconds=0.25,
            max_backoff_seconds=5.0,
            runner=runner,
            require_census=True,
            waiter=cancellation.wait,
            cancelled=cancellation.is_set,
        )
        checkpoint = runner.checkpoint
        details = {
            "checkpoint": checkpoint.to_dict(),
            "drain": drain.to_dict(),
            ContextKeys.INDEX_BUILD_ID: str(checkpoint.id),
            ContextKeys.INDEX_FINGERPRINT: fingerprint,
        }
        if drain.status in {"resumable", "cancelled"}:
            context["_index_checkpoint_resume"] = details
            _raise_pending(details, drain.reason)
        if drain.status != "complete":
            return StageResult.failed(
                "indexing", f"indexing failed closed: {drain.reason}", details=details
            )

        finalization = checkpoints.finalize(
            run_id,
            checkpoint.id,
            expected_revision=run_revision,
            idempotency_key=f"orchestrator:index-checkpoint:{checkpoint.id}:finalize",
            actor_type="orchestrator",
            actor_identifier="CheckpointIndexingStage",
            reason="sealed exact-run indexing census is complete",
        )
        details["finalization"] = finalization.to_dict()
        if finalization.status == "recoverable":
            context["_index_checkpoint_resume"] = details
            _raise_pending(details, "fresh_census_recoverable")
        if not finalization.advanced:
            return StageResult.failed(
                "indexing",
                f"guarded finalization failed closed: {finalization.status}",
                details=details,
            )

        telemetry_error = self._record_telemetry(
            corpus_service, run_id, checkpoint.entity_ids, runner.aggregate
        )
        if telemetry_error:
            details["telemetry_warning"] = telemetry_error
        context[ContextKeys.INDEX_BUILD_ID] = str(checkpoint.id)
        context[ContextKeys.INDEX_FINGERPRINT] = fingerprint
        return StageResult.ok(
            "indexing",
            "checkpoint census complete; transitioned to coverage_review",
            details=details,
            warnings=((telemetry_error,) if telemetry_error else ()),
        )

    def _record_telemetry(
        self,
        corpus_service: Any,
        run_id: UUID,
        entity_ids: tuple[UUID, ...],
        aggregate: dict[str, int | float],
    ) -> str | None:
        try:
            elapsed = float(aggregate["embedding_elapsed_seconds"])
            measured_texts = int(aggregate["embedding_texts"])
            measured_vectors = int(aggregate["complete"])
            if measured_texts == 0:
                batch_size = int(getattr(self.config, "embedding_batch_size", 64))
                sample_ids = list(entity_ids[:batch_size])
                with corpus_service.uow_factory() as uow:
                    records = uow.chunks.chunks_for_index(sample_ids)
                texts = [record["text"] for record in records]
                if texts:
                    from time import monotonic

                    started = monotonic()
                    vectors = corpus_service.embedder.batch(texts)
                    elapsed = monotonic() - started
                    measured_texts = len(texts)
                    measured_vectors = len(vectors)
                    aggregate["embedding_batches"] = 1
            from ...telemetry_service import PerformanceTelemetryService

            with corpus_service.uow_factory() as uow:
                PerformanceTelemetryService(uow.connection).record_embedding_throughput(
                    run_id,
                    "indexing",
                    batch_count=int(aggregate["embedding_batches"]),
                    vector_count=measured_vectors,
                    failed_count=int(aggregate["failed"]),
                    total_texts=measured_texts,
                    elapsed_seconds=elapsed,
                    endpoint_url=str(getattr(self.config, "embedding_url", "") or ""),
                    endpoint_model=str(
                        getattr(self.config, "embedding_model", "") or ""
                    ),
                    dimension=getattr(self.config, "embedding_dimension", None),
                )
            return None
        except Exception as exc:  # noqa: BLE001
            return f"{type(exc).__name__}: {exc}"
