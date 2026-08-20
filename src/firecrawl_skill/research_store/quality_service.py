"""Temporary compatibility facade for extraction-quality assessment.

Authoritative ownership moved to ``research_store.assessment.quality`` in
issue #264.  #269 owns removal of this legacy flat import path.
"""

from .assessment.quality import QualityEvaluationError, QualityService

__all__ = ["QualityEvaluationError", "QualityService"]
