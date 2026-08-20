"""Deterministic classifier fixture bound to the canonical owner."""

from firecrawl_skill.research_store.acquisition.classifier import (
    PROFILES,
    classify_target,
    classify_url_type,
    main,
)

__all__ = ["PROFILES", "classify_target", "classify_url_type", "main"]
