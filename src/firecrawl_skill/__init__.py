"""Canonical Python package boundary for the Firecrawl research skill.

Phase 1 establishes this namespace without relocating the existing implementation
packages.  The temporary compatibility layer keeps canonical and legacy imports
bound to one module graph until later refactor issues own source movement.
"""

from ._compat import install_legacy_import_facades as _install_legacy_import_facades

_install_legacy_import_facades()
del _install_legacy_import_facades
