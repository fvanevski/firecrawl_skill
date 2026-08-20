"""Connection-bound PostgreSQL claim/evidence persistence for issue #259."""

from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID


class PostgresClaimEvidenceRepository:
    """Canonical research-claim and claim-to-passage evidence persistence."""

    def __init__(self, connection: Any) -> None:
        self.__connection = connection

    def upsert_claim(
        self,
        run_id: UUID,
        claim_id: UUID,
        statement: str,
        semantic_status: str = "unassessed",
        uncertainty: str | None = None,
        evidence_packet_revision: int = 1,
    ) -> UUID:
        if not statement.strip():
            raise ValueError("claim statement must be non-empty")
        with self.__connection.cursor() as cur:
            cur.execute(
                """INSERT INTO research_claims (run_id, claim_id, statement,
                    semantic_status, uncertainty, evidence_packet_revision)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (run_id, claim_id) DO UPDATE SET
                    statement=excluded.statement,
                    semantic_status=excluded.semantic_status,
                    uncertainty=excluded.uncertainty,
                    evidence_packet_revision=excluded.evidence_packet_revision
                RETURNING id""",
                (
                    run_id,
                    claim_id,
                    statement,
                    semantic_status,
                    uncertainty,
                    evidence_packet_revision,
                ),
            )
            return cur.fetchone()[0]

    def list_claims(self, run_id: UUID) -> list[dict[str, Any]]:
        with self.__connection.cursor() as cur:
            cur.execute(
                """SELECT id, run_id, claim_id, statement, semantic_status,
                    uncertainty, evidence_packet_revision, created_at
                    FROM research_claims WHERE run_id=%s ORDER BY created_at""",
                (run_id,),
            )
            keys = (
                "id",
                "run_id",
                "claim_id",
                "statement",
                "semantic_status",
                "uncertainty",
                "evidence_packet_revision",
                "created_at",
            )
            results = []
            for row in cur.fetchall():
                item = dict(zip(keys, row))
                for key in ("id", "run_id", "claim_id"):
                    if item.get(key) is not None:
                        item[key] = str(item[key])
                results.append(item)
            return results

    def delete_claims(self, run_id: UUID) -> int:
        with self.__connection.cursor() as cur:
            cur.execute("DELETE FROM research_claims WHERE run_id=%s", (run_id,))
            return cur.rowcount

    def validate_passage_id(self, passage_id: UUID) -> bool:
        with self.__connection.cursor() as cur:
            cur.execute("SELECT 1 FROM chunks WHERE id=%s LIMIT 1", (passage_id,))
            return cur.fetchone() is not None

    def validate_snapshot_id(self, snapshot_id: UUID) -> bool:
        with self.__connection.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM asset_snapshots WHERE id=%s LIMIT 1", (snapshot_id,)
            )
            return cur.fetchone() is not None

    def validate_claim_id(self, claim_id: UUID) -> bool:
        with self.__connection.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM research_claims WHERE claim_id=%s LIMIT 1", (claim_id,)
            )
            return cur.fetchone() is not None

    def insert_evidence_link(
        self,
        run_id: UUID,
        claim_id: UUID,
        passage_id: UUID,
        snapshot_id: UUID,
        source_url: str = "",
        relationship: str = "supports",
        confidence: float = 1.0,
    ) -> UUID:
        if not self.validate_claim_id(claim_id):
            raise ValueError(f"unknown claim ID: {claim_id}")
        if not self.validate_passage_id(passage_id):
            raise ValueError(f"unknown passage ID: {passage_id}")
        if not self.validate_snapshot_id(snapshot_id):
            raise ValueError(f"unknown snapshot ID: {snapshot_id}")
        with self.__connection.cursor() as cur:
            cur.execute(
                """INSERT INTO claim_evidence_links (run_id, claim_id, passage_id,
                    snapshot_id, source_url, relationship, confidence)
                VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                (
                    run_id,
                    claim_id,
                    passage_id,
                    snapshot_id,
                    source_url,
                    relationship,
                    confidence,
                ),
            )
            return cur.fetchone()[0]

    def list_evidence_links(self, run_id: UUID) -> list[dict[str, Any]]:
        with self.__connection.cursor() as cur:
            cur.execute(
                """SELECT id, run_id, claim_id, passage_id, snapshot_id,
                    source_url, relationship, confidence, created_at
                    FROM claim_evidence_links
                    WHERE run_id=%s ORDER BY created_at""",
                (run_id,),
            )
            keys = (
                "id",
                "run_id",
                "claim_id",
                "passage_id",
                "snapshot_id",
                "source_url",
                "relationship",
                "confidence",
                "created_at",
            )
            results = []
            for row in cur.fetchall():
                item = dict(zip(keys, row))
                for key in (
                    "id",
                    "run_id",
                    "claim_id",
                    "passage_id",
                    "snapshot_id",
                ):
                    if item.get(key) is not None:
                        item[key] = str(item[key])
                results.append(item)
            return results

    def delete_evidence_links(self, run_id: UUID) -> int:
        with self.__connection.cursor() as cur:
            cur.execute("DELETE FROM claim_evidence_links WHERE run_id=%s", (run_id,))
            return cur.rowcount

    def export_claim_manifest(self, run_id: UUID) -> dict[str, Any]:
        with self.__connection.cursor() as cur:
            cur.execute(
                """SELECT id, run_id, claim_id, statement, semantic_status,
                    uncertainty, evidence_packet_revision, created_at
                    FROM research_claims WHERE run_id=%s ORDER BY created_at""",
                (run_id,),
            )
            claim_keys = (
                "id",
                "run_id",
                "claim_id",
                "statement",
                "semantic_status",
                "uncertainty",
                "evidence_packet_revision",
                "created_at",
            )
            claims = [dict(zip(claim_keys, row)) for row in cur.fetchall()]
            cur.execute(
                """SELECT id, run_id, claim_id, passage_id, snapshot_id,
                    source_url, relationship, confidence, created_at
                    FROM claim_evidence_links
                    WHERE run_id=%s ORDER BY created_at""",
                (run_id,),
            )
            link_keys = (
                "id",
                "run_id",
                "claim_id",
                "passage_id",
                "snapshot_id",
                "source_url",
                "relationship",
                "confidence",
                "created_at",
            )
            links = [dict(zip(link_keys, row)) for row in cur.fetchall()]

        def _json_serial(obj):
            if hasattr(obj, "isoformat"):
                return obj.isoformat()
            if hasattr(obj, "hex"):
                return str(obj)
            raise TypeError(f"Object of type {type(obj)} is not JSON serializable")

        state = json.dumps(
            {"claims": claims, "links": links}, sort_keys=True, default=_json_serial
        )
        source_hash = hashlib.sha256(state.encode()).hexdigest()

        def _stringify_row(row: dict) -> dict:
            return {
                key: _json_serial(value)
                if not isinstance(value, (str, int, float, bool, type(None)))
                else value
                for key, value in row.items()
            }

        return {
            "manifest_version": "claim-manifest-v1",
            "run_id": str(run_id),
            "source_state_hash": source_hash,
            "claim_count": len(claims),
            "link_count": len(links),
            "claims": [_stringify_row(claim) for claim in claims],
            "links": [_stringify_row(link) for link in links],
        }


class PostgresEvidencePacketRepository:
    """Canonical revisioned evidence-packet persistence."""

    def __init__(self, connection: Any) -> None:
        self.__connection = connection

    def persist_evidence_packet(
        self,
        run_id: UUID,
        research_spec_id: UUID,
        coverage_revision: int,
        packet_revision: int,
        payload: dict[str, Any],
    ) -> UUID:
        with self.__connection.cursor() as cur:
            cur.execute(
                """INSERT INTO evidence_packets (
                    run_id, research_spec_id, coverage_revision,
                    packet_revision, payload
                ) VALUES (%s, %s, %s, %s, %s) RETURNING id""",
                (
                    run_id,
                    research_spec_id,
                    coverage_revision,
                    packet_revision,
                    json.dumps(payload),
                ),
            )
            return cur.fetchone()[0]

    def get_evidence_packet(self, run_id: UUID, packet_revision: int | None = None):
        with self.__connection.cursor() as cur:
            if packet_revision is not None:
                cur.execute(
                    """SELECT id, run_id, research_spec_id, coverage_revision,
                        packet_revision, payload, created_at FROM evidence_packets
                        WHERE run_id=%s AND packet_revision=%s""",
                    (run_id, packet_revision),
                )
            else:
                cur.execute(
                    """SELECT id, run_id, research_spec_id, coverage_revision,
                        packet_revision, payload, created_at FROM evidence_packets
                        WHERE run_id=%s ORDER BY packet_revision DESC LIMIT 1""",
                    (run_id,),
                )
            row = cur.fetchone()
        if not row:
            return None
        from .domain import EvidencePacketRecord

        keys = (
            "id",
            "run_id",
            "research_spec_id",
            "coverage_revision",
            "packet_revision",
            "payload",
            "created_at",
        )
        data = dict(zip(keys, row))
        data["id"] = str(data["id"])
        data["run_id"] = str(data["run_id"])
        data["research_spec_id"] = str(data["research_spec_id"])
        return EvidencePacketRecord.from_mapping(data)
