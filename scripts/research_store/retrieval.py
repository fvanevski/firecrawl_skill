from __future__ import annotations

from collections import defaultdict
import json
from urllib.request import Request, urlopen


def reciprocal_rank_fusion(
    result_sets: list[list[dict]], key: str = "candidate_id", k: int = 60
) -> list[dict]:
    scores, values, reasons = defaultdict(float), {}, defaultdict(list)
    for set_index, results in enumerate(result_sets):
        for rank, item in enumerate(results, 1):
            identifier = item[key]
            scores[identifier] += 1.0 / (k + rank)
            if identifier not in values:
                values[identifier] = dict(item)
            else:
                for field, value in item.items():
                    if value is not None and field not in values[identifier]:
                        values[identifier][field] = value
            reasons[identifier].append(
                {"retriever": item.get("retriever", set_index), "rank": rank}
            )
    fused = [
        {
            **values[identifier],
            "fused_score": score,
            "match_reasons": reasons[identifier],
        }
        for identifier, score in scores.items()
    ]
    return sorted(fused, key=lambda item: (-item["fused_score"], str(item[key])))


def pack_context(
    passages: list[dict], max_tokens: int, max_passages: int
) -> list[dict]:
    if max_tokens < 0 or max_passages < 0:
        raise ValueError("budgets cannot be negative")
    packed, used = [], 0
    for passage in passages:
        tokens = int(
            passage.get("token_count")
            or max(1, (len(passage.get("text", "")) + 3) // 4)
        )
        if len(packed) >= max_passages:
            break
        if used + tokens > max_tokens:
            continue
        packed.append(passage)
        used += tokens
    return packed


def validate_relation(relation: dict) -> None:
    classes = {"observed", "source_asserted", "model_inferred"}
    if relation.get("relation_class") not in classes:
        raise ValueError("invalid relation_class")
    if not relation.get("object_id") and not relation.get("object_literal"):
        raise ValueError("relation needs an object")
    if relation["relation_class"] == "model_inferred" and not relation.get(
        "extraction_model"
    ):
        raise ValueError("model-inferred relation requires extraction provenance")
    if relation["relation_class"] != "model_inferred" and relation.get(
        "extraction_model"
    ):
        raise ValueError(
            "observed/source-asserted relation cannot masquerade as model extraction"
        )


class CohereCompatibleReranker:
    def __init__(self, url: str, model: str, api_key: str = ""):
        self.url, self.model, self.api_key = url.rstrip("/"), model, api_key

    def __call__(self, query: str, candidates: list[dict]) -> list[dict]:
        if not candidates:
            return []
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        documents = [
            item.get("excerpt") or item.get("title") or "" for item in candidates
        ]
        request = Request(
            self.url,
            data=json.dumps(
                {"model": self.model, "query": query, "documents": documents}
            ).encode(),
            headers=headers,
            method="POST",
        )
        with urlopen(request, timeout=60) as response:
            results = json.load(response).get("results", [])
        scores = {
            int(item["index"]): float(item["relevance_score"]) for item in results
        }
        reranked = [
            {**item, "reranker_score": scores.get(index)}
            for index, item in enumerate(candidates)
        ]
        return sorted(
            reranked,
            key=lambda item: (
                item.get("reranker_score") is None,
                -(item.get("reranker_score") or 0.0),
                -item.get("fused_score", 0.0),
            ),
        )
