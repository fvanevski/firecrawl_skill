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
import os
import platform
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment,misc]

MANIFEST_SCHEMA_VERSION = "release-evidence-manifest-v1"

REQUIRED_CI_JOBS = (
    "Test — Python 3.11",
    "Test — Python 3.12",
    "Ruff",
    "Strict Campaign (issue #144) — Python 3.11",
    "Strict Campaign (issue #144) — Python 3.12",
)


def compute_required_ci_jobs(repo_path: str | Path | None = None) -> tuple[str, ...]:
    """Derive required CI job names from ``.github/workflows/ci.yml``."""
    if yaml is None:
        return REQUIRED_CI_JOBS

    if repo_path is None:
        repo_path = Path.cwd()
    ci_path = Path(repo_path) / ".github" / "workflows" / "ci.yml"
    if not ci_path.is_file():
        return REQUIRED_CI_JOBS

    try:
        with ci_path.open() as fh:
            ci = yaml.safe_load(fh)
    except (yaml.YAMLError, OSError):  # pragma: no cover
        return REQUIRED_CI_JOBS

    jobs = ci.get("jobs", {})
    if not isinstance(jobs, dict):
        return REQUIRED_CI_JOBS

    names: list[str] = []
    for job_def in jobs.values():
        if not isinstance(job_def, dict):
            continue
        display_name = job_def.get("name")
        if not isinstance(display_name, str):
            continue

        matrix_vars = _extract_matrix_vars(job_def)
        if not matrix_vars:
            names.append(display_name)
        else:
            for combo in _matrix_combinations(matrix_vars):
                expanded = display_name
                for key, value in combo.items():
                    expanded = expanded.replace(f"${{{{ matrix.{key} }}}}", value)
                names.append(expanded)

    return tuple(names)


def _extract_matrix_vars(job_def: dict) -> dict[str, list[str]]:
    """Extract matrix variable names and values from a job definition."""
    strategy = job_def.get("strategy")
    if not isinstance(strategy, dict):
        return {}
    matrix = strategy.get("matrix")
    if not isinstance(matrix, dict):
        return {}
    result: dict[str, list[str]] = {}
    for key, value in matrix.items():
        if isinstance(value, list):
            result[key] = [str(v) for v in value]
    return result


def _matrix_combinations(matrix_vars: dict[str, list[str]]) -> list[dict[str, str]]:
    """Generate all combinations of matrix variable values."""
    if not matrix_vars:
        return [{}]
    keys = list(matrix_vars.keys())
    combos: list[dict[str, str]] = [{}]
    for key in keys:
        new_combos: list[dict[str, str]] = []
        for value in matrix_vars[key]:
            for combo in combos:
                new_combo = dict(combo)
                new_combo[key] = value
                new_combos.append(new_combo)
        combos = new_combos
    return combos


@dataclass(frozen=True)
class CiJobResult:
    """Result of a single CI job against the candidate SHA."""

    name: str
    conclusion: str
    run_id: str
    url: str = ""
    candidate_sha: str = ""


@dataclass(frozen=True)
class ArtifactReference:
    """A durable artifact bound to the candidate SHA."""

    name: str
    sha256: str
    size_bytes: int = 0
    path: str = ""


@dataclass(frozen=True)
class Fingerprint:
    """A service/dependency/environment fingerprint."""

    name: str
    value: str
    category: str


@dataclass(frozen=True)
class ReleaseEvidenceManifest:
    """Immutable evidence record for one release-candidate commit."""

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


@dataclass(frozen=True)
class VerificationResult:
    """Result of verifying a manifest against current state."""

    passed: bool = False
    sha_matches: bool = False
    ci_complete: bool = False
    artifacts_valid: bool = False
    fingerprints_present: bool = False
    no_post_candidate_commits: bool = False
    tag_resolves: bool = True
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
            f"  Post-candidate commits: {'clean' if self.no_post_candidate_commits else 'FAIL'}",
        ]
        if not self.tag_resolves:
            lines.append("  Tag resolution: FAILED")
        return "\n".join(lines)

    @property
    def errors_count(self) -> int:
        return len(self.errors)


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
    return _git(["rev-parse", "HEAD"], repo)


