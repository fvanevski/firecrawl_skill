"""Compatibility facade for versioned research workflow domain contracts.

Canonical definitions live in capability-oriented modules. This module remains
as the temporary legacy import surface required by the structural-refactor
campaign.
"""

from ._catalog import CANONICAL_MODELS as _CANONICAL_MODELS

# This module is intentionally a compatibility facade. Canonical definitions
# live in the capability modules; wildcard re-export is the facade contract.
from .acquisition import *  # noqa: F403
from .assessment import *  # noqa: F403
from .release import *  # noqa: F403
from .reporting import *  # noqa: F403
from .research import *  # noqa: F403
from .telemetry import *  # noqa: F403

CANONICAL_MODELS = _CANONICAL_MODELS
