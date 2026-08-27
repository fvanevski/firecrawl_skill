"""Reviewed PR-head bootstrap for the deterministic local assessment runner.

The unmerged PR revision owns new runner/profile semantics only after the host
operational guard has pinned their reviewed fingerprints. This controller keeps
that reviewed bootstrap checkout separate from the authoritative ``origin/main``
source snapshot used to derive trusted regression membership.
"""

from __future__ import annotations

import fcntl
import json
import subprocess
import sys
import tarfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import local_agent_assessment as base

BaseRunner = base.Runner
BasePytestEntryArgv = base.pytest_entry_argv
BaseDiscoverCandidateTestFiles = base.Runner._discover_candidate_test_files
BaseCollectPytestNodes = base.Runner._collect_pytest_nodes
BaseRunExactPytestNodes = base.Runner._run_exact_pytest_nodes


def _requested_sha(argv: Sequence[str] | None = None) -> str | None:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        index = args.index("--sha")
    except ValueError:
        return None
    if index + 1 >= len(args):
        return None
    value = args[index + 1]
    return value if base.SHA_RE.fullmatch(value) else None


def _source_checkout_requires_bootstrap(argv: Sequence[str] | None = None) -> bool:
    """Return whether this is a reviewed pre-merge self-assessment checkout."""
    requested_sha = _requested_sha(argv)
    if requested_sha is None:
        return False
    control_root = Path(base.__file__).resolve().parents[1]
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(control_root),
                "rev-parse",
                "HEAD",
                "origin/main",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=base.CONTROL_COMMAND_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    resolved = result.stdout.splitlines()
    if result.returncode != 0 or len(resolved) != 2:
        return False
    source_head, local_main = resolved
    if not base.SHA_RE.fullmatch(source_head) or not base.SHA_RE.fullmatch(local_main):
        return False
    return source_head == requested_sha and source_head != local_main


def _pr_pytest_entry_argv(
    python: Path, blocked_test_module_plugins: Sequence[str] = ()
) -> list[str]:
    """Use the trusted isolated launcher for every PR-mode pytest process."""
    blocked = tuple(sorted(set(blocked_test_module_plugins)))
    return [
        str(python),
        "-P",
        "-c",
        base.CANDIDATE_PYTEST_LAUNCHER,
        json.dumps(blocked, separators=(",", ":")),
    ]


def _pr_discover_candidate_test_files(
    self: BaseRunner, control_sha: str
) -> tuple[str, ...]:
    """Require every discovered candidate test module to be a regular Git blob."""
    files = BaseDiscoverCandidateTestFiles(self, control_sha)
    blobs: dict[str, str] = {}
    for path in files:
        raw = self._git("ls-tree", self.args.sha, "--", path).stdout.rstrip("\n")
        lines = raw.splitlines()
        if len(lines) != 1:
            raise base.AssessmentError(
                "BLOCKED", f"candidate test path did not resolve exactly in Git: {path}"
            )
        metadata, separator, listed_path = lines[0].partition("\t")
        parts = metadata.split()
        if not separator or len(parts) != 3:
            raise base.AssessmentError(
                "BLOCKED", f"candidate test Git entry is malformed: {path}"
            )
        mode, object_type, object_sha = parts
        if (
            mode not in {"100644", "100755"}
            or object_type != "blob"
            or not base.SHA_RE.fullmatch(object_sha)
            or listed_path != path
        ):
            raise base.AssessmentError(
                "BLOCKED",
                f"candidate test must be a regular Git file at the exact candidate SHA: {path}",
            )
        blobs[path] = object_sha
    self.candidate_test_blobs = blobs
    return files


