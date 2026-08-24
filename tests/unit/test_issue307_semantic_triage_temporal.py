"""Issue #307 regressions: bounded temporal card exposure and triage gate."""

from __future__ import annotations

import importlib.util
import json
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _load_research_workflow() -> Any:
    loader = SourceFileLoader(
        "issue307_research_workflow", str(SCRIPTS / "research_workflow.py")
    )
    spec = importlib.util.spec_from_loader("issue307_research_workflow", loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _assessment(status: str, basis: str, reason: str) -> dict[str, Any]:
    return {
        "status": status,
        "basis": basis,
        "reason": reason,
        "published_at": None,
        "updated_at": None,
        "publication_status": "unknown",
        "update_status": "unknown",
    }


def test_candidate_cards_expose_bounded_temporal_card_to_llm() -> None:
    workflow = _load_research_workflow()
    cards = workflow.candidate_cards(
        [
            {
                "candidate_id": "cand-a",
                "url": "https://example.test/a",
                "rank": 1,
                "temporal_assessment": _assessment(
                    "unknown",
                    "freshness",
                    "candidate lacks sufficient explicit temporal authority for pre-scrape proof",
                ),
            },
            {"candidate_id": "cand-b", "url": "https://example.test/b", "rank": 2},
        ]
    )

    assert cards[0]["temporal_assessment"]["status"] == "unknown"
    assert cards[1]["temporal_assessment"] is None


def _all_scrape_stub(
    _provider: Any, _model: Any, _system: str, prompt: str, *_rest: Any, **_kwargs: Any
) -> SimpleNamespace:
    cards = json.loads(prompt.split("Candidate cards:\n", 1)[1])
    return SimpleNamespace(
        value={
            "decisions": [
                {
                    "candidate_id": card["candidate_id"],
                    "relevance": "high",
                    "source_suitability": "good",
                    "scrape": True,
                    "priority": 5,
                    "rationale": "llm decision",
                }
                for card in cards
            ]
        },
        provenance={},
        attempts=1,
        error=None,
    )


def test_triage_ignores_llm_scrape_for_ineligible_candidate(
    monkeypatch: Any,
) -> None:
    workflow = _load_research_workflow()
    monkeypatch.setattr(workflow, "_structured", _all_scrape_stub)
    candidates = [
        {
            "candidate_id": "cand-ineligible",
            "url": "https://ineligible.example",
            "rank": 1,
            "temporal_assessment": _assessment(
                "ineligible",
                "publication_window",
                "known explicit temporal authority cannot satisfy the ResearchSpec",
            ),
        },
        {
            "candidate_id": "cand-eligible",
            "url": "https://eligible.example",
            "rank": 2,
            "temporal_assessment": _assessment(
                "eligible",
                "publication_window",
                "explicit temporal authority satisfies the ResearchSpec",
            ),
        },
        {
            "candidate_id": "cand-unknown",
            "url": "https://unknown.example",
            "rank": 3,
            "temporal_assessment": _assessment(
                "unknown",
                "freshness",
                "candidate lacks sufficient explicit temporal authority for pre-scrape proof",
            ),
        },
    ]

    ranked, _provenance = workflow.triage_candidates(
        "objective", {"questions": []}, candidates
    )

    assert [item["candidate_id"] for item in ranked] == [
        "cand-eligible",
        "cand-unknown",
    ]
    by_id = {item["candidate_id"]: item for item in candidates}
    forced = by_id["cand-ineligible"]["triage"]
    assert forced["scrape"] is False
    assert "deterministic temporal gate" in forced["rationale"]
    assert by_id["cand-eligible"]["triage"]["scrape"] is True
    assert by_id["cand-unknown"]["triage"]["scrape"] is True


class _FakeRuns:
    def __init__(self, spec_row: dict[str, Any] | None):
        self._spec_row = spec_row

    def get_research_spec(self, _run_id: Any) -> dict[str, Any] | None:
        return self._spec_row


class _FakeSearchResponses:
    def __init__(self, responses: dict[str, dict[str, Any]]):
        self._responses = responses

    def get_search_response(
        self, response_id: Any, run_id: Any = None
    ) -> dict[str, Any]:
        key = str(response_id)
        if key not in self._responses:
            raise KeyError(key)
        return self._responses[key]


class _FakeUow:
    def __init__(
        self, spec_row: dict[str, Any] | None, responses: dict[str, dict[str, Any]]
    ):
        self.runs = _FakeRuns(spec_row)
        self.search_responses = _FakeSearchResponses(responses)


def test_candidate_card_bounded_temporal_card_uses_persisted_reference() -> None:
    from firecrawl_skill.research_store.run_service import ResearchRunService

    run_id = uuid4()
    response_id = uuid4()
    candidate = {
        "id": uuid4(),
        "run_id": run_id,
        "published_at": "2026-08-17T00:00:00Z",
        "date_signals": {"publication_status": "explicit_provider_valid"},
    }
    spec_row = {
        "payload": {
            "time_window": {
                "start": "2026-08-18",
                "end": "2026-08-22",
                "uncertainty": "none",
            },
            "freshness_requirements": [],
        }
    }
    uow = _FakeUow(
        spec_row,
        {str(response_id): {"responded_at": "2026-08-23T12:00:00Z"}},
    )
    occurrences = [{"search_response_id": response_id}]

    card = ResearchRunService._bounded_temporal_assessment(
        None, uow, candidate, occurrences, run_id
    )

    assert card is not None
    assert card["status"] == "ineligible"
    assert card["basis"] == "publication_window"
    assert card["published_at"] == "2026-08-17T00:00:00+00:00"


def test_candidate_card_omits_temporal_card_without_persisted_reference() -> None:
    from firecrawl_skill.research_store.run_service import ResearchRunService

    run_id = uuid4()
    candidate = {"id": uuid4(), "run_id": run_id, "date_signals": {}}
    spec_row = {"payload": {"time_window": {}, "freshness_requirements": []}}

    assert (
        ResearchRunService._bounded_temporal_assessment(
            None, _FakeUow(None, {}), candidate, [], run_id
        )
        is None
    )
    assert (
        ResearchRunService._bounded_temporal_assessment(
            None, _FakeUow(spec_row, {}), candidate, [], run_id
        )
        is None
    )
