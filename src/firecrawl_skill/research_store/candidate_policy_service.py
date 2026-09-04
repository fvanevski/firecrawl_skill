"""PostgreSQL-authoritative ranking provenance and corpus-budget gates."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from firecrawl_skill.research_store.acquisition.candidate_ranking import (
    BudgetCheckResult,
    BudgetViolation,
    CandidateBudget,
    UrlType,
    check_corpus_budget,
    classify_url,
    is_generic_url_type,
)

from .run_budget_authority import load_persisted_candidate_budget


class CandidatePolicyError(RuntimeError):
    """Authoritative candidate-policy persistence or validation failed."""


@dataclass(frozen=True)
class BudgetMetrics:
    candidate_count: int
    total_bytes: int
    total_chunks: int
    generic_page_count: int
    extraction_attempts: int
    per_asset_chunk_counts: Mapping[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_count": self.candidate_count,
            "total_bytes": self.total_bytes,
            "total_chunks": self.total_chunks,
            "generic_page_count": self.generic_page_count,
            "extraction_attempts": self.extraction_attempts,
            "per_asset_chunk_counts": dict(sorted(self.per_asset_chunk_counts.items())),
        }


@dataclass(frozen=True)
class BudgetDecision:
    check_id: UUID
    phase: str
    result: BudgetCheckResult
    overridden_limits: frozenset[str]
    content_sha256: str

    @property
    def accepted(self) -> bool:
        return self.result.accepted_with_overrides(self.overridden_limits)

    @property
    def reason_code(self) -> str | None:
        if self.result.hard_violations:
            return "candidate_budget_hard_limit"
        if self.result.soft_violations and not self.accepted:
            return "candidate_budget_override_required"
        return None

    def summary(self) -> dict[str, Any]:
        return {
            "budget_check_id": str(self.check_id),
            "phase": self.phase,
            "accepted": self.accepted,
            "reason_code": self.reason_code,
            "overridden_limits": sorted(self.overridden_limits),
            "hard_violations": [item.to_dict() for item in self.result.hard_violations],
            "soft_violations": [item.to_dict() for item in self.result.soft_violations],
            "content_sha256": self.content_sha256,
        }


@dataclass(frozen=True)
class _CheckContent:
    metrics: BudgetMetrics
    budget: CandidateBudget
    scope: Mapping[str, Any]
    result: BudgetCheckResult
    content_sha256: str


def pre_extraction_scope(
    rankings: Sequence[Mapping[str, Any]],
    selected_candidate_ids: Sequence[UUID],
) -> dict[str, Any]:
    """Return the exact candidate scope fingerprinted by pre-extraction checks."""

    ranked_candidates: list[dict[str, Any]] = []
    for row in rankings:
        item: dict[str, Any] = {
            "candidate_id": str(row["candidate_id"]),
            "url_type": str(row["url_type"]),
            "decision": str(row["decision"]),
            "selected_ordinal": row.get("selected_ordinal"),
        }
        for key in ("search_response_id", "candidate_occurrence_id"):
            value = row.get(key)
            if value is not None:
                item[key] = str(value)
        coverage_items = row.get("coverage_item_ids")
        if coverage_items is not None:
            if not isinstance(coverage_items, Sequence) or isinstance(
                coverage_items, (str, bytes)
            ):
                raise CandidatePolicyError(
                    "pre-extraction coverage_item_ids must be a sequence"
                )
            item["coverage_item_ids"] = [str(value) for value in coverage_items]
        ranked_candidates.append(item)
    return {
        "ranked_candidates": ranked_candidates,
        "selected_candidate_ids": [str(item) for item in selected_candidate_ids],
    }


class CandidatePolicyService:
    """Persist immutable ranking decisions and exact scope-bound budget checks."""

    def __init__(self, uow_factory):
        self.uow_factory = uow_factory

    @staticmethod
    def _run_candidate_budget(
        uow: Any,
        run_id: UUID,
        fallback: CandidateBudget,
    ) -> CandidateBudget:
        """Prefer candidate policy persisted with a canonical planning snapshot."""

        runs = getattr(uow, "runs", None)
        getter = getattr(runs, "get_latest_budget_snapshot", None)
        if not callable(getter):
            return fallback
        row = getter(run_id)
        if row is None:
            return fallback
        if not isinstance(row, Mapping):
            raise CandidatePolicyError("persisted run budget snapshot row is malformed")
        raw_snapshot = row.get("snapshot")
        if isinstance(raw_snapshot, str):
            try:
                raw_snapshot = json.loads(raw_snapshot)
            except json.JSONDecodeError as exc:
                raise CandidatePolicyError(
                    "persisted run budget snapshot is not valid JSON"
                ) from exc
        if not isinstance(raw_snapshot, Mapping):
            raise CandidatePolicyError("persisted run budget snapshot is malformed")
        try:
            return load_persisted_candidate_budget(raw_snapshot)
        except (TypeError, ValueError) as exc:
            raise CandidatePolicyError(str(exc)) from exc

    def accepted_pre_extraction_replay(
        self,
        run_id: UUID,
        lifecycle_revision: int,
    ) -> dict[str, Any] | None:
        """Return one exact soft-authorized pre-extraction scope for replay.

        Hard-rejected, unresolved-soft, and violation-free checks are not replay
        authority. More than one authorized scope at one lifecycle revision is
        ambiguous and therefore fails closed.
        """

        max_rows = 100
        with self.uow_factory() as uow:
            connection = getattr(uow, "connection", None)
            if connection is None or not callable(getattr(connection, "cursor", None)):
                return None
            with connection.cursor() as cursor:
                cursor.execute(
                """SELECT id,scope,hard_violations,soft_violations,content_sha256
                     FROM corpus_budget_checks
                    WHERE run_id=%s AND phase='pre_extraction'
                      AND lifecycle_revision=%s
                    ORDER BY created_at,id LIMIT %s""",
                (run_id, lifecycle_revision, max_rows + 1),
            )
                rows = cursor.fetchall()
                if len(rows) > max_rows:
                    raise CandidatePolicyError(
                        "pre-extraction replay authority exceeds bounded check history"
                    )
                authorized: list[dict[str, Any]] = []
                for check_id, scope, hard_rows, soft_rows, digest in rows:
                    hard = {
                        str(item.get("limit_name"))
                        for item in (hard_rows or [])
                        if isinstance(item, Mapping) and item.get("limit_name")
                    }
                    soft = {
                        str(item.get("limit_name"))
                        for item in (soft_rows or [])
                        if isinstance(item, Mapping) and item.get("limit_name")
                    }
                    if hard or not soft:
                        continue
                    overridden = self._overrides(cursor, UUID(str(check_id)))
                    if not soft <= overridden:
                        continue
                    if not isinstance(scope, Mapping):
                        raise CandidatePolicyError(
                            "authorized pre-extraction replay scope is malformed"
                        )
                    authorized.append(
                        {
                            "check_id": UUID(str(check_id)),
                            "scope": dict(scope),
                            "content_sha256": str(digest),
                        }
                    )
                if len(authorized) > 1:
                    raise CandidatePolicyError(
                        "multiple authorized pre-extraction replay scopes exist for one revision"
                    )
                return authorized[0] if authorized else None

    def record_rankings(
        self,
        run_id: UUID,
        search_response_id: UUID,
        invocation_id: UUID,
        rankings: Sequence[Mapping[str, Any]],
    ) -> None:
        from .postgres_acquisition import CandidateRankingConflictError

        try:
            with self.uow_factory() as uow:
                uow.candidates.record_rankings(
                    run_id, search_response_id, invocation_id, rankings
                )
        except CandidateRankingConflictError as exc:
            raise CandidatePolicyError(str(exc)) from exc

    def evaluate_pre_extraction(
        self,
        run_id: UUID,
        invocation_id: UUID | None,
        rankings: Sequence[Mapping[str, Any]],
        selected_candidate_ids: Sequence[UUID],
        budget: CandidateBudget,
        *,
        lifecycle_revision: int | None = None,
    ) -> BudgetDecision:
        with self.uow_factory() as uow, uow.connection.cursor() as cursor:
            budget = self._run_candidate_budget(uow, run_id, budget)
            current = self._asset_metrics(uow, cursor, run_id)
            attempts = self._attempts(cursor, run_id) + len(selected_candidate_ids)
        metrics = BudgetMetrics(
            len(rankings),
            current.total_bytes,
            current.total_chunks,
            sum(is_generic_url_type(UrlType(str(row["url_type"]))) for row in rankings),
            attempts,
            current.per_asset_chunk_counts,
        )
        scope = pre_extraction_scope(rankings, selected_candidate_ids)
        return self._record_check(
            run_id,
            "pre_extraction",
            metrics,
            budget,
            scope,
            invocation_id=invocation_id,
            lifecycle_revision=lifecycle_revision,
        )

    def evaluate_post_extraction(
        self,
        run_id: UUID,
        invocation_id: UUID,
        budget: CandidateBudget,
    ) -> BudgetDecision:
        with self.uow_factory() as uow, uow.connection.cursor() as cursor:
            budget = self._run_candidate_budget(uow, run_id, budget)
            measured = self._asset_metrics(uow, cursor, run_id)
            metrics = BudgetMetrics(
                measured.candidate_count,
                measured.total_bytes,
                measured.total_chunks,
                measured.generic_page_count,
                self._attempts(cursor, run_id),
                measured.per_asset_chunk_counts,
            )
        return self._record_check(
            run_id,
            "post_extraction",
            metrics,
            budget,
            {"asset_ids": sorted(metrics.per_asset_chunk_counts)},
            invocation_id=invocation_id,
        )

    def evaluate_completion_admission(
        self,
        run_id: UUID,
        lifecycle_revision: int,
        budget: CandidateBudget,
    ) -> BudgetDecision:
        with self.uow_factory() as uow, uow.connection.cursor() as cursor:
            self._require_indexing_revision(uow, cursor, run_id, lifecycle_revision)
            budget = self._run_candidate_budget(uow, run_id, budget)
            measured = self._completion_metrics(uow, cursor, run_id, True)
            metrics = BudgetMetrics(
                measured.candidate_count,
                measured.total_bytes,
                measured.total_chunks,
                measured.generic_page_count,
                self._attempts(cursor, run_id),
                measured.per_asset_chunk_counts,
            )
        return self._record_check(
            run_id,
            "completion_admission",
            metrics,
            budget,
            {"subject_ids": sorted(metrics.per_asset_chunk_counts)},
            lifecycle_revision=lifecycle_revision,
        )

    def evaluate_completion_admission_preview(
        self,
        run_id: UUID,
        lifecycle_revision: int,
        budget: CandidateBudget,
    ) -> BudgetDecision:
        """Measure the retained set while the run is still acquiring.

        The preview persists an append-only ``completion_admission`` row bound to
        the acquiring lifecycle revision so an operator has an exact check to bind
        a soft-limit override to before the authoritative seal. It never authorizes
        sealing; the authoritative check still runs after the transition.
        """
        with self.uow_factory() as uow, uow.connection.cursor() as cursor:
            self._require_acquiring_revision(uow, cursor, run_id, lifecycle_revision)
            budget = self._run_candidate_budget(uow, run_id, budget)
            measured = _measure(
                uow,
                cursor,
                run_id,
                table="run_asset_promotion_subjects",
                stages=["retained"],
            )
            metrics = BudgetMetrics(
                measured.candidate_count,
                measured.total_bytes,
                measured.total_chunks,
                measured.generic_page_count,
                self._attempts(cursor, run_id),
                measured.per_asset_chunk_counts,
            )
        return self._record_check(
            run_id,
            "completion_admission",
            metrics,
            budget,
            {"subject_ids": sorted(metrics.per_asset_chunk_counts)},
            lifecycle_revision=lifecycle_revision,
        )

    def require_matching_completion_admission_preview(
        self,
        uow: Any,
        cursor: Any,
        run_id: UUID,
        lifecycle_revision: int,
        budget: CandidateBudget,
        preview_check_id: UUID,
    ) -> BudgetDecision:
        """Revalidate an accepted preview under the caller's acquiring run lock.

        This method intentionally accepts an existing UoW/cursor. The caller can
        therefore hold the authoritative run lock, remeasure the retained set,
        prove exact preview identity, and commit the first lifecycle transition
        in the same PostgreSQL transaction.
        """
        self._require_acquiring_revision(uow, cursor, run_id, lifecycle_revision)
        budget = self._run_candidate_budget(uow, run_id, budget)
        measured = _measure(
            uow,
            cursor,
            run_id,
            table="run_asset_promotion_subjects",
            stages=["retained"],
        )
        metrics = BudgetMetrics(
            measured.candidate_count,
            measured.total_bytes,
            measured.total_chunks,
            measured.generic_page_count,
            self._attempts(cursor, run_id),
            measured.per_asset_chunk_counts,
        )
        result = _check(metrics, budget)
        scope = {"subject_ids": sorted(metrics.per_asset_chunk_counts)}
        digest = _check_digest(
            run_id,
            "completion_admission",
            lifecycle_revision,
            metrics,
            budget,
            scope,
            result,
        )
        cursor.execute(
            """SELECT lifecycle_revision,content_sha256
                 FROM corpus_budget_checks
                WHERE id=%s AND run_id=%s AND phase='completion_admission'
                FOR SHARE""",
            (preview_check_id, run_id),
        )
        row = cursor.fetchone()
        if row is None:
            raise CandidatePolicyError(
                f"completion-admission preview {preview_check_id} does not belong "
                f"to run {run_id}"
            )
        stored_revision, stored_digest = row
        if stored_revision is None or int(stored_revision) != lifecycle_revision:
            raise CandidatePolicyError(
                "completion-admission preview revision no longer matches the "
                "acquiring run revision; re-curate and retry seal-acquisition"
            )
        if str(stored_digest) != digest:
            raise CandidatePolicyError(
                "completion-admission preview changed before acquisition seal; "
                "re-curate and retry seal-acquisition"
            )
        decision = self._decision(cursor, preview_check_id, result, digest)
        if not decision.accepted:
            raise CandidatePolicyError(decision_error_message(decision))
        return decision

    def find_matching_completion_admission_preview(
        self,
        run_id: UUID,
        lifecycle_revision: int,
        budget: CandidateBudget,
    ) -> UUID | None:
        """Recover an exact accepted predecessor preview after interrupted sealing.

        A retry in ``indexing`` may have lost the in-memory preview identifier.
        Recovery is allowed only for the exact acquiring predecessor revision and
        only when the current in-progress membership, metrics, budget, violations,
        and bound soft overrides reproduce that preview exactly.
        """
        if lifecycle_revision < 2:
            return None
        predecessor_revision = lifecycle_revision - 2
        with self.uow_factory() as uow, uow.connection.cursor() as cursor:
            self._require_indexing_revision(uow, cursor, run_id, lifecycle_revision)
            budget = self._run_candidate_budget(uow, run_id, budget)
            measured = _measure(
                uow,
                cursor,
                run_id,
                table="run_asset_promotion_subjects",
                stages=["retained", "evidence_eligible", "completion_critical"],
            )
            metrics = BudgetMetrics(
                measured.candidate_count,
                measured.total_bytes,
                measured.total_chunks,
                measured.generic_page_count,
                self._attempts(cursor, run_id),
                measured.per_asset_chunk_counts,
            )
            result = _check(metrics, budget)
            scope = {"subject_ids": sorted(metrics.per_asset_chunk_counts)}
            digest = _check_digest(
                run_id,
                "completion_admission",
                predecessor_revision,
                metrics,
                budget,
                scope,
                result,
            )
            cursor.execute(
                """SELECT id FROM corpus_budget_checks
                    WHERE run_id=%s AND phase='completion_admission'
                      AND lifecycle_revision=%s AND content_sha256=%s""",
                (run_id, predecessor_revision, digest),
            )
            rows = cursor.fetchall()
            if len(rows) > 1:
                raise CandidatePolicyError(
                    "multiple exact predecessor completion-admission previews matched; "
                    "repair fails closed"
                )
            if not rows:
                return None
            check_id = UUID(str(rows[0][0]))
            decision = self._decision(cursor, check_id, result, digest)
            if decision.result.hard_violations or not decision.accepted:
                return None
            return check_id

    def rebind_completion_admission_override(
        self,
        run_id: UUID,
        authoritative_check_id: UUID,
        preview_check_id: UUID,
        lifecycle_revision: int,
    ) -> BudgetDecision:
        """Carry a preview-bound soft override onto the authoritative check.

        The preview and authoritative checks must measure the identical retained
        set and the preview must be the exact acquiring predecessor revision of
        the current indexing state. Only after those facts are proven are soft
        overrides copied. Hard violations remain non-overridable.
        """
        with self.uow_factory() as uow, uow.connection.cursor() as cursor:
            self._require_indexing_revision(uow, cursor, run_id, lifecycle_revision)
            cursor.execute(
                """SELECT lifecycle_revision FROM corpus_budget_checks
                    WHERE id=%s AND run_id=%s AND phase='completion_admission'
                    FOR SHARE""",
                (preview_check_id, run_id),
            )
            revision_row = cursor.fetchone()
            if revision_row is None or revision_row[0] is None:
                raise CandidatePolicyError(
                    "completion-admission preview does not belong to the requested run"
                )
            preview_revision = int(revision_row[0])
            if preview_revision + 2 != lifecycle_revision:
                raise CandidatePolicyError(
                    "completion-admission preview is not the exact acquiring "
                    "predecessor of the indexing revision"
                )
            authoritative = self._load_check_content(
                cursor, authoritative_check_id, run_id
            )
            preview = self._load_check_content(cursor, preview_check_id, run_id)
            if preview.result.hard_violations:
                raise CandidatePolicyError(
                    "preview completion check has hard violations; "
                    "re-curation is required, not an override"
                )
            recomputed = _check_digest(
                run_id,
                "completion_admission",
                lifecycle_revision,
                preview.metrics,
                preview.budget,
                preview.scope,
                preview.result,
            )
            if recomputed != authoritative.content_sha256:
                raise CandidatePolicyError(
                    "completion membership changed between preview and authoritative "
                    "check; re-curate and retry seal-acquisition"
                )
            if self._overrides(cursor, preview_check_id):
                self._copy_overrides(
                    cursor,
                    run_id,
                    preview_check_id,
                    authoritative_check_id,
                )
            return self._decision(
                cursor,
                authoritative_check_id,
                authoritative.result,
                authoritative.content_sha256,
            )

    def require_matching_completion_check(
        self,
        uow: Any,
        cursor: Any,
        run_id: UUID,
        lifecycle_revision: int,
        budget: CandidateBudget,
        *,
        include_evidence: bool = False,
    ) -> BudgetDecision:
        self._require_indexing_revision(uow, cursor, run_id, lifecycle_revision)
        budget = self._run_candidate_budget(uow, run_id, budget)
        measured = self._completion_metrics(uow, cursor, run_id, include_evidence)
        metrics = BudgetMetrics(
            measured.candidate_count,
            measured.total_bytes,
            measured.total_chunks,
            measured.generic_page_count,
            self._attempts(cursor, run_id),
            measured.per_asset_chunk_counts,
        )
        result = _check(metrics, budget)
        scope = {"subject_ids": sorted(metrics.per_asset_chunk_counts)}
        digest = _check_digest(
            run_id,
            "completion_admission",
            lifecycle_revision,
            metrics,
            budget,
            scope,
            result,
        )
        cursor.execute(
            """SELECT id FROM corpus_budget_checks
               WHERE run_id=%s AND phase='completion_admission'
                 AND content_sha256=%s""",
            (run_id, digest),
        )
        row = cursor.fetchone()
        if row is None:
            raise CandidatePolicyError(
                "completion membership changed after its budget check; rerun admission"
            )
        decision = self._decision(cursor, UUID(str(row[0])), result, digest)
        if not decision.accepted:
            raise CandidatePolicyError(decision_error_message(decision))
        return decision

    def require_soft_override_action_binding(
        self,
        uow: Any,
        run_id: UUID,
        check_id: UUID,
        lifecycle_revision: int,
        authority_fingerprint: str,
        expected_limits: Sequence[str],
    ) -> tuple[str, ...]:
        """Validate the exact persisted soft-boundary represented by one action."""
        with uow.connection.cursor() as cursor:
            cursor.execute(
                """SELECT lifecycle_revision,soft_violations,hard_violations,
                          content_sha256
                     FROM corpus_budget_checks
                    WHERE id=%s AND run_id=%s FOR SHARE""",
                (check_id, run_id),
            )
            row = cursor.fetchone()
            if row is None:
                raise CandidatePolicyError(
                    "budget check does not belong to requested run"
                )
            if row[0] is None or int(row[0]) != lifecycle_revision:
                raise CandidatePolicyError(
                    "budget check lifecycle revision no longer matches operator action"
                )
            if str(row[3]) != authority_fingerprint:
                raise CandidatePolicyError(
                    "budget check fingerprint no longer matches operator action"
                )
            soft = {str(item.get("limit_name")) for item in (row[1] or [])}
            hard = {str(item.get("limit_name")) for item in (row[2] or [])}
            if hard:
                raise CandidatePolicyError(
                    "hard candidate-budget violations are not approvable"
                )
            overridden = self._overrides(cursor, check_id)
            unresolved = soft - overridden
            expected = {str(item) for item in expected_limits}
            if not unresolved or unresolved != expected:
                raise CandidatePolicyError(
                    "candidate-budget soft boundary changed after action creation"
                )
            return tuple(sorted(unresolved))

    def record_action_overrides(
        self,
        uow: Any,
        run_id: UUID,
        check_id: UUID,
        limit_names: Sequence[str],
        *,
        reason: str,
        author: str,
    ) -> tuple[UUID, ...]:
        """Record an exact action-owned set of soft overrides in the caller UoW."""
        requested = tuple(str(item).strip() for item in limit_names)
        if not requested or any(not item for item in requested):
            raise ValueError("at least one non-empty limit_name is required")
        if len(set(requested)) != len(requested):
            raise ValueError("duplicate limit_name values are not allowed")
        if not reason.strip() or not author.strip():
            raise ValueError("reason and author must be non-empty")
        with uow.connection.cursor() as cursor:
            cursor.execute(
                """SELECT soft_violations,hard_violations FROM corpus_budget_checks
                   WHERE id=%s AND run_id=%s FOR SHARE""",
                (check_id, run_id),
            )
            row = cursor.fetchone()
            if row is None:
                raise CandidatePolicyError(
                    "budget check does not belong to requested run"
                )
            soft = {str(item.get("limit_name")) for item in (row[0] or [])}
            hard = {str(item.get("limit_name")) for item in (row[1] or [])}
            requested_set = set(requested)
            if requested_set & hard:
                raise CandidatePolicyError(
                    "hard candidate-budget violations cannot be overridden"
                )
            if not requested_set <= soft:
                raise CandidatePolicyError(
                    "operator action references a non-soft candidate-budget limit"
                )
            override_ids: list[UUID] = []
            for limit_name in requested:
                digest = _sha256(
                    {
                        "budget_check_id": str(check_id),
                        "run_id": str(run_id),
                        "limit_name": limit_name,
                        "reason": reason.strip(),
                        "author": author.strip(),
                    }
                )
                cursor.execute(
                    """INSERT INTO budget_override_justifications(
                       budget_check_id,run_id,limit_name,reason,author,content_sha256)
                       VALUES(%s,%s,%s,%s,%s,%s)
                       ON CONFLICT(budget_check_id,content_sha256) DO NOTHING
                       RETURNING id""",
                    (
                        check_id,
                        run_id,
                        limit_name,
                        reason.strip(),
                        author.strip(),
                        digest,
                    ),
                )
                inserted = cursor.fetchone()
                if inserted is None:
                    cursor.execute(
                        """SELECT id FROM budget_override_justifications
                           WHERE budget_check_id=%s AND content_sha256=%s""",
                        (check_id, digest),
                    )
                    inserted = cursor.fetchone()
                if inserted is None:
                    raise CandidatePolicyError("budget override idempotency race")
                override_ids.append(UUID(str(inserted[0])))
            return tuple(override_ids)

    def record_override(
        self,
        run_id: UUID,
        check_id: UUID,
        limit_name: str,
        *,
        reason: str,
        author: str,
    ) -> UUID:
        with self.uow_factory() as uow:
            override_ids = self.record_action_overrides(
                uow,
                run_id,
                check_id,
                (limit_name,),
                reason=reason,
                author=author,
            )
        return override_ids[0]

    def list_checks(self, run_id: UUID) -> list[dict[str, Any]]:
        with self.uow_factory() as uow, uow.connection.cursor() as cursor:
            cursor.execute(
                """SELECT id,phase,invocation_id,lifecycle_revision,candidate_count,
                   total_bytes,total_chunks,generic_page_count,extraction_attempts,
                   per_asset_chunk_counts,scope,budget,hard_violations,
                   soft_violations,accepted_without_override,content_sha256,created_at
                   FROM corpus_budget_checks WHERE run_id=%s ORDER BY created_at,id""",
                (run_id,),
            )
            names = (
                "id",
                "phase",
                "invocation_id",
                "lifecycle_revision",
                "candidate_count",
                "total_bytes",
                "total_chunks",
                "generic_page_count",
                "extraction_attempts",
                "per_asset_chunk_counts",
                "scope",
                "budget",
                "hard_violations",
                "soft_violations",
                "accepted_without_override",
                "content_sha256",
                "created_at",
            )
            result = []
            for row in cursor.fetchall():
                item = dict(zip(names, row, strict=True))
                check_id = UUID(str(item["id"]))
                item["id"] = str(check_id)
                if item["invocation_id"] is not None:
                    item["invocation_id"] = str(item["invocation_id"])
                item["overridden_limits"] = sorted(self._overrides(cursor, check_id))
                result.append(item)
            return result

    def _record_check(
        self,
        run_id: UUID,
        phase: str,
        metrics: BudgetMetrics,
        budget: CandidateBudget,
        scope: Mapping[str, Any],
        *,
        invocation_id: UUID | None = None,
        lifecycle_revision: int | None = None,
    ) -> BudgetDecision:
        result = _check(metrics, budget)
        digest = _check_digest(
            run_id, phase, lifecycle_revision, metrics, budget, scope, result
        )
        with self.uow_factory() as uow, uow.connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO corpus_budget_checks(
                   run_id,invocation_id,lifecycle_revision,phase,candidate_count,
                   total_bytes,total_chunks,generic_page_count,extraction_attempts,
                   per_asset_chunk_counts,scope,budget,accepted_without_override,
                   hard_violations,soft_violations,content_sha256)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(run_id,phase,content_sha256) DO NOTHING RETURNING id""",
                (
                    run_id,
                    invocation_id,
                    lifecycle_revision,
                    phase,
                    metrics.candidate_count,
                    metrics.total_bytes,
                    metrics.total_chunks,
                    metrics.generic_page_count,
                    metrics.extraction_attempts,
                    json.dumps(dict(sorted(metrics.per_asset_chunk_counts.items()))),
                    json.dumps(scope, sort_keys=True),
                    json.dumps(budget.to_dict(), sort_keys=True),
                    result.accepted,
                    json.dumps([item.to_dict() for item in result.hard_violations]),
                    json.dumps([item.to_dict() for item in result.soft_violations]),
                    digest,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    """SELECT id FROM corpus_budget_checks
                       WHERE run_id=%s AND phase=%s AND content_sha256=%s""",
                    (run_id, phase, digest),
                )
                row = cursor.fetchone()
            if row is None:
                raise CandidatePolicyError("budget check idempotency race")
            return self._decision(cursor, UUID(str(row[0])), result, digest)

    @staticmethod
    def _decision(cursor, check_id, result, digest) -> BudgetDecision:
        return BudgetDecision(
            check_id,
            _phase(cursor, check_id),
            result,
            CandidatePolicyService._overrides(cursor, check_id),
            digest,
        )

    @staticmethod
    def _overrides(cursor, check_id: UUID) -> frozenset[str]:
        cursor.execute(
            "SELECT DISTINCT limit_name FROM budget_override_justifications WHERE budget_check_id=%s",
            (check_id,),
        )
        return frozenset(str(row[0]) for row in cursor.fetchall())

    @staticmethod
    def _attempts(cursor, run_id: UUID) -> int:
        cursor.execute(
            "SELECT count(*) FROM extraction_attempts WHERE run_id=%s", (run_id,)
        )
        return int(cursor.fetchone()[0] or 0)

    @staticmethod
    def _require_indexing_revision(uow, cursor, run_id, revision) -> None:
        state, current = uow.runs._lock_workflow_run(cursor, run_id)
        if state != "indexing":
            raise CandidatePolicyError(f"run {run_id} must be indexing; got {state}")
        if int(current) != revision:
            raise CandidatePolicyError(
                f"candidate budget revision is stale: expected {revision}, current {current}"
            )

    @staticmethod
    def _require_acquiring_revision(uow, cursor, run_id, revision) -> None:
        state, current = uow.runs._lock_workflow_run(cursor, run_id)
        if state != "acquiring":
            raise CandidatePolicyError(f"run {run_id} must be acquiring; got {state}")
        if int(current) != revision:
            raise CandidatePolicyError(
                f"candidate budget revision is stale: expected {revision}, current {current}"
            )

    def _load_check_content(
        self, cursor, check_id: UUID, run_id: UUID
    ) -> _CheckContent:
        cursor.execute(
            """SELECT candidate_count,total_bytes,total_chunks,generic_page_count,
               extraction_attempts,per_asset_chunk_counts,scope,budget,hard_violations,
               soft_violations,content_sha256
               FROM corpus_budget_checks WHERE id=%s AND run_id=%s FOR SHARE""",
            (check_id, run_id),
        )
        row = cursor.fetchone()
        if row is None:
            raise CandidatePolicyError(
                f"budget check {check_id} does not belong to run {run_id}"
            )
        (
            candidate_count,
            total_bytes,
            total_chunks,
            generic_page_count,
            extraction_attempts,
            per_asset_chunk_counts,
            scope,
            budget_dict,
            hard_violations,
            soft_violations,
            content_sha256,
        ) = row
        metrics = BudgetMetrics(
            int(candidate_count),
            int(total_bytes),
            int(total_chunks),
            int(generic_page_count),
            int(extraction_attempts),
            dict(per_asset_chunk_counts or {}),
        )
        budget = CandidateBudget(**(budget_dict or {}))
        hard = tuple(BudgetViolation(**item) for item in (hard_violations or []))
        soft = tuple(BudgetViolation(**item) for item in (soft_violations or []))
        result = BudgetCheckResult(
            violations=hard + soft,
            soft_violations=soft,
            hard_violations=hard,
            requires_override=bool(soft),
        )
        return _CheckContent(
            metrics=metrics,
            budget=budget,
            scope=scope,
            result=result,
            content_sha256=str(content_sha256),
        )

    @staticmethod
    def _copy_overrides(
        cursor, run_id: UUID, source_check_id: UUID, target_check_id: UUID
    ) -> None:
        cursor.execute(
            """SELECT limit_name,reason,author FROM budget_override_justifications
               WHERE budget_check_id=%s ORDER BY limit_name,author""",
            (source_check_id,),
        )
        for limit_name, reason, author in cursor.fetchall():
            digest = _sha256(
                {
                    "budget_check_id": str(target_check_id),
                    "run_id": str(run_id),
                    "limit_name": limit_name,
                    "reason": reason,
                    "author": author,
                }
            )
            cursor.execute(
                """INSERT INTO budget_override_justifications(
                   budget_check_id,run_id,limit_name,reason,author,content_sha256)
                   VALUES(%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(budget_check_id,content_sha256) DO NOTHING""",
                (target_check_id, run_id, limit_name, reason, author, digest),
            )

    @staticmethod
    def _asset_metrics(uow, cursor, run_id) -> BudgetMetrics:
        return _measure(uow, cursor, run_id, table="research_run_assets")

    @staticmethod
    def _completion_metrics(uow, cursor, run_id, include_evidence) -> BudgetMetrics:
        return _measure(
            uow,
            cursor,
            run_id,
            table="run_asset_promotion_subjects",
            include_evidence=include_evidence,
        )


