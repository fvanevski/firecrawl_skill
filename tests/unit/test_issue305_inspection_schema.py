from __future__ import annotations

from uuid import uuid4

import pytest

from firecrawl_skill.research_store.inspection_contract import (
    InspectionNotFoundError,
)
from firecrawl_skill.research_store.inspection_service import InspectionService


def test_promotion_subject_inspection_reuses_database_native_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = object.__new__(InspectionService)
    subject_id = uuid4()
    identity = {
        "identifier": str(subject_id),
        "identity_type": "promotion_subject",
        "related_ids": {
            "run": [],
            "promotion_subject": [str(subject_id)],
            "search_candidate": [],
            "extraction_attempt": [],
            "source": [],
            "snapshot": [],
            "document": [],
            "derivation": [],
            "chunk": [],
        },
    }
    monkeypatch.setattr(service, "resolve_identity", lambda _asset_id: identity)

    def _deny_connection_factory():
        raise InspectionNotFoundError(
            "asset inspection database is unavailable in unit scope"
        )

    monkeypatch.setattr(
        service, "connection_factory", _deny_connection_factory, raising=False
    )

    payload = service.inspect_asset(subject_id)

    assert payload["schema_version"] == "database-native-inspection-v1"
    assert payload["kind"] == "asset_inspection"
    assert payload["asset_id"] == str(subject_id)
    assert payload["matches"][0]["asset_type"] == "promotion_subject"
    assert payload["identity"] == identity
