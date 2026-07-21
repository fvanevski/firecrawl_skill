from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import subprocess
from typing import Any, Callable
from uuid import UUID

from .blob import ContentAddressedBlobStore
from .domain import SearchAdapterResult, utcnow
from .ports import SearchAdapter


class FirecrawlSearchAdapter:
    """Wraps Firecrawl CLI or runner to execute search queries and classify transport errors."""

    def __init__(self, runner: Callable[..., tuple[int, bytes, str]] | None = None):
        self.runner = runner or self._default_runner

    @staticmethod
    def _default_runner(cmd: list[str], timeout: int = 60) -> tuple[int, bytes, str]:
        try:
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
            )
            return proc.returncode, proc.stdout, proc.stderr.decode("utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            return -1, b"", "ETIMEDOUT: Firecrawl search process timed out"
        except Exception as exc:
            return -1, b"", f"Transport error: {type(exc).__name__}: {exc}"

    def search(
        self,
        query_text: str,
        *,
        backend: str = "firecrawl",
        limit: int = 20,
        sources: str = "web",
        tbs: str | None = None,
        retries: int = 2,
        **kwargs: Any,
    ) -> SearchAdapterResult:
        if not query_text.strip():
            raise ValueError("query_text must be non-empty")

        cmd = [
            "firecrawl",
            "search",
            query_text,
            "--limit",
            str(limit),
            "--sources",
            sources,
            "--ignore-invalid-urls",
            "--json",
        ]
        if tbs:
            cmd.extend(["--tbs", tbs])

        requested_at = utcnow()
        attempt = 0
        last_stderr = ""
        last_code = 0
        stdout_data = b""

        while attempt <= retries:
            code, stdout, stderr = self.runner(cmd)
            responded_at = utcnow()
            last_code = code
            last_stderr = stderr
            stdout_data = stdout

            if code == 0 and stdout:
                return SearchAdapterResult(
                    raw_payload=stdout,
                    http_status=200,
                    provider_request_id=None,
                    transport_error=None,
                    transport_metadata={
                        "attempt": attempt + 1,
                        "cmd": cmd,
                        "exit_code": code,
                    },
                    requested_at=requested_at,
                    responded_at=responded_at,
                )

            is_transient = any(
                err_code in stderr
                for err_code in ("EAI_AGAIN", "ENOTFOUND", "ECONNRESET", "ETIMEDOUT")
            )
            if not is_transient or attempt >= retries:
                break
            attempt += 1

        transport_err = None
        if last_code != 0 or not stdout_data:
            for err_tag in ("EAI_AGAIN", "ENOTFOUND", "ECONNRESET", "ETIMEDOUT"):
                if err_tag in last_stderr:
                    transport_err = f"Network transport error: {err_tag}"
                    break
            if not transport_err:
                if last_stderr.strip():
                    transport_err = f"Firecrawl search failed (exit {last_code}): {last_stderr.strip()[:300]}"
                else:
                    transport_err = f"Firecrawl search failed with exit code {last_code}"

        payload = (
            stdout_data
            if (stdout_data and last_code == 0)
            else json.dumps(
                {
                    "success": False,
                    "error": transport_err or "Empty response from search provider",
                }
            ).encode("utf-8")
        )

        return SearchAdapterResult(
            raw_payload=payload,
            http_status=500 if transport_err else None,
            provider_request_id=None,
            transport_error=transport_err,
            transport_metadata={
                "attempts": attempt + 1,
                "cmd": cmd,
                "exit_code": last_code,
                "stderr": last_stderr[:500],
            },
            requested_at=requested_at,
            responded_at=responded_at,
        )


@dataclass(frozen=True)
class AcquisitionResult:
    search_response_id: UUID
    run_id: UUID
    query_text: str
    backend: str
    status: str
    candidate_count: int
    candidates: list[dict[str, Any]]
    postgres_committed: bool
    scratch_exported: bool
    event_id: UUID | None = None
    scratch_error: str | None = None
    search_response: dict[str, Any] = field(default_factory=dict)


class AcquisitionService:
    """Service boundary for executing search acquisition and persisting results transactionally."""

    def __init__(
        self,
        uow_factory: Callable,
        blob_store: Any | None = None,
        search_adapter: SearchAdapter | None = None,
    ):
        self.uow_factory = uow_factory
        self.blob_store = blob_store
        self.search_adapter = search_adapter or FirecrawlSearchAdapter()

    def execute_search(
        self,
        run_id: UUID,
        query_text: str,
        *,
        backend: str = "firecrawl",
        plan_id: UUID | None = None,
        plan_query_id: UUID | None = None,
        idempotency_key: str | None = None,
        limit: int = 20,
        sources: str = "web",
        tbs: str | None = None,
        scratch_dir: Path | str | None = None,
        export_scratch: bool = True,
        metadata: dict[str, Any] | None = None,
    ) -> AcquisitionResult:
        run_id = UUID(str(run_id))
        if plan_id is not None:
            plan_id = UUID(str(plan_id))
        if plan_query_id is not None:
            plan_query_id = UUID(str(plan_query_id))

        if not query_text.strip():
            raise ValueError("query_text must be non-empty")

        key = idempotency_key or f"search:{run_id}:{plan_query_id or query_text}"

        adapter_result = self.search_adapter.search(
            query_text,
            backend=backend,
            limit=limit,
            sources=sources,
            tbs=tbs,
        )

        store = self.blob_store
        if store is None:
            store = ContentAddressedBlobStore(
                Path(os.environ.get("BLOB_ROOT", "data/blobs"))
            )

        postgres_committed = False
        event_id = None
        candidates = []
        resp_data = {}

        with self.uow_factory() as uow:
            resp_data = uow.runs.record_search_response(
                run_id,
                query_text,
                backend,
                adapter_result.raw_payload,
                key,
                store,
                plan_id=plan_id,
                plan_query_id=plan_query_id,
                provider_request_id=adapter_result.provider_request_id,
                http_status=adapter_result.http_status,
                error_message=adapter_result.transport_error,
                requested_at=adapter_result.requested_at,
                responded_at=adapter_result.responded_at,
                transport_metadata=adapter_result.transport_metadata,
                **(metadata or {}),
            )

            resp_id = resp_data["id"]

            if resp_data["status"] in ("succeeded", "empty"):
                candidates = uow.runs.record_response_candidates(
                    run_id,
                    resp_id,
                    store,
                    plan_id=plan_id,
                    plan_query_id=plan_query_id,
                )

            event_id = uow.runs.append_event(
                run_id,
                "acquisition.search_executed",
                "system",
                f"event:{key}",
                payload={
                    "search_response_id": str(resp_id),
                    "query_text": query_text,
                    "backend": backend,
                    "status": resp_data["status"],
                    "candidate_count": len(candidates),
                    "idempotency_key": key,
                },
            )

            uow.commit()
            postgres_committed = True

        scratch_exported = False
        scratch_err = None

        if export_scratch and scratch_dir:
            try:
                self._export_scratch_artifacts(
                    Path(scratch_dir),
                    query_text,
                    adapter_result.raw_payload,
                    candidates,
                    resp_data,
                )
                scratch_exported = True
            except Exception as exc:
                scratch_err = f"{type(exc).__name__}: {exc}"

        return AcquisitionResult(
            search_response_id=resp_data["id"],
            run_id=run_id,
            query_text=query_text,
            backend=backend,
            status=resp_data["status"],
            candidate_count=len(candidates),
            candidates=candidates,
            postgres_committed=postgres_committed,
            scratch_exported=scratch_exported,
            event_id=event_id,
            scratch_error=scratch_err,
            search_response=resp_data,
        )

    def reconcile_pending_searches(self, run_id: UUID) -> list[dict[str, Any]]:
        """Reconcile search responses for a run to ensure candidates are extracted without duplicates."""
        run_id = UUID(str(run_id))
        reconciled = []
        store = self.blob_store
        if store is None:
            store = ContentAddressedBlobStore(
                Path(os.environ.get("BLOB_ROOT", "data/blobs"))
            )

        with self.uow_factory() as uow:
            responses = uow.runs.list_search_responses(run_id)
            for resp in responses:
                if resp["status"] in ("succeeded", "empty"):
                    cands = uow.runs.record_response_candidates(
                        run_id,
                        resp["id"],
                        store,
                        plan_id=resp.get("plan_id"),
                        plan_query_id=resp.get("plan_query_id"),
                    )
                    reconciled.append(
                        {
                            "search_response_id": resp["id"],
                            "candidate_count": len(cands),
                            "status": resp["status"],
                        }
                    )
            uow.commit()
        return reconciled

    def _export_scratch_artifacts(
        self,
        target_dir: Path,
        query_text: str,
        raw_payload: bytes,
        candidates: list[dict[str, Any]],
        resp_data: dict[str, Any],
    ) -> None:
        target_dir.mkdir(parents=True, exist_ok=True)

        search_json_path = target_dir / "_search.json"
        search_json_path.write_bytes(raw_payload)

        created_at_val = resp_data.get("created_at")
        created_at_str = (
            created_at_val.isoformat()
            if hasattr(created_at_val, "isoformat")
            else str(created_at_val)
        )

        meta_json_path = target_dir / "_meta.json"
        meta_content = {
            "query": query_text,
            "search_response_id": str(resp_data["id"]),
            "run_id": str(resp_data["run_id"]),
            "backend": resp_data["backend"],
            "status": resp_data["status"],
            "candidate_count": len(candidates),
            "created_at": created_at_str,
        }
        meta_json_path.write_text(json.dumps(meta_content, indent=2), encoding="utf-8")
