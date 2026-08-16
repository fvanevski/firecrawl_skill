"""Connection-bound PostgreSQL audit-assessment persistence for issue #259."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID


class PostgresAuditRepository:
    """Canonical audit assessment/stage persistence and idempotent scheduling."""

    def __init__(self, connection: Any) -> None:
        self.__connection = connection

    def create_audit_assessment(
        self,
        run_id: UUID,
        target_type: str,
        target_id: UUID,
        target_hash: str,
        evaluator_version: str,
        prompt_template_version: str,
        policy_version: str,
        stage_set: list[str],
        status: str,
        *,
        audit_identity_hash: str,
        provider: str | None = None,
        model: str | None = None,
        prompt_hash: str | None = None,
        model_fingerprint: str,
        elapsed_ms: int = 0,
        audit_packet_manifest: dict[str, Any] | None = None,
    ) -> UUID:
        with self.__connection.cursor() as cur:
            cur.execute(
                """INSERT INTO audit_assessments (
                    run_id, target_type, target_id, target_hash,
                    evaluator_version, prompt_template_version, policy_version,
                    stage_set, status, provider, model, prompt_hash,
                    model_fingerprint, elapsed_ms, audit_packet_manifest,
                    audit_identity_hash
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id""",
                (
                    str(run_id),
                    target_type,
                    str(target_id),
                    target_hash,
                    evaluator_version,
                    prompt_template_version,
                    policy_version,
                    stage_set,
                    status,
                    provider,
                    model,
                    prompt_hash,
                    model_fingerprint,
                    elapsed_ms,
                    json.dumps(audit_packet_manifest, sort_keys=True)
                    if audit_packet_manifest
                    else None,
                    audit_identity_hash,
                ),
            )
            return UUID(str(cur.fetchone()[0]))

    def get_audit_assessment(self, assessment_id: UUID) -> dict[str, Any] | None:
        with self.__connection.cursor() as cur:
            cur.execute(
                """SELECT id, run_id, target_type, target_id, target_hash,
                    evaluator_version, prompt_template_version, policy_version,
                    stage_set, status, provider, model, prompt_hash,
                    model_fingerprint, elapsed_ms, audit_packet_manifest,
                    created_at, audit_identity_hash
                FROM audit_assessments WHERE id=%s""",
                (str(assessment_id),),
            )
            row = cur.fetchone()
        return self._row_to_audit_assessment_mapping(row) if row is not None else None

    def list_audit_assessments(
        self,
        run_id: UUID | None = None,
        target_id: UUID | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        conditions, params = [], []
        if run_id is not None:
            conditions.append("aa.run_id = %s")
            params.append(str(run_id))
        if target_id is not None:
            conditions.append("aa.target_id = %s")
            params.append(str(target_id))
        if status is not None:
            conditions.append("aa.status = %s")
            params.append(status)
        where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""
        query = f"""SELECT aa.id, aa.run_id, aa.target_type, aa.target_id,
            aa.target_hash, aa.evaluator_version, aa.prompt_template_version,
            aa.policy_version, aa.stage_set, aa.status, aa.provider, aa.model,
            aa.prompt_hash, aa.model_fingerprint, aa.elapsed_ms,
            aa.audit_packet_manifest, aa.created_at, aa.audit_identity_hash
         FROM audit_assessments aa{where_clause}
         ORDER BY aa.created_at DESC LIMIT %s OFFSET %s"""
        params.extend([limit, offset])
        with self.__connection.cursor() as cur:
            cur.execute(query, params)
            return [
                self._row_to_audit_assessment_mapping(row) for row in cur.fetchall()
            ]

    def detect_stale_assessments(
        self, run_id: UUID, target_type: str, target_id: UUID, current_hash: str
    ) -> list[dict[str, Any]]:
        with self.__connection.cursor() as cur:
            cur.execute(
                """SELECT id, target_hash, status, created_at
                FROM audit_assessments
                WHERE run_id = %s AND target_type = %s AND target_id = %s
                  AND target_hash != %s ORDER BY created_at DESC""",
                (str(run_id), target_type, str(target_id), current_hash),
            )
            return [
                {
                    "id": str(row[0]),
                    "target_hash": row[1],
                    "status": row[2],
                    "created_at": row[3],
                }
                for row in cur.fetchall()
            ]

    def export_audit_assessment(self, assessment_id: UUID) -> dict[str, Any] | None:
        assessment = self.get_audit_assessment(assessment_id)
        if assessment is None:
            return None
        export = dict(assessment)
        export["stages"] = self.list_audit_stage_outputs(assessment_id)
        return export

    def insert_audit_stage_output(
        self,
        assessment_id: UUID,
        stage: str,
        sequence_number: int,
        status: str,
        *,
        output: dict[str, Any] | None = None,
        error: str | None = None,
        error_details: dict[str, Any] | None = None,
        call_count: int = 0,
        used_fallback: bool = False,
    ) -> UUID:
        with self.__connection.cursor() as cur:
            cur.execute(
                """INSERT INTO audit_stage_outputs (
                    assessment_id, stage, sequence_number, status,
                    output, error, error_details, call_count, used_fallback
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (
                    str(assessment_id),
                    stage,
                    sequence_number,
                    status,
                    json.dumps(output, sort_keys=True) if output else None,
                    error,
                    json.dumps(error_details, sort_keys=True)
                    if error_details
                    else None,
                    call_count,
                    used_fallback,
                ),
            )
            return UUID(str(cur.fetchone()[0]))

    def list_audit_stage_outputs(
        self,
        assessment_id: UUID,
        stage: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        conditions, params = ["sa.assessment_id = %s"], [str(assessment_id)]
        if stage is not None:
            conditions.append("sa.stage = %s")
            params.append(stage)
        if status is not None:
            conditions.append("sa.status = %s")
            params.append(status)
        where_clause = " WHERE " + " AND ".join(conditions)
        query = f"""SELECT sa.id, sa.assessment_id, sa.stage,
            sa.sequence_number, sa.status, sa.output, sa.error, sa.error_details,
            sa.call_count, sa.used_fallback, sa.created_at
         FROM audit_stage_outputs sa{where_clause}
         ORDER BY sa.sequence_number ASC LIMIT %s OFFSET %s"""
        params.extend([limit, offset])
        with self.__connection.cursor() as cur:
            cur.execute(query, params)
            return [
                self._row_to_audit_stage_output_mapping(row) for row in cur.fetchall()
            ]

    def validate_assessment_exists(self, assessment_id: UUID) -> bool:
        with self.__connection.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM audit_assessments WHERE id = %s LIMIT 1",
                (str(assessment_id),),
            )
            return cur.fetchone() is not None

    def run_exists(self, run_id: UUID) -> bool:
        with self.__connection.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM research_runs WHERE id = %s LIMIT 1", (str(run_id),)
            )
            return cur.fetchone() is not None

    def invocation_exists(self, invocation_id: UUID) -> bool:
        with self.__connection.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM research_invocations WHERE id = %s LIMIT 1",
                (str(invocation_id),),
            )
            return cur.fetchone() is not None

    def validate_evidence_references(self, references: list[str | UUID]) -> list[str]:
        invalid_references = []
        for ref in references:
            ref_str = str(ref)
            try:
                ref_uuid = UUID(ref_str)
            except (ValueError, AttributeError):
                invalid_references.append(ref_str)
                continue
            with self.__connection.cursor() as cur:
                cur.execute(
                    """SELECT 1 WHERE
                    EXISTS (SELECT 1 FROM research_claims WHERE claim_id = %s) OR
                    EXISTS (SELECT 1 FROM chunks WHERE id = %s) OR
                    EXISTS (SELECT 1 FROM asset_snapshots WHERE id = %s) OR
                    EXISTS (SELECT 1 FROM research_runs WHERE id = %s) OR
                    EXISTS (SELECT 1 FROM research_invocations WHERE id = %s)""",
                    (ref_uuid, ref_uuid, ref_uuid, ref_uuid, ref_uuid),
                )
                if cur.fetchone() is None:
                    invalid_references.append(ref_str)
        return invalid_references

    def validate_audit_target(
        self, run_id: UUID, target_type: str, target_id: UUID
    ) -> bool:
        with self.__connection.cursor() as cur:
            if target_type == "run":
                if target_id != run_id:
                    return False
                cur.execute("SELECT 1 FROM research_runs WHERE id = %s", (str(run_id),))
            elif target_type == "invocation":
                cur.execute(
                    """SELECT 1 FROM research_invocations
                    WHERE id = %s AND run_id = %s""",
                    (str(target_id), str(run_id)),
                )
            else:
                return False
            return cur.fetchone() is not None

    def lookup_equivalent_assessment(
        self,
        run_id: UUID,
        target_type: str,
        target_id: UUID,
        audit_identity_hash: str,
    ) -> dict[str, Any] | None:
        with self.__connection.cursor() as cur:
            cur.execute(
                """SELECT id, run_id, target_type, target_id, target_hash,
                       evaluator_version, prompt_template_version, policy_version,
                       stage_set, status, provider, model, prompt_hash,
                       model_fingerprint, elapsed_ms, audit_packet_manifest,
                       created_at, audit_identity_hash
                FROM audit_assessments
                WHERE run_id = %s AND target_type = %s AND target_id = %s
                  AND audit_identity_hash = %s AND status = 'completed' LIMIT 1""",
                (str(run_id), target_type, str(target_id), audit_identity_hash),
            )
            row = cur.fetchone()
        return self._row_to_audit_assessment_mapping(row) if row is not None else None

    def insert_audit_assessment_if_absent(
        self,
        run_id: UUID,
        target_type: str,
        target_id: UUID,
        target_hash: str,
        evaluator_version: str,
        prompt_template_version: str,
        policy_version: str,
        stage_set: list[str],
        status: str,
        *,
        audit_identity_hash: str,
        provider: str | None = None,
        model: str | None = None,
        prompt_hash: str | None = None,
        model_fingerprint: str,
        elapsed_ms: int = 0,
        audit_packet_manifest: dict[str, Any] | None = None,
    ) -> UUID | None:
        with self.__connection.cursor() as cur:
            cur.execute(
                """INSERT INTO audit_assessments (
                    run_id, target_type, target_id, target_hash,
                    evaluator_version, prompt_template_version, policy_version,
                    stage_set, status, provider, model, prompt_hash,
                    model_fingerprint, elapsed_ms, audit_packet_manifest,
                    audit_identity_hash
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (run_id, target_type, target_id, audit_identity_hash)
                WHERE status = 'completed' DO NOTHING RETURNING id""",
                (
                    str(run_id),
                    target_type,
                    str(target_id),
                    target_hash,
                    evaluator_version,
                    prompt_template_version,
                    policy_version,
                    stage_set,
                    status,
                    provider,
                    model,
                    prompt_hash,
                    model_fingerprint,
                    elapsed_ms,
                    json.dumps(audit_packet_manifest, sort_keys=True)
                    if audit_packet_manifest
                    else None,
                    audit_identity_hash,
                ),
            )
            row = cur.fetchone()
        return UUID(str(row[0])) if row is not None else None

    @staticmethod
    def _row_to_audit_assessment_mapping(row) -> dict[str, Any]:
        keys = (
            "id",
            "run_id",
            "target_type",
            "target_id",
            "target_hash",
            "evaluator_version",
            "prompt_template_version",
            "policy_version",
            "stage_set",
            "status",
            "provider",
            "model",
            "prompt_hash",
            "model_fingerprint",
            "elapsed_ms",
            "audit_packet_manifest",
            "created_at",
            "audit_identity_hash",
        )
        result = dict(zip(keys, row))
        for key in ("id", "run_id", "target_id"):
            if result.get(key) is not None:
                result[key] = str(result[key])
        stage_set = result.get("stage_set")
        if isinstance(stage_set, (list, tuple)):
            result["stage_set"] = tuple(stage_set)
        elif isinstance(stage_set, str):
            result["stage_set"] = (stage_set,)
        return result

    @staticmethod
    def _row_to_audit_stage_output_mapping(row) -> dict[str, Any]:
        keys = (
            "id",
            "assessment_id",
            "stage",
            "sequence_number",
            "status",
            "output",
            "error",
            "error_details",
            "call_count",
            "used_fallback",
            "created_at",
        )
        result = dict(zip(keys, row))
        for key in ("id", "assessment_id"):
            if result.get(key) is not None:
                result[key] = str(result[key])
        return result
