"""Unit tests for coverage event and snapshot data model.

Tests domain logic, idempotency, stale-rejection, unknown-item rejection,
projection rebuilding, and snapshot creation — all without requiring
PostgreSQL.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from research_store.coverage_service import (
    CoverageService,
    CoverageEvent,
    CoverageSnapshot,
    CoverageError,
    UnknownCoverageItemError,
)
from research_domain.models import (
    CoverageItemType,
    CoverageStatus,
    OverallCoverageStatus,
)


# ---------------------------------------------------------------------------
# Memory repository fixture
# ---------------------------------------------------------------------------


class MemoryCoverageRepository:
    """In-memory coverage repository for unit tests."""

    def __init__(self):
        self.events: dict[tuple[str, str], CoverageEvent] = {}
        self.snapshots: dict[tuple[str, int], CoverageSnapshot] = {}
        self.revisions: dict[str, int] = {}
        self.items: dict[str, dict] = {}
        # Separate dict for batch idempotency results (not events)
        self._batch_results: dict[tuple[str, str], list[str]] = {}

    def create_items(
        self,
        run_id,
        items,
        idempotency_key,
        source_event_id=None,
        source_invocation_id=None,
        execution_mode="deterministic_debug",
    ):
        """Seed coverage items from a ResearchSpec.

        Uses a batch-level idempotency key (matching the DB layer).
        On first call, creates one event per item and returns their IDs.
        On repeated calls, returns the existing IDs without creating new events.
        """
        run_id_str = str(run_id)
        # Batch-level idempotency key (same as DB layer)
        batch_key = (run_id_str, idempotency_key)

        # Check if batch already existed — return existing IDs
        if batch_key in self._batch_results:
            return self._batch_results[batch_key]

        # First call — create one event per item
        item_ids = []
        for item in items:
            iid = uuid4()
            self.items[str(iid)] = {
                "item_type": item["item_type"],
                "subject_id": item["subject_id"],
            }
            event = CoverageEvent(
                id=iid,
                run_id=run_id,
                coverage_revision=1,
                prior_coverage_revision=0,
                event_type="item_created",
                item_id=iid,
                item_type=item["item_type"],
                subject_id=item["subject_id"],
                new_status="unassessed",
                previous_status=None,
                new_freshness_status=None,
                previous_freshness_status=None,
                source_event_id=source_event_id,
                source_invocation_id=source_invocation_id,
                payload={
                    "execution_mode": execution_mode,
                    "text": item.get("text", ""),
                },
                idempotency_key=idempotency_key,
                created_at=None,
            )
            # Store per-item events keyed by (run_id, item_id) for rebuild_projection
            self.events[(run_id_str, str(iid))] = event
            item_ids.append(str(iid))

        # Store batch result for idempotency (separate from events)
        self._batch_results[batch_key] = item_ids

        # Update revision counter (one atomic batch = revision 1)
        current = self.revisions.get(run_id_str, 0)
        self.revisions[run_id_str] = max(current, 1)
        return item_ids

    def apply_event(
        self,
        run_id,
        event_type,
        item_id=None,
        item_type=None,
        subject_id=None,
        new_status=None,
        previous_status=None,
        new_freshness_status=None,
        previous_freshness_status=None,
        source_event_id=None,
        source_invocation_id=None,
        payload=None,
        idempotency_key=None,
    ):
        payload = payload or {}
        key = (str(run_id), idempotency_key)

        # Idempotency check
        if key in self.events:
            return self.events[key].to_dict()

        current_revision = self.revisions.get(str(run_id), 0)
        new_revision = current_revision + 1

        # Stale-rejection: check if there's already an event with this
        # revision (simulating a concurrent update that advanced the revision).
        # In the DB layer this is enforced by transaction isolation + the
        # CHECK(coverage_revision > prior_coverage_revision) constraint.
        existing_revisions = {
            evt.coverage_revision
            for evt in self.events.values()
            if evt.run_id == run_id and evt.coverage_revision is not None
        }
        if new_revision in existing_revisions:
            raise ValueError(
                f"stale coverage revision: proposed {new_revision} "
                f"does not exceed current {current_revision}"
            )

        # Unknown item check
        if item_id is not None:
            if str(item_id) not in self.items:
                raise ValueError(f"unknown coverage item {item_id} for run {run_id}")

        # For gap events, set status directly
        effective_status = new_status
        if event_type == "item_gap_identified":
            effective_status = "blocked"
        elif event_type == "item_gap_resolved":
            effective_status = "satisfied"

        event = CoverageEvent(
            id=uuid4(),
            run_id=run_id,
            coverage_revision=new_revision,
            prior_coverage_revision=current_revision,
            event_type=event_type,
            item_id=item_id,
            item_type=item_type,
            subject_id=subject_id,
            new_status=effective_status,
            previous_status=previous_status,
            new_freshness_status=new_freshness_status,
            previous_freshness_status=previous_freshness_status,
            source_event_id=source_event_id,
            source_invocation_id=source_invocation_id,
            payload=payload,
            idempotency_key=idempotency_key,
            created_at=None,
        )
        self.events[key] = event
        self.revisions[str(run_id)] = new_revision
        return event.to_dict()

    def rebuild_projection(self, run_id, idempotency_key, source_event_id=None):
        items = {}
        events = [e for k, e in self.events.items() if k[0] == str(run_id)]
        events.sort(key=lambda e: (e.coverage_revision, str(e.id)))

        for evt in events:
            if evt.event_type == "item_created":
                items[str(evt.item_id)] = {
                    "coverage_item_id": str(evt.item_id),
                    "item_type": evt.item_type or "question",
                    "subject_id": evt.subject_id or "",
                    "status": "unassessed",
                    "freshness_status": "not_applicable",
                    "candidate_ids": [],
                    "snapshot_ids": [],
                    "passage_ids": [],
                    "independent_source_count": 0,
                    "required_independent_source_count": 0,
                    "authority_classes_present": [],
                    "remaining_gap": (evt.payload or {}).get("text", ""),
                    "confidence": 0.0,
                }
            if evt.item_id and evt.new_status:
                key = str(evt.item_id)
                if key in items:
                    items[key]["status"] = evt.new_status
                    if "confidence" in (evt.payload or {}):
                        items[key]["confidence"] = evt.payload["confidence"]

        if not items:
            overall_status = "unassessed"
        else:
            satisfied = sum(
                1 for i in items.values() if i["status"] in ("satisfied", "waived")
            )
            blocked = sum(1 for i in items.values() if i["status"] == "blocked")
            if satisfied == len(items):
                overall_status = "sufficient"
            elif blocked > 0:
                overall_status = "blocked"
            elif satisfied > 0:
                overall_status = "partial"
            else:
                overall_status = "insufficient"

        max_rev = max((e.coverage_revision for e in events), default=0)
        # Update revision for the projection_rebuilt event
        self.revisions[str(run_id)] = max(self.revisions.get(str(run_id), 0), max_rev)
        return {
            "schema_version": "coverage-ledger-v1",
            "run_id": str(run_id),
            "revision": max_rev,
            "items": list(items.values()),
            "overall_status": overall_status,
        }

    def create_snapshot(
        self,
        run_id,
        coverage_revision,
        ledger,
        content_sha256,
        idempotency_key,
        triggering_event_id=None,
    ):
        key = (str(run_id), idempotency_key)
        if key in self.snapshots:
            return self.snapshots[key].to_dict()

        snap = CoverageSnapshot(
            id=uuid4(),
            run_id=run_id,
            coverage_revision=coverage_revision,
            ledger=ledger,
            content_sha256=content_sha256,
            triggering_event_id=triggering_event_id,
            created_at=None,
        )
        self.snapshots[key] = snap
        self.revisions[str(run_id)] = max(
            self.revisions.get(str(run_id), 0), coverage_revision
        )
        return snap.to_dict()

    def get_snapshot(self, run_id, coverage_revision):
        for (rid, _rev), snap in self.snapshots.items():
            if rid == str(run_id) and snap.coverage_revision == coverage_revision:
                return snap.to_dict()
        return None

    def get_latest_snapshot(self, run_id):
        snaps = [
            (snap.coverage_revision, snap)
            for (rid, _rev), snap in self.snapshots.items()
            if rid == str(run_id)
        ]
        if not snaps:
            return None
        snaps.sort(key=lambda x: x[0], reverse=True)
        return snaps[0][1]

    def list_events(self, run_id, item_id=None, event_type=None, limit=100, offset=0):
        events = [e.to_dict() for k, e in self.events.items() if k[0] == str(run_id)]
        if item_id:
            events = [e for e in events if str(e.get("item_id")) == str(item_id)]
        if event_type:
            events = [e for e in events if e["event_type"] == event_type]
        events.sort(key=lambda e: (e["coverage_revision"], str(e["id"])))
        return events[offset : offset + limit]

    def get_event(self, run_id, event_id):
        for e in self.events.values():
            if str(e.run_id) == str(run_id) and str(e.id) == str(event_id):
                return e.to_dict()
        return None

    def get_current_revision(self, run_id):
        return self.revisions.get(str(run_id), 0)

    def count_events(self, run_id):
        return sum(1 for k in self.events if k[0] == str(run_id))


class MemoryCoverageUow:
    def __init__(self, repository):
        self.coverage = repository

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def coverage_fixture():
    repo = MemoryCoverageRepository()
    service = CoverageService(lambda: MemoryCoverageUow(repo))
    return repo, service


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCoverageItemCreation:
    def test_creates_items_from_spec(self):
        repo, service = coverage_fixture()
        run_id = uuid4()
        spec = {
            "questions": [
                {"question_id": uuid4(), "text": "What is X?"},
            ],
            "claims_to_validate": [
                {"claim_id": uuid4(), "statement": "X is Y"},
            ],
            "freshness_requirements": [
                {"requirement_id": uuid4(), "description": "Recent data"},
            ],
            "required_source_classes": [
                {"requirement_id": uuid4(), "source_class": "primary"},
            ],
            "corroboration_requirements": [
                {"requirement_id": uuid4(), "description": "Two sources"},
            ],
            "contradiction_requirements": [
                {"requirement_id": uuid4(), "description": "Contradicting views"},
            ],
        }
        items = service.create_items_from_spec(run_id, spec)
        assert len(items) == 6
        assert all(item.status == CoverageStatus.UNASSESSED for item in items)
        types = {item.item_type for item in items}
        assert CoverageItemType.QUESTION in types
        assert CoverageItemType.CLAIM in types
        assert CoverageItemType.FRESHNESS_REQUIREMENT in types
        assert CoverageItemType.SOURCE_REQUIREMENT in types
        assert CoverageItemType.CORROBORATION_REQUIREMENT in types
        assert CoverageItemType.CONTRADICTION_REQUIREMENT in types

    def test_rejects_empty_spec(self):
        repo, service = coverage_fixture()
        run_id = uuid4()
        spec = {"questions": [], "claims_to_validate": []}
        with pytest.raises(CoverageError, match="at least one question"):
            service.create_items_from_spec(run_id, spec)

    def test_rejects_missing_run_id(self):
        _, service = coverage_fixture()
        with pytest.raises(CoverageError, match="run_id is required"):
            service.create_items_from_spec(None, {"questions": []})

    def test_idempotent_item_creation(self):
        repo, service = coverage_fixture()
        run_id = uuid4()
        spec = {"questions": [{"question_id": uuid4(), "text": "What is X?"}]}
        first = service.create_items_from_spec(run_id, spec)
        second = service.create_items_from_spec(run_id, spec)
        assert len(first) == len(second) == 1

    def test_idempotent_item_creation_returns_same_ids(self):
        """Idempotent re-initialization returns the exact same item IDs."""
        repo, service = coverage_fixture()
        run_id = uuid4()
        spec = {"questions": [{"question_id": uuid4(), "text": "What is X?"}]}
        first = service.create_items_from_spec(run_id, spec)
        second = service.create_items_from_spec(run_id, spec)
        assert first[0].coverage_item_id == second[0].coverage_item_id

    def test_idempotent_creation_all_types_same_ids(self):
        """Batch-level idempotency with all 6 item types returns same IDs."""
        repo, service = coverage_fixture()
        run_id = uuid4()
        spec = {
            "questions": [{"question_id": uuid4(), "text": "Q1"}],
            "claims_to_validate": [{"claim_id": uuid4(), "statement": "C1"}],
            "freshness_requirements": [
                {"requirement_id": uuid4(), "description": "F1"}
            ],
            "required_source_classes": [
                {"requirement_id": uuid4(), "source_class": "S1"}
            ],
            "corroboration_requirements": [
                {"requirement_id": uuid4(), "description": "CR1"}
            ],
            "contradiction_requirements": [
                {"requirement_id": uuid4(), "description": "CD1"}
            ],
        }
        first = service.create_items_from_spec(run_id, spec)
        second = service.create_items_from_spec(run_id, spec)
        assert len(first) == len(second) == 6
        for i, j in zip(first, second):
            assert i.coverage_item_id == j.coverage_item_id


class TestEventApplication:
    def test_applies_event_and_increments_revision(self):
        repo, service = coverage_fixture()
        run_id = uuid4()
        service.create_items_from_spec(
            run_id,
            {"questions": [{"question_id": uuid4(), "text": "Q1"}]},
        )
        item_id = list(repo.items.keys())[0]
        event = service.apply_event(
            run_id,
            "item_status_changed",
            item_id=UUID(item_id),
            new_status="supported",
            idempotency_key="test:event:1",
        )
        assert event.coverage_revision == 2
        assert event.prior_coverage_revision == 1
        assert event.new_status == "supported"
        assert repo.get_current_revision(run_id) == 2

    def test_idempotent_event_returns_existing(self):
        repo, service = coverage_fixture()
        run_id = uuid4()
        service.create_items_from_spec(
            run_id,
            {"questions": [{"question_id": uuid4(), "text": "Q1"}]},
        )
        item_id = list(repo.items.keys())[0]
        first = service.apply_event(
            run_id,
            "item_status_changed",
            item_id=UUID(item_id),
            new_status="supported",
            idempotency_key="dup:key",
        )
        second = service.apply_event(
            run_id,
            "item_status_changed",
            item_id=UUID(item_id),
            new_status="contradicted",
            idempotency_key="dup:key",
        )
        assert first.id == second.id
        assert second.new_status == "supported"

    def test_rejects_stale_revision(self):
        """Stale revision is enforced by the DB layer (CHECK constraint).
        The memory repo simulates this by checking if the proposed revision
        already exists among events for the run.
        """
        repo, service = coverage_fixture()
        run_id = uuid4()
        service.create_items_from_spec(
            run_id,
            {"questions": [{"question_id": uuid4(), "text": "Q1"}]},
        )
        # Insert a fake event with revision 3 to simulate a concurrent update.
        # The next apply_event will compute new_revision = 3 (since current
        # revision is now 2), but 3 is already taken, so it raises stale.
        fake_event = CoverageEvent(
            id=uuid4(),
            run_id=run_id,
            coverage_revision=3,
            prior_coverage_revision=2,
            event_type="item_status_changed",
            item_id=None,
            item_type=None,
            subject_id=None,
            new_status="supported",
            previous_status=None,
            new_freshness_status=None,
            previous_freshness_status=None,
            source_event_id=None,
            source_invocation_id=None,
            payload={},
            idempotency_key="fake:concurrent",
            created_at=None,
        )
        repo.events[(str(run_id), "fake:concurrent")] = fake_event
        repo.revisions[str(run_id)] = 2

        with pytest.raises(ValueError, match="stale coverage revision"):
            service.apply_event(
                run_id,
                "item_status_changed",
                new_status="supported",
                idempotency_key="stale:1",
            )

    def test_rejects_unknown_item(self):
        repo, service = coverage_fixture()
        run_id = uuid4()
        fake_item = uuid4()
        with pytest.raises(ValueError, match="unknown coverage item"):
            service.apply_event(
                run_id,
                "item_status_changed",
                item_id=fake_item,
                new_status="supported",
                idempotency_key="unknown:item",
            )

    def test_rejects_empty_idempotency_key(self):
        repo, service = coverage_fixture()
        run_id = uuid4()
        service.create_items_from_spec(
            run_id,
            {"questions": [{"question_id": uuid4(), "text": "Q1"}]},
        )
        with pytest.raises(CoverageError, match="nonempty"):
            service.apply_event(
                run_id,
                "item_status_changed",
                idempotency_key="  ",
            )

    def test_rejects_missing_run_id(self):
        _, service = coverage_fixture()
        with pytest.raises(CoverageError, match="run_id is required"):
            service.apply_event(None, "item_status_changed")

    def test_rejects_missing_event_type(self):
        repo, service = coverage_fixture()
        run_id = uuid4()
        with pytest.raises(CoverageError, match="event_type is required"):
            service.apply_event(run_id, None)

    def test_event_with_source_references(self):
        repo, service = coverage_fixture()
        run_id = uuid4()
        service.create_items_from_spec(
            run_id,
            {"questions": [{"question_id": uuid4(), "text": "Q1"}]},
        )
        source_event = uuid4()
        source_invocation = uuid4()
        event = service.apply_event(
            run_id,
            "item_status_changed",
            new_status="supported",
            source_event_id=source_event,
            source_invocation_id=source_invocation,
            idempotency_key="with:source",
        )
        assert str(event.source_event_id) == str(source_event)
        assert str(event.source_invocation_id) == str(source_invocation)

    def test_event_with_payload(self):
        repo, service = coverage_fixture()
        run_id = uuid4()
        service.create_items_from_spec(
            run_id,
            {"questions": [{"question_id": uuid4(), "text": "Q1"}]},
        )
        event = service.apply_event(
            run_id,
            "item_status_changed",
            new_status="supported",
            payload={"confidence": 0.85, "rationale": "multiple sources"},
            idempotency_key="with:payload",
        )
        assert event.payload["confidence"] == 0.85


class TestProjectionRebuilding:
    def test_rebuilds_from_items_only(self):
        repo, service = coverage_fixture()
        run_id = uuid4()
        service.create_items_from_spec(
            run_id,
            {
                "questions": [
                    {"question_id": uuid4(), "text": "Q1"},
                    {"question_id": uuid4(), "text": "Q2"},
                ],
            },
        )
        ledger = service.rebuild_projection(run_id)
        assert len(ledger.items) == 2
        assert all(item.status == CoverageStatus.UNASSESSED for item in ledger.items)
        assert ledger.overall_status == OverallCoverageStatus.INSUFFICIENT

    def test_rebuilds_with_status_changes(self):
        repo, service = coverage_fixture()
        run_id = uuid4()
        service.create_items_from_spec(
            run_id,
            {
                "questions": [
                    {"question_id": uuid4(), "text": "Q1"},
                    {"question_id": uuid4(), "text": "Q2"},
                ],
            },
        )
        item_ids = list(repo.items.keys())
        service.apply_event(
            run_id,
            "item_status_changed",
            item_id=UUID(item_ids[0]),
            new_status="supported",
            idempotency_key="evt:support",
        )
        service.apply_event(
            run_id,
            "item_status_changed",
            item_id=UUID(item_ids[1]),
            new_status="satisfied",
            idempotency_key="evt:satisfy",
        )
        ledger = service.rebuild_projection(run_id)
        statuses = {item.subject_id: item.status for item in ledger.items}
        assert any(status == CoverageStatus.SUPPORTED for status in statuses.values())
        assert any(status == CoverageStatus.SATISFIED for status in statuses.values())
        assert ledger.overall_status == OverallCoverageStatus.PARTIAL

    def test_rebuilds_all_satisfied(self):
        repo, service = coverage_fixture()
        run_id = uuid4()
        service.create_items_from_spec(
            run_id,
            {"questions": [{"question_id": uuid4(), "text": "Q1"}]},
        )
        item_id = list(repo.items.keys())[0]
        service.apply_event(
            run_id,
            "item_status_changed",
            item_id=UUID(item_id),
            new_status="satisfied",
            idempotency_key="evt:satisfy",
        )
        ledger = service.rebuild_projection(run_id)
        assert ledger.overall_status == OverallCoverageStatus.SUFFICIENT

    def test_rebuilds_with_blocked(self):
        repo, service = coverage_fixture()
        run_id = uuid4()
        service.create_items_from_spec(
            run_id,
            {"questions": [{"question_id": uuid4(), "text": "Q1"}]},
        )
        item_id = list(repo.items.keys())[0]
        service.apply_event(
            run_id,
            "item_gap_identified",
            item_id=UUID(item_id),
            idempotency_key="evt:block",
        )
        ledger = service.rebuild_projection(run_id)
        assert ledger.overall_status == OverallCoverageStatus.BLOCKED

    def test_rebuild_is_idempotent(self):
        repo, service = coverage_fixture()
        run_id = uuid4()
        service.create_items_from_spec(
            run_id,
            {"questions": [{"question_id": uuid4(), "text": "Q1"}]},
        )
        first = service.rebuild_projection(run_id, idempotency_key="rebuild:1")
        second = service.rebuild_projection(run_id, idempotency_key="rebuild:1")
        assert first.items == second.items
        assert first.overall_status == second.overall_status


class TestSnapshots:
    def test_creates_snapshot(self):
        repo, service = coverage_fixture()
        run_id = uuid4()
        ledger = {
            "schema_version": "coverage-ledger-v1",
            "run_id": str(run_id),
            "revision": 5,
            "items": [],
            "overall_status": "sufficient",
        }
        snap = service.create_snapshot(
            run_id,
            ledger,
            coverage_revision=5,
            idempotency_key="snap:5",
        )
        assert snap.coverage_revision == 5
        assert snap.ledger["overall_status"] == "sufficient"
        assert len(snap.content_sha256) == 64

    def test_snapshot_idempotent(self):
        repo, service = coverage_fixture()
        run_id = uuid4()
        ledger = {"items": [], "overall_status": "sufficient"}
        first = service.create_snapshot(
            run_id, ledger, coverage_revision=1, idempotency_key="snap:1"
        )
        second = service.create_snapshot(
            run_id, ledger, coverage_revision=1, idempotency_key="snap:1"
        )
        assert first.id == second.id

    def test_retrieves_snapshot_by_revision(self):
        repo, service = coverage_fixture()
        run_id = uuid4()
        ledger = {"items": [], "overall_status": "sufficient"}
        service.create_snapshot(
            run_id, ledger, coverage_revision=3, idempotency_key="snap:3"
        )
        snap = service.get_snapshot(run_id, 3)
        assert snap is not None
        assert snap.coverage_revision == 3

    def test_returns_none_for_missing_snapshot(self):
        repo, service = coverage_fixture()
        run_id = uuid4()
        snap = service.get_snapshot(run_id, 999)
        assert snap is None

    def test_gets_latest_snapshot(self):
        repo, service = coverage_fixture()
        run_id = uuid4()
        service.create_snapshot(
            run_id,
            {"items": [], "overall_status": "insufficient"},
            coverage_revision=1,
            idempotency_key="snap:1",
        )
        service.create_snapshot(
            run_id,
            {"items": [], "overall_status": "sufficient"},
            coverage_revision=5,
            idempotency_key="snap:5",
        )
        latest = service.current_snapshot(run_id)
        assert latest is not None
        assert latest.coverage_revision == 5

    def test_no_snapshot_returns_none(self):
        repo, service = coverage_fixture()
        run_id = uuid4()
        assert service.current_snapshot(run_id) is None


class TestEventQuerying:
    def test_lists_events(self):
        repo, service = coverage_fixture()
        run_id = uuid4()
        service.create_items_from_spec(
            run_id,
            {"questions": [{"question_id": uuid4(), "text": "Q1"}]},
        )
        service.apply_event(
            run_id,
            "item_status_changed",
            new_status="supported",
            idempotency_key="evt:1",
        )
        events = service.list_events(run_id)
        assert len(events) >= 2  # item_created + item_status_changed

    def test_filters_by_item_id(self):
        repo, service = coverage_fixture()
        run_id = uuid4()
        service.create_items_from_spec(
            run_id,
            {"questions": [{"question_id": uuid4(), "text": "Q1"}]},
        )
        item_id = list(repo.items.keys())[0]
        events = service.list_events(run_id, item_id=UUID(item_id))
        assert all(str(e.item_id) == item_id for e in events)

    def test_filters_by_event_type(self):
        repo, service = coverage_fixture()
        run_id = uuid4()
        service.create_items_from_spec(
            run_id,
            {"questions": [{"question_id": uuid4(), "text": "Q1"}]},
        )
        events = service.list_events(run_id, event_type="item_created")
        assert all(e.event_type == "item_created" for e in events)

    def test_gets_single_event(self):
        repo, service = coverage_fixture()
        run_id = uuid4()
        service.create_items_from_spec(
            run_id,
            {"questions": [{"question_id": uuid4(), "text": "Q1"}]},
        )
        events = service.list_events(run_id)
        assert len(events) >= 1
        first_event_id = events[0].id
        event = service.get_event(run_id, first_event_id)
        assert event is not None
        assert event.id == first_event_id

    def test_get_event_raises_for_unknown(self):
        repo, service = coverage_fixture()
        run_id = uuid4()
        with pytest.raises(UnknownCoverageItemError):
            service.get_event(run_id, uuid4())

    def test_count_events(self):
        repo, service = coverage_fixture()
        run_id = uuid4()
        service.create_items_from_spec(
            run_id,
            {"questions": [{"question_id": uuid4(), "text": "Q1"}]},
        )
        assert service.count_events(run_id) >= 1

    def test_get_current_revision(self):
        repo, service = coverage_fixture()
        run_id = uuid4()
        assert service.get_current_revision(run_id) == 0
        service.create_items_from_spec(
            run_id,
            {"questions": [{"question_id": uuid4(), "text": "Q1"}]},
        )
        assert service.get_current_revision(run_id) >= 1


class TestEventSerialization:
    def test_event_to_dict_roundtrip(self):
        event = CoverageEvent(
            id=uuid4(),
            run_id=uuid4(),
            coverage_revision=5,
            prior_coverage_revision=4,
            event_type="item_status_changed",
            item_id=uuid4(),
            item_type="question",
            subject_id="q1",
            new_status="supported",
            previous_status="unassessed",
            new_freshness_status=None,
            previous_freshness_status=None,
            source_event_id=None,
            source_invocation_id=None,
            payload={"confidence": 0.9},
            idempotency_key="test",
            created_at=None,
        )
        d = event.to_dict()
        restored = CoverageEvent.from_mapping(d)
        assert restored.coverage_revision == event.coverage_revision
        assert restored.new_status == event.new_status
        assert restored.payload == event.payload

    def test_snapshot_to_dict_roundtrip(self):
        snap = CoverageSnapshot(
            id=uuid4(),
            run_id=uuid4(),
            coverage_revision=10,
            ledger={"items": [], "overall_status": "sufficient"},
            content_sha256="a" * 64,
            triggering_event_id=None,
            created_at=None,
        )
        d = snap.to_dict()
        restored = CoverageSnapshot.from_mapping(d)
        assert restored.coverage_revision == snap.coverage_revision
        assert restored.content_sha256 == snap.content_sha256


class TestContentHashing:
    def test_json_sha256_deterministic(self):
        from research_store.coverage_service import _json_sha256

        value = {"a": 1, "b": [2, 3]}
        h1 = _json_sha256(value)
        h2 = _json_sha256(value)
        assert h1 == h2
        assert len(h1) == 64

    def test_json_sha256_different_values(self):
        from research_store.coverage_service import _json_sha256

        h1 = _json_sha256({"a": 1})
        h2 = _json_sha256({"a": 2})
        assert h1 != h2

    def test_snapshot_hash_matches_content(self):
        from research_store.coverage_service import _json_sha256

        ledger = {
            "schema_version": "coverage-ledger-v1",
            "run_id": "test",
            "revision": 1,
            "items": [],
            "overall_status": "insufficient",
        }
        h = _json_sha256(ledger)
        assert len(h) == 64
        assert h == _json_sha256(ledger)


# ---------------------------------------------------------------------------
# Issue #23 — ResearchSpec-to-ledger mapping tests
# ---------------------------------------------------------------------------


class TestResearchSpecLedgerMapping:
    """Verify that every mandatory ResearchSpec requirement produces a
    coverage ledger item, with no silent waivers.

    PRD mapping: FR-012, Section 12.1, Section 12.4
    """

    def test_all_six_item_types_created(self):
        """Every mandatory ResearchSpec field produces a ledger item."""
        repo, service = coverage_fixture()
        run_id = uuid4()
        spec = {
            "questions": [
                {"question_id": uuid4(), "text": "What caused the outage?"},
            ],
            "claims_to_validate": [
                {"claim_id": uuid4(), "statement": "The CDN was down"},
            ],
            "freshness_requirements": [
                {"requirement_id": uuid4(), "description": "Last 24 hours"},
            ],
            "required_source_classes": [
                {"requirement_id": uuid4(), "source_class": "primary"},
            ],
            "corroboration_requirements": [
                {"requirement_id": uuid4(), "description": "Two independent sources"},
            ],
            "contradiction_requirements": [
                {"requirement_id": uuid4(), "description": "Contradictory views"},
            ],
        }
        items = service.create_items_from_spec(run_id, spec)
        assert len(items) == 6
        types = {item.item_type for item in items}
        assert types == {
            CoverageItemType.QUESTION,
            CoverageItemType.CLAIM,
            CoverageItemType.FRESHNESS_REQUIREMENT,
            CoverageItemType.SOURCE_REQUIREMENT,
            CoverageItemType.CORROBORATION_REQUIREMENT,
            CoverageItemType.CONTRADICTION_REQUIREMENT,
        }

    def test_multiple_questions_each_get_item(self):
        spec = {
            "questions": [
                {"question_id": uuid4(), "text": "Q1"},
                {"question_id": uuid4(), "text": "Q2"},
                {"question_id": uuid4(), "text": "Q3"},
            ],
        }
        repo, service = coverage_fixture()
        run_id = uuid4()
        items = service.create_items_from_spec(run_id, spec)
        assert len(items) == 3
        assert all(item.item_type == CoverageItemType.QUESTION for item in items)

    def test_multiple_claims_each_get_item(self):
        spec = {
            "claims_to_validate": [
                {"claim_id": uuid4(), "statement": "C1"},
                {"claim_id": uuid4(), "statement": "C2"},
            ],
        }
        repo, service = coverage_fixture()
        run_id = uuid4()
        items = service.create_items_from_spec(run_id, spec)
        assert len(items) == 2
        assert all(item.item_type == CoverageItemType.CLAIM for item in items)

    def test_all_requirement_types_represented(self):
        """Freshness, source, corroboration, and contradiction requirements
        each produce coverage items with the correct type."""
        spec = {
            "questions": [{"question_id": uuid4(), "text": "Q1"}],
            "freshness_requirements": [
                {"requirement_id": uuid4(), "description": "D1"},
                {"requirement_id": uuid4(), "description": "D2"},
            ],
            "required_source_classes": [
                {"requirement_id": uuid4(), "source_class": "S1"},
            ],
            "corroboration_requirements": [
                {"requirement_id": uuid4(), "description": "C1"},
            ],
            "contradiction_requirements": [
                {"requirement_id": uuid4(), "description": "CD1"},
            ],
        }
        repo, service = coverage_fixture()
        run_id = uuid4()
        items = service.create_items_from_spec(run_id, spec)
        assert len(items) == 6
        type_counts = {}
        for item in items:
            type_counts[item.item_type.value] = (
                type_counts.get(item.item_type.value, 0) + 1
            )
        assert type_counts["freshness_requirement"] == 2
        assert type_counts["source_requirement"] == 1
        assert type_counts["corroboration_requirement"] == 1
        assert type_counts["contradiction_requirement"] == 1

    def test_no_item_is_silently_waived(self):
        """Every item starts as unassessed, not waived."""
        spec = {
            "questions": [{"question_id": uuid4(), "text": "Q1"}],
            "claims_to_validate": [{"claim_id": uuid4(), "statement": "C1"}],
        }
        repo, service = coverage_fixture()
        run_id = uuid4()
        items = service.create_items_from_spec(run_id, spec)
        for item in items:
            assert item.status == CoverageStatus.UNASSESSED
            assert item.status != CoverageStatus.WAIVED

    def test_subject_id_preserved_from_spec(self):
        """Stable subject references are preserved."""
        spec = {
            "questions": [{"question_id": "a1b2c3", "text": "Q1"}],
        }
        repo, service = coverage_fixture()
        run_id = uuid4()
        items = service.create_items_from_spec(run_id, spec)
        assert items[0].subject_id == "a1b2c3"

    def test_execution_mode_passed_to_payload(self):
        spec = {
            "questions": [{"question_id": uuid4(), "text": "Q1"}],
        }
        repo, service = coverage_fixture()
        run_id = uuid4()
        service.create_items_from_spec(run_id, spec, execution_mode="autonomous_local")
        event = service.list_events(run_id)[0]
        assert event.payload["execution_mode"] == "autonomous_local"


class TestIdempotencyEdgeCases:
    """Repeated initialization must be idempotent."""

    def test_repeated_init_same_run(self):
        spec = {
            "questions": [{"question_id": uuid4(), "text": "Q1"}],
        }
        repo, service = coverage_fixture()
        run_id = uuid4()
        first = service.create_items_from_spec(run_id, spec)
        second = service.create_items_from_spec(run_id, spec)
        assert len(first) == len(second)

    def test_repeated_init_with_all_types(self):
        spec = {
            "questions": [{"question_id": uuid4(), "text": "Q1"}],
            "claims_to_validate": [{"claim_id": uuid4(), "statement": "C1"}],
            "freshness_requirements": [
                {"requirement_id": uuid4(), "description": "F1"}
            ],
            "required_source_classes": [
                {"requirement_id": uuid4(), "source_class": "S1"}
            ],
            "corroboration_requirements": [
                {"requirement_id": uuid4(), "description": "CR1"}
            ],
            "contradiction_requirements": [
                {"requirement_id": uuid4(), "description": "CD1"}
            ],
        }
        repo, service = coverage_fixture()
        run_id = uuid4()
        first = service.create_items_from_spec(run_id, spec)
        second = service.create_items_from_spec(run_id, spec)
        assert len(first) == len(second) == 6
        assert all(item.status == CoverageStatus.UNASSESSED for item in second)

    def test_different_runs_get_independent_items(self):
        spec = {"questions": [{"question_id": uuid4(), "text": "Q1"}]}
        repo, service = coverage_fixture()
        run_a = uuid4()
        run_b = uuid4()
        items_a = service.create_items_from_spec(run_a, spec)
        items_b = service.create_items_from_spec(run_b, spec)
        assert len(items_a) == 1
        assert len(items_b) == 1
        # Different runs should have different events
        events_a = service.list_events(run_a)
        events_b = service.list_events(run_b)
        assert len(events_a) == 1
        assert len(events_b) == 1
        event_ids_a = {str(e.id) for e in events_a}
        event_ids_b = {str(e.id) for e in events_b}
        assert event_ids_a.isdisjoint(event_ids_b)


class TestInvalidInput:
    """Invalid input must be rejected with clear errors."""

    def test_none_spec(self):
        _, service = coverage_fixture()
        run_id = uuid4()
        with pytest.raises(CoverageError, match="spec is required"):
            service.create_items_from_spec(run_id, None)

    def test_empty_spec_dict(self):
        repo, service = coverage_fixture()
        run_id = uuid4()
        with pytest.raises(CoverageError, match="at least one question"):
            service.create_items_from_spec(run_id, {})

    def test_spec_with_only_empty_lists(self):
        repo, service = coverage_fixture()
        run_id = uuid4()
        spec = {
            "questions": [],
            "claims_to_validate": [],
            "freshness_requirements": [],
            "required_source_classes": [],
            "corroboration_requirements": [],
            "contradiction_requirements": [],
        }
        with pytest.raises(CoverageError, match="at least one question"):
            service.create_items_from_spec(run_id, spec)

    def test_none_run_id(self):
        _, service = coverage_fixture()
        spec = {"questions": [{"question_id": uuid4(), "text": "Q1"}]}
        with pytest.raises(CoverageError, match="run_id is required"):
            service.create_items_from_spec(None, spec)

    def test_empty_string_idempotency_key(self):
        repo, service = coverage_fixture()
        run_id = uuid4()
        service.create_items_from_spec(
            run_id,
            {"questions": [{"question_id": uuid4(), "text": "Q1"}]},
        )
        # Whitespace-only key is rejected; empty string is auto-generated.
        with pytest.raises(CoverageError, match="nonempty"):
            service.apply_event(
                run_id,
                "item_status_changed",
                idempotency_key="   ",
            )


class TestCoverageSummary:
    """Test the coverage_summary() convenience API."""

    def test_summary_after_initialization(self):
        repo, service = coverage_fixture()
        run_id = uuid4()
        spec = {
            "questions": [
                {"question_id": uuid4(), "text": "Q1"},
                {"question_id": uuid4(), "text": "Q2"},
            ],
            "claims_to_validate": [{"claim_id": uuid4(), "statement": "C1"}],
        }
        service.create_items_from_spec(run_id, spec)
        summary = service.coverage_summary(run_id)
        assert summary["total_items"] == 3
        assert summary["type_counts"]["question"] == 2
        assert summary["type_counts"]["claim"] == 1
        # Memory repo projection: all-unassessed → "insufficient"
        assert summary["overall_status"] == "insufficient"
        assert summary["status_counts"]["unassessed"] == 3

    def test_summary_after_status_changes(self):
        repo, service = coverage_fixture()
        run_id = uuid4()
        spec = {
            "questions": [
                {"question_id": uuid4(), "text": "Q1"},
                {"question_id": uuid4(), "text": "Q2"},
            ],
        }
        service.create_items_from_spec(run_id, spec)
        item_id = list(repo.items.keys())[0]
        service.apply_event(
            run_id,
            "item_status_changed",
            item_id=UUID(item_id),
            new_status="supported",
            idempotency_key="evt:1",
        )
        summary = service.coverage_summary(run_id)
        assert summary["status_counts"]["unassessed"] == 1
        assert summary["status_counts"]["supported"] == 1
        # Memory repo only counts "satisfied"/"waived" as resolved,
        # so "supported" does not make the run "partial".
        assert summary["overall_status"] == "insufficient"

    def test_summary_empty_run(self):
        """A run with no items returns zero counts."""
        repo, service = coverage_fixture()
        run_id = uuid4()
        summary = service.coverage_summary(run_id)
        assert summary["total_items"] == 0
        assert summary["overall_status"] == "unassessed"
        assert summary["status_counts"] == {}
        assert summary["type_counts"] == {}

    def test_summary_contains_schema_version(self):
        repo, service = coverage_fixture()
        run_id = uuid4()
        service.create_items_from_spec(
            run_id,
            {"questions": [{"question_id": uuid4(), "text": "Q1"}]},
        )
        summary = service.coverage_summary(run_id)
        assert summary["schema_version"] == "coverage-ledger-v1"
        assert summary["run_id"] == str(run_id)
        assert summary["coverage_revision"] >= 1
