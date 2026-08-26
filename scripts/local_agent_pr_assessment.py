"""Reviewed PR-head bootstrap for the deterministic local assessment runner.

The unmerged PR revision owns new runner/profile semantics only after the host
operational guard has pinned their reviewed fingerprints.  This controller
keeps that reviewed bootstrap checkout separate from the authoritative
``origin/main`` source snapshot used to derive trusted regression membership.
"""

from __future__ import annotations

import fcntl
import tarfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any
import local_agent_assessment as base


BaseRunner = base.Runner


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
            control_blob = self._git("rev-parse", f"{control_ref}:{path}").stdout.strip()
            candidate_blob = self._git(
                "rev-parse", f"{self.args.sha}:{path}"
            ).stdout.strip()
            if control_blob != candidate_blob:
                raise base.AssessmentError(
                    "BLOCKED",
                    f"candidate cannot replace trusted regression implementation: {path}",
                )

        self.candidate_test_files = self._discover_candidate_test_files(control_ref)
        for path in (
            "pyproject.toml",
            "pyrefly-baseline.json",
            *base.PR_TEST_CONTROL_PATHS,
        ):
            control_blob = self._git("rev-parse", f"{control_ref}:{path}").stdout.strip()
            candidate_blob = self._git(
                "rev-parse", f"{self.args.sha}:{path}"
            ).stdout.strip()
            if control_blob != candidate_blob:
                policy = (
                    "trusted static-analysis policy"
                    if path in {"pyproject.toml", "pyrefly-baseline.json"}
                    else "trusted pytest control"
                )
                raise base.AssessmentError(
                    "BLOCKED", f"candidate cannot replace {policy}: {path}"
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
                self.evidence.candidate_test_manifest = base.build_candidate_test_manifest(
                    self.candidate_test_base_sha or "",
                    self.candidate_test_files,
                    (),
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


def main(argv=None) -> int:
    original_runner = base.Runner
    base.Runner = ReviewedPRRunner
    try:
        return base.main(argv)
    finally:
        base.Runner = original_runner
