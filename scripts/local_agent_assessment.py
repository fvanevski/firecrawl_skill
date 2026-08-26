"""Deterministic exact-SHA host-evidence runner.

The controlling checkout owns this module, its profiles, dependency locks, the
disposable-service helper, and skip policy.  Candidate source is mounted only
through a detached worktree and never supplies orchestration commands.
"""

from __future__ import annotations

import argparse
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
import xml.etree.ElementTree as ET
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import tomllib

SCHEMA_VERSION = "local-agent-assessment-v1"
PROFILE_SCHEMA_VERSION = 1
SERVICE_SCHEMA_VERSION = "firecrawl-disposable-services-v1"
LIFECYCLE_SCHEMA_VERSION = "local-agent-assessment-lifecycle-v1"
CONTROL_COMMAND_TIMEOUT_SECONDS = 300
PROCESS_TERMINATION_GRACE_SECONDS = 5.0
ALLOWED_PYTHONS = {"3.11", "3.12"}
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


class AssessmentError(RuntimeError):
    def __init__(self, status: str, message: str) -> None:
        super().__init__(message)
        self.status = status


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
    profile: str = ""
    profile_sha256: str = ""
    requested_sha: str = ""
    tested_sha: str | None = None
    expected_ref: str | None = None
    expected_ref_start: str | None = None
    expected_ref_end: str | None = None
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
    """Run one command in an owned process group and reap it before returning."""
    process = subprocess.Popen(
        list(argv),
        cwd=cwd,
        env=dict(env) if env is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        if process.returncode is None:
            raise RuntimeError("bounded subprocess did not terminate")
        return ProcessOutcome(
            returncode=process.returncode,
            stdout=stdout or "",
            stderr=stderr or "",
        )
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = process.communicate(timeout=terminate_grace_seconds)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = process.communicate()
        return ProcessOutcome(
            returncode=124,
            stdout=stdout or "",
            stderr=(stderr or "") + "\ncommand timed out\n",
            timed_out=True,
        )


class Runner:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.control_root = Path(__file__).resolve().parents[1]
        self.profile_path = (
            self.control_root / "references/local-agent-assessment-profiles.toml"
        )
        self.profile = load_profile(self.profile_path, args.profile)
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
        self.lock_handle: Any = None
        self.failed_checks = False
        self.service_ports: tuple[int, int] | None = None

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
        }
        for version in self.profile.python_versions:
            suffix = version.replace(".", "")
            paths[f"dependencies_py{suffix}"] = (
                self.control_root
                / f"requirements-local-agent-assessment-py{suffix}.lock"
            )
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
            [self.tools["git"], "-C", str(self.repo), *args], check=check
        )

    def preflight(self, *, mutate: bool = True) -> None:
        if not SHA_RE.fullmatch(self.args.sha):
            raise AssessmentError(
                "BLOCKED", "--sha must be a lowercase 40-character commit SHA"
            )
        if mutate:
            self.workspace_root.mkdir(parents=True, exist_ok=True)
            lock_dir = self.workspace_root / ".locks"
            lock_dir.mkdir(exist_ok=True)
            self.lock_handle = (lock_dir / "host-assessment.lock").open("a+")
            try:
                fcntl.flock(self.lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise AssessmentError(
                    "BLOCKED", "another host assessment owns the lifecycle lock"
                ) from exc
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
        for group in self.profile.pytest_groups:
            for selector in group.selectors:
                path = selector.split("::", 1)[0]
                self._git("cat-file", "-e", f"{self.args.sha}:{path}")
        self.evidence.control_fingerprint = self._fingerprint_control_plane()
        self._journal("preflight-complete")

    def plan(self) -> dict[str, Any]:
        self.preflight(mutate=False)
        return {
            "schema_version": SCHEMA_VERSION,
            "assessment_id": self.assessment_id,
            "profile": self.profile.name,
            "profile_sha256": self.evidence.profile_sha256,
            "requested_sha": self.args.sha,
            "control_fingerprint": self.evidence.control_fingerprint,
            "python_versions": list(self.profile.python_versions),
            "pytest_groups": [asdict(group) for group in self.profile.pytest_groups],
            "worktree": str(self.worktree),
            "materials": str(self.materials),
            "results": str(self.results),
            "gate_decision": "NOT_EVALUATED",
        }

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
            lock = (
                self.control_root
                / f"requirements-local-agent-assessment-py{suffix}.lock"
            )
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
                    "sync",
                    "--python",
                    str(venv / "bin/python"),
                    "--require-hashes",
                    str(lock),
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
        commands = (
            ("ruff-check", [str(venv / "bin/ruff"), "check", "."]),
            (
                "ruff-format",
                [str(venv / "bin/ruff"), "format", "--check", "--diff", "."],
            ),
            ("pyrefly", [str(venv / "bin/pyrefly"), "check"]),
        )
        for name, argv in commands:
            self._run_recorded(name, argv, cwd=self.worktree, env=self.base_env)

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
        if self.args.expected_ref:
            if self.args.fetch:
                self._git("fetch", "origin", "--prune")
            end = self._git("rev-parse", self.args.expected_ref).stdout.strip()
            self.evidence.expected_ref_end = end
            if end != self.evidence.expected_ref_start:
                raise AssessmentError(
                    "STALE", f"expected ref moved during assessment: {end}"
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
            finally:
                try:
                    fcntl.flock(self.lock_handle, fcntl.LOCK_UN)
                    self.lock_handle.close()
                except Exception as exc:  # noqa: BLE001 - cleanup must remain best effort
                    failures.append(f"lock release raised {type(exc).__name__}: {exc}")
                self.lock_handle = None
        return failures

    def execute(self) -> int:
        status = "INFRA_ERROR"
        host_blob_root = Path.home() / ".local/share/firecrawl/blobs"
        before_host_blobs: dict[str, dict[str, Any]] = {}
        error_message: str | None = None
        try:
            before_host_blobs = inventory(host_blob_root)
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
            if changed_host_blobs:
                status = "ISOLATION_BREACH"
                self.evidence.anomalies.append(
                    f"host default blob store changed: {changed_host_blobs[:20]}"
                )
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

    lock_dir = workspace_root / ".locks"
    lock_dir.mkdir(exist_ok=True)
    lock_handle = (lock_dir / "host-assessment.lock").open("a+")
    try:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        lock_handle.close()
        raise AssessmentError(
            "BLOCKED", "cannot recover while another host assessment is active"
        ) from exc

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
            lock_handle.close()
        except Exception as exc:  # noqa: BLE001 - preserve typed recovery result
            failures.append(f"recovery lock release raised {type(exc).__name__}: {exc}")
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
