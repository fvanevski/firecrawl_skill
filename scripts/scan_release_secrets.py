"""Fail-closed secret scan for release logs, fixtures, exports, and assets.

The scanner never serializes a matched secret value. Exact runtime credentials are
supplied by environment-variable *names* so release evidence can prove that none
of those values escaped into an artifact. A small set of high-confidence token
and private-key signatures is checked independently.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Iterable

MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_FINDINGS = 200
EXCLUDED_PARTS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
}
PATTERNS: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    (
        "private_key",
        re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    ),
    ("github_token", re.compile(rb"\bgh(?:p|o|u|s|r)_[A-Za-z0-9_]{20,}\b")),
    ("openai_style_token", re.compile(rb"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("bearer_token", re.compile(rb"(?i)\bBearer[ \t]+[A-Za-z0-9._~+/=-]{20,}")),
)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _iter_files(roots: Iterable[Path], output: Path) -> Iterable[Path]:
    output_resolved = output.resolve()
    for root in roots:
        root = root.resolve()
        if root.is_file():
            if root != output_resolved:
                yield root
            continue
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.resolve() == output_resolved:
                continue
            try:
                relative_parts = path.relative_to(root).parts
            except ValueError:
                relative_parts = path.parts
            if any(part in EXCLUDED_PARTS for part in relative_parts):
                continue
            yield path


def scan_paths(
    roots: Iterable[Path],
    *,
    output: Path,
    secret_env_names: Iterable[str] = (),
) -> dict[str, Any]:
    exact_secrets: list[tuple[str, bytes]] = []
    for name in secret_env_names:
        value = os.environ.get(name)
        if value and len(value.encode("utf-8")) >= 8:
            exact_secrets.append((name, value.encode("utf-8")))

    findings: list[dict[str, Any]] = []
    files_scanned = 0
    bytes_scanned = 0
    skipped_large: list[dict[str, Any]] = []
    seen: set[Path] = set()

    for path in _iter_files(roots, output):
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            skipped_large.append({"path": str(path), "bytes": size})
            continue
        data = path.read_bytes()
        files_scanned += 1
        bytes_scanned += len(data)

        for env_name, secret in exact_secrets:
            if secret in data:
                findings.append(
                    {
                        "path": str(path),
                        "kind": "environment_secret",
                        "source": env_name,
                    }
                )
        for kind, pattern in PATTERNS:
            if pattern.search(data):
                findings.append({"path": str(path), "kind": kind})
        if len(findings) >= MAX_FINDINGS:
            break

    report = {
        "schema_version": "release-secret-scan-v1",
        "status": "pass" if not findings and not skipped_large else "fail",
        "files_scanned": files_scanned,
        "bytes_scanned": bytes_scanned,
        "secret_env_names_checked": sorted({name for name, _ in exact_secrets}),
        "findings": findings[:MAX_FINDINGS],
        "findings_truncated": len(findings) > MAX_FINDINGS,
        "skipped_large_files": skipped_large,
        "policy": {
            "max_file_bytes": MAX_FILE_BYTES,
            "max_findings": MAX_FINDINGS,
            "exact_runtime_secrets_are_never_serialized": True,
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    output.write_text(encoded, encoding="utf-8")
    report["report_sha256"] = _sha256_bytes(encoded.encode("utf-8"))
    return report


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", action="append", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--secret-env", action="append", default=[])
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    report = scan_paths(
        args.root,
        output=args.output,
        secret_env_names=args.secret_env,
    )
    print(
        json.dumps(
            {
                "status": report["status"],
                "files_scanned": report["files_scanned"],
                "bytes_scanned": report["bytes_scanned"],
                "findings": len(report["findings"]),
                "skipped_large_files": len(report["skipped_large_files"]),
                "output": str(args.output),
                "report_sha256": report["report_sha256"],
            },
            sort_keys=True,
        )
    )
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
