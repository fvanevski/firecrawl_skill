"""Read-only operator projection for sealed indexing checkpoint membership."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from .index_checkpoint_models import IndexCheckpoint, IndexCheckpointError, IndexFinalization

_MAX_DOCUMENT_CENSUS_ITEMS = 100


class IndexCheckpointObservabilityMixin:
    """Reconcile sealed chunk membership to its owning documents for presentation."""

    uow_factory: Any

    def describe_checkpoint(self, checkpoint: IndexCheckpoint) -> dict[str, Any]:
        entity_ids = tuple(checkpoint.entity_ids)
        with self.uow_factory() as uow, uow.connection.cursor() as cursor:
            cursor.execute(
                """SELECT chunk.document_id,count(*)::int,
                          array_agg(chunk.id ORDER BY chunk.ordinal,chunk.id)
                     FROM chunks chunk
                    WHERE chunk.id=ANY(%s)
                    GROUP BY chunk.document_id
                    ORDER BY chunk.document_id""",
                (list(entity_ids),),
            )
            rows = cursor.fetchall()

        observed_ids = tuple(
            UUID(str(chunk_id))
            for _document_id, _count, chunk_ids in rows
            for chunk_id in (chunk_ids or ())
        )
        if len(observed_ids) != checkpoint.expected_count:
            raise IndexCheckpointError(
                "sealed checkpoint document census does not conserve chunk count: "
                f"expected={checkpoint.expected_count} observed={len(observed_ids)}"
            )
        if set(observed_ids) != set(entity_ids):
            raise IndexCheckpointError(
                "sealed checkpoint document census does not match exact chunk membership"
            )

        document_counts = [
            {"document_id": str(document_id), "chunk_count": int(chunk_count)}
            for document_id, chunk_count, _chunk_ids in rows
        ]
        mapped_chunk_count = sum(item["chunk_count"] for item in document_counts)
        if mapped_chunk_count != checkpoint.expected_count:
            raise IndexCheckpointError(
                "sealed checkpoint document census failed chunk conservation"
            )
        selected = document_counts[:_MAX_DOCUMENT_CENSUS_ITEMS]
        payload = checkpoint.to_dict()
        payload.update(
            {
                "document_count": len(document_counts),
                "mapped_chunk_count": mapped_chunk_count,
                "chunk_count_conserved": True,
                "per_document_chunk_counts": {
                    "items": selected,
                    "total_count": len(document_counts),
                    "returned_count": len(selected),
                    "truncated": len(document_counts) > len(selected),
                },
            }
        )
        return payload

    def describe_finalization(self, finalization: IndexFinalization) -> dict[str, Any]:
        payload = finalization.to_dict()
        payload["checkpoint"] = self.describe_checkpoint(finalization.checkpoint)
        return payload


__all__ = ["IndexCheckpointObservabilityMixin"]
