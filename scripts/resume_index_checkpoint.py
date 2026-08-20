#!/usr/bin/env python3
"""Resume one persisted indexing checkpoint and finalize it atomically."""

from __future__ import annotations

import argparse
import json
import signal
import subprocess
import time
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event
from typing import Any

from drain_index_jobs import (
    CANCELLED_EXIT_CODE,
    RESUMABLE_EXIT_CODE,
    DrainCancelled,
    DrainResult,
    drain_index_jobs_result,
)

from firecrawl_skill.research_store.config import StoreConfig
from firecrawl_skill.research_store.container import build_run_service, build_service
from firecrawl_skill.research_store.index_checkpoint_replay import (
    replay_completed_checkpoint,
)
from firecrawl_skill.research_store.index_checkpoint_service import (
    IndexCheckpointService,
)
from firecrawl_skill.research_store.indexing import IndexWorker


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Resume the exact PostgreSQL membership sealed for one indexing run, "
            "persist each census observation, and advance to coverage_review only "
            "after a fresh guarded finalization read."
        )
    )
    parser.add_argument("research_run_id")
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--max-batches", type=int, default=10_000)
    parser.add_argument("--deadline-seconds", type=float, default=300.0)
    parser.add_argument("--initial-backoff-seconds", type=float, default=0.25)
    parser.add_argument("--max-backoff-seconds", type=float, default=5.0)
    return parser


