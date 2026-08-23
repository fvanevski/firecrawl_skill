"""Database-native replay, history, and bounded corpus inspection."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any
from uuid import UUID

from .blob import ContentAddressedBlobStore
from .config import StoreConfig
from .identity_resolver import resolve_corpus_identity
from .inspection_contract import InspectionNotFoundError, PageRequest, PassageBounds
from .inspection_corpus import inspect_asset, lexical_search, passages, pattern_search
from .inspection_history import (
    list_extraction_attempts,
    list_invocations,
    list_runs,
    list_search_responses,
    replay_search,
    retry_candidates,
    scrape_candidates,
)
from .inspection_operations import list_operations
from .postgres import connect


def _missing_direct_scrape_dependency() -> Any:
    raise RuntimeError(
        "authoritative direct-scrape dependency was not injected; "
        "construct InspectionService through research_store.composition"
    )


class InspectionService:
    """Bounded reads over PostgreSQL plus verified immutable blob replay."""

    def __init__(
        self,
        config: StoreConfig,
        *,
        connection_factory: Callable[[], Any] | None = None,
        blob_store: ContentAddressedBlobStore | None = None,
        direct_scrape_factory: Callable[[], Any] | None = None,
    ) -> None:
        config.require_database()
        self.config = config
        self.connection_factory = connection_factory or (
            lambda: connect(config.database_url)
        )
        self.blob_store = blob_store or ContentAddressedBlobStore(config.blob_root)
        self.direct_scrape_factory = (
            direct_scrape_factory or _missing_direct_scrape_dependency
        )

    def list_runs(self, page: PageRequest | None = None) -> dict[str, Any]:
        return list_runs(self, page or PageRequest())

    def list_invocations(
        self, run: UUID | str, page: PageRequest | None = None
    ) -> dict[str, Any]:
        return list_invocations(self, run, page or PageRequest())

    def list_operations(
        self, run: UUID | str, page: PageRequest | None = None
    ) -> dict[str, Any]:
        return list_operations(self, run, page or PageRequest())

    def list_search_responses(
        self, run: UUID | str, page: PageRequest | None = None
    ) -> dict[str, Any]:
        return list_search_responses(self, run, page or PageRequest())

    def replay_search(
        self, search_response_id: UUID | str, *, max_bytes: int = 1_048_576
    ) -> dict[str, Any]:
        return replay_search(self, search_response_id, max_bytes=max_bytes)

    def scrape_candidates(
        self,
        candidate_ids: Sequence[UUID | str],
        *,
        format: str = "markdown",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        return scrape_candidates(
            self,
            candidate_ids,
            format=format,
            idempotency_key=idempotency_key,
        )

    def retry_candidates(
        self,
        prior_invocation_id: UUID | str,
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return retry_candidates(
            self,
            prior_invocation_id,
            idempotency_key=idempotency_key,
        )

    def list_extraction_attempts(
        self,
        *,
        run: UUID | str | None = None,
        candidate_id: UUID | str | None = None,
        page: PageRequest | None = None,
    ) -> dict[str, Any]:
        return list_extraction_attempts(
            self, run=run, candidate_id=candidate_id, page=page or PageRequest()
        )

    def resolve_identity(self, asset_id: UUID | str) -> dict[str, Any]:
        with self.connection_factory() as connection:
            return resolve_corpus_identity(connection, asset_id).to_dict()

    def inspect_asset(self, asset_id: UUID | str) -> dict[str, Any]:
        identity = self.resolve_identity(asset_id)
        if identity["identity_type"] == "promotion_subject":
            return {
                "schema_version": "inspection-v1",
                "kind": "asset_inspection",
                "asset_id": str(UUID(str(asset_id))),
                "matches": [
                    {
                        "asset_type": "promotion_subject",
                        "id": str(UUID(str(asset_id))),
                        "identity": identity,
                    }
                ],
                "match_count": 1,
                "identity": identity,
            }
        result = inspect_asset(self, asset_id)
        return {**result, "identity": identity}

    def passages(
        self, asset_id: UUID | str, bounds: PassageBounds | None = None
    ) -> dict[str, Any]:
        identity = self.resolve_identity(asset_id)
        resolved_id = UUID(str(asset_id))
        if identity["identity_type"] == "promotion_subject":
            related = identity["related_ids"]
            targets = related.get("snapshot") or related.get("search_candidate") or ()
            if not targets:
                raise InspectionNotFoundError(
                    "promotion subject has no retained PostgreSQL corpus identity: "
                    f"{resolved_id}"
                )
            resolved_id = UUID(str(targets[0]))
        result = passages(self, resolved_id, bounds or PassageBounds())
        return {**result, "identity": identity}

    def lexical_search(
        self,
        query: str,
        *,
        run: UUID | str | None = None,
        bounds: PassageBounds | None = None,
    ) -> dict[str, Any]:
        return lexical_search(self, query, run=run, bounds=bounds or PassageBounds())

    def pattern_search(
        self,
        pattern: str,
        *,
        mode: str = "literal",
        run: UUID | str | None = None,
        bounds: PassageBounds | None = None,
    ) -> dict[str, Any]:
        return pattern_search(
            self,
            pattern,
            mode=mode,
            run=run,
            bounds=bounds or PassageBounds(),
        )

    def _resolve_run(self, value: UUID | str) -> tuple[UUID, str | None]:
        text = str(value)
        try:
            identifier = UUID(text)
        except ValueError:
            identifier = None
        with self.connection_factory() as connection, connection.cursor() as cursor:
            if identifier is not None:
                cursor.execute(
                    "SELECT id,external_run_id FROM research_runs WHERE id=%s",
                    (identifier,),
                )
            else:
                cursor.execute(
                    "SELECT id,external_run_id FROM research_runs "
                    "WHERE external_run_id=%s",
                    (text,),
                )
            row = cursor.fetchone()
        if row is None:
            raise InspectionNotFoundError(f"research run not found: {text}")
        return UUID(str(row[0])), row[1]


__all__ = ["InspectionService"]
