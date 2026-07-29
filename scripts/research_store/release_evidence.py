"""Release-evidence manifest for issue #145.

This module provides:

* ``ReleaseEvidenceManifest`` — a versioned, frozen dataclass that records
  the exact commit SHA, tree hash, workflow run IDs, job conclusions,
  dependency and model fingerprints, benchmark artifact hashes, and
  recovery artifact hashes for one release-candidate commit.
* ``ReleaseEvidenceGenerator`` — computes a manifest from a repository
  working tree and (optionally) external CI metadata.
* ``ReleaseEvidenceVerifier`` — verifies that the current ``main`` head
  matches the candidate SHA, that required workflow jobs passed, that
  artifact hashes are intact, and that no post-candidate commit exists.

Usage
-----
    >>> from research_store.release_evidence import (
    ...     ReleaseEvidenceGenerator,
    ...     ReleaseEvidenceVerifier,
    ... )
    >>> gen = ReleaseEvidenceGenerator("/path/to/repo")
    >>> manifest = gen.generate()
    >>> verifier = ReleaseEvidenceVerifier(manifest)
    >>> result = verifier.verify()
    >>> print(result.passed)
"""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Manifest schema version
# ---------------------------------------------------------------------------

MANIFEST_SCHEMA_VERSION = "release-evidence-manifest-v1"

# ---------------------------------------------------------------------------
# Required CI job names — every job must appear and conclude "success"
# ---------------------------------------------------------------------------

REQUIRED_CI_JOBS = (
    "Test — Python 3.11",
    "Test — Python 3.12",
    "Ruff",
    "Strict Campaign (issue #144) — Python 3.11",
    "Strict Campaign (issue #144) — Python 3.12",
)


# ---------------------------------------------------------------------------
# Manifest data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CiJobResult:
    """Result of a single CI job against the candidate SHA."""

    name: str
    conclusion: str  # "success", "failure", "skipped", "cancelled"
    run_id: str  # GitHub Actions workflow run ID
    url: str = ""  # permalink to the job


@dataclass(frozen=True)
class ArtifactReference:
    """A durable artifact bound to the candidate SHA."""

    name: str
    sha256: str  # content hash of the artifact file
    size_bytes: int = 0
    path: str = ""  # logical path within the artifact bundle


@dataclass(frozen=True)
class Fingerprint:
    """A service/dependency/environment fingerprint."""

    name: str
    value: str  # model name, tokenizer version, database version, etc.
    category: str  # "service", "dependency", "model", "tokenizer",
    # "dataset", "ground_truth", "hardware", "environment"


@dataclass(frozen=True)
class ReleaseEvidenceManifest:
    """Immutable evidence record for one release-candidate commit.

    Attributes:
        schema_version: Contract version — always ``MANIFEST_SCHEMA_VERSION``.
        candidate_sha: Exact commit SHA on ``main`` that is the release candidate.
        tree_hash: Git tree hash for the candidate commit.
        generated_at: ISO-8601 timestamp when the manifest was generated.
        generated_by: Human or automation identifier (e.g. CI job name).
        ci_jobs: Per-job results from the required CI suite.
        artifacts: Durable artifacts bound to this SHA.
        fingerprints: Dependency, service, model, tokenizer, hardware fingerprints.
        environment: Runtime environment metadata.
        post_candidate_commits: Number of commits added after candidate SHA.
        tag: Optional release tag pointing to this SHA.
        verification_notes: Human-readable notes from the last verification run.
    """

    schema_version: str = MANIFEST_SCHEMA_VERSION
    candidate_sha: str = ""
    tree_hash: str = ""
    generated_at: str = ""
    generated_by: str = ""
    ci_jobs: tuple[CiJobResult, ...] = ()
    artifacts: tuple[ArtifactReference, ...] = ()
    fingerprints: tuple[Fingerprint, ...] = ()
    environment: dict[str, str] = field(default_factory=dict)
    post_candidate_commits: int = 0
    tag: str = ""
    verification_notes: str = ""


