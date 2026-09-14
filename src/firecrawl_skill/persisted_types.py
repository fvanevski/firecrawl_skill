"""Canonical registry for persisted workflow types shared across layers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

_MEMBER_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_PERSISTED_VALUE_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_POSTGRES_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_REVISION_RE = re.compile(r"^[0-9]{4}_[a-z0-9_]+$")


class PersistedTypeRegistryError(ValueError):
    """A persisted type registry or PostgreSQL projection is invalid."""


@dataclass(frozen=True)
class PersistedTypeValue:
    """One stable cross-layer value and the migration that introduced it."""

    member_name: str
    persisted_value: str
    introduced_in_revision: str
    introduced_in_registry_version: int


@dataclass(frozen=True)
class PostgresEnumAddition:
    """One ordered PostgreSQL enum addition required by a registry transition."""

    persisted_value: str
    introduced_in_revision: str
    after: str | None
    before: str | None


@dataclass(frozen=True)
class PersistedTypeRegistry:
    """Versioned canonical values for one PostgreSQL-backed workflow type."""

    key: str
    postgres_type: str
    current_version: int
    values: tuple[PersistedTypeValue, ...]

    def __post_init__(self) -> None:
        if not _POSTGRES_TYPE_RE.fullmatch(self.key):
            raise PersistedTypeRegistryError(f"invalid registry key: {self.key!r}")
        if not _POSTGRES_TYPE_RE.fullmatch(self.postgres_type):
            raise PersistedTypeRegistryError(
                f"invalid PostgreSQL type name: {self.postgres_type!r}"
            )
        if self.current_version < 1:
            raise PersistedTypeRegistryError("current_version must be positive")
        if not self.values:
            raise PersistedTypeRegistryError(
                "persisted type registry must not be empty"
            )

        member_names: set[str] = set()
        persisted_values: set[str] = set()
        for item in self.values:
            if not _MEMBER_NAME_RE.fullmatch(item.member_name):
                raise PersistedTypeRegistryError(
                    f"invalid member name: {item.member_name!r}"
                )
            if not _PERSISTED_VALUE_RE.fullmatch(item.persisted_value):
                raise PersistedTypeRegistryError(
                    f"invalid persisted value: {item.persisted_value!r}"
                )
            if not _REVISION_RE.fullmatch(item.introduced_in_revision):
                raise PersistedTypeRegistryError(
                    f"invalid migration revision: {item.introduced_in_revision!r}"
                )
            if not 1 <= item.introduced_in_registry_version <= self.current_version:
                raise PersistedTypeRegistryError(
                    "introduced registry version must be within the declared "
                    f"registry range: {item}"
                )
            if item.member_name in member_names:
                raise PersistedTypeRegistryError(
                    f"duplicate member name: {item.member_name}"
                )
            if item.persisted_value in persisted_values:
                raise PersistedTypeRegistryError(
                    f"duplicate persisted value: {item.persisted_value}"
                )
            member_names.add(item.member_name)
            persisted_values.add(item.persisted_value)

        versions = {item.introduced_in_registry_version for item in self.values}
        expected_versions = set(range(1, self.current_version + 1))
        if versions != expected_versions:
            raise PersistedTypeRegistryError(
                "registry versions must be contiguous and each introduce at least "
                f"one value: expected={sorted(expected_versions)} "
                f"observed={sorted(versions)}"
            )

    def _resolve_version(self, version: int | None) -> int:
        resolved = self.current_version if version is None else version
        if not 1 <= resolved <= self.current_version:
            raise PersistedTypeRegistryError(
                f"unsupported {self.key} registry version: {resolved}"
            )
        return resolved

    def entries(self, version: int | None = None) -> tuple[PersistedTypeValue, ...]:
        resolved = self._resolve_version(version)
        return tuple(
            item
            for item in self.values
            if item.introduced_in_registry_version <= resolved
        )

    def persisted_values(self, version: int | None = None) -> tuple[str, ...]:
        return tuple(item.persisted_value for item in self.entries(version))

    def enum_members(self, version: int | None = None) -> tuple[tuple[str, str], ...]:
        return tuple(
            (item.member_name, item.persisted_value) for item in self.entries(version)
        )

    def member_value(self, member_name: str, version: int | None = None) -> str:
        for item in self.entries(version):
            if item.member_name == member_name:
                return item.persisted_value
        raise PersistedTypeRegistryError(
            f"{member_name!r} is not registered for {self.key}"
        )

    def required_migration_revisions(
        self, version: int | None = None
    ) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(item.introduced_in_revision for item in self.entries(version))
        )

    def validate_migration_revisions(
        self, revisions: Iterable[str], version: int | None = None
    ) -> None:
        available = set(revisions)
        missing = [
            revision
            for revision in self.required_migration_revisions(version)
            if revision not in available
        ]
        if missing:
            raise PersistedTypeRegistryError(
                f"{self.key} registry references missing migrations: {missing}"
            )

    def version_for_postgres_values(self, values: Iterable[str]) -> int:
        observed = tuple(values)
        matches = [
            version
            for version in range(1, self.current_version + 1)
            if self.persisted_values(version) == observed
        ]
        if len(matches) != 1:
            raise PersistedTypeRegistryError(
                f"{self.postgres_type} values are not an exact registry projection: "
                f"{observed}"
            )
        return matches[0]

    def postgres_delta(
        self, values: Iterable[str], target_version: int | None = None
    ) -> tuple[PostgresEnumAddition, ...]:
        source_version = self.version_for_postgres_values(values)
        target = self._resolve_version(target_version)
        if source_version > target:
            raise PersistedTypeRegistryError(
                f"cannot migrate {self.key} backward from registry version "
                f"{source_version} to {target}"
            )

        source_values = set(self.persisted_values(source_version))
        target_entries = self.entries(target)
        additions: list[PostgresEnumAddition] = []
        for index, item in enumerate(target_entries):
            if item.persisted_value in source_values:
                continue
            after = target_entries[index - 1].persisted_value if index > 0 else None
            before = None
            if after is None:
                before = next(
                    (
                        candidate.persisted_value
                        for candidate in target_entries[index + 1 :]
                        if candidate.persisted_value in source_values
                    ),
                    None,
                )
            if after is None and before is None:
                raise PersistedTypeRegistryError(
                    f"cannot anchor leading {self.key} value: {item.persisted_value}"
                )
            additions.append(
                PostgresEnumAddition(
                    persisted_value=item.persisted_value,
                    introduced_in_revision=item.introduced_in_revision,
                    after=after,
                    before=before,
                )
            )
        return tuple(additions)

    def postgres_transition_sql(
        self, from_version: int, to_version: int
    ) -> tuple[str, ...]:
        source_values = self.persisted_values(from_version)
        additions = self.postgres_delta(source_values, to_version)
        statements: list[str] = [self.postgres_assertion_sql(from_version)]
        for addition in additions:
            statement = (
                f"ALTER TYPE {self.postgres_type} "
                f"ADD VALUE '{addition.persisted_value}'"
            )
            if addition.after is not None:
                statement += f" AFTER '{addition.after}'"
            elif addition.before is not None:
                statement += f" BEFORE '{addition.before}'"
            statements.append(statement + ";")
        if to_version != from_version:
            statements.append(self.postgres_assertion_sql(to_version))
        return tuple(statements)

    def postgres_assertion_sql(self, version: int | None = None) -> str:
        expected = self.persisted_values(version)
        array_literal = ", ".join(f"'{value}'" for value in expected)
        return f"""DO $$
