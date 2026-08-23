"""Correct multi-item coverage seeding over the legacy repository contract."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from firecrawl_skill.research_domain.models import (
    CoverageItem,
    CoverageItemType,
    CoverageStatus,
    FreshnessStatus,
)

from .assessment.coverage import CoverageError, CoverageService


class CompleteCoverageService(CoverageService):
    """Seed every ResearchSpec item with a stable per-item idempotency key.

    The underlying repository historically accepted one batch idempotency key
    but applied it to every row, so PostgreSQL's ``(run_id,idempotency_key)``
    uniqueness could collapse a multi-item ResearchSpec to its first item.  The
    canonical service now calls that repository with one deterministic key per
    logical item while retaining the existing persistence schema.
    """

    def create_items_from_spec(
        self,
        run_id: UUID,
        spec: Mapping[str, Any],
        *,
        execution_mode: str = "deterministic_debug",
        idempotency_key: str | None = None,
        source_event_id: UUID | None = None,
        source_invocation_id: UUID | None = None,
    ) -> list[CoverageItem]:
        if not run_id:
            raise CoverageError("run_id is required")
        if spec is None:
            raise CoverageError("spec is required")

        items: list[dict[str, Any]] = []
        for value in spec.get("questions", []):
            items.append(
                {
                    "item_type": "question",
                    "subject_id": str(value["question_id"]),
                    "text": value.get("text", ""),
                }
            )
        for value in spec.get("claims_to_validate", []):
            items.append(
                {
                    "item_type": "claim",
                    "subject_id": str(value["claim_id"]),
                    "text": value.get("statement", ""),
                }
            )
        for value in spec.get("freshness_requirements", []):
            items.append(
                {
                    "item_type": "freshness_requirement",
                    "subject_id": str(value["requirement_id"]),
                    "text": value.get("description", ""),
                }
            )
        for value in spec.get("required_source_classes", []):
            items.append(
                {
                    "item_type": "source_requirement",
                    "subject_id": str(value["requirement_id"]),
                    "text": value.get("source_class", ""),
                }
            )
        for key, item_type in (
            ("corroboration_requirements", "corroboration_requirement"),
            ("contradiction_requirements", "contradiction_requirement"),
        ):
            for value in spec.get(key, []):
                items.append(
                    {
                        "item_type": item_type,
                        "subject_id": str(value["requirement_id"]),
                        "text": value.get("description", ""),
                    }
                )
        if not items:
            raise CoverageError(
                "ResearchSpec must contain at least one question, claim, or requirement"
            )

        base_key = idempotency_key or f"spec:items:{run_id}"
        item_ids: list[UUID] = []
        with self.uow_factory() as uow:
            for item in items:
                with uow.connection.cursor() as cursor:
                    cursor.execute(
                        """SELECT item_id FROM coverage_events
                             WHERE run_id=%s AND event_type='item_created'
                               AND item_type=%s AND subject_id=%s
                             ORDER BY coverage_revision,id""",
                        (run_id, item["item_type"], item["subject_id"]),
                    )
                    existing = cursor.fetchall()
                if len(existing) > 1:
                    raise CoverageError(
                        "coverage item identity is ambiguous for "
                        f"{item['item_type']}:{item['subject_id']}"
                    )
                if existing:
                    item_ids.append(UUID(str(existing[0][0])))
                    continue
                logical_key = f"{base_key}:{item['item_type']}:{item['subject_id']}"
                uow.coverage.create_items(
                    run_id,
                    [item],
                    idempotency_key=logical_key,
                    source_event_id=source_event_id,
                    source_invocation_id=source_invocation_id,
                    execution_mode=execution_mode,
                )
                with uow.connection.cursor() as cursor:
                    cursor.execute(
                        """SELECT item_id FROM coverage_events
                             WHERE run_id=%s AND event_type='item_created'
                               AND item_type=%s AND subject_id=%s
                             ORDER BY coverage_revision,id""",
                        (run_id, item["item_type"], item["subject_id"]),
                    )
                    exact = cursor.fetchall()
                if len(exact) != 1:
                    raise CoverageError(
                        "coverage item persistence did not resolve exactly one identity "
                        f"for {item['item_type']}:{item['subject_id']}"
                    )
                item_ids.append(UUID(str(exact[0][0])))

            for item, item_id in zip(items, item_ids, strict=True):
                if item["item_type"] != "freshness_requirement":
                    continue
                uow.coverage.apply_event(
                    run_id=run_id,
                    event_type="freshness_observed",
                    item_id=item_id,
                    item_type="freshness_requirement",
                    subject_id=item["subject_id"],
                    new_freshness_status="uncertain",
                    payload={"freshness_status": "uncertain"},
                    idempotency_key=(
                        f"{base_key}:initial-freshness:{item['subject_id']}"
                    ),
                )

        return [
            CoverageItem(
                coverage_item_id=item_id,
                item_type=CoverageItemType(item["item_type"]),
                subject_id=item["subject_id"],
                status=CoverageStatus.UNASSESSED,
                candidate_ids=(),
                snapshot_ids=(),
                passage_ids=(),
                independent_source_count=0,
                required_independent_source_count=0,
                authority_classes_present=(),
                freshness_status=(
                    FreshnessStatus.UNCERTAIN
                    if item["item_type"] == "freshness_requirement"
                    else FreshnessStatus.NOT_APPLICABLE
                ),
                remaining_gap=item.get("text", ""),
                confidence=0.0,
                mechanical_failure_ids=(),
            )
            for item, item_id in zip(items, item_ids, strict=True)
        ]


__all__ = ["CompleteCoverageService"]
