"""Supported post-seal EvidencePacket and synthesis path for curated runs."""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any
from uuid import UUID

from firecrawl_skill import model_gateway

from .coverage_seed_service import CompleteCoverageService
from .evidence_preparation_service import EvidencePreparationError
from .reporting.construction import LocalSynthesisService, ReportServiceError


class CuratedSynthesisError(RuntimeError):
    """Curated evidence/synthesis authority could not be established."""


class AuthorityAlignedLocalSynthesisService(LocalSynthesisService):
    """Make curated stage persistence compatible with terminal provenance.

    Host-agent semantic calls intentionally persist an empty model identity, so
    durable stage rows use that same identity.  Curated terminal-grade synthesis
    also bypasses the cross-run semantic cache: a cache hit can reproduce an
    artifact but cannot supply the current run-local semantic call/artifact IDs
    required by ``completion_provenance.py``.  This path therefore prefers
    provenance completeness over cache reuse.
    """

    _active_execution_mode: ContextVar[str | None] = ContextVar(
        "curated_synthesis_execution_mode", default=None
    )

    def run_synthesis(
        self,
        run_id: UUID,
        packet_revision: int,
        model_name: str | None = None,
        prompt_version: str = "synthesis-v1",
        allow_commercial_fallback: bool = False,
    ) -> dict[str, Any]:
        with self.semantic.uow_factory() as uow:
            status = uow.runs.get_run_status(run_id=run_id)
        token = self._active_execution_mode.set(str(status.get("execution_mode") or ""))
        try:
            return super().run_synthesis(
                run_id,
                packet_revision,
                model_name=model_name,
                prompt_version=prompt_version,
                allow_commercial_fallback=allow_commercial_fallback,
            )
        finally:
            self._active_execution_mode.reset(token)

    def _bounded_llm_call(
        self, call_fn: Any, *, input_tokens: int = 0, batch_size: int = 1
    ) -> Any:
        if self._active_execution_mode.get() == "agent_led":
            return call_fn()
        return super()._bounded_llm_call(
            call_fn, input_tokens=input_tokens, batch_size=batch_size
        )

    def _check_cache(self, **_kwargs: Any) -> None:
        return None

    def _write_cache(self, **_kwargs: Any) -> None:
        return None

    def _init_stages(
        self,
        uow: Any,
        run_id: UUID,
        packet_revision: int,
        model_name: str,
        prompt_version: str,
        schema_version: int,
    ) -> dict[str, dict[str, Any]]:
        status = uow.runs.get_run_status(run_id=run_id)
        persisted_model = (
            "" if status.get("execution_mode") == "agent_led" else model_name
        )
        return super()._init_stages(
            uow,
            run_id,
            packet_revision,
            persisted_model,
            prompt_version,
            schema_version,
        )


