"""Database-native replay, history, and bounded corpus inspection."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any
from uuid import UUID

from .blob import ContentAddressedBlobStore
from .config import StoreConfig
from .identity_resolver import CorpusIdentityResolutionError, resolve_corpus_identity
from .inspection_contract import (
    _SCHEMA_VERSION,
    InspectionError,
    InspectionNotFoundError,
    PageRequest,
    PassageBounds,
)
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


class InspectionIdentityError(InspectionError):
    """A UUID is known but unsupported or ambiguous for the requested surface."""

    def __init__(self, message: str, *, code: str, details: dict[str, Any]) -> None:
        self.code = code
        self.details = details
        super().__init__(message)


class InspectionIdentityNotFoundError(InspectionNotFoundError):
    """A UUID is absent from the authoritative identity domains."""

    code = "not_found"

    def __init__(self, message: str, *, details: dict[str, Any]) -> None:
        self.details = details
        super().__init__(message)


class InspectionNoRetainedPassagesError(InspectionIdentityError):
    """A known identity has no retained PostgreSQL passage target."""

    def __init__(self, identifier: UUID, identity: dict[str, Any]) -> None:
        super().__init__(
            f"no retained passages resolve from authoritative identity: {identifier}",
            code="no_retained_passages",
            details={"provided_id": str(identifier), "identity": identity},
        )


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

    def _identity_error(self, exc: CorpusIdentityResolutionError) -> InspectionError:
        details = exc.to_dict()
        if exc.code == "not_found":
            return InspectionIdentityNotFoundError(
                f"authoritative identity not found: {exc.identifier}",
                details=details,
            )
        return InspectionIdentityError(
            str(exc),
            code="unsupported_identity_type",
            details=details,
        )

    def resolve_identity(self, asset_id: UUID | str) -> dict[str, Any]:
        with self.connection_factory() as connection:
            try:
                return resolve_corpus_identity(connection, asset_id).to_dict()
            except CorpusIdentityResolutionError as exc:
                raise self._identity_error(exc) from exc

    def _promotion_subject_snapshot(self, subject_id: UUID) -> UUID:
        with self.connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT snapshot_id FROM run_asset_promotion_subjects WHERE id=%s",
                (subject_id,),
            )
            row = cursor.fetchone()
        if row is None:
            raise InspectionIdentityNotFoundError(
                f"promotion subject not found: {subject_id}",
                details={"provided_id": str(subject_id)},
            )
        if row[0] is None:
            raise InspectionIdentityError(
                f"promotion subject has no retained snapshot: {subject_id}",
                code="no_retained_passages",
                details={"provided_id": str(subject_id)},
            )
        return UUID(str(row[0]))

    def inspect_asset(self, asset_id: UUID | str) -> dict[str, Any]:
        identifier = UUID(str(asset_id))
        try:
            result = inspect_asset(self, identifier)
        except InspectionNotFoundError:
            identity = self.resolve_identity(identifier)
            if identity["identity_type"] != "promotion_subject":
                raise InspectionIdentityError(
                    f"identity type is not inspectable through this fallback: {identity['identity_type']}",
                    code="unsupported_identity_type",
                    details={"provided_id": str(identifier), "identity": identity},
                )
            return {
                "schema_version": _SCHEMA_VERSION,
                "kind": "asset_inspection",
                "asset_id": str(identifier),
                "matches": [
                    {
                        "asset_type": "promotion_subject",
                        "id": str(identifier),
                        "identity": identity,
                    }
                ],
                "match_count": 1,
                "identity": identity,
            }

        if any(item.get("asset_type") == "search_response" for item in result["matches"]):
            return result
        identity = self.resolve_identity(identifier)
        return {**result, "identity": identity}

    def passages(
        self, asset_id: UUID | str, bounds: PassageBounds | None = None
    ) -> dict[str, Any]:
        bounds = bounds or PassageBounds()
        identifier = UUID(str(asset_id))
        direct_error: Exception | None = None
        try:
            result = passages(self, identifier, bounds)
        except InspectionNotFoundError as exc:
            direct_error = exc
        except ValueError as exc:
            if "invalid pagination cursor" not in str(exc):
                raise
            direct_error = exc
        else:
            return result

        try:
            identity = self.resolve_identity(identifier)
        except InspectionIdentityNotFoundError as resolution_error:
            try:
                inspected = inspect_asset(self, identifier)
            except InspectionNotFoundError:
                raise resolution_error from direct_error
            detected = sorted(
                {str(item.get("asset_type")) for item in inspected.get("matches", ())}
            )
            raise InspectionIdentityError(
                f"identity type does not support retained passages: {','.join(detected)}",
                code="unsupported_identity_type",
                details={
                    "provided_id": str(identifier),
                    "detected_identity_types": detected,
                },
            ) from direct_error

        if identity["identity_type"] != "promotion_subject":
            if isinstance(direct_error, ValueError):
                raise direct_error
            raise InspectionNoRetainedPassagesError(identifier, identity) from direct_error

        snapshot_id = self._promotion_subject_snapshot(identifier)
        try:
            result = passages(self, snapshot_id, bounds)
        except InspectionNotFoundError as exc:
            raise InspectionNoRetainedPassagesError(identifier, identity) from exc
        return {
            **result,
            "asset_id": str(identifier),
            "resolved_asset_id": str(snapshot_id),
            "identity": identity,
        }

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


__all__ = [
    "InspectionIdentityError",
    "InspectionIdentityNotFoundError",
    "InspectionNoRetainedPassagesError",
    "InspectionService",
]
