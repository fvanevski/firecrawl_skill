"""Connection-bound semantic, cache, synthesis, and endpoint persistence.

Issue #259 gives the remaining semantic durable-state families explicit
repositories.  Every repository receives the exact UoW-owned PostgreSQL
connection and deliberately has no connection/transaction lifecycle API.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _json_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


class PostgresSemanticCallRepository:
    """Canonical semantic-call/artifact provenance persistence."""

    def __init__(self, connection: Any, telemetry_service: Any = None) -> None:
        self.__connection = connection
        self.__telemetry_service = telemetry_service

    @staticmethod
    def _lock_workflow_run(cur, run_id):
        cur.execute(
            "SELECT state,lifecycle_revision FROM research_runs WHERE id=%s FOR UPDATE",
            (run_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise KeyError(run_id)
        return row

    def record_semantic_call(
        self,
        run_id,
        stage,
        provider,
        model,
        prompt_version,
        request,
        idempotency_key,
        *,
        invocation_id=None,
        model_revision="",
        status="pending",
        expected_revision=None,
        expected_execution_mode=None,
    ):
        with self.__connection.cursor() as cur:
            _state, current_revision = self._lock_workflow_run(cur, run_id)
            cur.execute(
                "SELECT execution_mode FROM research_runs WHERE id=%s", (run_id,)
            )
            current_mode = cur.fetchone()[0]
            if expected_revision is not None and current_revision != expected_revision:
                raise ValueError(
                    "stale semantic decision revision: "
                    f"expected {expected_revision}, current {current_revision}"
                )
            if (
                expected_execution_mode is not None
                and current_mode != expected_execution_mode
            ):
                raise ValueError(
                    "semantic authority changed before persistence: "
                    f"expected {expected_execution_mode}, current {current_mode}"
                )
            digest = _json_sha256(request)
            cur.execute(
                """INSERT INTO semantic_calls(
                run_id,invocation_id,stage,provider,model,model_revision,prompt_version,
                input_sha256,request,status,idempotency_key,started_at)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                  CASE WHEN %s IN ('running','complete','failed','cancelled') THEN now() END)
                ON CONFLICT(run_id,idempotency_key) DO UPDATE
                  SET idempotency_key=excluded.idempotency_key
                RETURNING id,invocation_id,stage,provider,model,model_revision,
                  prompt_version,input_sha256,status""",
                (
                    run_id,
                    invocation_id,
                    stage,
                    provider,
                    model,
                    model_revision,
                    prompt_version,
                    digest,
                    _canonical_json(request),
                    status,
                    idempotency_key,
                    status,
                ),
            )
            row = cur.fetchone()
            expected = (
                invocation_id,
                stage,
                provider,
                model,
                model_revision,
                prompt_version,
                digest,
                status,
            )
            if row[1:] != expected:
                raise ValueError("idempotency key was used for another semantic call")
            return row[0]

    def finalize_semantic_call(
        self, run_id, call_id, status, response_metadata, error=None
    ):
        if status not in {"complete", "failed", "cancelled"}:
            raise ValueError(
                "semantic call final status must be complete, failed, or cancelled"
            )
        response_json = _canonical_json(response_metadata or {})
        with self.__connection.cursor() as cur:
            self._lock_workflow_run(cur, run_id)
            cur.execute(
                """SELECT status,response_metadata,error FROM semantic_calls
                WHERE id=%s AND run_id=%s FOR UPDATE""",
                (call_id, run_id),
            )
            existing = cur.fetchone()
            if existing is None:
                raise ValueError("semantic call does not belong to the research run")
            expected = (status, json.loads(response_json), error)
            if existing[0] in {"complete", "failed", "cancelled"}:
                if existing != expected:
                    raise ValueError("semantic call was already finalized differently")
                return call_id
            cur.execute(
                """UPDATE semantic_calls SET status=%s,response_metadata=%s,error=%s,
                completed_at=now(),started_at=COALESCE(started_at,created_at)
                WHERE id=%s AND run_id=%s RETURNING id""",
                (status, response_json, error, call_id, run_id),
            )
            cur.fetchone()
        self._record_endpoint_telemetry(run_id, call_id, response_metadata)
        return call_id

    def _record_endpoint_telemetry(self, run_id, call_id, response_metadata):
        if self.__telemetry_service is None:
            return
        try:
            from firecrawl_skill.research_store.telemetry_service import (
                EndpointUsageRecord,
            )
            from firecrawl_skill.research_store.token_accounting import (
                extract_endpoint_usage,
            )

            accounting = extract_endpoint_usage(response_metadata or {})
            if accounting.source == "unavailable":
                return
            record = EndpointUsageRecord(
                run_id=str(run_id),
                call_id=str(call_id),
                endpoint_type="generative",
                provider=accounting.prompt_tokens and "openai-compatible",
                model="",
                model_revision="",
                prompt_tokens=accounting.prompt_tokens or 0,
                completion_tokens=accounting.completion_tokens or 0,
                total_tokens=accounting.total_tokens or 0,
                source=accounting.source,
            )
            self.__telemetry_service.record_endpoint_usage(record)
        except Exception:  # noqa: BLE001, S110
            pass

    def annotate_semantic_call(self, run_id, call_id, metadata):
        with self.__connection.cursor() as cur:
            self._lock_workflow_run(cur, run_id)
            cur.execute(
                """UPDATE semantic_calls
                SET response_metadata=response_metadata || %s::jsonb
                WHERE id=%s AND run_id=%s RETURNING id""",
                (_canonical_json(metadata or {}), call_id, run_id),
            )
            row = cur.fetchone()
            if row is None:
                raise ValueError("semantic call does not belong to the research run")
            return row[0]

    def get_semantic_call(self, run_id, call_id):
        with self.__connection.cursor() as cur:
            cur.execute(
                """SELECT id,run_id,invocation_id,stage,provider,model,model_revision,
                prompt_version,input_sha256,request,response_metadata,status,error,
                started_at,completed_at,created_at FROM semantic_calls
                WHERE id=%s AND run_id=%s""",
                (call_id, run_id),
            )
            row = cur.fetchone()
            if row is None:
                raise ValueError("semantic call does not belong to the research run")
            keys = (
                "id",
                "run_id",
                "invocation_id",
                "stage",
                "provider",
                "model",
                "model_revision",
                "prompt_version",
                "input_sha256",
                "request",
                "response_metadata",
                "status",
                "error",
                "started_at",
                "completed_at",
                "created_at",
            )
            result = dict(zip(keys, row))
            cur.execute(
                """SELECT id,artifact_type,schema_name,schema_version,payload,
                content_sha256,validation_status,validation_errors,created_at
                FROM semantic_artifacts WHERE semantic_call_id=%s AND run_id=%s
                ORDER BY created_at,id""",
                (call_id, run_id),
            )
            artifact_keys = (
                "id",
                "artifact_type",
                "schema_name",
                "schema_version",
                "payload",
                "content_sha256",
                "validation_status",
                "validation_errors",
                "created_at",
            )
            result["artifacts"] = [
                dict(zip(artifact_keys, item)) for item in cur.fetchall()
            ]
            return result

    def record_semantic_artifact(
        self,
        run_id,
        semantic_call_id,
        artifact_type,
        schema_name,
        schema_version,
        payload,
        idempotency_key,
        *,
        validation_status="valid",
        validation_errors=None,
    ):
        with self.__connection.cursor() as cur:
            self._lock_workflow_run(cur, run_id)
            digest = _json_sha256(payload)
            cur.execute(
                """INSERT INTO semantic_artifacts(
                run_id,semantic_call_id,artifact_type,schema_name,schema_version,payload,
                content_sha256,validation_status,validation_errors,idempotency_key)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(semantic_call_id,idempotency_key) DO UPDATE
                  SET idempotency_key=excluded.idempotency_key
                RETURNING id,artifact_type,schema_name,schema_version,content_sha256,
                  validation_status,validation_errors""",
                (
                    run_id,
                    semantic_call_id,
                    artifact_type,
                    schema_name,
                    schema_version,
                    _canonical_json(payload),
                    digest,
                    validation_status,
                    _canonical_json(validation_errors or []),
                    idempotency_key,
                ),
            )
            row = cur.fetchone()
            expected = (
                artifact_type,
                schema_name,
                schema_version,
                digest,
                validation_status,
                validation_errors or [],
            )
            if row[1:] != expected:
                raise ValueError(
                    "idempotency key was used for another semantic artifact"
                )
            return row[0]


class PostgresSynthesisStageRepository:
    """Canonical synthesis-stage persistence."""

    def __init__(self, connection: Any) -> None:
        self.__connection = connection

    @staticmethod
    def _keys():
        return (
            "id",
            "run_id",
            "stage_name",
            "stage_status",
            "semantic_call_id",
            "semantic_artifact_id",
            "evidence_packet_revision",
            "model_name",
            "prompt_version",
            "schema_version",
            "artifact",
            "error",
            "attempts",
            "created_at",
            "updated_at",
        )

    def get_synthesis_stages(self, run_id: UUID) -> list[dict[str, Any]]:
        with self.__connection.cursor() as cur:
            cur.execute(
                """SELECT id, run_id, stage_name, stage_status,
                          semantic_call_id, semantic_artifact_id,
                          evidence_packet_revision, model_name,
                          prompt_version, schema_version,
                          artifact, error, attempts, created_at, updated_at
                   FROM synthesis_stages WHERE run_id=%s ORDER BY stage_name""",
                (str(run_id),),
            )
            return [dict(zip(self._keys(), row)) for row in cur.fetchall()]

    def get_synthesis_stage(self, run_id: UUID, stage_name: str) -> dict[str, Any]:
        with self.__connection.cursor() as cur:
            cur.execute(
                """SELECT id, run_id, stage_name, stage_status,
                          semantic_call_id, semantic_artifact_id,
                          evidence_packet_revision, model_name,
                          prompt_version, schema_version,
                          artifact, error, attempts, created_at, updated_at
                   FROM synthesis_stages WHERE run_id=%s AND stage_name=%s""",
                (str(run_id), stage_name),
            )
            row = cur.fetchone()
        if row is None:
            raise KeyError((run_id, stage_name))
        return dict(zip(self._keys(), row))

    def insert_synthesis_stage(self, record: dict[str, Any]) -> None:
        with self.__connection.cursor() as cur:
            cur.execute(
                """INSERT INTO synthesis_stages
                   (id, run_id, stage_name, stage_status,
                    semantic_call_id, semantic_artifact_id,
                    evidence_packet_revision, model_name,
                    prompt_version, schema_version,
                    artifact, error, attempts, created_at, updated_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    str(record["id"]),
                    str(record["run_id"]),
                    record["stage_name"],
                    record["stage_status"],
                    str(record["semantic_call_id"])
                    if record.get("semantic_call_id")
                    else None,
                    str(record["semantic_artifact_id"])
                    if record.get("semantic_artifact_id")
                    else None,
                    record["evidence_packet_revision"],
                    record["model_name"],
                    record["prompt_version"],
                    record["schema_version"],
                    json.dumps(record["artifact"], default=str)
                    if record.get("artifact")
                    else None,
                    record.get("error"),
                    record["attempts"],
                    record["created_at"],
                    record["updated_at"],
                ),
            )

    def update_synthesis_stage(self, record: dict[str, Any]) -> None:
        with self.__connection.cursor() as cur:
            cur.execute(
                """UPDATE synthesis_stages SET
                   stage_status=%s,
                   semantic_call_id=COALESCE(%s, semantic_call_id),
                   semantic_artifact_id=COALESCE(%s, semantic_artifact_id),
                   artifact=COALESCE(%s, artifact),
                   error=%s, attempts=GREATEST(attempts, %s), updated_at=now()
                   WHERE run_id=%s AND stage_name=%s RETURNING id""",
                (
                    record["stage_status"],
                    str(record["semantic_call_id"])
                    if record.get("semantic_call_id")
                    else None,
                    str(record["semantic_artifact_id"])
                    if record.get("semantic_artifact_id")
                    else None,
                    json.dumps(record["artifact"], default=str)
                    if record.get("artifact")
                    else None,
                    record.get("error"),
                    record["attempts"],
                    str(record["run_id"]),
                    record["stage_name"],
                ),
            )
            if cur.fetchone() is None:
                raise KeyError((record["run_id"], record["stage_name"]))


