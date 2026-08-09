"""Authoritative synthesis provenance required for terminal completion.

This module binds a completed run to PostgreSQL-authoritative acquisition
membership, the current EvidencePacket, immutable semantic calls/artifacts, and
the deterministic validation result.  It is deliberately read-only: callers
may use it as a preflight, while ``GuardedResearchRunService`` repeats the same
read inside the terminal transaction before committing ``completed``.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REQUIRED_SYNTHESIS_STAGES = (
    "outline",
    "binding",
    "draft",
    "citation_pass",
    "validation",
)
_AUTHORITY_BY_MODE = {
    "autonomous_local": ("local", "local-model"),
    "agent_led": ("host-agent", "host-agent"),
    "deterministic_debug": ("deterministic-fixture", "deterministic-fixture"),
}
_EXPECTED_SCHEMA_BY_STAGE = {
    "draft": "synthesis-draft-v1",
    "citation_pass": "synthesis-citation-pass-v1",
}


class CompletionProvenanceError(RuntimeError):
    """Authoritative completion provenance is missing, stale, or inconsistent."""


@dataclass(frozen=True)
class _SemanticArtifactProvenance:
    stage_id: UUID
    semantic_call_id: UUID
    semantic_artifact_id: UUID
    provider: str
    model: str
    model_revision: str
    prompt_version: str
    input_sha256: str
    request: dict[str, Any]
    schema_name: str
    schema_version: int
    payload: dict[str, Any]
    content_sha256: str


@dataclass(frozen=True)
class CompletionProvenance:
    """Exact persisted identities used to authorize terminal completion."""

    run_id: UUID
    membership_seal_id: UUID
    membership_seal_revision: int
    source_manifest_sha256: str
    evidence_packet_id: UUID
    evidence_packet_revision: int
    evidence_packet_sha256: str
    synthesis_stage_id: UUID
    synthesis_semantic_call_id: UUID
    synthesis_artifact_id: UUID
    answer_sha256: str
    semantic_provider: str
    semantic_model: str
    semantic_model_revision: str
    prompt_version: str
    semantic_input_sha256: str
    synthesis_schema_name: str
    synthesis_schema_version: int
    citation_stage_id: UUID
    citation_semantic_call_id: UUID
    citation_artifact_id: UUID
    citation_artifact_sha256: str
    validation_stage_id: UUID
    validation_report_sha256: str
    claim_count: int
    binding_count: int

    def audit_metadata(self) -> dict[str, Any]:
        """Return JSON-safe immutable provenance for the transition ledger."""
        return {
            "schema_version": "completion-provenance-v1",
            "membership_seal_id": str(self.membership_seal_id),
            "membership_seal_revision": self.membership_seal_revision,
            "source_membership_sha256": self.source_manifest_sha256,
            "evidence_packet_id": str(self.evidence_packet_id),
            "evidence_packet_revision": self.evidence_packet_revision,
            "evidence_packet_sha256": self.evidence_packet_sha256,
            "synthesis_stage_id": str(self.synthesis_stage_id),
            "semantic_call_id": str(self.synthesis_semantic_call_id),
            "semantic_artifact_id": str(self.synthesis_artifact_id),
            "synthesis_artifact_sha256": self.answer_sha256,
            "semantic_provider": self.semantic_provider,
            "semantic_model": self.semantic_model,
            "semantic_model_revision": self.semantic_model_revision,
            "prompt_version": self.prompt_version,
            "semantic_input_sha256": self.semantic_input_sha256,
            "synthesis_schema_name": self.synthesis_schema_name,
            "synthesis_schema_version": self.synthesis_schema_version,
            "citation_stage_id": str(self.citation_stage_id),
            "citation_semantic_call_id": str(self.citation_semantic_call_id),
            "citation_semantic_artifact_id": str(self.citation_artifact_id),
            "citation_artifact_sha256": self.citation_artifact_sha256,
            "validation_stage_id": str(self.validation_stage_id),
            "validation_report_sha256": self.validation_report_sha256,
            "claim_count": self.claim_count,
            "binding_count": self.binding_count,
        }

    def completion_fields(self) -> dict[str, Any]:
        return {
            "source_manifest_sha256": self.source_manifest_sha256,
            "answer_sha256": self.answer_sha256,
            "provenance_type": "authoritative",
            "completion_provenance": self.audit_metadata(),
        }

    def assert_matches_completion(self, completion: dict[str, Any]) -> None:
        """Reject stale/caller-forged metadata at the terminal transaction."""
        expected = self.completion_fields()
        for key in ("source_manifest_sha256", "answer_sha256", "provenance_type"):
            if completion.get(key) != expected[key]:
                raise CompletionProvenanceError(
                    f"terminal completion provenance changed before commit: {key}"
                )
        supplied = completion.get("completion_provenance")
        if supplied != expected["completion_provenance"]:
            raise CompletionProvenanceError(
                "terminal completion provenance changed before commit"
            )


def normalize_sha256(label: str, value: str | None) -> str | None:
    """Validate an optional caller assertion as canonical SHA-256."""
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if not _SHA256_RE.fullmatch(normalized):
        raise CompletionProvenanceError(
            f"{label} must be exactly 64 hexadecimal SHA-256 characters"
        )
    return normalized


def _json_sha256(value: Any) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _validation_report_sha256(value: Any) -> str:
    # Must match ReportValidator._compute_report_hash().
    payload = json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _require_uuid(value: Any, label: str) -> UUID:
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise CompletionProvenanceError(f"{label} is not a valid UUID") from exc


def _require_sha256(value: Any, label: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _SHA256_RE.fullmatch(normalized):
        raise CompletionProvenanceError(
            f"{label} is not a canonical persisted SHA-256 digest"
        )
    return normalized


def _load_semantic_stage(
    cur: Any,
    *,
    run_id: UUID,
    execution_mode: str,
    stage: dict[str, Any],
    for_update: bool = False,
) -> _SemanticArtifactProvenance:
    stage_name = str(stage["stage_name"])
    call_id = stage.get("semantic_call_id")
    artifact_id = stage.get("semantic_artifact_id")
    if call_id is None or artifact_id is None:
        raise CompletionProvenanceError(
            f"{stage_name} requires a persisted semantic call and immutable artifact"
        )
    cur.execute(
        """SELECT c.provider,c.model,c.model_revision,c.prompt_version,
                  c.input_sha256,c.request,c.status,
                  a.schema_name,a.schema_version,a.payload,a.content_sha256,
                  a.validation_status,a.validation_errors
             FROM semantic_calls c
             JOIN semantic_artifacts a
               ON a.id=%s
              AND a.semantic_call_id=c.id
              AND a.run_id=c.run_id
            WHERE c.id=%s AND c.run_id=%s"""
        + (" FOR UPDATE OF c,a" if for_update else ""),
        (artifact_id, call_id, run_id),
    )
    row = cur.fetchone()
    if row is None:
        raise CompletionProvenanceError(
            f"{stage_name} semantic call/artifact linkage is missing or cross-run"
        )
    (
        provider,
        model,
        model_revision,
        prompt_version,
        input_sha256,
        request,
        call_status,
        schema_name,
        schema_version,
        payload,
        content_sha256,
        validation_status,
        validation_errors,
    ) = row
    if call_status != "complete":
        raise CompletionProvenanceError(
            f"{stage_name} semantic call is not complete: {call_status!r}"
        )
    if validation_status != "valid" or validation_errors:
        raise CompletionProvenanceError(
            f"{stage_name} immutable semantic artifact is not valid"
        )
    if not isinstance(request, dict) or not isinstance(payload, dict):
        raise CompletionProvenanceError(
            f"{stage_name} semantic provenance is not structured JSON"
        )
    expected_authority = _AUTHORITY_BY_MODE.get(execution_mode)
    if expected_authority is None:
        raise CompletionProvenanceError(
            "unsupported execution mode for authoritative completion: "
            f"{execution_mode!r}"
        )
    expected_provider, expected_request_authority = expected_authority
    if str(provider) != expected_provider:
        raise CompletionProvenanceError(
            f"{stage_name} provider {provider!r} is not authoritative for "
            f"execution mode {execution_mode!r}"
        )
    if request.get("authority") != expected_request_authority:
        raise CompletionProvenanceError(
            f"{stage_name} semantic authority is not authoritative for the run mode"
        )
    input_hash = _require_sha256(input_sha256, f"{stage_name} semantic input hash")
    if _json_sha256(request) != input_hash:
        raise CompletionProvenanceError(
            f"{stage_name} semantic input hash does not verify its persisted request"
        )
    expected_schema = _EXPECTED_SCHEMA_BY_STAGE.get(stage_name)
    if expected_schema is not None and str(schema_name) != expected_schema:
        raise CompletionProvenanceError(
            f"{stage_name} semantic artifact uses unexpected schema {schema_name!r}"
        )
    content_hash = _require_sha256(content_sha256, f"{stage_name} artifact content")
    if _json_sha256(payload) != content_hash:
        raise CompletionProvenanceError(
            f"{stage_name} immutable semantic artifact content hash does not verify"
        )
    stage_artifact = stage.get("artifact")
    if not isinstance(stage_artifact, dict) or stage_artifact != payload:
        raise CompletionProvenanceError(
            f"{stage_name} stage artifact is not the exact immutable semantic artifact"
        )
    if int(stage.get("schema_version") or 0) != int(schema_version):
        raise CompletionProvenanceError(
            f"{stage_name} stage/schema version does not match its semantic artifact"
        )
    if str(stage.get("model_name") or "") != str(model or ""):
        raise CompletionProvenanceError(
            f"{stage_name} stage model identity does not match its semantic call"
        )
    if str(stage.get("prompt_version") or "") != str(prompt_version or ""):
        raise CompletionProvenanceError(
            f"{stage_name} stage prompt version does not match its semantic call"
        )
    return _SemanticArtifactProvenance(
        stage_id=_require_uuid(stage["id"], f"{stage_name} stage id"),
        semantic_call_id=_require_uuid(call_id, f"{stage_name} semantic call id"),
        semantic_artifact_id=_require_uuid(
            artifact_id, f"{stage_name} semantic artifact id"
        ),
        provider=str(provider),
        model=str(model or ""),
        model_revision=str(model_revision or ""),
        prompt_version=str(prompt_version or ""),
        input_sha256=input_hash,
        request=request,
        schema_name=str(schema_name),
        schema_version=int(schema_version),
        payload=payload,
        content_sha256=content_hash,
    )


def load_authoritative_completion_provenance(
    uow: Any,
    run_id: UUID,
    *,
    for_update: bool = False,
) -> CompletionProvenance:
    """Load and verify the complete PostgreSQL authority chain for ``completed``."""
    connection = getattr(uow, "connection", None)
    if connection is None:
        raise CompletionProvenanceError(
            "authoritative completion requires a transactional PostgreSQL UoW"
        )
    with connection.cursor() as cur:
        suffix = " FOR UPDATE" if for_update else ""
        cur.execute(
            "SELECT state,lifecycle_revision,execution_mode "
            "FROM research_runs WHERE id=%s" + suffix,
            (run_id,),
        )
        run = cur.fetchone()
        if run is None:
            raise CompletionProvenanceError("research run was not found")
        _state, _revision, execution_mode = run

        cur.execute(
            """SELECT id,seal_revision,membership_sha256,
                      expected_asset_count,expected_chunk_count
                 FROM run_asset_membership_seals
                WHERE run_id=%s AND status='sealed'
                ORDER BY seal_revision DESC
                LIMIT 1"""
            + (" FOR UPDATE" if for_update else ""),
            (run_id,),
        )
        seal = cur.fetchone()
        if seal is None:
            raise CompletionProvenanceError(
                "completion requires an active exact PostgreSQL membership seal"
            )
        seal_id = _require_uuid(seal[0], "membership seal id")
        cur.execute("SELECT validate_run_asset_membership_seal(%s)", (seal_id,))
        cur.fetchone()
        seal_revision = int(seal[1])
        source_hash = _require_sha256(seal[2], "membership seal hash")
        expected_asset_count = int(seal[3])
        expected_chunk_count = int(seal[4])

        cur.execute(
            """SELECT subject_id,snapshot_id,role,chunk_ids,member_sha256
                 FROM run_asset_membership_members
                WHERE seal_id=%s AND run_id=%s
                ORDER BY ordinal"""
            + (" FOR UPDATE" if for_update else ""),
            (seal_id, run_id),
        )
        membership_rows = cur.fetchall()
        sealed_snapshots = {str(row[1]) for row in membership_rows}
        sealed_chunks = {
            str(chunk_id) for row in membership_rows for chunk_id in tuple(row[3] or ())
        }
        member_payloads = []
        for subject_id, snapshot_id, role, chunk_ids, member_sha256 in membership_rows:
            payload = {
                "subject_id": str(subject_id),
                "snapshot_id": str(snapshot_id),
                "role": str(role),
                "chunk_ids": [str(chunk_id) for chunk_id in tuple(chunk_ids or ())],
            }
            if _json_sha256(payload) != _require_sha256(
                member_sha256, "membership member hash"
            ):
                raise CompletionProvenanceError(
                    "membership member hash no longer verifies its exact "
                    "persisted member"
                )
            member_payloads.append(payload)
        if _json_sha256(member_payloads) != source_hash:
            raise CompletionProvenanceError(
                "membership seal hash no longer verifies its exact persisted members"
            )
        if len(membership_rows) != expected_asset_count:
            raise CompletionProvenanceError(
                "membership seal asset census no longer matches its persisted members"
            )
        if len(sealed_chunks) != expected_chunk_count:
            raise CompletionProvenanceError(
                "membership seal chunk census no longer matches its persisted members"
            )

        cur.execute(
            """SELECT id,packet_revision,payload
                 FROM evidence_packets
                WHERE run_id=%s
                ORDER BY packet_revision DESC
                LIMIT 1"""
            + (" FOR UPDATE" if for_update else ""),
            (run_id,),
        )
        packet_row = cur.fetchone()
        if packet_row is None:
            raise CompletionProvenanceError(
                "completion requires a persisted EvidencePacket"
            )
        packet_id = _require_uuid(packet_row[0], "evidence packet id")
        packet_revision = int(packet_row[1])
        packet = packet_row[2]
        if not isinstance(packet, dict):
            raise CompletionProvenanceError(
                "EvidencePacket payload is not structured JSON"
            )
        packet_hash = _json_sha256(packet)
        claims = packet.get("claims") or []
        passages = packet.get("passages") or []
        omitted_passages = packet.get("omitted_passages") or []
        bindings = packet.get("claim_evidence_bindings") or []
        if not claims or not passages:
            raise CompletionProvenanceError(
                "completion requires non-empty claims and evidence passages"
            )

        packet_items = [*passages, *omitted_passages]
        packet_passages = {
            str(item.get("passage_id")): item
            for item in packet_items
            if item.get("passage_id")
        }
        if len(packet_passages) != len(packet_items):
            raise CompletionProvenanceError(
                "EvidencePacket passage membership contains missing or duplicate "
                "identifiers"
            )
        if not set(packet_passages).issubset(sealed_chunks):
            raise CompletionProvenanceError(
                "EvidencePacket passage membership escapes the sealed source membership"
            )
        packet_snapshot_ids = {
            str(item.get("snapshot_id"))
            for item in packet_items
            if item.get("snapshot_id") is not None
        }
        if not packet_snapshot_ids.issubset(sealed_snapshots):
            raise CompletionProvenanceError(
                "EvidencePacket snapshot membership escapes the sealed source "
                "membership"
            )
        for passage_id, passage in packet_passages.items():
            if passage_id not in sealed_chunks:
                raise CompletionProvenanceError(
                    "EvidencePacket contains a passage outside sealed source membership"
                )
            snapshot_id = passage.get("snapshot_id")
            if snapshot_id is not None and str(snapshot_id) not in sealed_snapshots:
                raise CompletionProvenanceError(
                    "EvidencePacket contains a snapshot outside sealed source "
                    "membership"
                )

        claim_ids = {
            str(claim.get("claim_id")) for claim in claims if claim.get("claim_id")
        }
        if len(claim_ids) != len(claims):
            raise CompletionProvenanceError(
                "EvidencePacket claims require unique persisted claim identifiers"
            )
        binding_passages: dict[str, set[str]] = {
            claim_id: set() for claim_id in claim_ids
        }
        binding_relationships: dict[str, set[str]] = {
            claim_id: set() for claim_id in claim_ids
        }
        packet_binding_pairs: set[tuple[str, str, str]] = set()
        for binding in bindings:
            claim_id = str(binding.get("claim_id") or "")
            relationship = str(binding.get("relationship") or "")
            if claim_id not in claim_ids:
                raise CompletionProvenanceError(
                    "EvidencePacket binding references an unknown claim"
                )
            passage_ids = {str(item) for item in binding.get("passage_ids") or ()}
            if not passage_ids:
                raise CompletionProvenanceError(
                    "completed research requires evidence links for every claim"
                )
            if not passage_ids.issubset(packet_passages) or not passage_ids.issubset(
                sealed_chunks
            ):
                raise CompletionProvenanceError(
                    "claim evidence link escapes the current packet or sealed "
                    "membership"
                )
            binding_passages[claim_id].update(passage_ids)
            binding_relationships[claim_id].add(relationship)
            packet_binding_pairs.update(
                (claim_id, passage_id, relationship) for passage_id in passage_ids
            )
        missing_bindings = [
            claim_id
            for claim_id, passage_ids in binding_passages.items()
            if not passage_ids
        ]
        if missing_bindings:
            raise CompletionProvenanceError(
                "completed research requires persisted evidence links for every claim"
            )

        cur.execute(
            """SELECT claim_id,statement,semantic_status,evidence_packet_revision
                 FROM research_claims
                WHERE run_id=%s"""
            + (" FOR UPDATE" if for_update else ""),
            (run_id,),
        )
        persisted_claims = {
            str(row[0]): (row[1], str(row[2]), int(row[3])) for row in cur.fetchall()
        }
        for claim in claims:
            claim_id = str(claim["claim_id"])
            persisted = persisted_claims.get(claim_id)
            if persisted is None:
                raise CompletionProvenanceError(
                    "EvidencePacket claim is not backed by a persisted research_claim"
                )
            statement, semantic_status, persisted_revision = persisted
            if (
                statement != claim.get("statement")
                or semantic_status != str(claim.get("semantic_status"))
                or persisted_revision != packet_revision
            ):
                raise CompletionProvenanceError(
                    "EvidencePacket claim does not match current persisted claim "
                    "provenance"
                )

        cur.execute(
            """SELECT claim_id,passage_id,snapshot_id,relationship
                 FROM claim_evidence_links
                WHERE run_id=%s"""
            + (" FOR UPDATE" if for_update else ""),
            (run_id,),
        )
        persisted_link_pairs: set[tuple[str, str, str]] = set()
        for claim_id, passage_id, snapshot_id, relationship in cur.fetchall():
            if (
                str(passage_id) in sealed_chunks
                and str(snapshot_id) in sealed_snapshots
            ):
                persisted_link_pairs.add(
                    (str(claim_id), str(passage_id), str(relationship))
                )
        if not packet_binding_pairs.issubset(persisted_link_pairs):
            raise CompletionProvenanceError(
                "EvidencePacket bindings are not backed by persisted "
                "claim_evidence_links"
            )

        cur.execute(
            """SELECT id,stage_name,stage_status,semantic_call_id,
                      semantic_artifact_id,evidence_packet_revision,model_name,
                      prompt_version,schema_version,artifact,error
                 FROM synthesis_stages
                WHERE run_id=%s"""
            + (" FOR UPDATE" if for_update else ""),
            (run_id,),
        )
        stages = {
            str(row[1]): {
                "id": row[0],
                "stage_name": row[1],
                "stage_status": row[2],
                "semantic_call_id": row[3],
                "semantic_artifact_id": row[4],
                "evidence_packet_revision": int(row[5]),
                "model_name": row[6],
                "prompt_version": row[7],
                "schema_version": int(row[8]),
                "artifact": row[9],
                "error": row[10],
            }
            for row in cur.fetchall()
        }
        for stage_name in _REQUIRED_SYNTHESIS_STAGES:
            stage = stages.get(stage_name)
            if stage is None:
                raise CompletionProvenanceError(
                    f"completion requires synthesis stage {stage_name!r}"
                )
            if stage["stage_status"] != "completed":
                raise CompletionProvenanceError(
                    f"synthesis stage {stage_name!r} is not completed"
                )
            if stage["evidence_packet_revision"] != packet_revision:
                raise CompletionProvenanceError(
                    f"synthesis stage {stage_name!r} is stale for the current "
                    "EvidencePacket"
                )

        draft = _load_semantic_stage(
            cur,
            run_id=run_id,
            execution_mode=str(execution_mode),
            stage=stages["draft"],
            for_update=for_update,
        )
        citation = _load_semantic_stage(
            cur,
            run_id=run_id,
            execution_mode=str(execution_mode),
            stage=stages["citation_pass"],
            for_update=for_update,
        )

        packet_ref = f"packet-{run_id}-r{packet_revision}"
        draft_refs = {str(item) for item in draft.request.get("input_artifact_ids", ())}
        citation_refs = {
            str(item) for item in citation.request.get("input_artifact_ids", ())
        }
        if packet_ref not in draft_refs or packet_ref not in citation_refs:
            raise CompletionProvenanceError(
                "semantic calls are not bound to the current EvidencePacket revision"
            )

        draft_payload = draft.payload
        if draft_payload.get("unsupported_claims"):
            raise CompletionProvenanceError(
                "draft synthesis artifact contains unresolved unsupported claims"
            )
        draft_citations: set[tuple[str, str, tuple[str, ...]]] = set()
        for section in draft_payload.get("report_sections") or ():
            section_id = str(section.get("section_id") or "")
            for reference in section.get("claim_references") or ():
                claim_id = str(reference.get("claim_id") or "")
                cited = tuple(
                    sorted(str(item) for item in reference.get("passage_ids") or ())
                )
                relationship = str(reference.get("relationship") or "")
                if (
                    claim_id not in claim_ids
                    or not cited
                    or not set(cited).issubset(binding_passages[claim_id])
                    or relationship not in binding_relationships[claim_id]
                ):
                    raise CompletionProvenanceError(
                        "draft synthesis artifact is not bound to persisted "
                        "claim evidence"
                    )
                draft_citations.add((section_id, claim_id, cited))
        if not draft_citations:
            raise CompletionProvenanceError(
                "draft synthesis artifact contains no evidence-bound claim references"
            )

        citation_payload = citation.payload
        validate_citation_artifact(citation_payload, draft_citations)

        validation = stages["validation"]
        validation_artifact = validation.get("artifact")
        if not isinstance(validation_artifact, dict):
            raise CompletionProvenanceError(
                "validation stage lacks a structured validation artifact"
            )
        if validation_artifact.get("validation_status") != "valid":
            raise CompletionProvenanceError("validation status is not valid")
        if validation_artifact.get("is_complete") is not True:
            raise CompletionProvenanceError(
                "validation is valid but incomplete; completed requires zero warnings"
            )
        if validation_artifact.get("stale_packet") is not False:
            raise CompletionProvenanceError(
                "validation is stale for the current EvidencePacket"
            )
        if (
            int(validation_artifact.get("current_packet_revision") or 0)
            != packet_revision
        ):
            raise CompletionProvenanceError(
                "validation was not performed against the current EvidencePacket"
            )
        if int(validation_artifact.get("validation_errors_count") or 0) != 0:
            raise CompletionProvenanceError("validation contains unresolved errors")
        if int(validation_artifact.get("validation_warnings_count") or 0) != 0:
            raise CompletionProvenanceError("validation contains unresolved warnings")

        expected_report_hash = _validation_report_sha256(citation_payload)
        validation_report_hash = _require_sha256(
            validation_artifact.get("report_hash"), "validation report hash"
        )
        if validation_report_hash != expected_report_hash:
            raise CompletionProvenanceError(
                "validation report hash is not bound to the exact citation artifact"
            )

        manifest = validation_artifact.get("claim_manifest")
        if not isinstance(manifest, list):
            raise CompletionProvenanceError("validation claim manifest is missing")
        manifest_ids = {
            str(item.get("claim_id"))
            for item in manifest
            if isinstance(item, dict) and item.get("claim_id")
        }
        if manifest_ids != claim_ids or len(manifest_ids) != len(manifest):
            raise CompletionProvenanceError(
                "validation claim manifest does not exactly cover EvidencePacket claims"
            )
        for item in manifest:
            claim_id = str(item["claim_id"])
            cited = {str(value) for value in item.get("cited_passage_ids") or ()}
            if not cited or not cited.issubset(binding_passages[claim_id]):
                raise CompletionProvenanceError(
                    "validation claim manifest is not bound to persisted claim evidence"
                )
            if item.get("issues"):
                raise CompletionProvenanceError(
                    "validation claim manifest contains unresolved issues"
                )
            relationship = item.get("binding_relationship")
            if relationship not in binding_relationships[claim_id]:
                raise CompletionProvenanceError(
                    "validation claim relationship does not match persisted evidence"
                )

    return CompletionProvenance(
        run_id=run_id,
        membership_seal_id=seal_id,
        membership_seal_revision=seal_revision,
        source_manifest_sha256=source_hash,
        evidence_packet_id=packet_id,
        evidence_packet_revision=packet_revision,
        evidence_packet_sha256=packet_hash,
        synthesis_stage_id=draft.stage_id,
        synthesis_semantic_call_id=draft.semantic_call_id,
        synthesis_artifact_id=draft.semantic_artifact_id,
        answer_sha256=draft.content_sha256,
        semantic_provider=draft.provider,
        semantic_model=draft.model,
        semantic_model_revision=draft.model_revision,
        prompt_version=draft.prompt_version,
        semantic_input_sha256=draft.input_sha256,
        synthesis_schema_name=draft.schema_name,
        synthesis_schema_version=draft.schema_version,
        citation_stage_id=citation.stage_id,
        citation_semantic_call_id=citation.semantic_call_id,
        citation_artifact_id=citation.semantic_artifact_id,
        citation_artifact_sha256=citation.content_sha256,
        validation_stage_id=_require_uuid(validation["id"], "validation stage id"),
        validation_report_sha256=validation_report_hash,
        claim_count=len(claim_ids),
        binding_count=len(packet_binding_pairs),
    )


def resolve_completion_assertions(
    provenance: CompletionProvenance,
    *,
    source_manifest_sha256: str | None,
    answer_sha256: str | None,
) -> CompletionProvenance:
    """Validate optional CLI assertions against authoritative persisted hashes."""
    supplied_source = normalize_sha256("source_manifest_sha256", source_manifest_sha256)
    supplied_answer = normalize_sha256("answer_sha256", answer_sha256)
    if (
        supplied_source is not None
        and supplied_source != provenance.source_manifest_sha256
    ):
        raise CompletionProvenanceError(
            "source_manifest_sha256 does not match the active sealed membership"
        )
    if supplied_answer is not None and supplied_answer != provenance.answer_sha256:
        raise CompletionProvenanceError(
            "answer_sha256 does not match the immutable synthesis artifact"
        )
    return provenance


def validate_citation_artifact(
    citation_payload: dict[str, Any],
    draft_citations: set[tuple[str, str, tuple[str, ...]]],
) -> None:
    """Validate a citation-pass artifact against terminal provenance rules.

    Raises ``CompletionProvenanceError`` on any violation so that callers can
    enforce stage-time acceptance at least as strictly as the terminal gate.
    """
    if not isinstance(citation_payload, dict):
        raise CompletionProvenanceError("citation-pass artifact is not a mapping")

    if citation_payload.get("pass_status") != "passed":
        raise CompletionProvenanceError("citation-pass semantic artifact did not pass")

    if (
        citation_payload.get("invented_citations")
        or citation_payload.get("unsupported_claims")
        or citation_payload.get("entailment_mismatches")
    ):
        raise CompletionProvenanceError(
            "citation-pass semantic artifact contains unresolved failures"
        )

    validation_results = citation_payload.get("validation_results") or ()
    citation_results: set[tuple[str, str, tuple[str, ...]]] = set()
    for result in validation_results:
        if not isinstance(result, dict):
            raise CompletionProvenanceError(
                "citation validation result is not a mapping"
            )
        if result.get("status") != "valid" or result.get("issue"):
            raise CompletionProvenanceError(
                "citation-pass semantic artifact contains unresolved validation results"
            )
        citation_results.add(
            (
                str(result.get("section_id") or ""),
                str(result.get("claim_id") or ""),
                tuple(sorted(str(item) for item in result.get("passage_ids") or ())),
            )
        )

    if citation_results != draft_citations:
        raise CompletionProvenanceError(
            "citation-pass artifact does not exactly validate the immutable "
            "draft citations"
        )


__all__ = [
    "CompletionProvenance",
    "CompletionProvenanceError",
    "load_authoritative_completion_provenance",
    "normalize_sha256",
    "resolve_completion_assertions",
    "validate_citation_artifact",
]
