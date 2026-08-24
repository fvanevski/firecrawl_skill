from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from firecrawl_skill.research_store import inspection_cli, retrieval_admin
from firecrawl_skill.research_store.identity_resolver import (
    CorpusIdentityResolutionError,
)
from firecrawl_skill.research_store.inspection_contract import (
    InspectionNotFoundError,
    PassageBounds,
)
from firecrawl_skill.research_store.inspection_service import (
    InspectionIdentityError,
    InspectionIdentityNotFoundError,
    InspectionNoRetainedPassagesError,
    InspectionService,
)


def test_search_response_inspection_does_not_require_corpus_resolver(
    monkeypatch,
) -> None:
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
        "related_ids": {"snapshot": [str(unrelated_snapshot), str(exact_snapshot)]},
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


def _connection_stub():
    class _Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    return _Connection()


def test_resolver_ambiguity_maps_to_structured_unsupported_identity(
    monkeypatch,
) -> None:
    service = object.__new__(InspectionService)
    identifier = uuid4()
    service.connection_factory = _connection_stub
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


def test_unknown_identity_maps_to_structured_not_found(monkeypatch) -> None:
    service = object.__new__(InspectionService)
    identifier = uuid4()
    service.connection_factory = _connection_stub
    monkeypatch.setattr(
        "firecrawl_skill.research_store.inspection_service.resolve_corpus_identity",
        lambda _connection, _identifier: (_ for _ in ()).throw(
            CorpusIdentityResolutionError(
                "missing", code="not_found", identifier=identifier
            )
        ),
    )
    with pytest.raises(InspectionIdentityNotFoundError) as exc_info:
        service.resolve_identity(identifier)
    assert exc_info.value.code == "not_found"
    assert exc_info.value.details["identifier"] == str(identifier)


def test_known_identity_without_passages_is_typed_no_retained(monkeypatch) -> None:
    service = object.__new__(InspectionService)
    identifier = uuid4()
    identity = {
        "identifier": str(identifier),
        "identity_type": "document",
        "related_ids": {"document": [str(identifier)]},
    }
    monkeypatch.setattr(
        "firecrawl_skill.research_store.inspection_service.passages",
        lambda *_args: (_ for _ in ()).throw(InspectionNotFoundError("no rows")),
    )
    monkeypatch.setattr(service, "resolve_identity", lambda _identifier: identity)
    with pytest.raises(InspectionNoRetainedPassagesError) as exc_info:
        service.passages(identifier, PassageBounds())
    assert exc_info.value.code == "no_retained_passages"


def test_search_response_passage_request_is_structured_unsupported(monkeypatch) -> None:
    service = object.__new__(InspectionService)
    identifier = uuid4()
    monkeypatch.setattr(
        "firecrawl_skill.research_store.inspection_service.passages",
        lambda *_args: (_ for _ in ()).throw(InspectionNotFoundError("no rows")),
    )
    monkeypatch.setattr(
        service,
        "resolve_identity",
        lambda _identifier: (_ for _ in ()).throw(
            InspectionIdentityNotFoundError(
                "not corpus", details={"provided_id": str(identifier)}
            )
        ),
    )
    monkeypatch.setattr(
        "firecrawl_skill.research_store.inspection_service.inspect_asset",
        lambda *_args: {
            "matches": [{"asset_type": "search_response", "id": str(identifier)}]
        },
    )
    with pytest.raises(InspectionIdentityError) as exc_info:
        service.passages(identifier, PassageBounds())
    assert exc_info.value.code == "unsupported_identity_type"
    assert exc_info.value.details["detected_identity_types"] == ["search_response"]


def test_research_db_unknown_identity_is_structured_not_found(monkeypatch) -> None:
    identifier = uuid4()

    class _Uow:
        connection = object()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        retrieval_admin,
        "resolve_corpus_identity",
        lambda *_args: (_ for _ in ()).throw(
            CorpusIdentityResolutionError(
                "missing", code="not_found", identifier=identifier
            )
        ),
    )
    args = SimpleNamespace(
        ids=[str(identifier)],
        max_tokens=100,
        max_passages=1,
        research_run_id=None,
    )
    with pytest.raises(ValueError) as exc_info:
        retrieval_admin.fetch_passages(
            SimpleNamespace(fetch_passages=lambda *_args, **_kwargs: []),
            object(),
            args,
            resolve_run_id=lambda *_args: None,
            uow_factory=lambda _config: _Uow,
        )
    assert json.loads(str(exc_info.value))["code"] == "not_found"


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