def _phase(cursor, check_id: UUID) -> str:
    cursor.execute("SELECT phase FROM corpus_budget_checks WHERE id=%s", (check_id,))
    row = cursor.fetchone()
    if row is None:
        raise CandidatePolicyError("budget check disappeared")
    return str(row[0])


def _measure(
    uow,
    cursor,
    run_id,
    *,
    table,
    include_evidence=False,
    stages=None,
) -> BudgetMetrics:
    if table == "research_run_assets":
        prefix = """SELECT asset.snapshot_id,COALESCE(snapshot.raw_byte_length,0),
                   COALESCE(candidate.canonical_url,source.canonical_url),
                   count(DISTINCT chunk.id)
                   FROM research_run_assets asset
                   JOIN asset_snapshots snapshot ON snapshot.id=asset.snapshot_id
                   JOIN sources source ON source.id=snapshot.source_id
                   LEFT JOIN run_asset_promotion_subjects subject
                     ON subject.run_id=asset.run_id AND subject.snapshot_id=asset.snapshot_id
                    AND subject.role=asset.role
                   LEFT JOIN search_candidates candidate ON candidate.id=subject.candidate_id"""
        where = "WHERE asset.run_id=%s"
        group = "GROUP BY asset.snapshot_id,snapshot.raw_byte_length,candidate.canonical_url,source.canonical_url"
    else:
        prefix = """SELECT subject.id,COALESCE(snapshot.raw_byte_length,0),
                    COALESCE(candidate.canonical_url,source.canonical_url),
                    count(DISTINCT chunk.id)
                    FROM run_asset_promotion_subjects subject
                    JOIN asset_snapshots snapshot ON snapshot.id=subject.snapshot_id
                    JOIN sources source ON source.id=snapshot.source_id
                    LEFT JOIN search_candidates candidate ON candidate.id=subject.candidate_id"""
        if stages is None:
            stages = ["completion_critical"]
            if include_evidence:
                stages.insert(0, "evidence_eligible")
        where = "WHERE subject.run_id=%s AND subject.current_stage = ANY(%s)"
        group = "GROUP BY subject.id,snapshot.raw_byte_length,candidate.canonical_url,source.canonical_url"
    joins = """LEFT JOIN documents document ON document.snapshot_id=snapshot.id
             AND document.parser_version=%s AND document.normalization_version=%s
             LEFT JOIN chunks chunk ON chunk.document_id=document.id
             AND chunk.chunker_version=%s"""
    params = [
        uow.parser_version,
        uow.normalization_version,
        uow.chunker_version,
        run_id,
    ]
    if table != "research_run_assets":
        params.append(stages)
    cursor.execute(f"{prefix} {joins} {where} {group}", tuple(params))
    rows = cursor.fetchall()
    counts = {str(row[0]): int(row[3] or 0) for row in rows}
    return BudgetMetrics(
        len(rows),
        sum(int(row[1] or 0) for row in rows),
        sum(counts.values()),
        sum(is_generic_url_type(classify_url(str(row[2] or ""))) for row in rows),
        0,
        counts,
    )


