"""Strict release benchmark campaign for issue #144.

This module provides a CLI entry point that enforces strict release-mode
benchmark execution: two complete campaigns (A and B) with identical
versioned inputs, reproducibility comparison, and durable artifact
manifests.

Strict mode is mandatory and cannot be disabled through ordinary flags.
Simulation and workflow substitution are impossible in release campaigns.

Usage:
    strict_benchmark --candidate-sha <40-char-hex> [--campaign-dir DIR]
                     [--dataset PATH] [--database-url URL]
                     [--blob-root PATH] [--qdrant-url URL]
                     [--qdrant-api-key KEY] [--objectives OBJ1,OBJ2,...]
                     [--tolerance FLOAT] [--manifest PATH] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from hashlib import sha256
from pathlib import Path

SCRIPTS = Path(__file__).resolve()
REPO_ROOT = SCRIPTS.parents[2]
DEFAULT_DATASET = REPO_ROOT / "tests" / "fixtures" / "benchmark" / "benchmark-v2.json"


def _qdrant_compatibility_errors(caught_warnings) -> tuple[str, ...]:
    """Return only client/server compatibility warnings from Qdrant calls."""
    return tuple(
        str(item.message)
        for item in caught_warnings
        if "incompatible with server version" in str(item.message).lower()
    )


sys.path.insert(0, str(SCRIPTS))

from research_store.release_benchmark import (
    RELEASE_MODES,
    MetricStatus,
    ReleaseBenchmarkConfig,
    ReleaseBenchmarkResult,
    ReleaseBenchmarkRunner,
    ReproducibilityComparison,
)
from research_store.workflow_benchmark import load_benchmark_dataset


def _compute_file_hash(path: Path) -> str:
    """Compute SHA-256 hex digest of a file."""
    h = sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _get_full_sha(repo: Path | None = None) -> str:
    """Return the current git commit SHA as a full 40-character hex string.

    Raises ``ValueError`` when the repository cannot be resolved or the
    resolved SHA is not exactly 40 hexadecimal characters.
    """
    try:
        import subprocess

        cmd = ["git", "rev-parse", "HEAD"]
        cwd = str(repo) if repo else None
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            cwd=cwd,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "git rev-parse failed")
        sha = result.stdout.strip()
        if len(sha) != 40 or not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ValueError(f"expected 40-char hex SHA, got {len(sha)} chars: {sha!r}")
        return sha
    except (RuntimeError, ValueError):
        raise
    except Exception as exc:
        raise ValueError(f"unable to resolve git HEAD: {exc}") from exc


def _get_tree_hash(repo: Path | None = None) -> str:
    """Return the current git tree hash as a full 40-character hex string."""
    try:
        import subprocess

        cmd = ["git", "rev-parse", "HEAD^{tree}"]
        cwd = str(repo) if repo else None
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            cwd=cwd,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "git rev-parse tree failed")
        tree = result.stdout.strip()
        if len(tree) != 40 or not re.fullmatch(r"[0-9a-f]{40}", tree):
            raise ValueError(
                f"expected 40-char hex tree hash, got {len(tree)} chars: {tree!r}"
            )
        return tree
    except (RuntimeError, ValueError):
        raise
    except Exception as exc:
        raise ValueError(f"unable to resolve git tree hash: {exc}") from exc


def _get_firecrawl_version() -> str:
    """Return the Firecrawl CLI version string, or 'unknown'."""
    try:
        import subprocess

        result = subprocess.run(
            ["firecrawl", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:  # noqa: BLE001, S110
        pass
    return "unknown"


def _build_env_manifest(
    candidate_sha: str,
    dataset_path: Path,
    dataset_hash: str,
) -> dict:
    """Build runtime environment metadata for the campaign.

    The manifest captures the exact candidate SHA, tree hash, dataset hash,
    dependency-lock hash, service versions, Firecrawl CLI version, model IDs
    and revisions, tokenizer ID and revision, and hardware identity so that
    every campaign run is reproducible from the same immutable inputs.
    """
    import platform

    try:
        tree_hash = _get_tree_hash()
    except ValueError:
        tree_hash = "unresolvable"

    try:
        lock_hash = _compute_file_hash(
            SCRIPTS.parent.parent / "requirements-research-store.txt"
        )
    except Exception:  # noqa: BLE001
        lock_hash = "unresolvable"

    # Collect model/tokenizer fingerprints from environment.
    # URL-typed secrets must never be persisted in release evidence; only
    # model identifiers and non-secret metadata survive.
    _SECRET_URL_KEYS = frozenset(("GENERATIVE_URL", "EMBEDDING_URL", "RERANKER_URL"))
    fingerprints: dict[str, str] = {}
    for key in (
        "GENERATIVE_MODEL",
        "GENERATIVE_URL",
        "EMBEDDING_MODEL",
        "EMBEDDING_URL",
        "EMBEDDING_REVISION",
        "EMBEDDING_DIMENSION",
        "RERANKER_MODEL",
        "RERANKER_URL",
    ):
        val = os.environ.get(key, "")
        if val and key not in _SECRET_URL_KEYS:
            fingerprints[key] = val

    firecrawl_version = _get_firecrawl_version()

    return {
        "candidate_sha": candidate_sha,
        "tree_hash": tree_hash,
        "dataset_path": str(dataset_path),
        "dataset_hash": dataset_hash,
        "dependency_lock_hash": lock_hash,
        "firecrawl_version": firecrawl_version,
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor() or "unknown",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
        **fingerprints,
    }


def _write_json_atomic(path: Path, data: object) -> str:
    """Write JSON atomically via temp-file rename. Returns file hash."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(
        json.dumps(data, indent=2, default=str, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)
    return _compute_file_hash(path)


def _preflight_check(
    database_url: str,
    blob_root: Path | None,
    qdrant_url: str,
    qdrant_api_key: str,
    dataset_path: Path,
    campaign_dir: Path,
    candidate_sha: str,
) -> tuple[bool, list[str]]:
    """Run the complete release preflight through production adapters."""
    from .preflight import run_complete_preflight

    return run_complete_preflight(
        database_url=database_url,
        blob_root=blob_root,
        qdrant_url=qdrant_url,
        qdrant_api_key=qdrant_api_key,
        dataset_path=dataset_path,
        campaign_dir=campaign_dir,
        candidate_sha=candidate_sha,
        get_full_sha=_get_full_sha,
    )


