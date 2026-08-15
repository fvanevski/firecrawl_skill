"""Temporary compatibility facade for the legacy ``research_domain`` import root."""

from firecrawl_skill._compat import expose_canonical_package as _expose_canonical_package

_expose_canonical_package(__name__, "firecrawl_skill.research_domain")
