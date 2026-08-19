"""Functional release preflight probes using production adapters and paths."""

from __future__ import annotations

import json
import math
import os
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

import model_gateway

from ..acquisition_service import FirecrawlSearchAdapter
from ..config import StoreConfig
from ..container import build_service
from ..domain import IngestRequest
from ..indexing import OpenAICompatibleEmbedder
from ..qdrant import QdrantIndex
from ..qdrant_authority import (
    capture_configured_projection_state,
    require_configured_projection_preserved,
)
from ..resource_sampler import ResourceSampler
from ..retrieval import CohereCompatibleReranker
from ..valkey_queue import ValkeyQueue


def _config(
    *,
    database_url: str = "",
    blob_root: Path | None = None,
    qdrant_url: str = "",
    qdrant_api_key: str = "",
) -> StoreConfig:
    config = StoreConfig.from_env()
    return replace(
        config,
        database_url=database_url or config.database_url,
        blob_root=blob_root or config.blob_root,
        qdrant_url=qdrant_url or config.qdrant_url,
        qdrant_api_key=qdrant_api_key or config.qdrant_api_key,
    )


def probe_postgres(database_url: str) -> str:
    """Require the authoritative schema head and a fresh worker heartbeat."""
    if not database_url:
        raise RuntimeError("DATABASE_URL is required")
    import psycopg
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    with psycopg.connect(database_url, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT current_database(), version()")
        database_row = cur.fetchone()
        if database_row is None:
            raise RuntimeError("PostgreSQL identity query returned no row")
        database_name, database_version = database_row
        cur.execute(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema = 'public'"
        )
        table_row = cur.fetchone()
        if table_row is None:
            raise RuntimeError("PostgreSQL table-count query returned no row")
        table_count = int(table_row[0])
        schema_head = ScriptDirectory.from_config(
            Config(str(Path(__file__).resolve().parents[3] / "alembic.ini"))
        ).get_current_head()
        cur.execute("SELECT version_num FROM alembic_version")
        row = cur.fetchone()
        schema_current = row[0] if row else None
        if schema_current != schema_head:
            raise RuntimeError(
                f"PostgreSQL schema is not at head: {schema_current} != {schema_head}"
            )
        cur.execute(
            """SELECT EXTRACT(EPOCH FROM (now() - MAX(heartbeat_at)))
               FROM index_worker_heartbeats"""
        )
        worker_row = cur.fetchone()
        if worker_row is None:
            raise RuntimeError("worker-heartbeat query returned no row")
        worker_age = worker_row[0]
        if worker_age is None or float(worker_age) > 90:
            raise RuntimeError(
                "index worker heartbeat is missing or stale "
                f"(age={worker_age if worker_age is not None else 'missing'} seconds)"
            )
    return (
        f"PostgreSQL: {database_name} ({table_count} tables); "
        f"schema={schema_current}; worker heartbeat={float(worker_age):.1f}s; "
        f"{str(database_version)[:40]}"
    )


def _probe_writable_directory(path: Path, label: str) -> str:
    path.mkdir(parents=True, exist_ok=True)
    probe = path / f".strict-preflight-{uuid4()}"
    try:
        probe.write_text("ok", encoding="utf-8")
        if probe.read_text(encoding="utf-8") != "ok":
            raise RuntimeError(f"{label} round-trip content mismatch")
    finally:
        probe.unlink(missing_ok=True)
    return f"{label} ({path}): writable"


def run_complete_preflight(
    *,
    database_url: str,
    blob_root: Path | None,
    qdrant_url: str,
    qdrant_api_key: str,
    dataset_path: Path,
    campaign_dir: Path,
    candidate_sha: str,
    get_full_sha: Callable[[], str],
) -> tuple[bool, list[str]]:
    """Run every mandatory release probe without degraded fallbacks."""
    errors: list[str] = []

    if len(candidate_sha) != 40 or any(
        character not in "0123456789abcdef" for character in candidate_sha
    ):
        errors.append(
            "candidate SHA must be a full 40-character lowercase hexadecimal string"
        )
    else:
        try:
            current_sha = get_full_sha()
            if current_sha != candidate_sha:
                raise RuntimeError(
                    f"git HEAD ({current_sha}) does not match candidate SHA "
                    f"({candidate_sha})"
                )
            print(f"  Candidate SHA: {candidate_sha} ✓")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"candidate SHA verification failed: {exc}")

    if not dataset_path.is_file():
        errors.append(f"benchmark dataset not found: {dataset_path}")

    def run(label: str, probe):
        try:
            message = probe()
            if message:
                print(f"  {message}")
            return message
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{label} failed: {exc}")
            return None

    run("PostgreSQL readiness", lambda: probe_postgres(database_url))
    run(
        "Valkey queue round-trip",
        lambda: probe_valkey(os.environ.get("VALKEY_URL", "")),
    )
    run("Firecrawl functional probe", probe_firecrawl)

    embedding_result = None
    try:
        embedding_result = probe_embedding()
        print(f"  {embedding_result[0]}")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"Embedding functional probe failed: {exc}")

    run(
        "Qdrant active-alias round-trip",
        lambda: probe_qdrant(
            qdrant_url,
            qdrant_api_key,
            embedding_result[1] if embedding_result else None,
        ),
    )
    run("Reranker functional probe", probe_reranker)
    run("Structured generative probe", probe_generative)

    if blob_root is None:
        errors.append("Blob root readiness failed: BLOB_ROOT is required")
    else:
        run(
            "Blob root readiness",
            lambda: _probe_writable_directory(blob_root, "Blob root"),
        )
    run(
        "Campaign directory readiness",
        lambda: _probe_writable_directory(campaign_dir, "Campaign directory"),
    )
    run("Resource collector readiness", probe_resources)
    run(
        "Index-worker processing probe",
        lambda: probe_index_worker(
            database_url,
            blob_root,
            qdrant_url,
            qdrant_api_key,
        ),
    )
    return not errors, errors


