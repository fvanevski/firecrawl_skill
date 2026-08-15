"""Temporary legacy-import facades for the Phase 1 package-boundary migration."""

from __future__ import annotations

import sys
from importlib import import_module
from importlib.abc import Loader, MetaPathFinder
from importlib.machinery import ModuleSpec
from importlib.util import spec_from_loader
from pathlib import Path
from types import ModuleType
from typing import Any

_LEGACY_TO_CANONICAL = {
    "research_store": "firecrawl_skill.research_store",
    "research_domain": "firecrawl_skill.research_domain",
}


class _AliasLoader(Loader):
    """Return one canonical module object for a requested legacy descendant."""

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


class _LegacyAliasFinder(MetaPathFinder):
    """Resolve legacy descendants to their canonical ``firecrawl_skill`` modules."""

    _firecrawl_skill_package_boundary = True

    def find_spec(
        self,
        fullname: str,
        path: object = None,
        target: ModuleType | None = None,
    ) -> ModuleSpec | None:
        del path, target
        for legacy_prefix, canonical_prefix in _LEGACY_TO_CANONICAL.items():
            if fullname.startswith(f"{legacy_prefix}."):
                canonical_name = f"{canonical_prefix}{fullname[len(legacy_prefix) :]}"
                return spec_from_loader(fullname, _AliasLoader(canonical_name))
        return None


def install_legacy_import_facades() -> None:
    """Install the bounded legacy-to-canonical descendant alias finder once."""

    if any(
        getattr(finder, "_firecrawl_skill_package_boundary", False)
        for finder in sys.meta_path
    ):
        return
    sys.meta_path.insert(0, _LegacyAliasFinder())


def expose_canonical_package(legacy_name: str, canonical_name: str) -> ModuleType:
    """Expose one canonical package object through a temporary legacy root name."""

    expected_canonical = _LEGACY_TO_CANONICAL.get(legacy_name)
    if expected_canonical != canonical_name:
        raise ImportError(
            f"unsupported package-boundary alias: {legacy_name!r} -> {canonical_name!r}"
        )

    install_legacy_import_facades()
    module = import_module(canonical_name)
    sys.modules[legacy_name] = module

    canonical_prefix = f"{canonical_name}."
    legacy_prefix = f"{legacy_name}."
    for loaded_name, loaded_module in tuple(sys.modules.items()):
        if loaded_name.startswith(canonical_prefix) and loaded_module is not None:
            suffix = loaded_name[len(canonical_prefix) :]
            sys.modules.setdefault(f"{legacy_prefix}{suffix}", loaded_module)

    return module


def load_source_implementation(
    package_globals: dict[str, Any], implementation_name: str
) -> None:
    """Execute the retained ``scripts`` implementation under its canonical name.

    ``setuptools`` packages the retained implementation tree directly under
    ``firecrawl_skill.*``. This source-checkout bootstrap gives imports from
    ``src/`` the same canonical module identity without relocating those files.
    """

    canonical_name = str(package_globals["__name__"])
    expected_canonical = _LEGACY_TO_CANONICAL.get(implementation_name)
    if expected_canonical != canonical_name:
        raise ImportError(
            f"unsupported source package bootstrap: {implementation_name!r} -> "
            f"{canonical_name!r}"
        )

    implementation_dir = (
        Path(__file__).resolve().parents[2] / "scripts" / implementation_name
    )
    init_path = implementation_dir / "__init__.py"
    if not init_path.is_file():
        raise ImportError(f"missing retained implementation package: {init_path}")

    package_globals["__path__"] = [str(implementation_dir)]
    spec = package_globals.get("__spec__")
    if spec is not None:
        spec.submodule_search_locations = [str(implementation_dir)]

    code = compile(init_path.read_bytes(), str(init_path), "exec")
    # The executed code is the fixed, repository-owned package __init__.py at
    # the deterministic path above; no external or model-generated input is used.
    exec(code, package_globals, package_globals)  # noqa: S102
