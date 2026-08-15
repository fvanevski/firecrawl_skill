"""Canonical thin research-store CLI entrypoint.

Command grammar and command-family adapters live under this package.  This
module intentionally retains a small set of historical helper names as
compatibility seams; each delegates to the canonical application/runtime
implementation rather than duplicating policy.
"""

from __future__ import annotations

import os
import sys

from .. import (
    derivation_admin,
    export_serialization,
    index_admin,
    postgres,
    resource_admin,
    run_lookup,
    store_admin,
    store_runtime,
)
from ..config import StoreConfig
from ..container import (
    build_audit_service as build_audit_service,
)
from ..container import (
    build_resource_governor as build_resource_governor,
)
from ..container import (
    build_run_service as build_run_service,
)
from ..container import (
    build_service,
)
from ..container import (
    build_workflow_operation_service as build_workflow_operation_service,
)
from ..projection_reconciliation import reconcile_projection_compat
from ..reconciliation import (
    ReconciliationError as ReconciliationError,
)
from ..reconciliation import (
    reconcile_run as reconcile_run,
)
from ..run_integrity_export import (
    EXPORT_RUN_SCHEMA_VERSIONS as EXPORT_RUN_SCHEMA_VERSIONS,
)
from ..run_integrity_export import (
    INTEGRITY_SCHEMA_VERSIONS as INTEGRITY_SCHEMA_VERSIONS,
)
from ..run_integrity_export import (
    build_integrity_report as build_integrity_report,
)
from ..run_integrity_export import (
    build_run_export as build_run_export,
)
from ..service import dumps
from ..service import json_default as json_default
from . import (
    acquisition,
    admin,
    audit,
    benchmark,
    compatibility,
    derivation,
    evidence,
    indexing,
    retrieval,
    runs,
    synthesis,
)
from .parser import parser

# Historical helper seams used by tests and transitional callers.  These names
# delegate to non-CLI implementations so compatibility does not recreate the
# former monolith.
_canonical_export_json = export_serialization.canonical_export_json
_db = store_runtime.database
_uow_factory = store_runtime.uow_factory
_qdrant = index_admin.qdrant
_worker = index_admin.worker
_index_rows = index_admin.index_rows
_active_chunk_ids = index_admin.active_chunk_ids
_derivation_filter = index_admin.derivation_filter
_index_build = index_admin.index_build
_recover_activation = index_admin.recover_activation
_activate_index = index_admin.activate_index
_qdrant_alias_state = index_admin.qdrant_alias_state
_schema_state = store_admin.schema_state
PostgresUnitOfWork = postgres.PostgresUnitOfWork


def _blob_health(config):
    return store_admin.blob_health(config, database_fn=_db)


_classify_connectivity_failure = store_admin.classify_connectivity_failure
_endpoint_health = resource_admin.endpoint_health
_resource_status = resource_admin.resource_status


def _export_json(path, payload):
    return export_serialization.export_json(path, payload, replace_fn=os.replace)


def _resolve_run_id(config, external_id):
    return run_lookup.resolve_run_id(config, external_id, database_fn=_db)


def _resolve_any_run_id(config, external_id):
    return run_lookup.resolve_any_run_id(config, external_id, database_fn=_db)


def _index_reconcile(config, repair=False):
    return reconcile_projection_compat(
        config,
        repair=repair,
        index_build=_index_build,
    )


def _doctor(config):
    return store_admin.doctor(config, sys.modules[__name__])


def _cmd_rederive_v2(config, args) -> int:
    result = derivation_admin.rederive_v2(config, args, build_service)
    print(dumps(result))
    return 0


def _cmd_derivation_list(config, args) -> int:
    print(dumps(derivation_admin.list_derivations(config, args, build_service)))
    return 0


def _cmd_derivation_activate(config, args) -> int:
    output, exit_code = derivation_admin.activate_derivation(
        config, args, build_service
    )
    for item in output:
        print(dumps(item))
    return exit_code


def _cmd_derivation_compare(config, args) -> int:
    output, exit_code = derivation_admin.compare_derivations(
        config, args, build_service
    )
    print(dumps(output))
    return exit_code


def _cmd_normalize(config, args) -> int:
    result = derivation_admin.normalize(config, args, database_fn=_db)
    print(dumps(result))
    return 1 if result.get("error") == "specify --document <uuid> or --all" else 0


_PARSED_FAMILIES = (
    admin,
    indexing,
    derivation,
    runs,
    acquisition,
    retrieval,
    evidence,
    audit,
    synthesis,
    benchmark,
)
_RAW_ARGV_FAMILIES = (compatibility,)
_FAMILIES = _PARSED_FAMILIES + _RAW_ARGV_FAMILIES


def main(argv: list[str] | None = None):
    """Parse and dispatch one research-store CLI invocation."""
    resolved = list(sys.argv[1:] if argv is None else argv)
    for family in _RAW_ARGV_FAMILIES:
        if resolved and resolved[0] in family.COMMANDS:
            return family.run_argv(resolved[0], resolved[1:], sys.modules[__name__])

    args = parser().parse_args(resolved)
    config = StoreConfig.from_env()
    for family in _PARSED_FAMILIES:
        if args.command in family.COMMANDS:
            return family.run(args, config, sys.modules[__name__])
    raise AssertionError(f"unrouted CLI command: {args.command}")
