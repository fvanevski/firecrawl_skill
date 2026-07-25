"""Semantic claim-to-passage binding service."""

import json
import logging
from uuid import UUID, uuid4

from model_gateway import call_structured
from research_domain.registry import load_model

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
        endpoint_alias: str,
        model_name: str,
        provider: str = "local",
    ) -> int:
        """Evaluate claims against an EvidencePacket using a semantic model.

        Returns:
            The revision of the newly persisted EvidencePacket.
        """
        packet_dict = self.evidence.export_packet(run_id, packet_revision)
        if not packet_dict:
            raise ValueError(f"EvidencePacket {run_id} r{packet_revision} not found")

        passages = packet_dict.get("passages", [])
        claims = packet_dict.get("claims", [])

        if not claims:
            return packet_revision

        system_prompt = (
            "You are a rigorous evidence evaluator. "
            "Given a list of research claims and a list of passages, "
            "determine if the passages support, contradict, qualify, or provide context for each claim. "
            "Respond strictly using the JSON schema provided. "
            "Do not invent IDs. Only use the provided claim_id and passage_id values."
        )

        user_prompt_data = {
            "claims": [
                {"claim_id": c["claim_id"], "statement": c["statement"]} for c in claims
            ],
            "passages": [
                {"passage_id": p["passage_id"], "text": p["text"]} for p in passages
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
            status = uow.runs.get_run_status(run_id)
            context["run_revision"] = status["lifecycle_revision"]

        from copy import deepcopy

        schema = deepcopy(self.schema)
        valid_claim_ids = [c["claim_id"] for c in claims]
        valid_passage_ids = [p["passage_id"] for p in passages]
        schema["properties"]["evaluations"]["items"]["properties"]["claim_id"][
            "enum"
        ] = valid_claim_ids
        schema["properties"]["evaluations"]["items"]["properties"]["bindings"]["items"][
            "properties"
        ]["passage_ids"]["items"]["enum"] = valid_passage_ids

        result = call_structured(
            provider=provider,
            model=model_name,
            schema=schema,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            prompt_version=prompt_version,
            semantic_persistence=self.semantic,
            semantic_context=context,
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
        )

    def _process_evaluations(
        self,
        packet_dict: dict,
        evaluations: list[dict],
        model_name: str,
        prompt_version: str,
        schema_version: int,
        packet_revision: int,
    ) -> int:
        valid_claim_ids = {c["claim_id"] for c in packet_dict.get("claims", [])}
        valid_passage_ids = {p["passage_id"] for p in packet_dict.get("passages", [])}

        new_bindings = []
        updated_claims_map = {}

        for eval_item in evaluations:
            claim_id_str = eval_item.get("claim_id")
            if claim_id_str not in valid_claim_ids:
                raise ValueError(f"unknown claim IDs: ['{claim_id_str}']")

            semantic_status = eval_item.get("semantic_status")
            updated_claims_map[claim_id_str] = semantic_status

            bindings = eval_item.get("bindings", [])
            for b in bindings:
                passage_ids_str = b.get("passage_ids", [])
                for pid in passage_ids_str:
                    if pid not in valid_passage_ids:
                        raise ValueError(f"unknown passage IDs: ['{pid}']")

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