def _redact_url_credentials(value: str) -> str:
    """Return a service URL with any password replaced by ``***``."""
    try:
        parts = urlsplit(value)
    except (TypeError, ValueError):
        return "<redacted-service-url>"
    if parts.password is None or "@" not in parts.netloc:
        return value
    userinfo, hostinfo = parts.netloc.rsplit("@", 1)
    username = userinfo.split(":", 1)[0]
    return urlunsplit(
        (
            parts.scheme,
            f"{username}:***@{hostinfo}",
            parts.path,
            parts.query,
            parts.fragment,
        )
    )


def probe_valkey(valkey_url: str) -> str:
    """Round-trip one exact token on an isolated Valkey key."""
    if not valkey_url:
        raise RuntimeError("VALKEY_URL is required")
    import redis

    token = uuid4()
    client = redis.Redis.from_url(
        valkey_url,
        socket_connect_timeout=2,
        socket_timeout=2,
    )
    queue = ValkeyQueue(
        valkey_url,
        namespace=f"firecrawl:preflight:{token}",
        client=client,
    )
    try:
        returned = queue.round_trip(token, timeout_seconds=2.0)
        if returned != token:
            raise RuntimeError(
                f"Valkey returned {returned!s}, expected exact token {token}"
            )
    finally:
        queue.clear()
    safe_url = _redact_url_credentials(valkey_url)
    return f"Valkey ({safe_url}): isolated exact-token round-trip OK"


