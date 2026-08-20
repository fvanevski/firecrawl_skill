from __future__ import annotations

from .. import index_admin

COMMANDS = {
    "worker",
    "index-once",
    "index-list",
    "index-build",
    "reindex",
    "index-activate",
    "index-rollback",
    "index-prune",
    "prune-cache",
}


def run(args, config, deps) -> int:
    command = args.command
    if command in {"worker", "index-once"}:
        worker = deps._worker(config)
        if command == "index-once":
            result = worker.run_forever(batch_size=args.limit, once=True)
        else:
            worker.lease_seconds = args.lease_seconds or config.job_lease_seconds
            worker.max_attempts = args.max_attempts or config.max_index_attempts
            result = worker.run_forever(
                batch_size=args.batch_size,
                poll_seconds=args.poll_seconds or config.worker_poll_seconds,
                once=args.once,
            )
        print(deps.dumps(result))
        return 1 if result["failed"] else 0
    if command == "index-list":
        print(deps.dumps(index_admin.list_index_state(config)))
        return 0
    if command in {"index-build", "reindex"}:
        print(deps.dumps(deps._index_build(config, args.document)))
        return 0
    if command == "index-activate":
        print(deps.dumps(deps._activate_index(config, args.id, "activate")))
        return 0
    if command == "index-rollback":
        print(deps.dumps(deps._activate_index(config, args.id, "rollback")))
        return 0
    if command == "index-prune":
        print(
            deps.dumps(
                index_admin.prune_indexes(
                    config,
                    dry_run=args.dry_run,
                    force=args.force,
                    keep_last=args.keep_last,
                    index_id=args.index_id,
                )
            )
        )
        return 0
    if command == "prune-cache":
        print(deps.dumps({"deleted": index_admin.prune_cache(config)}))
        return 0
    raise AssertionError(command)