def _validate_pr_candidate_test_worktree_paths(self: BaseRunner) -> None:
    """Prove candidate test paths are regular files contained by the worktree."""
    if not self.candidate_test_files:
        return
    try:
        root = self.worktree.resolve(strict=True)
    except OSError as exc:
        raise base.AssessmentError(
            "STALE", "candidate worktree is unavailable during pytest validation"
        ) from exc

    for relative in self.candidate_test_files:
        current = root
        for part in Path(relative).parts:
            current = current / part
            if current.is_symlink():
                raise base.AssessmentError(
                    "STALE", f"candidate test path became symlinked: {relative}"
                )
        candidate_path = root / relative
        try:
            resolved = candidate_path.resolve(strict=True)
        except OSError as exc:
            raise base.AssessmentError(
                "STALE", f"candidate test path became unavailable: {relative}"
            ) from exc
        if (
            resolved == root
            or not resolved.is_relative_to(root)
            or not resolved.is_file()
        ):
            raise base.AssessmentError(
                "STALE",
                f"candidate test path escaped or ceased to be a regular file: {relative}",
            )


def _prepare_pr_candidate_test_source_manifest(
    self: BaseRunner,
) -> tuple[Path, str] | None:
    """Freeze exact candidate Git test sources before any candidate pytest executes."""
    if not self.candidate_test_files:
        return None
    cached_path = getattr(self, "_candidate_test_source_manifest_path", None)
    cached_sha256 = getattr(self, "_candidate_test_source_manifest_sha256", None)
    if cached_path is not None and cached_sha256 is not None:
        return cached_path, cached_sha256

    blobs = getattr(self, "candidate_test_blobs", {})
    if set(blobs) != set(self.candidate_test_files):
        raise base.AssessmentError(
            "BLOCKED", "candidate test Git blob authority is incomplete"
        )
    entries = []
    for path in self.candidate_test_files:
        blob_sha = blobs[path]
        source = self._git("cat-file", "-p", blob_sha).stdout
        entries.append({"path": path, "blob_sha": blob_sha, "source": source})
    payload = {
        "schema_version": 1,
        "candidate_sha": self.args.sha,
        "entries": entries,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    manifest_path = self.materials / "candidate-test-sources.json"
    try:
        with manifest_path.open("xb") as handle:
            handle.write(raw)
        manifest_path.chmod(0o400)
    except OSError as exc:
        raise base.AssessmentError(
            "INFRA_ERROR", "candidate test source manifest could not be materialized"
        ) from exc
    manifest_sha256 = base.sha256_bytes(raw)
    self._candidate_test_source_manifest_path = manifest_path
    self._candidate_test_source_manifest_sha256 = manifest_sha256
    return manifest_path, manifest_sha256


def _pr_pytest_env(self: BaseRunner, env: Mapping[str, str]) -> Mapping[str, str]:
    authority = _prepare_pr_candidate_test_source_manifest(self)
    if authority is None:
        return env
    path, sha256 = authority
    result = dict(env)
    result[base.CANDIDATE_TEST_SOURCE_MANIFEST_ENV] = str(path)
    result[base.CANDIDATE_TEST_SOURCE_MANIFEST_SHA256_ENV] = sha256
    result[base.CANDIDATE_TEST_SOURCE_SHA_ENV] = self.args.sha
    return result


def _pr_collect_pytest_nodes(
    self: BaseRunner,
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
    if cwd == self.worktree:
        _validate_pr_candidate_test_worktree_paths(self)
    nodes = BaseCollectPytestNodes(
        self,
        name,
        python,
        selectors,
        cwd=cwd,
        env=_pr_pytest_env(self, env) if cwd == self.worktree else env,
        max_nodes=max_nodes,
        failure_status=failure_status,
        reject_filtered_collection=reject_filtered_collection,
        blocked_test_module_plugins=blocked_test_module_plugins,
    )
    if reject_filtered_collection:
        expected_files = sorted({selector.split("::", 1)[0] for selector in selectors})
        collected_files = {node.split("::", 1)[0] for node in nodes}
        missing_files = [path for path in expected_files if path not in collected_files]
        if missing_files:
            raise base.AssessmentError(
                failure_status,
                f"pytest collection omitted candidate test modules: {missing_files}",
            )
    return nodes


def _pr_run_exact_pytest_nodes(
    self: BaseRunner,
    name: str,
    python: Path,
    node_ids: Sequence[str],
    expected_tests: int,
    *,
    env: Mapping[str, str],
    blocked_test_module_plugins: Sequence[str] = (),
) -> None:
    _validate_pr_candidate_test_worktree_paths(self)
    BaseRunExactPytestNodes(
        self,
        name,
        python,
        node_ids,
        expected_tests,
        env=_pr_pytest_env(self, env),
        blocked_test_module_plugins=blocked_test_module_plugins,
    )


class ReviewedPRRunner(BaseRunner):
    """Run PR-head assessment from a reviewed, fingerprint-pinned PR checkout."""

    def __init__(self, args) -> None:
        super().__init__(args)
        if self.target_kind != "pr-head":
            raise base.AssessmentError(
                "BLOCKED", "reviewed PR bootstrap accepts only pr-head targets"
            )
        self.control_plane_source_sha: str | None = None
        self.control_snapshot = self.materials / "control-main"
        self.control_snapshot_inventory: dict[str, dict[str, Any]] | None = None

    def _fingerprint_control_plane(self) -> dict[str, str]:
        fingerprints = super()._fingerprint_control_plane()
        fingerprints["pr_bootstrap"] = base.sha256_file(Path(__file__).resolve())
        return fingerprints

    def preflight(self, *, mutate: bool = True) -> None:
        if not base.SHA_RE.fullmatch(self.args.sha):
            raise base.AssessmentError(
                "BLOCKED", "--sha must be a lowercase 40-character commit SHA"
            )
        if self.repo != self.control_root:
            raise base.AssessmentError(
                "BLOCKED",
                "pr-head bootstrap must execute from its reviewed source checkout",
            )
        if mutate:
            self.workspace_root.mkdir(parents=True, exist_ok=True)
            lock_dir = self.workspace_root / ".locks"
            lock_dir.mkdir(exist_ok=True)
            self.lock_handle = (lock_dir / "host-assessment.lock").open("a+")
            try:
                fcntl.flock(self.lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise base.AssessmentError(
                    "BLOCKED", "another host assessment owns the lifecycle lock"
                ) from exc
            if (
                self.materials.exists()
                or self.results.exists()
                or self.worktree.exists()
            ):
                raise base.AssessmentError(
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
            self.base_env = base.build_base_environment(self.materials, self.tools)

        remote = self._git("remote", "get-url", "origin").stdout.strip()
        if remote != self.profile.repository_remote:
            raise base.AssessmentError(
                "BLOCKED", "repository origin does not match profile"
            )
        if self.profile.requires_fresh_fetch and not self.args.fetch:
            raise base.AssessmentError(
                "BLOCKED", "reviewed PR profile requires --fetch freshness"
            )

        self._git("fetch", "origin", "--prune")
        source_head = self._git("rev-parse", "HEAD").stdout.strip()
        control_ref = self._git("rev-parse", "origin/main").stdout.strip()
        if not base.SHA_RE.fullmatch(source_head) or not base.SHA_RE.fullmatch(
            control_ref
        ):
            raise base.AssessmentError(
                "BLOCKED", "PR bootstrap/control identity did not resolve exactly"
            )
        self.control_plane_source_sha = source_head
        self.evidence.control_sha = control_ref
        self.evidence.control_ref_start = control_ref
        if source_head != self.args.sha:
            raise base.AssessmentError(
                "STALE",
                f"reviewed PR bootstrap checkout is {source_head}, not requested SHA",
            )
        if source_head == control_ref:
            raise base.AssessmentError(
                "BLOCKED",
                "reviewed PR bootstrap is unnecessary when source checkout is origin/main",
            )
        if self._git("status", "--porcelain=v1", "--untracked-files=all").stdout:
            raise base.AssessmentError(
                "BLOCKED", "reviewed PR bootstrap checkout is not clean"
            )

        start = self._fetch_pr_head()
        self.evidence.pr_head_start = start
        if start != self.args.sha:
            raise base.AssessmentError(
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
                "rev-parse", f"{control_ref}:{path}"
            ).stdout.strip()
            candidate_blob = self._git(
                "rev-parse", f"{self.args.sha}:{path}"
            ).stdout.strip()
            if control_blob != candidate_blob:
                raise base.AssessmentError(
                    "BLOCKED",
                    f"candidate cannot replace trusted regression implementation: {path}",
                )

        self.candidate_test_files = self._discover_candidate_test_files(control_ref)
        for path in ("pyproject.toml", "pyrefly-baseline.json"):
            control_blob = self._git(
                "rev-parse", f"{control_ref}:{path}"
            ).stdout.strip()
            candidate_blob = self._git(
                "rev-parse", f"{self.args.sha}:{path}"
            ).stdout.strip()
            if control_blob != candidate_blob:
                raise base.AssessmentError(
                    "BLOCKED",
                    f"candidate cannot replace trusted static-analysis policy: {path}",
                )
        required_pytest_control = set(base.PR_TEST_CONTROL_PATHS)
        protected_pytest_control = required_pytest_control | set(
            base.pr_pytest_conftest_paths(self.profile.pr_test_roots)
        )
        for path in sorted(protected_pytest_control):
            self._require_matching_optional_regular_path(
                control_ref,
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
        plan = super().plan()
        plan["control_plane_source_sha"] = self.control_plane_source_sha
        plan["control_snapshot_source_sha"] = self.evidence.control_sha
        return plan

    def _create_control_snapshot(self) -> None:
        control_sha = self.evidence.control_sha
        if control_sha is None or not base.SHA_RE.fullmatch(control_sha):
            raise base.AssessmentError(
                "BLOCKED", "trusted main SHA unavailable for control snapshot"
            )
        if self.control_snapshot.exists():
            raise base.AssessmentError(
                "BLOCKED", "trusted control snapshot path already exists"
            )
        self.control_snapshot.mkdir(parents=True)
        self.control_snapshot.chmod(0o700)
        archive = self.materials / "control-main.tar"
        self._git(
            "archive",
            "--format=tar",
            f"--output={archive}",
            control_sha,
        )
        try:
            snapshot_root = self.control_snapshot.resolve()
            with tarfile.open(archive, mode="r:") as bundle:
                for member in bundle.getmembers():
                    target = (self.control_snapshot / member.name).resolve()
                    if target != snapshot_root and not target.is_relative_to(
                        snapshot_root
                    ):
                        raise base.AssessmentError(
                            "BLOCKED", "trusted control archive escaped snapshot root"
                        )
                bundle.extractall(self.control_snapshot)
        finally:
            archive.unlink(missing_ok=True)
        self._journal("control-snapshot-created")

    def create_worktree(self) -> None:
        self._create_control_snapshot()
        super().create_worktree()

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
                    cwd=self.control_snapshot,
                    env=runtime_env,
                    max_nodes=group.expected_tests,
                    failure_status="BLOCKED",
                )
                if len(nodes) != group.expected_tests:
                    raise base.AssessmentError(
                        "BLOCKED",
                        f"trusted profile membership drifted for {group.name}: "
                        f"expected {group.expected_tests}, collected {len(nodes)}",
                    )
                trusted_memberships[(group.name, version)] = nodes

        self.control_snapshot_inventory = base.inventory(self.control_snapshot)

        candidate_nodes: tuple[str, ...] = ()
        if self.candidate_test_files:
            candidate_python = environments[self.profile.pr_test_python] / "bin/python"
            try:
                candidate_nodes = self._collect_pytest_nodes(
                    "candidate-regressions",
                    candidate_python,
                    self.candidate_test_files,
                    cwd=self.worktree,
                    env=runtime_env,
                    max_nodes=self.profile.pr_test_max_nodes,
                    failure_status="FAIL",
                    reject_filtered_collection=True,
                    blocked_test_module_plugins=self.candidate_test_files,
                )
            except base.AssessmentError:
                self.evidence.candidate_test_manifest = (
                    base.build_candidate_test_manifest(
                        self.candidate_test_base_sha or "",
                        self.candidate_test_files,
                        (),
                    )
                )
                raise
        self.evidence.candidate_test_manifest = base.build_candidate_test_manifest(
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
            suffix = self.profile.pr_test_python.replace(".", "")
            self._run_exact_pytest_nodes(
                f"pytest-candidate-regressions-py{suffix}",
                environments[self.profile.pr_test_python] / "bin/python",
                candidate_nodes,
                len(candidate_nodes),
                env=runtime_env,
                blocked_test_module_plugins=self.candidate_test_files,
            )

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
            raise base.AssessmentError("STALE", "final exact-SHA worktree proof failed")

        self._git("fetch", "origin", "--prune")
        control_end = self._git("rev-parse", "origin/main").stdout.strip()
        source_head_end = self._git("rev-parse", "HEAD").stdout.strip()
        source_status_end = self._git(
            "status", "--porcelain=v1", "--untracked-files=all"
        ).stdout
        self.evidence.control_ref_end = control_end
        if (
            control_end != self.evidence.control_ref_start
            or control_end != self.evidence.control_sha
        ):
            raise base.AssessmentError(
                "STALE", f"trusted control ref moved during assessment: {control_end}"
            )
        if (
            source_head_end != self.control_plane_source_sha
            or source_head_end != self.args.sha
        ):
            raise base.AssessmentError(
                "STALE",
                f"reviewed PR bootstrap checkout moved during assessment: {source_head_end}",
            )
        if source_status_end:
            raise base.AssessmentError(
                "STALE", "reviewed PR bootstrap checkout became dirty during assessment"
            )
        if self.control_snapshot_inventory is None:
            raise base.AssessmentError(
                "INFRA_ERROR", "trusted control snapshot inventory is unavailable"
            )
        if base.inventory(self.control_snapshot) != self.control_snapshot_inventory:
            raise base.AssessmentError(
                "STALE", "trusted control snapshot changed after membership collection"
            )

        pr_end = self._fetch_pr_head()
        self.evidence.pr_head_end = pr_end
        if pr_end != self.evidence.pr_head_start or pr_end != self.args.sha:
            raise base.AssessmentError(
                "STALE", f"canonical PR #{self.pr_number} head moved: {pr_end}"
            )


def main(argv: Sequence[str] | None = None) -> int:
    original_runner = base.Runner
    original_pytest_entry_argv = base.pytest_entry_argv
    original_discover = BaseRunner._discover_candidate_test_files
    original_collect = BaseRunner._collect_pytest_nodes
    original_run_exact = BaseRunner._run_exact_pytest_nodes
    base.pytest_entry_argv = _pr_pytest_entry_argv
    BaseRunner._discover_candidate_test_files = _pr_discover_candidate_test_files
    BaseRunner._collect_pytest_nodes = _pr_collect_pytest_nodes
    BaseRunner._run_exact_pytest_nodes = _pr_run_exact_pytest_nodes
    if _source_checkout_requires_bootstrap(argv):
        base.Runner = ReviewedPRRunner
    try:
        return base.main(argv)
    finally:
        base.Runner = original_runner
        base.pytest_entry_argv = original_pytest_entry_argv
        BaseRunner._discover_candidate_test_files = original_discover
        BaseRunner._collect_pytest_nodes = original_collect
        BaseRunner._run_exact_pytest_nodes = original_run_exact