class CheckpointRunner:
    def __init__(
        self,
        *,
        worker: IndexWorker,
        checkpoint_service: IndexCheckpointService,
        checkpoint,
        deadline_at: datetime,
    ) -> None:
        self.worker = worker
        self.checkpoint_service = checkpoint_service
        self.checkpoint = checkpoint
        self.deadline_at = deadline_at

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        try:
            command = list(argv)
            batch_flag = command.index("--batch-size")
            limit = int(command[batch_flag + 1])
            result = self.worker.run_batch(
                limit=limit,
                entity_ids=list(self.checkpoint.entity_ids),
            )
            census = result.get("census")
            if not isinstance(census, dict):
                raise TypeError("scoped worker did not return the required census")
            self.checkpoint = self.checkpoint_service.observe(
                self.checkpoint.id,
                census,
                deadline_at=self.deadline_at,
            )
            result["checkpoint"] = self.checkpoint.to_dict()
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps(result, sort_keys=True, default=str),
                "",
            )
        except Exception as exc:  # noqa: BLE001
            return subprocess.CompletedProcess(
                list(argv),
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


def _build_runner(
    external_run_id: str,
    *,
    deadline_at: datetime,
    cancelled,
):
    if cancelled():
        raise DrainCancelled
    config = StoreConfig.from_env()
    config.require_database()
    run_service = build_run_service(config)
    status = run_service.status(external_id=external_run_id)
    checkpoint_service = IndexCheckpointService(
        run_service.uow_factory,
        max_attempts=config.max_index_attempts,
    )

    if status.state != "indexing":
        replay = replay_completed_checkpoint(checkpoint_service, status.id)
        return run_service, checkpoint_service, None, replay
    if cancelled():
        raise DrainCancelled

    corpus_service = build_service(config)
    if corpus_service.embedder is None:
        raise RuntimeError("configured embedding service is required")
    fingerprint = getattr(corpus_service.embedder, "fingerprint", None)
    if not isinstance(fingerprint, str) or not fingerprint.strip():
        raise RuntimeError("configured embedder must expose an immutable fingerprint")

    checkpoint = checkpoint_service.ensure(
        status.id,
        lifecycle_revision=status.lifecycle_revision,
        fingerprint=fingerprint.strip(),
        deadline_at=deadline_at,
        idempotency_key=f"index-checkpoint:{status.id}:r{status.lifecycle_revision}",
    )
    if checkpoint.status != "active":
        raise RuntimeError(
            f"indexing checkpoint is {checkpoint.status}: "
            f"{checkpoint.invalidation_reason or 'not resumable'}"
        )
    if cancelled():
        raise DrainCancelled

    worker = IndexWorker(
        uow_factory=corpus_service.uow_factory,
        index=corpus_service.index,
        embedder=corpus_service.embedder,
        queue=corpus_service.queue,
        lease_seconds=config.job_lease_seconds,
        max_attempts=config.max_index_attempts,
    )
    return (
        run_service,
        checkpoint_service,
        CheckpointRunner(
            worker=worker,
            checkpoint_service=checkpoint_service,
            checkpoint=checkpoint,
            deadline_at=deadline_at,
        ),
        None,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.batch_size <= 0:
        raise SystemExit("--batch-size must be positive")
    if args.max_batches <= 0:
        raise SystemExit("--max-batches must be positive")
    if args.deadline_seconds <= 0:
        raise SystemExit("--deadline-seconds must be positive")
    if args.initial_backoff_seconds <= 0:
        raise SystemExit("--initial-backoff-seconds must be positive")
    if args.max_backoff_seconds < args.initial_backoff_seconds:
        raise SystemExit(
            "--max-backoff-seconds must be at least --initial-backoff-seconds"
        )

    stop = Event()
    previous: dict[int, Any] = {}
    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            previous[signum] = signal.signal(signum, lambda *_args: stop.set())
        except ValueError:
            break

    setup_started = time.monotonic()
    deadline_at = datetime.now(timezone.utc) + timedelta(seconds=args.deadline_seconds)
    checkpoint = None
    finalization = None
    try:
        run_service, checkpoint_service, runner, finalization = _build_runner(
            args.research_run_id,
            deadline_at=deadline_at,
            cancelled=stop.is_set,
        )
        if finalization is not None:
            checkpoint = finalization.checkpoint
            result = DrainResult(
                status="complete",
                exit_code=0,
                recoverable=False,
                reason="completed_checkpoint_replayed",
                batches=0,
                elapsed_seconds=max(0.0, time.monotonic() - setup_started),
                last_census=finalization.census,
            )
        else:
            if runner is None:
                raise RuntimeError(
                    "indexing resume setup omitted its checkpoint runner"
                )
            checkpoint = runner.checkpoint
            result = drain_index_jobs_result(
                Path(__file__).resolve().with_name("research-db"),
                batch_size=args.batch_size,
                max_batches=args.max_batches,
                deadline_seconds=args.deadline_seconds,
                initial_backoff_seconds=args.initial_backoff_seconds,
                max_backoff_seconds=args.max_backoff_seconds,
                runner=runner,
                require_census=True,
                waiter=stop.wait,
                cancelled=stop.is_set,
            )
            checkpoint = runner.checkpoint
            if result.status == "complete":
                current = run_service.status(external_id=args.research_run_id)
                finalization = checkpoint_service.finalize(
                    current.id,
                    checkpoint.id,
                    expected_revision=current.lifecycle_revision,
                    idempotency_key=f"index-checkpoint:{checkpoint.id}:finalize",
                    actor_type="wrapper",
                    actor_identifier="frun-resume",
                )
                if not finalization.advanced:
                    if finalization.status == "recoverable":
                        result = DrainResult(
                            status="resumable",
                            exit_code=RESUMABLE_EXIT_CODE,
                            recoverable=True,
                            reason="fresh_finalization_read_found_recoverable_work",
                            batches=result.batches,
                            elapsed_seconds=result.elapsed_seconds,
                            last_worker_result=result.last_worker_result,
                            last_census=finalization.census,
                        )
                    else:
                        result = DrainResult(
                            status="failed",
                            exit_code=1,
                            recoverable=False,
                            reason=f"finalization_{finalization.status}",
                            batches=result.batches,
                            elapsed_seconds=result.elapsed_seconds,
                            last_worker_result=result.last_worker_result,
                            last_census=finalization.census,
                        )
    except DrainCancelled:
        result = DrainResult(
            status="cancelled",
            exit_code=CANCELLED_EXIT_CODE,
            recoverable=True,
            reason="cancelled_during_setup",
            batches=0,
            elapsed_seconds=max(0.0, time.monotonic() - setup_started),
        )
    except Exception as exc:  # noqa: BLE001
        result = DrainResult(
            status="failed",
            exit_code=1,
            recoverable=False,
            reason=f"setup_failed: {type(exc).__name__}: {exc}",
            batches=0,
            elapsed_seconds=max(0.0, time.monotonic() - setup_started),
        )
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)

    payload = result.to_dict()
    if checkpoint is not None:
        payload["checkpoint"] = checkpoint.to_dict()
    if finalization is not None:
        payload["finalization"] = finalization.to_dict()
    print(json.dumps(payload, sort_keys=True, default=str))
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
