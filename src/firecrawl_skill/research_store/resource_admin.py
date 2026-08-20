"""Model-endpoint and resource-governance administrative views."""

from __future__ import annotations

import urllib.request
from typing import Any

from firecrawl_skill.research_store.retrieval.projection.indexing import (
    OpenAICompatibleEmbedder,
)

from .retrieval import CohereCompatibleReranker
from .store_runtime import uow_factory


def endpoint_health(config) -> dict[str, Any]:
    result: dict[str, Any] = {"endpoints": []}
    try:
        with uow_factory(config)() as uow:
            rows = uow.model_endpoints.list_endpoints()
            for row in rows:
                result["endpoints"].append(
                    {
                        "endpoint_name": row["endpoint_name"],
                        "url": row["url"],
                        "status": row["status"],
                        "last_check_at": row["last_check_at"],
                        "last_error": row["last_error"],
                        "concurrent_requests": row["concurrent_requests"],
                        "queued_requests": row["queued_requests"],
                        "total_checks": row["total_checks"],
                        "total_failures": row["total_failures"],
                        "restart_count": row["restart_count"],
                    }
                )
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"failed to query health store: {exc}"

    if config.embedding_url:
        try:
            vector = OpenAICompatibleEmbedder(
                config.embedding_url,
                config.embedding_model,
                config.embedding_api_key,
                config.embedding_dimension,
            )("resource-governance-health-check")
            result["endpoints"].append(
                {
                    "endpoint_name": "embedding",
                    "url": config.embedding_url,
                    "status": "healthy",
                    "dimension": len(vector),
                    "live_probe": True,
                }
            )
        except Exception as exc:  # noqa: BLE001
            result["endpoints"].append(
                {
                    "endpoint_name": "embedding",
                    "url": config.embedding_url,
                    "status": "unhealthy",
                    "error": str(exc),
                    "live_probe": True,
                }
            )

    if config.reranker_url:
        try:
            ranked = CohereCompatibleReranker(
                config.reranker_url,
                config.reranker_model,
                config.reranker_api_key,
            )(
                "resource-governance-health-check",
                [
                    {"candidate_id": "relevant", "excerpt": "health check"},
                    {"candidate_id": "other", "excerpt": "noise"},
                ],
            )
            if not ranked or ranked[0]["candidate_id"] != "relevant":
                raise RuntimeError("unexpected reranker ordering")
            result["endpoints"].append(
                {
                    "endpoint_name": "reranker",
                    "url": config.reranker_url,
                    "status": "healthy",
                    "live_probe": True,
                }
            )
        except Exception as exc:  # noqa: BLE001
            result["endpoints"].append(
                {
                    "endpoint_name": "reranker",
                    "url": config.reranker_url,
                    "status": "unhealthy",
                    "error": str(exc),
                    "live_probe": True,
                }
            )

    if config.generative_url:
        try:
            model_url = config.generative_url.rstrip("/") + "/models"
            req = urllib.request.Request(model_url, method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"unexpected status {resp.status}")
            result["endpoints"].append(
                {
                    "endpoint_name": "generative",
                    "url": config.generative_url,
                    "status": "healthy",
                    "live_probe": True,
                }
            )
        except Exception as exc:  # noqa: BLE001
            result["endpoints"].append(
                {
                    "endpoint_name": "generative",
                    "url": config.generative_url,
                    "status": "unhealthy",
                    "error": str(exc),
                    "live_probe": True,
                }
            )
    else:
        result["endpoints"].append(
            {
                "endpoint_name": "generative",
                "url": "",
                "status": "unknown",
                "note": "GENERATIVE_URL not configured",
            }
        )
    return result


def resource_status(config) -> dict[str, Any]:
    status: dict[str, Any] = {
        "configuration": {
            "generative": {
                "max_concurrent": config.generative_max_concurrent,
                "max_input_tokens": config.generative_max_input_tokens,
                "max_batch_size": config.generative_max_batch_size,
                "health_check_interval": config.generative_health_check_interval,
                "backpressure_threshold": config.generative_backpressure_threshold,
                "token_cap": config.generative_token_cap,
            },
            "embedding": {
                "max_concurrent": config.embedding_max_concurrent,
                "max_batch_size": config.embedding_max_batch_size,
                "health_check_interval": config.embedding_health_check_interval,
                "backpressure_threshold": config.embedding_backpressure_threshold,
            },
            "reranker": {
                "max_concurrent": config.reranker_max_concurrent,
                "max_batch_size": config.reranker_max_batch_size,
                "health_check_interval": config.reranker_health_check_interval,
                "backpressure_threshold": config.reranker_backpressure_threshold,
            },
        },
        "endpoints": [],
    }
    try:
        with uow_factory(config)() as uow:
            rows = uow.model_endpoints.list_endpoints()
            for row in rows:
                status["endpoints"].append(
                    {
                        "endpoint_name": row["endpoint_name"],
                        "url": row["url"],
                        "status": row["status"],
                        "concurrent_requests": row["concurrent_requests"],
                        "queued_requests": row["queued_requests"],
                        "restart_count": row["restart_count"],
                    }
                )
    except Exception as exc:  # noqa: BLE001
        status["error"] = f"failed to query health store: {exc}"
    return status
