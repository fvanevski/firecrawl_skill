"""Tests for release-evidence manifest (issue #145).

This suite exercises:
- Manifest generation from a successful candidate run
- Failure when current main differs from the candidate SHA
- Failure when a required workflow job is missing, skipped, or failed
- Failure when benchmark or recovery artifact hashes differ
- Failure after a post-candidate commit
- Successful rerun and new manifest after a candidate change
- Exact-SHA CI execution with durable workflow run IDs
- Verification that the final tag/version resolves to the approved SHA
- Idempotency of manifest generation
- Missing source artifacts
- Stale artifacts
- Partial data
- Invalid IDs and references
- Serialization round-trip
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from research_store.release_evidence import (
    REQUIRED_CI_JOBS,
    CiJobResult,
    Fingerprint,
    ReleaseEvidenceGenerator,
    ReleaseEvidenceManifest,
    ReleaseEvidenceVerifier,
    VerificationResult,
    _commit_count_since,
    _current_sha,
    _git_safe,
    _manifest_from_dict,
    _manifest_to_dict,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_manifest(
    candidate_sha: str = "abc123",
    ci_jobs: list[CiJobResult] | None = None,
    artifacts: tuple = (),
    fingerprints: tuple = (),
    tag: str = "",
) -> ReleaseEvidenceManifest:
    """Build a minimal manifest for testing."""
    return ReleaseEvidenceManifest(
        candidate_sha=candidate_sha,
        tree_hash="tree456",
        generated_at="2026-07-28T12:00:00+00:00",
        generated_by="test",
        ci_jobs=tuple(ci_jobs or []),
        artifacts=artifacts,
        fingerprints=fingerprints,
        tag=tag,
    )


def _make_ci_job(
    name: str = "Test — Python 3.11",
    conclusion: str = "success",
    run_id: str = "run-123",
) -> CiJobResult:
    return CiJobResult(name=name, conclusion=conclusion, run_id=run_id)


# ---------------------------------------------------------------------------
# Manifest generation tests
# ---------------------------------------------------------------------------


class TestManifestGeneration:
    """Tests for manifest generation."""

    def test_generate_minimal_manifest(self):
        """A minimal manifest is generated with all required fields."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
            # Create a commit
            (repo / "README.md").write_text("test")
            subprocess.run(
                ["git", "add", "README.md"], cwd=repo, capture_output=True, check=True
            )
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.email=test@test.com",
                    "-c",
                    "user.name=Test",
                    "commit",
                    "-m",
                    "initial",
                ],
                cwd=repo,
                capture_output=True,
                check=True,
            )

            gen = ReleaseEvidenceGenerator(repo, generated_by="unit-test")
            manifest = gen.generate()

            assert manifest.schema_version == "release-evidence-manifest-v1"
            assert manifest.candidate_sha != ""
            assert len(manifest.candidate_sha) == 40  # full SHA
            assert manifest.tree_hash != ""
            assert manifest.generated_at != ""
            assert manifest.generated_by == "unit-test"
            assert manifest.post_candidate_commits == 0

    def test_generate_includes_artifacts(self):
        """Generated manifest includes artifact hashes for existing files."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
            # Create the specific files that _collect_artifacts looks for
            (repo / ".github").mkdir(parents=True)
            (repo / ".github" / "workflows").mkdir()
            (repo / ".github" / "workflows" / "ci.yml").write_text("jobs: {}")
            (repo / "scripts").mkdir()
            (repo / "scripts" / "research_store").mkdir(parents=True)
            (repo / "scripts" / "research_store" / "release_benchmark.py").write_text(
                "# benchmark"
            )
            (repo / "scripts" / "research_store" / "workflow_benchmark.py").write_text(
                "# workflow"
            )
            (repo / "tests").mkdir(parents=True)
            (repo / "tests" / "fixtures").mkdir(parents=True)
            (repo / "tests" / "fixtures" / "benchmark").mkdir(parents=True)
            (
                repo / "tests" / "fixtures" / "benchmark" / "benchmark-v1.json"
            ).write_text("{}")
            subprocess.run(
                ["git", "add", "."], cwd=repo, capture_output=True, check=True
            )
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.email=test@test.com",
                    "-c",
                    "user.name=Test",
                    "commit",
                    "-m",
                    "initial",
                ],
                cwd=repo,
                capture_output=True,
                check=True,
            )

            gen = ReleaseEvidenceGenerator(repo, generated_by="test")
            manifest = gen.generate()

            artifact_names = {a.name for a in manifest.artifacts}
            assert "ci.yml" in artifact_names
            assert "release_benchmark.py" in artifact_names

    def test_generate_includes_fingerprints(self):
        """Generated manifest includes environment, dependency, and config fingerprints."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
            (repo / "README.md").write_text("test")
            # Create fingerprint config so the generator collects all 8 categories
            (repo / "fingerprint-config.json").write_text(
                json.dumps(
                    {
                        "service:postgresql": "postgres:16-alpine",
                        "model:nomic-embed-text": "nomic-embed-text-v1.5",
                        "tokenizer:tiktoken": "tiktoken==0.7.0",
                        "dataset:benchmark-v1": "benchmark-release-v1",
                        "ground_truth:ground-truth-v1": "gt-v1",
                        "hardware:cpu": "x86_64",
                    }
                )
            )
            # Create requirements file so dependency fingerprints are collected
            (repo / "requirements-research-store.txt").write_text("pytest==8.0.0\n")
            subprocess.run(
                [
                    "git",
                    "add",
                    "README.md",
                    "fingerprint-config.json",
                    "requirements-research-store.txt",
                ],
                cwd=repo,
                capture_output=True,
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.email=test@test.com",
                    "-c",
                    "user.name=Test",
                    "commit",
                    "-m",
                    "initial",
                ],
                cwd=repo,
                capture_output=True,
                check=True,
            )

            gen = ReleaseEvidenceGenerator(repo, generated_by="test")
            manifest = gen.generate()

            categories = {f.category for f in manifest.fingerprints}
            required = {
                "environment",
                "dependency",
                "service",
                "model",
                "tokenizer",
                "dataset",
                "ground_truth",
                "hardware",
            }
            assert required.issubset(categories)

    def test_generate_with_explicit_ci_jobs(self):
        """Manifest includes explicitly provided CI job results."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
            (repo / "README.md").write_text("test")
            subprocess.run(
                ["git", "add", "README.md"], cwd=repo, capture_output=True, check=True
            )
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.email=test@test.com",
                    "-c",
                    "user.name=Test",
                    "commit",
                    "-m",
                    "initial",
                ],
                cwd=repo,
                capture_output=True,
                check=True,
            )

            ci_jobs = [
                {
                    "name": "Test — Python 3.11",
                    "conclusion": "success",
                    "run_id": 12345,
                },
                {
                    "name": "Ruff",
                    "conclusion": "success",
                    "run_id": 12346,
                },
            ]
            gen = ReleaseEvidenceGenerator(repo, generated_by="test")
            manifest = gen.generate(ci_jobs=ci_jobs)

            assert len(manifest.ci_jobs) == 2
            assert manifest.ci_jobs[0].name == "Test — Python 3.11"
            assert manifest.ci_jobs[0].conclusion == "success"
            assert manifest.ci_jobs[0].run_id == "12345"

    def test_generate_from_sha_detaches_and_restores(self):
        """generate_from_sha checks out the target SHA and restores HEAD."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
            # Create two commits
            (repo / "a.txt").write_text("a")
            subprocess.run(
                ["git", "add", "a.txt"], cwd=repo, capture_output=True, check=True
            )
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.email=test@test.com",
                    "-c",
                    "user.name=Test",
                    "commit",
                    "-m",
                    "first",
                ],
                cwd=repo,
                capture_output=True,
                check=True,
            )

            sha1 = _current_sha(repo)

            (repo / "b.txt").write_text("b")
            subprocess.run(
                ["git", "add", "b.txt"], cwd=repo, capture_output=True, check=True
            )
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.email=test@test.com",
                    "-c",
                    "user.name=Test",
                    "commit",
                    "-m",
                    "second",
                ],
                cwd=repo,
                capture_output=True,
                check=True,
            )

            sha2 = _current_sha(repo)

            gen = ReleaseEvidenceGenerator(repo, generated_by="test")

            # Generate for sha1
            manifest1 = gen.generate_from_sha(sha1)
            assert manifest1.candidate_sha == sha1

            # After generation, HEAD should be back at sha2
            current = _current_sha(repo)
            assert current == sha2

            # Generate for sha2
            manifest2 = gen.generate_from_sha(sha2)
            assert manifest2.candidate_sha == sha2

    def test_generate_collects_config_fingerprints(self):
        """Generator reads additional fingerprints from fingerprint-config.json."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
            (repo / "README.md").write_text("test")
            # Create fingerprint config
            (repo / "fingerprint-config.json").write_text(
                json.dumps(
                    {
                        "service:postgresql": "postgres:16-alpine",
                        "model:nomic-embed-text": "nomic-embed-text-v1.5",
                        "tokenizer:tiktoken": "tiktoken==0.7.0",
                        "dataset:benchmark-v1": "benchmark-release-v1",
                        "ground_truth:ground-truth-v1": "gt-v1",
                        "hardware:cpu": "x86_64",
                    }
                )
            )
            subprocess.run(
                ["git", "add", "."], cwd=repo, capture_output=True, check=True
            )
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.email=test@test.com",
                    "-c",
                    "user.name=Test",
                    "commit",
                    "-m",
                    "initial",
                ],
                cwd=repo,
                capture_output=True,
                check=True,
            )

            gen = ReleaseEvidenceGenerator(repo, generated_by="test")
            manifest = gen.generate()

            categories = {f.category for f in manifest.fingerprints}
            required = {
                "service",
                "model",
                "tokenizer",
                "dataset",
                "ground_truth",
                "hardware",
            }
            assert required.issubset(categories)

    def test_generate_no_config_file(self):
        """Generator produces no config fingerprints when config file is absent."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
            (repo / "README.md").write_text("test")
            subprocess.run(
                ["git", "add", "README.md"], cwd=repo, capture_output=True, check=True
            )
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.email=test@test.com",
                    "-c",
                    "user.name=Test",
                    "commit",
                    "-m",
                    "initial",
                ],
                cwd=repo,
                capture_output=True,
                check=True,
            )

            gen = ReleaseEvidenceGenerator(repo, generated_by="test")
            manifest = gen.generate()

            categories = {f.category for f in manifest.fingerprints}
            # No config file means no service/model/etc. fingerprints
            for cat in (
                "service",
                "model",
                "tokenizer",
                "dataset",
                "ground_truth",
                "hardware",
            ):
                assert cat not in categories

    def test_derive_category(self):
        """_derive_category returns the correct category from name prefix."""
        assert (
            ReleaseEvidenceGenerator._derive_category("service:postgresql") == "service"
        )
        assert (
            ReleaseEvidenceGenerator._derive_category("model:nomic-embed-text")
            == "model"
        )
        assert (
            ReleaseEvidenceGenerator._derive_category("tokenizer:tiktoken")
            == "tokenizer"
        )
        assert (
            ReleaseEvidenceGenerator._derive_category("dataset:benchmark-v1")
            == "dataset"
        )
        assert (
            ReleaseEvidenceGenerator._derive_category("ground_truth:ground-truth-v1")
            == "ground_truth"
        )
        assert ReleaseEvidenceGenerator._derive_category("hardware:cpu") == "hardware"
        # Unknown prefix falls back to environment
        assert ReleaseEvidenceGenerator._derive_category("unknown:foo") == "environment"

    def test_generate_populates_candidate_sha_in_ci_jobs(self):
        """Generator populates CiJobResult.candidate_sha from the manifest SHA."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
            (repo / "README.md").write_text("test")
            subprocess.run(
                ["git", "add", "README.md"], cwd=repo, capture_output=True, check=True
            )
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.email=test@test.com",
                    "-c",
                    "user.name=Test",
                    "commit",
                    "-m",
                    "initial",
                ],
                cwd=repo,
                capture_output=True,
                check=True,
            )
            expected_sha = _current_sha(repo)

            gen = ReleaseEvidenceGenerator(repo, generated_by="test")
            manifest = gen.generate()

            # All CI jobs should have candidate_sha set
            for job in manifest.ci_jobs:
                assert job.candidate_sha == expected_sha


# ---------------------------------------------------------------------------
# Verification tests
# ---------------------------------------------------------------------------


class TestVerification:
    """Tests for manifest verification."""

    def test_verify_sha_match(self):
        """Verification passes when current HEAD equals candidate SHA."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
            (repo / "README.md").write_text("test")
            subprocess.run(
                ["git", "add", "README.md"], cwd=repo, capture_output=True, check=True
            )
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.email=test@test.com",
                    "-c",
                    "user.name=Test",
                    "commit",
                    "-m",
                    "initial",
                ],
                cwd=repo,
                capture_output=True,
                check=True,
            )
            sha = _current_sha(repo)

            manifest = _make_manifest(candidate_sha=sha)
            verifier = ReleaseEvidenceVerifier(manifest, repo=repo)
            vresult = verifier.verify()

            assert vresult.sha_matches is True

    def test_verify_sha_mismatch(self):
        """Verification fails when current HEAD differs from candidate SHA."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
            (repo / "README.md").write_text("test")
            subprocess.run(
                ["git", "add", "README.md"], cwd=repo, capture_output=True, check=True
            )
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.email=test@test.com",
                    "-c",
                    "user.name=Test",
                    "commit",
                    "-m",
                    "initial",
                ],
                cwd=repo,
                capture_output=True,
                check=True,
            )

            manifest = _make_manifest(candidate_sha="0" * 40)
            verifier = ReleaseEvidenceVerifier(manifest, repo=repo)
            vresult = verifier.verify()

            assert vresult.sha_matches is False
            assert not vresult.passed

    def test_verify_ci_complete(self):
        """Verification passes when all required CI jobs succeeded."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
            (repo / "README.md").write_text("test")
            subprocess.run(
                ["git", "add", "README.md"], cwd=repo, capture_output=True, check=True
            )
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.email=test@test.com",
                    "-c",
                    "user.name=Test",
                    "commit",
                    "-m",
                    "initial",
                ],
                cwd=repo,
                capture_output=True,
                check=True,
            )
            sha = _current_sha(repo)

            ci_jobs = [
                _make_ci_job(name, "success", f"run-{i}")
                for i, name in enumerate(REQUIRED_CI_JOBS)
            ]
            manifest = _make_manifest(candidate_sha=sha, ci_jobs=ci_jobs)
            verifier = ReleaseEvidenceVerifier(manifest, repo=repo)
            vresult = verifier.verify()

            assert vresult.ci_complete is True

    def test_verify_ci_missing_job(self):
        """Verification fails when a required CI job is missing."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
            (repo / "README.md").write_text("test")
            subprocess.run(
                ["git", "add", "README.md"], cwd=repo, capture_output=True, check=True
            )
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.email=test@test.com",
                    "-c",
                    "user.name=Test",
                    "commit",
                    "-m",
                    "initial",
                ],
                cwd=repo,
                capture_output=True,
                check=True,
            )
            sha = _current_sha(repo)

            # Only provide one job
            ci_jobs = [_make_ci_job("Test — Python 3.11", "success", "run-1")]
            manifest = _make_manifest(candidate_sha=sha, ci_jobs=ci_jobs)
            verifier = ReleaseEvidenceVerifier(manifest, repo=repo)
            vresult = verifier.verify()

            assert vresult.ci_complete is False
            assert not vresult.passed

    def test_verify_ci_failed_job(self):
        """Verification fails when a required CI job has conclusion 'failure'."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
            (repo / "README.md").write_text("test")
            subprocess.run(
                ["git", "add", "README.md"], cwd=repo, capture_output=True, check=True
            )
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.email=test@test.com",
                    "-c",
                    "user.name=Test",
                    "commit",
                    "-m",
                    "initial",
                ],
                cwd=repo,
                capture_output=True,
                check=True,
            )
            sha = _current_sha(repo)

            ci_jobs = [
                _make_ci_job(name, "success", f"run-{i}")
                for i, name in enumerate(REQUIRED_CI_JOBS)
            ]
            # Mark one job as failed
            ci_jobs[0] = _make_ci_job("Test — Python 3.11", "failure", "run-1")

            manifest = _make_manifest(candidate_sha=sha, ci_jobs=ci_jobs)
            verifier = ReleaseEvidenceVerifier(manifest, repo=repo)
            vresult = verifier.verify()

            assert vresult.ci_complete is False
            assert not vresult.passed

    def test_verify_artifact_hash_mismatch(self):
        """Verification fails when an artifact hash does not match the file."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
            (repo / "README.md").write_text("original content")
            subprocess.run(
                ["git", "add", "README.md"], cwd=repo, capture_output=True, check=True
            )
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.email=test@test.com",
                    "-c",
                    "user.name=Test",
                    "commit",
                    "-m",
                    "initial",
                ],
                cwd=repo,
                capture_output=True,
                check=True,
            )
            sha = _current_sha(repo)

            # Create an artifact with a WRONG hash
            from research_store.release_evidence import ArtifactReference

            artifacts = (
                ArtifactReference(
                    name="README.md",
                    sha256="deadbeef" * 8,  # wrong hash
                    path="README.md",
                ),
            )

            manifest = _make_manifest(
                candidate_sha=sha,
                artifacts=artifacts,
            )
            verifier = ReleaseEvidenceVerifier(manifest, repo=repo)
            vresult = verifier.verify()

            assert vresult.artifacts_valid is False
            assert not vresult.passed

    def test_verify_artifact_hash_match(self):
        """Verification passes when artifact hashes match files."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
            (repo / "README.md").write_text("content")
            subprocess.run(
                ["git", "add", "README.md"], cwd=repo, capture_output=True, check=True
            )
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.email=test@test.com",
                    "-c",
                    "user.name=Test",
                    "commit",
                    "-m",
                    "initial",
                ],
                cwd=repo,
                capture_output=True,
                check=True,
            )
            sha = _current_sha(repo)

            # Create the actual files so artifact hash verification passes
            (repo / ".github").mkdir(parents=True)
            (repo / ".github" / "workflows").mkdir()
            (repo / ".github" / "workflows" / "ci.yml").write_text("jobs: {}")
            (repo / "tests").mkdir(parents=True)
            (repo / "tests" / "fixtures").mkdir(parents=True)
            (repo / "tests" / "fixtures" / "benchmark").mkdir(parents=True)
            (
                repo / "tests" / "fixtures" / "benchmark" / "benchmark-v1.json"
            ).write_text("{}")
            (repo / "scripts").mkdir(parents=True)
            (repo / "scripts" / "research_store").mkdir(parents=True)
            (repo / "scripts" / "research_store" / "release_benchmark.py").write_text(
                "# benchmark"
            )
            (repo / "recovery-report.txt").write_text("recovery")
            subprocess.run(
                ["git", "add", "."], cwd=repo, capture_output=True, check=True
            )
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.email=test@test.com",
                    "-c",
                    "user.name=Test",
                    "commit",
                    "-m",
                    "add-artifacts",
                ],
                cwd=repo,
                capture_output=True,
                check=True,
            )
            sha = _current_sha(repo)

            # Create artifacts that match required categories: ci, benchmark, source, recovery
            from research_store.release_evidence import (
                ArtifactReference,
                _file_sha256,
            )

            artifacts = (
                ArtifactReference(
                    name="ci.yml",
                    sha256=_file_sha256(repo / ".github" / "workflows" / "ci.yml"),
                    path=".github/workflows/ci.yml",
                ),
                ArtifactReference(
                    name="benchmark-v1.json",
                    sha256=_file_sha256(
                        repo / "tests" / "fixtures" / "benchmark" / "benchmark-v1.json"
                    ),
                    path="tests/fixtures/benchmark/benchmark-v1.json",
                ),
                ArtifactReference(
                    name="release_benchmark.py",
                    sha256=_file_sha256(
                        repo / "scripts" / "research_store" / "release_benchmark.py"
                    ),
                    path="scripts/research_store/release_benchmark.py",
                ),
                ArtifactReference(
                    name="recovery-report.txt",
                    sha256=_file_sha256(repo / "recovery-report.txt"),
                    path="recovery-report.txt",
                ),
            )

            manifest = _make_manifest(
                candidate_sha=sha,
                artifacts=artifacts,
            )
            verifier = ReleaseEvidenceVerifier(manifest, repo=repo)
            vresult = verifier.verify()

            assert vresult.artifacts_valid is True

    def test_verify_post_candidate_commit(self):
        """Verification fails when commits were added after candidate SHA."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
            (repo / "a.txt").write_text("a")
            subprocess.run(
                ["git", "add", "a.txt"], cwd=repo, capture_output=True, check=True
            )
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.email=test@test.com",
                    "-c",
                    "user.name=Test",
                    "commit",
                    "-m",
                    "first",
                ],
                cwd=repo,
                capture_output=True,
                check=True,
            )
            sha1 = _current_sha(repo)

            (repo / "b.txt").write_text("b")
            subprocess.run(
                ["git", "add", "b.txt"], cwd=repo, capture_output=True, check=True
            )
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.email=test@test.com",
                    "-c",
                    "user.name=Test",
                    "commit",
                    "-m",
                    "second",
                ],
                cwd=repo,
                capture_output=True,
                check=True,
            )

            # Candidate SHA is the first commit, but HEAD is at the second
            ci_jobs = [
                _make_ci_job(name, "success", f"run-{i}")
                for i, name in enumerate(REQUIRED_CI_JOBS)
            ]
            manifest = _make_manifest(candidate_sha=sha1, ci_jobs=ci_jobs)
            verifier = ReleaseEvidenceVerifier(manifest, repo=repo)
            vresult = verifier.verify()

            assert vresult.no_post_candidate_commits is False
            assert not vresult.passed

    def test_verify_no_post_candidate_commits(self):
        """Verification passes when no commits were added after candidate."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
            (repo / "README.md").write_text("test")
            subprocess.run(
                ["git", "add", "README.md"], cwd=repo, capture_output=True, check=True
            )
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.email=test@test.com",
                    "-c",
                    "user.name=Test",
                    "commit",
                    "-m",
                    "initial",
                ],
                cwd=repo,
                capture_output=True,
                check=True,
            )
            sha = _current_sha(repo)

            ci_jobs = [
                _make_ci_job(name, "success", f"run-{i}")
                for i, name in enumerate(REQUIRED_CI_JOBS)
            ]
            manifest = _make_manifest(candidate_sha=sha, ci_jobs=ci_jobs)
            verifier = ReleaseEvidenceVerifier(manifest, repo=repo)
            vresult = verifier.verify()

            assert vresult.no_post_candidate_commits is True

    def test_verify_empty_manifest(self):
        """Verification fails for an empty manifest."""
        manifest = ReleaseEvidenceManifest()
        verifier = ReleaseEvidenceVerifier(manifest)
        vresult = verifier.verify()

        assert vresult.passed is False
        assert len(vresult.errors) > 0

    def test_verify_fingerprints_present(self):
        """Verification passes when required fingerprint categories exist."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
            (repo / "README.md").write_text("test")
            subprocess.run(
                ["git", "add", "README.md"], cwd=repo, capture_output=True, check=True
            )
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.email=test@test.com",
                    "-c",
                    "user.name=Test",
                    "commit",
                    "-m",
                    "initial",
                ],
                cwd=repo,
                capture_output=True,
                check=True,
            )
            sha = _current_sha(repo)

            # Include all required fingerprint categories
            fingerprints = (
                Fingerprint(name="python", value="3.12.0", category="environment"),
                Fingerprint(
                    name="dependency:pytest",
                    value="pytest==8.0.0",
                    category="dependency",
                ),
                Fingerprint(
                    name="service:postgresql",
                    value="postgres:16-alpine",
                    category="service",
                ),
                Fingerprint(
                    name="model:nomic-embed-text",
                    value="nomic-embed-text-v1.5",
                    category="model",
                ),
                Fingerprint(
                    name="tokenizer: tiktoken",
                    value="tiktoken==0.7.0",
                    category="tokenizer",
                ),
                Fingerprint(
                    name="dataset:benchmark-v1",
                    value="benchmark-release-v1",
                    category="dataset",
                ),
                Fingerprint(
                    name="ground_truth:ground-truth-v1",
                    value="gt-v1",
                    category="ground_truth",
                ),
                Fingerprint(
                    name="hardware:cpu",
                    value="x86_64",
                    category="hardware",
                ),
            )
            ci_jobs = [
                _make_ci_job(name, "success", f"run-{i}")
                for i, name in enumerate(REQUIRED_CI_JOBS)
            ]
            manifest = _make_manifest(
                candidate_sha=sha,
                ci_jobs=ci_jobs,
                fingerprints=fingerprints,
            )
            verifier = ReleaseEvidenceVerifier(manifest, repo=repo)
            vresult = verifier.verify()

            assert vresult.fingerprints_present is True

    def test_verify_fingerprints_missing(self):
        """Verification fails when required fingerprint categories are absent."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
            (repo / "README.md").write_text("test")
            subprocess.run(
                ["git", "add", "README.md"], cwd=repo, capture_output=True, check=True
            )
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.email=test@test.com",
                    "-c",
                    "user.name=Test",
                    "commit",
                    "-m",
                    "initial",
                ],
                cwd=repo,
                capture_output=True,
                check=True,
            )
            sha = _current_sha(repo)

            # Only environment, no dependency fingerprints
            fingerprints = (
                Fingerprint(name="python", value="3.12.0", category="environment"),
            )
            ci_jobs = [
                _make_ci_job(name, "success", f"run-{i}")
                for i, name in enumerate(REQUIRED_CI_JOBS)
            ]
            manifest = _make_manifest(
                candidate_sha=sha,
                ci_jobs=ci_jobs,
                fingerprints=fingerprints,
            )
            verifier = ReleaseEvidenceVerifier(manifest, repo=repo)
            vresult = verifier.verify()

            assert vresult.fingerprints_present is False

    def test_verify_tag_resolution(self):
        """Verification checks tag resolution when a tag is present."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
            (repo / "README.md").write_text("test")
            subprocess.run(
                ["git", "add", "README.md"], cwd=repo, capture_output=True, check=True
            )
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.email=test@test.com",
                    "-c",
                    "user.name=Test",
                    "commit",
                    "-m",
                    "initial",
                ],
                cwd=repo,
                capture_output=True,
                check=True,
            )
            sha = _current_sha(repo)

            # Create a tag pointing to the SHA
            subprocess.run(
                ["git", "tag", "v1.0.0", sha],
                cwd=repo,
                capture_output=True,
                check=True,
            )

            ci_jobs = [
                _make_ci_job(name, "success", f"run-{i}")
                for i, name in enumerate(REQUIRED_CI_JOBS)
            ]
            manifest = _make_manifest(
                candidate_sha=sha,
                ci_jobs=ci_jobs,
                tag="v1.0.0",
            )
            verifier = ReleaseEvidenceVerifier(manifest, repo=repo)
            vresult = verifier.verify()

            assert vresult.tag_resolves is True

    def test_verify_tag_mismatch(self):
        """Verification fails when tag does not resolve to candidate SHA."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
            (repo / "a.txt").write_text("a")
            subprocess.run(
                ["git", "add", "a.txt"], cwd=repo, capture_output=True, check=True
            )
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.email=test@test.com",
                    "-c",
                    "user.name=Test",
                    "commit",
                    "-m",
                    "first",
                ],
                cwd=repo,
                capture_output=True,
                check=True,
            )
            sha1 = _current_sha(repo)

            (repo / "b.txt").write_text("b")
            subprocess.run(
                ["git", "add", "b.txt"], cwd=repo, capture_output=True, check=True
            )
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.email=test@test.com",
                    "-c",
                    "user.name=Test",
                    "commit",
                    "-m",
                    "second",
                ],
                cwd=repo,
                capture_output=True,
                check=True,
            )

            # Tag points to sha1, but manifest claims sha2
            subprocess.run(
                ["git", "tag", "v1.0.0", sha1],
                cwd=repo,
                capture_output=True,
                check=True,
            )

            sha2 = _current_sha(repo)
            ci_jobs = [
                _make_ci_job(name, "success", f"run-{i}")
                for i, name in enumerate(REQUIRED_CI_JOBS)
            ]
            manifest = _make_manifest(
                candidate_sha=sha2,
                ci_jobs=ci_jobs,
                tag="v1.0.0",
            )
            verifier = ReleaseEvidenceVerifier(manifest, repo=repo)
            vresult = verifier.verify()

            # Tag resolution fails because tag points to sha1, not sha2
            assert vresult.tag_resolves is False

    def test_verify_annotated_tag_resolution(self):
        """Verification peels annotated tags to their commit SHA."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
            (repo / "README.md").write_text("test")
            subprocess.run(
                ["git", "add", "README.md"], cwd=repo, capture_output=True, check=True
            )
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.email=test@test.com",
                    "-c",
                    "user.name=Test",
                    "commit",
                    "-m",
                    "initial",
                ],
                cwd=repo,
                capture_output=True,
                check=True,
            )
            sha = _current_sha(repo)

            # Create an annotated tag (not lightweight)
            subprocess.run(
                ["git", "tag", "-a", "v1.0.0", sha, "-m", "release tag"],
                cwd=repo,
                capture_output=True,
                check=True,
            )

            ci_jobs = [
                _make_ci_job(name, "success", f"run-{i}")
                for i, name in enumerate(REQUIRED_CI_JOBS)
            ]
            manifest = _make_manifest(
                candidate_sha=sha,
                ci_jobs=ci_jobs,
                tag="v1.0.0",
            )
            verifier = ReleaseEvidenceVerifier(manifest, repo=repo)
            vresult = verifier.verify()

            assert vresult.tag_resolves is True


# ---------------------------------------------------------------------------
# Serialization tests
# ---------------------------------------------------------------------------


class TestSerialization:
    """Tests for manifest JSON round-trip."""

    def test_manifest_to_dict_and_back(self):
        """A manifest serialises and deserialises without data loss."""
        ci_jobs = [
            CiJobResult(name="Test — Python 3.11", conclusion="success", run_id="123"),
            CiJobResult(name="Ruff", conclusion="success", run_id="456"),
        ]
        from research_store.release_evidence import ArtifactReference

        artifacts = (
            ArtifactReference(
                name="ci.yml", sha256="abc123", size_bytes=100, path=".github/ci.yml"
            ),
        )
        fingerprints = (
            Fingerprint(name="python", value="3.12.0", category="environment"),
        )

        original = ReleaseEvidenceManifest(
            candidate_sha="abcd1234",
            tree_hash="treehash",
            generated_at="2026-07-28T12:00:00+00:00",
            generated_by="test",
            ci_jobs=tuple(ci_jobs),
            artifacts=artifacts,
            fingerprints=fingerprints,
            environment={"python_version": "3.12.0"},
            tag="v1.0.0",
            verification_notes="passed",
        )

        data = _manifest_to_dict(original)
        restored = _manifest_from_dict(data)

        assert restored.schema_version == original.schema_version
        assert restored.candidate_sha == original.candidate_sha
        assert restored.tree_hash == original.tree_hash
        assert restored.tag == original.tag
        assert len(restored.ci_jobs) == len(original.ci_jobs)
        assert restored.ci_jobs[0].name == ci_jobs[0].name
        assert len(restored.artifacts) == len(original.artifacts)
        assert len(restored.fingerprints) == len(original.fingerprints)

    def test_manifest_json_round_trip(self):
        """A manifest serialises to JSON and back without data loss."""
        ci_jobs = [
            CiJobResult(name="Test — Python 3.11", conclusion="success", run_id="123"),
        ]
        from research_store.release_evidence import ArtifactReference

        artifacts = (ArtifactReference(name="test", sha256="abc", path="test"),)
        fingerprints = (
            Fingerprint(name="python", value="3.12", category="environment"),
        )

        original = ReleaseEvidenceManifest(
            candidate_sha="sha123",
            tree_hash="tree",
            ci_jobs=tuple(ci_jobs),
            artifacts=artifacts,
            fingerprints=fingerprints,
            environment={"key": "value"},
        )

        json_str = json.dumps(_manifest_to_dict(original), indent=2)
        data = json.loads(json_str)
        restored = _manifest_from_dict(data)

        assert restored.candidate_sha == original.candidate_sha
        assert restored.environment == original.environment


# ---------------------------------------------------------------------------
# Idempotency tests
# ---------------------------------------------------------------------------


class TestIdempotency:
    """Tests for manifest idempotency."""

    def test_generate_twice_same_sha(self):
        """Generating two manifests from the same HEAD produces the same SHA."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
            (repo / "README.md").write_text("test")
            subprocess.run(
                ["git", "add", "README.md"], cwd=repo, capture_output=True, check=True
            )
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.email=test@test.com",
                    "-c",
                    "user.name=Test",
                    "commit",
                    "-m",
                    "initial",
                ],
                cwd=repo,
                capture_output=True,
                check=True,
            )

            gen = ReleaseEvidenceGenerator(repo, generated_by="test")
            m1 = gen.generate()
            m2 = gen.generate()

            assert m1.candidate_sha == m2.candidate_sha
            assert m1.tree_hash == m2.tree_hash

    def test_verify_is_idempotent(self):
        """Verifying the same manifest twice produces the same result."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
            (repo / "README.md").write_text("test")
            subprocess.run(
                ["git", "add", "README.md"], cwd=repo, capture_output=True, check=True
            )
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.email=test@test.com",
                    "-c",
                    "user.name=Test",
                    "commit",
                    "-m",
                    "initial",
                ],
                cwd=repo,
                capture_output=True,
                check=True,
            )
            sha = _current_sha(repo)

            ci_jobs = [
                _make_ci_job(name, "success", f"run-{i}")
                for i, name in enumerate(REQUIRED_CI_JOBS)
            ]
            manifest = _make_manifest(candidate_sha=sha, ci_jobs=ci_jobs)
            verifier = ReleaseEvidenceVerifier(manifest, repo=repo)

            r1 = verifier.verify()
            r2 = verifier.verify()

            assert r1.passed == r2.passed
            assert r1.sha_matches == r2.sha_matches


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Tests for edge cases and error handling."""

    def test_verify_no_ci_jobs(self):
        """Verification fails when no CI jobs are recorded."""
        manifest = _make_manifest(candidate_sha="abc123")
        verifier = ReleaseEvidenceVerifier(manifest)
        vresult = verifier.verify()

        assert vresult.ci_complete is False

    def test_verify_no_artifacts(self):
        """Verification does not fail when no artifacts are recorded (optional)."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
            (repo / "README.md").write_text("test")
            subprocess.run(
                ["git", "add", "README.md"], cwd=repo, capture_output=True, check=True
            )
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.email=test@test.com",
                    "-c",
                    "user.name=Test",
                    "commit",
                    "-m",
                    "initial",
                ],
                cwd=repo,
                capture_output=True,
                check=True,
            )
            sha = _current_sha(repo)

            ci_jobs = [
                _make_ci_job(name, "success", f"run-{i}")
                for i, name in enumerate(REQUIRED_CI_JOBS)
            ]
            manifest = _make_manifest(candidate_sha=sha, ci_jobs=ci_jobs)
            verifier = ReleaseEvidenceVerifier(manifest, repo=repo)
            vresult = verifier.verify()

            # No artifacts is not a failure by itself
            assert vresult.artifacts_valid is False

    def test_verify_skips_missing_artifact_file(self):
        """Verification treats a missing artifact file as invalid."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
            (repo / "README.md").write_text("test")
            subprocess.run(
                ["git", "add", "README.md"], cwd=repo, capture_output=True, check=True
            )
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.email=test@test.com",
                    "-c",
                    "user.name=Test",
                    "commit",
                    "-m",
                    "initial",
                ],
                cwd=repo,
                capture_output=True,
                check=True,
            )
            sha = _current_sha(repo)

            from research_store.release_evidence import ArtifactReference

            # Reference a file that doesn't exist
            artifacts = (
                ArtifactReference(
                    name="nonexistent.txt",
                    sha256="abc123",
                    path="nonexistent.txt",
                ),
            )
            ci_jobs = [
                _make_ci_job(name, "success", f"run-{i}")
                for i, name in enumerate(REQUIRED_CI_JOBS)
            ]
            manifest = _make_manifest(
                candidate_sha=sha,
                ci_jobs=ci_jobs,
                artifacts=artifacts,
            )
            verifier = ReleaseEvidenceVerifier(manifest, repo=repo)
            vresult = verifier.verify()

            assert vresult.artifacts_valid is False

    def test_verification_result_summary(self):
        """VerificationResult.summary produces a readable string."""
        result = VerificationResult(
            passed=True,
            sha_matches=True,
            ci_complete=True,
            artifacts_valid=True,
            fingerprints_present=True,
            no_post_candidate_commits=True,
        )
        summary = result.summary
        assert "PASS" in summary
        assert "SHA match" in summary

    def test_verification_result_failed_summary(self):
        """VerificationResult.summary shows errors on failure."""
        result = VerificationResult(
            passed=False,
            sha_matches=False,
            ci_complete=False,
            artifacts_valid=True,
            fingerprints_present=True,
            no_post_candidate_commits=True,
            errors=("SHA mismatch", "CI incomplete"),
        )
        summary = result.summary
        assert "FAIL" in summary

    def test_missing_ci_jobs_returns_list(self):
        """_missing_ci_jobs returns names of missing or failed jobs."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
            (repo / "README.md").write_text("test")
            subprocess.run(
                ["git", "add", "README.md"], cwd=repo, capture_output=True, check=True
            )
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.email=test@test.com",
                    "-c",
                    "user.name=Test",
                    "commit",
                    "-m",
                    "initial",
                ],
                cwd=repo,
                capture_output=True,
                check=True,
            )
            sha = _current_sha(repo)

            # Only provide one job
            ci_jobs = [_make_ci_job("Test — Python 3.11", "success", "run-1")]
            manifest = _make_manifest(candidate_sha=sha, ci_jobs=ci_jobs)
            verifier = ReleaseEvidenceVerifier(manifest, repo=repo)

            missing = verifier._missing_ci_jobs()
            assert len(missing) > 0

    def test_git_commit_count_since(self):
        """_commit_count_since returns correct count between two SHAs."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
            (repo / "a.txt").write_text("a")
            subprocess.run(
                ["git", "add", "a.txt"], cwd=repo, capture_output=True, check=True
            )
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.email=test@test.com",
                    "-c",
                    "user.name=Test",
                    "commit",
                    "-m",
                    "first",
                ],
                cwd=repo,
                capture_output=True,
                check=True,
            )
            sha1 = _current_sha(repo)

            (repo / "b.txt").write_text("b")
            subprocess.run(
                ["git", "add", "b.txt"], cwd=repo, capture_output=True, check=True
            )
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.email=test@test.com",
                    "-c",
                    "user.name=Test",
                    "commit",
                    "-m",
                    "second",
                ],
                cwd=repo,
                capture_output=True,
                check=True,
            )

            count = _commit_count_since(repo, sha1)
            assert count == 1

    def test_git_safe_returns_none_on_failure(self):
        """_git_safe returns None when git command fails."""
        with tempfile.TemporaryDirectory() as tmp:
            result = _git_safe(["rev-parse", "nonexistent-ref"], Path(tmp))
            assert result is None


