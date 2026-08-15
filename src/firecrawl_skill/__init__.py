"""Canonical Python package boundary for the Firecrawl research skill.

Phase 1 establishes this namespace without relocating the existing implementation
trees. Temporary legacy names delegate to modules whose authoritative identity is
``firecrawl_skill.*`` until the owning cleanup issue removes those facades.
"""

from ._compat import install_legacy_import_facades as _install_legacy_import_facades

_install_legacy_import_facades()
del _install_legacy_import_facades
