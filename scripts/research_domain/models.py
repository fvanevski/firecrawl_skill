"""Compatibility facade for versioned research workflow domain contracts.

Canonical definitions live in capability-oriented modules. This module remains
as the temporary legacy import surface required by the structural-refactor
campaign.
"""

from ._catalog import CANONICAL_MODELS as _CANONICAL_MODELS
from .acquisition import *
from .assessment import *
from .release import *
from .reporting import *
from .research import *
from .telemetry import *

CANONICAL_MODELS = _CANONICAL_MODELS
