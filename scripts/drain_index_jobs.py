"""Drain durable PostgreSQL index jobs through a bounded census-aware barrier."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from threading import Event
from typing import Any
from uuid import UUID

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
Clock = Callable[[], float]
Waiter = Callable[[float], bool]

CENSUS_CLASSES = (
    "complete",
    "claimable",
    "running_live",
    "running_expired",
    "retryable_failed",
    "dead",
    "missing_job",
    "wrong_fingerprint",
    "manifest_inconsistent",
)
IRRECOVERABLE_CLASSES = (
    "dead",
    "missing_job",
    "wrong_fingerprint",
    "manifest_inconsistent",
)
RECOVERABLE_CLASSES = (
    "claimable",
    "running_live",
    "running_expired",
    "retryable_failed",
)
RESUMABLE_EXIT_CODE = 75
CANCELLED_EXIT_CODE = 130


class DrainResult:
    """Structured command outcome; this helper never advances run lifecycle state."""

    def __init__(
        self,
        *,
        status: str,
        exit_code: int,
        recoverable: bool,
        reason: str,
        batches: int,
        elapsed_seconds: float,
        last_worker_result: dict[str, Any] | None = None,
        last_census: dict[str, Any] | None = None,
    ) -> None:
        self.status = status
        self.exit_code = exit_code
        self.recoverable = recoverable
        self.reason = reason
        self.batches = batches
        self.elapsed_seconds = elapsed_seconds
        self.last_worker_result = last_worker_result
        self.last_census = last_census

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "index-drain-result-v1",
            "status": self.status,
            "exit_code": self.exit_code,
            "recoverable": self.recoverable,
            "lifecycle_terminal": False,
            "reason": self.reason,
            "batches": self.batches,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "last_worker_result": self.last_worker_result,
            "last_census": self.last_census,
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run bounded index-worker batches. With --research-run-id, seal the "
            "run's PostgreSQL chunk membership and wait until its exact census "
            "is complete or a recoverable command bound is reached."
        )
    )
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--max-batches", type=int, default=10_000)
    parser.add_argument("--deadline-seconds", type=float, default=300.0)
    parser.add_argument("--initial-backoff-seconds", type=float, default=0.25)
    parser.add_argument("--max-backoff-seconds", type=float, default=5.0)
    parser.add_argument(
        "--research-run-id",
        help=(
            "External fr_<uuid> whose exact PostgreSQL chunk membership is sealed "
            "for census-authoritative lifecycle draining."
        ),
    )
    parser.add_argument(
        "--research-db",
        default=os.environ.get("FIRECRAWL_RESEARCH_DB_COMMAND"),
        help=(
            "Path to research-db for unscoped projection maintenance; defaults "
            "to the sibling scripts/research-db."
        ),
    )
    return parser


def _default_runner(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv),
        check=False,
        capture_output=True,
        text=True,
    )


def _default_wait(seconds: float) -> bool:
    time.sleep(seconds)
    return False


def _nonnegative_int(payload: dict[str, Any], field: str) -> int:
    value = payload.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(
            f"worker result field {field!r} must be a non-negative integer"
        )
    return value


def _extract_census(payload: dict[str, Any]) -> dict[str, Any] | None:
    nested = payload.get("census")
    if nested is not None and not isinstance(nested, dict):
        raise ValueError("worker result field 'census' must be a JSON object")
    source = nested if isinstance(nested, dict) else payload
    census_markers = {
        "expected",
        "complete_manifests",
        "running_live",
        "running_expired",
        "retryable_failed",
        "missing_job",
        "wrong_fingerprint",
        "manifest_inconsistent",
    }
    if not census_markers.intersection(source):
        return None

    census: dict[str, Any] = dict(source)
    if "complete" not in census and "complete_manifests" in census:
        census["complete"] = census["complete_manifests"]
    if "complete_manifests" not in census and "complete" in census:
        census["complete_manifests"] = census["complete"]

    expected = _nonnegative_int(census, "expected")
    for field in CENSUS_CLASSES:
        _nonnegative_int(census, field)
    complete_manifests = _nonnegative_int(census, "complete_manifests")
    if complete_manifests != census["complete"]:
        raise ValueError(
            "worker census complete_manifests must equal census complete"
        )
    observed = sum(int(census[field]) for field in CENSUS_CLASSES)
    if observed != expected:
        raise ValueError(
            "worker census does not conserve sealed membership: "
            f"expected={expected} observed={observed}"
        )
    return census


def _result(
    *,
    status: str,
    exit_code: int,
    recoverable: bool,
    reason: str,
    batches: int,
    started: float,
    clock: Clock,
    payload: dict[str, Any] | None,
    census: dict[str, Any] | None,
) -> DrainResult:
    return DrainResult(
        status=status,
        exit_code=exit_code,
        recoverable=recoverable,
        reason=reason,
        batches=batches,
        elapsed_seconds=max(0.0, clock() - started),
        last_worker_result=payload,
        last_census=census,
    )


def drain_index_jobs_result(
    research_db: Path,
    *,
    batch_size: int = 64,
    max_batches: int = 10_000,
    deadline_seconds: float = 300.0,
    initial_backoff_seconds: float = 0.25,
    max_backoff_seconds: float = 5.0,
    runner: Runner = _default_runner,
    require_census: bool = False,
    clock: Clock = time.monotonic,
    waiter: Waiter | None = None,
) -> DrainResult:
    """Drain worker batches until the exact census is complete or bounded.

    Census mode is selected when the worker emits the #208 census, and is
    mandatory when ``require_census`` is true. In census mode, per-invocation
    ``claimed``, ``failed``, and ``lease_lost`` counters are diagnostic only;
    the exact PostgreSQL census determines wait, retry, reclaim, completion,
    and fail-closed behavior.
    """

    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    if max_batches <= 0:
        raise ValueError("max_batches must be positive")
    if deadline_seconds <= 0:
        raise ValueError("deadline_seconds must be positive")
    if initial_backoff_seconds <= 0:
        raise ValueError("initial_backoff_seconds must be positive")
    if max_backoff_seconds < initial_backoff_seconds:
        raise ValueError(
            "max_backoff_seconds must be greater than or equal to "
            "initial_backoff_seconds"
        )

    wait = waiter or _default_wait
    command = [
        str(research_db),
        "worker",
        "--once",
        "--batch-size",
        str(batch_size),
    ]
    started = clock()
    deadline = started + deadline_seconds
    backoff = initial_backoff_seconds
    last_payload: dict[str, Any] | None = None
    last_census: dict[str, Any] | None = None
    census_mode = require_census

    for batch_number in range(1, max_batches + 1):
        if clock() >= deadline:
            return _result(
                status="resumable" if census_mode else "failed",
                exit_code=RESUMABLE_EXIT_CODE if census_mode else 1,
                recoverable=census_mode,
                reason="deadline_exhausted",
                batches=batch_number - 1,
                started=started,
                clock=clock,
                payload=last_payload,
                census=last_census,
            )

        completed = runner(command)
        if completed.stdout:
            print(completed.stdout.rstrip())
        if completed.stderr:
            print(completed.stderr.rstrip(), file=sys.stderr)

        try:
            payload = json.loads(completed.stdout)
            if not isinstance(payload, dict):
                raise TypeError("worker result must be a JSON object")
            claimed = _nonnegative_int(payload, "claimed")
            failed = _nonnegative_int(payload, "failed")
            lease_lost = _nonnegative_int(payload, "lease_lost")
            census = _extract_census(payload)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            print(
                f"invalid worker result after batch {batch_number}: {exc}",
                file=sys.stderr,
            )
            return _result(
                status="failed",
                exit_code=1,
                recoverable=False,
                reason=f"invalid_worker_result: {exc}",
                batches=batch_number,
                started=started,
                clock=clock,
                payload=last_payload,
                census=last_census,
            )

        last_payload = payload
        if census is not None:
            census_mode = True
            last_census = census

        if completed.returncode != 0:
            print(
                f"worker batch {batch_number} exited with {completed.returncode}",
                file=sys.stderr,
            )
            return _result(
                status="failed",
                exit_code=completed.returncode or 1,
                recoverable=False,
                reason="worker_process_failed",
                batches=batch_number,
                started=started,
                clock=clock,
                payload=payload,
                census=census,
            )

        if census is None and census_mode:
            return _result(
                status="failed",
                exit_code=1,
                recoverable=False,
                reason="required_census_missing",
                batches=batch_number,
                started=started,
                clock=clock,
                payload=payload,
                census=last_census,
            )

        if census is None:
            # Backward-compatible unscoped projection maintenance. It is not
            # lifecycle completion evidence because no sealed census is present.
            if failed or lease_lost:
                print(
                    "worker drain stopped because the unscoped result reported "
                    f"failed={failed}, lease_lost={lease_lost}",
                    file=sys.stderr,
                )
                return _result(
                    status="failed",
                    exit_code=1,
                    recoverable=False,
                    reason="unscoped_worker_failure",
                    batches=batch_number,
                    started=started,
                    clock=clock,
                    payload=payload,
                    census=None,
                )
            if claimed == 0:
                return _result(
                    status="complete",
                    exit_code=0,
                    recoverable=False,
                    reason="unscoped_queue_empty",
                    batches=batch_number,
                    started=started,
                    clock=clock,
                    payload=payload,
                    census=None,
                )
            continue

        irrecoverable = {
            field: int(census[field])
            for field in IRRECOVERABLE_CLASSES
            if int(census[field]) > 0
        }
        if irrecoverable:
            print(
                "worker drain failed closed on irrecoverable census classes: "
                + json.dumps(irrecoverable, sort_keys=True),
                file=sys.stderr,
            )
            return _result(
                status="failed",
                exit_code=1,
                recoverable=False,
                reason="irrecoverable_census_state",
                batches=batch_number,
                started=started,
                clock=clock,
                payload=payload,
                census=census,
            )

        expected = int(census["expected"])
        complete = int(census["complete"])
        recoverable_count = sum(int(census[field]) for field in RECOVERABLE_CLASSES)
        if complete == expected and recoverable_count == 0:
            return _result(
                status="complete",
                exit_code=0,
                recoverable=False,
                reason="sealed_census_complete",
                batches=batch_number,
                started=started,
                clock=clock,
                payload=payload,
                census=census,
            )
        if recoverable_count == 0:
            return _result(
                status="failed",
                exit_code=1,
                recoverable=False,
                reason="incomplete_census_without_recoverable_work",
                batches=batch_number,
                started=started,
                clock=clock,
                payload=payload,
                census=census,
            )

        # A productive batch may be followed immediately by another claim.
        # A zero-claim observation with recoverable work must wait rather than
        # spin, then reobserve the authoritative census.
        if claimed > 0:
            backoff = initial_backoff_seconds
            continue
        if batch_number == max_batches:
            break

        remaining = deadline - clock()
        if remaining <= 0:
            return _result(
                status="resumable",
                exit_code=RESUMABLE_EXIT_CODE,
                recoverable=True,
                reason="deadline_exhausted",
                batches=batch_number,
                started=started,
                clock=clock,
                payload=payload,
                census=census,
            )
        delay = min(backoff, remaining)
        if wait(delay):
            return _result(
                status="cancelled",
                exit_code=CANCELLED_EXIT_CODE,
                recoverable=True,
                reason="cancelled_while_recoverable",
                batches=batch_number,
                started=started,
                clock=clock,
                payload=payload,
                census=census,
            )
        backoff = min(max_backoff_seconds, backoff * 2)

    if census_mode and last_census is not None:
        return _result(
            status="resumable",
            exit_code=RESUMABLE_EXIT_CODE,
            recoverable=True,
            reason="max_batches_exhausted",
            batches=max_batches,
            started=started,
            clock=clock,
            payload=last_payload,
            census=last_census,
        )
    print(
        f"worker drain exceeded --max-batches={max_batches} before reaching claimed=0",
        file=sys.stderr,
    )
    return _result(
        status="failed",
        exit_code=1,
        recoverable=False,
        reason="max_batches_exhausted_without_census",
        batches=max_batches,
        started=started,
        clock=clock,
        payload=last_payload,
        census=None,
    )


def drain_index_jobs(
    research_db: Path,
    *,
    batch_size: int = 64,
    max_batches: int = 10_000,
    deadline_seconds: float = 300.0,
    initial_backoff_seconds: float = 0.25,
    max_backoff_seconds: float = 5.0,
    runner: Runner = _default_runner,
    require_census: bool = False,
    clock: Clock = time.monotonic,
    waiter: Waiter | None = None,
) -> int:
    """Compatibility wrapper returning only the command exit code."""

    return drain_index_jobs_result(
        research_db,
        batch_size=batch_size,
        max_batches=max_batches,
        deadline_seconds=deadline_seconds,
        initial_backoff_seconds=initial_backoff_seconds,
        max_backoff_seconds=max_backoff_seconds,
        runner=runner,
        require_census=require_census,
        clock=clock,
        waiter=waiter,
    ).exit_code


def _run_scoped_runner(external_run_id: str) -> Runner:
    """Seal one run's current chunk IDs and return a scoped worker runner."""

    from research_store.config import StoreConfig
    from research_store.container import build_run_service, build_service
    from research_store.indexing import IndexWorker

    config = StoreConfig.from_env()
    config.require_database()
    run_service = build_run_service(config)
    status = run_service.status(external_id=external_run_id)
    if status.state != "indexing":
        raise RuntimeError(
            f"research run {external_run_id} must be in indexing state, "
            f"got {status.state}"
        )

    corpus_service = build_service(config)
    if corpus_service.embedder is None:
        raise RuntimeError(
            "configured embedding service is required for index draining"
        )
    with corpus_service.uow_factory() as uow, uow.connection.cursor() as cursor:
        cursor.execute(
            """SELECT DISTINCT c.id
                   FROM research_run_assets ra
                   JOIN documents d ON d.snapshot_id=ra.snapshot_id
                   JOIN chunks c ON c.document_id=d.id
                   WHERE ra.run_id=%s
                     AND d.parser_version=%s
                     AND d.normalization_version=%s
                     AND c.chunker_version=%s
                   ORDER BY c.id""",
            (
                status.id,
                uow.parser_version,
                uow.normalization_version,
                uow.chunker_version,
            ),
        )
        entity_ids = [UUID(str(row[0])) for row in cursor.fetchall()]

    worker = IndexWorker(
        uow_factory=corpus_service.uow_factory,
        index=corpus_service.index,
        embedder=corpus_service.embedder,
        queue=corpus_service.queue,
        lease_seconds=config.job_lease_seconds,
        max_attempts=config.max_index_attempts,
    )

    def run_scoped(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        try:
            command = list(argv)
            batch_flag = command.index("--batch-size")
            limit = int(command[batch_flag + 1])
            result = worker.run_batch(
                limit=limit,
                entity_ids=entity_ids,
            )
            return subprocess.CompletedProcess(
                list(argv),
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

    return run_scoped


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    research_db = (
        Path(args.research_db)
        if args.research_db
        else Path(__file__).resolve().with_name("research-db")
    )
    stop = Event()
    previous: dict[int, Any] = {}
    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            previous[signum] = signal.signal(signum, lambda *_args: stop.set())
        except ValueError:
            break

    try:
        runner = (
            _run_scoped_runner(args.research_run_id)
            if args.research_run_id
            else _default_runner
        )
        result = drain_index_jobs_result(
            research_db,
            batch_size=args.batch_size,
            max_batches=args.max_batches,
            deadline_seconds=args.deadline_seconds,
            initial_backoff_seconds=args.initial_backoff_seconds,
            max_backoff_seconds=args.max_backoff_seconds,
            runner=runner,
            require_census=bool(args.research_run_id),
            waiter=stop.wait,
        )
    except Exception as exc:  # noqa: BLE001
        result = DrainResult(
            status="failed",
            exit_code=1,
            recoverable=False,
            reason=f"setup_failed: {type(exc).__name__}: {exc}",
            batches=0,
            elapsed_seconds=0.0,
        )
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)

    print(json.dumps(result.to_dict(), sort_keys=True, default=str))
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
