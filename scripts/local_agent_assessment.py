"""Deterministic exact-SHA host-evidence runner.

The controlling checkout owns this module, its profiles, dependency locks, the
disposable-service helper, and skip policy.  Candidate source is mounted only
through a detached worktree and never supplies orchestration commands.
"""

from __future__ import annotations

import argparse
import ast
import ctypes
import errno
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import tomllib
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

SCHEMA_VERSION = "local-agent-assessment-v1"
PROFILE_SCHEMA_VERSION = 1
SERVICE_SCHEMA_VERSION = "firecrawl-disposable-services-v1"
LIFECYCLE_SCHEMA_VERSION = "local-agent-assessment-lifecycle-v1"
CONTROL_COMMAND_TIMEOUT_SECONDS = 300
PROCESS_TERMINATION_GRACE_SECONDS = 5.0
PROCESS_CONTAINMENT_FAILURE_RETURN_CODE = 125
PR_SET_CHILD_SUBREAPER = 36
PROC_ROOT = Path("/proc")
HOST_ASSESSMENT_LEASE_LABEL = "firecrawl-skill-local-agent-assessment-v1"
ALLOWED_PYTHONS = {"3.12"}
SERVICE_ENV_KEYS = {
    "RESEARCH_STORE_TEST_DATABASE_URL",
    "RESEARCH_STORE_TEST_ALLOW_RESET",
    "QDRANT_URL",
    "RESEARCH_STORE_TEST_QDRANT_URL",
    "RESEARCH_STORE_TEST_QDRANT_ALLOW_RESET",
}
SERVICE_IMAGE_KEYS = {"postgres", "qdrant"}
PROFILE_ENV_KEYS = {
    "EMBEDDING_MODEL",
    "EMBEDDING_REVISION",
    "EMBEDDING_DIMENSION",
    "FIRECRAWL_RELEASE_DETERMINISTIC_FIXTURES",
}
ALLOWED_PR_TEST_ROOTS = (
    "tests/unit",
    "tests/integration",
    "tests/contract",
    "tests/acceptance",
)
PR_TEST_CONTROL_PATHS = (
    "conftest.py",
    "scripts/conftest.py",
    "scripts/qdrant_test_support.py",
)
PR_NUMBER_MAX = 2_147_483_647
ASSESSMENT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SELECTOR_RE = re.compile(
    r"^tests/(?:unit|integration|contract|acceptance)/"
    r"[A-Za-z0-9_./-]+\.py(?:::[A-Za-z0-9_\[\]./-]+)*$"
)
EXIT_CODES = {
    "PASS": 0,
    "FAIL": 1,
    "BLOCKED": 2,
    "STALE": 3,
    "INFRA_ERROR": 4,
    "ISOLATION_BREACH": 5,
}
CANDIDATE_TEST_SOURCE_MANIFEST_ENV = "LOCAL_AGENT_CANDIDATE_TEST_SOURCE_MANIFEST"
CANDIDATE_TEST_SOURCE_MANIFEST_SHA256_ENV = (
    "LOCAL_AGENT_CANDIDATE_TEST_SOURCE_MANIFEST_SHA256"
)
CANDIDATE_TEST_SOURCE_SHA_ENV = "LOCAL_AGENT_CANDIDATE_TEST_SOURCE_SHA"
PYTEST_SKIP_ALLOWLIST_PATH = Path("references/pytest-skip-allowlist.json")
PYTEST_SKIP_VERIFIER_PATH = Path("scripts/verify_pytest_skips.py")
CANDIDATE_PYTEST_LAUNCHER = r"""
import hashlib
import importlib.abc
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest
from _pytest import pathlib as _pytest_pathlib
from _pytest import python as _pytest_python

_candidate_root = os.path.abspath(os.getcwd())
_blocked = frozenset(json.loads(sys.argv[1]))
_selected_blocked = {
    argument.split("::", 1)[0]
    for argument in sys.argv[2:]
    if argument.split("::", 1)[0] in _blocked
}
if len(_selected_blocked) > 1:
    raise RuntimeError(
        "candidate pytest process cannot select multiple changed test modules"
    )
if _candidate_root not in sys.path:
    sys.path.append(_candidate_root)

_compiled_sources = ()
if _blocked:
    _manifest_path = os.environ.get("LOCAL_AGENT_CANDIDATE_TEST_SOURCE_MANIFEST")
    _manifest_sha256 = os.environ.get(
        "LOCAL_AGENT_CANDIDATE_TEST_SOURCE_MANIFEST_SHA256"
    )
    _candidate_sha = os.environ.get("LOCAL_AGENT_CANDIDATE_TEST_SOURCE_SHA")
    if not _manifest_path or not _manifest_sha256 or not _candidate_sha:
        raise RuntimeError("candidate test source manifest authority is unavailable")
    _manifest_bytes = Path(_manifest_path).read_bytes()
    if hashlib.sha256(_manifest_bytes).hexdigest() != _manifest_sha256:
        raise RuntimeError("candidate test source manifest changed before pytest startup")
    _manifest = json.loads(_manifest_bytes)
    if _manifest.get("candidate_sha") != _candidate_sha:
        raise RuntimeError("candidate test source manifest SHA identity mismatch")
    _entries = _manifest.get("entries")
    if not isinstance(_entries, list):
        raise RuntimeError("candidate test source manifest entries are malformed")
    if tuple(sorted(entry.get("path") for entry in _entries)) != tuple(sorted(_blocked)):
        raise RuntimeError("candidate test source manifest membership mismatch")

    _compiled = []
    _module_paths = {}
    for _entry in _entries:
        _relative = _entry.get("path")
        _source = _entry.get("source")
        _blob_sha = _entry.get("blob_sha")
        if (
            not isinstance(_relative, str)
            or not isinstance(_source, str)
            or not isinstance(_blob_sha, str)
            or len(_blob_sha) != 40
            or any(ch not in "0123456789abcdef" for ch in _blob_sha)
        ):
            raise RuntimeError("candidate test source manifest entry is malformed")
        _absolute = os.path.abspath(os.path.join(_candidate_root, _relative))
        if os.path.commonpath((_candidate_root, _absolute)) != _candidate_root:
            raise RuntimeError("candidate test source manifest escaped candidate root")
        _path = Path(_absolute)
        _module_names = {_pytest_pathlib.module_name_from_path(_path, Path(_candidate_root))}
        try:
            _, _package_name = _pytest_pathlib.resolve_pkg_root_and_module_name(_path)
        except _pytest_pathlib.CouldNotResolvePathError:
            pass
        else:
            _module_names.add(_package_name)
        _code = compile(_source, _absolute, "exec", dont_inherit=True)
        for _module_name in _module_names:
            _previous = _module_paths.get(_module_name)
            if _previous is not None and _previous != _absolute:
                raise RuntimeError("candidate test module name is ambiguous")
            _module_paths[_module_name] = _absolute
            _compiled.append((_module_name, _absolute, _code))
    _compiled_sources = tuple(_compiled)


class _ExactCandidateLoader(importlib.abc.Loader):
    def __init__(self, path, code):
        self.path = path
        self.code = code

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        module.__file__ = self.path
        exec(self.code, module.__dict__)


class _ExactCandidateFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        del path, target
        for module_name, absolute, code in _compiled_sources:
            if module_name == fullname:
                return importlib.util.spec_from_loader(
                    fullname,
                    _ExactCandidateLoader(absolute, code),
                    origin=absolute,
                )
        return None


_original_importtestmodule = _pytest_python.importtestmodule


def _guarded_importtestmodule(path, config):
    absolute = os.path.abspath(os.fspath(path))
    relative = os.path.relpath(absolute, _candidate_root).replace(os.sep, "/")
    if relative not in _blocked:
        return _original_importtestmodule(path, config)

    finder = _ExactCandidateFinder()
    sys.meta_path.insert(0, finder)
    original_consider_module = config.pluginmanager.consider_module
    config.pluginmanager.consider_module = lambda _module: None
    try:
        return _original_importtestmodule(path, config)
    finally:
        config.pluginmanager.consider_module = original_consider_module
        if finder in sys.meta_path:
            sys.meta_path.remove(finder)


_pytest_python.importtestmodule = _guarded_importtestmodule
raise SystemExit(pytest.main(sys.argv[2:]))
"""


class AssessmentError(RuntimeError):
    def __init__(self, status: str, message: str) -> None:
        super().__init__(message)
        self.status = status


def _proc_parent_pid(pid: int, *, required: bool = False) -> int | None:
    status_path = PROC_ROOT / str(pid) / "status"
    try:
        content = status_path.read_bytes()
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        if required:
            raise AssessmentError(
                "BLOCKED", "Linux /proc parent-process inventory is unavailable"
            )
        return None
    except OSError as exc:
        if required:
            raise AssessmentError(
                "BLOCKED", "Linux /proc parent-process inventory is unavailable"
            ) from exc
        return None
    for line in content.splitlines():
        if not line.startswith(b"PPid:"):
            continue
        raw_parent = line.partition(b":")[2].strip()
        try:
            return int(raw_parent)
        except ValueError as exc:
            if required:
                raise AssessmentError(
                    "BLOCKED", "Linux /proc parent-process inventory is malformed"
                ) from exc
            return None
    if required:
        raise AssessmentError(
            "BLOCKED", "Linux /proc parent-process inventory is malformed"
        )
    return None


def _validate_procfs_parent_inventory() -> None:
    _proc_parent_pid(os.getpid(), required=True)


def enable_child_subreaper() -> None:
    """Make this Linux runner the nearest reaper for orphaned command descendants."""

    if sys.platform != "linux" or not hasattr(os, "memfd_create"):
        raise AssessmentError(
            "BLOCKED",
            "local host assessment requires Linux child-subreaper and memfd support",
        )
    _validate_procfs_parent_inventory()
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        error_number = ctypes.get_errno()
        raise AssessmentError(
            "BLOCKED",
            f"could not establish child-subreaper containment: {os.strerror(error_number)}",
        )


def _reap_adopted_children() -> None:
    while True:
        try:
            pid, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def _direct_child_pids() -> tuple[int, ...]:
    try:
        entries = tuple(PROC_ROOT.iterdir())
    except OSError as exc:
        raise RuntimeError("could not inspect adopted command descendants") from exc
    parent_pid = os.getpid()
    children: list[int] = []
    for entry in entries:
        if not entry.name.isdecimal():
            continue
        pid = int(entry.name)
        if pid == parent_pid:
            continue
        if _proc_parent_pid(pid) == parent_pid:
            children.append(pid)
    return tuple(sorted(set(children)))


def _signal_adopted_children(child_pids: Sequence[int], signum: int) -> None:
    own_process_group = os.getpgrp()
    process_groups: set[int] = set()
    for pid in child_pids:
        try:
            group = os.getpgid(pid)
        except ProcessLookupError:
            continue
        if group != own_process_group:
            process_groups.add(group)
    for group in sorted(process_groups):
        try:
            os.killpg(group, signum)
        except ProcessLookupError:
            pass
    for pid in child_pids:
        try:
            os.kill(pid, signum)
        except ProcessLookupError:
            pass


