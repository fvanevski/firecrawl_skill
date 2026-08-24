from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from firecrawl_skill.research_store import inspection_cli
from firecrawl_skill.research_store.identity_resolver import CorpusIdentityResolutionError
from firecrawl_skill.research_store.inspection_contract import (
    InspectionNotFoundError,
    PassageBounds,
)
from firecrawl_skill.research_store.inspection_service import (
    InspectionIdentityError,
    InspectionService,
)


def test_search_response_inspection_does_not_require_corpus_resolver(monkeypatch) -> None:
    service = object.__new__(InspectionService)
    response_id = uuid4()
    expected = {
        "schema_version": "database-native-inspection-v1",
        "kind": "asset_inspection",
        "asset_id": str(response_id),
        "matches": [{"asset_type": "search_response", "id": str(response_id)}],
        "match_count": 1,
    }
    monkeypatch.setattr(
        "firecrawl_skill.research_store.inspection_service.inspect_asset",
        lambda _service, _identifier: expected,
    )
    monkeypatch.setattr(
        service,
        "resolve_identity",
        lambda _identifier: (_ for _ in ()).throw(
            AssertionError("search-response inspection must not invoke corpus resolver")
        ),
    )

    assert service.inspect_asset(response_id) == expected


def test_promotion_subject_passages_use_exact_subject_snapshot(monkeypatch) -> None:
    service = object.__new__(InspectionService)
    subject_id = uuid4()
    exact_snapshot = uuid4()
    unrelated_snapshot = uuid4()
    calls = []
    identity = {
        "identifier": str(subject_id),
        "identity_type": "promotion_subject",
        "related_ids": {
            "snapshot": [str(unrelated_snapshot), str(exact_snapshot)],
        },
    }

    def fake_passages(_service, identifier, _bounds):
        calls.append(identifier)
        if identifier == subject_id:
            raise InspectionNotFoundError("subject is not a direct corpus key")
        assert identifier == exact_snapshot
        return {"kind": "passages", "asset_id": str(identifier), "items": []}

    monkeypatch.setattr(
        "firecrawl_skill.research_store.inspection_service.passages", fake_passages
    )
    monkeypatch.setattr(service, "resolve_identity", lambda _identifier: identity)
    monkeypatch.setattr(
        service, "_promotion_subject_snapshot", lambda _identifier: exact_snapshot
    )

    result = service.passages(subject_id, PassageBounds())

    assert calls == [subject_id, exact_snapshot]
    assert result["asset_id"] == str(subject_id)
    assert result["resolved_asset_id"] == str(exact_snapshot)


def test_resolver_ambiguity_maps_to_structured_unsupported_identity(monkeypatch) -> None:
    service = object.__new__(InspectionService)
    identifier = uuid4()

    class _Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    service.connection_factory = lambda: _Connection()
    monkeypatch.setattr(
        "firecrawl_skill.research_store.inspection_service.resolve_corpus_identity",
        lambda _connection, _identifier: (_ for _ in ()).throw(
            CorpusIdentityResolutionError(
                "ambiguous",
                code="ambiguous_identity",
                identifier=identifier,
                identity_types=("chunk", "document"),
            )
        ),
    )

    with pytest.raises(InspectionIdentityError) as exc_info:
        service.resolve_identity(identifier)
    assert exc_info.value.code == "unsupported_identity_type"
    assert exc_info.value.details["identity_types"] == ["chunk", "document"]


def test_cli_serializes_typed_inspection_diagnostic(monkeypatch, capsys) -> None:
    identifier = uuid4()

    class _Service:
        def passages(self, *_args):
            raise InspectionIdentityError(
                "unsupported",
                code="unsupported_identity_type",
                details={"provided_id": str(identifier)},
            )

    monkeypatch.setattr(inspection_cli, "build_inspection_service", _Service)
    code = inspection_cli.main(["passages", str(identifier)])
    payload = json.loads(capsys.readouterr().err)

    assert code == 3
    assert payload["failure_stage"] == "inspection"
    assert payload["code"] == "unsupported_identity_type"
    assert payload["details"]["provided_id"] == str(identifier)
