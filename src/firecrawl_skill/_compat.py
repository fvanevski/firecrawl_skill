"""Temporary import facades for the Phase 1 package-boundary migration."""

from __future__ import annotations

import sys
from importlib import import_module
from importlib.abc import Loader, MetaPathFinder
from importlib.machinery import ModuleSpec
from importlib.util import spec_from_loader
from types import ModuleType
from typing import Any

_CANONICAL_TO_LEGACY = {
    "firecrawl_skill.research_store": "research_store",
    "firecrawl_skill.research_domain": "research_domain",
}


class _AliasLoader(Loader):
    """Return one authoritative legacy module under a canonical alias."""

    def __init__(self, target_name: str) -> None:
        self._target_name = target_name
        self._metadata: tuple[Any, ...] | None = None

    def create_module(self, spec: ModuleSpec) -> ModuleType:
        del spec
        module = import_module(self._target_name)
        self._metadata = (
            module.__name__,
            module.__package__,
            module.__spec__,
            module.__loader__,
            getattr(module, "__cached__", None),
        )
        return module

    def exec_module(self, module: ModuleType) -> None:
        if self._metadata is None:
            return
        (
            module.__name__,
            module.__package__,
            module.__spec__,
            module.__loader__,
            module.__cached__,
        ) = self._metadata


class _CanonicalAliasFinder(MetaPathFinder):
    """Resolve canonical descendants to the existing implementation modules."""

    _firecrawl_skill_package_boundary = True

    def find_spec(
        self,
        fullname: str,
        path: object = None,
        target: ModuleType | None = None,
    ) -> ModuleSpec | None:
        del path, target
        for canonical_prefix, legacy_prefix in _CANONICAL_TO_LEGACY.items():
            if fullname.startswith(f"{canonical_prefix}."):
                legacy_name = f"{legacy_prefix}{fullname[len(canonical_prefix):]}"
                return spec_from_loader(fullname, _AliasLoader(legacy_name))
        return None


def install_legacy_import_facades() -> None:
    """Install the bounded canonical-to-legacy descendant alias finder once."""

    if any(
        getattr(finder, "_firecrawl_skill_package_boundary", False)
        for finder in sys.meta_path
    ):
        return
    sys.meta_path.insert(0, _CanonicalAliasFinder())


def expose_legacy_package(canonical_name: str, legacy_name: str) -> ModuleType:
    """Expose one legacy package as the canonical package object without copying it."""

    expected_legacy = _CANONICAL_TO_LEGACY.get(canonical_name)
    if expected_legacy != legacy_name:
        raise ImportError(
            f"unsupported package-boundary alias: {canonical_name!r} -> {legacy_name!r}"
        )

    install_legacy_import_facades()
    module = import_module(legacy_name)
    sys.modules[canonical_name] = module

    legacy_prefix = f"{legacy_name}."
    canonical_prefix = f"{canonical_name}."
    for loaded_name, loaded_module in tuple(sys.modules.items()):
        if loaded_name.startswith(legacy_prefix) and loaded_module is not None:
            suffix = loaded_name[len(legacy_prefix) :]
            sys.modules.setdefault(f"{canonical_prefix}{suffix}", loaded_module)

    return module
