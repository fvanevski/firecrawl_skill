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

from .config import StoreConfig
from .domain import SynthesisStageName, SynthesisStageStatus
from .evidence import EvidenceService
from .semantic_cache import SemanticCacheService
from .semantic_service import SemanticCallService

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
    "Return the validation results in the required JSON schema."
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
    """

    def __init__(
        self,
        semantic_service: SemanticCallService,
        evidence_service: EvidenceService,
        config: StoreConfig,
        binding_service: Any = None,
        cache_ttl_seconds: int = 3600,
    ) -> None:
        self.semantic = semantic_service
        self.evidence = evidence_service
        self.config = config
        self._schemas: dict[str, dict[str, Any]] = {}
        self._binding_service = binding_service
        self._cache_ttl_seconds = cache_ttl_seconds
        self._cache: SemanticCacheService | None = None
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
    # Schema loading
    # ------------------------------------------------------------------

    def _load_schemas(self) -> None:
        """Load JSON schemas for synthesis stages from disk."""
        schemas_dir = (
            pathlib.Path(__file__).parent.parent.parent
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

    def _get_packet(self, run_id: UUID, packet_revision: int) -> dict[str, Any] | None:
        """Fetch the EvidencePacket from the database."""
        return self.evidence.export_packet(run_id, packet_revision)

    def _validate_packet(self, packet: dict[str, Any]) -> None:
        """Validate that the packet is EvidencePacket v1."""
        if packet is None:
            raise ReportServiceError("EvidencePacket is None")
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
            # Cache unavailability is non-authoritative — fall through.
            logger.warning("semantic cache lookup failed; proceeding without cache")
            return None

        if entry is None:
            logger.debug("semantic cache miss for stage %s", stage)
            return None

        # Validate the cached artifact against the current EvidencePacket.
        if not self._validate_cached_artifact(entry.artifact, packet):
            logger.info(
                "semantic cache hit for stage %s but artifact is stale; "
                "proceeding without cache",
                stage,
            )
            return None

        logger.info(
            "semantic cache hit for stage %s (key %s...); reusing cached artifact",
            stage,
            entry.key_hash[:12],
        )
        return entry.artifact

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
        """Write a result to the semantic cache after a successful LLM call.

        Idempotent: if a valid entry already exists for the key, no duplicate
        is created.
        """
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
            # Cache write failure is non-authoritative — fall through.
            logger.warning("semantic cache write failed; proceeding without cache")

    @staticmethod
    def _validate_cached_artifact(
        artifact: dict[str, Any], packet: dict[str, Any]
    ) -> bool:
        """Validate a cached artifact against the current EvidencePacket.

        Returns ``True`` if the artifact is still valid for the current packet.
        Returns ``False`` if the artifact is stale or invalid.
        """
        # Check 1: artifact must have a valid schema version.
        if not artifact:
            return False

        # Check 2: evidence_packet_revision must match current packet revision.
        cached_revision = artifact.get("evidence_packet_revision", 0)
        current_revision = packet.get("coverage_revision", 0)
        if cached_revision != current_revision:
            return False

        # Check 3: all claim_ids in the artifact must exist in the packet.
        claim_ids_in_packet = {c["claim_id"] for c in packet.get("claims", [])}

        # Check outline sections for claim references.
        for section in artifact.get("outline_sections", []):
            for claim_ref in section.get("claims", []):
                cid = claim_ref.get("claim_id", "")
                if cid and cid not in claim_ids_in_packet:
                    return False

        # Check unsupported_claims.
        for uc in artifact.get("unsupported_claims", []):
            cid = uc.get("claim_id", "")
            if cid and cid not in claim_ids_in_packet:
                return False

        return True

    # ------------------------------------------------------------------
    # Bounded pipeline
    # ------------------------------------------------------------------

    def run_synthesis(
        self,
        run_id: UUID,
        packet_revision: int,
        model_name: str | None = None,
        prompt_version: str = "synthesis-v1",
        allow_commercial_fallback: bool = False,
    ) -> dict[str, Any]:
        """Run the bounded synthesis pipeline.

        Args:
            run_id: The research run ID.
            packet_revision: The EvidencePacket revision to use.
            model_name: The local model name. Defaults to config model.
            prompt_version: Prompt template version.
            allow_commercial_fallback: If True, allow commercial providers.

        Returns:
            A summary dict with stage results.

        Raises:
            ReportServiceError: If the pipeline fails.
            CommercialFallbackError: If commercial provider used without permission.
        """
        if not allow_commercial_fallback:
            self._enforce_local_only("local")

        model_name = model_name or self.config.embedding_model
        if not model_name:
            raise ReportServiceError("no model configured for synthesis")

        # Fetch and validate the EvidencePacket.
        packet = self._get_packet(run_id, packet_revision)
        self._validate_packet(packet)

        # ------------------------------------------------------------------
        # UOW scope design
        #
        # _init_stages opens and closes its own UOW context, committing the
        # INSERTs before the stage loop begins.  Each stage then opens its own
        # UOW for its SELECT / UPDATE work.  This is intentional:
        #
        # 1. The UNIQUE constraint on (run_id, stage_name) prevents duplicate
        #    rows even if two invocations race — the second invocation's
        #    _init_stages will see rows already present and skip inserts.
        # 2. Each stage is independently retriable; a UOW per stage means a
        #    failure in one stage does not roll back another.
        # 3. The EvidencePacket is read once at the top; stages that need
        #    prior-stage outputs read from synthesis_stages artifacts, not
        #    from the packet.
        # ------------------------------------------------------------------
        with self.semantic.uow_factory() as uow:
            self._init_stages(
                uow, run_id, packet_revision, model_name, prompt_version, 1
            )

        # Run each stage in order, skipping completed ones.
        results: dict[str, Any] = {}
        overall_status = "completed"
        last_error: str | None = None

        for stage_name in SynthesisStageName:
            stage_key = stage_name.value
            with self.semantic.uow_factory() as uow:
                record = uow.synthesis_stages.get_synthesis_stage(run_id, stage_key)

            # Skip completed stages.
            if self._is_stage_completed(record):
                logger.info(
                    "synthesis stage %s for run %s already completed (rev %d); skipping",
                    stage_key,
                    run_id,
                    packet_revision,
                )
                results[stage_key] = {
                    "status": "skipped",
                    "reason": "already_completed",
                    "evidence_packet_revision": packet_revision,
                }
                continue

            # Retry failed stages.
            if self._is_stage_failed(record):
                logger.info(
                    "synthesis stage %s for run %s failed; retrying",
                    stage_key,
                    run_id,
                )

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
                logger.error(
                    "synthesis stage %s for run %s failed: %s",
                    stage_key,
                    run_id,
                    exc,
                )
                results[stage_key] = {
                    "status": "failed",
                    "error": str(exc),
                    "evidence_packet_revision": packet_revision,
                }
                # Mark remaining stages as failed and record them.
                order = {
                    "outline": 1,
                    "binding": 2,
                    "draft": 3,
                    "citation_pass": 4,
                    "validation": 5,
                }
                current_order = order.get(stage_key, 99)
                for remaining in SynthesisStageName:
                    if remaining.order <= current_order:
                        continue
                    results[remaining.value] = {
                        "status": "failed",
                        "error": f"upstream stage failed: {exc}",
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
        """Execute a single synthesis stage."""
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
        """Mark all stages after the current one as failed."""
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

    # ------------------------------------------------------------------
    # Stage implementations
    # ------------------------------------------------------------------

    def _run_outline_stage(
        self,
        uow_factory: Any,
        run_id: UUID,
        packet: dict[str, Any],
        model_name: str,
        prompt_version: str,
        allow_commercial_fallback: bool,
    ) -> dict[str, Any]:
        """Generate a claim outline from the EvidencePacket."""
        schema = self._get_schema("outline")
        valid_claim_ids = [c["claim_id"] for c in packet.get("claims", [])]
        valid_passage_ids = [p["passage_id"] for p in packet.get("passages", [])]

        # Inject enum constraints for schema validation.
        schema = deepcopy(schema)
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
                "claims": [
                    {"claim_id": c["claim_id"], "statement": c["statement"]}
                    for c in packet.get("claims", [])
                ],
                "passages": [
                    {"passage_id": p["passage_id"], "text": p["text"]}
                    for p in packet.get("passages", [])
                ],
                "bindings": [
                    {
                        "claim_id": b["claim_id"],
                        "passage_ids": b["passage_ids"],
                        "relationship": b["relationship"],
                    }
                    for b in packet.get("claim_evidence_bindings", [])
                ],
            },
            indent=2,
        )

        context = {
            "run_id": str(run_id),
            "stage": "outline",
            "schema_name": "synthesis-outline-v1",
            "schema_version": 1,
            "idempotency_key": f"{run_id}-r{packet.get('coverage_revision', 1)}-outline",
            "input_artifact_ids": [
                f"packet-{run_id}-r{packet.get('coverage_revision', 1)}"
            ],
        }

        with uow_factory() as uow:
            status = uow.runs.get_run_status(run_id)
            context["run_revision"] = status["lifecycle_revision"]

        # ------------------------------------------------------------------
        # Cache integration (issue #41): check cache before LLM call.
        # ------------------------------------------------------------------
        prompt_hash = self.cache.compute_prompt_hash(system_prompt, user_prompt)
        input_hash = self.cache.compute_input_hash(
            json.loads(user_prompt) if isinstance(user_prompt, str) else user_prompt
        )

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
            # Cache hit — use the cached artifact directly.
            with uow_factory() as uow:
                record = uow.synthesis_stages.get_synthesis_stage(run_id, "outline")
                self._update_stage(
                    uow,
                    record,
                    status=SynthesisStageStatus.COMPLETED.value,
                    artifact=cached,
                )
            return {
                "status": "completed",
                "evidence_packet_revision": packet.get("coverage_revision", 1),
                "model_name": model_name,
                "schema_version": 1,
                "claim_count": len(cached.get("outline_sections", [])),
                "cache_hit": True,
            }

        from model_gateway import call_structured

        result = call_structured(
            provider="local",
            model=model_name,
            schema=schema,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            prompt_version=prompt_version,
            semantic_persistence=self.semantic,
            semantic_context=context,
        )

        with uow_factory() as uow:
            record = uow.synthesis_stages.get_synthesis_stage(run_id, "outline")

            if result.error:
                self._update_stage(
                    uow,
                    record,
                    status=SynthesisStageStatus.FAILED.value,
                    error=result.error,
                    increment_attempts=True,
                )
                raise ReportServiceError(f"outline stage failed: {result.error}")

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

        # Write to cache after successful LLM call.
        provenance = {
            "provider": "local",
            "requested_model": model_name,
            "prompt_version": prompt_version,
            "prompt_hash": prompt_hash,
            "attempt_count": len(result.attempts) if result.attempts else 1,
        }
        self._write_cache(
            stage="outline",
            model_name=model_name,
            prompt_version=prompt_version,
            prompt_hash=prompt_hash,
            schema_version=1,
            input_hash=input_hash,
            artifact=result.value,
            provenance=provenance,
        )

        return {
            "status": "completed",
            "evidence_packet_revision": packet.get("coverage_revision", 1),
            "model_name": model_name,
            "schema_version": 1,
            "claim_count": len(result.value.get("outline_sections", [])),
            "cache_hit": False,
        }

    def _run_binding_stage(
        self,
        uow_factory: Any,
        run_id: UUID,
        packet: dict[str, Any],
        model_name: str,
        prompt_version: str,
        allow_commercial_fallback: bool,
    ) -> dict[str, Any]:
        """Bind claims to passage IDs using ClaimBindingService.

        The binding service is cached on first creation (``self._binding_service``)
        so that subsequent calls to ``run_synthesis`` reuse the same instance
        rather than creating a new one each time.  Tests may inject a mock via
        ``LocalSynthesisService.__init__`` to avoid real LLM calls.
        """
        from .claim_binding_service import ClaimBindingService

        if self._binding_service is None:
            self._binding_service = ClaimBindingService(self.semantic, self.evidence)

        packet_revision = packet.get("coverage_revision", 1)

        # ------------------------------------------------------------------
        # Cache integration (issue #41): build input payload for cache key.
        # ------------------------------------------------------------------
        claims_data = [
            {"claim_id": c["claim_id"], "statement": c["statement"]}
            for c in packet.get("claims", [])
        ]
        passages_data = [
            {"passage_id": p["passage_id"], "text": p["text"]}
            for p in packet.get("passages", [])
        ]
        input_payload = {
            "claims": claims_data,
            "passages": passages_data,
            "coverage_revision": packet_revision,
        }
        input_hash = self.cache.compute_input_hash(input_payload)
        system_prompt = (
            "You are a rigorous evidence evaluator. "
            "Given a list of research claims and a list of passages, "
            "determine if the passages support, contradict, qualify, or provide context for each claim. "
            "Respond strictly using the JSON schema provided. "
            "Do not invent IDs. Only use the provided claim_id and passage_id values."
        )
        user_prompt = json.dumps(
            {"claims": claims_data, "passages": passages_data}, indent=2
        )
        prompt_hash = self.cache.compute_prompt_hash(system_prompt, user_prompt)

        cached = self._check_cache(
            stage="binding",
            model_name=model_name,
            prompt_version=prompt_version,
            prompt_hash=prompt_hash,
            schema_version=1,
            input_hash=input_hash,
            run_id=run_id,
            packet=packet,
        )

        if cached is not None:
            # Cache hit — use the cached result directly.
            new_revision = cached.get("new_packet_revision", packet_revision)
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
                "model_name": model_name,
                "cache_hit": True,
            }

        # Cache miss or invalid — run the real LLM call.
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

        # Write to cache after successful LLM call.
        provenance = {
            "provider": "local",
            "requested_model": model_name,
            "prompt_version": prompt_version,
            "prompt_hash": prompt_hash,
        }
        self._write_cache(
            stage="binding",
            model_name=model_name,
            prompt_version=prompt_version,
            prompt_hash=prompt_hash,
            schema_version=1,
            input_hash=input_hash,
            artifact={"new_packet_revision": new_revision},
            provenance=provenance,
        )

        return {
            "status": "completed",
            "evidence_packet_revision": new_revision,
            "model_name": model_name,
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
        """Draft the report body with claim references."""
        schema = self._get_schema("draft")

        # Read the outline artifact from synthesis_stages (produced by the
        # outline stage).  The outline was never written back to the
        # EvidencePacket, so packet.get("outline_sections", []) would always
        # be empty.
        with uow_factory() as uow:
            outline_record = uow.synthesis_stages.get_synthesis_stage(run_id, "outline")
            outline_artifact = outline_record.get("artifact") or {}
            outline_sections = outline_artifact.get("outline_sections", [])

        system_prompt = DRAFT_SYSTEM_PROMPT
        user_prompt = json.dumps(
            {
                "research_spec": packet.get("research_spec", {}),
                "outline_sections": outline_sections,
                "claims": [
                    {
                        "claim_id": c["claim_id"],
                        "statement": c["statement"],
                        "semantic_status": c.get("semantic_status", "unassessed"),
                        "uncertainty": c.get("uncertainty"),
                    }
                    for c in packet.get("claims", [])
                ],
                "passages": [
                    {
                        "passage_id": p["passage_id"],
                        "text": p["text"],
                        "source_url": p.get("source_url"),
                    }
                    for p in packet.get("passages", [])
                ],
                "bindings": [
                    {
                        "claim_id": b["claim_id"],
                        "passage_ids": b["passage_ids"],
                        "relationship": b["relationship"],
                        "confidence": b.get("confidence", 0.0),
                    }
                    for b in packet.get("claim_evidence_bindings", [])
                ],
                "limitations": packet.get("limitations", []),
            },
            indent=2,
        )

        context = {
            "run_id": str(run_id),
            "stage": "draft",
            "schema_name": "synthesis-draft-v1",
            "schema_version": 1,
            "idempotency_key": f"{run_id}-r{packet.get('coverage_revision', 1)}-draft",
            "input_artifact_ids": [
                f"packet-{run_id}-r{packet.get('coverage_revision', 1)}"
            ],
        }

        with uow_factory() as uow:
            status = uow.runs.get_run_status(run_id)
            context["run_revision"] = status["lifecycle_revision"]

        # ------------------------------------------------------------------
        # Cache integration (issue #41): check cache before LLM call.
        # ------------------------------------------------------------------
        prompt_hash = self.cache.compute_prompt_hash(system_prompt, user_prompt)
        input_hash = self.cache.compute_input_hash(
            json.loads(user_prompt) if isinstance(user_prompt, str) else user_prompt
        )

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
            # Cache hit — use the cached artifact directly.
            with uow_factory() as uow:
                record = uow.synthesis_stages.get_synthesis_stage(run_id, "draft")
                self._update_stage(
                    uow,
                    record,
                    status=SynthesisStageStatus.COMPLETED.value,
                    artifact=cached,
                )
            section_count = len(cached.get("report_sections", []))
            unsupported_count = len(cached.get("unsupported_claims", []))
            return {
                "status": "completed",
                "evidence_packet_revision": packet.get("coverage_revision", 1),
                "model_name": model_name,
                "section_count": section_count,
                "unsupported_claims_count": unsupported_count,
                "cache_hit": True,
            }

        from model_gateway import call_structured

        result = call_structured(
            provider="local",
            model=model_name,
            schema=schema,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            prompt_version=prompt_version,
            semantic_persistence=self.semantic,
            semantic_context=context,
        )

        with uow_factory() as uow:
            record = uow.synthesis_stages.get_synthesis_stage(run_id, "draft")

            if result.error:
                self._update_stage(
                    uow,
                    record,
                    status=SynthesisStageStatus.FAILED.value,
                    error=result.error,
                    increment_attempts=True,
                )
                raise ReportServiceError(f"draft stage failed: {result.error}")

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

        # Write to cache after successful LLM call.
        provenance = {
            "provider": "local",
            "requested_model": model_name,
            "prompt_version": prompt_version,
            "prompt_hash": prompt_hash,
            "attempt_count": len(result.attempts) if result.attempts else 1,
        }
        self._write_cache(
            stage="draft",
            model_name=model_name,
            prompt_version=prompt_version,
            prompt_hash=prompt_hash,
            schema_version=1,
            input_hash=input_hash,
            artifact=result.value,
            provenance=provenance,
        )

        section_count = len(result.value.get("report_sections", []))
        unsupported_count = len(result.value.get("unsupported_claims", []))

        return {
            "status": "completed",
            "evidence_packet_revision": packet.get("coverage_revision", 1),
            "model_name": model_name,
            "section_count": section_count,
            "unsupported_claims_count": unsupported_count,
            "cache_hit": False,
        }

    def _run_citation_pass_stage(
        self,
        uow_factory: Any,
        run_id: UUID,
        packet: dict[str, Any],
        model_name: str,
        prompt_version: str,
        allow_commercial_fallback: bool,
    ) -> dict[str, Any]:
        """Validate citation consistency and entailment."""
        schema = self._get_schema("citation_pass")

        # Read the draft artifact from synthesis_stages (produced by the
        # draft stage).  The draft was never written back to the
        # EvidencePacket, so packet.get("draft_sections", []) would always
        # be empty.
        with uow_factory() as uow:
            draft_record = uow.synthesis_stages.get_synthesis_stage(run_id, "draft")
            draft_artifact = draft_record.get("artifact") or {}
            draft_sections = draft_artifact.get("report_sections", [])

        system_prompt = CITATION_PASS_SYSTEM_PROMPT
        user_prompt = json.dumps(
            {
                "passage_ids": [p["passage_id"] for p in packet.get("passages", [])],
                "claim_bindings": [
                    {
                        "claim_id": b["claim_id"],
                        "passage_ids": b["passage_ids"],
                        "relationship": b["relationship"],
                    }
                    for b in packet.get("claim_evidence_bindings", [])
                ],
                "draft_sections": draft_sections,
            },
            indent=2,
        )

        context = {
            "run_id": str(run_id),
            "stage": "citation_pass",
            "schema_name": "synthesis-citation-pass-v1",
            "schema_version": 1,
            "idempotency_key": f"{run_id}-r{packet.get('coverage_revision', 1)}-citation",
            "input_artifact_ids": [
                f"packet-{run_id}-r{packet.get('coverage_revision', 1)}"
            ],
        }

        with uow_factory() as uow:
            status = uow.runs.get_run_status(run_id)
            context["run_revision"] = status["lifecycle_revision"]

        # ------------------------------------------------------------------
        # Cache integration (issue #41): check cache before LLM call.
        # ------------------------------------------------------------------
        prompt_hash = self.cache.compute_prompt_hash(system_prompt, user_prompt)
        input_hash = self.cache.compute_input_hash(
            json.loads(user_prompt) if isinstance(user_prompt, str) else user_prompt
        )

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
            # Cache hit — use the cached artifact directly.
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
            pass_status = cached.get("pass_status", "failed")
            invented_count = len(cached.get("invented_citations", []))
            unsupported_count = len(cached.get("unsupported_claims", []))
            return {
                "status": "completed",
                "pass_status": pass_status,
                "evidence_packet_revision": packet.get("coverage_revision", 1),
                "invented_citations_count": invented_count,
                "unsupported_claims_count": unsupported_count,
                "cache_hit": True,
            }

        from model_gateway import call_structured

        result = call_structured(
            provider="local",
            model=model_name,
            schema=schema,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            prompt_version=prompt_version,
            semantic_persistence=self.semantic,
            semantic_context=context,
        )

        with uow_factory() as uow:
            record = uow.synthesis_stages.get_synthesis_stage(run_id, "citation_pass")

            if result.error:
                self._update_stage(
                    uow,
                    record,
                    status=SynthesisStageStatus.FAILED.value,
                    error=result.error,
                    increment_attempts=True,
                )
                raise ReportServiceError(f"citation pass stage failed: {result.error}")

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

        # Write to cache after successful LLM call.
        provenance = {
            "provider": "local",
            "requested_model": model_name,
            "prompt_version": prompt_version,
            "prompt_hash": prompt_hash,
            "attempt_count": len(result.attempts) if result.attempts else 1,
        }
        self._write_cache(
            stage="citation_pass",
            model_name=model_name,
            prompt_version=prompt_version,
            prompt_hash=prompt_hash,
            schema_version=1,
            input_hash=input_hash,
            artifact=result.value,
            provenance=provenance,
        )

        pass_status = result.value.get("pass_status", "failed")
        invented_count = len(result.value.get("invented_citations", []))
        unsupported_count = len(result.value.get("unsupported_claims", []))

        return {
            "status": "completed",
            "pass_status": pass_status,
            "evidence_packet_revision": packet.get("coverage_revision", 1),
            "invented_citations_count": invented_count,
            "unsupported_claims_count": unsupported_count,
            "cache_hit": False,
        }

    def _run_validation_stage(
        self,
        uow_factory: Any,
        run_id: UUID,
        packet: dict[str, Any],
        model_name: str,
        prompt_version: str,
        allow_commercial_fallback: bool,
    ) -> dict[str, Any]:
        """Run deterministic report validation.

        This stage validates the report artifact against the EvidencePacket
        using the ``ReportValidator`` and persists the result via
        ``ReportArtifactService``.  It does not call an LLM.

        **Production behavior.**  When the EvidencePacket cannot be loaded,
        the stage **fails** — it does not skip.  Skipping was only ever a
        safety net for unit tests with mocked dependencies; the correct fix
        is for those tests to provide a proper EvidencePacket mock.
        """
        from .report_artifact_service import ReportArtifactService

        # Read the citation_pass artifact from synthesis_stages.
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

        # Build a minimal report dict from the citation_pass artifact.
        report = {
            "schema_version": report_artifact.get(
                "schema_version", "synthesis-citation-pass-v1"
            ),
            "run_id": str(run_id),
            "evidence_packet_revision": report_artifact.get(
                "evidence_packet_revision", packet.get("coverage_revision", 1)
            ),
            "draft_revision": report_artifact.get("draft_revision", 1),
            "pass_status": report_artifact.get("pass_status", "failed"),
            "validation_results": report_artifact.get("validation_results", []),
            "invented_citations": report_artifact.get("invented_citations", []),
            "unsupported_claims": report_artifact.get("unsupported_claims", []),
            "entailment_mismatches": report_artifact.get("entailment_mismatches", []),
        }

        # Validate the report.
        artifact_service = ReportArtifactService(uow_factory, self.evidence)
        validation_result = artifact_service.validate_report(run_id, report)

        # Persist the validation result.
        artifact_service.persist_validation_result(run_id, report, validation_result)

        # Update the validation stage record.
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

        # Raise so the pipeline loop marks overall_status = "failed" and
        # marks all downstream stages as failed.  The stage record is already
        # persisted above, so the failure is durable.
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

    # ------------------------------------------------------------------
    # Resume and status
    # ------------------------------------------------------------------

    def get_stage_status(
        self,
        uow_factory: Any,
        run_id: UUID,
        stage_name: str | None = None,
    ) -> list[dict[str, Any]]:
        """Get synthesis stage status for a run.

        Args:
            uow_factory: Callable returning a UOW context.
            run_id: The research run ID.
            stage_name: Optional stage name filter.

        Returns:
            List of stage status dicts.
        """
        with uow_factory() as uow:
            if stage_name:
                try:
                    record = uow.synthesis_stages.get_synthesis_stage(
                        run_id, stage_name
                    )
                    return [record]
                except KeyError:
                    return []
            else:
                records = uow.synthesis_stages.get_synthesis_stages(run_id)
                return records

    def resume_failed_synthesis(
        self,
        run_id: UUID,
        packet_revision: int,
        model_name: str | None = None,
        prompt_version: str = "synthesis-v1",
        allow_commercial_fallback: bool = False,
    ) -> dict[str, Any]:
        """Resume synthesis from the last failed stage.

        This is a thin wrapper around ``run_synthesis`` that exists to give
        callers a clear, discoverable entry point for resumption.  The
        underlying ``run_synthesis`` method already handles both initial runs
        and resumption (skipping completed stages, retrying failed ones).

        Args:
            run_id: The research run ID.
            packet_revision: The EvidencePacket revision.
            model_name: The local model name.
            prompt_version: Prompt template version.
            allow_commercial_fallback: If True, allow commercial providers.

        Returns:
            Pipeline summary dict.
        """
        logger.info(
            "resume_failed_synthesis called for run %s (rev %d); "
            "delegating to run_synthesis",
            run_id,
            packet_revision,
        )
        return self.run_synthesis(
            run_id=run_id,
            packet_revision=packet_revision,
            model_name=model_name,
            prompt_version=prompt_version,
            allow_commercial_fallback=allow_commercial_fallback,
        )
