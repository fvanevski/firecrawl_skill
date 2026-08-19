"""Connection-bound PostgreSQL retrieval provenance persistence."""

from __future__ import annotations

import json
from typing import Any


class PostgresRetrievalRepository:
    """Canonical retrieval execution/event persistence."""

    def __init__(self, connection: Any) -> None:
        self.__connection = connection

    def log_retrieval(self, run_id, event):
        fields = (
            "stage",
            "query",
            "filters",
            "retriever",
            "candidate_type",
            "candidate_id",
            "raw_score",
            "normalized_score",
            "rank",
            "reranker_score",
            "selected",
            "rejection_reason",
        )
        values = [
            json.dumps(event.get(field)) if field == "filters" else event.get(field)
            for field in fields
        ]
        with self.__connection.cursor() as cur:
            cur.execute(
                f"""INSERT INTO retrieval_events(run_id,{",".join(fields)})
                SELECT id,{",".join(["%s"] * len(fields))}
                FROM research_runs WHERE id=%s
                AND state NOT IN ('completed','partial','failed','cancelled')""",
                [*values, run_id],
            )
            if cur.rowcount != 1:
                raise KeyError(f"research run is absent or finished: {run_id}")

    def log_retrieval_batch(self, execution_id, run_id, events):
        if not events:
            return
        fields = (
            "retrieval_execution_id",
            "stage",
            "query",
            "filters",
            "retriever",
            "candidate_type",
            "candidate_id",
            "raw_score",
            "normalized_score",
            "rank",
            "reranker_score",
            "selected",
            "rejection_reason",
        )
        column_list = ",".join(fields)
        field_placeholders = ",".join(["%s"] * (len(fields) - 1))
        rows = []
        for event in events:
            row_values = (
                event.get("stage"),
                event.get("query"),
                json.dumps(event.get("filters"))
                if event.get("filters") is not None
                else None,
                event.get("retriever"),
                event.get("candidate_type"),
                event.get("candidate_id"),
                event.get("raw_score"),
                event.get("normalized_score"),
                event.get("rank"),
                event.get("reranker_score"),
                bool(event.get("selected")),
                event.get("rejection_reason"),
            )
            rows.append((execution_id, *row_values, run_id))
        with self.__connection.cursor() as cur:
            cur.executemany(
                f"""INSERT INTO retrieval_events(run_id, {column_list})
                SELECT research_runs.id, %s, {field_placeholders}
                FROM research_runs WHERE id=%s
                AND state NOT IN ('completed','partial','failed','cancelled')""",
                rows,
            )
            if cur.rowcount != len(events):
                raise KeyError(
                    f"research run is absent or finished: {run_id} "
                    f"(expected {len(events)} rows, got {cur.rowcount})"
                )

    def get_trace(self, execution_id):
        fields = (
            "stage",
            "query",
            "filters",
            "retriever",
            "candidate_type",
            "candidate_id",
            "raw_score",
            "normalized_score",
            "rank",
            "reranker_score",
            "selected",
            "rejection_reason",
        )
        with self.__connection.cursor() as cur:
            cur.execute(
                f"""SELECT {",".join(fields)} FROM retrieval_events
                WHERE retrieval_execution_id=%s
                ORDER BY created_at,
                    CASE stage
                        WHEN 'lexical' THEN 1
                        WHEN 'semantic' THEN 2
                        WHEN 'fused' THEN 3
                        WHEN 'reranked' THEN 4
                        ELSE 5
                    END,
                    rank""",
                (execution_id,),
            )
            result = [dict(zip(fields, row)) for row in cur.fetchall()]
        # Issue #217 made the explicit retrieval-selection name canonical while
        # retaining the legacy ``selected`` field for compatibility.
        for event in result:
            event["selected_for_retrieval"] = bool(event.get("selected"))
        return result

    def record_retrieval_execution(self, run_id, execution):
        with self.__connection.cursor() as cur:
            cur.execute(
                """INSERT INTO retrieval_executions(
                id, run_id, requested_mode, executed_mode, mechanical_status,
                component_health, errors, warnings, stage_counts,
                index_fingerprint, filters, skipped_stages, timing, config_identity
                ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    execution.execution_id,
                    run_id,
                    execution.requested_mode,
                    execution.executed_mode,
                    execution.mechanical_status.value
                    if hasattr(execution.mechanical_status, "value")
                    else execution.mechanical_status,
                    json.dumps(execution.component_health),
                    json.dumps(execution.errors),
                    json.dumps(execution.warnings),
                    json.dumps(execution.stage_counts),
                    execution.index_fingerprint,
                    json.dumps(execution.filters),
                    json.dumps(execution.skipped_stages),
                    json.dumps(execution.timing),
                    execution.config_identity,
                ),
            )