def _drain_adopted_children(signum: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        _reap_adopted_children()
        child_pids = _direct_child_pids()
        if not child_pids:
            return True
        _signal_adopted_children(child_pids, signum)
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def _terminate_adopted_children(terminate_grace_seconds: float) -> bool:
    """Terminate descendants adopted after the foreground command leader exits."""

    _reap_adopted_children()
    if not _direct_child_pids():
        return False
    if not _drain_adopted_children(signal.SIGTERM, terminate_grace_seconds):
        if not _drain_adopted_children(signal.SIGKILL, terminate_grace_seconds):
            raise RuntimeError("owned command descendants survived SIGKILL")
    return True


def _read_capture(handle: Any) -> str:
    handle.flush()
    handle.seek(0)
    return handle.read().decode("utf-8", errors="replace")


def acquire_host_assessment_lease() -> socket.socket:
    """Acquire the host-wide assessment lifecycle lease without filesystem writes."""

    try:
        lease = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    except OSError as exc:
        raise AssessmentError(
            "INFRA_ERROR",
            f"host assessment lifecycle lease is unavailable: {exc}",
        ) from exc
    try:
        lease.bind(f"\0{HOST_ASSESSMENT_LEASE_LABEL}")
    except OSError as exc:
        lease.close()
        if exc.errno == errno.EADDRINUSE:
            raise AssessmentError(
                "BLOCKED", "another host assessment owns the global lifecycle lease"
            ) from exc
        raise AssessmentError(
            "INFRA_ERROR",
            f"host assessment lifecycle lease is unavailable: {exc}",
        ) from exc
    return lease


def acquire_workspace_lifecycle_lock(workspace_root: Path):
    """Acquire the legacy workspace-local file lock as defense in depth."""

    workspace_root.mkdir(parents=True, exist_ok=True)
    lock_dir = workspace_root / ".locks"
    lock_dir.mkdir(exist_ok=True)
    handle = (lock_dir / "host-assessment.lock").open("a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise AssessmentError(
            "BLOCKED", "another host assessment owns the workspace lifecycle lock"
        ) from exc
    except OSError as exc:
        handle.close()
        raise AssessmentError(
            "INFRA_ERROR", f"workspace lifecycle lock is unavailable: {exc}"
        ) from exc
    return handle


@dataclass(frozen=True)
class PytestGroup:
    name: str
    python_versions: tuple[str, ...]
    selectors: tuple[str, ...]
    expected_tests: int


@dataclass(frozen=True)
class AssessmentProfile:
    name: str
    description: str
    python_versions: tuple[str, ...]
    static_python: str
    requires_disposable_services: bool
    reset_qdrant_after_tests: bool
    expected_skips: int
    command_timeout_seconds: int
    environment: Mapping[str, str]
    pytest_groups: tuple[PytestGroup, ...]
    candidate_code_trust: str
    trusted_refs: tuple[str, ...]
    repository_remote: str
    requires_fresh_fetch: bool
    allow_reviewed_pr_head: bool
    pr_test_python: str
    pr_test_roots: tuple[str, ...]
    pr_test_max_files: int
    pr_test_max_nodes: int


@dataclass
class CommandRecord:
    name: str
    argv: list[str]
    started_at: float
    duration_seconds: float
    returncode: int
    stdout_path: str
    stderr_path: str
    stdout_sha256: str
    stderr_sha256: str
    junit: dict[str, Any] | None = None
    junit_sha256: str | None = None
    expected_tests: int | None = None
    expected_skips: int | None = None
    junit_check_passed: bool | None = None
    timed_out: bool = False


@dataclass(frozen=True)
class ProcessOutcome:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


@dataclass
class AssessmentEvidence:
    schema_version: str = SCHEMA_VERSION
    host_evidence_result: str = "INFRA_ERROR"
    gate_decision: str = "NOT_EVALUATED"
    assessment_id: str = ""
    target_kind: str = "trusted-ref"
    pr_number: int | None = None
    profile: str = ""
    profile_sha256: str = ""
    requested_sha: str = ""
    tested_sha: str | None = None
    expected_ref: str | None = None
    expected_ref_start: str | None = None
    expected_ref_end: str | None = None
    pr_head_start: str | None = None
    pr_head_end: str | None = None
    control_sha: str | None = None
    control_ref_start: str | None = None
    control_ref_end: str | None = None
    candidate_test_manifest: dict[str, Any] | None = None
    control_fingerprint: dict[str, str] = field(default_factory=dict)
    python_versions: dict[str, str] = field(default_factory=dict)
    service_contract: dict[str, Any] | None = None
    commands: list[dict[str, Any]] = field(default_factory=list)
    anomalies: list[str] = field(default_factory=list)
    cleanup: dict[str, Any] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def ensure_descendant(path: Path, root: Path) -> Path:
    resolved_root = root.resolve()
    resolved = path.resolve()
    if resolved == resolved_root or not resolved.is_relative_to(resolved_root):
        raise ValueError(
            f"path must be a strict descendant of {resolved_root}: {resolved}"
        )
    return resolved


def validate_selector(selector: str) -> str:
    if (
        not SELECTOR_RE.fullmatch(selector)
        or ".." in Path(selector.split("::", 1)[0]).parts
    ):
        raise ValueError(f"invalid pytest selector: {selector}")
    return selector


def _require_str_list(value: Any, field_name: str) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) for item in value)
    ):
        raise TypeError(f"{field_name} must be a non-empty string array")
    return tuple(value)


def _require_positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise TypeError(f"{field_name} must be a positive integer")
    return value


def validate_target_args(
    args: argparse.Namespace, profile: AssessmentProfile
) -> tuple[str, int | None]:
    target_kind = str(getattr(args, "target_kind", "trusted-ref") or "trusted-ref")
    pr_number = getattr(args, "pr", None)
    if target_kind == "trusted-ref":
        if pr_number is not None:
            raise AssessmentError("BLOCKED", "trusted-ref target does not accept --pr")
        return target_kind, None
    if target_kind != "pr-head":
        raise AssessmentError(
            "BLOCKED", f"unsupported assessment target: {target_kind}"
        )
    if not profile.allow_reviewed_pr_head:
        raise AssessmentError(
            "BLOCKED", "profile does not permit reviewed PR-head assessment"
        )
    if (
        isinstance(pr_number, bool)
        or not isinstance(pr_number, int)
        or not 1 <= pr_number <= PR_NUMBER_MAX
    ):
        raise AssessmentError(
            "BLOCKED", "pr-head target requires a positive bounded --pr"
        )
    if getattr(args, "expected_ref", None) is not None:
        raise AssessmentError(
            "BLOCKED", "pr-head target does not accept --expected-ref"
        )
    if not getattr(args, "fetch", False):
        raise AssessmentError("BLOCKED", "pr-head target requires --fetch freshness")
    return target_kind, pr_number


def parse_collected_nodeids(
    stdout: str, allowed_files: Sequence[str], max_nodes: int
) -> tuple[str, ...]:
    allowed = set(allowed_files)
    nodes: list[str] = []
    for raw_line in stdout.splitlines():
        node_id = raw_line.strip()
        path, separator, _ = node_id.partition("::")
        if not separator or path not in allowed:
            continue
        if len(node_id) > 4096:
            raise AssessmentError("BLOCKED", "collected pytest node ID exceeds bound")
        nodes.append(node_id)
    if len(nodes) != len(set(nodes)):
        raise AssessmentError(
            "BLOCKED", "pytest collection returned duplicate node IDs"
        )
    result = tuple(sorted(nodes))
    if len(result) > max_nodes:
        raise AssessmentError(
            "BLOCKED", f"collected pytest node count exceeds bound {max_nodes}"
        )
    return result


def build_candidate_test_manifest(
    base_sha: str, files: Sequence[str], node_ids: Sequence[str]
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "rule": "changed-test-modules-v1",
        "base_sha": base_sha,
        "files": sorted(files),
        "node_ids": sorted(node_ids),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    payload["sha256"] = sha256_bytes(canonical)
    return payload


def pr_pytest_conftest_paths(test_roots: Sequence[str]) -> tuple[str, ...]:
    paths = {"conftest.py"}
    for root in test_roots:
        current = Path(root)
        while current != Path("."):
            paths.add((current / "conftest.py").as_posix())
            current = current.parent
    return tuple(sorted(paths))


def pytest_entry_argv(
    python: Path, blocked_test_module_plugins: Sequence[str] = ()
) -> list[str]:
    blocked = tuple(sorted(set(blocked_test_module_plugins)))
    if not blocked:
        return [str(python), "-m", "pytest"]
    return [
        str(python),
        "-P",
        "-c",
        CANDIDATE_PYTEST_LAUNCHER,
        json.dumps(blocked, separators=(",", ":")),
    ]


COLLECTION_TOTAL_RE = re.compile(r"(?m)^([0-9]+) tests? collected(?: in [^\n]+)?$")


def declares_pytest_plugins(source: str) -> bool:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        # Syntax-invalid candidate code will fail pytest collection later.
        return False

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Store)
            and node.id == "pytest_plugins"
        ):
            return True

        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound_name = alias.asname
                if bound_name is None and isinstance(node, ast.Import):
                    bound_name = alias.name.split(".", 1)[0]
                elif bound_name is None:
                    bound_name = alias.name

                if bound_name == "pytest_plugins":
                    return True

    return False


def parse_collection_total(stdout: str, *, failure_status: str = "BLOCKED") -> int:
    matches = COLLECTION_TOTAL_RE.findall(stdout)
    if len(matches) != 1:
        raise AssessmentError(
            failure_status, "pytest collection summary is missing or ambiguous"
        )
    return int(matches[0])