class CuratedSynthesisService:
    """Compose canonical evidence preparation and five-stage synthesis authority.

    ``frun synthesize`` owns the legal ``coverage_review -> synthesizing ->
    validating`` lifecycle path but deliberately does not terminalize the run.
    ``frun finish`` remains the independent completion-provenance gate.
    """

    POLICY_VERSION = "curated-synthesis-v1"
    PROMPT_VERSION = "synthesis-v1"
    _ALLOWED_STATES = frozenset({"coverage_review", "synthesizing", "validating"})

    def __init__(
        self,
        *,
        config: Any,
        run_service: Any,
        promotion_service: Any,
        evidence_preparation_service: Any,
        synthesis_service: Any,
        coverage_service: CompleteCoverageService,
    ) -> None:
        self.config = config
        self.run_service = run_service
        self.promotion = promotion_service
        self.evidence_preparation = evidence_preparation_service
        self.synthesis = synthesis_service
        self.coverage = coverage_service
        self.uow_factory = run_service.uow_factory

    def synthesize(self, external_run_id: str) -> dict[str, Any]:
        status = self._status(external_run_id)
        if status.state == "indexing":
            raise CuratedSynthesisError(
                f"run {external_run_id} is still indexing; run 'frun resume "
                f"{external_run_id}' before synthesis"
            )
        if status.state not in self._ALLOWED_STATES:
            raise CuratedSynthesisError(
                f"run {external_run_id} cannot synthesize from {status.state}; "
                "seal acquisition and complete indexing first"
            )

        readiness = self._preflight_semantic(status)
        seal = self.promotion.get_active_seal(status.id)
        if seal is None or not seal.members:
            raise CuratedSynthesisError(
                f"run {external_run_id} has no non-empty active completion membership seal"
            )

        spec_record = self._research_spec(status.id)
        spec = dict(spec_record["payload"])
        research_spec_id = self._database_research_spec_id(spec_record)
        coverage_items = self._coverage_items(status.id, spec, status.execution_mode)
        extracted_assets = self._sealed_assets(status.id, seal)

        status = self._ensure_synthesizing(external_run_id, status)

        packet_revision, evidence_mode = self._current_packet(
            status.id,
            research_spec_id=research_spec_id,
            seal=seal,
        )
        if packet_revision is None:
            if status.state == "validating":
                raise CuratedSynthesisError(
                    "current validating run has stale or unverified evidence; reopen "
                    "the run before rebuilding authoritative synthesis provenance"
                )
            try:
                prepared = self.evidence_preparation.prepare(
                    run_id=status.id,
                    run_revision=status.lifecycle_revision,
                    spec=spec,
                    research_spec_id=research_spec_id,
                    coverage_revision=self.coverage.get_current_revision(status.id),
                    extracted_assets=extracted_assets,
                    coverage_items=coverage_items,
                )
            except EvidencePreparationError as exc:
                raise CuratedSynthesisError(
                    f"authoritative evidence preparation failed: {exc}; retry with "
                    f"'frun synthesize {external_run_id}'"
                ) from exc
            packet_revision = prepared.packet_revision
            evidence_mode = "prepared"
            self._record_prepared_packet(
                status.id,
                packet_revision=packet_revision,
                research_spec_id=research_spec_id,
                membership_sha256=seal.membership_sha256,
            )

        stale_reset_count = self._reset_stale_stages(
            status.id,
            packet_revision=packet_revision,
            model_name=self._stage_model_name(status),
        )
        try:
            synthesis = self.synthesis.run_synthesis(
                run_id=status.id,
                packet_revision=packet_revision,
                model_name=self.config.generative_model,
                prompt_version=self.PROMPT_VERSION,
                allow_commercial_fallback=False,
            )
        except ReportServiceError as exc:
            raise CuratedSynthesisError(
                f"curated synthesis failed: {exc}; retry with "
                f"'frun synthesize {external_run_id}'"
            ) from exc

        completed = synthesis.get("overall_status") == "completed"
        if completed:
            status = self._ensure_validating(
                external_run_id, self._status(external_run_id)
            )
        else:
            status = self._status(external_run_id)
        next_action = (
            f"frun finish {external_run_id} --outcome satisfied"
            if completed and status.state == "validating"
            else f"frun synthesize {external_run_id}"
        )
        return {
            "schema_version": "curated-synthesis-result-v1",
            "run_id": external_run_id,
            "internal_run_id": str(status.id),
            "state": status.state,
            "lifecycle_revision": status.lifecycle_revision,
            "semantic_readiness": readiness,
            "membership_sha256": seal.membership_sha256,
            "evidence": {
                "mode": evidence_mode,
                "packet_revision": packet_revision,
                "research_spec_id": str(research_spec_id),
            },
            "stale_stage_reset_count": stale_reset_count,
            "synthesis": synthesis,
            "finished": False,
            "next_action": next_action,
        }

    def _status(self, external_run_id: str) -> Any:
        try:
            return self.run_service.status(external_id=external_run_id)
        except KeyError as exc:
            raise CuratedSynthesisError(
                f"research run {external_run_id!r} was not found"
            ) from exc

    def _ensure_synthesizing(self, external_run_id: str, status: Any) -> Any:
        if status.state != "coverage_review":
            return status
        self.run_service.transition(
            status.id,
            "synthesizing",
            expected_revision=status.lifecycle_revision,
            idempotency_key=f"curated:synthesize:{external_run_id}:enter",
            actor_type="operator",
            actor_identifier="frun",
            reason="operator requested authoritative curated synthesis",
        )
        return self._status(external_run_id)

    def _ensure_validating(self, external_run_id: str, status: Any) -> Any:
        if status.state == "validating":
            return status
        if status.state != "synthesizing":
            raise CuratedSynthesisError(
                f"completed synthesis cannot enter validation from {status.state}"
            )
        self.run_service.transition(
            status.id,
            "validating",
            expected_revision=status.lifecycle_revision,
            idempotency_key=f"curated:synthesize:{external_run_id}:validated",
            actor_type="operator",
            actor_identifier="frun",
            reason="all five authoritative synthesis stages completed",
        )
        return self._status(external_run_id)

    def _stage_model_name(self, status: Any) -> str:
        return (
            ""
            if status.execution_mode == "agent_led"
            else str(self.config.generative_model or "")
        )

    def _preflight_semantic(self, status: Any) -> dict[str, Any]:
        mode = status.execution_mode
        if mode == "deterministic_debug":
            raise CuratedSynthesisError(
                "deterministic_debug fixtures are not authoritative for production "
                "curated synthesis"
            )
        if mode == "agent_led":
            supplier = self.evidence_preparation.semantic.host_artifact_supplier
            if supplier is None:
                raise CuratedSynthesisError(
                    "agent_led curated synthesis requires an explicit HostArtifactSupplier"
                )
            return {"status": "available", "authority": "host-agent"}
        if mode != "autonomous_local":
            raise CuratedSynthesisError(f"unsupported execution mode: {mode}")
        base_url = str(self.config.generative_url or "").rstrip("/")
        model = str(self.config.generative_model or "").strip()
        if not base_url or not model:
            raise CuratedSynthesisError(
                "autonomous_local curated synthesis requires GENERATIVE_URL and "
                "GENERATIVE_MODEL"
            )
        probe = model_gateway.probe_local(base_url, self.config.generative_api_key)
        if probe.get("status") != "available":
            raise CuratedSynthesisError(
                "local generative endpoint is unavailable before semantic work: "
                f"{probe.get('error') or 'readiness probe failed'}"
            )
        models = [str(value) for value in probe.get("models", ()) if value]
        if models and model not in models:
            raise CuratedSynthesisError(
                f"configured generative model {model!r} is not advertised by the "
                "local endpoint"
            )
        return {
            "status": "available",
            "authority": "local-model",
            "model": model,
            "advertised_models": models,
        }

    def _research_spec(self, run_id: UUID) -> dict[str, Any]:
        with self.uow_factory() as uow:
            record = uow.runs.get_research_spec(run_id)
        if record is None or not isinstance(record.get("payload"), dict):
            raise CuratedSynthesisError("current ResearchSpec is unavailable")
        return record

    @staticmethod
    def _database_research_spec_id(spec_record: dict[str, Any]) -> UUID:
        try:
            return UUID(str(spec_record["id"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise CuratedSynthesisError(
                "ResearchSpec has no valid PostgreSQL row identity"
            ) from exc

    def _coverage_items(
        self, run_id: UUID, spec: dict[str, Any], execution_mode: str
    ) -> list[dict[str, Any]]:
        self.coverage.create_items_from_spec(
            run_id,
            spec,
            execution_mode=execution_mode,
            idempotency_key=f"curated-synthesis:coverage:{run_id}",
        )
        ledger = self.coverage.rebuild_projection(
            run_id,
            idempotency_key=f"curated-synthesis:projection:{run_id}",
        )
        return [
            {
                "coverage_item_id": str(item.coverage_item_id),
                "item_type": item.item_type.value,
                "subject_id": item.subject_id,
                "status": item.status.value,
                "candidate_ids": [str(value) for value in item.candidate_ids],
                "snapshot_ids": [str(value) for value in item.snapshot_ids],
                "passage_ids": [str(value) for value in item.passage_ids],
                "freshness_status": item.freshness_status.value,
                "text": item.remaining_gap,
            }
            for item in ledger.items
        ]

    def _sealed_assets(self, run_id: UUID, seal: Any) -> list[dict[str, Any]]:
        by_subject = {
            str(item["id"]): item
            for item in self.promotion.list_assets(run_id)
            if item.get("id") is not None
        }
        result: list[dict[str, Any]] = []
        for member in seal.members:
            subject = by_subject.get(str(member.subject_id))
            if subject is None or not subject.get("candidate_id"):
                raise CuratedSynthesisError(
                    f"sealed subject {member.subject_id} lacks candidate provenance"
                )
            if str(subject.get("snapshot_id")) != str(member.snapshot_id):
                raise CuratedSynthesisError(
                    f"sealed subject {member.subject_id} snapshot provenance changed"
                )
            result.append(
                {
                    "candidate_id": str(subject["candidate_id"]),
                    "snapshot_id": str(member.snapshot_id),
                    "chunk_ids": [str(value) for value in member.chunk_ids],
                }
            )
        return result

    def _current_packet(
        self,
        run_id: UUID,
        *,
        research_spec_id: UUID,
        seal: Any,
    ) -> tuple[int | None, str]:
        with self.uow_factory() as uow:
            packet = uow.evidence_packets.get_evidence_packet(run_id)
            if packet is None:
                return None, "missing"
            if UUID(str(packet.research_spec_id)) != research_spec_id:
                return None, "stale_spec"
            packet_items = [
                *packet.payload.get("passages", ()),
                *packet.payload.get("omitted_passages", ()),
            ]
            packet_passages = {
                UUID(str(item["passage_id"]))
                for item in packet_items
                if item.get("passage_id")
            }
            if not packet_passages or not packet_passages.issubset(set(seal.chunk_ids)):
                return None, "stale_membership"
            with uow.connection.cursor() as cursor:
                cursor.execute(
                    """SELECT payload FROM research_events
                         WHERE run_id=%s AND event_type='curated_evidence_prepared'
                           AND payload->>'packet_revision'=%s
                         ORDER BY sequence_number DESC,id DESC LIMIT 1""",
                    (run_id, str(packet.packet_revision)),
                )
                marker_row = cursor.fetchone()
        marker = marker_row[0] if marker_row is not None else None
        if not isinstance(marker, dict):
            return None, "unverified_history"
        if (
            marker.get("membership_sha256") != seal.membership_sha256
            or marker.get("policy_version") != self.POLICY_VERSION
            or marker.get("research_spec_id") != str(research_spec_id)
        ):
            return None, "stale_marker"
        return int(packet.packet_revision), "reused"

    def _record_prepared_packet(
        self,
        run_id: UUID,
        *,
        packet_revision: int,
        research_spec_id: UUID,
        membership_sha256: str,
    ) -> None:
        with self.uow_factory() as uow:
            uow.runs.append_event(
                run_id,
                "curated_evidence_prepared",
                "system",
                f"curated-evidence:{run_id}:{packet_revision}:{self.POLICY_VERSION}",
                actor_identifier="CuratedSynthesisService",
                payload={
                    "packet_revision": packet_revision,
                    "research_spec_id": str(research_spec_id),
                    "membership_sha256": membership_sha256,
                    "policy_version": self.POLICY_VERSION,
                },
            )

    def _reset_stale_stages(
        self,
        run_id: UUID,
        *,
        packet_revision: int,
        model_name: str,
    ) -> int:
        with self.uow_factory() as uow, uow.connection.cursor() as cursor:
            cursor.execute(
                """UPDATE synthesis_stages
                      SET stage_status='pending',semantic_call_id=NULL,
                          semantic_artifact_id=NULL,evidence_packet_revision=%s,
                          model_name=%s,prompt_version=%s,schema_version=1,
                          artifact=NULL,error=NULL,attempts=1,updated_at=now()
                    WHERE run_id=%s AND evidence_packet_revision<>%s""",
                (
                    packet_revision,
                    model_name,
                    self.PROMPT_VERSION,
                    run_id,
                    packet_revision,
                ),
            )
            return int(cursor.rowcount)


__all__ = [
    "AuthorityAlignedLocalSynthesisService",
    "CuratedSynthesisError",
    "CuratedSynthesisService",
]
