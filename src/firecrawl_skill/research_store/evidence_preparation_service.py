"""Authoritative run-scoped claim and EvidencePacket preparation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

from firecrawl_skill.research_domain.models import (
    EvidenceClaim,
    MechanicalStatus,
    RetrievalProvenance,
    SemanticStatus,
)
from firecrawl_skill.research_domain.registry import load_model
from firecrawl_skill.research_store.budget_policy import DEFAULT_POLICY

from .assessment.claims import ClaimManifestService
from .assessment.coverage import CoverageService
from .assessment.validation import EvidencePacketValidator
from .authorized_semantic import call_authorized_structured as call_structured
from .corpus_service import CorpusService
from .coverage_target_authority import (
    CoverageTargetAuthorityError,
    semantic_coverage_item,
)
from .exact_source_authority import (
    ExactSourceCoverageUnsatisfied,
    ExactSourceRequirementState,
    candidate_identity_map,
    canonical_source_identity,
    exact_source_binding_is_authoritative,
    requirement_candidate_groups,
)
from .semantic_service import SemanticCallService
from .temporal_coverage import (
    TemporalCoverageUnsatisfied,
    diagnose_temporal_coverage,
)
from .temporal_policy import (
    freshness_satisfied,
    has_temporal_obligations,
    normalize_temporal,
    passage_temporal_qualification,
)


@dataclass(frozen=True)
class EvidencePreparationResult:
    packet_revision: int
    claim_count: int
    binding_count: int
    passage_count: int


class EvidencePreparationError(RuntimeError):
    """The strict evidence path could not produce complete authority."""


def partition_temporal_passages(
    passages: list[dict[str, Any]],
    spec: dict[str, Any],
    *,
    now: datetime,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Partition claim-eligible passages from retained temporal context."""

    if not has_temporal_obligations(spec):
        return list(passages), []
    qualifying: list[dict[str, Any]] = []
    context_only: list[dict[str, Any]] = []
    for passage in passages:
        qualification = passage_temporal_qualification(passage, spec, now=now)
        target = qualifying if qualification.status == "satisfies" else context_only
        target.append(passage)
    return qualifying, context_only