DECLARE
    observed text[];
BEGIN
    SELECT array_agg(enumlabel ORDER BY enumsortorder)
      INTO observed
      FROM pg_enum
     WHERE enumtypid = '{self.postgres_type}'::regtype;

    IF observed IS DISTINCT FROM ARRAY[{array_literal}]::text[] THEN
        RAISE EXCEPTION
            '{self.postgres_type} registry mismatch: expected %, observed %',
            ARRAY[{array_literal}]::text[],
            observed;
    END IF;
END
$$;"""


COVERAGE_ITEM_TYPE = PersistedTypeRegistry(
    key="coverage_item_type",
    postgres_type="coverage_item_type",
    current_version=2,
    values=(
        PersistedTypeValue("QUESTION", "question", "0012_coverage_events", 1),
        PersistedTypeValue("CLAIM", "claim", "0012_coverage_events", 1),
        PersistedTypeValue(
            "SOURCE_REQUIREMENT",
            "source_requirement",
            "0012_coverage_events",
            1,
        ),
        PersistedTypeValue(
            "EXACT_SOURCE_REQUIREMENT",
            "exact_source_requirement",
            "0046_exact_source_coverage_item",
            2,
        ),
        PersistedTypeValue(
            "FRESHNESS_REQUIREMENT",
            "freshness_requirement",
            "0012_coverage_events",
            1,
        ),
        PersistedTypeValue(
            "CORROBORATION_REQUIREMENT",
            "corroboration_requirement",
            "0012_coverage_events",
            1,
        ),
        PersistedTypeValue(
            "CONTRADICTION_REQUIREMENT",
            "contradiction_requirement",
            "0012_coverage_events",
            1,
        ),
    ),
)


__all__ = [
    "COVERAGE_ITEM_TYPE",
    "PersistedTypeRegistry",
    "PersistedTypeRegistryError",
    "PersistedTypeValue",
    "PostgresEnumAddition",
]