class PostgresSemanticCacheRepository:
    """Canonical semantic-cache persistence."""

    def __init__(self, connection: Any) -> None:
        self.__connection = connection

    def get_cache_entry_by_key(self, key_hash: str) -> dict[str, Any] | None:
        with self.__connection.cursor() as cur:
            cur.execute(
                """SELECT id, key_hash, stage, model_fingerprint, input_hash,
                          prompt_hash, prompt_version, schema_version,
                          policy_version, configuration_hash,
                          artifact, provenance, status, ttl_seconds, created_at
                   FROM semantic_cache WHERE key_hash=%s""",
                (key_hash,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        keys = (
            "id",
            "key_hash",
            "stage",
            "model_fingerprint",
            "input_hash",
            "prompt_hash",
            "prompt_version",
            "schema_version",
            "policy_version",
            "configuration_hash",
            "artifact",
            "provenance",
            "status",
            "ttl_seconds",
            "created_at",
        )
        return dict(zip(keys, row))

    def insert_cache_entry(self, record: dict[str, Any]) -> None:
        with self.__connection.cursor() as cur:
            cur.execute(
                """INSERT INTO semantic_cache
                   (id, key_hash, stage, model_fingerprint, input_hash,
                    prompt_hash, prompt_version, schema_version,
                    policy_version, configuration_hash,
                    artifact, provenance, status, ttl_seconds, created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    str(record["id"]),
                    record["key_hash"],
                    record["stage"],
                    record["model_fingerprint"],
                    record["input_hash"],
                    record["prompt_hash"],
                    record["prompt_version"],
                    record["schema_version"],
                    record.get("policy_version"),
                    record.get("configuration_hash"),
                    json.dumps(record["artifact"], default=str)
                    if record.get("artifact")
                    else None,
                    json.dumps(record["provenance"], default=str)
                    if record.get("provenance")
                    else None,
                    record["status"],
                    record["ttl_seconds"],
                    record["created_at"],
                ),
            )

    def prune_cache_entries(
        self, *, older_than_seconds: int | None = None, ttl_seconds: int = 3600
    ) -> int:
        del ttl_seconds  # Per-row TTL is authoritative in the default path.
        with self.__connection.cursor() as cur:
            if older_than_seconds is not None:
                cur.execute(
                    """DELETE FROM semantic_cache
                       WHERE created_at < (extract(epoch from now()) - %s)
                       RETURNING id""",
                    (older_than_seconds,),
                )
            else:
                cur.execute(
                    """DELETE FROM semantic_cache WHERE status = 'valid'
                       AND (extract(epoch from now()) - created_at) > ttl_seconds
                       RETURNING id"""
                )
            return cur.rowcount

    def invalidate_cache_entry(self, key_hash: str) -> int:
        with self.__connection.cursor() as cur:
            cur.execute(
                """UPDATE semantic_cache SET status = 'pruned'
                   WHERE key_hash = %s AND status = 'valid' RETURNING id""",
                (key_hash,),
            )
            return cur.rowcount

    def invalidate_cache_entry_by_id(self, entry_id: UUID) -> int:
        with self.__connection.cursor() as cur:
            cur.execute(
                """UPDATE semantic_cache SET status = 'pruned'
                   WHERE id = %s AND status = 'valid' RETURNING id""",
                (str(entry_id),),
            )
            return cur.rowcount

    def update_cache_entry(self, record: dict[str, Any]) -> int:
        with self.__connection.cursor() as cur:
            cur.execute(
                """UPDATE semantic_cache SET artifact = %s, provenance = %s,
                       status = %s, ttl_seconds = %s, created_at = %s
                   WHERE key_hash = %s RETURNING id""",
                (
                    json.dumps(record["artifact"], default=str)
                    if record.get("artifact")
                    else None,
                    json.dumps(record["provenance"], default=str)
                    if record.get("provenance")
                    else None,
                    record["status"],
                    record["ttl_seconds"],
                    record["created_at"],
                    record["key_hash"],
                ),
            )
            return cur.rowcount


class PostgresModelEndpointRepository:
    """Canonical model-endpoint health persistence."""

    def __init__(self, connection: Any) -> None:
        self.__connection = connection

    def upsert_health(
        self,
        endpoint_name: str,
        *,
        url: str,
        status: str,
        last_check_at: float | None,
        last_error: str | None,
        concurrent_requests: int,
        queued_requests: int,
        total_checks: int,
        total_failures: int,
        degraded_since: float | None,
        restart_count: int,
    ) -> None:
        with self.__connection.cursor() as cur:
            cur.execute(
                """INSERT INTO model_endpoints
                     (endpoint_name, url, status, last_check_at, last_error,
                      concurrent_requests, queued_requests, total_checks,
                      total_failures, degraded_since, restart_count)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (endpoint_name) DO UPDATE SET
                     url = EXCLUDED.url, status = EXCLUDED.status,
                     last_check_at = EXCLUDED.last_check_at,
                     last_error = EXCLUDED.last_error,
                     concurrent_requests = EXCLUDED.concurrent_requests,
                     queued_requests = EXCLUDED.queued_requests,
                     total_checks = EXCLUDED.total_checks,
                     total_failures = EXCLUDED.total_failures,
                     degraded_since = EXCLUDED.degraded_since,
                     restart_count = EXCLUDED.restart_count""",
                (
                    endpoint_name,
                    url,
                    status,
                    last_check_at,
                    last_error,
                    concurrent_requests,
                    queued_requests,
                    total_checks,
                    total_failures,
                    degraded_since,
                    restart_count,
                ),
            )

    def get_health(self, endpoint_name: str) -> dict[str, Any] | None:
        with self.__connection.cursor() as cur:
            cur.execute(
                """SELECT endpoint_name, url, status, last_check_at,
                          last_error, concurrent_requests, queued_requests,
                          total_checks, total_failures, degraded_since, restart_count
                   FROM model_endpoints WHERE endpoint_name = %s""",
                (endpoint_name,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        keys = (
            "endpoint_name",
            "url",
            "status",
            "last_check_at",
            "last_error",
            "concurrent_requests",
            "queued_requests",
            "total_checks",
            "total_failures",
            "degraded_since",
            "restart_count",
        )
        return dict(zip(keys, row))

    def list_endpoints(self) -> list[dict[str, Any]]:
        with self.__connection.cursor() as cur:
            cur.execute(
                """SELECT endpoint_name, url, status, last_check_at,
                          last_error, concurrent_requests, queued_requests,
                          total_checks, total_failures, degraded_since, restart_count
                   FROM model_endpoints ORDER BY endpoint_name"""
            )
            rows = cur.fetchall()
        keys = (
            "endpoint_name",
            "url",
            "status",
            "last_check_at",
            "last_error",
            "concurrent_requests",
            "queued_requests",
            "total_checks",
            "total_failures",
            "degraded_since",
            "restart_count",
        )
        return [dict(zip(keys, row)) for row in rows]

    def clear_endpoint_health(self, endpoint_name: str) -> int:
        with self.__connection.cursor() as cur:
            cur.execute(
                "DELETE FROM model_endpoints WHERE endpoint_name = %s", (endpoint_name,)
            )
            return cur.rowcount
