"""PostgreSQL-authoritative ``fsearch`` workflow and CLI.

The workflow performs a run-bound authoritative preflight before constructing
or invoking Firecrawl, persists the search response and candidates through
``AcquisitionService``, and delegates selected extraction to
``DirectScrapeService`` using stable candidate IDs. It never reads or writes
scratch acquisition state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any
from uuid import UUID, uuid4

from .acquisition_authority import (
    AcquisitionPreflightError,
    AuthoritativeAcquisitionContext,
    require_authoritative_acquisition,
)
from .acquisition_service import (
    AcquisitionAuthorityChangedError,
    AcquisitionIdempotencyConflictError,
    AcquisitionResult,
    AcquisitionService,
)
from .config import StoreConfig
from .direct_scrape_service import (
    DirectScrapeBatchResult,
    DirectScrapeError,
    DirectScrapePersistenceError,
    DirectScrapeRequest,
)
from .domain import SearchAdapterResult, utcnow

try:
    from candidate_ranking import (
        DEFAULT_RANKING_POLICY,
        UrlType,
        assess_freshness,
        classify_url,
        compute_ranking_score,
    )
except ImportError:  # pragma: no cover
    UrlType = None
    compute_ranking_score = None
    classify_url = None
    assess_freshness = None
    DEFAULT_RANKING_POLICY = None

_MAX_DIAGNOSTIC_CHARS = 500
_MAX_SEARCH_RESULTS = 100
_MAX_SCRAPE_COUNT = 20
_MAX_OUTPUT_CANDIDATE_IDS = 100
_MAX_OUTPUT_EXTRACTION_OUTCOMES = 20
_MAX_OUTPUT_CHUNK_IDS_PER_OUTCOME = 25
_MAX_OUTPUT_CORPUS_IDS = 100
_RUN_ID_PATTERN = re.compile(
    r"^fr_(?:[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12})$"
)
_INVOCATION_ID_PATTERN = re.compile(r"^fc_[0-9a-f]{32}$")
_PROFILE_NAMES = (
    "ecommerce",
    "forum",
    "news_article",
    "media_release",
    "academic_debate",
)
_FAILURE_STAGES = frozenset(
    {
        "preflight",
        "search_transport",
        "candidate_parsing",
        "extraction",
        "ingestion",
        "indexing",
    }
)


class FSearchArgumentError(ValueError):
    """A machine-renderable CLI argument failure."""


class _FSearchArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise FSearchArgumentError(message)


class FSearchError(RuntimeError):
    """A public authoritative-fsearch failure with a stable stage label."""

    def __init__(
        self,
        stage: str,
        message: str,
        *,
        result: FSearchResult | None = None,
    ) -> None:
        if stage not in _FAILURE_STAGES:
            raise ValueError(f"unknown fsearch failure stage: {stage}")
        super().__init__(message)
        self.stage = stage
        self.result = result

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "schema_version": "authoritative-fsearch-error-v1",
            "status": "failed",
            "failure_stage": self.stage,
            "error": _bounded_text(str(self)),
        }
        if self.result is not None:
            value["result"] = self.result.to_dict()
        return value


@dataclass(frozen=True)
class FSearchRequest:
    query: str
    research_run_id: str
    limit: int = 20
    scrape_limit: int = 5
    sources: str = "web"
    tbs: str | None = None
    profile: str | None = None
    idempotency_key: str | None = None
    external_invocation_id: str | None = None

    def __post_init__(self) -> None:
        if not self.query.strip():
            raise ValueError("query is required")
        validate_research_run_id(self.research_run_id)
        if not 1 <= self.limit <= _MAX_SEARCH_RESULTS:
            raise ValueError(f"limit must be between 1 and {_MAX_SEARCH_RESULTS}")
        if not 0 <= self.scrape_limit <= _MAX_SCRAPE_COUNT:
            raise ValueError(f"scrape_limit must be between 0 and {_MAX_SCRAPE_COUNT}")
        if not self.sources.strip():
            raise ValueError("sources must be non-empty")
        if self.profile is not None and self.profile not in _PROFILE_NAMES:
            raise ValueError(f"unsupported classification profile: {self.profile}")
        if self.idempotency_key is not None and not self.idempotency_key.strip():
            raise ValueError("idempotency_key must be non-empty when provided")
        if self.external_invocation_id is not None:
            validate_invocation_id(self.external_invocation_id)


@dataclass(frozen=True)
class FSearchExtractionOutcome:
    candidate_id: UUID
    status: str
    failure_stage: str | None = None
    error: str | None = None
    extraction_attempt_id: UUID | None = None
    source_id: UUID | None = None
    snapshot_id: UUID | None = None
    document_id: UUID | None = None
    derivation_id: UUID | None = None
    chunk_ids: tuple[UUID, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        chunks, truncated = _bounded_strings(
            self.chunk_ids, _MAX_OUTPUT_CHUNK_IDS_PER_OUTCOME
        )
        return {
            "candidate_id": str(self.candidate_id),
            "status": self.status,
            "failure_stage": self.failure_stage,
            "error": _bounded_text(self.error),
            "extraction_attempt_id": _uuid_text(self.extraction_attempt_id),
            "source_id": _uuid_text(self.source_id),
            "snapshot_id": _uuid_text(self.snapshot_id),
            "document_id": _uuid_text(self.document_id),
            "derivation_id": _uuid_text(self.derivation_id),
            "chunk_ids": chunks,
            "chunk_id_count": len(self.chunk_ids),
            "chunk_ids_truncated": truncated,
        }


@dataclass(frozen=True)
class FSearchResult:
    status: str
    run_id: UUID
    research_run_id: str
    invocation_id: UUID
    external_invocation_id: str
    search_response_id: UUID | None
    candidate_ids: tuple[UUID, ...]
    search_replayed: bool = False
    extraction_invocation_id: UUID | None = None
    extraction_status: str | None = None
    extraction_replayed: bool = False
    extraction_outcomes: tuple[FSearchExtractionOutcome, ...] = ()
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        candidate_ids, candidate_truncated = _bounded_strings(
            self.candidate_ids, _MAX_OUTPUT_CANDIDATE_IDS
        )
        selected_outcomes = self.extraction_outcomes[:_MAX_OUTPUT_EXTRACTION_OUTCOMES]
        outcomes = [item.to_dict() for item in selected_outcomes]
        corpus_values = {
            "source_ids": _unique_ids(
                item.source_id for item in self.extraction_outcomes
            ),
            "snapshot_ids": _unique_ids(
                item.snapshot_id for item in self.extraction_outcomes
            ),
            "document_ids": _unique_ids(
                item.document_id for item in self.extraction_outcomes
            ),
            "derivation_ids": _unique_ids(
                item.derivation_id for item in self.extraction_outcomes
            ),
            "chunk_ids": _unique_ids(
                chunk for item in self.extraction_outcomes for chunk in item.chunk_ids
            ),
        }
        corpus_ids: dict[str, Any] = {}
        for name, values in corpus_values.items():
            bounded, truncated = _bounded_strings(values, _MAX_OUTPUT_CORPUS_IDS)
            corpus_ids[name] = bounded
            corpus_ids[
                f"{name[:-4]}_count" if name.endswith("_ids") else f"{name}_count"
            ] = len(values)
            corpus_ids[f"{name}_truncated"] = truncated
        return {
            "schema_version": "authoritative-fsearch-v1",
            "status": self.status,
            "run_id": str(self.run_id),
            "research_run_id": self.research_run_id,
            "invocation_id": str(self.invocation_id),
            "external_invocation_id": self.external_invocation_id,
            "search_response_id": _uuid_text(self.search_response_id),
            "search_replayed": self.search_replayed,
            "candidate_ids": candidate_ids,
            "candidate_count": len(self.candidate_ids),
            "candidate_ids_truncated": candidate_truncated,
            "extraction_invocation_id": _uuid_text(self.extraction_invocation_id),
            "extraction_status": self.extraction_status,
            "extraction_replayed": self.extraction_replayed,
            "extraction_outcomes": outcomes,
            "extraction_outcome_count": len(self.extraction_outcomes),
            "extraction_outcomes_truncated": (
                len(self.extraction_outcomes) > len(selected_outcomes)
            ),
            "corpus_ids": corpus_ids,
            "error": _bounded_text(self.error),
        }


class MetadataOnlyFirecrawlSearchAdapter:
    """Run Firecrawl search without implicit scrape or filesystem output."""

    def __init__(
        self,
        *,
        executable: str = "firecrawl",
        timeout_seconds: int = 60,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    ) -> None:
        self.executable = executable
        self.timeout_seconds = timeout_seconds
        self.runner = runner

    def search(
        self,
        query_text: str,
        *,
        backend: str = "firecrawl",
        limit: int = 20,
        sources: str = "web",
        tbs: str | None = None,
        retries: int = 2,
        **_: Any,
    ) -> SearchAdapterResult:
        if backend != "firecrawl":
            raise ValueError(
                "authoritative fsearch supports only the firecrawl backend"
            )
        if not query_text.strip():
            raise ValueError("query_text must be non-empty")

        command = [
            self.executable,
            "search",
            query_text,
            "--limit",
            str(limit),
            "--sources",
            sources,
            "--ignore-invalid-urls",
            "--json",
        ]
        if tbs:
            command.extend(["--tbs", tbs])

        requested_at = utcnow()
        last_code = 0
        last_stdout = b""
        last_stderr = b""
        responded_at = requested_at
        attempts = 0
        for attempt in range(retries + 1):
            attempts = attempt + 1
            try:
                process = self.runner(
                    command,
                    capture_output=True,
                    timeout=self.timeout_seconds,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                last_code = 124
                last_stdout = _as_bytes(exc.stdout)
                last_stderr = str(exc).encode("utf-8", errors="replace")
            except OSError as exc:
                last_code = 127
                last_stdout = b""
                last_stderr = str(exc).encode("utf-8", errors="replace")
            else:
                last_code = int(process.returncode)
                last_stdout = _as_bytes(process.stdout)
                last_stderr = _as_bytes(process.stderr)
            responded_at = utcnow()
            if last_code == 0 and last_stdout:
                return SearchAdapterResult(
                    raw_payload=last_stdout,
                    http_status=200,
                    provider_request_id=None,
                    transport_error=None,
                    transport_metadata={
                        "adapter": type(self).__name__,
                        "attempts": attempts,
                        "command": command,
                        "exit_code": last_code,
                        "implicit_scrape": False,
                    },
                    requested_at=requested_at,
                    responded_at=responded_at,
                )
            diagnostic = last_stderr.decode("utf-8", errors="replace")
            transient = any(
                marker in diagnostic
                for marker in ("EAI_AGAIN", "ENOTFOUND", "ECONNRESET", "ETIMEDOUT")
            )
            if not transient or attempt >= retries:
                break

        diagnostic = last_stderr.decode("utf-8", errors="replace").strip()
        transport_error = _classify_search_transport_error(
            last_code, diagnostic, bool(last_stdout)
        )
        payload = (
            last_stdout
            if last_code == 0 and last_stdout
            else json.dumps({"success": False, "error": transport_error}).encode(
                "utf-8"
            )
        )
        return SearchAdapterResult(
            raw_payload=payload,
            http_status=500,
            provider_request_id=None,
            transport_error=transport_error,
            transport_metadata={
                "adapter": type(self).__name__,
                "attempts": attempts,
                "command": command,
                "exit_code": last_code,
                "stderr": _bounded_text(diagnostic),
                "implicit_scrape": False,
            },
            requested_at=requested_at,
            responded_at=responded_at,
        )


class FSearchService:
    """Coordinate authoritative search persistence and selected extraction."""

    def __init__(
        self,
        config: StoreConfig,
        run_service: Any,
        invocation_service: Any,
        *,
        acquisition_factory: Callable[[], AcquisitionService],
        direct_scrape_factory: Callable[[], Any],
        preflight: Callable[..., AuthoritativeAcquisitionContext] = (
            require_authoritative_acquisition
        ),
        classify_target: Callable[[str, str, str], tuple[str, bool]] | None = None,
        profiles: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        self.config = config
        self.run_service = run_service
        self.invocation_service = invocation_service
        self.acquisition_factory = acquisition_factory
        self.direct_scrape_factory = direct_scrape_factory
        self.preflight = preflight
        if classify_target is None or profiles is None:
            from classifier import PROFILES
            from classifier import classify_target as default_classifier

            classify_target = classify_target or default_classifier
            profiles = profiles or PROFILES
        self.classify_target = classify_target
        self.profiles = profiles

    def execute(self, request: FSearchRequest) -> FSearchResult:
        run_status = self._resolve_run(request.research_run_id)
        try:
            context = self.preflight(run_id=run_status.id, config=self.config)
        except AcquisitionPreflightError as exc:
            raise FSearchError("preflight", str(exc)) from exc
        except Exception as exc:
            raise FSearchError("preflight", str(exc)) from exc

        external_invocation_id = request.external_invocation_id or new_invocation_id()
        search_key = request.idempotency_key or _default_search_key(
            run_status.id, request, external_invocation_id
        )
        invocation = self.invocation_service.begin(
            run_status.id,
            external_invocation_id,
            "fsearch",
            _invocation_input(request, search_key),
            idempotency_key=f"fsearch-invocation:{external_invocation_id}",
        )
        invocation_id = UUID(str(invocation.id))
        base_result = FSearchResult(
            status="running",
            run_id=run_status.id,
            research_run_id=request.research_run_id,
            invocation_id=invocation_id,
            external_invocation_id=external_invocation_id,
            search_response_id=None,
            candidate_ids=(),
        )

        try:
            acquisition = self.acquisition_factory().execute_search(
                run_status.id,
                request.query,
                idempotency_key=search_key,
                limit=request.limit,
                sources=request.sources,
                tbs=request.tbs,
                authority_context=context,
                replay_existing=True,
                metadata={
                    "invocation_id": str(invocation_id),
                    "classification_profile": request.profile,
                    "selected_scrape_limit": request.scrape_limit,
                    "implicit_scrape": False,
                },
            )
            result = self._after_search(request, base_result, acquisition, search_key)
        except FSearchError as exc:
            self._fail_invocation(
                run_status.id,
                invocation_id,
                exc.stage,
                str(exc),
                exc.result or base_result,
            )
            raise
        except Exception as exc:
            stage = _exception_stage(exc)
            failure = FSearchError(stage, str(exc), result=base_result)
            self._fail_invocation(
                run_status.id,
                invocation_id,
                stage,
                str(exc),
                base_result,
            )
            raise failure from exc

        terminal = "succeeded" if result.status in {"complete", "empty"} else "failed"
        self.invocation_service.complete(
            run_status.id,
            invocation_id,
            terminal,
            output=result.to_dict(),
            error=result.error,
        )
        if terminal != "succeeded":
            stage = _result_failure_stage(result)
            raise FSearchError(
                stage,
                result.error or "one or more selected extractions failed",
                result=result,
            )
        return result

    def _resolve_run(self, research_run_id: str) -> Any:
        validate_research_run_id(research_run_id)
        try:
            return self.run_service.status(external_id=research_run_id)
        except KeyError as exc:
            raise FSearchError(
                "preflight", f"research run does not exist: {research_run_id}"
            ) from exc
        except Exception as exc:
            raise FSearchError(
                "preflight", f"research run lookup failed: {exc}"
            ) from exc

    def _after_search(
        self,
        request: FSearchRequest,
        base_result: FSearchResult,
        acquisition: AcquisitionResult,
        search_key: str,
    ) -> FSearchResult:
        candidate_ids = tuple(
            _candidate_uuid(candidate)
            for candidate in _ordered_candidates(acquisition.candidates)
        )
        searched = FSearchResult(
            **{
                **asdict(base_result),
                "search_response_id": UUID(str(acquisition.search_response_id)),
                "candidate_ids": candidate_ids,
                "search_replayed": acquisition.replayed,
            }
        )
        if not acquisition.postgres_committed:
            raise FSearchError(
                "ingestion",
                "search response was not committed to PostgreSQL",
                result=searched,
            )
        if acquisition.status == "provider_error":
            error = acquisition.search_response.get("error_message") or (
                "Firecrawl search transport failed"
            )
            raise FSearchError("search_transport", str(error), result=searched)
        if acquisition.status == "parse_error":
            error = acquisition.search_response.get("error_message") or (
                "Firecrawl search response could not be parsed"
            )
            raise FSearchError("candidate_parsing", str(error), result=searched)
        if acquisition.status not in {"succeeded", "empty"}:
            error = acquisition.search_response.get("error_message") or (
                f"unexpected search response status: {acquisition.status}"
            )
            raise FSearchError("candidate_parsing", str(error), result=searched)
        if acquisition.status == "empty" or request.scrape_limit == 0:
            return FSearchResult(
                **{
                    **asdict(searched),
                    "status": "empty" if acquisition.status == "empty" else "complete",
                }
            )

        ranked_candidates = self._rank_candidates(acquisition.candidates)
        selected = ranked_candidates[: request.scrape_limit]
        requests = tuple(
            self._scrape_request(candidate, request.profile) for candidate in selected
        )
        if not requests:
            return FSearchResult(**{**asdict(searched), "status": "complete"})

        try:
            extraction: DirectScrapeBatchResult = self.direct_scrape_factory().execute(
                searched.run_id,
                requests,
                idempotency_key=_extraction_key(search_key, request, requests),
                parent_invocation_id=searched.invocation_id,
            )
        except DirectScrapePersistenceError as exc:
            raise FSearchError(exc.stage, str(exc), result=searched) from exc
        except DirectScrapeError as exc:
            raise FSearchError("extraction", str(exc), result=searched) from exc

        outcomes = tuple(_outcome_from_item(item) for item in extraction.items)
        status = "complete" if extraction.status == "complete" else extraction.status
        error = None
        if status != "complete":
            failed = [item for item in outcomes if item.status != "succeeded"]
            error = (
                failed[0].error
                if failed and failed[0].error
                else "one or more selected extractions failed"
            )
        return FSearchResult(
            **{
                **asdict(searched),
                "status": status,
                "extraction_invocation_id": UUID(str(extraction.invocation_id)),
                "extraction_status": extraction.status,
                "extraction_replayed": extraction.replayed,
                "extraction_outcomes": outcomes,
                "error": error,
            }
        )

    def _rank_candidates(
        self, candidates: Sequence[Mapping[str, Any]]
    ) -> list[Mapping[str, Any]]:
        """Rank candidates by relevance score with URL-type penalties.

        Returns candidates sorted by computed ranking score descending.
        """
        if compute_ranking_score is None or UrlType is None:
            return _ordered_candidates(candidates)

        scored: list[tuple[float, int, Mapping[str, Any]]] = []
        for idx, candidate in enumerate(candidates):
            url = str(
                candidate.get("original_url")
                or candidate.get("canonical_url")
                or candidate.get("url")
                or ""
            )
            title = str(candidate.get("title") or "")
            snippet = str(
                candidate.get("snippet") or candidate.get("description") or ""
            )
            try:
                url_type = classify_url(url, title, snippet)
            except Exception:  # noqa: BLE001
                url_type = UrlType.ARTICLE
            try:
                base_score_raw = candidate.get("rank")
                base_score = (
                    float(base_score_raw) if base_score_raw is not None else 0.5
                )
                base_score = max(0.0, min(1.0, base_score))
            except (TypeError, ValueError):
                base_score = 0.5
            score = compute_ranking_score(
                base_score=base_score,
                url_type=url_type,
                freshness_status=assess_freshness(
                    candidate.get("published_at"), utcnow()
                )[0]
                if candidate.get("published_at")
                else assess_freshness(None, utcnow())[0],
                is_duplicate=bool(candidate.get("duplicate", False)),
                expected_char_count=candidate.get("expected_char_count"),
                policy=DEFAULT_RANKING_POLICY,
            )
            try:
                rank_val = float(candidate.get("rank") or 0)
            except (TypeError, ValueError):
                rank_val = _MAX_SEARCH_RESULTS
            scored.append((score.total, rank_val, candidate))

        scored.sort(key=lambda x: (x[0], -x[1]), reverse=True)
        return [item[2] for item in scored]

    def _scrape_request(
        self, candidate: Mapping[str, Any], profile: str | None
    ) -> DirectScrapeRequest:
        candidate_id = _candidate_uuid(candidate)
        if profile is None:
            return DirectScrapeRequest(candidate_id=candidate_id)
        url = str(
            candidate.get("original_url")
            or candidate.get("canonical_url")
            or candidate.get("url")
            or ""
        )
        title = str(candidate.get("title") or "")
        snippet = str(candidate.get("snippet") or candidate.get("description") or "")
        category, matched = self.classify_target(url, title, snippet)
        if category == profile and matched:
            schema = self.profiles[profile]["target_schema"]
            return DirectScrapeRequest(
                candidate_id=candidate_id,
                format="json",
                schema=schema,
            )
        return DirectScrapeRequest(candidate_id=candidate_id)

    def _fail_invocation(
        self,
        run_id: UUID,
        invocation_id: UUID,
        stage: str,
        message: str,
        result: FSearchResult,
    ) -> None:
        try:
            self.invocation_service.complete(
                run_id,
                invocation_id,
                "failed",
                output={**result.to_dict(), "failure_stage": stage},
                error=_bounded_text(message),
            )
        except Exception:  # noqa: BLE001, S110
            pass


def build_fsearch_service(
    config: StoreConfig | None = None,
    *,
    search_adapter_factory: Callable[[], Any] = MetadataOnlyFirecrawlSearchAdapter,
) -> FSearchService:
    """Build the policy-complete authoritative fsearch service.

    The implementation is imported lazily to avoid a module-initialization cycle:
    ``fsearch_policy_service`` subclasses the compatibility service and imports
    the request/result/CLI helpers defined above.
    """
    from .fsearch_policy_service import build_policy_fsearch_service

    return build_policy_fsearch_service(
        config,
        search_adapter_factory=search_adapter_factory,
    )


def validate_research_run_id(value: str) -> str:
    if not _RUN_ID_PATTERN.fullmatch(value or ""):
        raise ValueError(
            "research run ID must match fr_<32 lowercase hexadecimal characters> "
            "or fr_<canonical lowercase UUID>"
        )
    return value


def validate_invocation_id(value: str) -> str:
    if not _INVOCATION_ID_PATTERN.fullmatch(value or ""):
        raise ValueError(
            "invocation ID must match fc_<32 lowercase hexadecimal characters>"
        )
    return value


def new_invocation_id() -> str:
    return f"fc_{uuid4().hex}"


def build_parser() -> argparse.ArgumentParser:
    parser = _FSearchArgumentParser(
        prog="fsearch",
        description=(
            "Run a PostgreSQL-authoritative Firecrawl search and optionally "
            "extract a bounded set of persisted candidates."
        ),
    )
    parser.add_argument("query")
    parser.add_argument(
        "--research-run-id",
        default=os.environ.get("FIRECRAWL_RESEARCH_RUN_ID"),
        help="required external research run ID (fr_<uuid>)",
    )
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--scrape-limit", type=int, default=5)
    parser.add_argument("--sources", default="web")
    parser.add_argument("--tbs")
    parser.add_argument("--profile", choices=_PROFILE_NAMES)
    parser.add_argument("--idempotency-key")
    parser.add_argument(
        "--invocation-id",
        default=os.environ.get("FIRECRAWL_INVOCATION_ID"),
    )
    parser.add_argument("--json", action="store_true", dest="json_output")
    parser.add_argument("--dir", help=argparse.SUPPRESS)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    service_factory: Callable[[], FSearchService] = build_fsearch_service,
) -> int:
    raw_argv = list(argv if argv is not None else sys.argv[1:])
    json_requested = "--json" in raw_argv
    parser = build_parser()
    try:
        args = parser.parse_args(raw_argv)
        if args.dir is not None:
            raise FSearchArgumentError(
                "--dir was removed; fsearch no longer writes acquisition artifacts. "
                "Use database-native export tooling for explicit exports."
            )
        if not args.research_run_id:
            raise FSearchArgumentError(
                "--research-run-id or FIRECRAWL_RESEARCH_RUN_ID is required"
            )
        request = FSearchRequest(
            query=args.query,
            research_run_id=args.research_run_id,
            limit=args.limit,
            scrape_limit=args.scrape_limit,
            sources=args.sources,
            tbs=args.tbs,
            profile=args.profile,
            idempotency_key=args.idempotency_key,
            external_invocation_id=args.invocation_id,
        )
        try:
            service = service_factory()
        except RuntimeError as exc:
            raise AcquisitionPreflightError(str(exc)) from exc
        result = service.execute(request)
    except (FSearchArgumentError, ValueError, AcquisitionPreflightError) as exc:
        return _emit_error("preflight", str(exc), json_requested)
    except FSearchError as exc:
        return _emit_fsearch_error(exc, json_requested)
    except Exception as exc:  # noqa: BLE001
        return _emit_error(_exception_stage(exc), str(exc), json_requested)

    _emit_result(result, json_requested)
    return 0


def _emit_result(result: FSearchResult, json_output: bool) -> None:
    payload = result.to_dict()
    if json_output:
        print(json.dumps(payload, sort_keys=True))
        return
    print(f"status: {result.status}")
    print(f"run_id: {result.run_id}")
    print(f"research_run_id: {result.research_run_id}")
    print(f"invocation_id: {result.invocation_id}")
    print(f"external_invocation_id: {result.external_invocation_id}")
    print(f"search_response_id: {result.search_response_id}")
    print(f"search_replayed: {result.search_replayed}")
    print(f"candidate_ids: {_compact_ids(payload['candidate_ids'])}")
    print(f"candidate_count: {payload['candidate_count']}")
    if result.extraction_status is not None:
        print(f"extraction_invocation_id: {result.extraction_invocation_id}")
        print(f"extraction_status: {result.extraction_status}")
        print(f"extraction_replayed: {result.extraction_replayed}")
        print(
            "extraction_outcomes: "
            + json.dumps(payload["extraction_outcomes"], sort_keys=True)
        )
    print("corpus_ids: " + json.dumps(payload["corpus_ids"], sort_keys=True))


def _emit_fsearch_error(error: FSearchError, json_output: bool) -> int:
    if json_output:
        print(json.dumps(error.to_dict(), sort_keys=True))
    else:
        print(f"ERROR [{error.stage}]: {_bounded_text(str(error))}", file=sys.stderr)
        if error.result is not None:
            print(
                "authoritative_result: "
                + json.dumps(error.result.to_dict(), sort_keys=True),
                file=sys.stderr,
            )
    return _exit_code(error.stage)


def _emit_error(stage: str, message: str, json_output: bool) -> int:
    return _emit_fsearch_error(FSearchError(stage, message), json_output)


def _exit_code(stage: str) -> int:
    return {
        "preflight": 2,
        "search_transport": 3,
        "candidate_parsing": 4,
        "extraction": 5,
        "ingestion": 6,
        "indexing": 7,
    }[stage]


def _ordered_candidates(
    candidates: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    def key(item: Mapping[str, Any]) -> tuple[int, str]:
        rank = item.get("rank")
        try:
            normalized_rank = int(rank)
        except (TypeError, ValueError):
            normalized_rank = _MAX_SEARCH_RESULTS + 1
        return normalized_rank, str(item.get("id") or "")

    return sorted(candidates, key=key)


def _candidate_uuid(candidate: Mapping[str, Any]) -> UUID:
    value = candidate.get("candidate_id") or candidate.get("id")
    if value is None:
        raise ValueError("persisted search candidate has no stable candidate ID")
    return UUID(str(value))


def _bounded_strings(values: Any, limit: int) -> tuple[list[str], bool]:
    normalized = [str(value) for value in values if value is not None]
    return normalized[:limit], len(normalized) > limit


def _compact_ids(values: Sequence[str]) -> str:
    return ",".join(values) if values else "none"


def _outcome_from_item(item: Any) -> FSearchExtractionOutcome:
    error = _bounded_text(getattr(item, "error", None))
    status = str(getattr(item, "status", "failed"))
    failure_stage = None
    if status != "succeeded":
        failure_class = str(getattr(item, "failure_class", "") or "")
        if failure_class in {"parser", "schema_validation", "malformed"}:
            failure_stage = "ingestion"
        elif failure_class == "indexing":
            failure_stage = "indexing"
        else:
            failure_stage = "extraction"
    return FSearchExtractionOutcome(
        candidate_id=UUID(str(item.candidate_id)),
        status=status,
        failure_stage=failure_stage,
        error=error,
        extraction_attempt_id=_optional_uuid(
            getattr(item, "extraction_attempt_id", None)
        ),
        source_id=_optional_uuid(getattr(item, "source_id", None)),
        snapshot_id=_optional_uuid(getattr(item, "snapshot_id", None)),
        document_id=_optional_uuid(getattr(item, "document_id", None)),
        derivation_id=_optional_uuid(getattr(item, "derivation_id", None)),
        chunk_ids=tuple(UUID(str(value)) for value in getattr(item, "chunk_ids", ())),
    )


def _result_failure_stage(result: FSearchResult) -> str:
    stages = {
        item.failure_stage
        for item in result.extraction_outcomes
        if item.failure_stage is not None
    }
    if "indexing" in stages:
        return "indexing"
    if "ingestion" in stages:
        return "ingestion"
    return "extraction"


def _exception_stage(exc: Exception) -> str:
    if isinstance(exc, AcquisitionPreflightError):
        return "preflight"
    if isinstance(
        exc,
        (AcquisitionAuthorityChangedError, AcquisitionIdempotencyConflictError),
    ):
        return "ingestion"
    if isinstance(exc, DirectScrapePersistenceError):
        return exc.stage
    if isinstance(exc, DirectScrapeError):
        return "extraction"
    return "ingestion"


def _classify_search_transport_error(
    returncode: int, diagnostic: str, has_payload: bool
) -> str:
    for marker in ("EAI_AGAIN", "ENOTFOUND", "ECONNRESET", "ETIMEDOUT"):
        if marker in diagnostic:
            return f"Network transport error: {marker}"
    if returncode == 0 and not has_payload:
        return "Firecrawl search returned an empty response"
    if diagnostic:
        bounded = _bounded_text(diagnostic)
        return f"Firecrawl search failed (exit {returncode}): {bounded}"
    return f"Firecrawl search failed with exit code {returncode}"


def _invocation_input(request: FSearchRequest, search_key: str) -> dict[str, Any]:
    return {
        "schema_version": "authoritative-fsearch-v1",
        "query": request.query,
        "limit": request.limit,
        "scrape_limit": request.scrape_limit,
        "sources": request.sources,
        "tbs": request.tbs,
        "profile": request.profile,
        "search_idempotency_key": search_key,
    }


def _default_search_key(
    run_id: UUID,
    request: FSearchRequest,
    external_invocation_id: str,
) -> str:
    payload = json.dumps(
        {
            "run_id": str(run_id),
            "external_invocation_id": external_invocation_id,
            "query": request.query,
            "limit": request.limit,
            "sources": request.sources,
            "tbs": request.tbs,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"fsearch:{hashlib.sha256(payload.encode()).hexdigest()}"


def _extraction_key(
    search_key: str,
    request: FSearchRequest,
    requests: Sequence[DirectScrapeRequest],
) -> str:
    payload = json.dumps(
        {
            "search_key": search_key,
            "profile": request.profile,
            "scrape_limit": request.scrape_limit,
            "requests": [
                {
                    "candidate_id": str(item.candidate_id),
                    "format": item.format,
                    "schema": item.schema,
                }
                for item in requests
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"fsearch-extraction:{hashlib.sha256(payload.encode()).hexdigest()}"


def _as_bytes(value: bytes | str | None) -> bytes:
    if value is None:
        return b""
    return value.encode("utf-8") if isinstance(value, str) else value


def _bounded_text(value: str | None) -> str | None:
    if value is None:
        return None
    return str(value)[:_MAX_DIAGNOSTIC_CHARS]


def _optional_uuid(value: Any) -> UUID | None:
    return UUID(str(value)) if value is not None else None


def _uuid_text(value: UUID | None) -> str | None:
    return str(value) if value is not None else None


def _unique_ids(values: Any) -> list[str]:
    return list(dict.fromkeys(str(value) for value in values if value is not None))


if __name__ == "__main__":
    raise SystemExit(main())
