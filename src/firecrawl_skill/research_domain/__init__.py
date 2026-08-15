"""Canonical source-checkout bootstrap for the retained research-domain implementation."""

from .._compat import load_source_implementation as _load_source_implementation

_load_source_implementation(globals(), "research_domain")
del _load_source_implementation