def probe_firecrawl() -> str:
    """Scrape through the production Firecrawl CLI adapter and require markdown."""
    probe_url = os.environ.get("PREFLIGHT_FIRECRAWL_URL", "https://example.com")
    result = FirecrawlSearchAdapter().search(
        probe_url,
        backend="firecrawl_scrape",
        retries=0,
    )
    if result.transport_error:
        raise RuntimeError(result.transport_error)
    payload = json.loads(result.raw_payload)
    if payload.get("success") is not True:
        raise RuntimeError(f"Firecrawl scrape did not succeed: {payload!r}")
    web = (payload.get("data") or {}).get("web") or []
    markdown = web[0].get("markdown") if web and isinstance(web[0], dict) else None
    if not isinstance(markdown, str) or not markdown.strip():
        raise RuntimeError("Firecrawl scrape returned no non-empty markdown")
    return f"Firecrawl scrape: functional OK ({len(markdown)} markdown chars)"


def probe_embedding() -> tuple[str, list[float]]:
    """Call the production embedding adapter and validate its configured contract."""
    config = _config()
    if not config.embedding_url:
        raise RuntimeError("EMBEDDING_URL is required")
    embedder = OpenAICompatibleEmbedder(
        config.embedding_url,
        config.embedding_model,
        config.embedding_api_key,
        config.embedding_dimension,
        config.embedding_fingerprint,
    )
    vector = embedder("strict preflight embedding probe")
    if len(vector) != config.embedding_dimension:
        raise RuntimeError(
            "embedding dimension "
            f"{len(vector)} != configured {config.embedding_dimension}"
        )
    if not all(math.isfinite(value) for value in vector):
        raise RuntimeError("embedding vector contains non-finite values")
    return (
        f"Embedding endpoint: production adapter OK (dim={len(vector)})",
        vector,
    )


def probe_qdrant(
    qdrant_url: str,
    qdrant_api_key: str,
    vector: list[float] | None = None,
) -> str:
    """Require the configured active alias and an exact named-vector round trip."""
    if not qdrant_url:
        raise RuntimeError("QDRANT_URL is required")
    config = _config(qdrant_url=qdrant_url, qdrant_api_key=qdrant_api_key)
    alias_index = QdrantIndex(
        qdrant_url,
        qdrant_api_key,
        config.qdrant_alias,
        config.embedding_dimension,
    )
    aliases = alias_index.list_aliases()
    target = aliases.get(config.qdrant_alias)
    if not target:
        raise RuntimeError(f"required active alias is missing: {config.qdrant_alias}")
    if target != config.physical_collection:
        raise RuntimeError(
            f"active alias targets {target}, expected configured collection "
            f"{config.physical_collection}"
        )

    index = alias_index.for_collection(target, config.embedding_dimension, "Cosine")
    schema = index.inspect_schema()
    if not schema.get("exists") or not schema.get("compatible"):
        raise RuntimeError(f"active collection schema is incompatible: {schema!r}")

    probe_id = uuid4()
    probe_vector = vector or [1.0] + [0.0] * (config.embedding_dimension - 1)
    if len(probe_vector) != config.embedding_dimension:
        raise RuntimeError("Qdrant probe vector does not match configured dimension")
    try:
        index.upsert(
            [
                {
                    "id": str(probe_id),
                    "vector": {"dense": probe_vector},
                    "payload": {"preflight_probe_id": str(probe_id)},
                }
            ]
        )
        points = index.retrieve([probe_id])
        if len(points) != 1:
            raise RuntimeError("Qdrant retrieve did not return the exact probe point")
        point = points[0]
        if str(point.get("id")) != str(probe_id) or (point.get("payload") or {}).get(
            "preflight_probe_id"
        ) != str(probe_id):
            raise RuntimeError(f"Qdrant probe payload mismatch: {point!r}")
    finally:
        try:
            index.delete([probe_id])
        except Exception:  # noqa: BLE001, S110
            pass
    return (
        f"Qdrant: alias {config.qdrant_alias} -> {target}; "
        "named-vector write/read/delete OK"
    )


