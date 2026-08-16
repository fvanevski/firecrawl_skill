"""Connection-bound PostgreSQL acquisition persistence for issue #258.

This module owns search-plan/response provenance, candidate identity/decision
persistence, and extraction-attempt history. Repositories receive the exact
connection owned by ``PostgresUnitOfWork`` and never own transaction lifecycle,
commit, rollback, or savepoints.
"""

from __future__ import annotations

import datetime
import hashlib
import io
import json
from functools import wraps
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from .domain import utcnow
from .parsing_legacy import extract_search_response_items, parse_raw_search_response
from .url import canonicalize_candidate_url

try:
    from research_domain import load_model, serialize_model
    from research_domain.codec import to_dict
    from research_domain.models import SearchPlan
    from research_domain.validation import ValidationContext, validate_references
except ImportError:
    import sys

    sys.path.insert(0, str(Path(__file__).parents[1]))
    from research_domain import load_model, serialize_model
    from research_domain.codec import to_dict
    from research_domain.models import SearchPlan
    from research_domain.validation import ValidationContext, validate_references


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _json_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _lock_workflow_run(cur: Any, run_id: Any) -> tuple[Any, int]:
    cur.execute(
        "SELECT state,lifecycle_revision FROM research_runs WHERE id=%s FOR UPDATE",
        (run_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise KeyError(run_id)
    return row


class PostgresSearchAcquisitionRepository:
    """Canonical search-plan and raw-response persistence on one UoW connection."""

    def __init__(self, connection: Any) -> None:
        self.__connection = connection

    def record_search_plan(
        self,
        run_id,
        research_spec_id,
        revision,
        search_plan,
        idempotency_key,
        **metadata,
    ):
        if revision <= 0:
            raise ValueError("search plan revision must be positive")
        if not idempotency_key.strip():
            raise ValueError("idempotency_key is required")
        if isinstance(search_plan, dict):
            plan_payload = dict(search_plan)
            plan_model = load_model(plan_payload)
        else:
            plan_model = search_plan
            plan_payload = serialize_model(plan_model)
        if not isinstance(plan_model, SearchPlan):
            raise TypeError("provided payload is not a valid SearchPlan")
        if plan_model.revision != revision:
            raise ValueError("search plan revision does not match parameter")

        with self.__connection.cursor() as cur:
            _lock_workflow_run(cur, run_id)
            cur.execute(
                """SELECT id, payload FROM research_specs
                WHERE run_id=%s AND (id=%s OR payload->>'research_spec_id'=%s)
                ORDER BY spec_revision DESC LIMIT 1""",
                (run_id, research_spec_id, str(research_spec_id)),
            )
            spec_row = cur.fetchone()
            if spec_row is None:
                raise ValueError("search plan references an unknown research spec")
            db_spec_id, spec_payload = spec_row
            spec_model = load_model(spec_payload)
            if plan_model.research_spec_id != spec_model.research_spec_id:
                raise ValueError(
                    "search plan research_spec_id does not match research spec"
                )
            validate_references(plan_model, ValidationContext(research_spec=spec_model))
            digest = _json_sha256(plan_payload)
            cur.execute(
                """SELECT id, research_spec_id, revision, content_sha256
                FROM search_plans WHERE run_id=%s AND idempotency_key=%s""",
                (run_id, idempotency_key),
            )
            idempotent = cur.fetchone()
            if idempotent is not None:
                stored_id, stored_spec_id, stored_revision, stored_digest = idempotent
                if (stored_spec_id, stored_revision, stored_digest) != (
                    db_spec_id,
                    revision,
                    digest,
                ):
                    raise ValueError("idempotency key was used for another search plan")
                return stored_id
            cur.execute(
                "SELECT id FROM search_plans WHERE run_id=%s AND revision=%s",
                (run_id, revision),
            )
            if cur.fetchone() is not None:
                raise ValueError(
                    f"search plan revision {revision} already exists for run"
                )
            cur.execute(
                "UPDATE search_plans SET status='superseded' WHERE run_id=%s AND status='active'",
                (run_id,),
            )
            cur.execute(
                """INSERT INTO search_plans(
                run_id, research_spec_id, revision, schema_name, schema_version,
                status, payload, content_sha256, idempotency_key)
                VALUES(%s,%s,%s,%s,%s,'active',%s,%s,%s) RETURNING id""",
                (
                    run_id,
                    db_spec_id,
                    revision,
                    plan_model.SCHEMA_VERSION,
                    1,
                    _canonical_json(plan_payload),
                    digest,
                    idempotency_key,
                ),
            )
            plan_id = cur.fetchone()[0]
            for idx, query in enumerate(plan_model.queries):
                query_payload = to_dict(query)
                freshness_dict = to_dict(query.freshness_requirement)
                cur.execute(
                    """INSERT INTO search_plan_queries(
                    id, plan_id, run_id, query_index, query_text, facet,
                    target_question_ids, target_claim_ids, intended_source_classes,
                    expected_organizations, freshness_requirement, expected_contribution,
                    domain_restrictions, negative_terms, priority, status, payload)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending',%s)""",
                    (
                        query.query_id,
                        plan_id,
                        run_id,
                        idx,
                        query.query,
                        query.facet,
                        json.dumps([str(qid) for qid in query.target_question_ids]),
                        json.dumps([str(cid) for cid in query.target_claim_ids]),
                        json.dumps(list(query.intended_source_classes)),
                        json.dumps(list(query.expected_organizations)),
                        _canonical_json(freshness_dict),
                        query.expected_contribution,
                        json.dumps(list(query.domain_restrictions)),
                        json.dumps(list(query.negative_terms)),
                        query.priority,
                        _canonical_json(query_payload),
                    ),
                )
            cur.execute(
                "UPDATE research_runs SET search_plan_id=%s WHERE id=%s",
                (plan_id, run_id),
            )
            return plan_id

    def get_search_plan(self, run_id, plan_id=None, revision=None):
        with self.__connection.cursor() as cur:
            columns = """id, run_id, research_spec_id, revision, schema_name,
                schema_version, status, payload, content_sha256, idempotency_key, created_at"""
            if plan_id is not None:
                cur.execute(
                    f"SELECT {columns} FROM search_plans WHERE id=%s AND run_id=%s",
                    (plan_id, run_id),
                )
            elif revision is not None:
                cur.execute(
                    f"SELECT {columns} FROM search_plans WHERE run_id=%s AND revision=%s",
                    (run_id, revision),
                )
            else:
                cur.execute(
                    f"SELECT {columns} FROM search_plans WHERE run_id=%s ORDER BY revision DESC LIMIT 1",
                    (run_id,),
                )
            row = cur.fetchone()
            if row is None:
                raise ValueError("search plan not found")
            cur.execute(
                """SELECT id, plan_id, run_id, query_index, query_text, facet,
                target_question_ids, target_claim_ids, intended_source_classes,
                expected_organizations, freshness_requirement, expected_contribution,
                domain_restrictions, negative_terms, priority, status, payload, created_at
                FROM search_plan_queries WHERE plan_id=%s ORDER BY query_index ASC""",
                (row[0],),
            )
            queries = [self._plan_query_mapping(q) for q in cur.fetchall()]
        keys = (
            "id",
            "run_id",
            "research_spec_id",
            "revision",
            "schema_name",
            "schema_version",
            "status",
            "payload",
            "content_sha256",
            "idempotency_key",
            "created_at",
        )
        result = dict(zip(keys, row, strict=True))
        result["queries"] = queries
        return result

    def list_search_plans(self, run_id):
        with self.__connection.cursor() as cur:
            cur.execute(
                """SELECT id, run_id, research_spec_id, revision, schema_name,
                schema_version, status, content_sha256, idempotency_key, created_at
                FROM search_plans WHERE run_id=%s ORDER BY revision ASC""",
                (run_id,),
            )
            keys = (
                "id",
                "run_id",
                "research_spec_id",
                "revision",
                "schema_name",
                "schema_version",
                "status",
                "content_sha256",
                "idempotency_key",
                "created_at",
            )
            return [dict(zip(keys, row, strict=True)) for row in cur.fetchall()]

    @staticmethod
    def _plan_query_mapping(row):
        keys = (
            "id",
            "plan_id",
            "run_id",
            "query_index",
            "query_text",
            "facet",
            "target_question_ids",
            "target_claim_ids",
            "intended_source_classes",
            "expected_organizations",
            "freshness_requirement",
            "expected_contribution",
            "domain_restrictions",
            "negative_terms",
            "priority",
            "status",
            "payload",
            "created_at",
        )
        return dict(zip(keys, row, strict=True))

    def get_plan_query(self, query_id, run_id=None):
        with self.__connection.cursor() as cur:
            columns = """id, plan_id, run_id, query_index, query_text, facet,
                target_question_ids, target_claim_ids, intended_source_classes,
                expected_organizations, freshness_requirement, expected_contribution,
                domain_restrictions, negative_terms, priority, status, payload, created_at"""
            if run_id is not None:
                cur.execute(
                    f"SELECT {columns} FROM search_plan_queries WHERE id=%s AND run_id=%s",
                    (query_id, run_id),
                )
            else:
                cur.execute(
                    f"SELECT {columns} FROM search_plan_queries WHERE id=%s",
                    (query_id,),
                )
            row = cur.fetchone()
        if row is None:
            raise ValueError("search plan query not found")
        return self._plan_query_mapping(row)

    def list_plan_queries(self, plan_id):
        with self.__connection.cursor() as cur:
            cur.execute(
                """SELECT id, plan_id, run_id, query_index, query_text, facet,
                target_question_ids, target_claim_ids, intended_source_classes,
                expected_organizations, freshness_requirement, expected_contribution,
                domain_restrictions, negative_terms, priority, status, payload, created_at
                FROM search_plan_queries WHERE plan_id=%s ORDER BY query_index ASC""",
                (plan_id,),
            )
            return [self._plan_query_mapping(row) for row in cur.fetchall()]

    def record_search_response(
        self,
        run_id,
        query_text,
        backend,
        raw_payload,
        idempotency_key,
        blob_store,
        *,
        plan_id=None,
        plan_query_id=None,
        provider_request_id=None,
        parser_version="firecrawl-search-v1",
        http_status=None,
        error_message=None,
        requested_at=None,
        responded_at=None,
        transport_metadata=None,
        **metadata,
    ):
        run_id = UUID(str(run_id))
        plan_id = UUID(str(plan_id)) if plan_id is not None else None
        plan_query_id = UUID(str(plan_query_id)) if plan_query_id is not None else None
        if not isinstance(query_text, str) or not query_text.strip():
            raise ValueError("query_text must be non-empty")
        if not isinstance(backend, str) or not backend.strip():
            raise ValueError("backend must be non-empty")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("idempotency_key must be non-empty")
        raw_bytes = (
            raw_payload.encode("utf-8") if isinstance(raw_payload, str) else raw_payload
        )
        content_sha256 = hashlib.sha256(raw_bytes).hexdigest()

        with self.__connection.cursor() as cur:
            _lock_workflow_run(cur, run_id)
            cur.execute(
                """SELECT id, content_sha256, query_text, backend, status, result_count,
                    raw_blob_sha256, raw_blob_bytes, mime_type, error_message,
                    payload_summary, transport_metadata, provider_request_id,
                    http_status, parser_version, plan_id, plan_query_id,
                    requested_at, responded_at, created_at
                FROM search_responses WHERE run_id=%s AND idempotency_key=%s""",
                (run_id, idempotency_key),
            )
            existing = cur.fetchone()
            if existing is not None:
                if existing[1] != content_sha256:
                    raise ValueError(
                        f"idempotency_key conflict: key '{idempotency_key}' already recorded with different content SHA-256"
                    )
                return self._search_response_mapping(existing, run_id, idempotency_key)
            if plan_id is not None:
                cur.execute(
                    "SELECT id FROM search_plans WHERE id=%s AND run_id=%s",
                    (plan_id, run_id),
                )
                if cur.fetchone() is None:
                    raise ValueError(
                        f"search plan {plan_id} not found for run {run_id}"
                    )
            if plan_query_id is not None:
                cur.execute(
                    "SELECT id, plan_id FROM search_plan_queries WHERE id=%s AND run_id=%s",
                    (plan_query_id, run_id),
                )
                pq_row = cur.fetchone()
                if pq_row is None:
                    raise ValueError(
                        f"search plan query {plan_query_id} not found for run {run_id}"
                    )
                if plan_id is not None and pq_row[1] != plan_id:
                    raise ValueError(
                        f"search plan query {plan_query_id} does not belong to plan {plan_id}"
                    )
                if plan_id is None:
                    plan_id = pq_row[1]
            blob_ref = blob_store.put(
                io.BytesIO(raw_bytes), mime_type="application/json"
            )
            parsed_status, parsed_result_count, parsed_summary, parsed_error = (
                parse_raw_search_response(
                    raw_bytes,
                    http_status=http_status,
                    parser_version=parser_version,
                )
            )
            final_error = error_message or parsed_error
            now_dt = utcnow()
            req_at = requested_at or now_dt
            resp_at = responded_at or now_dt
            t_meta = dict(transport_metadata) if transport_metadata else {}
            if metadata:
                t_meta.update(metadata)
            cur.execute(
                """INSERT INTO search_responses(
                    run_id, plan_id, plan_query_id, query_text, backend,
                    provider_request_id, status, http_status, parser_version,
                    raw_blob_sha256, raw_blob_bytes, mime_type, content_sha256,
                    result_count, error_message, transport_metadata, payload_summary,
                    idempotency_key, requested_at, responded_at, created_at)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id, created_at""",
                (
                    run_id,
                    plan_id,
                    plan_query_id,
                    query_text,
                    backend,
                    provider_request_id,
                    parsed_status,
                    http_status,
                    parser_version,
                    blob_ref.sha256,
                    blob_ref.byte_length,
                    blob_ref.mime_type or "application/json",
                    content_sha256,
                    parsed_result_count,
                    final_error,
                    json.dumps(t_meta),
                    json.dumps(parsed_summary),
                    idempotency_key,
                    req_at,
                    resp_at,
                    now_dt,
                ),
            )
            resp_id, created_at = cur.fetchone()
        return {
            "id": resp_id,
            "run_id": run_id,
            "plan_id": plan_id,
            "plan_query_id": plan_query_id,
            "query_text": query_text,
            "backend": backend,
            "provider_request_id": provider_request_id,
            "status": parsed_status,
            "http_status": http_status,
            "parser_version": parser_version,
            "raw_blob_sha256": blob_ref.sha256,
            "raw_blob_bytes": blob_ref.byte_length,
            "mime_type": blob_ref.mime_type or "application/json",
            "content_sha256": content_sha256,
            "result_count": parsed_result_count,
            "error_message": final_error,
            "transport_metadata": t_meta,
            "payload_summary": parsed_summary,
            "idempotency_key": idempotency_key,
            "requested_at": req_at,
            "responded_at": resp_at,
            "created_at": created_at,
        }

    @staticmethod
    def _search_response_mapping(row, run_id=None, idempotency_key=None):
        # Legacy idempotency SELECT order differs from the ordinary read order.
        if len(row) == 20:
            return {
                "id": row[0],
                "run_id": run_id,
                "plan_id": row[15],
                "plan_query_id": row[16],
                "query_text": row[2],
                "backend": row[3],
                "provider_request_id": row[12],
                "status": row[4],
                "http_status": row[13],
                "parser_version": row[14],
                "raw_blob_sha256": row[6],
                "raw_blob_bytes": row[7],
                "mime_type": row[8],
                "content_sha256": row[1],
                "result_count": row[5],
                "error_message": row[9],
                "transport_metadata": row[11],
                "payload_summary": row[10],
                "idempotency_key": idempotency_key,
                "requested_at": row[17],
                "responded_at": row[18],
                "created_at": row[19],
            }
        keys = (
            "id",
            "run_id",
            "plan_id",
            "plan_query_id",
            "query_text",
            "backend",
            "provider_request_id",
            "status",
            "http_status",
            "parser_version",
            "raw_blob_sha256",
            "raw_blob_bytes",
            "mime_type",
            "content_sha256",
            "result_count",
            "error_message",
            "transport_metadata",
            "payload_summary",
            "idempotency_key",
            "requested_at",
            "responded_at",
            "created_at",
        )
        return dict(zip(keys, row, strict=True))

    def get_search_response(self, response_id, run_id=None):
        response_id = UUID(str(response_id))
        with self.__connection.cursor() as cur:
            query = """SELECT id, run_id, plan_id, plan_query_id, query_text, backend,
                provider_request_id, status, http_status, parser_version,
                raw_blob_sha256, raw_blob_bytes, mime_type, content_sha256,
                result_count, error_message, transport_metadata, payload_summary,
                idempotency_key, requested_at, responded_at, created_at
                FROM search_responses WHERE id=%s"""
            params = [response_id]
            if run_id is not None:
                query += " AND run_id=%s"
                params.append(UUID(str(run_id)))
            cur.execute(query, tuple(params))
            row = cur.fetchone()
        if row is None:
            raise ValueError(f"search response {response_id} not found")
        return self._search_response_mapping(row)

    def list_search_responses(
        self, run_id, *, plan_id=None, plan_query_id=None, status=None
    ):
        run_id = UUID(str(run_id))
        with self.__connection.cursor() as cur:
            query = """SELECT id, run_id, plan_id, plan_query_id, query_text, backend,
                provider_request_id, status, http_status, parser_version,
                raw_blob_sha256, raw_blob_bytes, mime_type, content_sha256,
                result_count, error_message, transport_metadata, payload_summary,
                idempotency_key, requested_at, responded_at, created_at
                FROM search_responses WHERE run_id=%s"""
            params = [run_id]
            if plan_id is not None:
                query += " AND plan_id=%s"
                params.append(UUID(str(plan_id)))
            if plan_query_id is not None:
                query += " AND plan_query_id=%s"
                params.append(UUID(str(plan_query_id)))
            if status is not None:
                query += " AND status=%s"
                params.append(status)
            query += " ORDER BY created_at ASC, id ASC"
            cur.execute(query, tuple(params))
            return [self._search_response_mapping(row) for row in cur.fetchall()]

    def open_raw_search_response_blob(self, response_id, blob_store, run_id=None):
        response = self.get_search_response(response_id, run_id=run_id)
        return blob_store.open(response["raw_blob_sha256"])


class CandidateRankingConflictError(RuntimeError):
    """A candidate ranking idempotency key conflicts with persisted evidence."""


class PostgresCandidateRepository:
    """Canonical candidate identity, occurrence, and selection-decision persistence."""

    def __init__(
        self,
        connection: Any,
        search_repository: PostgresSearchAcquisitionRepository,
    ) -> None:
        self.__connection = connection
        self.__search_repository = search_repository

    def record_response_candidates(
        self,
        run_id,
        search_response_id,
        blob_store,
        *,
        plan_id=None,
        plan_query_id=None,
    ):
        run_id = UUID(str(run_id))
        search_response_id = UUID(str(search_response_id))
        plan_id = UUID(str(plan_id)) if plan_id is not None else None
        plan_query_id = UUID(str(plan_query_id)) if plan_query_id is not None else None
        response = self.__search_repository.get_search_response(
            search_response_id, run_id=run_id
        )
        plan_id = response.get("plan_id") if plan_id is None else plan_id
        plan_query_id = (
            response.get("plan_query_id") if plan_query_id is None else plan_query_id
        )
        with blob_store.open(response["raw_blob_sha256"]) as handle:
            raw_bytes = handle.read()
        try:
            payload_data = json.loads(raw_bytes.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload_data = {}
        items = extract_search_response_items(payload_data)
        occurrences = []
        with self.__connection.cursor() as cur:
            _lock_workflow_run(cur, run_id)
            for idx, item in enumerate(items, start=1):
                raw_url = title = snippet = pub_date = None
                date_signals: dict[str, Any] = {}
                backend_meta: dict[str, Any] = {}
                if isinstance(item, dict):
                    raw_url = (
                        item.get("url") or item.get("link") or item.get("target_url")
                    )
                    title = item.get("title") or item.get("name")
                    snippet = (
                        item.get("snippet")
                        or item.get("description")
                        or item.get("content")
                        or item.get("markdown")
                    )
                    pub_date = (
                        item.get("published_at")
                        or item.get("publishedDate")
                        or item.get("date")
                    )
                    if pub_date:
                        date_signals["published_date"] = str(pub_date)
                    backend_meta = {
                        key: value
                        for key, value in item.items()
                        if key
                        not in (
                            "url",
                            "link",
                            "title",
                            "snippet",
                            "description",
                            "content",
                            "markdown",
                        )
                    }
                elif isinstance(item, str):
                    raw_url = item
                if not raw_url or not isinstance(raw_url, str) or not raw_url.strip():
                    continue
                try:
                    canonical_url, redacted_orig_url = canonicalize_candidate_url(
                        raw_url
                    )
                except ValueError:
                    continue
                canonical_sha = hashlib.sha256(canonical_url.encode()).hexdigest()
                domain = urlsplit(canonical_url).hostname or "unknown"
                cur.execute(
                    """SELECT id, recurrence_count, title, snippet, date_signals, backend_metadata
                    FROM search_candidates WHERE run_id=%s AND canonical_url_sha256=%s""",
                    (run_id, canonical_sha),
                )
                candidate = cur.fetchone()
                now_dt = utcnow()
                if candidate is not None:
                    (
                        cand_id,
                        recurrence,
                        old_title,
                        old_snippet,
                        old_dates,
                        old_backend,
                    ) = candidate
                    cur.execute(
                        """UPDATE search_candidates SET recurrence_count=%s,last_seen_at=%s,
                        title=%s,snippet=%s,date_signals=%s,backend_metadata=%s
                        WHERE id=%s AND run_id=%s""",
                        (
                            recurrence + 1,
                            now_dt,
                            title or old_title,
                            snippet or old_snippet,
                            json.dumps({**(old_dates or {}), **date_signals}),
                            json.dumps({**(old_backend or {}), **backend_meta}),
                            cand_id,
                            run_id,
                        ),
                    )
                else:
                    cur.execute(
                        """INSERT INTO search_candidates(
                        run_id,canonical_url,canonical_url_sha256,original_url,title,snippet,
                        domain,backend,date_signals,backend_metadata,recurrence_count,
                        first_seen_at,last_seen_at,created_at)
                        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,1,%s,%s,%s) RETURNING id""",
                        (
                            run_id,
                            canonical_url,
                            canonical_sha,
                            redacted_orig_url,
                            title,
                            snippet,
                            domain,
                            response["backend"],
                            json.dumps(date_signals),
                            json.dumps(backend_meta),
                            now_dt,
                            now_dt,
                            now_dt,
                        ),
                    )
                    cand_id = cur.fetchone()[0]
                raw_item = item if isinstance(item, dict) else {"url": raw_url}
                cur.execute(
                    """INSERT INTO candidate_occurrences(
                    candidate_id,run_id,search_response_id,plan_id,plan_query_id,rank,
                    query_text,original_url,title,snippet,raw_item,discovered_at)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(search_response_id,rank) DO UPDATE SET
                    candidate_id=EXCLUDED.candidate_id,original_url=EXCLUDED.original_url,
                    title=EXCLUDED.title,snippet=EXCLUDED.snippet,raw_item=EXCLUDED.raw_item
                    RETURNING id""",
                    (
                        cand_id,
                        run_id,
                        search_response_id,
                        plan_id,
                        plan_query_id,
                        idx,
                        response["query_text"],
                        redacted_orig_url,
                        title,
                        snippet,
                        json.dumps(raw_item),
                        now_dt,
                    ),
                )
                occurrence_id = cur.fetchone()[0]
                occurrences.append(
                    {
                        "id": occurrence_id,
                        "candidate_id": cand_id,
                        "run_id": run_id,
                        "search_response_id": search_response_id,
                        "plan_id": plan_id,
                        "plan_query_id": plan_query_id,
                        "rank": idx,
                        "query_text": response["query_text"],
                        "canonical_url": canonical_url,
                        "original_url": redacted_orig_url,
                        "title": title,
                        "snippet": snippet,
                        "raw_item": raw_item,
                    }
                )
        return occurrences

    @staticmethod
    def _candidate_mapping(row):
        keys = (
            "id",
            "run_id",
            "canonical_url",
            "canonical_url_sha256",
            "original_url",
            "title",
            "snippet",
            "domain",
            "backend",
            "published_at",
            "date_signals",
            "backend_metadata",
            "recurrence_count",
            "duplicate_group_id",
            "first_seen_at",
            "last_seen_at",
            "created_at",
            "independence_assessment",
        )
        return dict(zip(keys, row, strict=True))

    def get_candidate(self, candidate_id, run_id=None):
        candidate_id = UUID(str(candidate_id))
        with self.__connection.cursor() as cur:
            query = """SELECT id,run_id,canonical_url,canonical_url_sha256,original_url,title,
                snippet,domain,backend,published_at,date_signals,backend_metadata,recurrence_count,
                duplicate_group_id,first_seen_at,last_seen_at,created_at,independence_assessment
                FROM search_candidates WHERE id=%s"""
            params = [candidate_id]
            if run_id is not None:
                query += " AND run_id=%s"
                params.append(UUID(str(run_id)))
            cur.execute(query, tuple(params))
            row = cur.fetchone()
        if row is None:
            raise ValueError(f"search candidate {candidate_id} not found")
        return self._candidate_mapping(row)

    def list_candidates(
        self, run_id, *, domain=None, min_recurrence=None, duplicate_group_id=None
    ):
        run_id = UUID(str(run_id))
        with self.__connection.cursor() as cur:
            query = """SELECT id,run_id,canonical_url,canonical_url_sha256,original_url,title,
                snippet,domain,backend,published_at,date_signals,backend_metadata,recurrence_count,
                duplicate_group_id,first_seen_at,last_seen_at,created_at,independence_assessment
                FROM search_candidates WHERE run_id=%s"""
            params = [run_id]
            if domain is not None:
                query += " AND domain=%s"
                params.append(domain)
            if min_recurrence is not None:
                query += " AND recurrence_count>=%s"
                params.append(int(min_recurrence))
            if duplicate_group_id is not None:
                query += " AND duplicate_group_id=%s"
                params.append(UUID(str(duplicate_group_id)))
            query += " ORDER BY recurrence_count DESC, created_at ASC, id ASC"
            cur.execute(query, tuple(params))
            return [self._candidate_mapping(row) for row in cur.fetchall()]

    def list_candidates_paginated(
        self,
        run_id,
        *,
        plan_id=None,
        plan_query_id=None,
        query_text=None,
        domain=None,
        min_recurrence=None,
        duplicate_group_id=None,
        limit=20,
        offset=0,
    ):
        run_id = UUID(str(run_id))
        if limit < 1 or limit > 200:
            raise ValueError("limit must be between 1 and 200")
        if offset < 0:
            raise ValueError("offset must be non-negative")
        needs_join = (
            plan_id is not None or plan_query_id is not None or query_text is not None
        )
        base = (
            "FROM search_candidates c JOIN candidate_occurrences o ON o.candidate_id=c.id WHERE c.run_id=%s"
            if needs_join
            else "FROM search_candidates c WHERE c.run_id=%s"
        )
        clauses = []
        params: list[Any] = [run_id]
        for clause, value in (
            ("o.plan_id=%s", UUID(str(plan_id)) if plan_id is not None else None),
            (
                "o.plan_query_id=%s",
                UUID(str(plan_query_id)) if plan_query_id is not None else None,
            ),
            ("o.query_text=%s", query_text),
            ("c.domain=%s", domain),
            (
                "c.recurrence_count>=%s",
                int(min_recurrence) if min_recurrence is not None else None,
            ),
            (
                "c.duplicate_group_id=%s",
                UUID(str(duplicate_group_id))
                if duplicate_group_id is not None
                else None,
            ),
        ):
            if value is not None:
                clauses.append(clause)
                params.append(value)
        where = "" if not clauses else " AND " + " AND ".join(clauses)
        columns = """c.id,c.run_id,c.canonical_url,c.canonical_url_sha256,c.original_url,
            c.title,c.snippet,c.domain,c.backend,c.published_at,c.date_signals,c.backend_metadata,
            c.recurrence_count,c.duplicate_group_id,c.first_seen_at,c.last_seen_at,c.created_at,
            c.independence_assessment"""
        distinct = "DISTINCT " if needs_join else ""
        with self.__connection.cursor() as cur:
            if needs_join:
                cur.execute(
                    f"SELECT COUNT(*) FROM (SELECT DISTINCT c.id {base}{where}) sub",
                    tuple(params),
                )
            else:
                cur.execute(f"SELECT COUNT(*) {base}{where}", tuple(params))
            total = cur.fetchone()[0]
            cur.execute(
                f"SELECT {distinct}{columns} {base}{where} ORDER BY c.recurrence_count DESC,c.created_at ASC,c.id ASC LIMIT %s OFFSET %s",
                (*params, limit, offset),
            )
            items = [self._candidate_mapping(row) for row in cur.fetchall()]
        return {
            "items": items,
            "total_count": total,
            "limit": limit,
            "offset": offset,
            "has_next": offset + len(items) < total,
        }

    def list_candidate_occurrences(self, candidate_id, run_id=None):
        candidate_id = UUID(str(candidate_id))
        with self.__connection.cursor() as cur:
            query = """SELECT id,candidate_id,run_id,search_response_id,plan_id,plan_query_id,
                rank,query_text,original_url,title,snippet,raw_item,discovered_at
                FROM candidate_occurrences WHERE candidate_id=%s"""
            params = [candidate_id]
            if run_id is not None:
                query += " AND run_id=%s"
                params.append(UUID(str(run_id)))
            query += " ORDER BY discovered_at ASC,rank ASC,id ASC"
            cur.execute(query, tuple(params))
            keys = (
                "id",
                "candidate_id",
                "run_id",
                "search_response_id",
                "plan_id",
                "plan_query_id",
                "rank",
                "query_text",
                "original_url",
                "title",
                "snippet",
                "raw_item",
                "discovered_at",
            )
            return [dict(zip(keys, row, strict=True)) for row in cur.fetchall()]

    def assign_duplicate_group(self, candidate_ids, group_id=None, run_id=None):
        if not candidate_ids:
            raise ValueError("candidate_ids must not be empty")
        candidates = [UUID(str(item)) for item in candidate_ids]
        target = UUID(str(group_id)) if group_id is not None else candidates[0]
        with self.__connection.cursor() as cur:
            cur.execute(
                """INSERT INTO duplicate_groups(id,run_id,rationale,created_at)
                VALUES(%s,%s,'legacy assignment',%s) ON CONFLICT(id) DO NOTHING""",
                (
                    target,
                    UUID(str(run_id)) if run_id else None,
                    datetime.datetime.now(datetime.timezone.utc),
                ),
            )
            if run_id is None:
                cur.execute(
                    "UPDATE search_candidates SET duplicate_group_id=%s WHERE id=ANY(%s)",
                    (target, candidates),
                )
            else:
                cur.execute(
                    "UPDATE search_candidates SET duplicate_group_id=%s WHERE id=ANY(%s) AND run_id=%s",
                    (target, candidates, UUID(str(run_id))),
                )
        return target

    def persist_duplicate_group(self, group_id, run_id, rationale):
        with self.__connection.cursor() as cur:
            cur.execute(
                """INSERT INTO duplicate_groups(id,run_id,rationale) VALUES(%s,%s,%s)
                ON CONFLICT(id) DO UPDATE SET rationale=EXCLUDED.rationale""",
                (UUID(str(group_id)), UUID(str(run_id)), rationale),
            )

    def update_candidate_independence(self, candidate_id, independence_assessment_dict):
        with self.__connection.cursor() as cur:
            cur.execute(
                "UPDATE search_candidates SET independence_assessment=%s::jsonb WHERE id=%s",
                (json.dumps(independence_assessment_dict), UUID(str(candidate_id))),
            )

    def record_rankings(self, run_id, search_response_id, invocation_id, rankings):
        """Persist immutable selected/rejected candidate decisions."""
        with self.__connection.cursor() as cursor:
            for row in rankings:
                payload = {
                    "run_id": str(run_id),
                    "search_response_id": str(search_response_id),
                    "invocation_id": str(invocation_id),
                    **{
                        key: str(value) if isinstance(value, UUID) else value
                        for key, value in row.items()
                    },
                }
                digest = hashlib.sha256(
                    json.dumps(
                        payload,
                        sort_keys=True,
                        separators=(",", ":"),
                        default=str,
                    ).encode()
                ).hexdigest()
                cursor.execute(
                    "SELECT content_sha256 FROM candidate_rankings WHERE invocation_id=%s AND candidate_id=%s",
                    (invocation_id, row["candidate_id"]),
                )
                existing = cursor.fetchone()
                if existing is not None:
                    if str(existing[0]) != digest:
                        raise CandidateRankingConflictError(
                            "ranking provenance conflict"
                        )
                    continue
                cursor.execute(
                    """INSERT INTO candidate_rankings(
                    run_id,search_response_id,invocation_id,candidate_id,source_rank,url,url_type,
                    base_score,url_type_penalty,freshness_status,freshness_penalty,is_duplicate,
                    duplication_penalty,expected_char_count,size_penalty,total_score,rationale,
                    decision,selected_ordinal,decision_reason,content_sha256)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        run_id,
                        search_response_id,
                        invocation_id,
                        row["candidate_id"],
                        row["source_rank"],
                        row["url"],
                        row["url_type"],
                        row["base_score"],
                        row["url_type_penalty"],
                        row["freshness_status"],
                        row["freshness_penalty"],
                        row["is_duplicate"],
                        row["duplication_penalty"],
                        row.get("expected_char_count"),
                        row["size_penalty"],
                        row["total_score"],
                        row["rationale"],
                        row["decision"],
                        row.get("selected_ordinal"),
                        row["decision_reason"],
                        digest,
                    ),
                )


class PostgresExtractionAttemptRepository:
    """Canonical extraction-attempt history on one UoW connection."""

    def __init__(self, connection: Any) -> None:
        self.__connection = connection

    @staticmethod
    def _blob_fields(blob):
        if blob is None:
            return None, None, None, None
        return blob.sha256, blob.uri, blob.byte_length, blob.mime_type

    def create_attempt(
        self,
        candidate_id,
        run_id,
        invocation_id,
        attempt_number,
        method,
        method_version,
        requested_format,
        start_time,
        end_time,
        exit_status,
        http_status,
        backend_status,
        raw_blob,
        normalized_blob,
        parser_used,
        quality_metrics,
        failure_class,
        retry_parent_id,
        disposition,
        error_message,
        selection_reason,
    ):
        raw_sha, raw_uri, raw_len, raw_mime = self._blob_fields(raw_blob)
        norm_sha, norm_uri, norm_len, norm_mime = self._blob_fields(normalized_blob)
        with self.__connection.cursor() as cur:
            cur.execute(
                """INSERT INTO extraction_attempts(
                candidate_id,run_id,invocation_id,attempt_number,method,method_version,
                requested_format,start_time,end_time,exit_status,http_status,backend_status,
                raw_blob_sha256,raw_blob_uri,raw_blob_byte_length,raw_blob_mime_type,
                normalized_blob_sha256,normalized_blob_uri,normalized_blob_byte_length,
                normalized_blob_mime_type,parser_used,quality_metrics,failure_class,retry_parent_id,
                disposition,error_message,selection_reason)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id""",
                (
                    str(candidate_id),
                    str(run_id),
                    str(invocation_id) if invocation_id else None,
                    attempt_number,
                    method,
                    method_version,
                    requested_format,
                    start_time,
                    end_time,
                    exit_status,
                    http_status,
                    backend_status,
                    raw_sha,
                    raw_uri,
                    raw_len,
                    raw_mime,
                    norm_sha,
                    norm_uri,
                    norm_len,
                    norm_mime,
                    parser_used,
                    json.dumps(quality_metrics.to_dict()) if quality_metrics else None,
                    failure_class,
                    str(retry_parent_id) if retry_parent_id else None,
                    disposition,
                    error_message,
                    selection_reason,
                ),
            )
            row = cur.fetchone()
        return UUID(str(row[0])) if row else None

    def complete_attempt(
        self,
        attempt_id,
        exit_status,
        raw_blob,
        normalized_blob,
        parser_used,
        quality_metrics,
        failure_class,
        http_status,
        backend_status,
        end_time,
        error_message,
    ):
        raw_sha, raw_uri, raw_len, raw_mime = self._blob_fields(raw_blob)
        norm_sha, norm_uri, norm_len, norm_mime = self._blob_fields(normalized_blob)
        with self.__connection.cursor() as cur:
            cur.execute(
                """UPDATE extraction_attempts SET exit_status=%s,end_time=%s,http_status=%s,
                backend_status=%s,raw_blob_sha256=COALESCE(%s,raw_blob_sha256),
                raw_blob_uri=COALESCE(%s,raw_blob_uri),raw_blob_byte_length=COALESCE(%s,raw_blob_byte_length),
                raw_blob_mime_type=COALESCE(%s,raw_blob_mime_type),
                normalized_blob_sha256=COALESCE(%s,normalized_blob_sha256),
                normalized_blob_uri=COALESCE(%s,normalized_blob_uri),
                normalized_blob_byte_length=COALESCE(%s,normalized_blob_byte_length),
                normalized_blob_mime_type=COALESCE(%s,normalized_blob_mime_type),
                parser_used=COALESCE(%s,parser_used),quality_metrics=COALESCE(%s,quality_metrics),
                failure_class=%s,error_message=COALESCE(%s,error_message) WHERE id=%s""",
                (
                    exit_status,
                    end_time,
                    http_status,
                    backend_status,
                    raw_sha,
                    raw_uri,
                    raw_len,
                    raw_mime,
                    norm_sha,
                    norm_uri,
                    norm_len,
                    norm_mime,
                    parser_used,
                    json.dumps(quality_metrics.to_dict()) if quality_metrics else None,
                    failure_class,
                    error_message,
                    str(attempt_id),
                ),
            )

    def update_disposition(self, attempt_id, disposition):
        with self.__connection.cursor() as cur:
            cur.execute(
                "UPDATE extraction_attempts SET disposition=%s WHERE id=%s",
                (disposition, str(attempt_id)),
            )

    def record_quality_metrics(self, attempt_id, quality_metrics):
        with self.__connection.cursor() as cur:
            cur.execute(
                "UPDATE extraction_attempts SET quality_metrics=%s WHERE id=%s",
                (
                    json.dumps(quality_metrics.to_dict()) if quality_metrics else None,
                    str(attempt_id),
                ),
            )

    def select_final_attempt(self, candidate_id, attempt_id, selection_reason):
        with self.__connection.cursor() as cur:
            cur.execute(
                "UPDATE extraction_attempts SET selected=false WHERE candidate_id=%s AND selected=true",
                (str(candidate_id),),
            )
            cur.execute(
                """UPDATE extraction_attempts SET selected=true,
                selection_reason=COALESCE(%s,selection_reason) WHERE id=%s AND candidate_id=%s""",
                (selection_reason, str(attempt_id), str(candidate_id)),
            )

    @staticmethod
    def _columns():
        return """id,candidate_id,run_id,invocation_id,attempt_number,method,method_version,
        requested_format,start_time,end_time,exit_status,http_status,backend_status,
        raw_blob_sha256,raw_blob_uri,raw_blob_byte_length,raw_blob_mime_type,
        normalized_blob_sha256,normalized_blob_uri,normalized_blob_byte_length,
        normalized_blob_mime_type,parser_used,quality_metrics,failure_class,retry_parent_id,
        disposition,error_message,selection_reason,selected,created_at"""

    def get_selected_attempt(self, candidate_id):
        with self.__connection.cursor() as cur:
            cur.execute(
                f"SELECT {self._columns()} FROM extraction_attempts WHERE candidate_id=%s AND selected=true LIMIT 1",
                (str(candidate_id),),
            )
            row = cur.fetchone()
        return None if row is None else self._row_to_extraction_attempt_mapping(row)

    def list_attempts_for_candidate(
        self, candidate_id, run_id=None, limit=100, offset=0
    ):
        with self.__connection.cursor() as cur:
            if run_id:
                cur.execute(
                    f"SELECT {self._columns()} FROM extraction_attempts WHERE candidate_id=%s AND run_id=%s ORDER BY attempt_number ASC LIMIT %s OFFSET %s",
                    (str(candidate_id), str(run_id), limit, offset),
                )
            else:
                cur.execute(
                    f"SELECT {self._columns()} FROM extraction_attempts WHERE candidate_id=%s ORDER BY attempt_number ASC LIMIT %s OFFSET %s",
                    (str(candidate_id), limit, offset),
                )
            return [
                self._row_to_extraction_attempt_mapping(row) for row in cur.fetchall()
            ]

    def list_attempts_for_run(
        self,
        run_id,
        candidate_id=None,
        method=None,
        exit_status=None,
        disposition=None,
        limit=100,
        offset=0,
    ):
        conditions = ["run_id=%s"]
        params: list[Any] = [str(run_id)]
        for column, value in (
            ("candidate_id", candidate_id),
            ("method", method),
            ("exit_status", exit_status),
            ("disposition", disposition),
        ):
            if value is not None:
                conditions.append(f"{column}=%s")
                params.append(str(value) if column == "candidate_id" else value)
        with self.__connection.cursor() as cur:
            cur.execute(
                f"SELECT {self._columns()} FROM extraction_attempts WHERE {' AND '.join(conditions)} ORDER BY attempt_number ASC LIMIT %s OFFSET %s",
                (*params, limit, offset),
            )
            return [
                self._row_to_extraction_attempt_mapping(row) for row in cur.fetchall()
            ]

    def get_attempt(self, attempt_id, run_id=None):
        with self.__connection.cursor() as cur:
            if run_id:
                cur.execute(
                    f"SELECT {self._columns()} FROM extraction_attempts WHERE id=%s AND run_id=%s LIMIT 1",
                    (str(attempt_id), str(run_id)),
                )
            else:
                cur.execute(
                    f"SELECT {self._columns()} FROM extraction_attempts WHERE id=%s LIMIT 1",
                    (str(attempt_id),),
                )
            row = cur.fetchone()
        return None if row is None else self._row_to_extraction_attempt_mapping(row)

    @staticmethod
    def _row_to_extraction_attempt_mapping(row):
        keys = (
            "id",
            "candidate_id",
            "run_id",
            "invocation_id",
            "attempt_number",
            "method",
            "method_version",
            "requested_format",
            "start_time",
            "end_time",
            "exit_status",
            "http_status",
            "backend_status",
            "raw_blob_sha256",
            "raw_blob_uri",
            "raw_blob_byte_length",
            "raw_blob_mime_type",
            "normalized_blob_sha256",
            "normalized_blob_uri",
            "normalized_blob_byte_length",
            "normalized_blob_mime_type",
            "parser_used",
            "quality_metrics",
            "failure_class",
            "retry_parent_id",
            "disposition",
            "error_message",
            "selection_reason",
            "selected",
            "created_at",
        )
        result = dict(zip(keys, row, strict=True))
        for key in ("id", "candidate_id", "run_id", "invocation_id", "retry_parent_id"):
            if result.get(key) is not None:
                result[key] = str(result[key])
        for prefix in ("raw", "normalized"):
            sha = result.get(f"{prefix}_blob_sha256")
            result[f"{prefix}_blob"] = (
                None
                if not sha
                else {
                    "sha256": sha,
                    "uri": result.get(f"{prefix}_blob_uri", f"blob://sha256/{sha}"),
                    "byte_length": result.get(f"{prefix}_blob_byte_length", 0),
                    "mime_type": result.get(f"{prefix}_blob_mime_type"),
                }
            )
        metrics = result.get("quality_metrics")
        if isinstance(metrics, str):
            try:
                result["quality_metrics"] = json.loads(metrics)
            except (json.JSONDecodeError, TypeError):
                result["quality_metrics"] = None
        elif not isinstance(metrics, dict):
            result["quality_metrics"] = None
        return result


def install_candidate_policy_repository(candidate_policy_module: Any) -> None:
    """Route legacy policy-service ranking writes through the candidate repository."""

    service_type = candidate_policy_module.CandidatePolicyService
    if getattr(service_type, "_issue_258_repository_installed", False):
        return

    original_record_rankings = service_type.record_rankings

    @wraps(original_record_rankings)
    def record_rankings(self, run_id, search_response_id, invocation_id, rankings):
        try:
            with self.uow_factory() as uow:
                uow.candidates.record_rankings(
                    run_id, search_response_id, invocation_id, rankings
                )
        except CandidateRankingConflictError as exc:
            raise candidate_policy_module.CandidatePolicyError(str(exc)) from exc

    service_type.record_rankings = record_rankings
    service_type._issue_258_repository_installed = True


__all__ = [
    "CandidateRankingConflictError",
    "PostgresCandidateRepository",
    "PostgresExtractionAttemptRepository",
    "PostgresSearchAcquisitionRepository",
    "install_candidate_policy_repository",
]
