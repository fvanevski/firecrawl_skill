"""Temporary compatibility facade for the strict release campaign.

Issue #265 moved the authoritative implementation to
``research_store.release.strict``. #269 owns removal of this legacy flat import
path after supported callers migrate.
"""

from __future__ import annotations

import sys as _sys

from .release import strict as _impl

if __name__ == "__main__":
    raise SystemExit(_impl.main())

_sys.modules[__name__] = _impl