def probe_reranker() -> str:
    """Call the production Cohere-compatible adapter and validate all scores."""
    config = _config()
    if not config.reranker_url:
        raise RuntimeError("RERANKER_URL is required")
    reranker = CohereCompatibleReranker(
        config.reranker_url,
        config.reranker_model,
        config.reranker_api_key,
    )
    candidates = [
        {"candidate_id": "relevant", "excerpt": "preflight relevance target"},
        {"candidate_id": "control", "excerpt": "unrelated control document"},
    ]
    reranked = reranker("preflight relevance target", candidates)
    if len(reranked) != len(candidates):
        raise RuntimeError("reranker did not return every supplied candidate")
    scores = [item.get("reranker_score") for item in reranked]
    if any(score is None or not math.isfinite(float(score)) for score in scores):
        raise RuntimeError(
            f"reranker returned missing or non-finite scores: {scores!r}"
        )
    numeric = [float(score) for score in scores]
    if numeric != sorted(numeric, reverse=True):
        raise RuntimeError(f"reranker output is not ordered by score: {numeric!r}")
    return f"Reranker endpoint: production adapter OK ({len(reranked)} documents)"


def probe_generative() -> str:
    """Execute a schema-constrained local model request through model_gateway."""
    config = _config()
    if not config.generative_url:
        raise RuntimeError("GENERATIVE_URL is required")
    probe_id = str(uuid4())
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "status": {"type": "string", "const": "ok"},
            "probe_id": {"type": "string", "const": probe_id},
        },
        "required": ["status", "probe_id"],
    }
    result = model_gateway.call_structured(
        provider="local",
        model=config.generative_model,
        system_prompt="Return only the requested strict preflight JSON object.",
        user_prompt=json.dumps({"status": "ok", "probe_id": probe_id}),
        schema=schema,
        max_output_tokens=64,
        timeout=30,
        max_attempts=1,
        prompt_version="strict-preflight-v1",
    )
    if result.error or result.value != {"status": "ok", "probe_id": probe_id}:
        raise RuntimeError(
            f"structured generative probe failed: {result.error or result.value!r}"
        )
    return "Generative endpoint: schema-constrained request OK"


def probe_resources() -> str:
    """Collect real CPU and GPU samples through the production sampler."""
    sampler = ResourceSampler(interval_seconds=0.1, max_samples=2)
    sampler.begin_window()
    cpu_samples, gpu_samples = sampler.end_window()
    cpu = next(
        (
            sample
            for sample in reversed(cpu_samples)
            if sample.status == "measured" and sample.value is not None
        ),
        None,
    )
    gpu = next(
        (
            sample
            for sample in reversed(gpu_samples)
            if sample.status == "measured" and sample.value is not None
        ),
        None,
    )
    if cpu is None:
        raise RuntimeError("CPU collector did not produce a measured sample")
    if gpu is None:
        reasons = [sample.failure_reason for sample in gpu_samples]
        raise RuntimeError(
            f"GPU collector did not produce a measured sample: {reasons}"
        )
    return (
        f"Resource collectors: CPU={cpu.value}% normalized; "
        f"GPU={gpu.value} MiB; device={gpu.device_uuid or '<unknown>'}"
    )


