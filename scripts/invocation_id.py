#!/usr/bin/env python3
"""Create and validate portable Firecrawl scratch invocation IDs."""

import argparse
import os
import re
import uuid


ID_PATTERN = re.compile(r"^fc_[a-f0-9]{32}$")


def new_invocation_id():
    return f"fc_{uuid.uuid4().hex}"


def validate_invocation_id(value):
    if not ID_PATTERN.fullmatch(value or ""):
        raise ValueError("invocation ID must match fc_<32 lowercase hexadecimal characters>")
    return value


def resolve_invocation_id(value=None):
    candidate = value or os.environ.get("FIRECRAWL_INVOCATION_ID")
    return validate_invocation_id(candidate) if candidate else new_invocation_id()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validate", metavar="ID")
    args = parser.parse_args()
    try:
        print(validate_invocation_id(args.validate) if args.validate else resolve_invocation_id())
    except ValueError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
