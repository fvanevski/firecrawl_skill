"""Canonical report construction, validation, and persistence slice.

Use the explicit submodules ``reporting.construction``,
``reporting.validation``, and ``reporting.artifacts``.  The package does not
eagerly import those services because temporary #269 compatibility facades
still route legacy flat imports into this namespace and eager imports would
create partially initialized legacy-module cycles.
"""

__all__: list[str] = []
