from __future__ import annotations

from .. import resource_admin, store_admin

COMMANDS = {
    "migrate",
    "status",
    "doctor",
    "endpoint-health",
    "resource-status",
    "ingest-ready",
    "parser-info",
    "ingest-result",
    "verify-blobs",
}


def run(args, config, deps) -> int:
    command = args.command
    if command == "migrate":
        print(deps.dumps(store_admin.migrate(config)))
        return 0
    if command == "status":
        result, exit_code = store_admin.status(config)
        print(deps.dumps(result))
        return exit_code
    if command == "doctor":
        checks, failed = deps._doctor(config)
        print(deps.dumps(checks))
        return 1 if failed else 0
    if command == "endpoint-health":
        health = resource_admin.endpoint_health(config)
        print(deps.dumps(health))
        has_unhealthy = any(
            endpoint.get("status") in ("unhealthy", "unknown")
            for endpoint in health.get("endpoints", [])
        )
        return 1 if has_unhealthy else 0
    if command == "resource-status":
        print(deps.dumps(resource_admin.resource_status(config)))
        return 0
    if command == "ingest-ready":
        print(deps.dumps(store_admin.ingest_ready(config)))
        return 0
    if command == "parser-info":
        print(deps.dumps(store_admin.parser_info(config)))
        return 0
    if command == "ingest-result":
        print(deps.dumps(store_admin.ingest_result(config, args, deps.build_service)))
        return 0
    if command == "verify-blobs":
        health = deps._blob_health(config)
        print(deps.dumps(health))
        return 0 if health["integrity"] == "pass" else 1
    raise AssertionError(command)
