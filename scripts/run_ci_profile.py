#!/usr/bin/env python3
"""Execute one profile from the centralized CI/test authority."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

from ci_authority import (
    AuthorityError,
    Profile,
    Selector,
    execution_selectors,
    load_profiles,
    require_sha,
    resolved_membership,
)

EXPECTED_TOOLS = {
    "pytest": "9.1.1",
    "ruff": "0.16.5",
    "pyrefly": "1.2.0",
}
EXTENSIONLESS_STATIC_TARGETS = ("scripts/fsearch_smart",)


def run(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        list(argv),
        cwd=cwd,
        check=False,
        env=dict(env) if env is not None else None,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    print(completed.stdout, end="")
    if check and completed.returncode != 0:
        raise AuthorityError(
            f"command failed ({completed.returncode}): {' '.join(argv)}"
        )
    return completed


def verify_head(repo: Path, expected: str) -> None:
    actual = run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()
    if actual != expected:
        raise AuthorityError(f"checked-out head is {actual}, expected {expected}")
    dirty = run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=repo
    ).stdout
    if dirty:
        raise AuthorityError("profile checkout is not clean before validation")


def verify_tools(repo: Path) -> None:
    commands = {
        "pytest": [sys.executable, "-m", "pytest", "--version"],
        "ruff": ["ruff", "--version"],
        "pyrefly": ["pyrefly", "--version"],
    }
    for tool, command in commands.items():
        output = run(command, cwd=repo).stdout
        version = EXPECTED_TOOLS[tool]
        if not re.search(rf"(?<![0-9]){re.escape(version)}(?![0-9])", output):
            raise AuthorityError(
                f"{tool} version authority mismatch: expected {version}, observed {output.strip()}"
            )


def parse_helper_json(stdout: str) -> dict[str, object]:
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            value = json.loads(line)
            if isinstance(value, dict):
                return value
    raise AuthorityError("disposable service helper did not emit JSON authority")


def start_services(
    repo: Path, namespace: str, profile: Profile
) -> tuple[dict[str, str], list[list[str]]]:
    env: dict[str, str] = {}
    cleanup: list[list[str]] = []
    service_set = set(profile.services)
    try:
        if service_set & {"postgres", "qdrant", "fresh-migration-db"}:
            helper = repo / "scripts/disposable-test-services"
            cleanup.append(
                [
                    str(helper),
                    "--format",
                    "json",
                    "--namespace",
                    namespace,
                    "down",
                ]
            )
            result = run(
                [
                    str(helper),
                    "--format",
                    "json",
                    "--namespace",
                    namespace,
                    "up",
                ],
                cwd=repo,
            )
            payload = parse_helper_json(result.stdout)
            helper_env = payload.get("environment")
            if not isinstance(helper_env, dict):
                raise AuthorityError("disposable helper environment is malformed")
            env.update({str(key): str(value) for key, value in helper_env.items()})
            env["DATABASE_URL"] = env["RESEARCH_STORE_TEST_DATABASE_URL"]
            if "fresh-migration-db" in service_set:
                migration_db = f"{namespace.replace('-', '_')}_migration_test"
                run(
                    [
                        "docker",
                        "exec",
                        f"{namespace}_pg",
                        "createdb",
                        "-U",
                        "postgres",
                        migration_db,
                    ],
                    cwd=repo,
                )
                pg_url = env["RESEARCH_STORE_TEST_DATABASE_URL"]
                env["FIRECRAWL_CI_MIGRATION_DATABASE_URL"] = (
                    pg_url.rsplit("/", 1)[0] + "/" + migration_db
                )
        if "valkey" in service_set:
            name = f"{namespace}_valkey"
            run(
                [
                    "docker",
                    "run",
                    "--name",
                    name,
                    "-d",
                    "-p",
                    "127.0.0.1:56379:6379",
                    "valkey/valkey:8-alpine",
                ],
                cwd=repo,
            )
            cleanup.insert(0, ["docker", "rm", "-f", name])
            ready = False
            for _ in range(30):
                result = run(
                    ["docker", "exec", name, "valkey-cli", "ping"],
                    cwd=repo,
                    check=False,
                )
                if result.returncode == 0 and "PONG" in result.stdout:
                    ready = True
                    break
                run(["sleep", "1"], cwd=repo)
            if not ready:
                raise AuthorityError("Valkey did not become ready")
            env["VALKEY_URL"] = "redis://127.0.0.1:56379/0"
        return env, cleanup
    except Exception as exc:
        if cleanup:
            try:
                stop_services(repo, cleanup)
            except AuthorityError as cleanup_exc:
                raise AuthorityError(
                    f"service startup failed and cleanup failed: {exc}; {cleanup_exc}"
                ) from cleanup_exc
        raise


def stop_services(repo: Path, cleanup: Sequence[Sequence[str]]) -> None:
    failures: list[str] = []
    expected_absent: list[str] = []
    for command in cleanup:
        command_list = list(command)
        executable = Path(command_list[0]).name if command_list else ""
        if executable == "disposable-test-services" and "--namespace" in command_list:
            index = command_list.index("--namespace")
            if index + 1 < len(command_list):
                namespace = command_list[index + 1]
                expected_absent.extend([f"{namespace}_pg", f"{namespace}_qdrant"])
        elif command_list[:3] == ["docker", "rm", "-f"] and len(command_list) == 4:
            expected_absent.append(command_list[3])
        result = run(command_list, cwd=repo, check=False)
        if result.returncode != 0:
            failures.append("cleanup command failed: " + " ".join(command_list))
    for name in sorted(set(expected_absent)):
        inspection = run(["docker", "inspect", name], cwd=repo, check=False)
        if inspection.returncode == 0:
            failures.append(f"helper-owned container remains after cleanup: {name}")
    if failures:
        raise AuthorityError("service cleanup failed: " + "; ".join(failures))


def changed_python_paths(repo: Path, base_sha: str, head_sha: str) -> list[str]:
    require_sha(base_sha, "base SHA")
    require_sha(head_sha, "head SHA")
    output = run(
        [
            "git",
            "diff",
            "--name-only",
            "--diff-filter=ACMR",
            f"{base_sha}...{head_sha}",
            "--",
        ],
        cwd=repo,
    ).stdout
    return sorted(
        path
        for path in output.splitlines()
        if Path(path).suffix in {".py", ".pyi"} and (repo / path).is_file()
    )


def run_static(repo: Path, *, base_sha: str, head_sha: str) -> None:
    changed_python = changed_python_paths(repo, base_sha, head_sha)
    if changed_python:
        run(
            ["ruff", "check", "--output-format=github", *changed_python],
            cwd=repo,
        )
        run(
            ["ruff", "format", "--check", "--diff", *changed_python],
            cwd=repo,
        )
    run(
        ["ruff", "check", "--output-format=github", *EXTENSIONLESS_STATIC_TARGETS],
        cwd=repo,
    )
    run(
        ["ruff", "format", "--check", "--diff", *EXTENSIONLESS_STATIC_TARGETS],
        cwd=repo,
    )
    run(
        ["ruff", "check", "--select", "I", *EXTENSIONLESS_STATIC_TARGETS],
        cwd=repo,
    )
    run(["pyrefly", "check", "--output-format=github"], cwd=repo)
    run(
        ["pyrefly", "check", *EXTENSIONLESS_STATIC_TARGETS, "--output-format=github"],
        cwd=repo,
    )


def run_pytest_batch(
    repo: Path,
    selectors: Sequence[Selector],
    *,
    env: Mapping[str, str],
    skip_allowlist: str,
    evidence_dir: Path,
    label: str,
) -> None:
    if not selectors:
        return
    junit = evidence_dir / f"pytest-{label}.xml"
    skips = evidence_dir / f"pytest-{label}-skips.json"
    argv = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-ra",
        "-p",
        "no:cacheprovider",
        "--import-mode=importlib",
        f"--junitxml={junit}",
    ]
    for selector in selectors:
        argv.extend(selector.argv())
    run(argv, cwd=repo, env=env)
    verifier_argv = [
        sys.executable,
        "scripts/verify_pytest_skips.py",
        "--junitxml",
        str(junit),
        "--allowlist",
        skip_allowlist,
        "--output",
        str(skips),
    ]
    for selector in sorted({item.path for item in selectors}):
        verifier_argv.extend(["--scope-selector", selector])
    run(verifier_argv, cwd=repo, env=env)


def run_pytest_profile(
    repo: Path,
    profile: Profile,
    selectors: Sequence[Selector],
    *,
    namespace: str,
    skip_allowlist: str,
) -> None:
    service_env: dict[str, str] = {}
    cleanup: list[list[str]] = []
    temp_root = Path(os.environ.get("RUNNER_TEMP", tempfile.gettempdir()))
    evidence_dir = temp_root / f"ci-profile-{profile.name}-{namespace}"
    evidence_dir.mkdir(parents=True, exist_ok=False)
    runtime_env = dict(os.environ)
    runtime_env["BLOB_ROOT"] = str(evidence_dir / "blobs")
    Path(runtime_env["BLOB_ROOT"]).mkdir()
    runtime_env["PYTHONDONTWRITEBYTECODE"] = "1"
    runtime_env.setdefault("EMBEDDING_MODEL", "ci-deterministic")
    runtime_env.setdefault("EMBEDDING_REVISION", "test")
    runtime_env.setdefault("EMBEDDING_DIMENSION", "4")
    runtime_env.setdefault("FIRECRAWL_RELEASE_DETERMINISTIC_FIXTURES", "1")
    try:
        service_env, cleanup = start_services(repo, namespace, profile)
        runtime_env.update(service_env)
        executable = execution_selectors(selectors)
        plain = [item for item in executable if item.keyword is None]
        run_pytest_batch(
            repo,
            plain,
            env=runtime_env,
            skip_allowlist=skip_allowlist,
            evidence_dir=evidence_dir,
            label="plain",
        )
        keyword_groups: dict[str, list[Selector]] = {}
        for selector in executable:
            if selector.keyword is not None:
                keyword_groups.setdefault(selector.keyword, []).append(selector)
        for index, (_, group) in enumerate(sorted(keyword_groups.items()), start=1):
            run_pytest_batch(
                repo,
                group,
                env=runtime_env,
                skip_allowlist=skip_allowlist,
                evidence_dir=evidence_dir,
                label=f"filtered-{index:03d}",
            )
        manifest = {
            "schema_version": "ci-profile-evidence-v1",
            "profile": profile.name,
            "namespace": namespace,
            "services": list(profile.services),
            "membership": [item.expression for item in selectors],
            "execution_membership": [item.expression for item in executable],
        }
        (evidence_dir / "profile.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"CI_PROFILE_EVIDENCE={evidence_dir}")
    finally:
        if cleanup:
            stop_services(repo, cleanup)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--base-sha")
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--namespace", required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    repo = Path(args.repo).resolve()
    try:
        head_sha = require_sha(args.head_sha, "head SHA")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,47}", args.namespace):
            raise AuthorityError("namespace must match [a-z0-9][a-z0-9_-]{0,47}")
        verify_head(repo, head_sha)
        verify_tools(repo)
        profiles, _, skip_allowlist = load_profiles(repo)
        if args.profile not in profiles:
            raise AuthorityError(f"unknown profile: {args.profile}")
        membership, _ = resolved_membership(repo, head_sha=head_sha)
        profile = profiles[args.profile]
        if profile.kind == "static":
            if args.base_sha is None:
                raise AuthorityError("static profile requires --base-sha")
            base_sha = require_sha(args.base_sha, "base SHA")
            run_static(repo, base_sha=base_sha, head_sha=head_sha)
        else:
            run_pytest_profile(
                repo,
                profile,
                membership[profile.name],
                namespace=args.namespace,
                skip_allowlist=skip_allowlist,
            )
        verify_head(repo, head_sha)
        return 0
    except (AuthorityError, json.JSONDecodeError) as exc:
        print(f"run-ci-profile: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