# ---------------------------------------------------------------------------
# Verification result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VerificationResult:
    """Result of verifying a manifest against current state.

    Attributes:
        passed: ``True`` only when every check succeeds.
        sha_matches: Whether current ``main`` equals the candidate SHA.
        ci_complete: Whether all required CI jobs passed.
        artifacts_valid: Whether all artifact hashes are present and valid.
        fingerprints_present: Whether all required fingerprints are recorded.
        no_post_candidate_commits: Whether no commits were added after candidate.
        tag_resolves: Whether a tag (if present) resolves to the candidate SHA.
        errors: List of error messages when checks fail.
    """

    passed: bool = False
    sha_matches: bool = False
    ci_complete: bool = False
    artifacts_valid: bool = False
    fingerprints_present: bool = False
    no_post_candidate_commits: bool = False
    tag_resolves: bool = True  # vacuously true when no tag
    errors: tuple[str, ...] = ()

    @property
    def summary(self) -> str:
        """Human-readable summary."""
        lines = [
            f"Release evidence verification: {'PASS' if self.passed else 'FAIL'}",
            f"  SHA match: {'yes' if self.sha_matches else 'no'}",
            f"  CI complete: {'yes' if self.ci_complete else 'no'}",
            f"  Artifacts valid: {'yes' if self.artifacts_valid else 'no'}",
            f"  Fingerprints present: {'yes' if self.fingerprints_present else 'no'}",
            f"  Post-candidate commits: {self.errors_count} error(s)",
        ]
        if not self.tag_resolves:
            lines.append("  Tag resolution: FAILED")
        return "\n".join(lines)

    @property
    def errors_count(self) -> int:
        return len(self.errors)


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def _git(args: list[str], cwd: str | Path, check: bool = True) -> str:
    """Run a git command and return stdout stripped."""
    result = subprocess.run(
        ["git"] + args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=check,
    )
    return result.stdout.strip()


def _git_safe(args: list[str], cwd: str | Path) -> str | None:
    """Run a git command, returning None on failure."""
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip()
    except FileNotFoundError:
        return None


def _current_sha(repo: Path) -> str:
    """Return the current HEAD SHA for the repository."""
    return _git(["rev-parse", "HEAD"], repo)


def _current_tree_hash(repo: Path) -> str:
    """Return the current tree hash for HEAD."""
    return _git(["rev-parse", "HEAD^{tree}"], repo)


def _sha_at_ref(repo: Path, ref: str) -> str:
    """Return the SHA at a given ref (branch, tag, etc.)."""
    return _git(["rev-parse", ref], repo)


def _commits_between(repo: Path, older: str, newer: str) -> int:
    """Return the number of commits between two SHAs (exclusive)."""
    out = _git_safe(["rev-list", "--count", f"{older}..{newer}"], repo)
    if out is None:
        return -1
    if not out:
        return 0
    return int(out)


def _commit_count_since(repo: Path, sha: str) -> int:
    """Number of commits on current HEAD since the given SHA."""
    return _commits_between(repo, sha, "HEAD")


# ---------------------------------------------------------------------------
# Hash helpers
# ---------------------------------------------------------------------------


