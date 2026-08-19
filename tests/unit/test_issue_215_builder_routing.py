"""Public builder routing contract for issue #215."""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from research_store.config import StoreConfig
from research_store.fsearch_policy_service import PolicyFSearchService
from research_store.fsearch_service import build_fsearch_service


def test_public_builder_returns_policy_complete_service(tmp_path, monkeypatch):
    config = replace(
        StoreConfig.from_env(),
        database_url="postgresql://example.invalid/research",
        blob_root=tmp_path / "blobs",
    )
    monkeypatch.delenv("DATABASE_URL", raising=False)
    service = build_fsearch_service(config)
    assert isinstance(service, PolicyFSearchService)