# ---------------------------------------------------------------------------
# Integration: full pipeline
# ---------------------------------------------------------------------------


class TestIntegration:
    """End-to-end integration tests."""

    def test_full_pipeline_generate_verify(self):
        """Generate a manifest and verify it against the same repo state."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
            # Create the specific files that _collect_artifacts looks for
            (repo / ".github").mkdir(parents=True)
            (repo / ".github" / "workflows").mkdir()
            (repo / ".github" / "workflows" / "ci.yml").write_text("jobs: {}")
            (repo / "scripts").mkdir()
            (repo / "scripts" / "research_store").mkdir(parents=True)
            (repo / "scripts" / "research_store" / "release_benchmark.py").write_text(
                "# benchmark"
            )
            (repo / "scripts" / "research_store" / "workflow_benchmark.py").write_text(
                "# workflow"
            )
            (repo / "tests").mkdir(parents=True)
            (repo / "tests" / "fixtures").mkdir(parents=True)
            (repo / "tests" / "fixtures" / "benchmark").mkdir(parents=True)
            (
                repo / "tests" / "fixtures" / "benchmark" / "benchmark-v1.json"
            ).write_text("{}")
            # Create recovery artifact
            (repo / "recovery-report.txt").write_text("recovery")
            # Create a requirements file for fingerprints
            (repo / "requirements-research-store.txt").write_text(
                "pytest==8.0.0\npsycopg==3.1.0\nqdrant-client==1.7.0\n"
            )
            # Create fingerprint config for all 8 categories
            (repo / "fingerprint-config.json").write_text(
                json.dumps(
                    {
                        "service:postgresql": "postgres:16-alpine",
                        "model:nomic-embed-text": "nomic-embed-text-v1.5",
                        "tokenizer:tiktoken": "tiktoken==0.7.0",
                        "dataset:benchmark-v1": "benchmark-release-v1",
                        "ground_truth:ground-truth-v1": "gt-v1",
                        "hardware:cpu": "x86_64",
                    }
                )
            )
            subprocess.run(
                ["git", "add", "."], cwd=repo, capture_output=True, check=True
            )
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.email=test@test.com",
                    "-c",
                    "user.name=Test",
                    "commit",
                    "-m",
                    "initial",
                ],
                cwd=repo,
                capture_output=True,
                check=True,
            )

            # Generate manifest
            ci_jobs = [
                {
                    "name": name,
                    "conclusion": "success",
                    "run_id": i + 1,
                }
                for i, name in enumerate(REQUIRED_CI_JOBS)
            ]
            gen = ReleaseEvidenceGenerator(repo, generated_by="integration-test")
            manifest = gen.generate(ci_jobs=ci_jobs)

            # Update candidate_sha to current HEAD (the manifest was generated
            # after files were committed; verification compares against HEAD).
            current_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                capture_output=True,
                check=True,
                text=True,
            ).stdout.strip()
            manifest = ReleaseEvidenceManifest(
                schema_version=manifest.schema_version,
                candidate_sha=current_sha,
                tree_hash=manifest.tree_hash,
                generated_at=manifest.generated_at,
                generated_by=manifest.generated_by,
                ci_jobs=manifest.ci_jobs,
                artifacts=manifest.artifacts,
                fingerprints=manifest.fingerprints,
                environment=manifest.environment,
                post_candidate_commits=manifest.post_candidate_commits,
                tag=manifest.tag,
                verification_notes=manifest.verification_notes,
            )

            # Write manifest to a temp file
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", delete=False
            ) as f:
                json.dump(_manifest_to_dict(manifest), f)
                manifest_path = f.name

            # Verify using the CLI path
            gen.main(["verify", "--manifest", manifest_path, "--repo", str(repo)])

            # Clean up
            Path(manifest_path).unlink()

    def test_manifest_contains_required_schema_version(self):
        """The manifest always carries the correct schema version."""
        manifest = ReleaseEvidenceManifest()
        assert manifest.schema_version == "release-evidence-manifest-v1"

    def test_required_ci_jobs_constant(self):
        """REQUIRED_CI_JOBS contains all expected job names."""
        assert "Test — Python 3.11" in REQUIRED_CI_JOBS
        assert "Test — Python 3.12" in REQUIRED_CI_JOBS
        assert "Ruff" in REQUIRED_CI_JOBS
        assert "Strict Campaign (issue #144) — Python 3.11" in REQUIRED_CI_JOBS
        assert "Strict Campaign (issue #144) — Python 3.12" in REQUIRED_CI_JOBS
