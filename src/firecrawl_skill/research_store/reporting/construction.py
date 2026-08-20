"""Bounded autonomous-local report synthesis service.

This service decomposes report synthesis into four bounded stages:

1. **outline** — generate a claim outline from the EvidencePacket.
2. **binding** — bind each claim to passage IDs (via ClaimBindingService).
3. **draft** — draft the report body with claim references.
4. **citation_pass** — validate citation consistency and entailment.

Each stage:

* consumes ``EvidencePacket v1`` only;
* persists its semantic call and artifact via ``SemanticCallService``;
* binds draft claims to passage IDs;
* validates structured output via model-level ``call_structured`` with
  JSON schemas that are augmented with ``enum`` constraints for known IDs;
* resumes after a failed stage;
* avoids repeating already completed valid stages;
* uses only configured local endpoints unless an external fallback is
  explicitly enabled via ``allow_commercial_fallback``.

.. note::

   Validation is performed by the model through structured output with
   enum-constrained schemas (``call_structured``).  There is no
   post-hoc ``jsonschema.validate()`` pass.  The ``ClaimBindingService``
   performs additional post-validation to reject unknown claim/passage IDs.

## Architecture

* PostgreSQL is the authoritative store for synthesis stage state
  (``synthesis_stages`` table).
* Semantic calls and artifacts are persisted through the existing
  ``SemanticCallService``.
* The EvidencePacket is read from ``evidence_packets`` and validated before
  each stage.
* No commercial or remote fallback occurs without explicit configuration.

## Idempotency

Each stage is independently retryable.  A failed stage can be retried with a
new attempt count; completed stages are skipped on resume.
"""

from __future__ import annotations

import json
import logging
import pathlib
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from firecrawl_skill.research_store.assessment.evidence import EvidenceService
from firecrawl_skill.research_store.authorized_semantic import (
    call_authorized_structured,
)
from firecrawl_skill.research_store.completion_provenance import (
    CompletionProvenanceError,
    validate_citation_artifact,
)
from firecrawl_skill.research_store.config import StoreConfig
from firecrawl_skill.research_store.domain import (
    SynthesisStageName,
    SynthesisStageStatus,
)
from firecrawl_skill.research_store.semantic_cache import (
    SemanticCacheService,
    _compute_cache_key,
)
from firecrawl_skill.research_store.semantic_service import SemanticCallService

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Stage prompts
# ---------------------------------------------------------------------------

OUTLINE_SYSTEM_PROMPT = (
    "You are a rigorous research synthesizer. "
    "Given an EvidencePacket with claims, passages, and bindings, "
    "produce a structured claim outline organized into sections. "
    "Every claim must reference a valid claim_id from the EvidencePacket. "
    "Every required_passage_id must reference a valid passage_id. "
    "List unsupported claims separately with a reason."
)

DRAFT_SYSTEM_PROMPT = (
    "You are a rigorous research synthesizer. "
    "Given an EvidencePacket with claims, passages, bindings, and an outline, "
    "produce a structured draft report. "
    "Every claim reference must cite at least one valid passage_id from the "
    "EvidencePacket. "
    "Unsupported claims must be explicitly listed in the unsupported_claims "
    "section. "
    "Do not invent passage IDs."
)