def load_profile(path: Path, name: str) -> AssessmentProfile:
    document = tomllib.loads(path.read_text(encoding="utf-8"))
    if document.get("schema_version") != PROFILE_SCHEMA_VERSION:
        raise ValueError("unsupported local-agent assessment profile schema")
    profiles = document.get("profiles")
    if not isinstance(profiles, dict) or name not in profiles:
        raise ValueError(f"unknown assessment profile: {name}")
    raw = profiles[name]
    if not isinstance(raw, dict):
        raise TypeError(f"profile {name} must be a table")

    versions = _require_str_list(raw.get("python_versions"), "python_versions")
    if set(versions) - ALLOWED_PYTHONS:
        raise ValueError(f"unsupported Python version in profile {name}")
    static_python = str(raw.get("static_python") or "")
    if static_python not in versions:
        raise ValueError("static_python must be one of python_versions")
    raw_env = raw.get("environment", {})
    if not isinstance(raw_env, dict) or set(raw_env) - PROFILE_ENV_KEYS:
        raise ValueError("profile environment contains non-allowlisted keys")
    environment = {key: str(value) for key, value in raw_env.items()}

    raw_groups = raw.get("pytest_groups")
    if not isinstance(raw_groups, list) or not raw_groups:
        raise ValueError("profile must define pytest_groups")
    groups: list[PytestGroup] = []
    seen_names: set[str] = set()
    for raw_group in raw_groups:
        if not isinstance(raw_group, dict):
            raise TypeError("pytest_groups entries must be tables")
        group_name = str(raw_group.get("name") or "")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,47}", group_name):
            raise ValueError(f"invalid pytest group name: {group_name}")
        if group_name in seen_names:
            raise ValueError(f"duplicate pytest group name: {group_name}")
        seen_names.add(group_name)
        group_versions = _require_str_list(
            raw_group.get("python_versions"),
            f"pytest_groups.{group_name}.python_versions",
        )
        if not set(group_versions).issubset(versions):
            raise ValueError(f"group {group_name} uses undeclared Python version")
        selectors = tuple(
            validate_selector(selector)
            for selector in _require_str_list(
                raw_group.get("selectors"),
                f"pytest_groups.{group_name}.selectors",
            )
        )
        expected_tests = raw_group.get("expected_tests")
        if not isinstance(expected_tests, int) or expected_tests <= 0:
            raise ValueError(
                f"pytest_groups.{group_name}.expected_tests must be positive"
            )
        groups.append(
            PytestGroup(group_name, group_versions, selectors, expected_tests)
        )

    timeout = int(raw.get("command_timeout_seconds", 1800))
    if not 30 <= timeout <= 7200:
        raise ValueError("command_timeout_seconds must be between 30 and 7200")
    expected_skips = int(raw.get("expected_skips", 0))
    if expected_skips != 0:
        raise ValueError("profile schema v1 supports only exact zero-skip evidence")
    candidate_code_trust = str(raw.get("candidate_code_trust") or "")
    if candidate_code_trust != "trusted-ref-only":
        raise ValueError("profile must declare candidate_code_trust=trusted-ref-only")
    trusted_refs = _require_str_list(raw.get("trusted_refs"), "trusted_refs")
    if any(not ref.startswith("origin/") for ref in trusted_refs):
        raise ValueError("trusted_refs must name remote-tracking refs")
    repository_remote = str(raw.get("repository_remote") or "")
    if not re.fullmatch(
        r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\.git", repository_remote
    ):
        raise ValueError("repository_remote must be a canonical HTTPS GitHub URL")
    requires_fresh_fetch = raw.get("requires_fresh_fetch")
    if requires_fresh_fetch is not True:
        raise ValueError("trusted-ref-only profiles must require a fresh fetch")
    allow_reviewed_pr_head = raw.get("allow_reviewed_pr_head") is True
    pr_test_python = str(raw.get("pr_test_python") or "")
    pr_test_roots = _require_str_list(raw.get("pr_test_roots"), "pr_test_roots")
    if pr_test_python not in versions:
        raise ValueError("pr_test_python must be one of python_versions")
    if len(set(pr_test_roots)) != len(pr_test_roots) or not set(pr_test_roots).issubset(
        ALLOWED_PR_TEST_ROOTS
    ):
        raise ValueError("pr_test_roots must be unique repository test roots")
    pr_test_max_files = _require_positive_int(
        raw.get("pr_test_max_files"), "pr_test_max_files"
    )
    pr_test_max_nodes = _require_positive_int(
        raw.get("pr_test_max_nodes"), "pr_test_max_nodes"
    )
    return AssessmentProfile(
        name=name,
        description=str(raw.get("description") or ""),
        python_versions=versions,
        static_python=static_python,
        requires_disposable_services=bool(raw.get("requires_disposable_services")),
        reset_qdrant_after_tests=bool(raw.get("reset_qdrant_after_tests")),
        expected_skips=expected_skips,
        command_timeout_seconds=timeout,
        environment=environment,
        pytest_groups=tuple(groups),
        candidate_code_trust=candidate_code_trust,
        trusted_refs=trusted_refs,
        repository_remote=repository_remote,
        requires_fresh_fetch=requires_fresh_fetch,
        allow_reviewed_pr_head=allow_reviewed_pr_head,
        pr_test_python=pr_test_python,
        pr_test_roots=pr_test_roots,
        pr_test_max_files=pr_test_max_files,
        pr_test_max_nodes=pr_test_max_nodes,
    )


def parse_service_contract(payload: str, namespace: str) -> dict[str, Any]:
    document = json.loads(payload)
    required_top = {
        "schema_version",
        "namespace",
        "host",
        "postgres",
        "qdrant",
        "environment",
        "images",
    }
    if not isinstance(document, dict) or set(document) != required_top:
        raise ValueError("unexpected disposable-service JSON fields")
    if document["schema_version"] != SERVICE_SCHEMA_VERSION:
        raise ValueError("unsupported disposable-service schema")
    if document["namespace"] != namespace or document["host"] != "127.0.0.1":
        raise ValueError("disposable-service identity mismatch")
    environment = document["environment"]
    if not isinstance(environment, dict) or set(environment) != SERVICE_ENV_KEYS:
        raise ValueError("unexpected disposable-service environment keys")
    if not all(isinstance(value, str) and value for value in environment.values()):
        raise ValueError("disposable-service environment values must be strings")
    images = document["images"]
    if not isinstance(images, dict) or set(images) != SERVICE_IMAGE_KEYS:
        raise ValueError("unexpected disposable-service image keys")
    if not all(
        isinstance(value, str) and re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", value)
        for value in images.values()
    ):
        raise ValueError("disposable-service images must be digest-pinned")

    pg_url = urlparse(environment["RESEARCH_STORE_TEST_DATABASE_URL"])
    qdrant_url = urlparse(environment["QDRANT_URL"])
    if pg_url.hostname != "127.0.0.1" or qdrant_url.hostname != "127.0.0.1":
        raise ValueError("disposable services must bind to loopback")
    if pg_url.port in {55432, 6333} or qdrant_url.port in {55432, 6333}:
        raise ValueError("disposable services resolved to protected port")
    database = pg_url.path.removeprefix("/")
    postgres = document["postgres"]
    qdrant = document["qdrant"]
    if not isinstance(postgres, dict) or set(postgres) != {
        "container",
        "port",
        "database",
    }:
        raise ValueError("unexpected PostgreSQL service fields")
    if not isinstance(qdrant, dict) or set(qdrant) != {
        "container",
        "port",
        "ready_url",
    }:
        raise ValueError("unexpected Qdrant service fields")
    if (
        postgres["container"] != f"{namespace}_pg"
        or postgres["port"] != pg_url.port
        or postgres["database"] != database
    ):
        raise ValueError("PostgreSQL service metadata mismatch")
    if (
        qdrant["container"] != f"{namespace}_qdrant"
        or qdrant["port"] != qdrant_url.port
        or qdrant["ready_url"] != f"{environment['QDRANT_URL']}/readyz"
    ):
        raise ValueError("Qdrant service metadata mismatch")
    if "test" not in database.replace("-", "_").split("_"):
        raise ValueError("disposable database lacks standalone test segment")
    if environment["RESEARCH_STORE_TEST_ALLOW_RESET"] != database:
        raise ValueError("PostgreSQL reset acknowledgement mismatch")
    if not (
        environment["RESEARCH_STORE_TEST_QDRANT_URL"]
        == environment["RESEARCH_STORE_TEST_QDRANT_ALLOW_RESET"]
        == environment["QDRANT_URL"]
    ):
        raise ValueError("Qdrant reset acknowledgement mismatch")
    return document


def build_base_environment(
    materials_dir: Path, tool_paths: Mapping[str, str]
) -> dict[str, str]:
    path_entries = sorted({str(Path(value).parent) for value in tool_paths.values()})
    path_entries.extend(
        ["/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin", "/sbin", "/bin"]
    )
    home = materials_dir / "home"
    temp = materials_dir / "tmp"
    xdg_data = materials_dir / "xdg-data"
    xdg_cache = materials_dir / "xdg-cache"
    for path in (home, temp, xdg_data, xdg_cache):
        path.mkdir(parents=True, exist_ok=True)
    return {
        "PATH": os.pathsep.join(dict.fromkeys(path_entries)),
        "HOME": str(home),
        "TMPDIR": str(temp),
        "XDG_DATA_HOME": str(xdg_data),
        "XDG_CACHE_HOME": str(xdg_cache),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PIP_CONFIG_FILE": "/dev/null",
        "UV_NO_PROGRESS": "1",
    }


def junit_summary(path: Path) -> dict[str, Any]:
    root = ET.parse(path).getroot()
    cases = list(root.iter("testcase"))
    failures = sum(case.find("failure") is not None for case in cases)
    errors = sum(case.find("error") is not None for case in cases)
    skipped_elements = [(case, case.find("skipped")) for case in cases]
    skipped = [pair for pair in skipped_elements if pair[1] is not None]
    skip_details = []
    for case, element in skipped:
        assert element is not None
        file_name = (
            case.get("file") or case.get("classname", "").replace(".", "/") + ".py"
        )
        node_id = f"{file_name}::{case.get('name', '')}"
        skip_details.append(
            {
                "node_id": node_id,
                "reason": (element.get("message") or element.text or "").strip(),
            }
        )
    return {
        "tests": len(cases),
        "passed": len(cases) - failures - errors - len(skipped),
        "failed": failures,
        "errors": errors,
        "skipped": len(skipped),
        "skip_details": sorted(skip_details, key=lambda item: item["node_id"]),
    }


def inventory(root: Path) -> dict[str, dict[str, Any]]:
    if not root.exists():
        return {}
    result: dict[str, dict[str, Any]] = {}
    for path in root.rglob("*"):
        relative = str(path.relative_to(root))
        stat = path.lstat()
        if path.is_symlink():
            result[relative] = {"type": "symlink", "target": os.readlink(path)}
        elif path.is_file():
            result[relative] = {
                "type": "file",
                "size": stat.st_size,
                "sha256": sha256_file(path),
            }
        elif path.is_dir():
            result[relative] = {"type": "directory"}
        else:
            result[relative] = {"type": "other", "mode": stat.st_mode}
    return result


def _redact(text: str) -> str:
    return re.sub(r"(postgresql://[^:\s]+:)[^@\s]+(@)", r"\1<redacted>\2", text)


def run_bounded_process(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str] | None,
    timeout: float,
    terminate_grace_seconds: float = PROCESS_TERMINATION_GRACE_SECONDS,
) -> ProcessOutcome:
    """Run one command, bounding leader lifetime and all adopted descendants."""

    stdout_fd = os.memfd_create("firecrawl-assessment-stdout", os.MFD_CLOEXEC)
    stderr_fd = os.memfd_create("firecrawl-assessment-stderr", os.MFD_CLOEXEC)
    with (
        os.fdopen(stdout_fd, "w+b") as stdout_capture,
        os.fdopen(stderr_fd, "w+b") as stderr_capture,
    ):
        process = subprocess.Popen(
            list(argv),
            cwd=cwd,
            env=dict(env) if env is not None else None,
            stdout=stdout_capture,
            stderr=stderr_capture,
            start_new_session=True,
        )
        timed_out = False
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=terminate_grace_seconds)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=terminate_grace_seconds)
                except subprocess.TimeoutExpired as exc:
                    raise RuntimeError(
                        "bounded subprocess leader survived SIGKILL"
                    ) from exc

        if process.returncode is None:
            raise RuntimeError("bounded subprocess did not terminate")
        surviving_descendants = _terminate_adopted_children(terminate_grace_seconds)
        stdout = _read_capture(stdout_capture)
        stderr = _read_capture(stderr_capture)
        if timed_out:
            return ProcessOutcome(
                returncode=124,
                stdout=stdout,
                stderr=stderr + "\ncommand timed out\n",
                timed_out=True,
            )
        return ProcessOutcome(
            returncode=(
                PROCESS_CONTAINMENT_FAILURE_RETURN_CODE
                if surviving_descendants and process.returncode == 0
                else process.returncode
            ),
            stdout=stdout,
            stderr=stderr
            + (
                "\ncommand left surviving descendants\n"
                if surviving_descendants
                else ""
            ),
        )


