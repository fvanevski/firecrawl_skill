"""Canonical release-benchmark boundary.

``research_store.release_benchmark`` carries reviewed path-keyed Pyrefly debt.
Keep that implementation path stable in #265 rather than laundering the
baseline; this module exposes the canonical release namespace.  #269 owns
removal of the temporary bridge once the debt-bearing implementation can move.
"""

from __future__ import annotations

import sys as _sys

from .. import release_benchmark as _impl
from ..release_benchmark import *  # noqa: F403

_sys.modules[__name__] = _impl