def _evidence_candidate_row(
    passage: dict[str, Any],
    chunk_to_candidate: dict[UUID, UUID],
    spec: dict[str, Any],
    *,
    temporal_required: bool,
    now: datetime,
) -> dict[str, Any]:
    """Project one corpus passage into EvidenceService candidate semantics."""

    publication_date = (
        passage["published_at"].isoformat()
        if passage.get("published_at") is not None
        and hasattr(passage["published_at"], "isoformat")
        else None
    )
    row: dict[str, Any] = {
        "candidate_id": chunk_to_candidate[UUID(str(passage["chunk_id"]))],
        "snapshot_id": passage["snapshot_id"],
        "chunk_id": passage["chunk_id"],
        "text": passage["text"],
        "url": passage["url"],
        "date": publication_date,
    }
    if temporal_required:
        qualification = passage_temporal_qualification(passage, spec, now=now)
        # Presence is authoritative for temporal packets: None deliberately blocks
        # the legacy publication-date fallback for nonqualifying passages.
        row["freshness_date"] = (
            qualification.authoritative_time
            if qualification.status == "satisfies"
            else None
        )
    return row


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
            Path(__file__).resolve().parents[3]
            / "schemas"
            / "research-workflow"
            / "claim-extraction-v1.json"
        )
        self.schema = json.loads(schema_path.read_text(encoding="utf-8"))

    def _select_exact_source_passages(
        self,
        *,
        run_id: UUID,
        run_revision: int,
        coverage_revision: int,
        semantic_items: list[dict[str, Any]],
        exact_requirements: list[dict[str, Any]],
        exact_passages: dict[str, list[dict[str, Any]]],
        exact_groups: dict[str, frozenset[UUID]],
    ) -> dict[str, list[dict[str, Any]]]:
        """Select one directly usable passage per item and exact-source requirement."""

        max_pairs_per_call = 8
        if len(semantic_items) > 1:
            selected: dict[str, list[dict[str, Any]]] = {}
            for item in semantic_items:
                selected.update(
                    self._select_exact_source_passages(
                        run_id=run_id,
                        run_revision=run_revision,
                        coverage_revision=coverage_revision,
                        semantic_items=[item],
                        exact_requirements=exact_requirements,
                        exact_passages=exact_passages,
                        exact_groups=exact_groups,
                    )
                )
            return selected
        if len(exact_requirements) > max_pairs_per_call:
            item_id = str(semantic_items[0]["coverage_item_id"])
            selected = {item_id: []}
            for offset in range(0, len(exact_requirements), max_pairs_per_call):
                requirement_batch = exact_requirements[
                    offset : offset + max_pairs_per_call
                ]
                requirement_ids = {
                    str(requirement["requirement_id"])
                    for requirement in requirement_batch
                }
                partial = self._select_exact_source_passages(
                    run_id=run_id,
                    run_revision=run_revision,
                    coverage_revision=coverage_revision,
                    semantic_items=semantic_items,
                    exact_requirements=requirement_batch,
                    exact_passages={
                        requirement_id: exact_passages[requirement_id]
                        for requirement_id in requirement_ids
                    },
                    exact_groups={
                        requirement_id: exact_groups[requirement_id]
                        for requirement_id in requirement_ids
                    },
                )
                selected[item_id].extend(partial[item_id])
            return selected

        expected_pairs = [
            (str(item["coverage_item_id"]), str(requirement["requirement_id"]))
            for item in semantic_items
            for requirement in exact_requirements
        ]
        requirement_ids = [str(value["requirement_id"]) for value in exact_requirements]
        item_ids = [str(value["coverage_item_id"]) for value in semantic_items]
        all_passage_ids = sorted(
            {
                str(passage["chunk_id"])
                for passages in exact_passages.values()
                for passage in passages
            }
        )
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "selections": {
                    "type": "array",
                    "minItems": len(expected_pairs),
                    "maxItems": len(expected_pairs),
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "coverage_item_id": {"type": "string", "enum": item_ids},
                            "requirement_id": {
                                "type": "string",
                                "enum": requirement_ids,
                            },
                            "source_passage_id": {
                                "anyOf": [
                                    {"type": "string", "enum": all_passage_ids},
                                    {"type": "null"},
                                ]
                            },
                            "evidence_usable": {"type": "boolean"},
                            "rationale": {"type": "string", "maxLength": 240},
                        },
                        "required": [
                            "coverage_item_id",
                            "requirement_id",
                            "source_passage_id",
                            "evidence_usable",
                            "rationale",
                        ],
                    },
                }
            },
            "required": ["selections"],
        }
        prompt_requirements = []
        for requirement in exact_requirements:
            requirement_id = str(requirement["requirement_id"])
            prompt_requirements.append(
                {
                    "requirement_id": requirement_id,
                    "canonical_url": requirement["canonical_url"],
                    "passages": [
                        {
                            "passage_id": str(passage["chunk_id"]),
                            "source_url": passage.get("url")
                            or passage.get("source_url"),
                            "text": passage["text"],
                        }
                        for passage in exact_passages[requirement_id]
                    ],
                }
            )
        pair_fingerprint = hashlib.sha256(
            "|".join(
                f"{item_id}:{requirement_id}"
                for item_id, requirement_id in expected_pairs
            ).encode("utf-8")
        ).hexdigest()[:16]
        deterministic_fixture = {
            "selections": [
                {
                    "coverage_item_id": item_id,
                    "requirement_id": requirement_id,
                    "source_passage_id": None,
                    "evidence_usable": False,
                    "rationale": "semantic exact-source relevance was not evaluated",
                }
                for item_id, requirement_id in expected_pairs
            ]
        }
        result = call_structured(
            semantic_service=self.semantic,
            semantic_context={
                "run_id": str(run_id),
                "run_revision": run_revision,
                "stage": "exact_source_passage_selection",
                "schema_name": "exact-source-passage-selection-v1",
                "schema_version": 1,
                "idempotency_key": (
                    f"{run_id}-c{coverage_revision}-exact-source-passage-selection-"
                    f"{pair_fingerprint}"
                ),
                "input_artifact_ids": all_passage_ids,
            },
            deterministic_fixture=deterministic_fixture,
            actor_identifier="release-campaign-exact-source-selector",
            host_artifact_supplier=self.semantic.host_artifact_supplier,
            provider="local",
            model=self.config.generative_model,
            schema=schema,
            system_prompt=(
                "For every coverage item and exact-source requirement, determine whether "
                "one supplied passage from that exact source directly provides evidence "
                "usable to answer or evaluate the item. Select the single strongest "
                "passage when direct support exists. Otherwise set evidence_usable=false "
                "and source_passage_id=null. Do not use another source, infer facts not "
                "stated in the passage, or treat a link/mention as evidence from the "
                "required exact source. Return exactly one selection for every supplied "
                "coverage-item/requirement pair."
            ),
            user_prompt=json.dumps(
                {
                    "coverage_items": [
                        {
                            "coverage_item_id": str(item["coverage_item_id"]),
                            "text": item.get("text", ""),
                        }
                        for item in semantic_items
                    ],
                    "exact_source_requirements": prompt_requirements,
                },
                indent=2,
                default=str,
            ),
            max_output_tokens=min(4096, max(1024, len(expected_pairs) * 256)),
            expand_output_on_length=False,
            prompt_version="exact-source-passage-selection-v1",
        )
        if result.error or not result.value:
            raise EvidencePreparationError(
                "exact-source passage selection failed: "
                f"{result.error or 'empty output'}"
            )
        selections = result.value.get("selections", [])
        by_pair: dict[tuple[str, str], dict[str, Any]] = {}
        for selection in selections:
            pair = (
                str(selection.get("coverage_item_id")),
                str(selection.get("requirement_id")),
            )
            if pair not in expected_pairs:
                raise EvidencePreparationError(
                    f"exact-source passage selection returned unknown pair {pair}"
                )
            if pair in by_pair:
                raise EvidencePreparationError(
                    f"exact-source passage selection duplicated pair {pair}"
                )
            by_pair[pair] = selection
        if set(by_pair) != set(expected_pairs):
            raise EvidencePreparationError(
                "exact-source passage selection did not evaluate every required pair"
            )

        passage_by_requirement = {
            requirement_id: {
                str(passage["chunk_id"]): passage
                for passage in exact_passages[requirement_id]
            }
            for requirement_id in requirement_ids
        }
        selected_by_item = {item_id: [] for item_id in item_ids}
        unusable_requirements: set[str] = set()
        for item_id, requirement_id in expected_pairs:
            selection = by_pair[(item_id, requirement_id)]
            passage_id = selection.get("source_passage_id")
            usable = selection.get("evidence_usable") is True
            if not usable or passage_id is None:
                unusable_requirements.add(requirement_id)
                continue
            passage_id = str(passage_id)
            passage = passage_by_requirement[requirement_id].get(passage_id)
            if passage is None:
                raise EvidencePreparationError(
                    "exact-source passage selection crossed requirement authority"
                )
            selected_by_item[item_id].append(passage)

        if unusable_requirements:
            requirements_by_id = {
                str(value["requirement_id"]): value for value in exact_requirements
            }
            raise ExactSourceCoverageUnsatisfied(
                tuple(
                    ExactSourceRequirementState(
                        requirement_id=requirement_id,
                        canonical_url=str(
                            requirements_by_id[requirement_id]["canonical_url"]
                        ),
                        candidate_ids=tuple(
                            str(value)
                            for value in sorted(exact_groups[requirement_id], key=str)
                        ),
                        acquired=True,
                        selected=False,
                        satisfied=False,
                        reason="required_exact_source_not_evidentially_usable",
                    )
                    for requirement_id in sorted(unusable_requirements)
                )
            )
        return selected_by_item

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

        exact_requirements = list(spec.get("exact_source_requirements", ()))
        exact_items = {
            str(item.get("subject_id")): item
            for item in coverage_items
            if item.get("item_type") == "exact_source_requirement"
        }
        for requirement in exact_requirements:
            requirement_id = str(requirement["requirement_id"])
            if requirement_id not in exact_items:
                raise EvidencePreparationError(
                    f"exact-source requirement {requirement_id} has no coverage item"
                )

        initial_identities = candidate_identity_map(extracted_assets)
        initial_groups = requirement_candidate_groups(
            exact_requirements, initial_identities
        )
        exact_candidate_ids = {
            candidate_id
            for candidate_ids in initial_groups.values()
            for candidate_id in candidate_ids
        }
        ordered_assets = sorted(
            extracted_assets,
            key=lambda asset: (
                UUID(str(asset["candidate_id"])) not in exact_candidate_ids,
                int(asset.get("ordinal") or 0),
                str(asset.get("candidate_id")),
            ),
        )

        chunk_to_candidate: dict[UUID, UUID] = {}
        chunk_ids: list[UUID] = []
        if exact_requirements:
            # Exact authority needs a bounded passage pool, not an arbitrary
            # first-chunk representative. Interleave exact-source chunks so one
            # large document cannot starve another required exact source, then
            # use any remaining capacity for one contextual chunk per substitute.
            max_run_passages = 50
            max_run_tokens = 16000
            exact_assets = [
                asset
                for asset in ordered_assets
                if UUID(str(asset["candidate_id"])) in exact_candidate_ids
            ]
            contextual_assets = [
                asset
                for asset in ordered_assets
                if UUID(str(asset["candidate_id"])) not in exact_candidate_ids
            ]
            exact_chunks = [
                (
                    UUID(str(asset["candidate_id"])),
                    [UUID(str(value)) for value in asset.get("chunk_ids", ())],
                )
                for asset in exact_assets
            ]
            max_depth = max((len(values) for _, values in exact_chunks), default=0)
            for depth in range(max_depth):
                for candidate_id, values in exact_chunks:
                    if len(chunk_ids) >= max_run_passages:
                        break
                    if depth >= len(values):
                        continue
                    chunk_id = values[depth]
                    if chunk_id in chunk_to_candidate:
                        continue
                    chunk_ids.append(chunk_id)
                    chunk_to_candidate[chunk_id] = candidate_id
                if len(chunk_ids) >= max_run_passages:
                    break
            for asset in contextual_assets:
                if len(chunk_ids) >= max_run_passages:
                    break
                candidate_id = UUID(str(asset["candidate_id"]))
                for raw_chunk_id in list(asset.get("chunk_ids", ()))[:1]:
                    chunk_id = UUID(str(raw_chunk_id))
                    if chunk_id in chunk_to_candidate:
                        continue
                    chunk_ids.append(chunk_id)
                    chunk_to_candidate[chunk_id] = candidate_id
        else:
            max_run_passages = 20
            max_run_tokens = 3000
            for asset in ordered_assets:
                candidate_id = UUID(str(asset["candidate_id"]))
                # Preserve the established no-exact-source representative path.
                for raw_chunk_id in list(asset.get("chunk_ids", ()))[:1]:
                    chunk_id = UUID(str(raw_chunk_id))
                    chunk_ids.append(chunk_id)
                    chunk_to_candidate[chunk_id] = candidate_id
        if not chunk_ids:
            raise EvidencePreparationError("extracted assets contain no chunks")

        execution, passages = self.corpus.select_run_passages(
            run_id,
            chunk_ids,
            max_tokens=max_run_tokens,
            max_passages=min(max_run_passages, len(chunk_ids)),
        )
        if (
            execution.mechanical_status is not MechanicalStatus.SUCCEEDED
            or not passages
        ):
            raise EvidencePreparationError("run-scoped passage retrieval failed")

        temporal_required = has_temporal_obligations(spec)
        temporal_reference = datetime.now(timezone.utc)
        qualifying_passages, _context_only_passages = partition_temporal_passages(
            passages,
            spec,
            now=temporal_reference,
        )
        semantic_passages = qualifying_passages if temporal_required else passages
        try:
            semantic_items = [
                semantic_coverage_item(spec, item)
                for item in coverage_items
                if item.get("item_type") in {"question", "claim"}
            ]
        except CoverageTargetAuthorityError as exc:
            raise EvidencePreparationError(
                f"coverage semantic target is not bound to ResearchSpec: {exc}"
            ) from exc

        exact_groups: dict[str, frozenset[UUID]] = {}
        exact_passages: dict[str, list[dict[str, Any]]] = {}
        selected_exact_by_item: dict[str, list[dict[str, Any]]] = {}
        required_exact_passages: list[dict[str, Any]] = []
        if exact_requirements:
            identities = candidate_identity_map(
                extracted_assets,
                passages=passages,
                chunk_to_candidate=chunk_to_candidate,
            )
            exact_groups = requirement_candidate_groups(exact_requirements, identities)
            asset_by_candidate = {
                UUID(str(asset["candidate_id"])): asset for asset in extracted_assets
            }
            missing_states: list[ExactSourceRequirementState] = []
            for requirement in exact_requirements:
                requirement_id = str(requirement["requirement_id"])
                canonical_url = canonical_source_identity(requirement["canonical_url"])
                if canonical_url is None:
                    raise EvidencePreparationError(
                        f"exact-source requirement {requirement_id} has invalid canonical URL"
                    )
                candidate_ids = exact_groups[requirement_id]
                item_id = UUID(str(exact_items[requirement_id]["coverage_item_id"]))
                if not candidate_ids:
                    missing_states.append(
                        ExactSourceRequirementState(
                            requirement_id=requirement_id,
                            canonical_url=canonical_url,
                            reason="required_exact_source_not_acquired",
                        )
                    )
                    continue
                snapshots = {
                    str(asset_by_candidate[candidate_id]["snapshot_id"])
                    for candidate_id in candidate_ids
                    if candidate_id in asset_by_candidate
                    and asset_by_candidate[candidate_id].get("snapshot_id")
                }
                self.coverage.apply_event(
                    run_id,
                    "item_status_changed",
                    item_id=item_id,
                    item_type="exact_source_requirement",
                    subject_id=requirement_id,
                    new_status="acquired",
                    payload={
                        "candidate_ids": [
                            str(value) for value in sorted(candidate_ids, key=str)
                        ],
                        "snapshot_ids": sorted(snapshots),
                        "exact_source_identity": canonical_url,
                        "remaining_gap": "exact source acquired but not yet bound",
                    },
                    idempotency_key=(
                        f"exact-source-acquired:{run_id}:{item_id}:{coverage_revision}"
                    ),
                )
                matches = [
                    passage
                    for passage in semantic_passages
                    if chunk_to_candidate.get(UUID(str(passage["chunk_id"])))
                    in candidate_ids
                ]
                exact_passages[requirement_id] = matches
                if not matches:
                    missing_states.append(
                        ExactSourceRequirementState(
                            requirement_id=requirement_id,
                            canonical_url=canonical_url,
                            candidate_ids=tuple(
                                str(value) for value in sorted(candidate_ids, key=str)
                            ),
                            acquired=True,
                            reason=(
                                "required_exact_source_temporally_unqualified"
                                if temporal_required
                                else "required_exact_source_has_no_evidentiary_passage"
                            ),
                        )
                    )
                    continue
            if missing_states:
                raise ExactSourceCoverageUnsatisfied(tuple(missing_states))

        if temporal_required and not qualifying_passages:
            raise TemporalCoverageUnsatisfied(
                diagnose_temporal_coverage(
                    passages,
                    spec,
                    now=temporal_reference,
                )
            )

        if not semantic_items:
            raise EvidencePreparationError("no question or claim coverage items")

        if exact_requirements:
            selected_exact_by_item = self._select_exact_source_passages(
                run_id=run_id,
                run_revision=run_revision,
                coverage_revision=coverage_revision,
                semantic_items=semantic_items,
                exact_requirements=exact_requirements,
                exact_passages=exact_passages,
                exact_groups=exact_groups,
            )
            selected_by_chunk = {
                UUID(str(passage["chunk_id"])): passage
                for selected in selected_exact_by_item.values()
                for passage in selected
            }
            required_exact_passages = list(selected_by_chunk.values())

        if required_exact_passages:
            required_ids = {
                UUID(str(passage["chunk_id"])) for passage in required_exact_passages
            }
            passages = [
                *required_exact_passages,
                *[
                    passage
                    for passage in passages
                    if UUID(str(passage["chunk_id"])) not in required_ids
                ],
            ]
            semantic_passages = required_exact_passages

        allowed_item_ids = [str(item["coverage_item_id"]) for item in semantic_items]
        if exact_requirements:
            assigned_passage_by_item = {
                item_id: selected_exact_by_item[item_id][0]
                for item_id in allowed_item_ids
            }
            required_exact_passage_ids_by_item = {
                item_id: [
                    str(passage["chunk_id"])
                    for passage in selected_exact_by_item[item_id]
                ]
                for item_id in allowed_item_ids
            }
        else:
            assigned_passage_by_item = {
                str(item["coverage_item_id"]): semantic_passages[
                    index % len(semantic_passages)
                ]
                for index, item in enumerate(semantic_items)
            }
            required_exact_passage_ids_by_item = {}
        required_exact_passage_ids = [
            str(passage["chunk_id"]) for passage in required_exact_passages
        ]

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
            "temporal_policy": {
                "bounded": temporal_required,
                "publication_window": spec.get("time_window", {}),
                "freshness_requirements": spec.get("freshness_requirements", []),
                "retrieval_time_is_not_publication": True,
            },
            "passages": [
                {
                    "passage_id": str(passage["chunk_id"]),
                    "source_url": passage["url"],
                    "published_at": (
                        passage["published_at"].isoformat()
                        if passage.get("published_at") is not None
                        and hasattr(passage["published_at"], "isoformat")
                        else passage.get("published_at")
                    ),
                    "updated_at": passage.get("updated_at")
                    or passage.get("last_modified"),
                    "retrieved_at": passage["retrieved_at"].isoformat(),
                    "text": passage["text"],
                }
                for passage_id in dict.fromkeys(
                    passage["chunk_id"] for passage in assigned_passage_by_item.values()
                )
                for passage in [
                    next(p for p in passages if p["chunk_id"] == passage_id)
                ]
            ],
        }
        deterministic_claims = []
        for item in semantic_items:
            passage = assigned_passage_by_item[str(item["coverage_item_id"])]
            excerpt = " ".join(str(passage["text"]).split())[:600]
            deterministic_claims.append(
                {
                    "coverage_item_id": str(item["coverage_item_id"]),
                    "source_passage_id": str(passage["chunk_id"]),
                    "statement": (
                        "The authoritative source evidence for "
                        f"{item.get('text', 'the research item')} states: {excerpt}"
                    ),
                    "authority_class": (
                        required_authority_classes[0]
                        if required_authority_classes
                        else "unclassified"
                    ),
                    "freshness_status": (
                        "satisfied" if temporal_required else "not_applicable"
                    ),
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
            _evidence_candidate_row(
                passage,
                chunk_to_candidate,
                spec,
                temporal_required=temporal_required,
                now=temporal_reference,
            )
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
        if required_exact_passage_ids:
            included_ids = {str(passage.passage_id) for passage in packet.passages}
            missing_required = set(required_exact_passage_ids) - included_ids
            if missing_required:
                raise ExactSourceCoverageUnsatisfied(
                    tuple(
                        ExactSourceRequirementState(
                            requirement_id=str(requirement["requirement_id"]),
                            canonical_url=str(requirement["canonical_url"]),
                            candidate_ids=tuple(
                                str(value)
                                for value in sorted(
                                    exact_groups[str(requirement["requirement_id"])],
                                    key=str,
                                )
                            ),
                            acquired=True,
                            reason="required_exact_source_omitted_by_evidence_budget",
                        )
                        for requirement in exact_requirements
                    )
                )
        initial_revision = self.evidence.persist_packet(packet)

        from .assessment.binding import ClaimBindingService

        binding_service = ClaimBindingService(self.semantic, self.evidence)
        bound_revision = binding_service.evaluate_claims(
            run_id=run_id,
            packet_revision=initial_revision,
            prompt_version="claim-binding-v1",
            model_name=self.config.generative_model,
            provider="local",
            required_passage_ids_by_claim={
                str(claim.claim_id): (
                    list(
                        required_exact_passage_ids_by_item[
                            str(claim_to_item[claim.claim_id])
                        ]
                    )
                    if required_exact_passage_ids_by_item
                    else [
                        str(
                            assigned_passage_by_item[
                                str(claim_to_item[claim.claim_id])
                            ]["chunk_id"]
                        )
                    ]
                )
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
        if not validation.is_valid:
            raise EvidencePreparationError(validation.summary)
        if exact_requirements:
            evaluated = EvidencePacketValidator.EVALUATED_STATUSES
            passage_by_id = {
                passage.passage_id: passage for passage in final_packet.passages
            }
            bindings_by_claim: dict[UUID, list[Any]] = {}
            for binding in final_packet.claim_evidence_bindings:
                bindings_by_claim.setdefault(binding.claim_id, []).append(binding)

            def claim_has_authoritative_exact_binding(
                claim: Any, selected_passage_ids: set[UUID]
            ) -> bool:
                matching = [
                    binding
                    for binding in bindings_by_claim.get(claim.claim_id, ())
                    if any(
                        passage_id in selected_passage_ids
                        for passage_id in binding.passage_ids
                    )
                ]
                return (
                    claim.semantic_status in evaluated
                    and bool(matching)
                    and all(
                        exact_source_binding_is_authoritative(
                            claim.semantic_status, binding.relationship
                        )
                        for binding in matching
                    )
                )

            unsatisfied_states: list[ExactSourceRequirementState] = []
            for requirement in exact_requirements:
                requirement_id = str(requirement["requirement_id"])
                candidate_ids = exact_groups[requirement_id]
                selected_passage_ids = {
                    passage_id
                    for binding in final_packet.claim_evidence_bindings
                    for passage_id in binding.passage_ids
                    if passage_id in passage_by_id
                    and passage_by_id[passage_id].candidate_id in candidate_ids
                }
                all_claims_exact = all(
                    claim_has_authoritative_exact_binding(claim, selected_passage_ids)
                    for claim in final_packet.claims
                )
                if not selected_passage_ids or not all_claims_exact:
                    unsatisfied_states.append(
                        ExactSourceRequirementState(
                            requirement_id=requirement_id,
                            canonical_url=str(requirement["canonical_url"]),
                            candidate_ids=tuple(
                                str(value) for value in sorted(candidate_ids, key=str)
                            ),
                            acquired=True,
                            selected=bool(selected_passage_ids),
                            satisfied=False,
                            passage_ids=tuple(
                                str(value)
                                for value in sorted(selected_passage_ids, key=str)
                            ),
                            reason="required_exact_source_not_evidentially_usable",
                        )
                    )
            if unsatisfied_states:
                raise ExactSourceCoverageUnsatisfied(tuple(unsatisfied_states))
        blocking_warnings = [
            finding
            for finding in validation.warnings
            if temporal_required or finding.code != "NO_FRESHNESS_DATES"
        ]
        if blocking_warnings:
            raise EvidencePreparationError(
                f"packet is valid but incomplete ({len(blocking_warnings)} warnings)"
            )

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
            exact_source_requirements=exact_requirements,
            exact_candidate_groups=exact_groups,
            freshness_requirements=spec.get("freshness_requirements", []),
            corpus_passages={UUID(str(p["chunk_id"])): p for p in passages},
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
        exact_source_requirements: list[dict[str, Any]],
        exact_candidate_groups: dict[str, frozenset[UUID]],
        freshness_requirements: list[dict[str, Any]],
        corpus_passages: dict[UUID, dict[str, Any]],
    ) -> None:
        bindings: dict[UUID, list[Any]] = {}
        for binding in final_packet.claim_evidence_bindings:
            bindings.setdefault(binding.claim_id, []).append(binding)
        passages = {p.passage_id: p for p in final_packet.passages}
        supported_passage_ids: set[UUID] = set()
        supported_snapshot_ids: set[UUID] = set()
        supported_sources: set[str] = set()
        authority_classes: set[str] = set()

        for claim in final_packet.claims:
            item_id = claim_to_item[claim.claim_id]
            claim_bindings = bindings.get(claim.claim_id, [])
            if not claim_bindings:
                raise EvidencePreparationError(
                    f"claim {claim.claim_id} has no authoritative evidence binding"
                )
            bound_ids = list(
                dict.fromkeys(
                    passage_id
                    for binding in claim_bindings
                    for passage_id in binding.passage_ids
                )
            )
            bound = [passages[pid] for pid in bound_ids]
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
                    "confidence": min(binding.confidence for binding in claim_bindings),
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
                idempotency_key=(
                    f"source-requirement:{run_id}:{item_id}:"
                    f"{final_packet.coverage_revision}"
                ),
            )

        for item in coverage_items:
            if item.get("item_type") != "exact_source_requirement":
                continue
            item_id = UUID(str(item["coverage_item_id"]))
            requirement = next(
                (
                    value
                    for value in exact_source_requirements
                    if str(value.get("requirement_id")) == str(item.get("subject_id"))
                ),
                None,
            )
            if requirement is None:
                raise EvidencePreparationError(
                    f"exact-source coverage item {item_id} has no spec requirement"
                )
            candidate_ids = exact_candidate_groups[str(requirement["requirement_id"])]
            selected = [
                passage
                for passage_id in supported_passage_ids
                if (passage := passages.get(passage_id)) is not None
                and passage.candidate_id in candidate_ids
            ]
            if not selected:
                continue
            self.coverage.apply_event(
                run_id,
                "item_status_changed",
                item_id=item_id,
                item_type="exact_source_requirement",
                subject_id=str(requirement["requirement_id"]),
                new_status="satisfied",
                payload={
                    "candidate_ids": [
                        str(value) for value in sorted(candidate_ids, key=str)
                    ],
                    "snapshot_ids": [str(value.snapshot_id) for value in selected],
                    "passage_ids": [str(value.passage_id) for value in selected],
                    "independent_source_count": len(
                        {value.source_url for value in selected}
                    ),
                    "exact_source_identity": requirement["canonical_url"],
                    "confidence": 1.0,
                    "remaining_gap": "",
                },
                idempotency_key=(
                    f"exact-source-satisfied:{run_id}:{item_id}:"
                    f"{final_packet.coverage_revision}"
                ),
            )

        for item in coverage_items:
            if item.get("item_type") != "freshness_requirement":
                continue
            item_id = UUID(str(item["coverage_item_id"]))
            requirement = next(
                (
                    candidate
                    for candidate in freshness_requirements
                    if str(candidate.get("requirement_id"))
                    == str(item.get("subject_id"))
                ),
                None,
            )
            if requirement is None:
                raise EvidencePreparationError(
                    f"freshness coverage item {item_id} has no spec requirement"
                )
            max_age = requirement.get("max_age_days")
            if max_age is None:
                continue
            qualifying: list[UUID] = []
            has_temporal_signal = False
            for passage_id in supported_passage_ids:
                temporal = corpus_passages.get(passage_id)
                if temporal is None:
                    continue
                publication = normalize_temporal(temporal.get("published_at"))
                update = normalize_temporal(
                    temporal.get("updated_at") or temporal.get("last_modified")
                )
                has_temporal_signal = (
                    has_temporal_signal or publication is not None or update is not None
                )
                if freshness_satisfied(
                    published_at=publication,
                    updated_at=update,
                    max_age_days=int(max_age),
                ):
                    qualifying.append(passage_id)
            freshness_status = (
                "satisfied"
                if qualifying
                else "unsatisfied"
                if has_temporal_signal
                else "uncertain"
            )
            self.coverage.apply_freshness_observed(
                run_id,
                item_id,
                freshness_status=freshness_status,
                idempotency_key=(
                    f"freshness:{run_id}:{item_id}:{final_packet.coverage_revision}"
                ),
            )
            if not qualifying:
                continue
            snapshots = {
                passages[passage_id].snapshot_id
                for passage_id in qualifying
                if passage_id in passages
            }
            self.coverage.apply_event(
                run_id,
                "item_status_changed",
                item_id=item_id,
                new_status="satisfied",
                payload={
                    "snapshot_ids": [
                        str(value) for value in sorted(snapshots, key=str)
                    ],
                    "passage_ids": [
                        str(value) for value in sorted(qualifying, key=str)
                    ],
                    "confidence": 1.0,
                    "remaining_gap": "",
                    "max_age_days": int(max_age),
                    "temporal_authority": "publication_or_explicit_update",
                },
                idempotency_key=(
                    f"freshness-support:{run_id}:{item_id}:"
                    f"{final_packet.coverage_revision}"
                ),
            )
