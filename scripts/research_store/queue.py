from __future__ import annotations

from uuid import UUID


class ValkeyQueue:
    """Transient wakeups only; PostgreSQL index_jobs remains durable."""

    def __init__(self, url: str, namespace: str = "firecrawl:research:v1"):
        self.url, self.namespace = url, namespace

    def notify(self, job_id: UUID, ttl_seconds: int = 3600):
        try:
            import redis
        except ImportError as exc:
            raise RuntimeError(
                "Valkey wakeups require the redis Python package"
            ) from exc
        client = redis.Redis.from_url(self.url)
        key = f"{self.namespace}:index-wakeup"
        pipeline = client.pipeline()
        pipeline.lpush(key, str(job_id))
        pipeline.expire(key, ttl_seconds)
        pipeline.execute()

    def prune_cache(self):
        try:
            import redis
        except ImportError as exc:
            raise RuntimeError(
                "Valkey cache pruning requires the redis Python package"
            ) from exc
        client = redis.Redis.from_url(self.url)
        deleted = 0
        for key in client.scan_iter(f"{self.namespace}:cache:*"):
            deleted += client.delete(key)
        return deleted