class Runner:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.control_root = Path(__file__).resolve().parents[1]
        self.profile_path = (
            self.control_root / "references/local-agent-assessment-profiles.toml"
        )
        self.profile = load_profile(self.profile_path, args.profile)
        self.target_kind, self.pr_number = validate_target_args(args, self.profile)
        self.repo = Path(args.repo).resolve()
        self.allowed_root = Path(
            os.environ.get(
                "LOCAL_AGENT_ASSESSMENT_ALLOWED_ROOT", "/tmp/opencode/verify"
            )
        ).resolve()
        self.workspace_root = Path(args.workspace_root).resolve()
        if self.workspace_root != self.allowed_root:
            raise AssessmentError(
                "BLOCKED",
                f"workspace root must equal sanctioned root {self.allowed_root}",
            )
        self.assessment_id = args.assessment_id or f"{args.profile}-{args.sha[:12]}"
        if not ASSESSMENT_ID_RE.fullmatch(self.assessment_id):
            raise AssessmentError(
                "BLOCKED", "assessment ID does not satisfy helper namespace contract"
            )
        self.worktree = ensure_descendant(
            self.workspace_root / "worktrees" / self.assessment_id,
            self.workspace_root,
        )
        self.materials = ensure_descendant(
            self.workspace_root / "materials" / self.assessment_id,
            self.workspace_root,
        )
        self.results = ensure_descendant(
            self.workspace_root / "results" / self.assessment_id,
            self.workspace_root,
        )
        self.logs = self.results / "logs"
        self.journal_path = self.results / "lifecycle.json"
        self.evidence = AssessmentEvidence(
            assessment_id=self.assessment_id,
            target_kind=self.target_kind,
            pr_number=self.pr_number,
            profile=args.profile,
            profile_sha256=sha256_file(self.profile_path),
            requested_sha=args.sha,
            expected_ref=args.expected_ref,
        )
        self.tools = self._resolve_tools()
        self.base_env: dict[str, str] = {}
        self.service_contract: dict[str, Any] | None = None
        self.command_records: list[CommandRecord] = []
        self.last_raw_stdout = ""
        self.last_raw_stderr = ""
        self.worktree_added = False
        self.services_started = False
        self.materials_created = False
        self.results_created = False
        self.host_lease: socket.socket | None = None
        self.lock_handle: Any = None
        self.failed_checks = False
        self.service_ports: tuple[int, int] | None = None
        self.candidate_test_base_sha: str | None = None
        self.candidate_test_files: tuple[str, ...] = ()
        self.candidate_test_blobs: dict[str, str] = {}
        self._candidate_test_source_manifest_path: Path | None = None
        self._candidate_test_source_manifest_sha256: str | None = None

    def _journal(self, stage: str) -> None:
        if not self.results_created:
            return
        payload = {
            "schema_version": LIFECYCLE_SCHEMA_VERSION,
            "assessment_id": self.assessment_id,
            "pid": os.getpid(),
            "stage": stage,
            "repo": str(self.repo),
            "worktree": str(self.worktree),
            "materials": str(self.materials),
            "worktree_added": self.worktree_added,
            "services_started": self.services_started,
            "service_ports": list(self.service_ports) if self.service_ports else None,
            "updated_at": time.time(),
        }
        temporary = self.journal_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.journal_path)

    def _resolve_tools(self) -> dict[str, str]:
        result = {}
        for name in ("bash", "curl", "docker", "git", "uv"):
            resolved = shutil.which(name)
            if not resolved:
                raise AssessmentError("BLOCKED", f"required command not found: {name}")
            result[name] = str(Path(resolved).resolve())
        return result

    def _fingerprint_control_plane(self) -> dict[str, str]:
        paths = {
            "runner": Path(__file__).resolve(),
            "shim": self.control_root / "scripts/local-agent-assessment",
            "service_helper": self.control_root / "scripts/disposable-test-services",
            "profile": self.profile_path,
            "static_policy": self.control_root / "pyproject.toml",
            "static_baseline": self.control_root / "pyrefly-baseline.json",
            "ruff_e402_debt": self.control_root / "ci/ruff-e402-debt.toml",
            "ruff_e731_debt": self.control_root / "ci/ruff-e731-debt.toml",
            "central_static_runner": self.control_root / "scripts/run_ci_profile.py",
            "central_ci_authority": self.control_root / "scripts/ci_authority.py",
            "pytest_skip_allowlist": self.control_root / PYTEST_SKIP_ALLOWLIST_PATH,
            "pytest_skip_verifier": self.control_root / PYTEST_SKIP_VERIFIER_PATH,
            "toolchain_manifest": self.control_root / "requirements-ci.txt",
        }
        missing = [str(path) for path in paths.values() if not path.is_file()]
        if missing:
            raise AssessmentError(
                "BLOCKED", f"trusted control-plane files missing: {missing}"
            )
        return {name: sha256_file(path) for name, path in paths.items()}

    def _control(
        self,
        argv: Sequence[str],
        *,
        check: bool = True,
        timeout: float = CONTROL_COMMAND_TIMEOUT_SECONDS,
    ) -> subprocess.CompletedProcess[str]:
        outcome = run_bounded_process(
            argv,
            cwd=self.repo,
            env=self.base_env or None,
            timeout=timeout,
        )
        if outcome.timed_out:
            command = Path(str(argv[0])).name if argv else "control command"
            raise AssessmentError(
                "BLOCKED", f"control command timed out after {timeout:g}s: {command}"
            )
        completed = subprocess.CompletedProcess(
            list(argv), outcome.returncode, outcome.stdout, outcome.stderr
        )
        if check and completed.returncode != 0:
            message = _redact((completed.stderr or completed.stdout).strip())
            raise AssessmentError("BLOCKED", f"control command failed: {message}")
        return completed

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return self._control(
            [
                self.tools["git"],
                "-c",
                "maintenance.auto=false",
                "-c",
                "gc.auto=0",
                "-C",
                str(self.repo),
                *args,
            ],
            check=check,
        )

    def _git_tree_entry(self, commit: str, path: str) -> tuple[str, str, str] | None:
        raw = self._git("ls-tree", commit, "--", path).stdout.rstrip("\n")
        if not raw:
            return None
        lines = raw.splitlines()
        if len(lines) != 1:
            raise AssessmentError(
                "BLOCKED", f"Git path did not resolve exactly at {commit}: {path}"
            )
        metadata, separator, listed_path = lines[0].partition("\t")
        parts = metadata.split()
        if not separator or len(parts) != 3 or listed_path != path:
            raise AssessmentError("BLOCKED", f"Git path entry is malformed: {path}")
        mode, object_type, object_sha = parts
        if not SHA_RE.fullmatch(object_sha):
            raise AssessmentError("BLOCKED", f"Git path object is malformed: {path}")
        return mode, object_type, object_sha

    def _require_matching_optional_regular_path(
        self,
        control_sha: str,
        candidate_sha: str,
        path: str,
        *,
        allow_both_missing: bool = True,
    ) -> None:
        control_entry = self._git_tree_entry(control_sha, path)
        candidate_entry = self._git_tree_entry(candidate_sha, path)
        if control_entry is None and candidate_entry is None and allow_both_missing:
            return
        if (
            control_entry is None
            or candidate_entry is None
            or control_entry != candidate_entry
            or control_entry[0] not in {"100644", "100755"}
            or control_entry[1] != "blob"
        ):
            raise AssessmentError(
                "BLOCKED", f"candidate cannot replace trusted pytest control: {path}"
            )

    def _fetch_pr_head(self) -> str:
        if self.pr_number is None:
            raise AssessmentError("BLOCKED", "pr-head target is missing PR identity")
        self._git(
            "fetch",
            "--no-tags",
            "origin",
            f"refs/pull/{self.pr_number}/head",
        )
        resolved = self._git("rev-parse", "FETCH_HEAD").stdout.strip()
        if not SHA_RE.fullmatch(resolved):
            raise AssessmentError(
                "BLOCKED", "canonical PR head did not resolve to an exact SHA"
            )
        return resolved

    def _discover_candidate_test_files(self, control_sha: str) -> tuple[str, ...]:
        merge_base = self._git("merge-base", control_sha, self.args.sha).stdout.strip()
        if not SHA_RE.fullmatch(merge_base):
            raise AssessmentError(
                "BLOCKED", "PR merge base did not resolve to an exact SHA"
            )
        self.candidate_test_base_sha = merge_base
        pytest_control_pathspecs = tuple(
            dict.fromkeys(
                (
                    *self.profile.pr_test_roots,
                    *pr_pytest_conftest_paths(self.profile.pr_test_roots),
                )
            )
        )
        pytest_control_changes = self._git(
            "diff",
            "--name-only",
            "-z",
            "--no-renames",
            "--diff-filter=AMD",
            merge_base,
            self.args.sha,
            "--",
            *pytest_control_pathspecs,
        ).stdout
        changed_conftests = sorted(
            path
            for path in pytest_control_changes.split("\0")
            if path and Path(path).name == "conftest.py"
        )
        if changed_conftests:
            raise AssessmentError(
                "BLOCKED",
                f"candidate cannot replace pytest control files: {changed_conftests}",
            )
        changed = self._git(
            "diff",
            "--name-only",
            "-z",
            "--diff-filter=AMR",
            merge_base,
            self.args.sha,
            "--",
            *self.profile.pr_test_roots,
        ).stdout
        trusted_test_paths = {
            selector.split("::", 1)[0]
            for group in self.profile.pytest_groups
            for selector in group.selectors
        }
        files = tuple(
            sorted(
                path
                for path in changed.split("\0")
                if path
                and Path(path).name.startswith("test_")
                and Path(path).suffix == ".py"
                and path not in trusted_test_paths
            )
        )
        if len(files) != len(set(files)):
            raise AssessmentError(
                "BLOCKED", "candidate test discovery returned duplicate paths"
            )
        if len(files) > self.profile.pr_test_max_files:
            raise AssessmentError(
                "BLOCKED",
                f"candidate test file count exceeds bound {self.profile.pr_test_max_files}",
            )
        for path in files:
            validate_selector(path)
            if not any(
                path.startswith(f"{root}/") for root in self.profile.pr_test_roots
            ):
                raise AssessmentError(
                    "BLOCKED", f"candidate test escaped configured roots: {path}"
                )
        for path in files:
            source = self._git("show", f"{self.args.sha}:{path}").stdout
            if declares_pytest_plugins(source):
                raise AssessmentError(
                    "BLOCKED",
                    f"candidate changed test module cannot declare pytest_plugins: {path}",
                )
        return files

    def _acquire_lifecycle_locks(self) -> None:
        if self.host_lease is not None or self.lock_handle is not None:
            raise AssessmentError(
                "INFRA_ERROR", "assessment lifecycle locks already acquired"
            )
        lease = acquire_host_assessment_lease()
        try:
            lock_handle = acquire_workspace_lifecycle_lock(self.workspace_root)
        except Exception:
            lease.close()
            raise
        self.host_lease = lease
        self.lock_handle = lock_handle

    def _ensure_lifecycle_locks(self) -> None:
        if self.host_lease is not None and self.lock_handle is not None:
            return
        if self.host_lease is not None or self.lock_handle is not None:
            raise AssessmentError(
                "INFRA_ERROR", "assessment lifecycle lock state is incomplete"
            )
        self._acquire_lifecycle_locks()

    def preflight(self, *, mutate: bool = True) -> None:
        if not SHA_RE.fullmatch(self.args.sha):
            raise AssessmentError(
                "BLOCKED", "--sha must be a lowercase 40-character commit SHA"
            )
        if mutate:
            self._ensure_lifecycle_locks()
            if (
                self.materials.exists()
                or self.results.exists()
                or self.worktree.exists()
            ):
                raise AssessmentError(
                    "BLOCKED", "assessment ID already has staged state"
                )
            self.materials.mkdir(parents=True)
            self.materials.chmod(0o700)
            self.materials_created = True
            self.results.mkdir(parents=True)
            self.results.chmod(0o700)
            self.results_created = True
            self.logs.mkdir()
            self.logs.chmod(0o700)
            self.base_env = build_base_environment(self.materials, self.tools)
        remote = self._git("remote", "get-url", "origin").stdout.strip()
        if remote != self.profile.repository_remote:
            raise AssessmentError("BLOCKED", "repository origin does not match profile")
        if self.profile.requires_fresh_fetch and not self.args.fetch:
            raise AssessmentError(
                "BLOCKED", "trusted-ref-only profile requires --fetch freshness"
            )
        if self.target_kind == "trusted-ref":
            if self.args.fetch:
                self._git("fetch", "origin", "--prune")
            self._git("cat-file", "-e", f"{self.args.sha}^{{commit}}")
            if self.args.expected_ref not in self.profile.trusted_refs:
                raise AssessmentError(
                    "BLOCKED",
                    "profile permits candidate execution only from an allowlisted trusted ref",
                )
            start = self._git("rev-parse", self.args.expected_ref).stdout.strip()
            self.evidence.expected_ref_start = start
            if start != self.args.sha:
                raise AssessmentError(
                    "STALE",
                    f"expected ref {self.args.expected_ref} is {start}, not requested SHA",
                )
        else:
            if self.repo != self.control_root:
                raise AssessmentError(
                    "BLOCKED",
                    "pr-head assessment must run from the trusted control checkout",
                )
            self._git("fetch", "origin", "--prune")
            control_ref = self._git("rev-parse", "origin/main").stdout.strip()
            control_head = self._git("rev-parse", "HEAD").stdout.strip()
            if not SHA_RE.fullmatch(control_ref) or not SHA_RE.fullmatch(control_head):
                raise AssessmentError(
                    "BLOCKED", "trusted main identity did not resolve exactly"
                )
            self.evidence.control_sha = control_head
            self.evidence.control_ref_start = control_ref
            if control_head != control_ref:
                raise AssessmentError(
                    "STALE",
                    "pr-head control checkout is not freshly fetched origin/main",
                )
            if self._git("status", "--porcelain=v1", "--untracked-files=all").stdout:
                raise AssessmentError(
                    "BLOCKED", "pr-head control checkout is not clean"
                )
            start = self._fetch_pr_head()
            self.evidence.pr_head_start = start
            if start != self.args.sha:
                raise AssessmentError(
                    "STALE",
                    f"canonical PR #{self.pr_number} head is {start}, not requested SHA",
                )
            self._git("cat-file", "-e", f"{self.args.sha}^{{commit}}")
            trusted_test_paths = sorted(
                {
                    selector.split("::", 1)[0]
                    for group in self.profile.pytest_groups
                    for selector in group.selectors
                }
            )
            for path in trusted_test_paths:
                control_blob = self._git(
                    "rev-parse", f"{control_head}:{path}"
                ).stdout.strip()
                candidate_blob = self._git(
                    "rev-parse", f"{self.args.sha}:{path}"
                ).stdout.strip()
                if control_blob != candidate_blob:
                    raise AssessmentError(
                        "BLOCKED",
                        f"candidate cannot replace trusted regression implementation: {path}",
                    )
            self.candidate_test_files = self._discover_candidate_test_files(
                control_head
            )
            for path in (
                "pyproject.toml",
                "pyrefly-baseline.json",
                "ci/ruff-e402-debt.toml",
                "ci/ruff-e731-debt.toml",
            ):
                control_blob = self._git(
                    "rev-parse", f"{control_head}:{path}"
                ).stdout.strip()
                candidate_blob = self._git(
                    "rev-parse", f"{self.args.sha}:{path}"
                ).stdout.strip()
                if control_blob != candidate_blob:
                    raise AssessmentError(
                        "BLOCKED",
                        f"candidate cannot replace trusted static-analysis policy: {path}",
                    )
            required_pytest_control = set(PR_TEST_CONTROL_PATHS)
            protected_pytest_control = required_pytest_control | set(
                pr_pytest_conftest_paths(self.profile.pr_test_roots)
            )
            for path in sorted(protected_pytest_control):
                self._require_matching_optional_regular_path(
                    control_head,
                    self.args.sha,
                    path,
                    allow_both_missing=path not in required_pytest_control,
                )
        for group in self.profile.pytest_groups:
            for selector in group.selectors:
                path = selector.split("::", 1)[0]
                self._git("cat-file", "-e", f"{self.args.sha}:{path}")
        self.evidence.control_fingerprint = self._fingerprint_control_plane()
        self._journal("preflight-complete")

    def plan(self) -> dict[str, Any]:
        lease = acquire_host_assessment_lease()
        try:
            self.preflight(mutate=False)
            return {
                "schema_version": SCHEMA_VERSION,
                "assessment_id": self.assessment_id,
                "target_kind": self.target_kind,
                "pr_number": self.pr_number,
                "profile": self.profile.name,
                "profile_sha256": self.evidence.profile_sha256,
                "requested_sha": self.args.sha,
                "control_sha": self.evidence.control_sha,
                "pr_head_start": self.evidence.pr_head_start,
                "candidate_test_base_sha": self.candidate_test_base_sha,
                "candidate_test_files": list(self.candidate_test_files),
                "control_fingerprint": self.evidence.control_fingerprint,
                "python_versions": list(self.profile.python_versions),
                "pytest_groups": [
                    asdict(group) for group in self.profile.pytest_groups
                ],
                "worktree": str(self.worktree),
                "materials": str(self.materials),
                "results": str(self.results),
                "gate_decision": "NOT_EVALUATED",
            }
        finally:
            try:
                lease.close()
            except OSError as exc:
                raise AssessmentError(
                    "INFRA_ERROR",
                    f"plan global lifecycle lease release failed: {exc}",
                ) from exc

    def _run_recorded(
        self,
        name: str,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout: int | None = None,
        junit: Path | None = None,
    ) -> CommandRecord:
        safe_name = re.sub(r"[^a-zA-Z0-9_.-]+", "-", name)
        stdout_path = self.logs / f"{safe_name}.stdout.log"
        stderr_path = self.logs / f"{safe_name}.stderr.log"
        started = time.time()
        outcome = run_bounded_process(
            argv,
            cwd=cwd,
            env=env,
            timeout=timeout or self.profile.command_timeout_seconds,
        )
        returncode = outcome.returncode
        self.last_raw_stdout = outcome.stdout
        self.last_raw_stderr = outcome.stderr
        stdout = _redact(self.last_raw_stdout)
        stderr = _redact(self.last_raw_stderr)
        stdout_path.write_text(stdout, encoding="utf-8")
        stderr_path.write_text(stderr, encoding="utf-8")
        stdout_path.chmod(0o600)
        stderr_path.chmod(0o600)
        junit_data = (
            junit_summary(junit) if junit is not None and junit.is_file() else None
        )
        junit_digest = (
            sha256_file(junit) if junit is not None and junit.is_file() else None
        )
        if junit is not None and junit.is_file():
            junit.chmod(0o600)
        record = CommandRecord(
            name=name,
            argv=list(argv),
            started_at=started,
            duration_seconds=round(time.time() - started, 3),
            returncode=returncode,
            stdout_path=str(stdout_path),
            stderr_path=str(stderr_path),
            stdout_sha256=sha256_file(stdout_path),
            stderr_sha256=sha256_file(stderr_path),
            junit=junit_data,
            junit_sha256=junit_digest,
            timed_out=outcome.timed_out,
        )
        self.command_records.append(record)
        if returncode != 0:
            self.failed_checks = True
        return record

    def create_worktree(self) -> None:
        if self.worktree.exists():
            raise AssessmentError(
                "BLOCKED", f"worktree path already exists: {self.worktree}"
            )
        self._git("worktree", "add", "--detach", str(self.worktree), self.args.sha)
        self.worktree_added = True
        self._journal("worktree-created")
        tested = self._control(
            [self.tools["git"], "-C", str(self.worktree), "rev-parse", "HEAD"]
        ).stdout.strip()
        if tested != self.args.sha:
            raise AssessmentError(
                "STALE", f"detached worktree resolved unexpected SHA {tested}"
            )
        status = self._control(
            [
                self.tools["git"],
                "-C",
                str(self.worktree),
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            ]
        ).stdout
        if status:
            raise AssessmentError("STALE", "new exact-SHA worktree is not clean")
        self.evidence.tested_sha = tested

    def provision_environments(self) -> dict[str, Path]:
        environments: dict[str, Path] = {}
        for version in self.profile.python_versions:
            suffix = version.replace(".", "")
            venv = (
                self.worktree / ".venv-research-store"
                if version == self.profile.static_python
                else self.materials / f"venv-py{suffix}"
            )
            manifest = self.control_root / "requirements-ci.txt"
            self._run_recorded(
                f"venv-py{suffix}",
                [self.tools["uv"], "venv", str(venv), "--python", version],
                cwd=self.control_root,
                env=self.base_env,
                timeout=600,
            )
            if self.command_records[-1].returncode != 0:
                raise AssessmentError(
                    "BLOCKED", f"failed to create Python {version} environment"
                )
            self._run_recorded(
                f"dependencies-py{suffix}",
                [
                    self.tools["uv"],
                    "pip",
                    "install",
                    "--python",
                    str(venv / "bin/python"),
                    "-r",
                    str(manifest),
                ],
                cwd=self.control_root,
                env=self.base_env,
                timeout=1200,
            )
            if self.command_records[-1].returncode != 0:
                raise AssessmentError(
                    "BLOCKED", f"failed to provision Python {version} dependencies"
                )
            version_record = self._run_recorded(
                f"python-version-py{suffix}",
                [str(venv / "bin/python"), "--version"],
                cwd=self.worktree,
                env=self.base_env,
                timeout=30,
            )
            if version_record.returncode != 0:
                raise AssessmentError("BLOCKED", f"failed to identify Python {version}")
            self.evidence.python_versions[version] = (
                Path(version_record.stdout_path).read_text(encoding="utf-8").strip()
            )
            environments[version] = venv
        return environments

    def _allocate_ports(self) -> tuple[int, int]:
        selected: list[int] = []
        for port in range(55436, 55636):
            if port in {55432, 6333}:
                continue
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                try:
                    sock.bind(("127.0.0.1", port))
                except OSError:
                    continue
            selected.append(port)
            if len(selected) == 2:
                return selected[0], selected[1]
        raise AssessmentError(
            "BLOCKED", "no disposable loopback port pair is available"
        )

    def start_services(self) -> dict[str, str]:
        pg_port, qdrant_port = self._allocate_ports()
        self.service_ports = (pg_port, qdrant_port)
        self.services_started = True
        self._journal("services-starting")
        helper = self.control_root / "scripts/disposable-test-services"
        record = self._run_recorded(
            "services-up",
            [
                str(helper),
                "--format",
                "json",
                "--namespace",
                self.assessment_id,
                "--pg-port",
                str(pg_port),
                "--qdrant-port",
                str(qdrant_port),
                "up",
            ],
            cwd=self.control_root,
            env=self.base_env,
            timeout=180,
        )
        if record.returncode != 0:
            raise AssessmentError("BLOCKED", "disposable services failed to start")
        payload = self.last_raw_stdout
        self.service_contract = parse_service_contract(payload, self.assessment_id)
        self.last_raw_stdout = ""
        self.last_raw_stderr = ""
        self._journal("services-ready")
        self.evidence.service_contract = {
            "schema_version": self.service_contract["schema_version"],
            "namespace": self.service_contract["namespace"],
            "host": self.service_contract["host"],
            "postgres": self.service_contract["postgres"],
            "qdrant": self.service_contract["qdrant"],
            "images": self.service_contract["images"],
        }
        return dict(self.service_contract["environment"])

    def run_static(self, environments: Mapping[str, Path]) -> None:
        venv = environments[self.profile.static_python]
        environment = dict(self.base_env)
        environment["PATH"] = os.pathsep.join(
            [str(venv / "bin"), environment.get("PATH", "")]
        )
        base_sha = (
            self.candidate_test_base_sha
            if self.target_kind == "pr-head"
            and self.candidate_test_base_sha is not None
            else self.args.sha
        )
        self._run_recorded(
            "central-static-profile",
            [
                str(venv / "bin/python"),
                str(self.control_root / "scripts/run_ci_profile.py"),
                "--repo",
                str(self.worktree),
                "--profile",
                "static",
                "--base-sha",
                base_sha,
                "--head-sha",
                self.args.sha,
                "--namespace",
                self.assessment_id,
            ],
            cwd=self.worktree,
            env=environment,
        )

    def _pytest_policy_args(self, root: Path) -> list[str]:
        return [
            "-q",
            "-ra",
            "-p",
            "no:cacheprovider",
            "--import-mode=importlib",
            "-c",
            "/dev/null",
            "--rootdir",
            str(root),
            "--confcutdir",
            str(root),
        ]

    def _collect_pytest_nodes(
        self,
        name: str,
        python: Path,
        selectors: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        max_nodes: int,
        failure_status: str,
        reject_filtered_collection: bool = False,
        blocked_test_module_plugins: Sequence[str] = (),
    ) -> tuple[str, ...]:
        allowed_files = sorted({selector.split("::", 1)[0] for selector in selectors})
        argv = [
            *pytest_entry_argv(python, blocked_test_module_plugins),
            *self._pytest_policy_args(cwd),
            "--collect-only",
            *selectors,
        ]
        junit: Path | None = None
        if reject_filtered_collection:
            junit = self.results / f"collect-{name}.xml"
            argv.extend(["--color=no", f"--junitxml={junit}"])
        record = self._run_recorded(
            f"collect-{name}",
            argv,
            cwd=cwd,
            env=env,
            junit=junit,
        )
        if record.returncode != 0:
            raise AssessmentError(failure_status, f"pytest collection failed: {name}")
        nodes = parse_collected_nodeids(
            self.last_raw_stdout,
            allowed_files,
            max_nodes,
        )
        collection_total: int | None = None
        if reject_filtered_collection:
            collection_total = parse_collection_total(
                self.last_raw_stdout, failure_status=failure_status
            )
        if reject_filtered_collection and (
            record.junit is None
            or record.junit["skipped"] != 0
            or record.junit["failed"] != 0
            or record.junit["errors"] != 0
            or collection_total != len(nodes)
        ):
            raise AssessmentError(
                failure_status,
                f"pytest collection omitted or filtered candidate tests: {name}",
            )
        return nodes

    def _apply_pytest_expectations(
        self,
        record: CommandRecord,
        expected_tests: int,
        *,
        expected_skips: int | None = None,
    ) -> None:
        if expected_skips is None:
            expected_skips = self.profile.expected_skips
        record.expected_tests = expected_tests
        record.expected_skips = expected_skips
        expected_passed = expected_tests - expected_skips
        record.junit_check_passed = bool(
            record.junit
            and record.junit["tests"] == expected_tests
            and record.junit["passed"] == expected_passed
            and record.junit["failed"] == 0
            and record.junit["errors"] == 0
            and record.junit["skipped"] == expected_skips
        )
        if record.junit is None:
            self.failed_checks = True
            self.evidence.anomalies.append(f"{record.name}: JUnit evidence is missing")
        elif not record.junit_check_passed:
            self.failed_checks = True
            self.evidence.anomalies.append(
                f"{record.name}: expected {expected_tests} tests with "
                f"{expected_skips} classified skips; observed {record.junit}"
            )

    def _verify_pytest_skips(
        self,
        name: str,
        python: Path,
        junit: Path,
        scope_selectors: Sequence[str],
        *,
        env: Mapping[str, str],
    ) -> int | None:
        output = self.results / f"{name}-skips.json"
        argv = [
            str(python),
            str(self.control_root / PYTEST_SKIP_VERIFIER_PATH),
            "--junitxml",
            str(junit),
            "--allowlist",
            str(self.control_root / PYTEST_SKIP_ALLOWLIST_PATH),
            "--output",
            str(output),
        ]
        for selector in sorted(set(scope_selectors)):
            argv.extend(["--scope-selector", selector])
        record = self._run_recorded(
            f"verify-skips-{name}",
            argv,
            cwd=self.control_root,
            env=env,
        )
        if record.returncode != 0:
            self.evidence.anomalies.append(
                f"{name}: classified pytest skip verification failed"
            )
            return None
        try:
            report = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AssessmentError(
                "BLOCKED", f"{name}: pytest skip verifier output is unavailable"
            ) from exc
        skip_count = report.get("skip_count")
        if report.get("status") != "passed" or not isinstance(skip_count, int):
            raise AssessmentError(
                "BLOCKED", f"{name}: pytest skip verifier output is malformed"
            )
        return skip_count

    def _collect_candidate_pytest_nodes_isolated(
        self,
        python: Path,
        runtime_env: Mapping[str, str],
    ) -> tuple[str, ...]:
        """Collect each changed candidate test module in a fresh pytest process."""
        candidate_nodes: list[str] = []
        for index, path in enumerate(self.candidate_test_files, start=1):
            remaining = self.profile.pr_test_max_nodes - len(candidate_nodes)
            if remaining <= 0:
                raise AssessmentError(
                    "BLOCKED",
                    f"collected pytest node count exceeds bound {self.profile.pr_test_max_nodes}",
                )
            file_nodes = self._collect_pytest_nodes(
                f"candidate-regressions-{index:03d}",
                python,
                (path,),
                cwd=self.worktree,
                env=runtime_env,
                max_nodes=remaining,
                failure_status="FAIL",
                reject_filtered_collection=True,
                blocked_test_module_plugins=self.candidate_test_files,
            )
            if not file_nodes:
                raise AssessmentError(
                    "FAIL", f"pytest collection omitted candidate test module: {path}"
                )
            if any(node.split("::", 1)[0] != path for node in file_nodes):
                raise AssessmentError(
                    "BLOCKED",
                    f"candidate collection returned unexpected module while collecting {path}",
                )
            candidate_nodes.extend(file_nodes)
        if len(candidate_nodes) != len(set(candidate_nodes)):
            raise AssessmentError(
                "BLOCKED",
                "candidate collection returned duplicate node IDs across isolated modules",
            )
        return tuple(sorted(candidate_nodes))

    def _run_candidate_pytest_nodes_isolated(
        self,
        python: Path,
        candidate_nodes: Sequence[str],
        runtime_env: Mapping[str, str],
    ) -> None:
        """Execute each changed candidate test module in a fresh pytest process."""
        nodes_by_file: dict[str, list[str]] = {
            path: [] for path in self.candidate_test_files
        }
        for node in candidate_nodes:
            path = node.split("::", 1)[0]
            if path not in nodes_by_file:
                raise AssessmentError(
                    "BLOCKED",
                    f"candidate manifest contains unexpected test module: {path}",
                )
            nodes_by_file[path].append(node)

        suffix = self.profile.pr_test_python.replace(".", "")
        for index, path in enumerate(self.candidate_test_files, start=1):
            file_nodes = tuple(nodes_by_file[path])
            if not file_nodes:
                raise AssessmentError(
                    "FAIL", f"candidate test module has no executable nodes: {path}"
                )
            self._run_exact_pytest_nodes(
                f"pytest-candidate-regressions-{index:03d}-py{suffix}",
                python,
                file_nodes,
                len(file_nodes),
                env=runtime_env,
                blocked_test_module_plugins=self.candidate_test_files,
                classify_skips=True,
            )

    def _run_exact_pytest_nodes(
        self,
        name: str,
        python: Path,
        node_ids: Sequence[str],
        expected_tests: int,
        *,
        env: Mapping[str, str],
        blocked_test_module_plugins: Sequence[str] = (),
        classify_skips: bool = False,
    ) -> None:
        junit = self.results / f"{name}.xml"
        record = self._run_recorded(
            name,
            [
                *pytest_entry_argv(python, blocked_test_module_plugins),
                *self._pytest_policy_args(self.worktree),
                f"--junitxml={junit}",
                *node_ids,
            ],
            cwd=self.worktree,
            env=env,
            junit=junit,
        )
        if not classify_skips:
            self._apply_pytest_expectations(record, expected_tests)
            return
        if record.junit is None:
            self._apply_pytest_expectations(record, expected_tests)
            return
        scope_selectors = sorted({node.split("::", 1)[0] for node in node_ids})
        expected_skips = self._verify_pytest_skips(
            name,
            python,
            junit,
            scope_selectors,
            env=env,
        )
        if expected_skips is None:
            record.expected_tests = expected_tests
            record.expected_skips = None
            record.junit_check_passed = False
            return
        self._apply_pytest_expectations(
            record,
            expected_tests,
            expected_skips=expected_skips,
        )

    def _run_pr_pytest(
        self,
        environments: Mapping[str, Path],
        runtime_env: Mapping[str, str],
    ) -> None:
        trusted_memberships: dict[tuple[str, str], tuple[str, ...]] = {}
        for group in self.profile.pytest_groups:
            for version in group.python_versions:
                nodes = self._collect_pytest_nodes(
                    f"trusted-{group.name}-py{version.replace('.', '')}",
                    environments[version] / "bin/python",
                    group.selectors,
                    cwd=self.control_root,
                    env=runtime_env,
                    max_nodes=group.expected_tests,
                    failure_status="BLOCKED",
                )
                if len(nodes) != group.expected_tests:
                    raise AssessmentError(
                        "BLOCKED",
                        f"trusted profile membership drifted for {group.name}: "
                        f"expected {group.expected_tests}, collected {len(nodes)}",
                    )
                trusted_memberships[(group.name, version)] = nodes

        candidate_nodes: tuple[str, ...] = ()
        if self.candidate_test_files:
            candidate_python = environments[self.profile.pr_test_python] / "bin/python"
            try:
                candidate_nodes = self._collect_candidate_pytest_nodes_isolated(
                    candidate_python,
                    runtime_env,
                )
            except AssessmentError:
                self.evidence.candidate_test_manifest = build_candidate_test_manifest(
                    self.candidate_test_base_sha or "",
                    self.candidate_test_files,
                    (),
                )
                raise
        self.evidence.candidate_test_manifest = build_candidate_test_manifest(
            self.candidate_test_base_sha or "",
            self.candidate_test_files,
            candidate_nodes,
        )

        for group in self.profile.pytest_groups:
            for version in group.python_versions:
                suffix = version.replace(".", "")
                self._run_exact_pytest_nodes(
                    f"pytest-{group.name}-py{suffix}",
                    environments[version] / "bin/python",
                    trusted_memberships[(group.name, version)],
                    group.expected_tests,
                    env=runtime_env,
                    blocked_test_module_plugins=self.candidate_test_files,
                )
        if candidate_nodes:
            self._run_candidate_pytest_nodes_isolated(
                environments[self.profile.pr_test_python] / "bin/python",
                candidate_nodes,
                runtime_env,
            )

    def run_pytest(
        self,
        environments: Mapping[str, Path],
        service_env: Mapping[str, str],
    ) -> None:
        runtime_env = dict(self.base_env)
        runtime_env.update(service_env)
        runtime_env.update(self.profile.environment)
        if self.profile.requires_disposable_services:
            runtime_env["DATABASE_URL"] = service_env[
                "RESEARCH_STORE_TEST_DATABASE_URL"
            ]
        runtime_env["BLOB_ROOT"] = str(self.materials / "blob-root")
        Path(runtime_env["BLOB_ROOT"]).mkdir(parents=True, exist_ok=True)
        if self.target_kind == "pr-head":
            self._run_pr_pytest(environments, runtime_env)
            return
        for group in self.profile.pytest_groups:
            for version in group.python_versions:
                suffix = version.replace(".", "")
                junit = self.results / f"pytest-{group.name}-py{suffix}.xml"
                record = self._run_recorded(
                    f"pytest-{group.name}-py{suffix}",
                    [
                        str(environments[version] / "bin/python"),
                        "-m",
                        "pytest",
                        "-q",
                        "-ra",
                        "-p",
                        "no:cacheprovider",
                        "--import-mode=importlib",
                        "--confcutdir",
                        str(self.worktree),
                        f"--junitxml={junit}",
                        *group.selectors,
                    ],
                    cwd=self.worktree,
                    env=runtime_env,
                    junit=junit,
                )
                record.expected_tests = group.expected_tests
                record.expected_skips = self.profile.expected_skips
                record.junit_check_passed = bool(
                    record.junit
                    and record.junit["tests"] == group.expected_tests
                    and record.junit["passed"] == group.expected_tests
                    and record.junit["failed"] == 0
                    and record.junit["errors"] == 0
                    and record.junit["skipped"] == self.profile.expected_skips
                )
                if record.junit is None:
                    self.failed_checks = True
                    self.evidence.anomalies.append(
                        f"{record.name}: JUnit evidence is missing"
                    )
                elif not record.junit_check_passed:
                    self.failed_checks = True
                    self.evidence.anomalies.append(
                        f"{record.name}: expected {group.expected_tests} passing tests and zero skips; observed {record.junit}"
                    )

    def reset_qdrant(self) -> None:
        if not self.service_contract:
            raise AssessmentError(
                "INFRA_ERROR", "service contract unavailable for Qdrant reset"
            )
        pg_port = str(self.service_contract["postgres"]["port"])
        qdrant_port = str(self.service_contract["qdrant"]["port"])
        helper = self.control_root / "scripts/disposable-test-services"
        record = self._run_recorded(
            "qdrant-reset",
            [
                str(helper),
                "--format",
                "json",
                "--namespace",
                self.assessment_id,
                "--pg-port",
                pg_port,
                "--qdrant-port",
                qdrant_port,
                "reset-qdrant",
            ],
            cwd=self.control_root,
            env=self.base_env,
            timeout=120,
        )
        if record.returncode != 0:
            raise AssessmentError("INFRA_ERROR", "Qdrant reset failed")
        refreshed = parse_service_contract(self.last_raw_stdout, self.assessment_id)
        self.last_raw_stdout = ""
        self.last_raw_stderr = ""
        ready = self._run_recorded(
            "qdrant-ready",
            [
                self.tools["curl"],
                "--fail",
                "--silent",
                "--show-error",
                refreshed["qdrant"]["ready_url"],
            ],
            cwd=self.control_root,
            env=self.base_env,
            timeout=30,
        )
        if ready.returncode != 0:
            raise AssessmentError("INFRA_ERROR", "fresh Qdrant did not satisfy /readyz")

    def final_identity(self) -> None:
        head = self._control(
            [self.tools["git"], "-C", str(self.worktree), "rev-parse", "HEAD"]
        ).stdout.strip()
        status = self._control(
            [
                self.tools["git"],
                "-C",
                str(self.worktree),
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            ]
        ).stdout
        diff = self._control(
            [self.tools["git"], "-C", str(self.worktree), "diff", "--check"]
        ).stdout
        if head != self.args.sha or status or diff:
            raise AssessmentError("STALE", "final exact-SHA worktree proof failed")
        if self.target_kind == "trusted-ref" and self.args.expected_ref:
            if self.args.fetch:
                self._git("fetch", "origin", "--prune")
            end = self._git("rev-parse", self.args.expected_ref).stdout.strip()
            self.evidence.expected_ref_end = end
            if end != self.evidence.expected_ref_start:
                raise AssessmentError(
                    "STALE", f"expected ref moved during assessment: {end}"
                )
        elif self.target_kind == "pr-head":
            self._git("fetch", "origin", "--prune")
            control_end = self._git("rev-parse", "origin/main").stdout.strip()
            control_head_end = self._git("rev-parse", "HEAD").stdout.strip()
            control_status_end = self._git(
                "status", "--porcelain=v1", "--untracked-files=all"
            ).stdout
            self.evidence.control_ref_end = control_end
            if (
                control_end != self.evidence.control_ref_start
                or control_end != self.evidence.control_sha
            ):
                raise AssessmentError(
                    "STALE",
                    f"trusted control ref moved during assessment: {control_end}",
                )
            if (
                control_head_end != self.evidence.control_sha
                or control_head_end != control_end
            ):
                raise AssessmentError(
                    "STALE",
                    f"trusted control checkout moved during assessment: {control_head_end}",
                )
            if control_status_end:
                raise AssessmentError(
                    "STALE", "trusted control checkout became dirty during assessment"
                )
            pr_end = self._fetch_pr_head()
            self.evidence.pr_head_end = pr_end
            if pr_end != self.evidence.pr_head_start or pr_end != self.args.sha:
                raise AssessmentError(
                    "STALE", f"canonical PR #{self.pr_number} head moved: {pr_end}"
                )

    def cleanup(self) -> list[str]:
        failures: list[str] = []
        try:
            self._journal("cleanup-started")
        except Exception as exc:  # noqa: BLE001 - cleanup must remain best effort
            failures.append(f"cleanup journal start failed: {type(exc).__name__}")
        if self.services_started and self.service_ports:
            try:
                helper = self.control_root / "scripts/disposable-test-services"
                command = [
                    str(helper),
                    "--format",
                    "json",
                    "--namespace",
                    self.assessment_id,
                    "--pg-port",
                    str(self.service_ports[0]),
                    "--qdrant-port",
                    str(self.service_ports[1]),
                    "down",
                ]
                completed = run_bounded_process(
                    command,
                    cwd=self.control_root,
                    env=self.base_env,
                    timeout=120,
                )
                if completed.returncode != 0:
                    failures.append("disposable service teardown failed")
                containers = (
                    f"{self.assessment_id}_pg",
                    f"{self.assessment_id}_qdrant",
                )
                remaining: list[str] = []
                for name in containers:
                    inspection = run_bounded_process(
                        [self.tools["docker"], "inspect", name],
                        cwd=self.control_root,
                        env=self.base_env,
                        timeout=30,
                    )
                    if inspection.timed_out:
                        failures.append(f"container inspection timed out: {name}")
                    elif inspection.returncode == 0:
                        remaining.append(name)
                if remaining:
                    failures.append(f"helper-owned containers remain: {remaining}")
                elif not any(
                    failure.startswith("container inspection timed out:")
                    for failure in failures
                ):
                    self.services_started = False
            except Exception as exc:  # noqa: BLE001 - cleanup must remain best effort
                failures.append(f"service cleanup raised {type(exc).__name__}: {exc}")
        if self.worktree_added:
            try:
                completed = self._git(
                    "worktree", "remove", "--force", str(self.worktree), check=False
                )
                if completed.returncode != 0:
                    failures.append("git worktree removal failed")
                else:
                    self.worktree_added = False
                    self._git("worktree", "prune", check=False)
            except Exception as exc:  # noqa: BLE001 - cleanup must remain best effort
                failures.append(f"worktree cleanup raised {type(exc).__name__}: {exc}")
        if self.materials_created and not self.args.keep_materials:
            try:
                if self.materials.exists():
                    shutil.rmtree(self.materials)
            except Exception as exc:  # noqa: BLE001 - cleanup must remain best effort
                failures.append(f"materials cleanup raised {type(exc).__name__}: {exc}")
        if self.lock_handle is not None:
            try:
                self._journal("cleanup-complete" if not failures else "cleanup-failed")
            except Exception as exc:  # noqa: BLE001 - cleanup must remain best effort
                failures.append(f"cleanup journal finish failed: {type(exc).__name__}")
        return failures

    def _release_lifecycle_locks(self) -> list[str]:
        """Release workspace and host-wide locks after the final isolation audit."""

        failures: list[str] = []
        if self.lock_handle is not None:
            lock_handle = self.lock_handle
            self.lock_handle = None
            try:
                fcntl.flock(lock_handle, fcntl.LOCK_UN)
            except Exception as exc:  # noqa: BLE001 - terminal release fails closed
                failures.append(f"lock unlock raised {type(exc).__name__}: {exc}")
            try:
                lock_handle.close()
            except Exception as exc:  # noqa: BLE001 - terminal release fails closed
                failures.append(f"lock close raised {type(exc).__name__}: {exc}")
        if self.host_lease is not None:
            try:
                self.host_lease.close()
            except OSError as exc:
                failures.append(
                    f"global lifecycle lease release raised {type(exc).__name__}: {exc}"
                )
            finally:
                self.host_lease = None
        return failures

    def execute(self) -> int:
        status = "INFRA_ERROR"
        host_blob_root = Path.home() / ".local/share/firecrawl/blobs"
        before_host_blobs: dict[str, dict[str, Any]] = {}
        host_blob_baseline_captured = False
        error_message: str | None = None
        try:
            if not SHA_RE.fullmatch(self.args.sha):
                raise AssessmentError(
                    "BLOCKED", "--sha must be a lowercase 40-character commit SHA"
                )
            self._ensure_lifecycle_locks()
            before_host_blobs = inventory(host_blob_root)
            host_blob_baseline_captured = True
            self.preflight()
            self.create_worktree()
            environments = self.provision_environments()
            service_env: dict[str, str] = {}
            if self.profile.requires_disposable_services:
                service_env = self.start_services()
            self.run_static(environments)
            self.run_pytest(environments, service_env)
            if self.profile.reset_qdrant_after_tests:
                self.reset_qdrant()
            self.final_identity()
            status = "FAIL" if self.failed_checks else "PASS"
        except AssessmentError as exc:
            status = exc.status
            error_message = str(exc)
            self.evidence.anomalies.append(str(exc))
        except (
            OSError,
            ValueError,
            TypeError,
            json.JSONDecodeError,
            ET.ParseError,
        ) as exc:
            status = "INFRA_ERROR"
            error_message = f"{type(exc).__name__}: {exc}"
            self.evidence.anomalies.append(error_message)
        finally:
            try:
                cleanup_failures = self.cleanup()
            except Exception as exc:  # noqa: BLE001 - preserve typed evidence
                cleanup_failures = [
                    f"cleanup orchestration raised {type(exc).__name__}: {exc}"
                ]
            if host_blob_baseline_captured:
                try:
                    after_host_blobs = inventory(host_blob_root)
                    changed_host_blobs = sorted(
                        key
                        for key in before_host_blobs.keys() | after_host_blobs.keys()
                        if before_host_blobs.get(key) != after_host_blobs.get(key)
                    )
                except Exception as exc:  # noqa: BLE001 - isolation fails closed
                    changed_host_blobs = ["<inventory-failed>"]
                    self.evidence.anomalies.append(
                        f"host blob inventory failed closed: {type(exc).__name__}: {exc}"
                    )
            else:
                changed_host_blobs = []
            if changed_host_blobs:
                status = "ISOLATION_BREACH"
                self.evidence.anomalies.append(
                    f"host default blob store changed: {changed_host_blobs[:20]}"
                )
            release_failures = self._release_lifecycle_locks()
            cleanup_failures.extend(release_failures)
            if cleanup_failures:
                if status != "ISOLATION_BREACH":
                    status = "INFRA_ERROR"
                self.evidence.anomalies.extend(cleanup_failures)
            self.evidence.cleanup = {
                "services_removed": not self.services_started,
                "worktree_removed": not self.worktree_added,
                "materials_removed": not self.materials_created
                or not self.materials.exists(),
                "materials_retained_by_policy": bool(
                    self.materials_created
                    and self.materials.exists()
                    and self.args.keep_materials
                ),
                "failures": cleanup_failures,
            }
            self.evidence.host_evidence_result = status
            self.evidence.commands = [asdict(record) for record in self.command_records]
            self.evidence.finished_at = time.time()
            if error_message:
                self.evidence.cleanup["terminal_error"] = error_message
            try:
                self.write_evidence()
            except Exception as exc:  # noqa: BLE001 - preserve terminal reporting
                print(
                    f"local-agent-assessment: evidence write failed: {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
                status = "INFRA_ERROR"
        return EXIT_CODES[status]

    def write_evidence(self) -> None:
        if not self.results_created:
            print(json.dumps(asdict(self.evidence), sort_keys=True), file=sys.stderr)
            return
        self.results.mkdir(parents=True, exist_ok=True)
        json_path = self.results / "assessment.json"
        passed = sum(
            record.junit["passed"]
            for record in self.command_records
            if record.junit is not None
        )
        failed = sum(
            record.junit["failed"] + record.junit["errors"]
            for record in self.command_records
            if record.junit is not None
        )
        skipped = sum(
            record.junit["skipped"]
            for record in self.command_records
            if record.junit is not None
        )
        markdown = "\n".join(
            [
                "# Local host assessment",
                "",
                f"- HOST_EVIDENCE_RESULT: {self.evidence.host_evidence_result}",
                "- GATE_DECISION: NOT_EVALUATED",
                f"- ASSESSMENT_SHA: {self.evidence.tested_sha or self.evidence.requested_sha}",
                f"- TARGET_KIND: {self.evidence.target_kind}",
                f"- PR_NUMBER: {self.evidence.pr_number or 'none'}",
                f"- PROFILE: {self.evidence.profile}",
                f"- TESTS: passed={passed} failed={failed} skipped={skipped}",
                f"- CLEANUP: {'PASS' if not self.evidence.cleanup.get('failures') else 'FAIL'}",
                f"- ANOMALIES: {len(self.evidence.anomalies)}",
                f"- EVIDENCE_JSON: {json_path}",
                "",
            ]
        )
        markdown_path = self.results / "assessment.md"
        markdown_temporary = markdown_path.with_suffix(".md.tmp")
        json_temporary = json_path.with_suffix(".json.tmp")
        try:
            markdown_temporary.write_text(markdown, encoding="utf-8")
            markdown_temporary.chmod(0o600)
            os.replace(markdown_temporary, markdown_path)
            json_temporary.write_text(
                json.dumps(asdict(self.evidence), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            json_temporary.chmod(0o600)
            os.replace(json_temporary, json_path)
        except Exception:
            json_temporary.unlink(missing_ok=True)
            markdown_temporary.unlink(missing_ok=True)
            json_path.unlink(missing_ok=True)
            markdown_path.unlink(missing_ok=True)
            raise
        try:
            print(markdown, end="")
        except OSError:
            pass


def require_exact_recovery_path(
    raw: Any, expected: Path, root: Path, label: str
) -> Path:
    if not isinstance(raw, str) or raw != str(expected):
        raise AssessmentError(
            "BLOCKED", f"recovery {label} path does not match assessment identity"
        )
    resolved_root = root.resolve()
    resolved_expected = expected.resolve(strict=False)
    if (
        resolved_expected == resolved_root
        or not resolved_expected.is_relative_to(resolved_root)
        or resolved_expected != expected
    ):
        raise AssessmentError(
            "BLOCKED", f"recovery {label} path has a symlinked ancestor"
        )
    current = expected
    while current != root:
        if current.is_symlink():
            raise AssessmentError(
                "BLOCKED", f"recovery {label} path has a symlinked component"
            )
        if root not in current.parents:
            raise AssessmentError("BLOCKED", f"recovery {label} escaped root")
        current = current.parent
    return expected


def recover_abandoned(args: argparse.Namespace) -> int:
    allowed_root = Path(
        os.environ.get("LOCAL_AGENT_ASSESSMENT_ALLOWED_ROOT", "/tmp/opencode/verify")
    ).resolve()
    workspace_root = Path(args.workspace_root).resolve()
    if workspace_root != allowed_root:
        raise AssessmentError(
            "BLOCKED", f"workspace root must equal sanctioned root {allowed_root}"
        )
    if not ASSESSMENT_ID_RE.fullmatch(args.assessment_id):
        raise AssessmentError("BLOCKED", "invalid recovery assessment ID")
    results = workspace_root / "results" / args.assessment_id
    require_exact_recovery_path(str(results), results, workspace_root, "results")
    journal_path = results / "lifecycle.json"
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    if journal.get("schema_version") != LIFECYCLE_SCHEMA_VERSION:
        raise AssessmentError("BLOCKED", "unsupported lifecycle journal schema")
    if journal.get("assessment_id") != args.assessment_id:
        raise AssessmentError("BLOCKED", "lifecycle journal identity mismatch")
    repo = Path(args.repo).resolve()
    if Path(str(journal.get("repo"))).resolve() != repo:
        raise AssessmentError("BLOCKED", "recovery repository does not match journal")
    expected_worktree = workspace_root / "worktrees" / args.assessment_id
    expected_materials = workspace_root / "materials" / args.assessment_id
    worktree = require_exact_recovery_path(
        journal.get("worktree"), expected_worktree, workspace_root, "worktree"
    )
    materials = require_exact_recovery_path(
        journal.get("materials"), expected_materials, workspace_root, "materials"
    )
    ports = journal.get("service_ports")
    if ports is not None and (
        not isinstance(ports, list)
        or len(ports) != 2
        or not all(isinstance(port, int) and 1024 <= port <= 65535 for port in ports)
        or len(set(ports)) != 2
        or set(ports) & {55432, 6333}
    ):
        raise AssessmentError("BLOCKED", "invalid service ports in lifecycle journal")

    tools: dict[str, str] = {}
    for name in ("bash", "curl", "docker", "git", "uv"):
        resolved = shutil.which(name)
        if not resolved:
            raise AssessmentError(
                "BLOCKED", f"required recovery command not found: {name}"
            )
        tools[name] = str(Path(resolved).resolve())

    host_lease = acquire_host_assessment_lease()
    try:
        lock_handle = acquire_workspace_lifecycle_lock(workspace_root)
    except Exception:
        host_lease.close()
        raise

    failures: list[str] = []
    try:
        recovery_materials = materials / "recovery"
        environment = build_base_environment(recovery_materials, tools)
        if ports is not None:
            try:
                helper = (
                    Path(__file__).resolve().parents[1]
                    / "scripts/disposable-test-services"
                )
                completed = run_bounded_process(
                    [
                        str(helper),
                        "--format",
                        "json",
                        "--namespace",
                        args.assessment_id,
                        "--pg-port",
                        str(ports[0]),
                        "--qdrant-port",
                        str(ports[1]),
                        "down",
                    ],
                    cwd=helper.parent.parent,
                    env=environment,
                    timeout=120,
                )
                if completed.returncode != 0:
                    failures.append("disposable service recovery teardown failed")
            except Exception as exc:  # noqa: BLE001 - recovery is best effort
                failures.append(f"service recovery raised {type(exc).__name__}: {exc}")

        try:
            listed = run_bounded_process(
                [tools["git"], "-C", str(repo), "worktree", "list", "--porcelain"],
                cwd=repo,
                env=environment,
                timeout=30,
            )
            if listed.returncode != 0:
                failures.append("could not inspect registered Git worktrees")
            elif f"worktree {worktree}\n" in listed.stdout:
                removed = run_bounded_process(
                    [
                        tools["git"],
                        "-C",
                        str(repo),
                        "worktree",
                        "remove",
                        "--force",
                        str(worktree),
                    ],
                    cwd=repo,
                    env=environment,
                    timeout=120,
                )
                if removed.returncode != 0:
                    failures.append("Git worktree recovery removal failed")
        except Exception as exc:  # noqa: BLE001 - recovery is best effort
            failures.append(f"worktree recovery raised {type(exc).__name__}: {exc}")
        try:
            if materials.exists():
                shutil.rmtree(materials)
        except Exception as exc:  # noqa: BLE001 - recovery is best effort
            failures.append(f"materials recovery raised {type(exc).__name__}: {exc}")

        try:
            journal["stage"] = (
                "recovery-complete" if not failures else "recovery-failed"
            )
            journal["recovered_at"] = time.time()
            journal["recovery_failures"] = failures
            journal["services_started"] = bool(failures)
            journal["worktree_added"] = bool(failures)
            temporary = journal_path.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(journal, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            temporary.chmod(0o600)
            os.replace(temporary, journal_path)
        except Exception as exc:  # noqa: BLE001 - preserve typed recovery result
            failures.append(f"recovery journal raised {type(exc).__name__}: {exc}")
    finally:
        try:
            fcntl.flock(lock_handle, fcntl.LOCK_UN)
        except Exception as exc:  # noqa: BLE001 - preserve typed recovery result
            failures.append(
                f"recovery workspace-lock unlock raised {type(exc).__name__}: {exc}"
            )
        try:
            lock_handle.close()
        except Exception as exc:  # noqa: BLE001 - preserve typed recovery result
            failures.append(
                f"recovery workspace-lock close raised {type(exc).__name__}: {exc}"
            )
        try:
            host_lease.close()
        except OSError as exc:
            failures.append(
                f"recovery global-lease release raised {type(exc).__name__}: {exc}"
            )
    result = {
        "schema_version": "local-agent-assessment-recovery-v1",
        "recovery_result": "PASS" if not failures else "FAIL",
        "assessment_id": args.assessment_id,
        "recovery_failures": failures,
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if not failures else EXIT_CODES["INFRA_ERROR"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("plan", "run"):
        child = subparsers.add_parser(command)
        child.add_argument("--repo", default=".")
        child.add_argument("--sha", required=True)
        child.add_argument("--profile", required=True)
        child.add_argument("--assessment-id")
        child.add_argument(
            "--target-kind",
            choices=("trusted-ref", "pr-head"),
            default="trusted-ref",
        )
        child.add_argument("--pr", type=int)
        child.add_argument("--expected-ref")
        child.add_argument("--fetch", action="store_true")
        child.add_argument("--workspace-root", default="/tmp/opencode/verify")
        child.add_argument("--keep-materials", action="store_true")
    recover = subparsers.add_parser("recover")
    recover.add_argument("--repo", default=".")
    recover.add_argument("--assessment-id", required=True)
    recover.add_argument("--workspace-root", default="/tmp/opencode/verify")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        enable_child_subreaper()
        if args.command == "recover":
            return recover_abandoned(args)
        runner = Runner(args)
        if args.command == "plan":
            plan = runner.plan()
            print(json.dumps(plan, indent=2, sort_keys=True))
            return 0
        return runner.execute()
    except (
        AssessmentError,
        OSError,
        ValueError,
        TypeError,
        json.JSONDecodeError,
    ) as exc:
        status = exc.status if isinstance(exc, AssessmentError) else "BLOCKED"
        if args.command == "recover":
            print(
                json.dumps(
                    {
                        "schema_version": "local-agent-assessment-recovery-v1",
                        "recovery_result": "FAIL",
                        "assessment_id": args.assessment_id,
                        "recovery_failures": [f"{type(exc).__name__}: {exc}"],
                    },
                    sort_keys=True,
                ),
                file=sys.stderr,
            )
            return EXIT_CODES[status]
        print(
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "host_evidence_result": status,
                    "gate_decision": "NOT_EVALUATED",
                    "error": f"{type(exc).__name__}: {exc}",
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return EXIT_CODES[status]


if __name__ == "__main__":
    raise SystemExit(main())
