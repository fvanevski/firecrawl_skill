from __future__ import annotations

from pathlib import Path
from uuid import UUID

from .blob import ContentAddressedBlobStore
from .derivation_service import DerivationService
from .domain import IngestRequest
from .export_serialization import export_json
from .normalization import NORMALIZATION_VERSION, NormalizationService
from .parsing.interfaces import TypedBlock
from .store_runtime import database, uow_factory


def _service(config, build_service):
    return DerivationService(
        uow_factory=uow_factory(config),
        corpus_service=build_service(config),
        blob_root=config.blob_root,
    )


def rederive(config, args, build_service) -> dict:
    service = build_service(config)
    with database(config) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT a.id,a.requested_url,a.final_url,a.retrieved_at,a.http_status,
            a.etag,a.last_modified,a.mime_type,a.content_sha256,a.firecrawl_version,
            a.crawl_options,d.title,d.published_at,d.metadata
            FROM asset_snapshots a LEFT JOIN LATERAL(
              SELECT title,published_at,metadata FROM documents
              WHERE snapshot_id=a.id ORDER BY id DESC LIMIT 1
            ) d ON true WHERE (%s::uuid IS NULL OR a.id=%s::uuid)
            ORDER BY a.retrieved_at,a.id""",
            (args.snapshot, args.snapshot),
        )
        snapshots = cur.fetchall()
    store = ContentAddressedBlobStore(config.blob_root)
    results = []
    for row in snapshots:
        with store.open(row[8]) as handle:
            content = handle.read()
        result = service.ingest(
            IngestRequest(
                requested_url=row[1],
                final_url=row[2],
                retrieved_at=row[3],
                http_status=row[4],
                etag=row[5],
                last_modified=row[6],
                mime_type=row[7] or "text/markdown",
                content=content,
                firecrawl_version=row[9],
                crawl_options=row[10] or {},
                title=row[11],
                published_at=row[12],
                metadata=row[13] or {},
            )
        )
        results.append(result.__dict__)
    return {"rederived": len(results), "assets": results}


def rederive_v2(config, args, build_service) -> dict:
    config.require_database()
    derivation_service = _service(config, build_service)
    result = derivation_service.rederive(
        snapshot_id=UUID(args.snapshot) if args.snapshot else None,
        document_id=UUID(args.document) if args.document else None,
        parser_version=args.parser_version,
        normalization_version=args.normalization_version,
        chunker_name=args.chunker_name,
        chunker_version=args.chunker_version,
        tokenizer_name=args.tokenizer_name,
        dry_run=args.dry_run,
    )
    if args.activate and result.get("total_rederived", 0) > 0:
        last_result = result["results"][-1]
        derivation_id = last_result.get("derivation_id")
        if derivation_id:
            try:
                activated = derivation_service.activate_derivation(UUID(derivation_id))
                result["activated"] = str(activated.id)
            except ValueError as exc:
                result["activate_error"] = str(exc)
        else:
            result["activate_error"] = "no derivation_id in last rederive result"
    if args.report:
        export_json(Path(args.report), result)
    return result


def list_derivations(config, args, build_service) -> dict:
    config.require_database()
    derivations = _service(config, build_service).list_derivations(
        document_id=UUID(args.document) if args.document else None,
        snapshot_id=UUID(args.snapshot) if args.snapshot else None,
        status=args.status,
    )
    return {"derivations": len(derivations), "items": derivations}


def activate_derivation(config, args, build_service) -> tuple[list[dict], int]:
    config.require_database()
    service = _service(config, build_service)
    output: list[dict] = []
    try:
        derivation = service.get_derivation(UUID(args.id))
        if derivation is None:
            return [{"error": f"derivation {args.id} not found"}], 1
        output.append(
            {
                "confirming": {
                    "derivation_id": str(derivation.id),
                    "parser_version": derivation.parser_version,
                    "normalization_version": derivation.normalization_version,
                    "chunker_name": derivation.chunker_name,
                    "chunker_version": derivation.chunker_version,
                    "tokenizer_name": derivation.tokenizer_name,
                    "status": derivation.status,
                    "chunk_count": derivation.chunk_count,
                    "block_count": derivation.block_count,
                }
            }
        )
        if args.document and str(derivation.document_id) != str(UUID(args.document)):
            output.append(
                {
                    "error": (
                        f"derivation {args.id} belongs to document "
                        f"{derivation.document_id}, not {args.document}"
                    )
                }
            )
            return output, 1
        activated = service.activate_derivation(UUID(args.id))
        output.append({"activated": activated.to_dict()})
        return output, 0
    except ValueError as exc:
        output.append({"error": str(exc)})
        return output, 1


def compare_derivations(config, args, build_service) -> tuple[dict, int]:
    config.require_database()
    service = _service(config, build_service)
    try:
        report = service.compare_derivations(UUID(args.old_id), UUID(args.new_id))
        output = report.to_dict()
        if args.output != "-":
            export_json(Path(args.output), output)
        return output, 0
    except ValueError as exc:
        return {"error": str(exc)}, 1


def normalize(config, args, *, database_fn=database) -> dict:
    config.require_database()
    with database_fn(config) as conn:
        return _normalize_with_connection(conn, args)


def _normalize_with_connection(conn, args) -> dict:
    """Normalize and persist documents within one caller-owned transaction."""
    with conn.cursor() as cur:
        if args.all:
            cur.execute(
                """SELECT d.id, d.title, snap.requested_url, d.document_sha256,
                   db.id AS block_id, db.ordinal, db.block_type,
                   db.char_start, db.char_end, db.text, db.parser_version
                   FROM documents d
                   JOIN asset_snapshots snap ON snap.id = d.snapshot_id
                   JOIN document_blocks db ON db.document_id = d.id
                   ORDER BY d.id, db.ordinal"""
            )
            rows = cur.fetchall()
        elif args.document:
            doc_uuid = UUID(args.document)
            cur.execute(
                """SELECT d.id, d.title, snap.requested_url, d.document_sha256,
                   db.id AS block_id, db.ordinal, db.block_type,
                   db.char_start, db.char_end, db.text, db.parser_version
                   FROM documents d
                   JOIN asset_snapshots snap ON snap.id = d.snapshot_id
                   JOIN document_blocks db ON db.document_id = d.id
                   WHERE d.id = %s ORDER BY db.ordinal""",
                (doc_uuid,),
            )
            rows = cur.fetchall()
        else:
            return {"error": "specify --document <uuid> or --all"}

    if not rows:
        return {"message": "no blocks found", "normalized": 0}

    docs: dict[tuple, list] = {}
    for row in rows:
        docs.setdefault((str(row[0]), row[1], row[2], row[3]), []).append(row)

    service = NormalizationService(
        aggressive=args.aggressive,
        document_type=args.document_type,
    )
    results = []
    upserted_blocks = 0
    upserted_transforms = 0

    for doc_key, doc_rows in docs.items():
        doc_id = UUID(doc_key[0])
        block_ids = [UUID(str(row[4])) for row in doc_rows]
        typed_blocks = [
            TypedBlock(
                ordinal=int(row[5]),
                block_type=row[6],
                text=row[9] or "",
                heading_path=(),
                char_start=int(row[7]) if row[7] is not None else None,
                char_end=int(row[8]) if row[8] is not None else None,
                parser_version=row[10] or "canonical-v1",
            )
            for row in doc_rows
        ]
        norm_result = service.normalize(
            blocks=typed_blocks,
            source_block_ids=block_ids,
            document_id=doc_id,
            document_type=args.document_type,
        )

        persisted_block_ids: dict[UUID, UUID] = {}
        with conn.cursor() as block_cur:
            for nb in (
                norm_result.blocks
                + norm_result.suppressed_blocks
                + norm_result.removed_blocks
            ):
                block_cur.execute(
                    """INSERT INTO normalized_blocks
                       (id, source_block_id, document_id, ordinal, block_type,
                        text, heading_path, char_start, char_end, disposition,
                        rule_version, transformation_reason, parser_version)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (source_block_id, rule_version) DO UPDATE SET
                         disposition = EXCLUDED.disposition,
                         transformation_reason = EXCLUDED.transformation_reason,
                         text = EXCLUDED.text,
                         char_start = EXCLUDED.char_start,
                         char_end = EXCLUDED.char_end,
                         parser_version = EXCLUDED.parser_version
                       RETURNING id""",
                    (
                        str(nb.id),
                        str(nb.source_block_id),
                        str(nb.document_id) if nb.document_id else None,
                        nb.ordinal,
                        nb.block_type,
                        nb.text if nb.disposition != "remove" else "",
                        list(nb.heading_path) if nb.heading_path else None,
                        nb.char_start,
                        nb.char_end,
                        nb.disposition,
                        nb.rule_version,
                        nb.transformation_reason,
                        nb.parser_version,
                    ),
                )
                persisted_row = block_cur.fetchone()
                if persisted_row is None:
                    raise RuntimeError("normalized block upsert returned no identity")
                persisted_block_ids[nb.id] = UUID(str(persisted_row[0]))

        with conn.cursor() as transform_cur:
            for tr in norm_result.transformations:
                transform_cur.execute(
                    """INSERT INTO transformation_records
                       (id, normalized_block_id, rule_id, rule_version,
                        reason, before_text, after_text, confidence)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (normalized_block_id, rule_id) DO UPDATE SET
                         rule_id = EXCLUDED.rule_id,
                         reason = EXCLUDED.reason,
                         before_text = EXCLUDED.before_text,
                         after_text = EXCLUDED.after_text,
                         confidence = EXCLUDED.confidence""",
                    (
                        str(tr.id),
                        (
                            str(persisted_block_ids[tr.normalized_block_id])
                            if tr.normalized_block_id
                            else None
                        ),
                        tr.rule_id,
                        tr.rule_version,
                        tr.reason,
                        tr.before_text,
                        tr.after_text,
                        tr.confidence,
                    ),
                )
            conn.commit()

        upserted_blocks += len(
            norm_result.blocks
            + norm_result.suppressed_blocks
            + norm_result.removed_blocks
        )
        upserted_transforms += len(norm_result.transformations)
        results.append(
            {
                "document_id": str(doc_id),
                "title": doc_key[1],
                "url": doc_key[2],
                "content_sha256": doc_key[3],
                "source_block_count": len(typed_blocks),
                "kept": len(
                    [
                        block
                        for block in norm_result.blocks
                        if block.disposition == "keep"
                    ]
                ),
                "altered": len(
                    [
                        block
                        for block in norm_result.blocks
                        if block.disposition == "alter"
                    ]
                ),
                "suppressed": len(norm_result.suppressed_blocks),
                "removed": len(norm_result.removed_blocks),
                "transformations": len(norm_result.transformations),
                "diagnostics": norm_result.diagnostics(),
            }
        )

    return {
        "rule_version": NORMALIZATION_VERSION,
        "aggressive": args.aggressive,
        "document_type": args.document_type,
        "documents_processed": len(results),
        "normalized_blocks_upserted": upserted_blocks,
        "transformation_records_upserted": upserted_transforms,
        "documents": results,
    }
