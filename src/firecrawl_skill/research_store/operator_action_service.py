"""Durable operator-action policy for genuine human research decisions."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from .asset_promotion_service import AssetPromotionService
from .candidate_budget_outcomes import CandidateBudgetAdmissionContext
from .candidate_policy_service import CandidatePolicyError, CandidatePolicyService
from .research_controller_contract import CONTROLLER_POLICY_SCHEMA_VERSION
from .run_service import RunStatus

OPERATOR_ACTION_SCHEMA_VERSION = "operator-action-v1"
OPERATOR_ACTION_POLICY_VERSION = "operator-action-policy-v1"

ACTION_BUDGET = "candidate_budget_authorization"
ACTION_CURATION = "curation_selection_required"
ACTION_SCOPE = "material_scope_change_required"
ACTION_MANUAL = "manual_environment_resolution"

ACTION_KINDS = frozenset({ACTION_BUDGET, ACTION_CURATION, ACTION_SCOPE, ACTION_MANUAL})

_MAX_TEMPORAL_ACTION_EVENTS = 10_000
_EVENT_PAGE_SIZE = 100


class OperatorActionError(RuntimeError):
    """A requested operator action is invalid or no longer authoritative."""


class StaleOperatorActionError(OperatorActionError):
    """The action was proposed against authority that has since changed."""


class OperatorActionConflictError(OperatorActionError):
    """The action is already resolved with different semantics."""


@dataclass(frozen=True)
class OperatorActionRecord:
    id: UUID
    action_id: str
    run_id: UUID
    public_run_id: str
    lifecycle_revision: int
    kind: str
    status: str
    policy_version: str
    authority_fingerprint: str
    creation_payload: Mapping[str, Any]
    created_at: Any
    resolution_id: UUID | None = None
    resolution_actor: str | None = None
    resolution_reason: str | None = None
    resolution_payload: Mapping[str, Any] | None = None
    resolved_at: Any | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> OperatorActionRecord:
        return cls(
            id=UUID(str(value["id"])),
            action_id=str(value["external_action_id"]),
            run_id=UUID(str(value["run_id"])),
            public_run_id=str(value["run_external_id"]),
            lifecycle_revision=int(value["lifecycle_revision"]),
            kind=str(value["action_kind"]),
            status=str(value["status"]),
            policy_version=str(value["policy_version"]),
            authority_fingerprint=str(value["authority_fingerprint"]),
            creation_payload=dict(value.get("creation_payload") or {}),
            created_at=value.get("created_at"),
            resolution_id=(
                UUID(str(value["resolution_id"]))
                if value.get("resolution_id") is not None
                else None
            ),
            resolution_actor=(
                str(value["resolution_actor"])
                if value.get("resolution_actor") is not None
                else None
            ),
            resolution_reason=(
                str(value["resolution_reason"])
                if value.get("resolution_reason") is not None
                else None
            ),
            resolution_payload=dict(value.get("resolution_payload") or {}),
            resolved_at=value.get("resolved_at"),
        )

    def to_public_dict(self) -> dict[str, Any]:
        public_payload = dict(self.creation_payload.get("public") or {})
        result: dict[str, Any] = {
            "schema_version": OPERATOR_ACTION_SCHEMA_VERSION,
            "action_id": self.action_id,
            "run_id": self.public_run_id,
            "kind": self.kind,
            "status": self.status,
            "public_payload": public_payload,
            "created_at": self.created_at,
            "resolved_at": self.resolved_at,
        }
        if self.status != "pending":
            result["resolution"] = {
                "actor": self.resolution_actor,
                "reason": self.resolution_reason,
                "payload": dict(self.resolution_payload or {}),
            }
        return result


def validate_public_action_id(value: str) -> str:
    if not isinstance(value, str) or not value.startswith("oa_"):
        raise ValueError("operator action ID must use public oa_<uuid> form")
    raw = value[3:]
    if len(raw) != 32 or raw != raw.lower():
        raise ValueError("operator action ID must use public oa_<uuid> form")
    try:
        UUID(hex=raw)
    except ValueError as exc:
        raise ValueError("operator action ID must use public oa_<uuid> form") from exc
    return value


def _canonical_sha256(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class OperatorActionService:
    """Persist and resolve human-only decisions without exposing generated policy inputs."""

    def __init__(
        self,
        uow_factory: Any,
        *,
        candidate_policy: CandidatePolicyService | None = None,
        promotion_service: AssetPromotionService | None = None,
    ) -> None:
        self.uow_factory = uow_factory
        self.candidate_policy = candidate_policy or CandidatePolicyService(uow_factory)
        self.promotion_service = promotion_service or AssetPromotionService(uow_factory)

    @staticmethod
    def _require_text(value: str, field: str) -> str:
        normalized = " ".join(str(value).split())
        if not normalized:
            raise ValueError(f"{field} is required")
        return normalized

    def describe(self, action_id: str) -> OperatorActionRecord:
        action_id = validate_public_action_id(action_id)
        with self.uow_factory() as uow:
            return OperatorActionRecord.from_mapping(
                uow.operator_actions.get_action(external_action_id=action_id)
            )

    def active_for_run(self, status: RunStatus) -> OperatorActionRecord | None:
        with self.uow_factory() as uow:
            raw = uow.operator_actions.pending_for_run(status.id, for_update=True)
            if raw is None:
                return None
            action = OperatorActionRecord.from_mapping(raw)
            stale_reason = self._stale_reason(uow, action, status)
            if stale_reason is None:
                return action
            self._supersede(uow, action, stale_reason)
            uow.commit()
            return None

    def curation_completed(self, status: RunStatus) -> bool:
        with self.uow_factory() as uow, uow.connection.cursor() as cursor:
            cursor.execute(
                """SELECT policy_version FROM operator_actions
                   WHERE run_id=%s AND lifecycle_revision=%s
                     AND action_kind=%s AND status='resolved'
                   ORDER BY resolved_at DESC,id DESC
                   LIMIT 2""",
                (status.id, status.lifecycle_revision, ACTION_CURATION),
            )
            rows = cursor.fetchall()
        if not rows:
            return False
        if any(str(row[0]) == OPERATOR_ACTION_POLICY_VERSION for row in rows):
            return True
        raise OperatorActionError(
            "resolved curation authority uses a stale operator-action policy version"
        )

    def ensure_budget_action(
        self,
        status: RunStatus,
        context: CandidateBudgetAdmissionContext,
    ) -> OperatorActionRecord:
        if (
            context.run_id != status.id
            or context.lifecycle_revision != status.lifecycle_revision
        ):
            raise StaleOperatorActionError(
                "candidate-budget action does not match the current run revision"
            )
        with self.uow_factory() as uow:
            unresolved = self.candidate_policy.require_soft_override_action_binding(
                uow,
                status.id,
                context.check_id,
                status.lifecycle_revision,
                context.scope_fingerprint,
                context.violated_limits,
            )
            payload = {
                "internal": {
                    "check_id": str(context.check_id),
                    "soft_limits": list(unresolved),
                    "scope_fingerprint": context.scope_fingerprint,
                },
                "public": {
                    "authorization_required": True,
                    "reason": "candidate budget soft exception requires explicit human approval",
                },
            }
            action = self._ensure_action(
                uow,
                status,
                ACTION_BUDGET,
                context.scope_fingerprint,
                payload,
            )
            uow.commit()
            return action

    def ensure_scope_action(
        self,
        status: RunStatus,
        gap: Mapping[str, Any],
    ) -> OperatorActionRecord:
        if str(gap.get("kind") or "") != "temporal_coverage_gap":
            raise OperatorActionError(
                "scope action requires a typed temporal coverage gap"
            )
        with self.uow_factory() as uow, uow.connection.cursor() as cursor:
            state, revision = uow.runs._lock_workflow_run(cursor, status.id)
            if int(revision) != status.lifecycle_revision or str(state) != status.state:
                raise StaleOperatorActionError(
                    "temporal scope action does not match current lifecycle authority"
                )
            spec = uow.runs.get_research_spec(status.id)
            active_gap = self._active_temporal_gap(uow, status.id)
            if active_gap != dict(gap):
                raise StaleOperatorActionError(
                    "temporal coverage authority changed before action persistence"
                )
            spec_id = str(spec["id"]) if spec else None
            spec_revision = int(spec["spec_revision"]) if spec else None
            fingerprint = _canonical_sha256(
                {
                    "gap": active_gap,
                    "spec_id": spec_id,
                    "spec_revision": spec_revision,
                }
            )
            payload = {
                "internal": {
                    "gap": active_gap,
                    "spec_id": spec_id,
                    "spec_revision": spec_revision,
                },
                "public": {
                    "scope_change_required": True,
                    "reason": "authoritative temporal coverage remains unsatisfied",
                },
            }
            action = self._ensure_action(
                uow, status, ACTION_SCOPE, fingerprint, payload
            )
            uow.commit()
            return action

    def ensure_curation_action(self, status: RunStatus) -> OperatorActionRecord:
        with self.uow_factory() as uow:
            census = self.promotion_service.curation_census(
                uow,
                status.id,
                lifecycle_revision=status.lifecycle_revision,
                for_update=True,
            )
            if not census:
                raise OperatorActionError(
                    "curated selection requires at least one authoritative asset subject"
                )
            fingerprint = _canonical_sha256(census)
            payload = {
                "internal": {"census": census},
                "public": {
                    "subjects": [
                        {
                            "subject_id": item["subject_id"],
                            "snapshot_id": item["snapshot_id"],
                            "role": item["role"],
                        }
                        for item in census
                    ],
                    "reject_rest_required": True,
                },
            }
            action = self._ensure_action(
                uow, status, ACTION_CURATION, fingerprint, payload
            )
            uow.commit()
            return action

    def approve(
        self,
        action_id: str,
        *,
        reason: str,
        authorized_by: str,
    ) -> OperatorActionRecord:
        reason = self._require_text(reason, "approval reason")
        authorized_by = self._require_text(authorized_by, "authorizing actor")
        action_id = validate_public_action_id(action_id)
        with self.uow_factory() as uow:
            action = self._locked_action(uow, action_id, ACTION_BUDGET)
            expected_resolution = {"decision": "approved"}
            if action.status == "resolved":
                return self._reuse_resolution(
                    action,
                    actor=authorized_by,
                    reason=reason,
                    payload=expected_resolution,
                )
            internal = dict(action.creation_payload.get("internal") or {})
            check_id = UUID(str(internal.get("check_id")))
            soft_limits = tuple(str(item) for item in internal.get("soft_limits") or ())
            fingerprint = str(internal.get("scope_fingerprint") or "")
            self.candidate_policy.require_soft_override_action_binding(
                uow,
                action.run_id,
                check_id,
                action.lifecycle_revision,
                fingerprint,
                soft_limits,
            )
            self.candidate_policy.record_action_overrides(
                uow,
                action.run_id,
                check_id,
                soft_limits,
                reason=reason,
                author=authorized_by,
            )
            resolved = self._resolve(
                uow,
                action,
                actor=authorized_by,
                reason=reason,
                payload=expected_resolution,
            )
            uow.commit()
            return resolved

    def curate(
        self,
        action_id: str,
        *,
        retain_subject_ids: Sequence[UUID],
        reject_rest: bool,
        reason: str,
        authorized_by: str,
    ) -> OperatorActionRecord:
        reason = self._require_text(reason, "curation reason")
        authorized_by = self._require_text(authorized_by, "authorizing actor")
        if not reject_rest:
            raise OperatorActionError(
                "curation submission must disposition the full action census with --reject-rest"
            )
        retain = tuple(UUID(str(item)) for item in retain_subject_ids)
        if not retain:
            raise OperatorActionError("curation must retain at least one subject")
        if len(set(retain)) != len(retain):
            raise OperatorActionError("curation contains duplicate retained subjects")
        action_id = validate_public_action_id(action_id)
        expected_resolution = {
            "decision": "curated",
            "retained_subject_ids": sorted(str(item) for item in retain),
            "reject_rest": True,
        }
        with self.uow_factory() as uow:
            action = self._locked_action(uow, action_id, ACTION_CURATION)
            if action.status == "resolved":
                return self._reuse_resolution(
                    action,
                    actor=authorized_by,
                    reason=reason,
                    payload=expected_resolution,
                )
            census = self.promotion_service.curation_census(
                uow,
                action.run_id,
                lifecycle_revision=action.lifecycle_revision,
                for_update=True,
            )
            if _canonical_sha256(census) != action.authority_fingerprint:
                raise StaleOperatorActionError(
                    "curation subject membership changed after action creation"
                )
            allowed = {UUID(str(item["subject_id"])) for item in census}
            unknown = set(retain) - allowed
            if unknown:
                raise OperatorActionError(
                    "curation contains subject(s) outside the exact action census"
                )
            self.promotion_service.apply_curated_selection(
                uow,
                action.run_id,
                lifecycle_revision=action.lifecycle_revision,
                retain_subject_ids=set(retain),
                reason=reason,
                actor_identifier=authorized_by,
            )
            resolved = self._resolve(
                uow,
                action,
                actor=authorized_by,
                reason=reason,
                payload=expected_resolution,
            )
            uow.commit()
            return resolved

    def fork(
        self,
        action_id: str,
        revised_objective: str,
        *,
        reason: str,
        authorized_by: str,
    ) -> tuple[OperatorActionRecord, str]:
        revised_objective = self._require_text(revised_objective, "revised objective")
        reason = self._require_text(reason, "scope-change reason")
        authorized_by = self._require_text(authorized_by, "authorizing actor")
        action_id = validate_public_action_id(action_id)
        with self.uow_factory() as uow:
            action = self._locked_action(uow, action_id, ACTION_SCOPE)
            if action.status == "resolved":
                payload = dict(action.resolution_payload or {})
                if (
                    action.resolution_actor == authorized_by
                    and action.resolution_reason == reason
                    and payload.get("decision") == "forked"
                    and payload.get("child_objective") == revised_objective
                    and str(payload.get("child_run_id") or "").startswith("fr_")
                ):
                    return action, str(payload["child_run_id"])
                raise OperatorActionConflictError(
                    f"operator action {action_id} is already resolved with different semantics"
                )
            parent = uow.runs.get_run_status(run_id=action.run_id)
            if revised_objective == " ".join(str(parent["objective"]).split()):
                raise OperatorActionError(
                    "fork requires a materially revised objective/scope; unchanged scope stays on the parent run"
                )
            stale_reason = self._stale_reason(
                uow, action, RunStatus.from_mapping(parent)
            )
            if stale_reason is not None:
                raise StaleOperatorActionError(stale_reason)
            internal = dict(action.creation_payload.get("internal") or {})
            parent_spec_id = (
                UUID(str(internal["spec_id"])) if internal.get("spec_id") else None
            )
            parent_spec_revision = (
                int(internal["spec_revision"])
                if internal.get("spec_revision") is not None
                else None
            )
            parent_policy = self._controller_policy_for_run(uow, action.run_id)
            child_external_id = f"fr_{uuid4().hex}"
            execution_mode = str(parent["execution_mode"])
            child_id = uow.runs.start_run(
                revised_objective,
                {
                    "external_run_id": child_external_id,
                    "execution_mode": execution_mode,
                    "metadata": {
                        "controller": "research-controller-v1",
                        "retained_only": parent_policy["retained_only"],
                        "curated": parent_policy["curated"],
                        "run_mode": (
                            "curated" if parent_policy["curated"] else "autonomous"
                        ),
                        "lineage_parent_run_id": action.public_run_id,
                        "lineage_operator_action_id": action.action_id,
                    },
                },
            )
            uow.runs.append_event(
                child_id,
                "run_started",
                "operator",
                f"operator-action:fork:{action.action_id}:run-started",
                actor_identifier=authorized_by,
                payload={
                    "objective": revised_objective,
                    "execution_mode": execution_mode,
                    "policy_version": "run-state-v1",
                },
            )
            uow.runs.append_event(
                child_id,
                "controller.policy_recorded",
                "controller",
                f"controller:policy:{child_id}",
                actor_identifier="ResearchWorkflowController",
                payload={
                    "schema_version": CONTROLLER_POLICY_SCHEMA_VERSION,
                    "retained_only": parent_policy["retained_only"],
                    "curated": parent_policy["curated"],
                    "evaluated_at": self._utc_now_iso(),
                },
            )
            uow.operator_actions.record_lineage(
                child_run_id=child_id,
                parent_run_id=action.run_id,
                operator_action_id=action.id,
                parent_spec_id=parent_spec_id,
                parent_spec_revision=parent_spec_revision,
                reason=reason,
                child_objective=revised_objective,
            )
            resolved = self._resolve(
                uow,
                action,
                actor=authorized_by,
                reason=reason,
                payload={
                    "decision": "forked",
                    "child_run_id": child_external_id,
                    "parent_run_id": action.public_run_id,
                    "child_objective": revised_objective,
                },
            )
            uow.commit()
            return resolved, child_external_id

    def _ensure_action(
        self,
        uow: Any,
        status: RunStatus,
        kind: str,
        fingerprint: str,
        payload: dict[str, Any],
    ) -> OperatorActionRecord:
        if kind not in ACTION_KINDS:
            raise OperatorActionError(f"unsupported operator action kind: {kind}")
        pending = uow.operator_actions.pending_for_run(status.id, for_update=True)
        if pending is not None:
            current = OperatorActionRecord.from_mapping(pending)
            if (
                current.lifecycle_revision == status.lifecycle_revision
                and current.kind == kind
                and current.policy_version == OPERATOR_ACTION_POLICY_VERSION
                and current.authority_fingerprint == fingerprint
                and dict(current.creation_payload) == payload
            ):
                return current
            self._supersede(
                uow,
                current,
                "persisted authority changed before operator resolution",
            )
        action_id = f"oa_{uuid4().hex}"
        creation_sha256 = _canonical_sha256(payload)
        raw = uow.operator_actions.create_action(
            external_action_id=action_id,
            run_id=status.id,
            lifecycle_revision=status.lifecycle_revision,
            action_kind=kind,
            policy_version=OPERATOR_ACTION_POLICY_VERSION,
            authority_fingerprint=fingerprint,
            creation_payload=payload,
            creation_sha256=creation_sha256,
        )
        return OperatorActionRecord.from_mapping(raw)

    def _locked_action(
        self,
        uow: Any,
        action_id: str,
        expected_kind: str,
    ) -> OperatorActionRecord:
        raw = uow.operator_actions.get_action(
            external_action_id=action_id, for_update=True
        )
        action = OperatorActionRecord.from_mapping(raw)
        if action.kind != expected_kind:
            raise OperatorActionError(
                f"operator action {action_id} is {action.kind}, not {expected_kind}"
            )
        if action.status == "superseded":
            raise OperatorActionConflictError(
                f"operator action {action_id} is already superseded"
            )
        if action.status == "pending":
            status = RunStatus.from_mapping(
                uow.runs.get_run_status(run_id=action.run_id)
            )
            stale_reason = self._stale_reason(uow, action, status)
            if stale_reason is not None:
                raise StaleOperatorActionError(stale_reason)
        return action

    @staticmethod
    def _reuse_resolution(
        action: OperatorActionRecord,
        *,
        actor: str,
        reason: str,
        payload: Mapping[str, Any],
    ) -> OperatorActionRecord:
        if (
            action.status == "resolved"
            and action.resolution_actor == actor
            and action.resolution_reason == reason
            and dict(action.resolution_payload or {}) == dict(payload)
        ):
            return action
        raise OperatorActionConflictError(
            f"operator action {action.action_id} is already resolved with different semantics"
        )

    def _stale_reason(
        self,
        uow: Any,
        action: OperatorActionRecord,
        status: RunStatus,
    ) -> str | None:
        if action.lifecycle_revision != status.lifecycle_revision:
            return (
                "operator action lifecycle revision is stale: "
                f"action={action.lifecycle_revision}, current={status.lifecycle_revision}"
            )
        if action.policy_version != OPERATOR_ACTION_POLICY_VERSION:
            return (
                "operator action policy version is stale: "
                f"action={action.policy_version}, "
                f"current={OPERATOR_ACTION_POLICY_VERSION}"
            )
        if action.kind == ACTION_BUDGET:
            internal = dict(action.creation_payload.get("internal") or {})
            try:
                self.candidate_policy.require_soft_override_action_binding(
                    uow,
                    action.run_id,
                    UUID(str(internal.get("check_id"))),
                    action.lifecycle_revision,
                    str(internal.get("scope_fingerprint") or ""),
                    tuple(str(item) for item in internal.get("soft_limits") or ()),
                )
            except (CandidatePolicyError, TypeError, ValueError) as exc:
                return f"candidate-budget authority changed: {exc}"
            return None
        if action.kind == ACTION_CURATION:
            try:
                census = self.promotion_service.curation_census(
                    uow,
                    action.run_id,
                    lifecycle_revision=action.lifecycle_revision,
                    for_update=False,
                )
            except (KeyError, RuntimeError, ValueError) as exc:
                return f"curation authority changed: {exc}"
            if _canonical_sha256(census) != action.authority_fingerprint:
                return "curation subject membership changed after action creation"
            return None
        if action.kind == ACTION_SCOPE:
            internal = dict(action.creation_payload.get("internal") or {})
            active_gap = self._active_temporal_gap(uow, action.run_id)
            spec = uow.runs.get_research_spec(action.run_id)
            current = _canonical_sha256(
                {
                    "gap": active_gap,
                    "spec_id": str(spec["id"]) if spec else None,
                    "spec_revision": int(spec["spec_revision"]) if spec else None,
                }
            )
            if active_gap is None or current != action.authority_fingerprint:
                return "temporal scope authority changed after action creation"
            if active_gap != dict(internal.get("gap") or {}):
                return "temporal coverage gap changed after action creation"
            return None
        return None

    def _resolve(
        self,
        uow: Any,
        action: OperatorActionRecord,
        *,
        actor: str,
        reason: str,
        payload: dict[str, Any],
    ) -> OperatorActionRecord:
        resolution_sha256 = _canonical_sha256(
            {
                "action_id": action.action_id,
                "actor": actor,
                "reason": reason,
                "payload": payload,
            }
        )
        raw = uow.operator_actions.finish_action(
            action.id,
            status="resolved",
            resolution_id=uuid4(),
            resolution_actor=actor,
            resolution_reason=reason,
            resolution_payload=payload,
            resolution_sha256=resolution_sha256,
        )
        return OperatorActionRecord.from_mapping(raw)

    def _supersede(
        self,
        uow: Any,
        action: OperatorActionRecord,
        reason: str,
    ) -> OperatorActionRecord:
        raw = uow.operator_actions.finish_action(
            action.id,
            status="superseded",
            resolution_id=uuid4(),
            resolution_actor="controller",
            resolution_reason=reason,
            resolution_payload={"decision": "superseded"},
            resolution_sha256=_canonical_sha256(
                {
                    "action_id": action.action_id,
                    "decision": "superseded",
                    "reason": reason,
                }
            ),
        )
        return OperatorActionRecord.from_mapping(raw)

    @staticmethod
    def _controller_policy_for_run(uow: Any, run_id: UUID) -> dict[str, bool]:
        events = uow.runs.list_events(
            run_id,
            event_type="controller.policy_recorded",
            limit=2,
            offset=0,
        )
        if len(events) != 1:
            raise OperatorActionError(
                "scope-fork parent has no unique canonical controller policy"
            )
        payload = events[0].get("payload") or {}
        if not isinstance(payload, Mapping):
            raise OperatorActionError(
                "scope-fork parent controller policy is malformed"
            )
        if payload.get("schema_version") != CONTROLLER_POLICY_SCHEMA_VERSION:
            raise OperatorActionError(
                "scope-fork parent controller policy is malformed"
            )
        retained_only = payload.get("retained_only")
        curated = payload.get("curated")
        if not isinstance(retained_only, bool) or not isinstance(curated, bool):
            raise OperatorActionError(
                "scope-fork parent controller policy is malformed"
            )
        return {"retained_only": retained_only, "curated": curated}

    @staticmethod
    def _active_temporal_gap(uow: Any, run_id: UUID) -> dict[str, Any] | None:
        latest_gap: dict[str, Any] | None = None
        latest_gap_sequence = -1
        latest_resolution_sequence = -1
        offset = 0
        while offset < _MAX_TEMPORAL_ACTION_EVENTS:
            limit = min(_EVENT_PAGE_SIZE, _MAX_TEMPORAL_ACTION_EVENTS - offset)
            events = uow.runs.list_events(
                run_id,
                limit=limit,
                offset=offset,
            )
            for event in events:
                sequence = int(event.get("sequence_number") or 0)
                event_type = str(event.get("event_type") or "")
                if event_type == "evidence.temporal_coverage_gap":
                    payload = event.get("payload") or {}
                    if not isinstance(payload, Mapping):
                        raise OperatorActionError(
                            "persisted temporal coverage gap is malformed"
                        )
                    gap = payload.get("temporal_coverage_gap")
                    if not isinstance(gap, Mapping):
                        raise OperatorActionError(
                            "persisted temporal coverage gap is malformed"
                        )
                    if sequence > latest_gap_sequence:
                        latest_gap = dict(gap)
                        latest_gap_sequence = sequence
                elif event_type == "evidence.temporal_coverage_resolved":
                    latest_resolution_sequence = max(
                        latest_resolution_sequence,
                        sequence,
                    )
            offset += len(events)
            if len(events) < limit:
                break
        else:
            raise OperatorActionError(
                "run event history exceeds bounded temporal action authority scan"
            )

        if latest_gap is None or latest_resolution_sequence > latest_gap_sequence:
            return None
        return latest_gap

    @staticmethod
    def _utc_now_iso() -> str:
        from datetime import datetime, timezone

        return datetime.now(timezone.utc).isoformat()


__all__ = [
    "ACTION_BUDGET",
    "ACTION_CURATION",
    "ACTION_MANUAL",
    "ACTION_SCOPE",
    "OPERATOR_ACTION_POLICY_VERSION",
    "OPERATOR_ACTION_SCHEMA_VERSION",
    "OperatorActionConflictError",
    "OperatorActionError",
    "OperatorActionRecord",
    "OperatorActionService",
    "StaleOperatorActionError",
    "validate_public_action_id",
]