def _legacy_preflight_check(
    database_url: str,
    blob_root: Path | None,
    qdrant_url: str,
    qdrant_api_key: str,
    dataset_path: Path,
    campaign_dir: Path,
    candidate_sha: str,
) -> tuple[bool, list[str]]:
    """Deprecated inline probes retained temporarily for rollback diagnostics.

    The active release path is ``_preflight_check`` above. This implementation
    is not called by the CLI and must not be used as release evidence.
    """
    errors: list[str] = []

    # 0. Candidate SHA verification (MANDATORY)
    if len(candidate_sha) != 40 or not re.fullmatch(r"[0-9a-f]{40}", candidate_sha):
        errors.append(
            f"candidate SHA must be a full 40-character hex string; "
            f"got {len(candidate_sha)} chars: {candidate_sha!r}"
        )
    else:
        try:
            current_sha = _get_full_sha()
            if current_sha != candidate_sha:
                errors.append(
                    f"git HEAD ({current_sha}) does not match "
                    f"candidate SHA ({candidate_sha})"
                )
            else:
                print(f"  Candidate SHA: {candidate_sha} ✓")
        except ValueError as exc:
            errors.append(f"candidate SHA verification failed: {exc}")
    if not dataset_path.is_file():
        errors.append(f"benchmark dataset not found: {dataset_path}")

    # 2. PostgreSQL connectivity (MANDATORY)
    try:
        import psycopg

        conn = psycopg.connect(database_url)
        cur = conn.cursor()
        cur.execute("SELECT current_database(), version()")
        db_name, db_version = cur.fetchone()
        cur.execute(
            "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'public'"
        )
        table_count = cur.fetchone()[0]
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        alembic_config = Config(str(SCRIPTS.parent.parent / "alembic.ini"))
        schema_head = ScriptDirectory.from_config(alembic_config).get_current_head()
        cur.execute("SELECT version_num FROM alembic_version")
        schema_current = cur.fetchone()[0]
        if schema_current != schema_head:
            errors.append(
                f"PostgreSQL schema is not at head: {schema_current} != {schema_head}"
            )
        cur.execute(
            """SELECT EXTRACT(EPOCH FROM (now() - MAX(heartbeat_at)))
               FROM index_worker_heartbeats"""
        )
        worker_age = cur.fetchone()[0]
        if worker_age is None or float(worker_age) > 90:
            errors.append(
                "index worker heartbeat is missing or stale "
                f"(age={worker_age if worker_age is not None else 'missing'} seconds)"
            )
        # Index-worker queue processing: verify the worker can accept a test job.
        try:
            from uuid import UUID

            test_job_id = str(UUID(int=42))
            cur.execute(
                """INSERT INTO index_jobs (
                       id, run_id, entity_type, entity_id, status,
                       created_at, updated_at
                   ) VALUES (%s, %s, %s, %s, 'pending', now(), now())
                   ON CONFLICT DO NOTHING""",
                (
                    test_job_id,
                    str(UUID(int=1)),
                    "preflight_test",
                    test_job_id,
                ),
            )
            cur.execute("SELECT status FROM index_jobs WHERE id = %s", (test_job_id,))
            row = cur.fetchone()
            if row is None or row[0] != "pending":
                raise RuntimeError(
                    "index_jobs queue write failed: job not found or not pending"
                )
            cur.execute("DELETE FROM index_jobs WHERE id = %s", (test_job_id,))
            print("  Index-worker queue: write and process OK")
        except Exception as queue_exc:  # noqa: BLE001
            errors.append(f"index-worker queue processing check failed: {queue_exc}")
        conn.close()
        print(f"  PostgreSQL: {db_name} ({table_count} tables) — {db_version[:60]}")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"PostgreSQL connection failed: {exc}")

    # 2.5. Valkey connectivity and queue round-trip (MANDATORY)
    valkey_url = os.environ.get("VALKEY_URL", "")
    if valkey_url:
        try:
            from uuid import UUID

            from .valkey_queue import ValkeyQueue

            queue = ValkeyQueue(url=valkey_url)
            pushed = queue.notify(UUID(int=1))
            if not pushed:
                raise RuntimeError("Valkey LPUSH failed — queue notify returned False")
            popped = queue.wait(timeout_seconds=2.0)
            if not popped:
                raise RuntimeError("Valkey BLPOP timed out — queue round-trip failed")
            print(f"  Valkey ({valkey_url}): queue round-trip OK")
        except ImportError:
            errors.append(
                "redis (Valkey client) is required for queue round-trip preflight"
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Valkey queue round-trip failed: {exc}")
    else:
        errors.append("VALKEY_URL is required")

    # 3. Qdrant connectivity, active alias, and collection state (MANDATORY)
    if qdrant_url:
        try:
            import warnings

            from qdrant_client import QdrantClient

            with warnings.catch_warnings(record=True) as caught_warnings:
                warnings.simplefilter("always")
                qdrant = QdrantClient(url=qdrant_url, api_key=qdrant_api_key or None)
                alias_name = os.environ.get("QDRANT_ALIAS", "research_chunks_active")
                aliases = qdrant.get_aliases().aliases
                alias = next(
                    (item for item in aliases if item.alias_name == alias_name), None
                )
                if alias is None:
                    raise RuntimeError(
                        f"required active alias is missing: {alias_name}"
                    )
                collection = qdrant.get_collection(alias.collection_name)
            compatibility_errors = _qdrant_compatibility_errors(caught_warnings)
            if compatibility_errors:
                raise RuntimeError(compatibility_errors[0])
            if "green" not in str(collection.status).lower():
                raise RuntimeError(
                    f"active collection is not green: {alias.collection_name} ({collection.status})"
                )
            # Qdrant write/read operation test (MANDATORY)
            test_id = "preflight_write_read_test"
            test_vector = [0.1] * 3
            test_payload = {"source": "preflight", "test_id": test_id}
            qdrant.upsert(
                alias.collection_name,
                points=[
                    {"id": test_id, "vector": test_vector, "payload": test_payload}
                ],
            )
            results = qdrant.search(
                alias.collection_name, query_vector=test_vector, limit=1
            )
            qdrant.delete_points(alias.collection_name, points=[test_id])
            if not results or not any(
                p.payload.get("test_id") == test_id for p in results
            ):
                raise RuntimeError("Qdrant write/read operation test failed")
            print(
                f"  Qdrant: alias {alias_name} -> {alias.collection_name}; "
                f"status={collection.status}; write/read OK"
            )
            qdrant.close()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Qdrant readiness failed: {exc}")
    else:
        errors.append("QDRANT_URL is required")

    # 4. Firecrawl CLI and backend availability (MANDATORY)
    try:
        import subprocess

        result = subprocess.run(
            ["firecrawl", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode == 0:
            version = result.stdout.strip()
            print(f"  Firecrawl CLI: {version}")
        else:
            errors.append("Firecrawl CLI returned non-zero exit code")
    except FileNotFoundError:
        errors.append("Firecrawl CLI not found in PATH")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"Firecrawl CLI check failed: {exc}")
    try:
        import urllib.request

        firecrawl_url = os.environ.get("FIRECRAWL_API_URL", "")
        if not firecrawl_url:
            raise RuntimeError("FIRECRAWL_API_URL is not configured")
        with urllib.request.urlopen(
            firecrawl_url.rstrip("/") + "/", timeout=10
        ) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        if payload.get("message") != "Firecrawl API":
            raise RuntimeError(f"unexpected API root response: {payload!r}")
        print(f"  Firecrawl API ({firecrawl_url}): reachable")

        # Functional search test (MANDATORY)
        search_url = firecrawl_url.rstrip("/") + "/search"
        search_payload = json.dumps({"query": "site:example.com", "limit": 1}).encode(
            "utf-8"
        )
        search_req = urllib.request.Request(
            search_url,
            data=search_payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(search_req, timeout=30) as resp:
            search_result = json.loads(resp.read().decode("utf-8"))
        # Firecrawl search returns {"success": true, "data": [...]} or similar.
        # We only require a successful HTTP response with structured JSON.
        if not isinstance(search_result, dict) or "success" not in search_result:
            raise RuntimeError(
                f"Firecrawl search returned unexpected format: {search_result!r}"
            )
        print("  Firecrawl search: functional OK")

        # Functional scrape test (MANDATORY)
        scrape_url = firecrawl_url.rstrip("/") + "/scrape"
        scrape_payload = json.dumps(
            {"url": "https://example.com", "formats": ["markdown"]}
        ).encode("utf-8")
        scrape_req = urllib.request.Request(
            scrape_url,
            data=scrape_payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(scrape_req, timeout=30) as resp:
            scrape_result = json.loads(resp.read().decode("utf-8"))
        if not isinstance(scrape_result, dict) or "success" not in scrape_result:
            raise RuntimeError(
                f"Firecrawl scrape returned unexpected format: {scrape_result!r}"
            )
        print("  Firecrawl scrape: functional OK")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"Firecrawl backend check failed: {exc}")

    # 5. Embedding endpoint health (MANDATORY)
    try:
        import urllib.request

        embedding_url = os.environ.get(
            "EMBEDDING_URL", os.environ.get("FIRECRAWL_EMBEDDING_URL", "")
        )
        if embedding_url:
            # Normalize to the API root: strip any operation suffix
            # (e.g. /embeddings, /rerank) so /v1/models is correct.
            # "http://host/v1/embeddings" → "http://host/v1"
            # "http://host/v1" → "http://host/v1"
            base = re.sub(r"/v1/[^/]+/?$", "/v1", embedding_url.rstrip("/"))
            req = urllib.request.Request(
                f"{base}/models",
                headers={
                    "Authorization": f"Bearer {os.environ.get('EMBEDDING_API_KEY', '')}"
                }
                if os.environ.get("EMBEDDING_API_KEY")
                else {},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            models = [m["id"] for m in data.get("data", [])]
            embed_models = [m for m in models if "embed" in m.lower()]
            configured_model = os.environ.get("EMBEDDING_MODEL", "")
            if not embed_models or (
                configured_model and configured_model not in models
            ):
                raise RuntimeError(
                    f"configured embedding model unavailable: {configured_model or '<unset>'}"
                )
            print(
                f"  Embedding endpoint ({embedding_url}): {len(embed_models)} model(s): {', '.join(embed_models) or 'none'}"
            )

            # Actual embedding request test (MANDATORY)
            embed_endpoint = re.sub(r"/v1$", "/v1/embeddings", base)
            embed_payload = json.dumps(
                {
                    "model": configured_model or embed_models[0],
                    "input": ["preflight test text"],
                    "encoding_type": "float",
                }
            ).encode("utf-8")
            embed_req = urllib.request.Request(
                embed_endpoint,
                data=embed_payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(embed_req, timeout=30) as resp:
                embed_result = json.loads(resp.read().decode())
            if (
                not isinstance(embed_result, dict)
                or "data" not in embed_result
                or not isinstance(embed_result["data"], list)
                or len(embed_result["data"]) == 0
            ):
                raise RuntimeError(
                    f"embedding request returned unexpected format: {embed_result!r}"
                )
            vector = embed_result["data"][0].get("embedding")
            if not isinstance(vector, list) or len(vector) == 0:
                raise RuntimeError(
                    f"embedding vector missing or empty: {embed_result!r}"
                )
            print(f"  Embedding endpoint: actual request OK (vector dim={len(vector)})")
        else:
            errors.append("EMBEDDING_URL not set — embedding unavailable")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"Embedding endpoint check failed: {exc}")

    # 6. Generative endpoint health (MANDATORY)
    try:
        import urllib.request

        generative_url = os.environ.get(
            "GENERATIVE_URL", os.environ.get("FIRECRAWL_GENERATIVE_URL", "")
        )
        if generative_url:
            # Normalize to the API root: strip any operation suffix
            # (e.g. /embeddings, /rerank) so /v1/models is correct.
            # "http://host/v1" → "http://host/v1" (no change)
            base = re.sub(r"/v1/[^/]+/?$", "/v1", generative_url.rstrip("/"))
            req = urllib.request.Request(
                f"{base}/models",
                headers={
                    "Authorization": f"Bearer {os.environ.get('GENERATIVE_API_KEY', '')}"
                }
                if os.environ.get("GENERATIVE_API_KEY")
                else {},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            models = [m["id"] for m in data.get("data", [])]
            chat_models = [
                m for m in models if "chat" in m.lower() or "llm" in m.lower()
            ]
            configured_model = os.environ.get("GENERATIVE_MODEL", "")
            if not models or (configured_model and configured_model not in models):
                raise RuntimeError(
                    f"configured generative model unavailable: {configured_model or '<unset>'}"
                )
            print(
                f"  Generative endpoint ({generative_url}): {len(chat_models)} model(s): {', '.join(chat_models) or 'none'}"
            )

            # Actual structured generative request test (MANDATORY)
            chat_endpoint = re.sub(r"/v1$", "/v1/chat/completions", base)
            chat_payload = json.dumps(
                {
                    "model": configured_model or models[0],
                    "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
                    "max_tokens": 10,
                }
            ).encode("utf-8")
            chat_req = urllib.request.Request(
                chat_endpoint,
                data=chat_payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(chat_req, timeout=30) as resp:
                chat_result = json.loads(resp.read().decode())
            if (
                not isinstance(chat_result, dict)
                or "choices" not in chat_result
                or not isinstance(chat_result["choices"], list)
                or len(chat_result["choices"]) == 0
            ):
                raise RuntimeError(
                    f"generative request returned unexpected format: {chat_result!r}"
                )
            content = chat_result["choices"][0].get("message", {}).get("content", "")
            if not isinstance(content, str) or len(content) == 0:
                raise RuntimeError(
                    f"generative response missing content: {chat_result!r}"
                )
            print(
                f"  Generative endpoint: actual request OK (content='{content[:40]}')"
            )
        else:
            errors.append("GENERATIVE_URL not set — generative LLM unavailable")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"Generative endpoint check failed: {exc}")

    # 7. Reranker endpoint health (MANDATORY)
    try:
        import urllib.request

        reranker_url = os.environ.get(
            "RERANKER_URL", os.environ.get("FIRECRAWL_RERANKER_URL", "")
        )
        if reranker_url:
            # Normalize to the API root: strip any operation suffix
            # (e.g. /embeddings, /rerank) so /v1/models is correct.
            # "http://host/v1/rerank" → "http://host/v1"
            base = re.sub(r"/v1/[^/]+/?$", "/v1", reranker_url.rstrip("/"))
            req = urllib.request.Request(
                f"{base}/models",
                headers={
                    "Authorization": f"Bearer {os.environ.get('RERANKER_API_KEY', '')}"
                }
                if os.environ.get("RERANKER_API_KEY")
                else {},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
            models = [m["id"] for m in data.get("data", [])]
            rerank_models = [m for m in models if "rerank" in m.lower()]
            configured_model = os.environ.get("RERANKER_MODEL", "")
            if not rerank_models or (
                configured_model and configured_model not in models
            ):
                raise RuntimeError(
                    f"configured reranker model unavailable: {configured_model or '<unset>'}"
                )
            print(
                f"  Reranker endpoint ({reranker_url}): {len(rerank_models)} model(s): {', '.join(rerank_models) or 'none'}"
            )

            # Actual reranking request test (MANDATORY)
            rerank_endpoint = re.sub(r"/v1$", "/v1/rerank", base)
            rerank_payload = json.dumps(
                {
                    "model": configured_model or rerank_models[0],
                    "query": "preflight test query",
                    "documents": ["preflight test document text"],
                }
            ).encode("utf-8")
            rerank_req = urllib.request.Request(
                rerank_endpoint,
                data=rerank_payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(rerank_req, timeout=30) as resp:
                rerank_result = json.loads(resp.read().decode())
            if (
                not isinstance(rerank_result, dict)
                or "results" not in rerank_result
                or not isinstance(rerank_result["results"], list)
                or len(rerank_result["results"]) == 0
            ):
                raise RuntimeError(
                    f"reranking request returned unexpected format: {rerank_result!r}"
                )
            first_result = rerank_result["results"][0]
            if "index" not in first_result or "score" not in first_result:
                raise RuntimeError(
                    f"reranking result missing index/score: {rerank_result!r}"
                )
            print(
                f"  Reranker endpoint: actual request OK (score={first_result['score']})"
            )
        else:
            errors.append("RERANKER_URL not set — reranker unavailable")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"Reranker endpoint check failed: {exc}")

    # 8. Blob root writability (MANDATORY)
    if blob_root:
        try:
            blob_root.mkdir(parents=True, exist_ok=True)
            test_file = blob_root / ".preflight_test"
            test_file.write_text("ok", encoding="utf-8")
            test_file.unlink()
            print(f"  Blob root ({blob_root}): writable")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Blob root not writable: {exc}")
    else:
        errors.append("BLOB_ROOT is required")

    # 9. Resource collectors (MANDATORY for complete CPU/GPU telemetry)
    try:
        import importlib.metadata

        import psutil

        print(f"  CPU collector: psutil {getattr(psutil, '__version__', 'unknown')}")
    except ImportError:
        errors.append("psutil is required for process-scoped CPU telemetry")
    try:
        import pynvml

        version = getattr(pynvml, "__version__", "") or importlib.metadata.version(
            "nvidia-ml-py"
        )
        nvml_initialized = False
        try:
            pynvml.nvmlInit()
            nvml_initialized = True
            handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            device_uuid = pynvml.nvmlDeviceGetUUID(handle)
            pynvml.nvmlDeviceGetMemoryInfo(handle)
        finally:
            if nvml_initialized:
                pynvml.nvmlShutdown()
        if not device_uuid:
            raise RuntimeError("GPU device 0 did not provide a UUID")
        print(f"  GPU collector: pynvml {version}; device={device_uuid}")
    except ImportError:
        errors.append("nvidia-ml-py is required for GPU telemetry")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"GPU collector readiness failed: {exc}")

    # 10. Campaign directory writability (MANDATORY)
    try:
        campaign_dir.mkdir(parents=True, exist_ok=True)
        print(f"  Campaign directory ({campaign_dir}): writable")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"Campaign directory not writable: {exc}")

    # 11. Firecrawl search functional check (MANDATORY)
    try:
        import subprocess

        search_result = subprocess.run(
            [
                "firecrawl",
                "search",
                "site:github.com Python",
                "--limit",
                "1",
                "--sources",
                "web",
                "--ignore-invalid-urls",
                "--scrape",
                "--scrape-formats",
                "markdown",
                "--json",
            ],
            capture_output=True,
            text=False,
            timeout=60,
            check=False,
        )
        if search_result.returncode == 0 and search_result.stdout:
            search_data = json.loads(search_result.stdout.decode("utf-8"))
            results = (
                search_data
                if isinstance(search_data, list)
                else search_data.get("results", [])
            )
            if isinstance(results, list) and len(results) > 0:
                print(f"  Firecrawl search: {len(results)} result(s) returned")
            else:
                errors.append(
                    "Firecrawl search returned no results (API root works but search is broken)"
                )
        else:
            stderr = search_result.stderr.decode("utf-8", errors="replace").strip()
            if search_result.returncode == 0:
                errors.append(
                    "Firecrawl search returned non-zero exit code without output"
                )
            else:
                errors.append(
                    f"Firecrawl search functional check failed (exit {search_result.returncode}): {stderr[:300]}"
                )
    except FileNotFoundError:
        # Already caught in check #4
        pass
    except subprocess.TimeoutExpired:
        errors.append("Firecrawl search functional check timed out after 60s")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"Firecrawl search functional check failed: {exc}")

    # 12. Qdrant write/read operation (MANDATORY)
    if qdrant_url:
        try:
            import uuid as uuid_module

            from qdrant_client import QdrantClient
            from qdrant_client.models import (
                Distance,
                PayloadSchemaType,
                PointStruct,
                VectorParams,
            )
            from research_store.config import StoreConfig

            embedding_dimension = StoreConfig.from_env().embedding_dimension
            client = QdrantClient(url=qdrant_url, api_key=qdrant_api_key or None)
            test_collection = "preflight_test_write_read"
            alias_name = os.environ.get("QDRANT_ALIAS", "research_chunks_active")
            try:
                aliases = client.get_aliases()
                target_alias = next(
                    (item for item in aliases.aliases if item.alias_name == alias_name),
                    None,
                )
                if target_alias is None:
                    raise RuntimeError(
                        f"preflight cannot locate active alias: {alias_name}"
                    )
                collection_name = target_alias.collection_name
            except Exception:  # noqa: BLE001
                # Fallback: create a dedicated test collection
                collection_name = test_collection
                try:
                    client.create_collection(
                        collection_name,
                        vectors_config=VectorParams(
                            size=embedding_dimension,
                            distance=Distance.COSINE,
                        ),
                    )
                    client.create_payload_index(
                        collection_name,
                        "preflight_test",
                        field_schema=PayloadSchemaType.KEYWORD,
                    )
                except Exception:
                    client.close()
                    raise
            test_id = str(uuid_module.uuid4())
            test_vector = [0.0] * embedding_dimension
            client.upsert(
                collection_name,
                points=[
                    PointStruct(
                        id=test_id,
                        vector=test_vector,
                        payload={"preflight_test": "true"},
                    )
                ],
            )
            retrieved = client.retrieve(
                collection_name, ids=[test_id], with_payload=True
            )
            if not retrieved or retrieved[0].payload.get("preflight_test") != "true":
                raise RuntimeError("Qdrant write/read round-trip failed")
            print(f"  Qdrant write/read: round-trip OK on {collection_name}")
            # Cleanup if we created a test collection
            if collection_name == test_collection:
                try:
                    client.delete_collection(collection_name)
                except Exception:  # noqa: S110, BLE001
                    pass
            client.close()
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Qdrant write/read operation failed: {exc}")

    return (len(errors) == 0, errors)


def _run_campaign(
    campaign_label: str,
    dataset_path: Path,
    database_url: str,
    blob_root: Path | None,
    qdrant_url: str,
    qdrant_api_key: str,
    objective_ids: tuple[str, ...] | None,
    strict: bool,
    reproducibility_tolerance: float,
    campaign_dir: Path,
    candidate_sha: str,
    execution_modes: tuple[str, ...] = ("autonomous_local", "deterministic_debug"),
) -> tuple[ReleaseBenchmarkResult, str]:
    """Execute a single strict campaign wave and return its result and integrity hash.

    Args:
        campaign_label: Display label for the campaign run.
        dataset_path: Path to the benchmark dataset.
        database_url: Target PostgreSQL database.
        blob_root: Target blob storage path.
        qdrant_url: Target Qdrant vector database URL.
        qdrant_api_key: Target Qdrant vector database API key.
        objective_ids: Specific objective IDs to run, or None for all.
        strict: Whether to enforce strict mode (mandatory).
        reproducibility_tolerance: Acceptance threshold for deterministic deviation.
        campaign_dir: Directory to write campaign artifacts to.
        candidate_sha: Exact 40-character git commit SHA for the release candidate.
        execution_modes: Workflow modes to run.

    Returns:
        Tuple of (result, campaign_id).
    """
    print(f"[Campaign {campaign_label}] Starting strict benchmark campaign...")

    # Make candidate SHA available to the benchmark runner.
    os.environ["CANDIDATE_SHA"] = candidate_sha

    # Load benchmark dataset
    print(f"[Campaign {campaign_label}] Loading dataset from {dataset_path}")
    loader = load_benchmark_dataset(dataset_path)

    # Build config with strict mode mandatory
    config = ReleaseBenchmarkConfig(
        database_url=database_url,
        blob_root=blob_root,
        qdrant_url=qdrant_url,
        qdrant_api_key=qdrant_api_key,
        execution_modes=execution_modes,
        objective_ids=objective_ids,
        strict=strict,
        reproducibility_tolerance=reproducibility_tolerance,
    )

    print(
        f"[Campaign {campaign_label}] Config: strict={config.strict}, "
        f"modes={config.execution_modes}, "
        f"objectives={config.objective_ids or 'all'}"
    )

    # Build runner
    runner = ReleaseBenchmarkRunner(loader, config)

    # Execute campaign
    start = time.monotonic()
    result = runner.run()
    elapsed = (time.monotonic() - start) * 1000

    print(f"[Campaign {campaign_label}] Campaign ID: {result.campaign_id}")
    print(f"[Campaign {campaign_label}] Duration: {elapsed:.0f}ms")
    print(f"[Campaign {campaign_label}] Runs: {len(result.runs)}")

    for run in result.runs:
        status = "OK" if not run.errors else f"ERROR: {run.errors}"
        print(
            f"  - {run.mode}: run_id={run.run_id[:12] if run.run_id else 'N/A'} ... {status}"
        )

    if result.recommendation:
        print(
            f"[Campaign {campaign_label}] Recommendation: "
            f"{result.recommendation.outcome}"
        )

    # Persist campaign artifacts
    campaign_ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    artifacts_dir = campaign_dir / campaign_label / campaign_ts
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    # Write result JSON
    result_path = artifacts_dir / "result.json"
    result_hash = _write_json_atomic(
        result_path,
        {
            "schema_version": result.schema_version,
            "campaign_id": result.campaign_id,
            "campaign_timestamp": result.campaign_timestamp,
            "environment": result.environment,
            "recommendation": {
                "outcome": result.recommendation.outcome
                if result.recommendation
                else None,
                "supported_claims": result.recommendation.supported_claims
                if result.recommendation
                else (),
                "withdrawn_claims": result.recommendation.withdrawn_claims
                if result.recommendation
                else (),
                "known_limitations": result.recommendation.known_limitations
                if result.recommendation
                else (),
                "conditions": result.recommendation.conditions
                if result.recommendation
                else (),
                "p0_regressions": result.recommendation.p0_regressions
                if result.recommendation
                else (),
            }
            if result.recommendation
            else None,
            "total_duration_ms": result.total_duration_ms,
            "runs": [
                {
                    "campaign_id": run.campaign_id,
                    "run_id": run.run_id,
                    "mode": run.mode,
                    "objective_id": run.objective_id,
                    "quality": {
                        "candidate_recall": run.quality.candidate_recall
                        if run.quality
                        else None,
                        "source_quality_score": run.quality.source_quality_score
                        if run.quality
                        else None,
                        "coverage_completeness": run.quality.coverage_completeness
                        if run.quality
                        else None,
                        "unsupported_claim_rate": run.quality.unsupported_claim_rate
                        if run.quality
                        else None,
                        "citation_accuracy": run.quality.citation_accuracy
                        if run.quality
                        else None,
                        "report_quality_score": run.quality.report_quality_score
                        if run.quality
                        else None,
                    }
                    if run.quality
                    else None,
                    "quality_metrics": [
                        {
                            "name": qm.name,
                            "value": qm.value,
                            "status": getattr(
                                qm, "status", MetricStatus.UNEVALUATED
                            ).value,
                            "formula": qm.formula,
                            "source": {
                                "table": qm.source.table,
                                "column": qm.source.column,
                                "run_id": qm.source.run_id,
                                "method": qm.source.method,
                                "record_ids": list(qm.source.event_ids),
                                "stages": list(qm.source.stages),
                                "stage_set_version": qm.source.stage_set_version,
                                "sample_count": qm.source.sample_count,
                                "device_type": qm.source.device_type,
                                "device_index": qm.source.device_index,
                                "device_uuid": qm.source.device_uuid,
                                "collector": qm.source.collector,
                                "collector_version": qm.source.collector_version,
                                "status_counts": dict(qm.source.status_counts),
                            },
                        }
                        for qm in run.quality_metrics
                    ]
                    if run.quality_metrics
                    else [],
                    "performance": {
                        "total_latency_ms": run.performance.total_latency_ms
                        if run.performance
                        else None,
                        "total_tokens": run.performance.total_tokens
                        if run.performance
                        else None,
                        "semantic_calls": run.performance.semantic_calls
                        if run.performance
                        else None,
                        "cache_hit_rate": run.performance.cache_hit_rate
                        if run.performance
                        else None,
                        "embedding_throughput": run.performance.embedding_throughput
                        if run.performance
                        else None,
                        "cpu_percent": run.performance.cpu_percent
                        if run.performance
                        else None,
                        "gpu_memory_mb": run.performance.gpu_memory_mb
                        if run.performance
                        else None,
                    }
                    if run.performance
                    else None,
                    "performance_metrics": [
                        {
                            "name": pm.name,
                            "value": pm.value,
                            "status": getattr(
                                pm, "status", MetricStatus.UNEVALUATED
                            ).value,
                            "formula": pm.formula,
                            "source": {
                                "table": pm.source.table,
                                "column": pm.source.column,
                                "run_id": pm.source.run_id,
                                "method": pm.source.method,
                                "record_ids": list(pm.source.event_ids),
                                "stages": list(pm.source.stages),
                                "stage_set_version": pm.source.stage_set_version,
                                "sample_count": pm.source.sample_count,
                                "device_type": pm.source.device_type,
                                "device_index": pm.source.device_index,
                                "device_uuid": pm.source.device_uuid,
                                "collector": pm.source.collector,
                                "collector_version": pm.source.collector_version,
                                "status_counts": dict(pm.source.status_counts),
                            },
                        }
                        for pm in run.performance_metrics
                    ]
                    if run.performance_metrics
                    else [],
                    "errors": run.errors,
                    "integrity_checks": [
                        {
                            "check": c.check_name,
                            "passed": c.passed,
                            "details": c.details,
                        }
                        for c in run.integrity_checks
                    ]
                    if run.integrity_checks
                    else [],
                }
                for run in result.runs
            ],
        },
    )

    # Write environment manifest
    dataset_hash = _compute_file_hash(dataset_path)
    env_manifest = {
        **_build_env_manifest(candidate_sha, dataset_path, dataset_hash),
        "database_url_set": bool(database_url),
        "blob_root_set": bool(blob_root),
        "strict": strict,
        "execution_modes": RELEASE_MODES,
        "objective_ids": list(objective_ids) if objective_ids else ["all"],
        "reproducibility_tolerance": reproducibility_tolerance,
        "reproducibility_policy_version": "reproducibility-policy-v2",
        "operational_reproducibility_ratio_limit": float(
            loader.quality_thresholds.get(
                "max_operational_reproducibility_ratio",
                config.operational_reproducibility_ratio_limit,
            )
        ),
    }
    env_manifest_path = artifacts_dir / "environment.json"
    _write_json_atomic(env_manifest_path, env_manifest)

    # Write summary
    summary_path = artifacts_dir / "summary.txt"
    summary_path.write_text(result.summary() + "\n", encoding="utf-8")

    print(f"[Campaign {campaign_label}] Artifacts written to {artifacts_dir}")
    print(f"[Campaign {campaign_label}] Result hash: {result_hash}")

    return result, result_hash


def _compare_campaigns(
    result_a: ReleaseBenchmarkResult,
    result_b: ReleaseBenchmarkResult,
    campaign_dir: Path,
    reproducibility_tolerance: float,
    dataset_path: Path,
) -> ReproducibilityComparison:
    """Compare two campaign runs for reproducibility.

    Args:
        result_a: Campaign A result.
        result_b: Campaign B result.
        campaign_dir: Directory to write comparison artifacts to.
        reproducibility_tolerance: Tolerance to use for the comparison.
        dataset_path: Path to the benchmark dataset used by both campaigns.

    Returns:
        ReproducibilityComparison.
    """
    print("[Reproducibility] Comparing Campaign A and Campaign B...")

    loader = load_benchmark_dataset(dataset_path)
    runner = ReleaseBenchmarkRunner(loader, ReleaseBenchmarkConfig())

    comparison = runner.compare_campaigns(
        result_a, result_b, tolerance=reproducibility_tolerance
    )

    print(f"[Reproducibility] All within tolerance: {comparison.all_within_tolerance}")
    for detail in comparison.details:
        print(f"  - {detail}")

    # Write comparison artifacts
    comparison_dir = (
        campaign_dir
        / "reproducibility"
        / time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    )
    comparison_dir.mkdir(parents=True, exist_ok=True)

    _write_json_atomic(
        comparison_dir / "comparison.json",
        {
            "schema_version": comparison.schema_version,
            "run_a_id": comparison.run_a_id,
            "run_b_id": comparison.run_b_id,
            "mode": comparison.mode,
            "objective_id": comparison.objective_id,
            "all_within_tolerance": comparison.all_within_tolerance,
            "quality_tolerances": list(comparison.quality_tolerances),
            "performance_tolerances": list(comparison.performance_tolerances),
            "policy_version": comparison.policy_version,
            "relative_tolerance": comparison.relative_tolerance,
            "operational_ratio_limit": comparison.operational_ratio_limit,
            "operational_absolute_tolerances": dict(
                comparison.operational_absolute_tolerances
            ),
            "details": comparison.details,
            "observations": comparison.observations,
        },
    )

    summary_path = comparison_dir / "summary.txt"
    lines = [
        f"Reproducibility Comparison — {comparison.run_a_id} vs {comparison.run_b_id}",
        f"Outcome: {'PASS' if comparison.all_within_tolerance else 'FAIL'}",
        f"Quality tolerances: {len(comparison.quality_tolerances)} metrics compared",
        f"Performance tolerances: {len(comparison.performance_tolerances)} metrics compared",
    ]
    for detail in comparison.details:
        lines.append(f"  - {detail}")
    for observation in comparison.observations:
        lines.append(f"  - observation: {observation}")
    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return comparison


def _build_manifest(
    campaign_dir: Path,
    result_a: ReleaseBenchmarkResult,
    result_b: ReleaseBenchmarkResult,
    comparison: ReproducibilityComparison,
    dataset_path: Path,
    candidate_sha: str,
) -> dict:
    """Build the durable artifact manifest."""
    campaign_a_dir = None
    campaign_b_dir = None
    for label_dir in (campaign_dir / "A", campaign_dir / "B"):
        if label_dir.exists():
            latest = max(label_dir.iterdir(), key=lambda p: p.name)
            if label_dir == campaign_dir / "A":
                campaign_a_dir = latest
            else:
                campaign_b_dir = latest

    try:
        tree_hash = _get_tree_hash()
    except ValueError:
        tree_hash = "unresolvable"

    manifest = {
        "schema_version": "campaign-manifest-v1",
        "candidate_sha": candidate_sha,
        "tree_hash": tree_hash,
        "dataset_path": str(dataset_path),
        "dataset_hash": _compute_file_hash(dataset_path),
        "dataset_version": load_benchmark_dataset(dataset_path).dataset.version,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
        "campaign_a": {
            "campaign_id": result_a.campaign_id,
            "result_hash": _compute_file_hash(campaign_a_dir / "result.json")
            if campaign_a_dir
            else None,
            "result_path": str(campaign_a_dir) if campaign_a_dir else None,
            "runs": len(result_a.runs),
            "run_ids": [run.run_id for run in result_a.runs],
            "recommendation": result_a.recommendation.outcome
            if result_a.recommendation
            else None,
        },
        "campaign_b": {
            "campaign_id": result_b.campaign_id,
            "result_hash": _compute_file_hash(campaign_b_dir / "result.json")
            if campaign_b_dir
            else None,
            "result_path": str(campaign_b_dir) if campaign_b_dir else None,
            "runs": len(result_b.runs),
            "run_ids": [run.run_id for run in result_b.runs],
            "recommendation": result_b.recommendation.outcome
            if result_b.recommendation
            else None,
        },
        "reproducibility": {
            "all_within_tolerance": comparison.all_within_tolerance,
            "run_a_id": comparison.run_a_id,
            "run_b_id": comparison.run_b_id,
            "policy_version": comparison.policy_version,
            "relative_tolerance": comparison.relative_tolerance,
            "operational_ratio_limit": comparison.operational_ratio_limit,
            "operational_absolute_tolerances": dict(
                comparison.operational_absolute_tolerances
            ),
            "details": list(comparison.details),
            "observations": list(comparison.observations),
        },
        "modes": list(RELEASE_MODES),
    }
    return manifest


def main(
    argv: list[str] | None = None,
    execution_modes: tuple[str, ...] = ("autonomous_local", "deterministic_debug"),
) -> int:
    """Execute strict release benchmark campaigns.

    Returns 0 on success, 1 on failure.
    """
    parser = argparse.ArgumentParser(
        description="Strict release benchmark campaign (issue #144). "
        "Strict mode is mandatory and cannot be disabled."
    )
    parser.add_argument(
        "--candidate-sha",
        type=str,
        required=True,
        help="Exact 40-character git commit SHA to benchmark (mandatory)",
    )
    parser.add_argument(
        "--campaign-dir",
        type=Path,
        default=Path("/tmp/firecrawl_strict_campaign"),
        help="Directory to write campaign artifacts "
        "(default: /tmp/firecrawl_strict_campaign)",
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=DEFAULT_DATASET,
        help="Path to the benchmark dataset JSON file",
    )
    parser.add_argument(
        "--database-url",
        type=str,
        default=os.environ.get("DATABASE_URL", ""),
        help="PostgreSQL connection string",
    )
    parser.add_argument(
        "--blob-root",
        type=Path,
        default=Path(os.environ.get("BLOB_ROOT", "/tmp/benchmark-blobs")),
        help="Path to the content-addressed blob store root",
    )
    parser.add_argument(
        "--qdrant-url",
        type=str,
        default=os.environ.get("QDRANT_URL", ""),
        help="Qdrant URL",
    )
    parser.add_argument(
        "--qdrant-api-key",
        type=str,
        default=os.environ.get("QDRANT_API_KEY", ""),
        help="Qdrant API key",
    )
    parser.add_argument(
        "--objectives",
        type=str,
        default=None,
        help="Comma-separated objective IDs to run (default: all)",
    )
    parser.add_argument(
        "--tolerance",
        type=float,
        default=0.15,
        help="Reproducibility tolerance (default: 0.15)",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="Path to write the final manifest (default: <campaign-dir>/manifest.json)",
    )
    parser.add_argument(
        "--recovery-report",
        type=Path,
        default=None,
        help="Recovery report path (default: <campaign-dir>/recovery-report.txt)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Validate configuration without executing campaigns",
    )

    args = parser.parse_args(argv)

    # ── Strict mode is mandatory ─────────────────────────────────────────
    # There is no --no-strict flag. Strict mode is always ON for release
    # campaigns per issue #144.
    strict = True

    # ── Validate inputs ──────────────────────────────────────────────────
    candidate_sha = args.candidate_sha
    if len(candidate_sha) != 40 or not re.fullmatch(r"[0-9a-f]{40}", candidate_sha):
        print(
            f"ERROR: --candidate-sha must be a full 40-character hex string; "
            f"got {len(candidate_sha)} chars: {candidate_sha!r}",
            file=sys.stderr,
        )
        return 1

    if not args.dataset.is_file():
        print(f"ERROR: dataset not found: {args.dataset}", file=sys.stderr)
        return 1

    if not args.database_url:
        print(
            "ERROR: --database-url is required (or DATABASE_URL env var)",
            file=sys.stderr,
        )
        return 1

    if args.tolerance < 0.0 or args.tolerance > 1.0:
        print(
            "ERROR: --tolerance must be between 0.0 and 1.0",
            file=sys.stderr,
        )
        return 1

    # Parse objectives
    objective_ids: tuple[str, ...] | None = None
    if args.objectives:
        objective_ids = tuple(o.strip() for o in args.objectives.split(","))

    campaign_dir = args.campaign_dir
    campaign_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("Strict Release Benchmark Campaign (issue #144)")
    print("=" * 60)
    print("  Strict mode:         ON (mandatory)")
    print(f"  Candidate SHA:       {candidate_sha}")
    print(f"  Dataset:             {args.dataset}")
    print(f"  Database URL:        {'set' if args.database_url else 'NOT SET'}")
    print(f"  Blob root:           {args.blob_root}")
    print(f"  Qdrant URL:          {args.qdrant_url or 'NOT SET'}")
    print(f"  Objectives:          {list(objective_ids) if objective_ids else 'all'}")
    print(f"  Reproducibility tolerance: {args.tolerance}")
    print("=" * 60)

    if args.dry_run:
        print("\n[Dry run] Configuration validated. No campaigns executed.")
        # Still run preflight in dry-run mode to surface infrastructure issues.
        ok, errors = _preflight_check(
            database_url=args.database_url,
            blob_root=args.blob_root,
            qdrant_url=args.qdrant_url,
            qdrant_api_key=args.qdrant_api_key,
            dataset_path=args.dataset,
            campaign_dir=campaign_dir,
            candidate_sha=candidate_sha,
        )
        if not ok:
            print("\n[Preflight] FAILED — required infrastructure unavailable:")
            for err in errors:
                print(f"  - {err}")
            return 1
        return 0

    # ── Preflight: validate infrastructure before starting campaigns ─────
    print("\n[Preflight] Checking required infrastructure...")
    ok, errors = _preflight_check(
        database_url=args.database_url,
        blob_root=args.blob_root,
        qdrant_url=args.qdrant_url,
        qdrant_api_key=args.qdrant_api_key,
        dataset_path=args.dataset,
        campaign_dir=campaign_dir,
        candidate_sha=candidate_sha,
    )
    if not ok:
        print("\n[Preflight] FAILED — required infrastructure unavailable:")
        for err in errors:
            print(f"  - {err}")
        print("\nCampaign execution aborted. Fix the above issues and retry.")
        return 1
    print("[Preflight] OK — all required infrastructure available.\n")

    # ── Execute Campaign A ───────────────────────────────────────────────
    result_a, hash_a = _run_campaign(
        campaign_label="A",
        dataset_path=args.dataset,
        database_url=args.database_url,
        blob_root=args.blob_root,
        qdrant_url=args.qdrant_url,
        qdrant_api_key=args.qdrant_api_key,
        objective_ids=objective_ids,
        strict=strict,
        reproducibility_tolerance=args.tolerance,
        campaign_dir=campaign_dir,
        candidate_sha=candidate_sha,
        execution_modes=execution_modes,
    )

    # ── Execute Campaign B ───────────────────────────────────────────────
    result_b, hash_b = _run_campaign(
        campaign_label="B",
        dataset_path=args.dataset,
        database_url=args.database_url,
        blob_root=args.blob_root,
        qdrant_url=args.qdrant_url,
        qdrant_api_key=args.qdrant_api_key,
        objective_ids=objective_ids,
        strict=strict,
        reproducibility_tolerance=args.tolerance,
        campaign_dir=campaign_dir,
        candidate_sha=candidate_sha,
        execution_modes=execution_modes,
    )

    # ── Reproducibility comparison ───────────────────────────────────────
    comparison = _compare_campaigns(
        result_a, result_b, campaign_dir, args.tolerance, args.dataset
    )

    # ── Build and write manifest ─────────────────────────────────────────
    manifest = _build_manifest(
        campaign_dir, result_a, result_b, comparison, args.dataset, candidate_sha
    )
    manifest_path = args.manifest or campaign_dir / "manifest.json"
    _write_json_atomic(manifest_path, manifest)

    # ── Summary ──────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("Campaign Summary")
    print("=" * 60)
    print(f"  Candidate SHA:       {candidate_sha}")
    print(f"  Campaign A: {result_a.campaign_id} (hash: {hash_a[:12]})")
    print(f"  Campaign B: {result_b.campaign_id} (hash: {hash_b[:12]})")
    print(f"  Reproducibility: {'PASS' if comparison.all_within_tolerance else 'FAIL'}")
    print(f"  Manifest: {manifest_path}")

    if result_a.recommendation:
        print(f"  Campaign A recommendation: {result_a.recommendation.outcome}")
    if result_b.recommendation:
        print(f"  Campaign B recommendation: {result_b.recommendation.outcome}")

    # ── Recovery report ──────────────────────────────────────────────────
    recovery_report_path = args.recovery_report or campaign_dir / "recovery-report.txt"
    recovery_lines = [
        "Recovery Report — Strict Benchmark Campaign",
        f"Candidate SHA: {candidate_sha}",
        f"Timestamp: {time.strftime('%Y-%m-%dT%H:%M:%S+00:00', time.gmtime())}",
        f"Campaign A: {result_a.campaign_id}",
        f"Campaign B: {result_b.campaign_id}",
        f"Reproducibility: {'PASS' if comparison.all_within_tolerance else 'FAIL'}",
        "Campaign A run IDs:",
        *[
            f"- {run.mode}/{run.objective_id}: {run.run_id or 'MISSING'}"
            for run in result_a.runs
        ],
        "Campaign B run IDs:",
        *[
            f"- {run.mode}/{run.objective_id}: {run.run_id or 'MISSING'}"
            for run in result_b.runs
        ],
    ]
    if result_a.recommendation:
        recovery_lines.append(
            f"Campaign A recommendation: {result_a.recommendation.outcome}"
        )
    if result_b.recommendation:
        recovery_lines.append(
            f"Campaign B recommendation: {result_b.recommendation.outcome}"
        )
    recovery_lines.append("")
    recovery_report_path.parent.mkdir(parents=True, exist_ok=True)
    recovery_report_path.write_text("\n".join(recovery_lines) + "\n", encoding="utf-8")
    manifest["recovery_report"] = {
        "path": str(recovery_report_path),
        "sha256": _compute_file_hash(recovery_report_path),
    }
    _write_json_atomic(manifest_path, manifest)
    print(f"  Recovery report: {recovery_report_path}")
    print(f"  Manifest hash: {_compute_file_hash(manifest_path)[:12]}")

    print("=" * 60)

    # Exit with failure if either campaign recommends anything other than GO,
    # or reproducibility failed.
    def is_go(rec):
        return rec and rec.outcome == "go"

    if not is_go(result_a.recommendation) or not is_go(result_b.recommendation):
        print(
            "\nFATAL: Release policy not met. (Must be unequivocally GO with reproducibility passing)"
        )
        return 1

    if not comparison.all_within_tolerance:
        print(
            "\nWARNING: Reproducibility comparison FAILED. "
            "Out-of-tolerance differences detected."
        )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