def _worker_heartbeat_is_fresh(database_url: str) -> bool:
    import psycopg

    with psycopg.connect(database_url, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT EXTRACT(EPOCH FROM (now() - MAX(heartbeat_at)))
               FROM index_worker_heartbeats"""
        )
        row = cur.fetchone()
        age = row[0] if row else None
    return age is not None and float(age) <= 90


def probe_index_worker(
    database_url: str,
    blob_root: Path | None,
    qdrant_url: str,
    qdrant_api_key: str,
) -> str:
    """Ingest transient chunks and require an external worker to index them."""
    if not _worker_heartbeat_is_fresh(database_url):
        raise RuntimeError("cannot run worker probe without a fresh worker heartbeat")
    if blob_root is None:
        raise RuntimeError("BLOB_ROOT is required for worker probe")

    config = _config(
        database_url=database_url,
        blob_root=blob_root,
        qdrant_url=qdrant_url,
        qdrant_api_key=qdrant_api_key,
    )
    projection_before = capture_configured_projection_state(config)
    probe_id = uuid4()
    service = build_service(config)
    request = IngestRequest(
        requested_url=f"https://preflight.invalid/{probe_id}",
        final_url=f"https://preflight.invalid/{probe_id}",
        content=(f"# Strict preflight\n\nworker probe {probe_id}\n").encode(),
        mime_type="text/markdown",
        title="Strict preflight worker probe",
        http_status=200,
        firecrawl_version="strict-preflight-v1",
        metadata={"preflight_probe_id": str(probe_id)},
    )

    ingest = None
    chunk_ids: tuple[UUID, ...] = ()
    job_ids: tuple[UUID, ...] = ()
    collection = config.physical_collection
    blob_path: Path | None = None
    cleanup_errors: list[str] = []
    primary_error: Exception | None = None
    last_rows: list[tuple] = []
    try:
        ingest = service.ingest(request)
        chunk_ids = tuple(ingest.chunk_ids)
        if not chunk_ids:
            raise RuntimeError("worker probe ingestion produced no chunks")
        blob_path = service.blob_store.path_for(ingest.content_sha256)

        import psycopg

        timeout = float(os.environ.get("PREFLIGHT_WORKER_TIMEOUT_SECONDS", "60"))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with (
                psycopg.connect(database_url, autocommit=True) as conn,
                conn.cursor() as cur,
            ):
                cur.execute(
                    """SELECT j.id,j.status,j.attempt_count,j.started_at,
                              j.completed_at,j.error,d.physical_collection,d.dimension,
                              j.entity_id
                       FROM index_jobs j
                       JOIN embedding_manifests em ON em.id=j.manifest_id
                       JOIN index_definitions d ON d.id=j.index_definition_id
                       WHERE j.entity_id=ANY(%s)
                       ORDER BY j.created_at,j.id""",
                    (list(chunk_ids),),
                )
                last_rows = list(cur.fetchall())
            if len(last_rows) != len(chunk_ids):
                raise RuntimeError(
                    "worker probe durable job count mismatch: "
                    f"{len(last_rows)} != {len(chunk_ids)}"
                )
            failures = [row for row in last_rows if row[1] in {"failed", "dead"}]
            if failures:
                raise RuntimeError(
                    "worker probe job failed: "
                    + "; ".join(
                        f"{row[0]}={row[1]}:{row[5] or '<no error>'}"
                        for row in failures
                    )
                )
            if all(row[1] == "complete" for row in last_rows):
                break
            time.sleep(0.25)
        else:
            states = {str(row[8]): row[1] for row in last_rows}
            raise RuntimeError(
                f"worker did not complete probe jobs within {timeout:.1f}s; "
                f"last states={states!r}"
            )

        job_ids = tuple(row[0] for row in last_rows)
        if any(row[2] < 1 or row[3] is None or row[4] is None for row in last_rows):
            raise RuntimeError(f"worker jobs lack processing evidence: {last_rows!r}")
        collections = {row[6] for row in last_rows}
        if collections != {config.physical_collection}:
            raise RuntimeError(
                f"worker used collections {collections}, expected "
                f"{config.physical_collection}"
            )
        dimensions = {int(row[7]) for row in last_rows}
        if dimensions != {config.embedding_dimension}:
            raise RuntimeError(
                f"worker used dimensions {dimensions}, expected "
                f"{config.embedding_dimension}"
            )

        index = QdrantIndex(
            qdrant_url,
            qdrant_api_key,
            collection,
            config.embedding_dimension,
        )
        points = index.retrieve(chunk_ids)
        returned_ids = {str(point.get("id")) for point in points}
        expected_ids = {str(chunk_id) for chunk_id in chunk_ids}
        if returned_ids != expected_ids:
            raise RuntimeError(
                "worker completed but exact Qdrant points differ: "
                f"returned={returned_ids}, expected={expected_ids}"
            )
        invalid_payloads = [
            point
            for point in points
            if str((point.get("payload") or {}).get("chunk_id")) != str(point.get("id"))
        ]
        if invalid_payloads:
            raise RuntimeError(
                f"worker Qdrant payload provenance mismatch: {invalid_payloads!r}"
            )
    except Exception as exc:  # noqa: BLE001
        primary_error = exc
    finally:
        worker_active = False
        if chunk_ids:
            try:
                import psycopg

                with (
                    psycopg.connect(database_url, autocommit=True) as conn,
                    conn.cursor() as cur,
                ):
                    cur.execute(
                        "SELECT EXISTS(SELECT 1 FROM index_jobs "
                        "WHERE entity_id=ANY(%s) AND status='running')",
                        (list(chunk_ids),),
                    )
                    row = cur.fetchone()
                    if row is None:
                        raise RuntimeError("worker-lease query returned no row")
                    worker_active = bool(row[0])
            except Exception as exc:  # noqa: BLE001
                worker_active = True
                cleanup_errors.append(
                    f"could not prove worker leases inactive during cleanup: {exc}"
                )

        if chunk_ids and not worker_active:
            try:
                QdrantIndex(
                    qdrant_url,
                    qdrant_api_key,
                    collection,
                    config.embedding_dimension,
                ).delete(chunk_ids)
            except Exception as exc:  # noqa: BLE001
                cleanup_errors.append(f"Qdrant cleanup failed: {exc}")
        for chunk_id in chunk_ids:
            try:
                ValkeyQueue(config.valkey_url).discard(chunk_id)
            except Exception as exc:  # noqa: BLE001
                cleanup_errors.append(f"Valkey cleanup failed: {exc}")
        if ingest is not None:
            if worker_active:
                cleanup_errors.append(
                    "worker probe still owns an active lease; PostgreSQL/blob records "
                    "were preserved to avoid racing the worker"
                )
            else:
                try:
                    import psycopg

                    with (
                        psycopg.connect(database_url, autocommit=True) as conn,
                        conn.cursor() as cur,
                    ):
                        cur.execute(
                            "DELETE FROM index_jobs WHERE entity_id=ANY(%s)",
                            (list(chunk_ids),),
                        )
                        cur.execute(
                            "DELETE FROM embedding_manifests WHERE chunk_id=ANY(%s)",
                            (list(chunk_ids),),
                        )
                        cur.execute(
                            "DELETE FROM chunks WHERE id=ANY(%s)",
                            (list(chunk_ids),),
                        )
                        cur.execute(
                            "DELETE FROM document_blocks WHERE document_id=%s",
                            (ingest.document_id,),
                        )
                        cur.execute(
                            "DELETE FROM documents WHERE id=%s", (ingest.document_id,)
                        )
                        cur.execute(
                            "DELETE FROM asset_snapshots WHERE id=%s",
                            (ingest.snapshot_id,),
                        )
                        cur.execute(
                            "DELETE FROM sources WHERE id=%s", (ingest.source_id,)
                        )
                except Exception as exc:  # noqa: BLE001
                    cleanup_errors.append(f"PostgreSQL cleanup failed: {exc}")
        if blob_path is not None and not worker_active:
            try:
                blob_path.unlink(missing_ok=True)
            except Exception as exc:  # noqa: BLE001
                cleanup_errors.append(f"blob cleanup failed: {exc}")

    try:
        require_configured_projection_preserved(config, projection_before)
    except Exception as exc:  # noqa: BLE001
        cleanup_errors.append(f"Qdrant projection preservation failed: {exc}")

    if primary_error is not None:
        detail = f"; cleanup: {'; '.join(cleanup_errors)}" if cleanup_errors else ""
        raise RuntimeError(f"{primary_error}{detail}") from primary_error
    if cleanup_errors:
        raise RuntimeError("; ".join(cleanup_errors))
    return (
        f"Index worker: {len(job_ids)} job(s) claimed, embedded, indexed, "
        "completed, verified, and cleaned up"
    )
