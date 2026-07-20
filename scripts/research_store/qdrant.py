from __future__ import annotations

import json
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class QdrantIndex:
    """Rebuildable HTTP projection; it never owns canonical text."""

    def __init__(self, url: str, api_key: str, collection: str, dimension: int):
        self.url, self.api_key, self.collection, self.dimension = (
            url.rstrip("/"),
            api_key,
            collection,
            dimension,
        )

    def _request(self, method: str, path: str, payload=None):
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["api-key"] = self.api_key
        data = json.dumps(payload).encode() if payload is not None else None
        with urlopen(
            Request(self.url + path, data=data, headers=headers, method=method),
            timeout=15,
        ) as response:
            return json.load(response)

    def ensure_schema(self):
        try:
            result = self._request("GET", f"/collections/{self.collection}")["result"][
                "config"
            ]["params"]["vectors"]
            vector = result.get("dense", result)
            if (
                vector.get("size") != self.dimension
                or vector.get("distance") != "Cosine"
            ):
                raise RuntimeError("Qdrant collection schema is incompatible")
        except HTTPError as exc:
            if exc.code != 404:
                raise
            self._request(
                "PUT",
                f"/collections/{self.collection}",
                {
                    "vectors": {
                        "dense": {"size": self.dimension, "distance": "Cosine"}
                    },
                    "sparse_vectors": {"sparse": {}},
                },
            )

    def upsert(self, points: list[dict], attempts: int = 5):
        for attempt in range(attempts):
            try:
                self._request(
                    "PUT",
                    f"/collections/{self.collection}/points?wait=true",
                    {"points": points},
                )
                return
            except (HTTPError, URLError, TimeoutError):
                if attempt + 1 == attempts:
                    raise
                time.sleep(min(2**attempt, 10))

    def delete(self, ids):
        self._request(
            "POST",
            f"/collections/{self.collection}/points/delete?wait=true",
            {"points": [str(i) for i in ids]},
        )

    def search(self, vector, filters, limit):
        payload = {
            "query": vector,
            "using": "dense",
            "limit": limit,
            "with_payload": True,
        }
        if filters:
            payload["filter"] = filters
        return self._request(
            "POST", f"/collections/{self.collection}/points/query", payload
        )["result"]["points"]

    def point_ids(self, offset=None, limit=256):
        payload = {"limit": limit, "with_payload": False, "with_vector": False}
        if offset:
            payload["offset"] = offset
        return self._request(
            "POST", f"/collections/{self.collection}/points/scroll", payload
        )["result"]