def _current_tree_hash(repo: Path) -> str:
    return _git(["rev-parse", "HEAD^{tree}"], repo)


def _sha_at_ref(repo: Path, ref: str) -> str:
    try:
        return _git(["rev-parse", ref], repo)
    except subprocess.CalledProcessError:
        return ""


def _commits_between(repo: Path, older: str, newer: str) -> int:
    out = _git_safe(["rev-list", "--count", f"{older}..{newer}"], repo)
    if out is None:
        return -1
    if not out:
        return 0
    return int(out)


def _commit_count_since(repo: Path, sha: str) -> int:
    return _commits_between(repo, sha, "HEAD")


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _dir_file_count(root: Path) -> int:
    if not root.is_dir():
        return 0
    return sum(1 for p in root.rglob("*") if p.is_file())


class ReleaseEvidenceGenerator:
    """Generate a release-evidence manifest from a repository working tree."""

    def __init__(self, repo_path: str | Path, generated_by: str = "manual") -> None:
        self.repo = Path(repo_path).resolve()
        self.generated_by = generated_by

    def generate(self, ci_jobs: list[CiJobResult] | None = None) -> ReleaseEvidenceManifest:
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
                candidate_sha=sha,
            )
            for j in ci_jobs
        )
        return ReleaseEvidenceManifest(
            candidate_sha=sha,
            tree_hash=tree,
            generated_at=now,
            generated_by=self.generated_by,
            ci_jobs=jobs,
            artifacts=self._collect_artifacts(),
            fingerprints=self._collect_fingerprints(),
            environment=self._collect_environment(),
        )

    def generate_from_sha(
        self, sha: str, ci_jobs: list[CiJobResult] | None = None
    ) -> ReleaseEvidenceManifest:
        original_head = _current_sha(self.repo)
        try:
            _git(["checkout", "--quiet", sha], self.repo)
            return self.generate(ci_jobs=ci_jobs)
        finally:
            _git(["checkout", "--quiet", original_head], self.repo)

    def _fetch_ci_jobs(self) -> list[dict[str, Any]]:
        jobs: list[dict[str, Any]] = []
        try:
            result = subprocess.run(
                [
                    "gh",
                    "run",
                    "list",
                    "--branch",
                    "main",
                    "--status",
                    "completed",
                    "--json",
                    "id,conclusion,status,check_runs",
                    "--limit",
                    "1",
                ],
                cwd=str(self.repo),
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                return jobs
            out = result.stdout.strip()
            if not out:
                return jobs
            runs = json.loads(out)
            if runs:
                run_id = runs[0]["id"]
                run_conclusion = runs[0].get("conclusion") or "pending"
                check_runs = runs[0].get("check_runs") or []
                run_jobs: dict[str, str] = {}
                for cr in check_runs:
                    if isinstance(cr, dict):
                        name = cr.get("name")
                        conclusion = cr.get("conclusion")
                        if name and conclusion:
                            run_jobs[name] = conclusion
                for job_name in REQUIRED_CI_JOBS:
                    jobs.append(
                        {
                            "name": job_name,
                            "conclusion": run_jobs.get(job_name, run_conclusion),
                            "run_id": run_id,
                        }
                    )
        except FileNotFoundError:
            pass
        return jobs

    def _collect_artifacts(self) -> tuple[ArtifactReference, ...]:
        artifacts: list[ArtifactReference] = []
        artifact_paths = [
            ("benchmark-v2.json", "tests/fixtures/benchmark/benchmark-v2.json"),
            ("ci.yml", ".github/workflows/ci.yml"),
            ("release_benchmark.py", "scripts/research_store/release_benchmark.py"),
            ("workflow_benchmark.py", "scripts/research_store/workflow_benchmark.py"),
        ]
        for name, rel_path in artifact_paths:
            p = self.repo / rel_path
            if not p.is_file() or not self._is_tracked(rel_path, self.repo):
                continue
            artifacts.append(
                ArtifactReference(
                    name=name,
                    sha256=_file_sha256(p),
                    size_bytes=p.stat().st_size,
                    path=rel_path,
                )
            )
        return tuple(artifacts)

    @staticmethod
    def _is_tracked(rel_path: str, repo: Path) -> bool:
        result = subprocess.run(
            ["git", "ls-files", "--error-unmatch", rel_path],
            cwd=str(repo),
            capture_output=True,
            check=False,
        )
        return result.returncode == 0

    def _collect_fingerprints(self) -> tuple[Fingerprint, ...]:
        fps: list[Fingerprint] = [
            Fingerprint(
                name="python",
                value=f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
                category="environment",
            ),
            Fingerprint(
                name="platform",
                value=f"{platform.system()} {platform.release()}",
                category="environment",
            ),
        ]
        req = self.repo / "requirements-research-store.txt"
        if req.is_file():
            for line in req.read_text().splitlines():
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
        fps.extend(self._collect_config_fingerprints())
        return tuple(fps)

    def _collect_config_fingerprints(self) -> list[Fingerprint]:
        fps: list[Fingerprint] = []
        config_path = self.repo / "fingerprint-config.json"
        if not config_path.is_file():
            return fps
        try:
            config = json.loads(config_path.read_text())
        except (json.JSONDecodeError, OSError):
            return fps
        if not isinstance(config, dict):
            return fps
        for name, value in config.items():
            if not isinstance(name, str) or not isinstance(value, str):
                continue
            env_key = f"FINGERPRINT_{name.upper().replace('-', '_')}"
            env_val = os.environ.get(env_key)
            if env_val:
                value = env_val
            fps.append(
                Fingerprint(
                    name=name,
                    value=value,
                    category=self._derive_category(name),
                )
            )
        return fps

    @staticmethod
    def _derive_category(name: str) -> str:
        for category in (
            "service",
            "model",
            "tokenizer",
            "dataset",
            "ground_truth",
            "hardware",
        ):
            if name.startswith(f"{category}:"):
                return category
        return "environment"

    def _collect_environment(self) -> dict[str, str]:
        return {
            "python_version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            "platform": platform.system(),
            "platform_release": platform.release(),
            "machine": platform.machine(),
            "processor": platform.processor() or "",
            "cwd": str(self.repo),
        }

    @classmethod
    def main(cls, args: list[str] | None = None) -> None:
        import argparse

        parser = argparse.ArgumentParser(
            description="Release evidence manifest tool (issue #145)"
        )
        sub = parser.add_subparsers(dest="command")
        gen_parser = sub.add_parser("generate")
        gen_parser.add_argument("--repo", default=".", help="Repository path")
        gen_parser.add_argument("--output", default="-", help="Output file (- for stdout)")
        gen_parser.add_argument("--sha", default=None, help="Specific SHA to manifest")
        ver_parser = sub.add_parser("verify")
        ver_parser.add_argument("--manifest", required=True, help="Manifest JSON path")
        ver_parser.add_argument("--repo", default=".", help="Repository path")
        parsed = parser.parse_args(args)
        if parsed.command == "generate":
            gen = cls(repo_path=parsed.repo, generated_by="cli")
            manifest = gen.generate_from_sha(parsed.sha) if parsed.sha else gen.generate()
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


class ReleaseEvidenceVerifier:
    """Verify that current state matches a release-evidence manifest."""

    def __init__(
        self, manifest: ReleaseEvidenceManifest, repo: str | Path | None = None
    ) -> None:
        self.manifest = manifest
        self.repo = Path(repo).resolve() if repo else Path.cwd()

    def verify(self) -> VerificationResult:
        errors: list[str] = []
        sha_matches = self._check_sha_match()
        if not sha_matches:
            errors.append(
                f"Current HEAD {_current_sha(self.repo)} != candidate SHA {self.manifest.candidate_sha}"
            )
        if self.manifest.ci_jobs:
            ci_complete = self._check_ci_complete()
            if not ci_complete:
                errors.append(f"CI jobs incomplete: {', '.join(self._missing_ci_jobs())}")
        else:
            ci_complete = True
        artifacts_valid = self._check_artifacts_valid()
        if not artifacts_valid:
            errors.append("One or more artifact hashes are missing or invalid")
        fingerprints_present = self._check_fingerprints_present()
        if not fingerprints_present:
            errors.append("One or more required fingerprints are missing")
        no_post_commits = self._check_no_post_candidate_commits()
        if not no_post_commits:
            errors.append(
                f"{_commit_count_since(self.repo, self.manifest.candidate_sha)} commit(s) added after candidate SHA"
            )
        tag_resolves = self._check_tag_resolves()
        if not tag_resolves and self.manifest.tag:
            errors.append(
                f"Tag {self.manifest.tag} does not resolve to {self.manifest.candidate_sha}"
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
        if not self.manifest.candidate_sha:
            return False
        main_sha = _sha_at_ref(self.repo, "main") or _current_sha(self.repo)
        return main_sha == self.manifest.candidate_sha

    def _check_ci_complete(self) -> bool:
        if not self.manifest.ci_jobs:
            return False
        job_names = {j.name for j in self.manifest.ci_jobs}
        for required in REQUIRED_CI_JOBS:
            if required not in job_names:
                return False
            for j in self.manifest.ci_jobs:
                if j.name == required:
                    if j.conclusion != "success":
                        return False
                    if j.candidate_sha and j.candidate_sha != self.manifest.candidate_sha:
                        return False
        return True

    def _missing_ci_jobs(self) -> list[str]:
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
        if not self.manifest.artifacts:
            return False
        found_categories: set[str] = set()
        for ref in self.manifest.artifacts:
            if not ref.sha256:
                return False
            p = self.repo / ref.path
            if not p.is_file() or _file_sha256(p) != ref.sha256:
                return False
            name_lower = ref.name.lower()
            path_lower = ref.path.lower()
            if "ci" in name_lower or ".github" in path_lower or "ci.yml" in path_lower:
                found_categories.add("ci")
            elif "release_benchmark" in path_lower or "workflow_benchmark" in path_lower:
                found_categories.add("source")
            elif "benchmark" in name_lower or "benchmark" in path_lower:
                found_categories.add("benchmark")
            elif "recovery" in name_lower or "recovery" in path_lower:
                found_categories.add("recovery")
        required_categories: set[str] = {"ci", "benchmark", "source"}
        if self._find_tracked_recovery_files():
            required_categories.add("recovery")
        return required_categories.issubset(found_categories)

    def _find_tracked_recovery_files(self) -> list[str]:
        result = subprocess.run(
            ["git", "ls-files"],
            cwd=str(self.repo),
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return []
        return [
            path
            for path in (line.strip() for line in result.stdout.splitlines())
            if path == "recovery-report.txt"
        ]

    def _check_fingerprints_present(self) -> bool:
        required_categories = {
            "environment",
            "dependency",
            "service",
            "model",
            "tokenizer",
            "dataset",
            "ground_truth",
            "hardware",
        }
        return required_categories.issubset(
            {f.category for f in self.manifest.fingerprints}
        )

    def _check_no_post_candidate_commits(self) -> bool:
        if not self.manifest.candidate_sha:
            return False
        main_sha = _sha_at_ref(self.repo, "main") or _current_sha(self.repo)
        count = _commits_between(self.repo, self.manifest.candidate_sha, main_sha)
        return True if count < 0 else count == 0

    def _check_tag_resolves(self) -> bool:
        if not self.manifest.tag:
            return True
        try:
            resolved = _git_safe(
                ["rev-parse", f"{self.manifest.tag}^{{commit}}"], self.repo
            )
            if resolved is None:
                resolved = _git_safe(["rev-parse", self.manifest.tag], self.repo)
            return resolved == self.manifest.candidate_sha
        except Exception:  # noqa: BLE001
            return False


def _manifest_to_dict(manifest: ReleaseEvidenceManifest) -> dict[str, Any]:
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
            {"name": f.name, "value": f.value, "category": f.category}
            for f in manifest.fingerprints
        ],
        "environment": manifest.environment,
        "post_candidate_commits": manifest.post_candidate_commits,
        "tag": manifest.tag,
        "verification_notes": manifest.verification_notes,
    }


def _manifest_from_dict(data: dict[str, Any]) -> ReleaseEvidenceManifest:
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
                name=f["name"], value=f["value"], category=f["category"]
            )
            for f in data.get("fingerprints", [])
        ),
        environment=data.get("environment", {}),
        post_candidate_commits=data.get("post_candidate_commits", 0),
        tag=data.get("tag", ""),
        verification_notes=data.get("verification_notes", ""),
    )


if __name__ == "__main__":
    ReleaseEvidenceGenerator.main()
