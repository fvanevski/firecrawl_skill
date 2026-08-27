"""Connection-bound PostgreSQL coverage persistence for issue #257."""

from __future__ import annotations

import json
from typing import Any

from .postgres_research import _lock_workflow_run


class PostgresCoverageRepository:
    """Coverage event/snapshot persistence on the exact containing UoW connection."""

    def __init__(self, connection: Any) -> None:
        self.__connection = connection

    def create_items(
        self,
        run_id,
        items,
        idempotency_key,
        source_event_id=None,
        source_invocation_id=None,
        execution_mode="deterministic_debug",
    ):
        with self.__connection.cursor() as cur:
            _lock_workflow_run(cur, run_id)
            item_ids = []
            for item in items:
                cur.execute(
                    """INSERT INTO coverage_events(
                        run_id, coverage_revision, prior_coverage_revision,
                        event_type, item_id, item_type, subject_id,
                        new_status, previous_status,
                        source_event_id, source_invocation_id,
                        payload, idempotency_key
                    ) VALUES(%s, 1, 0, 'item_created', gen_random_uuid(), %s, %s,
                        'unassessed', NULL, %s, %s, %s, %s)
                    ON CONFLICT(run_id, idempotency_key) DO NOTHING
                    RETURNING item_id""",
                    (
                        run_id,
                        item["item_type"],
                        item["subject_id"],
                        source_event_id,
                        source_invocation_id,
                        json.dumps(
                            {
                                "execution_mode": execution_mode,
                                "text": item.get("text", ""),
                            }
                        ),
                        idempotency_key,
                    ),
                )
                row = cur.fetchone()
                if row:
                    item_ids.append(row[0])
            if not item_ids:
                cur.execute(
                    """SELECT item_id FROM coverage_events
                    WHERE run_id=%s AND event_type='item_created'
                      AND item_type=ANY(%s)
                    ORDER BY created_at, id""",
                    (run_id, [item["item_type"] for item in items]),
                )
                item_ids = [row[0] for row in cur.fetchall()]
            cur.execute(
                "UPDATE research_runs SET current_coverage_revision = 1 WHERE id = %s AND current_coverage_revision < 1",
                (run_id,),
            )
            return item_ids

    def apply_event(
        self,
        run_id,
        event_type,
        item_id=None,
        item_type=None,
        subject_id=None,
        new_status=None,
        previous_status=None,
        new_freshness_status=None,
        previous_freshness_status=None,
        source_event_id=None,
        source_invocation_id=None,
        payload=None,
        idempotency_key=None,
    ):
        payload = payload or {}
        with self.__connection.cursor() as cur:
            _lock_workflow_run(cur, run_id)
            cur.execute(
                """SELECT id, coverage_revision, prior_coverage_revision,
                    event_type, item_id, item_type, subject_id,
                    new_status, previous_status,
                    new_freshness_status, previous_freshness_status,
                    source_event_id, source_invocation_id,
                    payload, idempotency_key, created_at
                FROM coverage_events
                WHERE run_id=%s AND idempotency_key=%s""",
                (run_id, idempotency_key),
            )
            existing = cur.fetchone()
            if existing:
                keys = (
                    "id",
                    "coverage_revision",
                    "prior_coverage_revision",
                    "event_type",
                    "item_id",
                    "item_type",
                    "subject_id",
                    "new_status",
                    "previous_status",
                    "new_freshness_status",
                    "previous_freshness_status",
                    "source_event_id",
                    "source_invocation_id",
                    "payload",
                    "idempotency_key",
                    "created_at",
                )
                return dict(zip(keys, existing))
            cur.execute(
                "SELECT current_coverage_revision FROM research_runs WHERE id=%s",
                (run_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise KeyError(run_id)
            current_revision = row[0]
            new_revision = current_revision + 1
            if new_revision <= current_revision:
                raise ValueError(
                    f"stale coverage revision: proposed {new_revision} "
                    f"does not exceed current {current_revision}"
                )
            if item_id is not None:
                cur.execute(
                    """SELECT 1 FROM coverage_events
                    WHERE run_id=%s AND item_id=%s AND event_type='item_created'
                    LIMIT 1""",
                    (run_id, item_id),
                )
                if not cur.fetchone():
                    raise ValueError(
                        f"unknown coverage item {item_id} for run {run_id}"
                    )
            cur.execute(
                """INSERT INTO coverage_events(
                    run_id, coverage_revision, prior_coverage_revision,
                    event_type, item_id, item_type, subject_id,
                    new_status, previous_status,
                    new_freshness_status, previous_freshness_status,
                    source_event_id, source_invocation_id,
                    payload, idempotency_key
                ) VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id, run_id, coverage_revision, prior_coverage_revision,
                    event_type, item_id, item_type, subject_id,
                    new_status, previous_status,
                    new_freshness_status, previous_freshness_status,
                    source_event_id, source_invocation_id,
                    payload, idempotency_key, created_at""",
                (
                    run_id,
                    new_revision,
                    current_revision,
                    event_type,
                    item_id,
                    item_type,
                    subject_id,
                    new_status,
                    previous_status,
                    new_freshness_status,
                    previous_freshness_status,
                    source_event_id,
                    source_invocation_id,
                    json.dumps(payload),
                    idempotency_key,
                ),
            )
            keys = (
                "id",
                "run_id",
                "coverage_revision",
                "prior_coverage_revision",
                "event_type",
                "item_id",
                "item_type",
                "subject_id",
                "new_status",
                "previous_status",
                "new_freshness_status",
                "previous_freshness_status",
                "source_event_id",
                "source_invocation_id",
                "payload",
                "idempotency_key",
                "created_at",
            )
            result = dict(zip(keys, cur.fetchone()))
            cur.execute(
                "UPDATE research_runs SET current_coverage_revision=%s WHERE id=%s",
                (new_revision, run_id),
            )
            return result

    def rebuild_projection(self, run_id, idempotency_key, source_event_id=None):
        with self.__connection.cursor() as cur:
            _lock_workflow_run(cur, run_id)
            cur.execute(
                """SELECT event_type, item_id, item_type, subject_id,
                    new_status, previous_status,
                    new_freshness_status, previous_freshness_status,
                    payload FROM coverage_events
                    WHERE run_id=%s ORDER BY coverage_revision, id""",
                (run_id,),
            )
            events = cur.fetchall()
            items: dict[str, dict[str, Any]] = {}
            for evt in events:
                (
                    event_type,
                    item_id,
                    item_type,
                    subject_id,
                    new_status,
                    _previous_status,
                    _new_freshness_status,
                    _previous_freshness_status,
                    payload,
                ) = evt
                if event_type == "item_created":
                    items[str(item_id)] = {
                        "coverage_item_id": str(item_id),
                        "item_type": item_type or "question",
                        "subject_id": subject_id or "",
                        "status": "unassessed",
                        "freshness_status": "not_applicable",
                        "candidate_ids": [],
                        "snapshot_ids": [],
                        "passage_ids": [],
                        "independent_source_count": 0,
                        "required_independent_source_count": 0,
                        "authority_classes_present": [],
                        "remaining_gap": (payload or {}).get("text", ""),
                        "confidence": 0.0,
                    }
                elif event_type == "item_status_changed" and item_id:
                    key = str(item_id)
                    if key in items:
                        items[key]["status"] = new_status or items[key]["status"]
                        items[key]["confidence"] = (payload or {}).get(
                            "confidence", items[key].get("confidence", 0.0)
                        )
                        items[key]["remaining_gap"] = (payload or {}).get(
                            "remaining_gap", items[key].get("remaining_gap", "")
                        )
                        if "candidate_ids" in (payload or {}):
                            items[key]["candidate_ids"] = [
                                str(value) for value in payload["candidate_ids"]
                            ]
                        if "snapshot_ids" in (payload or {}):
                            items[key]["snapshot_ids"] = [
                                str(value) for value in payload["snapshot_ids"]
                            ]
                        if "passage_ids" in (payload or {}):
                            items[key]["passage_ids"] = [
                                str(value) for value in payload["passage_ids"]
                            ]
                        if "independent_source_count" in (payload or {}):
                            items[key]["independent_source_count"] = payload[
                                "independent_source_count"
                            ]
                        if "authority_classes_present" in (payload or {}):
                            items[key]["authority_classes_present"] = payload[
                                "authority_classes_present"
                            ]
                    else:
                        items[key] = {
                            "coverage_item_id": str(item_id),
                            "item_type": item_type or "question",
                            "subject_id": subject_id or "",
                            "status": new_status or "unassessed",
                            "freshness_status": "not_applicable",
                            "candidate_ids": [],
                            "snapshot_ids": [],
                            "passage_ids": [],
                            "independent_source_count": 0,
                            "required_independent_source_count": 0,
                            "authority_classes_present": [],
                            "remaining_gap": "",
                            "confidence": 0.0,
                        }
                elif event_type == "item_gap_identified" and item_id:
                    key = str(item_id)
                    if key in items:
                        items[key]["status"] = "blocked"
                elif event_type == "item_gap_resolved" and item_id:
                    key = str(item_id)
                    if key in items:
                        items[key]["status"] = "satisfied"
                elif event_type == "candidate_identified" and item_id:
                    key = str(item_id)
                    if key in items:
                        candidate_id = (payload or {}).get("candidate_id")
                        if candidate_id:
                            candidate = str(candidate_id)
                            if candidate not in items[key]["candidate_ids"]:
                                items[key]["candidate_ids"].append(candidate)
                elif event_type == "extraction_attempted" and item_id:
                    key = str(item_id)
                    if key in items:
                        source_url = (payload or {}).get("source_url")
                        if source_url:
                            items[key].setdefault("_source_urls", []).append(
                                str(source_url)
                            )
                elif event_type == "asset_acquired" and item_id:
                    key = str(item_id)
                    if key in items:
                        items[key]["status"] = "acquired"
                        source_url = (payload or {}).get("source_url")
                        if source_url:
                            items[key].setdefault("_source_urls", []).append(
                                str(source_url)
                            )
                        if "independent_source_count" in (payload or {}):
                            items[key]["independent_source_count"] = payload[
                                "independent_source_count"
                            ]
                elif event_type == "evidence_retrieved" and item_id:
                    key = str(item_id)
                    if key in items:
                        for passage_id in (payload or {}).get("passage_ids", []):
                            passage = str(passage_id)
                            if passage not in items[key]["passage_ids"]:
                                items[key]["passage_ids"].append(passage)
                elif event_type == "source_class_observed" and item_id:
                    key = str(item_id)
                    if key in items:
                        authority_class = (payload or {}).get("authority_class")
                        if (
                            authority_class
                            and authority_class
                            not in items[key]["authority_classes_present"]
                        ):
                            items[key]["authority_classes_present"].append(
                                authority_class
                            )
                elif event_type == "freshness_observed" and item_id:
                    key = str(item_id)
                    if key in items:
                        freshness = (payload or {}).get("freshness_status")
                        if freshness:
                            items[key]["freshness_status"] = freshness

            for item in items.values():
                source_urls = item.pop("_source_urls", [])
                if source_urls:
                    item["independent_source_count"] = len(set(source_urls))

            if not items:
                overall_status = "unassessed"
            else:
                satisfied = sum(
                    1
                    for item in items.values()
                    if item["status"] in ("satisfied", "waived")
                )
                blocked = sum(
                    1 for item in items.values() if item["status"] == "blocked"
                )
                total = len(items)
                if satisfied == total:
                    overall_status = "sufficient"
                elif blocked > 0:
                    overall_status = "blocked"
                elif satisfied > 0:
                    overall_status = "partial"
                else:
                    overall_status = "insufficient"

            cur.execute(
                "SELECT COALESCE(MAX(coverage_revision), 0) FROM coverage_events WHERE run_id=%s",
                (run_id,),
            )
            current_revision = cur.fetchone()[0]
            if current_revision == 0:
                current_revision = 1
            ledger = {
                "schema_version": "coverage-ledger-v1",
                "run_id": str(run_id),
                "revision": current_revision,
                "items": list(items.values()),
                "overall_status": overall_status,
            }
            cur.execute(
                """INSERT INTO coverage_events(
                    run_id, coverage_revision, prior_coverage_revision,
                    event_type, source_event_id, payload, idempotency_key
                ) VALUES(%s, %s, %s, 'projection_rebuilt', %s, %s, %s)
                ON CONFLICT(run_id, idempotency_key) DO NOTHING
                RETURNING id""",
                (
                    run_id,
                    current_revision + 1,
                    current_revision,
                    source_event_id,
                    json.dumps(
                        {
                            "item_count": len(items),
                            "overall_status": overall_status,
                            "source_event_id": str(source_event_id)
                            if source_event_id
                            else None,
                            "coverage_revision": current_revision,
                        }
                    ),
                    idempotency_key,
                ),
            )
            return ledger

    def create_snapshot(
        self,
        run_id,
        coverage_revision,
        ledger,
        content_sha256,
        idempotency_key,
        triggering_event_id=None,
    ):
        del idempotency_key
        with self.__connection.cursor() as cur:
            _lock_workflow_run(cur, run_id)
            cur.execute(
                """SELECT id, run_id, coverage_revision, ledger,
                    content_sha256, triggering_event_id, created_at
                FROM coverage_snapshots
                WHERE run_id=%s AND coverage_revision=%s""",
                (run_id, coverage_revision),
            )
            existing = cur.fetchone()
            keys = (
                "id",
                "run_id",
                "coverage_revision",
                "ledger",
                "content_sha256",
                "triggering_event_id",
                "created_at",
            )
            if existing:
                return dict(zip(keys, existing))
            cur.execute(
                """INSERT INTO coverage_snapshots(
                    run_id, coverage_revision, ledger,
                    content_sha256, triggering_event_id
                ) VALUES(%s, %s, %s, %s, %s)
                RETURNING id, run_id, coverage_revision, ledger,
                    content_sha256, triggering_event_id, created_at""",
                (
                    run_id,
                    coverage_revision,
                    json.dumps(ledger) if isinstance(ledger, dict) else ledger,
                    content_sha256,
                    triggering_event_id,
                ),
            )
            result = dict(zip(keys, cur.fetchone()))
            cur.execute(
                "UPDATE research_runs SET current_coverage_revision=%s WHERE id=%s",
                (coverage_revision, run_id),
            )
            return result

    def get_snapshot(self, run_id, coverage_revision):
        with self.__connection.cursor() as cur:
            cur.execute(
                """SELECT id, run_id, coverage_revision, ledger,
                    content_sha256, triggering_event_id, created_at
                FROM coverage_snapshots
                WHERE run_id=%s AND coverage_revision=%s""",
                (run_id, coverage_revision),
            )
            row = cur.fetchone()
        if row is None:
            return None
        keys = (
            "id",
            "run_id",
            "coverage_revision",
            "ledger",
            "content_sha256",
            "triggering_event_id",
            "created_at",
        )
        return dict(zip(keys, row))

    def get_latest_snapshot(self, run_id):
        with self.__connection.cursor() as cur:
            cur.execute(
                """SELECT id, run_id, coverage_revision, ledger,
                    content_sha256, triggering_event_id, created_at
                FROM coverage_snapshots WHERE run_id=%s
                ORDER BY coverage_revision DESC LIMIT 1""",
                (run_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        keys = (
            "id",
            "run_id",
            "coverage_revision",
            "ledger",
            "content_sha256",
            "triggering_event_id",
            "created_at",
        )
        return dict(zip(keys, row))

    @staticmethod
    def _coverage_event_mapping(row):
        keys = (
            "id",
            "run_id",
            "coverage_revision",
            "prior_coverage_revision",
            "event_type",
            "item_id",
            "item_type",
            "subject_id",
            "new_status",
            "previous_status",
            "new_freshness_status",
            "previous_freshness_status",
            "source_event_id",
            "source_invocation_id",
            "payload",
            "idempotency_key",
            "created_at",
        )
        return dict(zip(keys, row))

    def list_coverage_events(
        self,
        run_id,
        item_id=None,
        event_type=None,
        limit=100,
        offset=0,
    ):
        conditions = ["run_id = %s"]
        params = [run_id]
        if item_id is not None:
            conditions.append("item_id = %s")
            params.append(item_id)
        if event_type is not None:
            conditions.append("event_type = %s")
            params.append(event_type)
        where = " AND ".join(conditions)
        with self.__connection.cursor() as cur:
            cur.execute(
                f"""SELECT id, run_id, coverage_revision, prior_coverage_revision,
                    event_type, item_id, item_type, subject_id,
                    new_status, previous_status,
                    new_freshness_status, previous_freshness_status,
                    source_event_id, source_invocation_id,
                    payload, idempotency_key, created_at
                FROM coverage_events WHERE {where}
                ORDER BY coverage_revision, id LIMIT %s OFFSET %s""",
                (*params, limit, offset),
            )
            return [self._coverage_event_mapping(row) for row in cur.fetchall()]

    def get_event(self, run_id, event_id):
        with self.__connection.cursor() as cur:
            cur.execute(
                """SELECT id, run_id, coverage_revision, prior_coverage_revision,
                    event_type, item_id, item_type, subject_id,
                    new_status, previous_status,
                    new_freshness_status, previous_freshness_status,
                    source_event_id, source_invocation_id,
                    payload, idempotency_key, created_at
                FROM coverage_events WHERE run_id=%s AND id=%s""",
                (run_id, event_id),
            )
            row = cur.fetchone()
        return None if row is None else self._coverage_event_mapping(row)

    def get_current_revision(self, run_id):
        with self.__connection.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(current_coverage_revision, 0) FROM research_runs WHERE id=%s",
                (run_id,),
            )
            row = cur.fetchone()
        return 0 if row is None else row[0]

    def count_events(self, run_id):
        with self.__connection.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM coverage_events WHERE run_id=%s", (run_id,)
            )
            return cur.fetchone()[0]

    def count_coverage_items(self, run_id):
        with self.__connection.cursor() as cur:
            cur.execute(
                """SELECT COUNT(*) FROM coverage_events
                WHERE run_id=%s AND event_type='item_created'""",
                (run_id,),
            )
            return cur.fetchone()[0]

    def get_coverage_summary(self, run_id):
        """Return a summary derived from the latest authoritative ledger snapshot."""
        with self.__connection.cursor() as cur:
            cur.execute(
                """SELECT coverage_revision, ledger
                FROM coverage_snapshots WHERE run_id=%s
                ORDER BY coverage_revision DESC LIMIT 1""",
                (str(run_id),),
            )
            row = cur.fetchone()
        if row is None:
            return None

        coverage_revision, ledger = row
        if not isinstance(ledger, dict):
            raise ValueError("coverage snapshot ledger must be a JSON object")
        ledger_run_id = ledger.get("run_id")
        if ledger_run_id is not None and str(ledger_run_id) != str(run_id):
            raise ValueError("coverage snapshot ledger run_id does not match requested run")

        items = ledger.get("items", [])
        if not isinstance(items, list):
            raise ValueError("coverage snapshot ledger items must be a list")

        status_counts: dict[str, int] = {}
        type_counts: dict[str, int] = {}
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("coverage snapshot ledger items must be JSON objects")
            status = item.get("status")
            item_type = item.get("item_type")
            if not isinstance(status, str) or not status:
                raise ValueError("coverage snapshot item status must be a non-empty string")
            if not isinstance(item_type, str) or not item_type:
                raise ValueError("coverage snapshot item_type must be a non-empty string")
            status_counts[status] = status_counts.get(status, 0) + 1
            type_counts[item_type] = type_counts.get(item_type, 0) + 1

        overall_status = ledger.get("overall_status")
        if not isinstance(overall_status, str) or not overall_status:
            raise ValueError(
                "coverage snapshot overall_status must be a non-empty string"
            )

        schema_version = ledger.get("schema_version", "coverage-ledger-v1")
        if not isinstance(schema_version, str) or not schema_version:
            raise ValueError("coverage snapshot schema_version must be a non-empty string")

        return {
            "schema_version": schema_version,
            "run_id": str(run_id),
            "coverage_revision": int(coverage_revision),
            "total_items": len(items),
            "status_counts": dict(sorted(status_counts.items())),
            "type_counts": dict(sorted(type_counts.items())),
            "overall_status": overall_status,
        }