CITATION_PASS_SYSTEM_PROMPT = (
    "You are a rigorous citation validator. "
    "Given an EvidencePacket and a draft report, validate every claim "
    "reference in the draft. "
    "Check that: "
    "1. Every cited passage_id exists in the EvidencePacket. "
    "2. No invented citations exist. "
    "3. Unsupported claims are explicitly labeled. "
    "4. The relationship (supports/contradicts/qualifies/context) matches "
    "the EvidencePacket binding. "
    "For each validation result: use status='valid' with issue='' when the "
    "reference is fully supported; use status='invented', 'unsupported', or "
    "'entailment_mismatch' with a substantive non-empty issue description "
    "when there is a problem. The schema enforces that status=='valid' requires "
    "issue=='' and any error status requires a non-empty issue string. Never "
    "use issue='none' — empty string is the only canonical no-error "
    "representation."
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ReportServiceError(RuntimeError):
    """Base exception for ReportService errors."""


class CommercialFallbackError(ReportServiceError):
    """Raised when a commercial provider is used without explicit permission."""


class LocalSynthesisService:
    """Bounded autonomous-local report synthesis service.

    Args:
        semantic_service: SemanticCallService for model call persistence.
        evidence_service: EvidenceService for EvidencePacket access.
        config: StoreConfig for local endpoint configuration.
        binding_service: Optional ClaimBindingService for claim-to-passage
            binding.  If not provided, a new instance is created internally.
            Injecting a mock is useful for unit tests that avoid real LLM
            calls.
        resource_governor: Optional ResourceGovernor for bounded concurrent
            generative calls.  When provided, every LLM call is wrapped with
            acquire/release so that concurrency, token, and batch caps are
            enforced.
    """

    def __init__(
        self,
        semantic_service: SemanticCallService,
        evidence_service: EvidenceService,
        config: StoreConfig,
        binding_service: Any = None,
        cache_ttl_seconds: int = 3600,
        resource_governor: Any = None,
    ) -> None:
        self.semantic = semantic_service
        self.evidence = evidence_service
        self.config = config
        self._schemas: dict[str, dict[str, Any]] = {}
        self._binding_service = binding_service
        self._cache_ttl_seconds = cache_ttl_seconds
        self._cache: SemanticCacheService | None = None
        self._resource_governor = resource_governor
        self._load_schemas()

    @property
    def cache(self) -> SemanticCacheService:
        """Lazy-initialize the semantic cache service."""
        if self._cache is None:
            self._cache = SemanticCacheService(
                uow_factory=self.semantic.uow_factory,
                ttl_seconds=self._cache_ttl_seconds,
                valkey_url=self.config.valkey_url,
            )
        return self._cache

    # ------------------------------------------------------------------
    # Resource governance (P7-06)
    # ------------------------------------------------------------------

    def _bounded_llm_call(
        self,
        call_fn,
        *,
        input_tokens: int = 0,
        batch_size: int = 1,
    ) -> Any:
        """Execute an LLM call through the resource governor.

        Wraps ``call_fn`` with ``acquire_sync`` / ``release_sync`` so that
        concurrency, token, and batch caps are enforced.  If no governor is
        configured the call proceeds without gating.

        Args:
            call_fn: A zero-arity callable that performs the LLM call.
            input_tokens: Estimated input token count for the request.
                Passed to the governor for token-cap enforcement.
            batch_size: Number of items in the batch.
                Passed to the governor for batch-cap enforcement.

        Returns:
            Whatever ``call_fn`` returns.

        Raises:
            ResourceLimitError / EndpointUnavailableError when a governor
            limit is exceeded.  These are re-raised after release so the
            caller can handle them (e.g. record the error for resume).
        """
        if self._resource_governor is None:
            return call_fn()

        governor = self._resource_governor
        try:
            governor.acquire_sync(
                "generative", input_tokens=input_tokens, batch_size=batch_size
            )
            return call_fn()
        finally:
            governor.release_sync("generative")

    # ------------------------------------------------------------------
    # Schema loading
    # ------------------------------------------------------------------

    def _load_schemas(self) -> None:
        """Load JSON schemas for synthesis stages from disk."""
        schemas_dir = (
            pathlib.Path(__file__).resolve().parents[4]
            / "schemas"
            / "research-workflow"
        )
        schema_files = [
            "synthesis-outline-v1.json",
            "synthesis-draft-v1.json",
            "synthesis-citation-pass-v1.json",
            "claim-binding-v1.json",
        ]
        for name in schema_files:
            path = schemas_dir / name
            if path.is_file():
                try:
                    self._schemas[name] = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    logger.warning("failed to load schema %s: %s", name, exc)
            else:
                logger.warning("schema file not found: %s", path)

    def _get_schema(self, stage_name: str) -> dict[str, Any]:
        """Get the JSON schema for a synthesis stage."""
        file_name = SynthesisStageName(stage_name).schema_file
        schema = self._schemas.get(file_name)
        if schema is None:
            raise ReportServiceError(f"schema not loaded for stage: {stage_name}")
        return deepcopy(schema)

    # ------------------------------------------------------------------
    # EvidencePacket access
    # ------------------------------------------------------------------

    def _get_packet(self, run_id: UUID, packet_revision: int) -> dict[str, Any]:
        """Fetch the EvidencePacket from the database or fail closed."""
        record = self.evidence.export_packet(run_id, packet_revision)
        if record is None:
            raise ReportServiceError(
                f"EvidencePacket {run_id} r{packet_revision} not found"
            )
        packet = dict(record.get("payload", record))
        packet["_packet_revision"] = int(record.get("packet_revision", packet_revision))
        return packet

    def _validate_packet(self, packet: dict[str, Any]) -> None:
        """Validate that the packet is EvidencePacket v1."""
        version = packet.get("schema_version", "")
        if not version.startswith("evidence-packet-v1"):
            raise ReportServiceError(
                f"unsupported EvidencePacket version: {version}; "
                "expected evidence-packet-v1"
            )
        if not packet.get("claims"):
            raise ReportServiceError(
                "EvidencePacket has no claims; synthesis cannot proceed"
            )
        if not packet.get("passages"):
            raise ReportServiceError(
                "EvidencePacket has no passages; synthesis cannot proceed"
            )

    # ------------------------------------------------------------------
    # Local endpoint enforcement
    # ------------------------------------------------------------------

    def _enforce_local_only(self, provider: str) -> None:
        """Ensure only local endpoints are used unless explicitly allowed."""
        if provider != "local":
            raise CommercialFallbackError(
                f"commercial provider '{provider}' is not permitted. "
                "Set allow_commercial_fallback=True to use commercial providers."
            )

    # ------------------------------------------------------------------
    # Stage management
    # ------------------------------------------------------------------

    def _init_stages(
        self,
        uow: Any,
        run_id: UUID,
        packet_revision: int,
        model_name: str,
        prompt_version: str,
        schema_version: int,
    ) -> dict[str, dict[str, Any]]:
        """Initialize or load synthesis stage records for a run.

        Creates rows for all stages that don't exist yet.  Returns a dict
        mapping stage_name -> dict (UOW-compatible record).
        """
        stages: dict[str, dict[str, Any]] = {}
        for stage_name in SynthesisStageName:
            try:
                existing = uow.synthesis_stages.get_synthesis_stage(
                    run_id, stage_name.value
                )
                stages[stage_name.value] = existing
            except KeyError:
                now = _utcnow()
                record: dict[str, Any] = {
                    "id": uuid4(),
                    "run_id": run_id,
                    "stage_name": stage_name.value,
                    "stage_status": SynthesisStageStatus.PENDING.value,
                    "semantic_call_id": None,
                    "semantic_artifact_id": None,
                    "evidence_packet_revision": packet_revision,
                    "model_name": model_name,
                    "prompt_version": prompt_version,
                    "schema_version": schema_version,
                    "artifact": None,
                    "error": None,
                    "attempts": 1,
                    "created_at": now,
                    "updated_at": now,
                }
                uow.synthesis_stages.insert_synthesis_stage(record)
                stages[stage_name.value] = record

        return stages

    def _update_stage(
        self,
        uow: Any,
        record: dict[str, Any],
        *,
        status: str,
        artifact: dict[str, Any] | None = None,
        error: str | None = None,
        semantic_call_id: UUID | None = None,
        semantic_artifact_id: UUID | None = None,
        increment_attempts: bool = False,
    ) -> None:
        """Update a synthesis stage record."""
        updated = dict(record)
        updated["stage_status"] = status
        if semantic_call_id is not None:
            updated["semantic_call_id"] = semantic_call_id
        if semantic_artifact_id is not None:
            updated["semantic_artifact_id"] = semantic_artifact_id
        if artifact is not None:
            updated["artifact"] = artifact
        if error is not None:
            updated["error"] = error
        if increment_attempts:
            updated["attempts"] = record.get("attempts", 1) + 1
        updated["updated_at"] = _utcnow()
        uow.synthesis_stages.update_synthesis_stage(updated)

    def _commit_stage_failure(
        self,
        uow_factory: Any,
        run_id: UUID,
        stage_name: str,
        error: str,
        *,
        increment_attempts: bool = True,
    ) -> None:
        """Commit a failed-stage update through its own UOW boundary.

        The real PostgreSQL UOW rolls back on any exception during ``__exit__``.
        Callers must never raise from inside the same UOW context that writes
        the failure — otherwise the just-committed ``failed`` status is lost.
        This helper opens and closes its own UOW so the write is durable
        before the caller raises.
        """
        with uow_factory() as uow:
            record = uow.synthesis_stages.get_synthesis_stage(run_id, stage_name)
            self._update_stage(
                uow,
                record,
                status=SynthesisStageStatus.FAILED.value,
                error=error,
                increment_attempts=increment_attempts,
            )

    def _is_stage_completed(self, record: dict[str, Any]) -> bool:
        """Check if a stage has already completed successfully."""
        return record.get("stage_status") == SynthesisStageStatus.COMPLETED.value

    def _is_stage_failed(self, record: dict[str, Any]) -> bool:
        """Check if a stage has failed."""
        return record.get("stage_status") == SynthesisStageStatus.FAILED.value

    # ------------------------------------------------------------------
    # Cache integration (issue #41)
    # ------------------------------------------------------------------

    def _check_cache(
        self,
        *,
        stage: str,
        model_name: str,
        prompt_version: str,
        prompt_hash: str,
        schema_version: int,
        input_hash: str,
        run_id: UUID,
        packet: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Check the semantic cache before an LLM call.

        Returns the cached artifact if a valid, non-expired entry exists
        **and** the artifact passes current reference validation.
        Returns ``None`` if the cache misses or the cached artifact is stale.
        """
        key_hash = _compute_cache_key(
            stage=stage,
            model_name=model_name,
            model_revision="",
            endpoint_alias="local",
            prompt_version=prompt_version,
            prompt_hash=prompt_hash,
            schema_version=schema_version,
            input_hash=input_hash,
            policy_version="budget-policy-v1",
            configuration={
                "chunker_version": str(self.config.chunker_version),
                "parser_version": str(self.config.parser_version),
            },
        )
        try:
            entry = self.cache.lookup(
                stage=stage,
                model_name=model_name,
                model_revision="",
                endpoint_alias="local",
                prompt_version=prompt_version,
                prompt_hash=prompt_hash,
                schema_version=schema_version,
                input_hash=input_hash,
                policy_version="budget-policy-v1",
                configuration={
                    "chunker_version": self.config.chunker_version,
                    "parser_version": self.config.parser_version,
                },
            )
        except Exception:  # noqa: BLE001
            logger.warning("semantic cache lookup failed; proceeding without cache")
            self._record_cache_lookup(
                run_id, stage, key_hash, model_name=model_name, hit=False
            )
            return None

        if entry is None:
            logger.debug("semantic cache miss for stage %s", stage)
            self._record_cache_lookup(
                run_id, stage, key_hash, model_name=model_name, hit=False
            )
            return None

        if not self._validate_cached_artifact(entry.artifact, packet):
            logger.info(
                "semantic cache hit for stage %s but artifact is stale; "
                "proceeding without cache",
                stage,
            )
            self._record_cache_lookup(
                run_id, stage, entry.key_hash, model_name=model_name, hit=False
            )
            return None

        logger.info(
            "semantic cache hit for stage %s (key %s...); reusing cached artifact",
            stage,
            entry.key_hash[:12],
        )
        self._record_cache_lookup(
            run_id, stage, entry.key_hash, model_name=model_name, hit=True
        )
        return entry.artifact

    def _record_cache_lookup(
        self,
        run_id: UUID,
        stage: str,
        key_hash: str,
        *,
        model_name: str,
        hit: bool,
    ) -> None:
        """Persist one exact-run semantic-cache lookup observation."""
        try:
            from firecrawl_skill.research_store.telemetry_service import (
                PerformanceTelemetryService,
            )

            with self.semantic.uow_factory() as uow:
                PerformanceTelemetryService(uow.connection).record_cache_event(
                    run_id,
                    stage,
                    "lookup",
                    key_hash=key_hash,
                    model_fingerprint=f"{model_name}::local",
                    hit=hit,
                )
        except Exception:  # noqa: BLE001
            logger.warning(
                "run-scoped cache telemetry persistence failed for stage %s", stage
            )

    def _write_cache(
        self,
        *,
        stage: str,
        model_name: str,
        prompt_version: str,
        prompt_hash: str,
        schema_version: int,
        input_hash: str,
        artifact: dict[str, Any],
        provenance: dict[str, Any],
    ) -> None:
        """Write a result to the semantic cache after a successful LLM call."""
        try:
            self.cache.insert(
                stage=stage,
                model_name=model_name,
                model_revision="",
                endpoint_alias="local",
                prompt_version=prompt_version,
                prompt_hash=prompt_hash,
                schema_version=schema_version,
                input_hash=input_hash,
                policy_version="budget-policy-v1",
                configuration={
                    "chunker_version": self.config.chunker_version,
                    "parser_version": self.config.parser_version,
                },
                artifact=artifact,
                provenance=provenance,
            )
        except Exception:  # noqa: BLE001
            logger.warning("semantic cache write failed; proceeding without cache")

    @staticmethod
    def _validate_cached_artifact(
        artifact: dict[str, Any], packet: dict[str, Any]
    ) -> bool:
        if not artifact:
            return False
        cached_revision = artifact.get("evidence_packet_revision", 0)
        current_revision = packet.get("_packet_revision", 0)
        if cached_revision != current_revision:
            return False
        claim_ids_in_packet = {c["claim_id"] for c in packet.get("claims", [])}
        for section in artifact.get("outline_sections", []):
            for claim_ref in section.get("claims", []):
                cid = claim_ref.get("claim_id", "")
                if cid and cid not in claim_ids_in_packet:
                    return False
        for uc in artifact.get("unsupported_claims", []):
            cid = uc.get("claim_id", "")
            if cid and cid not in claim_ids_in_packet:
                return False
        for section in artifact.get("report_sections", []):
            for claim_ref in section.get("claims", []):
                cid = claim_ref.get("claim_id", "")
                if cid and cid not in claim_ids_in_packet:
                    return False
        for ic in artifact.get("invented_citations", []):
            cid = ic.get("claim_id", "")
            if cid and cid not in claim_ids_in_packet:
                return False
        return True

    def run_synthesis(
        self,
        run_id: UUID,
        packet_revision: int,
        model_name: str | None = None,
        prompt_version: str = "synthesis-v1",
        allow_commercial_fallback: bool = False,
    ) -> dict[str, Any]:
        if not allow_commercial_fallback:
            self._enforce_local_only("local")
        packet = self._get_packet(run_id, packet_revision)
        self._validate_packet(packet)
        with self.semantic.uow_factory() as uow:
            status = uow.runs.get_run_status(run_id=run_id)
        execution_mode = status.get("execution_mode", "agent_led")
        if execution_mode == "deterministic_debug":
            model_name = ""
        elif not model_name:
            raise ReportServiceError("no model configured for synthesis")
        with self.semantic.uow_factory() as uow:
            self._init_stages(
                uow, run_id, packet_revision, model_name, prompt_version, 1
            )
        results: dict[str, Any] = {}
        overall_status = "completed"
        last_error: str | None = None
        for stage_name in SynthesisStageName:
            stage_key = stage_name.value
            with self.semantic.uow_factory() as uow:
                record = uow.synthesis_stages.get_synthesis_stage(run_id, stage_key)
            if self._is_stage_completed(record):
                results[stage_key] = {
                    "status": "skipped",
                    "reason": "already_completed",
                    "evidence_packet_revision": packet_revision,
                }
                continue
            try:
                stage_result = self._execute_stage(
                    uow_factory=self.semantic.uow_factory,
                    run_id=run_id,
                    stage_name=stage_key,
                    packet=packet,
                    model_name=model_name,
                    prompt_version=prompt_version,
                    allow_commercial_fallback=allow_commercial_fallback,
                )
                results[stage_key] = stage_result
            except ReportServiceError as exc:
                overall_status = "failed"
                last_error = str(exc)
                results[stage_key] = {
                    "status": "failed",
                    "error": str(exc),
                    "evidence_packet_revision": packet_revision,
                }
                self._mark_remaining_failed(
                    uow_factory=self.semantic.uow_factory,
                    run_id=run_id,
                    current_stage=stage_key,
                    error=str(exc),
                )
                break
        summary = {
            "run_id": str(run_id),
            "evidence_packet_revision": packet_revision,
            "model_name": model_name,
            "prompt_version": prompt_version,
            "overall_status": overall_status,
            "stages": results,
        }
        if last_error:
            summary["error"] = last_error
        return summary

    def _execute_stage(
        self,
        uow_factory: Any,
        run_id: UUID,
        stage_name: str,
        packet: dict[str, Any],
        model_name: str,
        prompt_version: str,
        allow_commercial_fallback: bool,
    ) -> dict[str, Any]:
        stage_map = {
            "outline": self._run_outline_stage,
            "binding": self._run_binding_stage,
            "draft": self._run_draft_stage,
            "citation_pass": self._run_citation_pass_stage,
            "validation": self._run_validation_stage,
        }
        executor = stage_map.get(stage_name)
        if executor is None:
            raise ReportServiceError(f"unknown synthesis stage: {stage_name}")
        return executor(
            uow_factory=uow_factory,
            run_id=run_id,
            packet=packet,
            model_name=model_name,
            prompt_version=prompt_version,
            allow_commercial_fallback=allow_commercial_fallback,
        )

    def _mark_remaining_failed(
        self,
        uow_factory: Any,
        run_id: UUID,
        current_stage: str,
        error: str,
    ) -> None:
        order = {
            "outline": 1,
            "binding": 2,
            "draft": 3,
            "citation_pass": 4,
            "validation": 5,
        }
        current_order = order.get(current_stage, 99)
        with uow_factory() as uow:
            for stage_name in SynthesisStageName:
                if stage_name.order <= current_order:
                    continue
                try:
                    record = uow.synthesis_stages.get_synthesis_stage(
                        run_id, stage_name.value
                    )
                    if record.get("stage_status") in (
                        SynthesisStageStatus.PENDING.value,
                        SynthesisStageStatus.RUNNING.value,
                    ):
                        self._update_stage(
                            uow,
                            record,
                            status=SynthesisStageStatus.FAILED.value,
                            error=f"upstream stage failed: {error}",
                            increment_attempts=False,
                        )
                except KeyError:
                    continue

    def _run_outline_stage(
        self,
        uow_factory: Any,
        run_id: UUID,
        packet: dict[str, Any],
        model_name: str,
        prompt_version: str,
        allow_commercial_fallback: bool,
    ) -> dict[str, Any]:
        schema = self._get_schema("outline")
        valid_claim_ids = [c["claim_id"] for c in packet.get("claims", [])]
        valid_passage_ids = [p["passage_id"] for p in packet.get("passages", [])]
        packet_revision = packet.get("_packet_revision", 1)
        schema = deepcopy(schema)
        schema["properties"]["run_id"]["enum"] = [str(run_id)]
        schema["properties"]["evidence_packet_revision"]["enum"] = [packet_revision]
        for section in schema["properties"]["outline_sections"]["items"]["properties"][
            "claims"
        ]["items"]["properties"]:
            if section == "claim_id":
                schema["properties"]["outline_sections"]["items"]["properties"][
                    "claims"
                ]["items"]["properties"][section]["enum"] = valid_claim_ids
            elif section == "required_passage_ids":
                schema["properties"]["outline_sections"]["items"]["properties"][
                    "claims"
                ]["items"]["properties"][section]["items"]["enum"] = valid_passage_ids
        system_prompt = OUTLINE_SYSTEM_PROMPT
        user_prompt = json.dumps(
            {
                "run_id": str(run_id),
                "evidence_packet_revision": packet_revision,
                "claims": [
                    {"claim_id": c["claim_id"], "statement": c["statement"]}
                    for c in packet.get("claims", [])
                ],
                "passages": [
                    {"passage_id": p["passage_id"], "text": p["text"]}
                    for p in packet.get("passages", [])
                ],
            },
            indent=2,
        )
        context = {
            "run_id": str(run_id),
            "stage": "outline",
            "schema_name": "synthesis-outline-v1",
            "schema_version": 1,
            "idempotency_key": f"{run_id}-r{packet_revision}-outline",
            "input_artifact_ids": [f"packet-{run_id}-r{packet_revision}"],
            "prompt_version": prompt_version,
        }
        with uow_factory() as uow:
            status = uow.runs.get_run_status(run_id=run_id)
            context["run_revision"] = status["lifecycle_revision"]
        prompt_hash = self.cache.compute_prompt_hash(system_prompt, user_prompt)
        input_hash = self.cache.compute_input_hash(json.loads(user_prompt))
        cached = self._check_cache(
            stage="outline",
            model_name=model_name,
            prompt_version=prompt_version,
            prompt_hash=prompt_hash,
            schema_version=1,
            input_hash=input_hash,
            run_id=run_id,
            packet=packet,
        )
        if cached is not None:
            with uow_factory() as uow:
                record = uow.synthesis_stages.get_synthesis_stage(run_id, "outline")
                self._update_stage(
                    uow,
                    record,
                    status=SynthesisStageStatus.COMPLETED.value,
                    artifact=cached,
                )
            return {"status": "completed", "cache_hit": True}

        def _call():
            deterministic_fixture = {
                "schema_version": "synthesis-outline-v1",
                "run_id": str(run_id),
                "evidence_packet_revision": packet_revision,
                "outline_sections": [],
                "unsupported_claims": [],
            }
            return call_authorized_structured(
                semantic_service=self.semantic,
                semantic_context=context,
                deterministic_fixture=deterministic_fixture,
                actor_identifier="release-campaign-host-outline",
                host_artifact_supplier=self.semantic.host_artifact_supplier,
                provider="local",
                model=model_name,
                schema=schema,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                prompt_version=prompt_version,
            )

        result = self._bounded_llm_call(_call)
        if result.error:
            self._commit_stage_failure(uow_factory, run_id, "outline", result.error)
            raise ReportServiceError(f"outline stage failed: {result.error}")
        if result.value is None:
            self._commit_stage_failure(
                uow_factory, run_id, "outline", "structured result is missing"
            )
            raise ReportServiceError("outline stage returned no structured result")
        with uow_factory() as uow:
            record = uow.synthesis_stages.get_synthesis_stage(run_id, "outline")
            self._update_stage(
                uow,
                record,
                status=SynthesisStageStatus.COMPLETED.value,
                artifact=result.value,
                semantic_call_id=result.semantic_call_id,
                semantic_artifact_id=(
                    result.artifact_ids[-1] if result.artifact_ids else None
                ),
            )
        self._write_cache(
            stage="outline",
            model_name=model_name,
            prompt_version=prompt_version,
            prompt_hash=prompt_hash,
            schema_version=1,
            input_hash=input_hash,
            artifact=result.value,
            provenance={"provider": "local", "prompt_version": prompt_version},
        )
        return {"status": "completed", "cache_hit": False}

    def _run_binding_stage(
        self,
        uow_factory: Any,
        run_id: UUID,
        packet: dict[str, Any],
        model_name: str,
        prompt_version: str,
        allow_commercial_fallback: bool,
    ) -> dict[str, Any]:
        from firecrawl_skill.research_store.assessment.binding import ClaimBindingService

        if self._binding_service is None:
            self._binding_service = ClaimBindingService(self.semantic, self.evidence)
        packet_revision = packet.get("_packet_revision", 1)
        if packet.get("claim_evidence_bindings") and all(
            claim.get("semantic_status") != "unassessed"
            for claim in packet.get("claims", [])
        ):
            with uow_factory() as uow:
                record = uow.synthesis_stages.get_synthesis_stage(run_id, "binding")
                self._update_stage(
                    uow,
                    record,
                    status=SynthesisStageStatus.COMPLETED.value,
                    artifact={"new_packet_revision": packet_revision},
                )
            return {"status": "completed", "cache_hit": False}
        new_revision = self._binding_service.evaluate_claims(
            run_id=run_id,
            packet_revision=packet_revision,
            prompt_version=prompt_version,
            model_name=model_name,
            provider="local",
        )
        with uow_factory() as uow:
            record = uow.synthesis_stages.get_synthesis_stage(run_id, "binding")
            self._update_stage(
                uow,
                record,
                status=SynthesisStageStatus.COMPLETED.value,
                artifact={"new_packet_revision": new_revision},
            )
        return {
            "status": "completed",
            "evidence_packet_revision": new_revision,
            "cache_hit": False,
        }

    def _run_draft_stage(
        self,
        uow_factory: Any,
        run_id: UUID,
        packet: dict[str, Any],
        model_name: str,
        prompt_version: str,
        allow_commercial_fallback: bool,
    ) -> dict[str, Any]:
        schema = self._get_schema("draft")
        packet_revision = packet.get("_packet_revision", 1)
        system_prompt = DRAFT_SYSTEM_PROMPT
        user_prompt = json.dumps(
            {"run_id": str(run_id), "claims": packet.get("claims", [])}, indent=2
        )
        context = {
            "run_id": str(run_id),
            "stage": "draft",
            "schema_name": "synthesis-draft-v1",
            "schema_version": 1,
            "idempotency_key": f"{run_id}-r{packet_revision}-draft",
            "input_artifact_ids": [f"packet-{run_id}-r{packet_revision}"],
            "prompt_version": prompt_version,
        }
        with uow_factory() as uow:
            status = uow.runs.get_run_status(run_id=run_id)
            context["run_revision"] = status["lifecycle_revision"]
        prompt_hash = self.cache.compute_prompt_hash(system_prompt, user_prompt)
        input_hash = self.cache.compute_input_hash(json.loads(user_prompt))
        cached = self._check_cache(
            stage="draft",
            model_name=model_name,
            prompt_version=prompt_version,
            prompt_hash=prompt_hash,
            schema_version=1,
            input_hash=input_hash,
            run_id=run_id,
            packet=packet,
        )
        if cached is not None:
            with uow_factory() as uow:
                record = uow.synthesis_stages.get_synthesis_stage(run_id, "draft")
                self._update_stage(
                    uow,
                    record,
                    status=SynthesisStageStatus.COMPLETED.value,
                    artifact=cached,
                )
            return {"status": "completed", "cache_hit": True}

        def _call():
            deterministic_fixture = {
                "schema_version": "synthesis-draft-v1",
                "run_id": str(run_id),
                "evidence_packet_revision": packet_revision,
                "report_sections": [],
                "unsupported_claims": [],
                "limitations": list(packet.get("limitations", [])),
            }
            return call_authorized_structured(
                semantic_service=self.semantic,
                semantic_context=context,
                deterministic_fixture=deterministic_fixture,
                actor_identifier="release-campaign-host-draft",
                host_artifact_supplier=self.semantic.host_artifact_supplier,
                provider="local",
                model=model_name,
                schema=schema,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                prompt_version=prompt_version,
            )

        result = self._bounded_llm_call(_call)
        if result.error:
            self._commit_stage_failure(uow_factory, run_id, "draft", result.error)
            raise ReportServiceError(f"draft stage failed: {result.error}")
        if result.value is None:
            self._commit_stage_failure(
                uow_factory, run_id, "draft", "structured result is missing"
            )
            raise ReportServiceError("draft stage returned no structured result")
        with uow_factory() as uow:
            record = uow.synthesis_stages.get_synthesis_stage(run_id, "draft")
            self._update_stage(
                uow,
                record,
                status=SynthesisStageStatus.COMPLETED.value,
                artifact=result.value,
                semantic_call_id=result.semantic_call_id,
                semantic_artifact_id=(
                    result.artifact_ids[-1] if result.artifact_ids else None
                ),
            )
        self._write_cache(
            stage="draft",
            model_name=model_name,
            prompt_version=prompt_version,
            prompt_hash=prompt_hash,
            schema_version=1,
            input_hash=input_hash,
            artifact=result.value,
            provenance={"provider": "local", "prompt_version": prompt_version},
        )
        return {"status": "completed", "cache_hit": False}

    def _run_citation_pass_stage(
        self,
        uow_factory: Any,
        run_id: UUID,
        packet: dict[str, Any],
        model_name: str,
        prompt_version: str,
        allow_commercial_fallback: bool,
    ) -> dict[str, Any]:
        schema = self._get_schema("citation_pass")
        packet_revision = packet.get("_packet_revision", 1)
        with uow_factory() as uow:
            draft_record = uow.synthesis_stages.get_synthesis_stage(run_id, "draft")
            draft_artifact = draft_record.get("artifact") or {}
        draft_sections = draft_artifact.get("report_sections", [])
        system_prompt = CITATION_PASS_SYSTEM_PROMPT
        user_prompt = json.dumps(
            {
                "run_id": str(run_id),
                "evidence_packet_revision": packet_revision,
                "draft_sections": draft_sections,
            },
            indent=2,
        )
        context = {
            "run_id": str(run_id),
            "stage": "citation_pass",
            "schema_name": "synthesis-citation-pass-v1",
            "schema_version": 1,
            "idempotency_key": f"{run_id}-r{packet_revision}-citation",
            "input_artifact_ids": [f"packet-{run_id}-r{packet_revision}"],
            "prompt_version": prompt_version,
        }
        with uow_factory() as uow:
            status = uow.runs.get_run_status(run_id=run_id)
            context["run_revision"] = status["lifecycle_revision"]
        prompt_hash = self.cache.compute_prompt_hash(system_prompt, user_prompt)
        input_hash = self.cache.compute_input_hash(json.loads(user_prompt))
        cached = self._check_cache(
            stage="citation_pass",
            model_name=model_name,
            prompt_version=prompt_version,
            prompt_hash=prompt_hash,
            schema_version=1,
            input_hash=input_hash,
            run_id=run_id,
            packet=packet,
        )
        if cached is not None:
            with uow_factory() as uow:
                record = uow.synthesis_stages.get_synthesis_stage(
                    run_id, "citation_pass"
                )
                self._update_stage(
                    uow,
                    record,
                    status=SynthesisStageStatus.COMPLETED.value,
                    artifact=cached,
                )
            return {"status": "completed", "cache_hit": True}

        def _call():
            deterministic_fixture = {
                "schema_version": "synthesis-citation-pass-v1",
                "run_id": str(run_id),
                "evidence_packet_revision": packet_revision,
                "draft_revision": 1,
                "pass_status": "passed",
                "validation_results": [],
                "invented_citations": [],
                "unsupported_claims": [],
                "entailment_mismatches": [],
            }
            return call_authorized_structured(
                semantic_service=self.semantic,
                semantic_context=context,
                deterministic_fixture=deterministic_fixture,
                actor_identifier="release-campaign-host-citation-validator",
                host_artifact_supplier=self.semantic.host_artifact_supplier,
                provider="local",
                model=model_name,
                schema=schema,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                prompt_version=prompt_version,
            )

        result = self._bounded_llm_call(_call)
        if result.error:
            self._commit_stage_failure(
                uow_factory, run_id, "citation_pass", result.error
            )
            raise ReportServiceError(f"citation pass stage failed: {result.error}")
        if result.value is None:
            self._commit_stage_failure(
                uow_factory, run_id, "citation_pass", "structured result is missing"
            )
            raise ReportServiceError(
                "citation pass stage returned no structured result"
            )
        draft_citations: set[tuple[str, str, tuple[str, ...]]] = set()
        for section in draft_sections:
            for reference in section.get("claim_references", []):
                claim_id = str(reference.get("claim_id") or "")
                cited = tuple(
                    sorted(str(item) for item in reference.get("passage_ids") or ())
                )
                draft_citations.add((section["section_id"], claim_id, cited))
        try:
            validate_citation_artifact(result.value, draft_citations)
        except CompletionProvenanceError as exc:
            self._commit_stage_failure(uow_factory, run_id, "citation_pass", str(exc))
            raise ReportServiceError(
                f"citation-pass semantic validation failed: {exc}"
            )
        with uow_factory() as uow:
            record = uow.synthesis_stages.get_synthesis_stage(run_id, "citation_pass")
            self._update_stage(
                uow,
                record,
                status=SynthesisStageStatus.COMPLETED.value,
                artifact=result.value,
                semantic_call_id=result.semantic_call_id,
                semantic_artifact_id=(
                    result.artifact_ids[-1] if result.artifact_ids else None
                ),
            )
        self._write_cache(
            stage="citation_pass",
            model_name=model_name,
            prompt_version=prompt_version,
            prompt_hash=prompt_hash,
            schema_version=1,
            input_hash=input_hash,
            artifact=result.value,
            provenance={"provider": "local", "prompt_version": prompt_version},
        )
        return {"status": "completed", "cache_hit": False}

    def _run_validation_stage(
        self,
        uow_factory: Any,
        run_id: UUID,
        packet: dict[str, Any],
        model_name: str,
        prompt_version: str,
        allow_commercial_fallback: bool,
    ) -> dict[str, Any]:
        from firecrawl_skill.research_store.reporting.artifacts import (
            ReportArtifactService,
        )

        with uow_factory() as uow:
            try:
                citation_record = uow.synthesis_stages.get_synthesis_stage(
                    run_id, "citation_pass"
                )
            except KeyError:
                raise ReportServiceError(
                    "citation_pass stage must complete before validation"
                )
        report_artifact = citation_record.get("artifact") or {}
        report = {
            "schema_version": report_artifact.get(
                "schema_version", "synthesis-citation-pass-v1"
            ),
            "run_id": str(run_id),
            "evidence_packet_revision": report_artifact.get(
                "evidence_packet_revision", packet.get("_packet_revision", 1)
            ),
            "draft_revision": report_artifact.get("draft_revision", 1),
            "pass_status": report_artifact.get("pass_status", "failed"),
            "validation_results": report_artifact.get("validation_results", []),
            "invented_citations": report_artifact.get("invented_citations", []),
            "unsupported_claims": report_artifact.get("unsupported_claims", []),
            "entailment_mismatches": report_artifact.get("entailment_mismatches", []),
        }
        artifact_service = ReportArtifactService(uow_factory, self.evidence)
        validation_result = artifact_service.validate_report(run_id, report)
        artifact_service.persist_validation_result(
            run_id,
            report,
            validation_result,
            model_name=model_name,
            prompt_version=prompt_version,
        )
        with uow_factory() as uow:
            try:
                record = uow.synthesis_stages.get_synthesis_stage(run_id, "validation")
            except KeyError:
                record = None
            if record is not None:
                self._update_stage(
                    uow,
                    record,
                    status=(
                        SynthesisStageStatus.COMPLETED.value
                        if validation_result.is_valid
                        else SynthesisStageStatus.FAILED.value
                    ),
                    artifact=validation_result.to_dict(),
                    error=None
                    if validation_result.is_valid
                    else validation_result.summary,
                )
        if not validation_result.is_valid:
            raise ReportServiceError(
                f"report validation failed: {validation_result.summary}"
            )
        return {
            "status": "completed",
            "report_hash": validation_result.report_hash,
            "evidence_packet_revision": validation_result.packet_revision,
            "stale_packet": validation_result.stale_packet,
            "validation_status": "valid",
            "claim_count": len(validation_result.claim_manifest),
        }

    def get_stage_status(
        self,
        uow_factory: Any,
        run_id: UUID,
        stage_name: str | None = None,
    ) -> list[dict[str, Any]]:
        with uow_factory() as uow:
            if stage_name:
                try:
                    record = uow.synthesis_stages.get_synthesis_stage(
                        run_id, stage_name
                    )
                    return [record]
                except KeyError:
                    return []
            return uow.synthesis_stages.get_synthesis_stages(run_id)

    def resume_failed_synthesis(
        self,
        run_id: UUID,
        packet_revision: int,
        model_name: str | None = None,
        prompt_version: str = "synthesis-v1",
        allow_commercial_fallback: bool = False,
    ) -> dict[str, Any]:
        return self.run_synthesis(
            run_id=run_id,
            packet_revision=packet_revision,
            model_name=model_name,
            prompt_version=prompt_version,
            allow_commercial_fallback=allow_commercial_fallback,
        )
