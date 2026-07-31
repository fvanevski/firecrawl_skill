"""Semantic claim-to-passage binding service."""

import json
import logging
from copy import deepcopy
from uuid import UUID, uuid4

from research_domain.registry import load_model

from .authorized_semantic import call_authorized_structured as call_structured
from .evidence import EvidenceService
from .semantic_service import SemanticCallService

logger = logging.getLogger(__name__)


class ClaimBindingService:
    """Service for binding claims to evidence passages via LLM."""

    def __init__(
        self,
        semantic_service: SemanticCallService,
        evidence_service: EvidenceService,
    ):
        self.semantic = semantic_service
        self.evidence = evidence_service

        import pathlib

        schema_path = (
            pathlib.Path(__file__).parent.parent.parent
            / "schemas"
            / "research-workflow"
            / "claim-binding-v1.json"
        )
        with open(schema_path) as f:
            self.schema = json.load(f)

    def evaluate_claims(
        self,
        run_id: UUID,
        packet_revision: int,
        prompt_version: str,
        model_name: str,
        provider: str = "local",
        required_passage_ids_by_claim: dict[str, list[str]] | None = None,
    ) -> int:
        """Evaluate claims against an EvidencePacket using a semantic model.

        Calling this method replaces all prior claim-evidence bindings for the
        packet. Each evaluation produces a new packet revision.

        Returns:
            The revision of the newly persisted EvidencePacket.
        """
        packet_record = self.evidence.export_packet(run_id, packet_revision)
        if not packet_record:
            raise ValueError(f"EvidencePacket {run_id} r{packet_revision} not found")
        packet_dict = packet_record.get("payload", packet_record)

        passages = packet_dict.get("passages", [])
        claims = packet_dict.get("claims", [])

        if not claims:
            return packet_revision

        required_passage_ids_by_claim = required_passage_ids_by_claim or {}
        valid_claim_ids = [claim["claim_id"] for claim in claims]
        passage_by_id = {passage["passage_id"]: passage for passage in passages}
        unknown_required_claims = set(required_passage_ids_by_claim) - set(
            valid_claim_ids
        )
        if unknown_required_claims:
            raise ValueError(
                "required passage lineage has unknown claim IDs: "
                f"{sorted(unknown_required_claims)}"
            )
        unknown_required_passages = sorted(
            {
                passage_id
                for passage_ids in required_passage_ids_by_claim.values()
                for passage_id in passage_ids
                if passage_id not in passage_by_id
            }
        )
        if unknown_required_passages:
            raise ValueError(
                "required passage lineage has unknown passage IDs: "
                f"{unknown_required_passages}"
            )

        fully_scoped = all(
            required_passage_ids_by_claim.get(claim_id) for claim_id in valid_claim_ids
        )
        if fully_scoped:
            scoped_ids = {
                passage_id
                for claim_id in valid_claim_ids
                for passage_id in required_passage_ids_by_claim[claim_id]
            }
            prompt_passages = [
                passage for passage in passages if passage["passage_id"] in scoped_ids
            ]
        else:
            prompt_passages = passages
        if not prompt_passages:
            raise ValueError("claim binding has no eligible evidence passages")

        system_prompt = (
            "You are a rigorous evidence evaluator. Determine whether the supplied "
            "passages support, contradict, qualify, or contextualize each claim. "
            "Return exactly one compact evaluation for every claim. When a claim "
            "has required_passage_ids, return exactly one binding containing those "
            "exact IDs and no others. Otherwise use only the minimum passages needed. "
            "Never repeat identifiers. Keep uncertainty concise. Respond strictly "
            "using the supplied JSON schema and do not invent IDs."
        )

        user_prompt_data = {
            "claims": [
                {
                    "claim_id": c["claim_id"],
                    "statement": c["statement"],
                    "required_passage_ids": required_passage_ids_by_claim.get(
                        c["claim_id"], []
                    ),
                }
                for c in claims
            ],
            "passages": [
                {"passage_id": p["passage_id"], "text": p["text"]}
                for p in prompt_passages
            ],
        }
        user_prompt = json.dumps(user_prompt_data, indent=2)

        context = {
            "run_id": str(run_id),
            "stage": "claim_binding",
            "schema_name": "claim-binding-v1",
            "schema_version": 1,
            "idempotency_key": f"{run_id}-r{packet_revision}-binding",
            "input_artifact_ids": [f"packet-{run_id}-r{packet_revision}"],
        }

        with self.semantic.uow_factory() as uow:
            status = uow.runs.get_run_status(run_id=run_id)
            context["run_revision"] = status["lifecycle_revision"]

        schema = deepcopy(self.schema)
        valid_passage_ids = [p["passage_id"] for p in prompt_passages]
        schema["properties"]["evaluations"]["minItems"] = len(valid_claim_ids)
        schema["properties"]["evaluations"]["maxItems"] = len(valid_claim_ids)
        schema["properties"]["evaluations"]["items"]["properties"]["bindings"][
            "minItems"
        ] = 1
        schema["properties"]["evaluations"]["items"]["properties"]["claim_id"][
            "enum"
        ] = valid_claim_ids
        bindings_schema = schema["properties"]["evaluations"]["items"]["properties"][
            "bindings"
        ]
        bindings_schema["maxItems"] = (
            1 if fully_scoped else min(4, len(valid_passage_ids))
        )
        passage_ids_schema = bindings_schema["items"]["properties"]["passage_ids"]
        passage_ids_schema["items"]["enum"] = valid_passage_ids
        passage_ids_schema["maxItems"] = (
            max(
                len(required_passage_ids_by_claim.get(claim_id, ()))
                for claim_id in valid_claim_ids
            )
            if fully_scoped
            else min(4, len(valid_passage_ids))
        )
        bindings_schema["items"]["properties"]["uncertainty"]["maxLength"] = 240

        deterministic_fixture = {
            "evaluations": [
                {
                    "claim_id": claim["claim_id"],
                    "semantic_status": "supported",
                    "bindings": [
                        {
                            "passage_ids": list(
                                required_passage_ids_by_claim.get(
                                    claim["claim_id"],
                                    [
                                        prompt_passages[index % len(prompt_passages)][
                                            "passage_id"
                                        ]
                                    ],
                                )
                            ),
                            "relationship": "supports",
                            "confidence": 0.8,
                            "uncertainty": "deterministic debug fixture",
                        }
                    ],
                }
                for index, claim in enumerate(claims)
            ]
        }
        result = call_structured(
            semantic_service=self.semantic,
            semantic_context=context,
            deterministic_fixture=deterministic_fixture,
            actor_identifier="release-campaign-host-claim-binder",
            host_artifact_supplier=self.semantic.host_artifact_supplier,
            provider=provider,
            model=model_name,
            schema=schema,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_output_tokens=min(4096, max(1024, len(claims) * 512)),
            expand_output_on_length=False,
            prompt_version=prompt_version,
        )

        if result.error:
            raise RuntimeError(f"Semantic claim binding failed: {result.error}")

        return self._process_evaluations(
            packet_dict=packet_dict,
            evaluations=result.value.get("evaluations", []),
            model_name=model_name,
            prompt_version=prompt_version,
            schema_version=context["schema_version"],
            packet_revision=packet_revision,
            required_passage_ids_by_claim=required_passage_ids_by_claim,
        )

    def _process_evaluations(
        self,
        packet_dict: dict,
        evaluations: list[dict],
        model_name: str,
        prompt_version: str,
        schema_version: int,
        packet_revision: int,
        required_passage_ids_by_claim: dict[str, list[str]] | None = None,
    ) -> int:
        from research_domain.models import SemanticStatus

        required_passage_ids_by_claim = required_passage_ids_by_claim or {}
        valid_claim_ids = {c["claim_id"] for c in packet_dict.get("claims", [])}
        valid_passage_ids = {p["passage_id"] for p in packet_dict.get("passages", [])}
        valid_semantic_statuses = {s.value for s in SemanticStatus}
        unknown_required_claims = set(required_passage_ids_by_claim) - valid_claim_ids
        if unknown_required_claims:
            raise ValueError(
                f"required passage lineage has unknown claim IDs: {sorted(unknown_required_claims)}"
            )
        for claim_id, passage_ids in required_passage_ids_by_claim.items():
            unknown_passages = set(passage_ids) - valid_passage_ids
            if unknown_passages:
                raise ValueError(
                    f"required passage lineage for claim {claim_id} has unknown passage IDs: "
                    f"{sorted(unknown_passages)}"
                )

        # Phase 1: Validate all evaluations before mutating state.
        # This prevents partial evaluation failures where some bindings are
        # lost because an invalid claim ID appears later in the list.
        for eval_item in evaluations:
            claim_id_str = eval_item.get("claim_id")
            if claim_id_str not in valid_claim_ids:
                raise ValueError(f"unknown claim IDs: ['{claim_id_str}']")

            semantic_status = eval_item.get("semantic_status")
            if semantic_status not in valid_semantic_statuses:
                raise ValueError(
                    f"invalid semantic_status '{semantic_status}' for claim "
                    f"{claim_id_str}; expected one of {sorted(valid_semantic_statuses)}"
                )

            bindings = eval_item.get("bindings", [])
            if not bindings:
                raise ValueError(
                    f"claim {claim_id_str} has no authoritative passage binding"
                )
            for b in bindings:
                passage_ids_str = b.get("passage_ids", [])
                required_passage_ids = required_passage_ids_by_claim.get(
                    claim_id_str, []
                )
                if required_passage_ids:
                    passage_ids_str = list(dict.fromkeys(required_passage_ids))
                for pid in passage_ids_str:
                    if pid not in valid_passage_ids:
                        raise ValueError(f"unknown passage IDs: ['{pid}']")

        evaluated_claim_ids = [item.get("claim_id") for item in evaluations]
        if len(evaluated_claim_ids) != len(set(evaluated_claim_ids)):
            raise ValueError(
                "claim binding output contains duplicate claim evaluations"
            )
        if set(evaluated_claim_ids) != valid_claim_ids:
            raise ValueError("claim binding output must evaluate every packet claim")

        # Phase 2: Build bindings and claim updates (safe now that validation passed).
        new_bindings = []
        updated_claims_map = {}

        for eval_item in evaluations:
            claim_id_str = eval_item.get("claim_id")
            semantic_status = eval_item.get("semantic_status")
            updated_claims_map[claim_id_str] = semantic_status

            bindings = eval_item.get("bindings", [])
            for b in bindings:
                passage_ids_str = b.get("passage_ids", [])
                required_passage_ids = required_passage_ids_by_claim.get(
                    claim_id_str, []
                )
                if required_passage_ids:
                    passage_ids_str = list(dict.fromkeys(required_passage_ids))

                if not passage_ids_str:
                    continue

                new_bindings.append(
                    {
                        "binding_id": str(uuid4()),
                        "claim_id": claim_id_str,
                        "passage_ids": passage_ids_str,
                        "relationship": b["relationship"],
                        "confidence": b.get("confidence", 0.0),
                        "uncertainty": b.get("uncertainty", ""),
                        "model": model_name,
                        "prompt_version": prompt_version,
                        "schema_version": schema_version,
                        "input_packet_revision": packet_revision,
                    }
                )

        packet_dict["claim_evidence_bindings"] = []

        for c in packet_dict.get("claims", []):
            cid = c["claim_id"]
            if cid in updated_claims_map:
                c["semantic_status"] = updated_claims_map[cid]

        for b_dict in new_bindings:
            packet_dict["claim_evidence_bindings"].append(b_dict)

        new_packet = load_model(packet_dict)
        return self.evidence.persist_packet(new_packet)
