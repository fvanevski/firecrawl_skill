from __future__ import annotations

import json
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


class QdrantIndex:
    """Rebuildable HTTP dense-vector projection; it never owns canonical text.

    PostgreSQL full-text search (FTS) acts as the lexical retriever.
    Sparse vectors are explicitly excluded until measured evaluation justifies them.
    """

    def __init__(
        self,
        url: str,
        api_key: str,
        collection: str,
        dimension: int,
        distance: str = "Cosine",
    ):
        self.url, self.api_key, self.collection, self.dimension, self.distance = (
            url.rstrip("/"),
            api_key,
            collection,
            dimension,
            distance,
        )

    def for_collection(
        self,
        collection: str,
        dimension: int | None = None,
        distance: str | None = None,
    ) -> QdrantIndex:
        return type(self)(
            self.url,
            self.api_key,
            collection,
            dimension or self.dimension,
            distance or self.distance,
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

    def inspect_schema(self) -> dict:
        """Inspect collection compatibility without creating or updating it."""
        try:
            response = self._request(
                "GET", f"/collections/{quote(self.collection, safe='')}"
            )
        except HTTPError as exc:
            if exc.code == 404:
                return {
                    "collection": self.collection,
                    "exists": False,
                    "compatible": False,
                    "expected": {
                        "size": self.dimension,
                        "distance": self.distance,
                        "sparse": False,
                    },
                }
            raise
        params = response["result"]["config"]["params"]
        vectors = params.get("vectors", {})
        vector = vectors.get("dense", vectors)
        actual = {
            "size": vector.get("size"),
            "distance": vector.get("distance"),
            "sparse": bool(params.get("sparse_vectors")),
        }
        expected = {"size": self.dimension, "distance": self.distance, "sparse": False}
        return {
            "collection": self.collection,
            "exists": True,
            "compatible": actual == expected,
            "actual": actual,
            "expected": expected,
        }

    def ensure_schema(self):
        """Ensure the collection exists with a dense-only schema.

        Qdrant is a rebuildable projection.  When the existing collection
        carries obsolete vector configuration (e.g. ``sparse_vectors``), drop
        and recreate it so the authoritative PostgreSQL state can drive a
        clean rebuild.
        """
        status = self.inspect_schema()
        if not status["exists"]:
            self._request(
                "PUT",
                f"/collections/{quote(self.collection, safe='')}",
                {
                    "vectors": {
                        "dense": {"size": self.dimension, "distance": self.distance}
                    },
                },
            )
            return {**status, "created": True, "compatible": True}
        if not status["compatible"]:
            # Sparse-vector or otherwise incompatible collection — drop and
            # recreate.  Qdrant is a rebuildable projection; PostgreSQL owns
            # the authoritative chunk state.
            self._request("DELETE", f"/collections/{quote(self.collection, safe='')}")
            self._request(
                "PUT",
                f"/collections/{quote(self.collection, safe='')}",
                {
                    "vectors": {
                        "dense": {"size": self.dimension, "distance": self.distance}
                    },
                },
            )
            return {
                **status,
                "created": True,
                "compatible": True,
                "recreated": True,
                "reason": "dropped incompatible collection and recreated with dense-only schema",
            }
        return {**status, "created": False}

    def upsert(self, points: list[dict], attempts: int = 5):
        if not points:
            return
        for attempt in range(attempts):
            try:
                self._request(
                    "PUT",
                    f"/collections/{quote(self.collection, safe='')}/points?wait=true",
                    {"points": points},
                )
                return
            except (HTTPError, URLError, TimeoutError):
                if attempt + 1 == attempts:
                    raise
                time.sleep(min(2**attempt, 10))

    def retrieve(self, ids, *, with_payload: bool = True) -> list[dict]:
        """Retrieve exact point IDs from the selected physical collection."""
        response = self._request(
            "POST",
            f"/collections/{quote(self.collection, safe='')}/points",
            {
                "ids": [str(identifier) for identifier in ids],
                "with_payload": with_payload,
                "with_vector": False,
            },
        )
        return response.get("result", [])

    def delete(self, ids):
        self._request(
            "POST",
            f"/collections/{quote(self.collection, safe='')}/points/delete?wait=true",
            {"points": [str(i) for i in ids]},
        )

    def delete_collection(self) -> None:
        self._request("DELETE", f"/collections/{quote(self.collection, safe='')}")

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
            "POST",
            f"/collections/{quote(self.collection, safe='')}/points/query",
            payload,
        )["result"]["points"]

    def point_ids(self, offset=None, limit=256, filters=None):
        payload = {
            "limit": limit,
            "with_payload": bool(filters),
            "with_vector": False,
        }
        if filters:
            payload["filter"] = filters
        if offset:
            payload["offset"] = offset
        return self._request(
            "POST",
            f"/collections/{quote(self.collection, safe='')}/points/scroll",
            payload,
        )["result"]

    def list_aliases(self) -> dict[str, str]:
        aliases = self._request("GET", "/aliases").get("result", {}).get("aliases", [])
        return {item["alias_name"]: item["collection_name"] for item in aliases}

    def switch_alias(self, alias: str, target_collection: str) -> bool:
        """Atomically repoint an alias, returning False when already active."""
        aliases = self.list_aliases()
        current = aliases.get(alias)
        if current == target_collection:
            return False
        actions = []
        if current is not None:
            actions.append({"delete_alias": {"alias_name": alias}})
        actions.append(
            {
                "create_alias": {
                    "collection_name": target_collection,
                    "alias_name": alias,
                }
            }
        )
        self._request("POST", "/collections/aliases", {"actions": actions})
        return True