def _check(metrics: BudgetMetrics, budget: CandidateBudget) -> BudgetCheckResult:
    return check_corpus_budget(
        (None,) * metrics.candidate_count,
        metrics.total_bytes,
        metrics.total_chunks,
        metrics.generic_page_count,
        metrics.extraction_attempts,
        metrics.per_asset_chunk_counts,
        budget=budget,
    )


def _check_digest(run_id, phase, lifecycle_revision, metrics, budget, scope, result):
    return _sha256(
        {
            "schema_version": "candidate-budget-check-v1",
            "run_id": str(run_id),
            "phase": phase,
            "lifecycle_revision": lifecycle_revision,
            "metrics": metrics.to_dict(),
            "scope": scope,
            "budget": budget.to_dict(),
            "hard_violations": [item.to_dict() for item in result.hard_violations],
            "soft_violations": [item.to_dict() for item in result.soft_violations],
        }
    )


def decision_error_message(decision: BudgetDecision) -> str:
    if decision.result.hard_violations:
        limits = ",".join(item.limit_name for item in decision.result.hard_violations)
        kind = "hard limit rejected"
    else:
        limits = ",".join(
            item.limit_name
            for item in decision.result.soft_violations
            if item.limit_name not in decision.overridden_limits
        )
        kind = "override required"
    return f"candidate budget {kind} phase={decision.phase} check={decision.check_id} limits={limits}"


def _jsonable(value):
    if isinstance(value, UUID):
        return str(value)
    return value


def _sha256(value: Mapping[str, Any]) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(raw).hexdigest()


__all__ = [
    "BudgetDecision",
    "BudgetMetrics",
    "CandidatePolicyError",
    "CandidatePolicyService",
    "decision_error_message",
    "pre_extraction_scope",
]
