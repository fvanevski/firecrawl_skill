"""Temporary canonical facade for the existing ``research_domain`` package.

The implementation remains under ``scripts/research_domain`` until an owning
refactor issue moves it.  Canonical and legacy imports share one module graph.
"""

from .._compat import expose_legacy_package as _expose_legacy_package

_expose_legacy_package(__name__, "research_domain")
