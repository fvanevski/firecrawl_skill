from __future__ import annotations

import json
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


PAYLOAD_INDEX_SCHEMAS: dict[str, str] = {
    "snapshot_id": "uuid",
    "document_id": "uuid",
    "source_id": "uuid",
    "domain": "keyword",
    "published_at": "datetime",
}


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
        result = response["result"]
        params = result["config"]["params"]
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
            "status": result.get("status"),
            "optimizer_status": result.get("optimizer_status"),
            "points_count": result.get("points_count"),
            "indexed_vectors_count": result.get("indexed_vectors_count"),
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
                "reason": (
                    "dropped incompatible collection and recreated with "
                    "dense-only schema"
                ),
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

    @staticmethod
    def _expected_payload_schemas(
        fields: dict[str, str] | list[str] | tuple[str, ...],
    ) -> dict[str, str]:
        if isinstance(fields, dict):
            return {str(field): str(schema) for field, schema in fields.items()}
        return {
            str(field): PAYLOAD_INDEX_SCHEMAS.get(str(field), "keyword")
            for field in fields
        }

    @staticmethod
    def _payload_schema_type(schema) -> str | None:
        if isinstance(schema, str):
            return schema.lower()
        if not isinstance(schema, dict):
            return None
        value = schema.get("data_type") or schema.get("type")
        if isinstance(value, str):
            return value.lower()
        known = {
            "keyword",
            "integer",
            "float",
            "bool",
            "geo",
            "datetime",
            "text",
            "uuid",
        }
        for key in schema:
            if str(key).lower() in known:
                return str(key).lower()
        return None

    def inspect_payload_indexes(
        self,
        fields: dict[str, str]
        | list[str]
        | tuple[str, ...] = PAYLOAD_INDEX_SCHEMAS,
    ) -> dict[str, dict]:
        """Read payload-index state from ``result.payload_schema`` without writes."""
        expected = self._expected_payload_schemas(fields)
        try:
            response = self._request(
                "GET", f"/collections/{quote(self.collection, safe='')}"
            )
        except HTTPError as exc:
            if exc.code == 404:
                return {
                    field: {
                        "present": False,
                        "compatible": False,
                        "expected_type": schema,
                        "actual_type": None,
                    }
                    for field, schema in expected.items()
                }
            raise
        payload_schema = response.get("result", {}).get("payload_schema", {}) or {}
        status: dict[str, dict] = {}
        for field, expected_type in expected.items():
            raw_schema = payload_schema.get(field)
            actual_type = self._payload_schema_type(raw_schema)
            present = raw_schema is not None
            status[field] = {
                "present": present,
                "compatible": present and actual_type == expected_type.lower(),
                "expected_type": expected_type.lower(),
                "actual_type": actual_type,
            }
        return status

    def ensure_payload_indexes(
        self,
        fields: dict[str, str]
        | list[str]
        | tuple[str, ...] = PAYLOAD_INDEX_SCHEMAS,
        *,
        create_missing: bool = True,
    ) -> dict[str, bool]:
        """Idempotently create *missing* payload indexes with typed schemas.

        Incompatible existing schemas are never overwritten or deleted.  Set
        ``create_missing=False`` for a read-only compatibility check.  The
        return shape is retained as ``field -> bool`` for callers that used the
        original issue-#222 helper; ``True`` means a compatible index exists.
        """
        expected = self._expected_payload_schemas(fields)
        status = self.inspect_payload_indexes(expected)
        created = False
        if create_missing:
            for field, detail in status.items():
                if detail["present"]:
                    continue
                self._request(
                    "PUT",
                    f"/collections/{quote(self.collection, safe='')}/index?wait=true",
                    {
                        "field_name": field,
                        "field_schema": expected[field],
                    },
                )
                created = True
            if created:
                status = self.inspect_payload_indexes(expected)
        return {field: detail["compatible"] for field, detail in status.items()}

    def inspect_shard_state(self) -> list[dict]:
        """Return actual local/remote shard replicas from Qdrant cluster info.

        This method never fabricates a healthy single-shard fallback.  An empty
        list therefore means the cluster endpoint returned no shard replicas.
        """
        try:
            response = self._request(
                "GET", f"/collections/{quote(self.collection, safe='')}/cluster"
            )
        except HTTPError as exc:
            if exc.code == 404:
                return [
                    {
                        "state": "missing",
                        "collection": self.collection,
                        "location": "collection",
                    }
                ]
            raise
        result = response.get("result", {}) or {}
        shards: list[dict] = []
        for location, key in (("local", "local_shards"), ("remote", "remote_shards")):
            for shard in result.get(key, []) or []:
                item = {
                    "shard_id": shard.get("shard_id"),
                    "state": str(shard.get("state", "unknown")).lower(),
                    "location": location,
                }
                if location == "local":
                    item["point_count"] = shard.get("points_count")
                else:
                    item["peer_id"] = shard.get("peer_id")
                if shard.get("shard_key") is not None:
                    item["shard_key"] = shard.get("shard_key")
                shards.append(item)
        return shards

    def inspect_shard_health(self) -> dict:
        """Return fail-closed shard/replica health from the cluster endpoint."""
        try:
            response = self._request(
                "GET", f"/collections/{quote(self.collection, safe='')}/cluster"
            )
        except HTTPError as exc:
            if exc.code == 404:
                return {
                    "healthy": False,
                    "shards": [
                        {
                            "state": "missing",
                            "collection": self.collection,
                            "location": "collection",
                        }
                    ],
                    "shard_count": 0,
                    "transfers": [],
                    "resharding_operations": [],
                }
            raise
        result = response.get("result", {}) or {}
        shards: list[dict] = []
        for location, key in (("local", "local_shards"), ("remote", "remote_shards")):
            for shard in result.get(key, []) or []:
                item = {
                    "shard_id": shard.get("shard_id"),
                    "state": str(shard.get("state", "unknown")).lower(),
                    "location": location,
                }
                if location == "local":
                    item["point_count"] = shard.get("points_count")
                else:
                    item["peer_id"] = shard.get("peer_id")
                if shard.get("shard_key") is not None:
                    item["shard_key"] = shard.get("shard_key")
                shards.append(item)
        transfers = result.get("shard_transfers", []) or []
        resharding = result.get("resharding_operations", []) or []
        states_healthy = bool(shards) and all(
            shard.get("state") == "active" for shard in shards
        )
        return {
            "healthy": states_healthy and not transfers and not resharding,
            "shards": shards,
            "shard_count": result.get("shard_count"),
            "peer_id": result.get("peer_id"),
            "transfers": transfers,
            "resharding_operations": resharding,
        }
