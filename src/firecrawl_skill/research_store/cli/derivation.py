from __future__ import annotations

from .. import derivation_admin

COMMANDS = {
    "rederive",
    "rederive-v2",
    "derivation-list",
    "derivation-activate",
    "derivation-compare",
    "normalize",
}


def run(args, config, deps) -> int:
    command = args.command
    if command == "rederive":
        print(deps.dumps(derivation_admin.rederive(config, args, deps.build_service)))
        return 0
    if command == "rederive-v2":
        return deps._cmd_rederive_v2(config, args)
    if command == "derivation-list":
        return deps._cmd_derivation_list(config, args)
    if command == "derivation-activate":
        return deps._cmd_derivation_activate(config, args)
    if command == "derivation-compare":
        return deps._cmd_derivation_compare(config, args)
    if command == "normalize":
        return deps._cmd_normalize(config, args)
    raise AssertionError(command)
