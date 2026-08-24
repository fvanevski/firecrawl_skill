"""Dedicated bounded retry policy for candidate first-byte timeouts."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

FIRST_BYTE_TIMEOUT_RETRIES_ENV = "FIRECRAWL_EXTRACTION_FIRST_BYTE_TIMEOUT_RETRIES"


def _env_retry_count(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value


@dataclass(frozen=True)
class FirstByteTimeoutRetryPolicy:
    """Retry budget dedicated to ``first_byte_timeout`` only.

    This budget is intentionally independent from generic transport/HTTP retries.
    The adapter still enforces the existing overall-candidate wall-clock deadline,
    so configuring retries never authorizes unbounded provider work.
    """

    retries: int = 1

    def __post_init__(self) -> None:
        if self.retries < 0:
            raise ValueError("first-byte timeout retry count must be non-negative")

    @classmethod
    def from_env(cls) -> FirstByteTimeoutRetryPolicy:
        return cls(retries=_env_retry_count(FIRST_BYTE_TIMEOUT_RETRIES_ENV, 1))

    def to_dict(self) -> dict[str, Any]:
        return {"first_byte_timeout_retries": self.retries}


__all__ = ["FIRST_BYTE_TIMEOUT_RETRIES_ENV", "FirstByteTimeoutRetryPolicy"]
