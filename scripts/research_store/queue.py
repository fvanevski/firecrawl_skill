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
        except Exception:
            return False

    def wait(self, timeout_seconds: float = 5.0) -> bool:
        """Wait for at most a finite interval and then return to PostgreSQL."""
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        try:
            client = self._redis()
            if client is None:
                return False
            # redis-py accepts float timeouts on current Redis/Valkey servers.
            return client.blpop(self.wakeup_key, timeout=timeout_seconds) is not None
        except Exception:
            return False

    def prune_cache(self):
        client = self._redis()
        if client is None:
            return 0
        deleted = 0
        for key in client.scan_iter(f"{self.namespace}:cache:*"):
            deleted += client.delete(key)
        return deleted
