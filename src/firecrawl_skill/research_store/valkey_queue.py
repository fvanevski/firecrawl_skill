from __future__ import annotations

from uuid import UUID


class ValkeyQueue:
    """Best-effort wakeups only; PostgreSQL index_jobs remains durable."""

    def __init__(
        self,
        url: str,
        namespace: str = "firecrawl:research:v1",
        *,
        client=None,
    ):
        self.url, self.namespace, self._client = url, namespace, client

    @property
    def wakeup_key(self) -> str:
        return f"{self.namespace}:index-wakeup"

    def _redis(self):
        if self._client is not None:
            return self._client
        if not self.url:
            return None
        try:
            import redis
        except ImportError:
            return None
        self._client = redis.Redis.from_url(self.url)
        return self._client

    def notify(self, job_id: UUID, ttl_seconds: int = 3600) -> bool:
        """Publish a transient LPUSH wakeup without affecting corpus success."""
        try:
            client = self._redis()
            if client is None:
                return False
            pipeline = client.pipeline()
            pipeline.lpush(self.wakeup_key, str(job_id))
            pipeline.expire(self.wakeup_key, ttl_seconds)
            pipeline.execute()
            return True
        except Exception:  # noqa: BLE001
            return False

    def pop(self, timeout_seconds: float = 5.0) -> UUID | None:
        """Pop and decode one exact wakeup token from this queue namespace."""
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        try:
            client = self._redis()
            if client is None:
                return None
            result = client.blpop(self.wakeup_key, timeout=timeout_seconds)
            if result is None:
                return None
            raw = result[1]
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            return UUID(str(raw))
        except Exception:  # noqa: BLE001
            return None

    def wait(self, timeout_seconds: float = 5.0) -> bool:
        """Wait for at most a finite interval and then return to PostgreSQL."""
        return self.pop(timeout_seconds) is not None

    def round_trip(self, job_id: UUID, timeout_seconds: float = 5.0) -> UUID | None:
        """Push and pop one exact token, suitable for isolated preflight keys."""
        if not self.notify(job_id):
            return None
        return self.pop(timeout_seconds)

    def clear(self) -> bool:
        """Delete this namespace's wakeup key without touching other queues."""
        try:
            client = self._redis()
            return bool(client is not None and client.delete(self.wakeup_key))
        except Exception:  # noqa: BLE001
            return False

    def discard(self, job_id: UUID) -> int:
        """Remove one exact wakeup token without consuming unrelated work."""
        try:
            client = self._redis()
            if client is None:
                return 0
            return int(client.lrem(self.wakeup_key, 0, str(job_id)))
        except Exception:  # noqa: BLE001
            return 0

    def prune_cache(self):
        client = self._redis()
        if client is None:
            return 0
        deleted = 0
        for key in client.scan_iter(f"{self.namespace}:cache:*"):
            deleted += client.delete(key)
        return deleted
