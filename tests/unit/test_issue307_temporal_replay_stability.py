"""Replay-stability regressions for deterministic temporal candidate admission.

Issue #307: candidate temporal admission must be evaluated against the persisted
search-response timestamp (``search_responses.responded_at``), never the wall
clock. An idempotent replay of the same persisted search response must
therefore reproduce the same assessments and the same
``acquisition.temporal_admission`` event payload, regardless of when the replay
happens.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Self
from uuid import UUID, uuid4

from firecrawl_skill.research_store import candidate_temporal_policy
from firecrawl_skill.research_store.acquisition.models import AcquisitionResult
from firecrawl_skill.research_store.acquisition.temporal_acquisition import (
    TemporalAcquisitionService,
)
from firecrawl_skill.research_store.candidate_temporal_policy import (
    assess_candidate_temporal,
)

PERSISTED_RESPONSE_AT = datetime(2026, 8, 1, tzinfo=timezone.utc)
LATER_WALL_CLOCK = PERSISTED_RESPONSE_AT + timedelta(days=730)
FRESHNESS_SPEC = {"freshness_requirements": [{"max_age_days": 30}]}
CANDIDATE_ID = "00000000-0000-0000-0000-00000000c001"


def _candidate() -> dict[str, Any]:
    one_day_before = PERSISTED_RESPONSE_AT - timedelta(days=1)
    return {
        "published_at": one_day_before.isoformat(),
        "date_signals": {
            "publication_status": "explicit_provider_valid",
            "update_status": "explicit_provider_valid",
            "updated_date": one_day_before.isoformat(),
        },
    }


class TestPolicyReplayStability:
    def test_same_persisted_reference_is_deterministic(self) -> None:
        first = assess_candidate_temporal(
            _candidate(), FRESHNESS_SPEC, now=PERSISTED_RESPONSE_AT
        )
        second = assess_candidate_temporal(
            _candidate(), FRESHNESS_SPEC, now=PERSISTED_RESPONSE_AT
        )
        assert first == second
        assert first.to_dict() == second.to_dict()
        assert first.status == "eligible"

    def test_later_reference_would_differ(self) -> None:
        # Proves the regression is meaningful: the same persisted candidate is
        # ineligible when the evaluation reference is far in the future, so a
        # wall-clock reference would have flipped the admission result on replay.
        later = assess_candidate_temporal(
            _candidate(), FRESHNESS_SPEC, now=LATER_WALL_CLOCK
        )
        assert later.status == "ineligible"
        assert (
            later.to_dict()
            != assess_candidate_temporal(
                _candidate(), FRESHNESS_SPEC, now=PERSISTED_RESPONSE_AT
            ).to_dict()
        )


class _FakeRuns:
    def __init__(self, owner: _FakeUow) -> None:
        self._owner = owner

    def get_research_spec(self, run_id: UUID) -> dict[str, Any] | None:
        if self._owner.spec_payload is None:
            return None
        return {
            "id": "spec-1",
            "run_id": str(run_id),
            "spec_revision": 1,
            "payload": self._owner.spec_payload,
        }

    def append_event(
        self,
        run_id: UUID,
        event_type: str,
        actor_type: str,
        idempotency_key: str,
        *,
        invocation_id: str | None = None,
        actor_identifier: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> str:
        self._owner.events.append((event_type, idempotency_key, payload))
        return "event-1"


class _FakeCandidates:
    def __init__(self, owner: _FakeUow) -> None:
        self._owner = owner

    def get_candidate(
        self, candidate_id: UUID, run_id: UUID | None = None
    ) -> dict[str, Any] | None:
        return self._owner.candidate


class _FakeCursor:
    def execute(self, *args: Any, **kwargs: Any) -> None:
        return None

    def fetchone(self) -> None:
        return None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class _FakeConnection:
    def cursor(self) -> _FakeCursor:
        return _FakeCursor()


class _FakeUow:
    def __init__(
        self,
        spec_payload: dict[str, Any] | None,
        candidate: dict[str, Any] | None,
    ) -> None:
        self.spec_payload = spec_payload
        self.candidate = candidate
        self.events: list[tuple[str, str, dict[str, Any] | None]] = []
        self.committed = 0
        self.connection = _FakeConnection()
        self.runs = _FakeRuns(self)
        self.candidates = _FakeCandidates(self)

    def commit(self) -> None:
        self.committed += 1

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


@dataclass
class _FakeResult:
    search_response: dict[str, Any] = field(default_factory=dict)
    candidates: list[dict[str, Any]] = field(default_factory=list)
    search_response_id: UUID = field(default_factory=uuid4)
    candidate_count: int = 0


class _FakeDelegate:
    def __init__(self, result: _FakeResult, uow_factory: Any) -> None:
        self._result = result
        self.uow_factory = uow_factory
        self.calls = 0

    def execute_search(self, *args: Any, **kwargs: Any) -> _FakeResult:
        self.calls += 1
        return self._result


class _FakeDateTime:
    """Stand-in for the wall clock that the admission policy must never use."""

    @staticmethod
    def now(tz: Any = None) -> datetime:
        return LATER_WALL_CLOCK


def test_service_threads_persisted_reference_across_wall_clocks(
    monkeypatch: Any,
) -> None:
    uow = _FakeUow(FRESHNESS_SPEC, _candidate())
    result = _FakeResult(
        search_response={"responded_at": PERSISTED_RESPONSE_AT},
        candidates=[
            {
                "candidate_id": CANDIDATE_ID,
                "raw_item": {
                    "url": "https://example.org/x",
                    "date": "2026-08-22T12:00:00Z",
                },
            }
        ],
    )
    delegate = _FakeDelegate(result, lambda: uow)
    service = TemporalAcquisitionService(delegate)

    first = service.execute_search(uuid4(), "query", tbs="qdr:30d")
    first_admission = first.search_response["temporal_admission"]
    assert first_admission["eligible"] == 1
    assert first_admission["evaluated_at"] == PERSISTED_RESPONSE_AT.isoformat()

    # Simulate a replay that happens much later on the wall clock. The service
    # must still evaluate the persisted response at its own responded_at, so the
    # admission payload is byte-stable across replays.
    monkeypatch.setattr(candidate_temporal_policy, "datetime", _FakeDateTime)
    second = service.execute_search(uuid4(), "query", tbs="qdr:30d")
    second_admission = second.search_response["temporal_admission"]
    assert second_admission == first_admission
    assert second_admission["eligible"] == 1

    event_payloads = [payload for _, _, payload in uow.events if payload]
    assert len(event_payloads) == 2
    assert event_payloads[0] == event_payloads[1]


def test_generic_provider_date_never_populates_admission_reference() -> None:
    # A generic provider date carried by the raw item must not leak into the
    # admission reference: the reference comes only from the persisted response.
    uow = _FakeUow(FRESHNESS_SPEC, _candidate())
    result = _FakeResult(
        search_response={
            "responded_at": PERSISTED_RESPONSE_AT,
            "date": "1999-01-01T00:00:00Z",
        },
        candidates=[{"candidate_id": CANDIDATE_ID, "raw_item": {}}],
    )
    service = TemporalAcquisitionService(_FakeDelegate(result, lambda: uow))
    out = service.execute_search(uuid4(), "query", tbs="qdr:30d")
    assert out.search_response["temporal_admission"]["evaluated_at"] == (
        PERSISTED_RESPONSE_AT.isoformat()
    )


def _acquisition_result(search_response: dict[str, Any]) -> AcquisitionResult:
    return AcquisitionResult(
        search_response_id=uuid4(),
        run_id=uuid4(),
        query_text="replay-stability query",
        backend="firecrawl",
        status="ok",
        candidate_count=0,
        candidates=[],
        postgres_committed=True,
        search_response=search_response,
    )


class TestPersistedResponseReference:
    def test_returns_persisted_datetime(self) -> None:
        result = _acquisition_result({"responded_at": PERSISTED_RESPONSE_AT})
        reference = TemporalAcquisitionService._persisted_response_reference(result)
        assert reference == PERSISTED_RESPONSE_AT

    def test_missing_reference_returns_none(self) -> None:
        result = _acquisition_result({})
        assert TemporalAcquisitionService._persisted_response_reference(result) is None

    def test_non_datetime_reference_returns_none(self) -> None:
        result = _acquisition_result({"responded_at": "2026-08-01T00:00:00Z"})
        assert TemporalAcquisitionService._persisted_response_reference(result) is None