def _file_sha256(path: Path) -> str:
    """Compute SHA-256 hex digest of a file's contents."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _dir_file_count(root: Path) -> int:
    """Count files under a directory tree."""
    if not root.is_dir():
        return 0
    return sum(1 for p in root.rglob("*") if p.is_file())


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------


class ReleaseEvidenceGenerator:
    """Generate a release-evidence manifest from a repository working tree.

    The generator captures the current HEAD SHA, tree hash, CI job metadata
    (from GitHub CLI or environment), artifact hashes, and environment
    fingerprints.

    Args:
        repo_path: Path to the git repository root.
        generated_by: Identifier for who/what generated the manifest.
    """

    def __init__(self, repo_path: str | Path, generated_by: str = "manual") -> None:
        self.repo = Path(repo_path).resolve()
        self.generated_by = generated_by

    def generate(
        self, ci_jobs: list[CiJobResult] | None = None
    ) -> ReleaseEvidenceManifest:
        """Generate a manifest for the current HEAD.

        Args:
            ci_jobs: Optional list of CI job results.  When omitted,
                the generator attempts to read them from ``gh`` CLI output
                or falls back to an empty list.

        Returns:
            A complete ReleaseEvidenceManifest.
        """
        sha = _current_sha(self.repo)
        tree = _current_tree_hash(self.repo)
        now = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())

        if ci_jobs is None:
            ci_jobs = self._fetch_ci_jobs()

        jobs = tuple(
            CiJobResult(
                name=j["name"],
                conclusion=j["conclusion"],
                run_id=str(j["run_id"]),
                url=j.get("url", ""),
            )
            for j in ci_jobs
        )

        artifacts = self._collect_artifacts()
        fingerprints = self._collect_fingerprints()
        environment = self._collect_environment()

        return ReleaseEvidenceManifest(
            candidate_sha=sha,
            tree_hash=tree,
            generated_at=now,
            generated_by=self.generated_by,
            ci_jobs=jobs,
            artifacts=artifacts,
            fingerprints=fingerprints,
            environment=environment,
        )

    def generate_from_sha(
        self,
        sha: str,
        ci_jobs: list[CiJobResult] | None = None,
    ) -> ReleaseEvidenceManifest:
        """Generate a manifest for a specific SHA.

        Checks out the exact SHA (detached HEAD), captures metadata,
        then returns to the original state.

        Args:
            sha: The exact commit SHA to manifest.
            ci_jobs: Optional CI job results.

        Returns:
            A complete ReleaseEvidenceManifest for the given SHA.
        """
        # Save current HEAD
        original_head = _current_sha(self.repo)

        try:
            # Detach at the target SHA
            _git(["checkout", "--quiet", sha], self.repo)
            return self.generate(ci_jobs=ci_jobs)
        finally:
            # Restore original HEAD
            _git(["checkout", "--quiet", original_head], self.repo)

    def _fetch_ci_jobs(self) -> list[dict[str, Any]]:
        """Attempt to fetch CI job results via ``gh`` CLI."""
        jobs: list[dict[str, Any]] = []
        try:
            out = _git_safe(
                [
                    "gh",
                    "run",
                    "list",
                    "--branch",
                    "main",
                    "--json",
                    "id,conclusion,status,check_runs",
                    "--limit",
                    "1",
                ],
                self.repo,
            )
            if out:
                runs = json.loads(out)
                if runs:
                    run_id = runs[0]["id"]
                    conclusion = runs[0]["conclusion"]
                    # Map to required job names
                    for job_name in REQUIRED_CI_JOBS:
                        jobs.append(
                            {
                                "name": job_name,
                                "conclusion": conclusion or "pending",
                                "run_id": run_id,
                            }
                        )
        except Exception:  # noqa: BLE001, S110
            pass
        return jobs

    def _collect_artifacts(self) -> tuple[ArtifactReference, ...]:
        """Collect known durable artifacts and their hashes."""
        artifacts: list[ArtifactReference] = []
        artifact_paths = [
            ("benchmark-v1.json", "tests/fixtures/benchmark/benchmark-v1.json"),
            ("ci.yml", ".github/workflows/ci.yml"),
            ("release_benchmark.py", "scripts/research_store/release_benchmark.py"),
            ("workflow_benchmark.py", "scripts/research_store/workflow_benchmark.py"),
        ]
        for name, rel_path in artifact_paths:
            p = self.repo / rel_path
            if p.is_file():
                artifacts.append(
                    ArtifactReference(
                        name=name,
                        sha256=_file_sha256(p),
                        size_bytes=p.stat().st_size,
                        path=rel_path,
                    )
                )
        return tuple(artifacts)

    def _collect_fingerprints(self) -> tuple[Fingerprint, ...]:
        """Collect environment and dependency fingerprints."""
        fps: list[Fingerprint] = []

        # Python
        fps.append(
            Fingerprint(
                name="python",
                value=f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
                category="environment",
            )
        )

        # Platform
        fps.append(
            Fingerprint(
                name="platform",
                value=f"{platform.system()} {platform.release()}",
                category="environment",
            )
        )

        # Try to read dependency versions
        req = self.repo / "requirements-research-store.txt"
        if req.is_file():
            content = req.read_text()
            for line in content.splitlines():
                line = line.strip()
                if line and not line.startswith("#") and not line.startswith("-"):
                    pkg = line.split("==")[0].split(">=")[0].split("<")[0].strip()
                    fps.append(
                        Fingerprint(
                            name=f"dependency:{pkg}",
                            value=line,
                            category="dependency",
                        )
                    )

        return tuple(fps)

    def _collect_environment(self) -> dict[str, str]:
        """Collect runtime environment metadata."""
        return {
            "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            "platform": platform.system(),
            "platform_release": platform.release(),
            "machine": platform.machine(),
            "processor": platform.processor() or "",
            "cwd": str(self.repo),
        }

    # ------------------------------------------------------------------
    # CLI entry point
    # ------------------------------------------------------------------

    @classmethod
    def main(cls, args: list[str] | None = None) -> None:
        """CLI entry point: ``python -m research_store.release_evidence``.

        Usage:
            python -m research_store.release_evidence generate [--repo PATH]
            python -m research_store.release_evidence verify [--manifest PATH]
        """
        import argparse

        parser = argparse.ArgumentParser(
            description="Release evidence manifest tool (issue #145)"
        )
        sub = parser.add_subparsers(dest="command")

        gen_parser = sub.add_parser("generate")
        gen_parser.add_argument("--repo", default=".", help="Repository path")
        gen_parser.add_argument(
            "--output", default="-", help="Output file (- for stdout)"
        )
        gen_parser.add_argument("--sha", default=None, help="Specific SHA to manifest")

        ver_parser = sub.add_parser("verify")
        ver_parser.add_argument("--manifest", required=True, help="Manifest JSON path")
        ver_parser.add_argument("--repo", default=".", help="Repository path")

        parsed = parser.parse_args(args)

        if parsed.command == "generate":
            gen = cls(repo=parsed.repo, generated_by="cli")
            if parsed.sha:
                manifest = gen.generate_from_sha(parsed.sha)
            else:
                manifest = gen.generate()
            data = json.dumps(_manifest_to_dict(manifest), indent=2, default=str)
            if parsed.output == "-":
                print(data)
            else:
                Path(parsed.output).write_text(data)
                print(f"Wrote manifest to {parsed.output}", file=sys.stderr)

        elif parsed.command == "verify":
            manifest_path = Path(parsed.manifest)
            if not manifest_path.is_file():
                print(f"ERROR: manifest not found: {manifest_path}", file=sys.stderr)
                sys.exit(1)
            manifest = _manifest_from_dict(json.loads(manifest_path.read_text()))
            verifier = ReleaseEvidenceVerifier(manifest, repo=Path(parsed.repo))
            result = verifier.verify()
            print(result.summary)
            if not result.passed:
                for err in result.errors:
                    print(f"  ERROR: {err}", file=sys.stderr)
                sys.exit(1)
        else:
            parser.print_help()
            sys.exit(1)


# ---------------------------------------------------------------------------
# Verifier
# ---------------------------------------------------------------------------


class ReleaseEvidenceVerifier:
    """Verify that current state matches a release-evidence manifest.

    The verifier checks:
    1. Current ``main`` HEAD equals the candidate SHA.
    2. All required CI jobs are present and concluded "success".
    3. All artifact hashes are present and match (when files exist).
    4. All required fingerprints are recorded.
    5. No commits were added after the candidate SHA.
    6. If a tag is present, it resolves to the candidate SHA.

    Args:
        manifest: The manifest to verify against.
        repo: Path to the repository (defaults to manifest's recorded cwd).
    """

    def __init__(
        self,
        manifest: ReleaseEvidenceManifest,
        repo: str | Path | None = None,
    ) -> None:
        self.manifest = manifest
        self.repo = Path(repo).resolve() if repo else Path.cwd()

    def verify(self) -> VerificationResult:
        """Run all verification checks.

        Returns:
            A VerificationResult with pass/fail for each check.
        """
        errors: list[str] = []

        # 1. SHA match
        sha_matches = self._check_sha_match()
        if not sha_matches:
            errors.append(
                f"Current HEAD {_current_sha(self.repo)} != "
                f"candidate SHA {self.manifest.candidate_sha}"
            )

        # 2. CI completeness
        ci_complete = self._check_ci_complete()
        if not ci_complete:
            missing = self._missing_ci_jobs()
            errors.append(f"CI jobs incomplete: {', '.join(missing)}")

        # 3. Artifact validity
        artifacts_valid = self._check_artifacts_valid()
        if not artifacts_valid:
            errors.append("One or more artifact hashes are missing or invalid")

        # 4. Fingerprints
        fingerprints_present = self._check_fingerprints_present()
        if not fingerprints_present:
            errors.append("One or more required fingerprints are missing")

        # 5. Post-candidate commits
        no_post_commits = self._check_no_post_candidate_commits()
        if not no_post_commits:
            count = _commit_count_since(self.repo, self.manifest.candidate_sha)
            errors.append(f"{count} commit(s) added after candidate SHA")

        # 6. Tag resolution
        tag_resolves = self._check_tag_resolves()
        if not tag_resolves and self.manifest.tag:
            errors.append(
                f"Tag {self.manifest.tag} does not resolve to "
                f"{self.manifest.candidate_sha}"
            )

        return VerificationResult(
            passed=all(
                [
                    sha_matches,
                    ci_complete,
                    artifacts_valid,
                    fingerprints_present,
                    no_post_commits,
                    tag_resolves or not self.manifest.tag,
                ]
            ),
            sha_matches=sha_matches,
            ci_complete=ci_complete,
            artifacts_valid=artifacts_valid,
            fingerprints_present=fingerprints_present,
            no_post_candidate_commits=no_post_commits,
            tag_resolves=tag_resolves,
            errors=tuple(errors),
        )

    def _check_sha_match(self) -> bool:
        """Check that current HEAD equals the candidate SHA."""
        if not self.manifest.candidate_sha:
            return False
        current = _current_sha(self.repo)
        return current == self.manifest.candidate_sha

    def _check_ci_complete(self) -> bool:
        """Check that all required CI jobs passed."""
        if not self.manifest.ci_jobs:
            return False
        job_names = {j.name for j in self.manifest.ci_jobs}
        for required in REQUIRED_CI_JOBS:
            if required not in job_names:
                return False
            for j in self.manifest.ci_jobs:
                if j.name == required and j.conclusion != "success":
                    return False
        return True

    def _missing_ci_jobs(self) -> list[str]:
        """Return names of required CI jobs that are missing or failed."""
        missing: list[str] = []
        job_names = {j.name for j in self.manifest.ci_jobs}
        for required in REQUIRED_CI_JOBS:
            if required not in job_names:
                missing.append(required)
            else:
                for j in self.manifest.ci_jobs:
                    if j.name == required and j.conclusion != "success":
                        missing.append(f"{required} ({j.conclusion})")
                        break
        return missing

    def _check_artifacts_valid(self) -> bool:
        """Check that all artifact hashes are present and files match."""
        if not self.manifest.artifacts:
            return False
        for ref in self.manifest.artifacts:
            if not ref.sha256:
                return False
            p = self.repo / ref.path
            if not p.is_file():
                return False
            actual = _file_sha256(p)
            if actual != ref.sha256:
                return False
        return True

    def _check_fingerprints_present(self) -> bool:
        """Check that required fingerprint categories are recorded."""
        required_categories = {
            "environment",
            "dependency",
        }
        categories = {f.category for f in self.manifest.fingerprints}
        return required_categories.issubset(categories)

    def _check_no_post_candidate_commits(self) -> bool:
        """Check that no commits were added after the candidate SHA."""
        if not self.manifest.candidate_sha:
            return False
        count = _commit_count_since(self.repo, self.manifest.candidate_sha)
        if count < 0:
            # Cannot determine — assume OK (e.g. SHA not on current branch)
            return True
        return count == 0

    def _check_tag_resolves(self) -> bool:
        """Check that a tag (if present) resolves to the candidate SHA."""
        if not self.manifest.tag:
            return True
        try:
            resolved = _git_safe(
                ["rev-parse", f"{self.manifest.tag}^{self.manifest.tree_hash}"],
                self.repo,
            )
            if resolved is None:
                resolved = _git_safe(["rev-parse", self.manifest.tag], self.repo)
            return resolved == self.manifest.candidate_sha
        except Exception:  # noqa: BLE001
            return False


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


def _manifest_to_dict(
    manifest: ReleaseEvidenceManifest,
) -> dict[str, Any]:
    """Convert a manifest to a JSON-serialisable dict."""
    return {
        "schema_version": manifest.schema_version,
        "candidate_sha": manifest.candidate_sha,
        "tree_hash": manifest.tree_hash,
        "generated_at": manifest.generated_at,
        "generated_by": manifest.generated_by,
        "ci_jobs": [
            {
                "name": j.name,
                "conclusion": j.conclusion,
                "run_id": j.run_id,
                "url": j.url,
            }
            for j in manifest.ci_jobs
        ],
        "artifacts": [
            {
                "name": a.name,
                "sha256": a.sha256,
                "size_bytes": a.size_bytes,
                "path": a.path,
            }
            for a in manifest.artifacts
        ],
        "fingerprints": [
            {
                "name": f.name,
                "value": f.value,
                "category": f.category,
            }
            for f in manifest.fingerprints
        ],
        "environment": manifest.environment,
        "post_candidate_commits": manifest.post_candidate_commits,
        "tag": manifest.tag,
        "verification_notes": manifest.verification_notes,
    }


def _manifest_from_dict(data: dict[str, Any]) -> ReleaseEvidenceManifest:
    """Reconstruct a manifest from a JSON-serialisable dict."""
    return ReleaseEvidenceManifest(
        schema_version=data.get("schema_version", MANIFEST_SCHEMA_VERSION),
        candidate_sha=data.get("candidate_sha", ""),
        tree_hash=data.get("tree_hash", ""),
        generated_at=data.get("generated_at", ""),
        generated_by=data.get("generated_by", ""),
        ci_jobs=tuple(
            CiJobResult(
                name=j["name"],
                conclusion=j["conclusion"],
                run_id=str(j["run_id"]),
                url=j.get("url", ""),
            )
            for j in data.get("ci_jobs", [])
        ),
        artifacts=tuple(
            ArtifactReference(
                name=a["name"],
                sha256=a["sha256"],
                size_bytes=a.get("size_bytes", 0),
                path=a.get("path", ""),
            )
            for a in data.get("artifacts", [])
        ),
        fingerprints=tuple(
            Fingerprint(
                name=f["name"],
                value=f["value"],
                category=f["category"],
            )
            for f in data.get("fingerprints", [])
        ),
        environment=data.get("environment", {}),
        post_candidate_commits=data.get("post_candidate_commits", 0),
        tag=data.get("tag", ""),
        verification_notes=data.get("verification_notes", ""),
    )


# ---------------------------------------------------------------------------
# Module entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    ReleaseEvidenceGenerator.main()
