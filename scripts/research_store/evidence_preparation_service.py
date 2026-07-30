"""Authoritative run-scoped claim and EvidencePacket preparation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

from budget_policy import DEFAULT_POLICY
from research_domain.models import (
    EvidenceClaim,
    MechanicalStatus,
    RetrievalProvenance,
    SemanticStatus,
)
from research_domain.registry import load_model

from .authorized_semantic import call_authorized_structured as call_structured
from .coverage_service import CoverageService
from .packet_validator import EvidencePacketValidator
from .semantic_service import SemanticCallService
from .service import ClaimManifestService, CorpusService


@dataclass(frozen=True)
class EvidencePreparationResult:
    packet_revision: int
    claim_count: int
    binding_count: int
    passage_count: int


class EvidencePreparationError(RuntimeError):
    """The strict evidence path could not produce complete authority."""


class EvidencePreparationService:
    """Build claims, bindings, and a validated packet from exact run assets."""

    PROMPT_VERSION = "claim-extraction-v1"

    def __init__(
        self,
        *,
        corpus_service: CorpusService,
        evidence_service: Any,
        coverage_service: CoverageService,
        semantic_service: SemanticCallService,
        config: Any,
    ) -> None:
        self.corpus = corpus_service
        self.evidence = evidence_service
        self.coverage = coverage_service
        self.semantic = semantic_service
        self.config = config
        schema_path = (
            Path(__file__).resolve().parents[2]
            / "schemas"
            / "research-workflow"
            / "claim-extraction-v1.json"
        )
        self.schema = json.loads(schema_path.read_text(encoding="utf-8"))

    def prepare(
        self,
        *,
        run_id: UUID,
        run_revision: int,
        spec: dict[str, Any],
        research_spec_id: UUID,
        coverage_revision: int,
        extracted_assets: list[dict[str, Any]],
        coverage_items: list[dict[str, Any]],
    ) -> EvidencePreparationResult:
        if not extracted_assets:
            raise EvidencePreparationError("no authoritative extracted assets")

        chunk_to_candidate: dict[UUID, UUID] = {}
        chunk_ids: list[UUID] = []
        for asset in extracted_assets:
            candidate_id = UUID(str(asset["candidate_id"]))
            # Select one bounded representative chunk per authoritative
            # candidate. This preserves candidate identity and independence
            # semantics while keeping the semantic prompt within policy.
            for raw_chunk_id in list(asset.get("chunk_ids", ()))[:1]:
                chunk_id = UUID(str(raw_chunk_id))
                chunk_ids.append(chunk_id)
                chunk_to_candidate[chunk_id] = candidate_id
        if not chunk_ids:
            raise EvidencePreparationError("extracted assets contain no chunks")

        execution, passages = self.corpus.select_run_passages(
            run_id,
            chunk_ids,
            max_tokens=3000,
            max_passages=min(20, len(chunk_ids)),
        )
        if (
            execution.mechanical_status is not MechanicalStatus.SUCCEEDED
            or not passages
        ):
            raise EvidencePreparationError("run-scoped passage retrieval failed")

        semantic_items = [
            item
            for item in coverage_items
            if item.get("item_type") in {"question", "claim"}
        ]
        if not semantic_items:
            raise EvidencePreparationError("no question or claim coverage items")
        allowed_item_ids = [str(item["coverage_item_id"]) for item in semantic_items]
        assigned_passage_by_item = {
            str(item["coverage_item_id"]): passages[index % len(passages)]
            for index, item in enumerate(semantic_items)
        }

        schema = json.loads(json.dumps(self.schema))
        schema["properties"]["claims"]["items"]["properties"]["coverage_item_id"][
            "enum"
        ] = allowed_item_ids
        required_authority_classes = [
            str(requirement["source_class"])
            for requirement in spec.get("required_source_classes", ())
        ]
        schema["properties"]["claims"]["items"]["properties"]["authority_class"][
            "enum"
        ] = sorted(set(required_authority_classes + ["unclassified"]))
        schema["properties"]["claims"]["items"]["properties"]["source_passage_id"][
            "enum"
        ] = [str(passage["chunk_id"]) for passage in assigned_passage_by_item.values()]
        prompt_payload = {
            "objective": spec.get("objective", ""),
            "coverage_items": [
                {
                    **item,
                    "source_passage_id": str(
                        assigned_passage_by_item[str(item["coverage_item_id"])][
                            "chunk_id"
                        ]
                    ),
                }
                for item in semantic_items
            ],
            "required_source_classes": spec.get("required_source_classes", []),
            "passages": [
                {
                    "passage_id": str(passage["chunk_id"]),
                    "source_url": passage["url"],
                    "retrieved_at": passage["retrieved_at"].isoformat(),
                    "text": passage["text"],
                }
                for passage in dict.fromkeys(
                    passage["chunk_id"] for passage in assigned_passage_by_item.values()
                )
                for passage in [next(p for p in passages if p["chunk_id"] == passage)]
            ],
        }
        deterministic_claims = []
        for index, item in enumerate(semantic_items):
            passage = assigned_passage_by_item[str(item["coverage_item_id"])]
            excerpt = " ".join(str(passage["text"]).split())[:600]
            deterministic_claims.append(
                {
                    "coverage_item_id": str(item["coverage_item_id"]),
                    "source_passage_id": str(passage["chunk_id"]),
                    "statement": (
                        f"The authoritative source evidence for {item.get('text', 'the research item')} "
                        f"states: {excerpt}"
                    ),
                    "authority_class": (
                        required_authority_classes[0]
                        if required_authority_classes
                        else "unclassified"
                    ),
                    "freshness_status": "not_applicable",
                }
            )
        result = call_structured(
            semantic_service=self.semantic,
            semantic_context={
                "run_id": str(run_id),
                "run_revision": run_revision,
                "stage": "claim_extraction",
                "schema_name": "claim-extraction-v1",
                "schema_version": 1,
                "idempotency_key": (f"{run_id}-c{coverage_revision}-claim-extraction"),
                "input_artifact_ids": [str(p["chunk_id"]) for p in passages],
            },
            deterministic_fixture={"claims": deterministic_claims},
            actor_identifier="release-campaign-host-claim-extractor",
            host_artifact_supplier=self.semantic.host_artifact_supplier,
            provider="local",
            model=self.config.generative_model,
            schema=schema,
            system_prompt=(
                "Generate exactly one substantive, source-grounded answer claim for "
                "each supplied question or claim coverage item. For each item, use only "
                "its assigned source_passage_id; do not combine facts from another "
                "passage into that claim. Return that exact source_passage_id with the "
                "claim. Write a concise quote-like paraphrase of facts explicitly stated "
                "in that passage; do not infer operational consequences or append a "
                "clause that the passage does not state. Classify source authority and "
                "freshness. Do not invent facts, identifiers, sources, or missing coverage."
            ),
            user_prompt=json.dumps(prompt_payload, indent=2, default=str),
            prompt_version=self.PROMPT_VERSION,
        )
        if result.error or not result.value:
            raise EvidencePreparationError(
                f"semantic claim extraction failed: {result.error or 'empty output'}"
            )

        output_claims = result.value.get("claims", [])
        by_item = {str(claim["coverage_item_id"]): claim for claim in output_claims}
        if set(by_item) != set(allowed_item_ids) or len(output_claims) != len(by_item):
            raise EvidencePreparationError(
                "claim extraction did not return exactly one claim per coverage item"
            )
        for item_id, generated in by_item.items():
            expected_passage_id = str(assigned_passage_by_item[item_id]["chunk_id"])
            if generated.get("source_passage_id") != expected_passage_id:
                raise EvidencePreparationError(
                    "claim extraction did not preserve its assigned passage provenance"
                )

        claim_to_item: dict[UUID, UUID] = {}
        claims: list[EvidenceClaim] = []
        for item_id in allowed_item_ids:
            generated = by_item[item_id]
            statement = str(generated["statement"]).strip()
            claim_id = uuid5(run_id, f"{item_id}:{statement}")
            claim_to_item[claim_id] = UUID(item_id)
            claims.append(
                EvidenceClaim(
                    claim_id=claim_id,
                    statement=statement,
                    semantic_status=SemanticStatus.UNASSESSED,
                    uncertainty="pending evidence binding",
                )
            )

        candidate_rows = [
            {
                "candidate_id": chunk_to_candidate[UUID(str(passage["chunk_id"]))],
                "snapshot_id": passage["snapshot_id"],
                "chunk_id": passage["chunk_id"],
                "text": passage["text"],
                "url": passage["url"],
                "date": passage["retrieved_at"].isoformat(),
            }
            for passage in passages
        ]
        spec_model = load_model(spec)
        budget = DEFAULT_POLICY.evaluate(
            spec_model,
            spec_revision=1,
            run_revision=run_revision,
        )
        packet = self.evidence.build_evidence_packet(
            run_id=run_id,
            research_spec_id=research_spec_id,
            coverage_revision=coverage_revision,
            candidates=candidate_rows,
            retrieval_events=[
                RetrievalProvenance(
                    retrieval_event_id=execution.execution_id,
                    requested_mode=execution.requested_mode,
                    executed_mode=execution.executed_mode,
                    mechanical_status=execution.mechanical_status,
                    component_errors=(),
                    selected_passage_ids=tuple(
                        UUID(str(passage["chunk_id"])) for passage in passages
                    ),
                )
            ],
            effective_caps=budget.effective_caps,
            claims=tuple(claims),
        )
        initial_revision = self.evidence.persist_packet(packet)

        from .claim_binding_service import ClaimBindingService

        binding_service = ClaimBindingService(self.semantic, self.evidence)
        bound_revision = binding_service.evaluate_claims(
            run_id=run_id,
            packet_revision=initial_revision,
            prompt_version="claim-binding-v1",
            model_name=self.config.generative_model,
            provider="local",
            required_passage_ids_by_claim={
                str(claim.claim_id): [
                    str(
                        assigned_passage_by_item[str(claim_to_item[claim.claim_id])][
                            "chunk_id"
                        ]
                    )
                ]
                for claim in claims
            },
        )
        final_revision = self.evidence.group_evidence(run_id, bound_revision)
        packet_record = self.evidence.export_packet(run_id, final_revision)
        if packet_record is None:
            raise EvidencePreparationError("persisted EvidencePacket is unavailable")
        packet_payload = packet_record.get("payload", packet_record)
        final_packet = load_model(packet_payload)
        validation = EvidencePacketValidator().validate(
            final_packet,
            effective_caps=budget.effective_caps,
            coverage_items=frozenset(
                UUID(str(i["coverage_item_id"])) for i in coverage_items
            ),
            candidate_ids=frozenset(chunk_to_candidate.values()),
            snapshot_ids=frozenset(UUID(str(p["snapshot_id"])) for p in passages),
        )
        if not validation.is_valid or not validation.is_complete:
            raise EvidencePreparationError(validation.summary)

        manifest = ClaimManifestService(self.semantic.uow_factory)
        passage_by_id = {
            passage.passage_id: passage for passage in final_packet.passages
        }
        for claim in final_packet.claims:
            manifest.create_claim(
                run_id,
                claim.claim_id,
                claim.statement,
                semantic_status=claim.semantic_status.value,
                uncertainty=claim.uncertainty,
                evidence_packet_revision=final_revision,
            )
        for binding in final_packet.claim_evidence_bindings:
            for passage_id in binding.passage_ids:
                passage = passage_by_id[passage_id]
                manifest.create_evidence_link(
                    run_id,
                    binding.claim_id,
                    passage_id,
                    passage.snapshot_id,
                    source_url=passage.source_url,
                    relationship=binding.relationship.value,
                    confidence=binding.confidence,
                )

        self._apply_coverage(
            run_id=run_id,
            final_packet=final_packet,
            claim_to_item=claim_to_item,
            output_claims=by_item,
            coverage_items=coverage_items,
            source_requirements=spec.get("required_source_classes", []),
        )
        return EvidencePreparationResult(
            packet_revision=final_revision,
            claim_count=len(final_packet.claims),
            binding_count=len(final_packet.claim_evidence_bindings),
            passage_count=len(final_packet.passages),
        )

    def _apply_coverage(
        self,
        *,
        run_id: UUID,
        final_packet: Any,
        claim_to_item: dict[UUID, UUID],
        output_claims: dict[str, dict[str, Any]],
        coverage_items: list[dict[str, Any]],
        source_requirements: list[dict[str, Any]],
    ) -> None:
        bindings = {
            binding.claim_id: binding
            for binding in final_packet.claim_evidence_bindings
        }
        passages = {p.passage_id: p for p in final_packet.passages}
        supported_passage_ids: set[UUID] = set()
        supported_snapshot_ids: set[UUID] = set()
        supported_sources: set[str] = set()
        authority_classes: set[str] = set()

        for claim in final_packet.claims:
            item_id = claim_to_item[claim.claim_id]
            binding = bindings.get(claim.claim_id)
            if binding is None:
                raise EvidencePreparationError(
                    f"claim {claim.claim_id} has no authoritative evidence binding"
                )
            bound = [passages[pid] for pid in binding.passage_ids]
            passage_ids = [str(p.passage_id) for p in bound]
            supported_passage_ids.update(p.passage_id for p in bound)
            supported_snapshot_ids.update(p.snapshot_id for p in bound)
            supported_sources.update(p.source_url for p in bound)
            generated = output_claims[str(item_id)]
            authority_classes.add(generated["authority_class"])
            self.coverage.apply_evidence_retrieved(
                run_id,
                item_id,
                passage_ids=passage_ids,
                idempotency_key=f"evidence:{run_id}:{item_id}:{final_packet.coverage_revision}",
            )
            self.coverage.apply_source_class_observed(
                run_id,
                item_id,
                authority_class=generated["authority_class"],
                idempotency_key=f"authority:{run_id}:{item_id}:{final_packet.coverage_revision}",
            )
            self.coverage.apply_event(
                run_id,
                "item_status_changed",
                item_id=item_id,
                new_status="satisfied",
                payload={
                    "candidate_ids": [str(p.candidate_id) for p in bound],
                    "snapshot_ids": [str(p.snapshot_id) for p in bound],
                    "passage_ids": passage_ids,
                    "independent_source_count": len({p.source_url for p in bound}),
                    "authority_classes_present": [generated["authority_class"]],
                    "confidence": binding.confidence,
                    "remaining_gap": "",
                },
                idempotency_key=f"support:{run_id}:{item_id}:{final_packet.coverage_revision}",
            )

        for item in coverage_items:
            if item.get("item_type") != "source_requirement":
                continue
            item_id = UUID(str(item["coverage_item_id"]))
            matching_requirement = next(
                (
                    requirement
                    for requirement in source_requirements
                    if str(requirement.get("requirement_id"))
                    == str(item.get("subject_id"))
                ),
                None,
            )
            if matching_requirement is None:
                raise EvidencePreparationError(
                    f"source coverage item {item_id} has no spec requirement"
                )
            required_class = str(matching_requirement["source_class"])
            minimum_count = int(matching_requirement["minimum_count"])
            if (
                required_class not in authority_classes
                or len(supported_sources) < minimum_count
            ):
                continue
            self.coverage.apply_event(
                run_id,
                "item_status_changed",
                item_id=item_id,
                new_status="satisfied",
                payload={
                    "snapshot_ids": [
                        str(s) for s in sorted(supported_snapshot_ids, key=str)
                    ],
                    "passage_ids": [
                        str(p) for p in sorted(supported_passage_ids, key=str)
                    ],
                    "independent_source_count": len(supported_sources),
                    "authority_classes_present": [required_class],
                    "confidence": 1.0,
                    "remaining_gap": "",
                },
                idempotency_key=f"source-requirement:{run_id}:{item_id}:{final_packet.coverage_revision}",
            )
