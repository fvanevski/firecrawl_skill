from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).with_name("disposable-test-services")


def run_helper(
    *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SCRIPT), *args],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def test_env_emits_exact_reset_contract() -> None:
    result = run_helper("--namespace", "fc263", "env")

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert result.stdout.splitlines() == [
        "export RESEARCH_STORE_TEST_DATABASE_URL='postgresql://postgres:postgres@127.0.0.1:55436/fc263_test'",
        "export RESEARCH_STORE_TEST_ALLOW_RESET='fc263_test'",
        "export QDRANT_URL='http://127.0.0.1:55437'",
        "export RESEARCH_STORE_TEST_QDRANT_URL='http://127.0.0.1:55437'",
        "export RESEARCH_STORE_TEST_QDRANT_ALLOW_RESET='http://127.0.0.1:55437'",
    ]


def test_namespace_normalization_preserves_standalone_test_segment() -> None:
    result = run_helper("--namespace", "issue-263", "env")

    assert result.returncode == 0, result.stderr
    assert "/issue_263_test'" in result.stdout
    assert "RESEARCH_STORE_TEST_ALLOW_RESET='issue_263_test'" in result.stdout


@pytest.mark.parametrize(
    "namespace", ["", "../oops", "UpperCase", "has.dot", "with space"]
)
def test_invalid_namespace_fails_closed(namespace: str) -> None:
    result = run_helper("--namespace", namespace, "env")

    assert result.returncode != 0
    assert "namespace must match" in result.stderr


@pytest.mark.parametrize(
    ("option", "port"),
    [
        ("--pg-port", "55432"),
        ("--pg-port", "6333"),
        ("--qdrant-port", "55432"),
        ("--qdrant-port", "6333"),
    ],
)
def test_known_persistent_ports_are_rejected(option: str, port: str) -> None:
    result = run_helper(option, port, "env")

    assert result.returncode != 0
    assert "reserved for a persistent local service" in result.stderr


def test_services_cannot_share_a_host_port() -> None:
    result = run_helper("--pg-port", "55446", "--qdrant-port", "55446", "env")

    assert result.returncode != 0
    assert "host ports must differ" in result.stderr


def make_fake_tools(
    tmp_path: Path, *, existing: bool = False, owned: bool = True
) -> tuple[dict[str, str], Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "docker.log"

    docker = bin_dir / "docker"
    docker.write_text(
        """#!/usr/bin/env python3
import os
import sys
from pathlib import Path

args = sys.argv[1:]
with Path(os.environ["FAKE_DOCKER_LOG"]).open("a") as stream:
    stream.write(" ".join(args) + "\\n")

if args and args[0] == "inspect":
    if not os.environ.get("FAKE_DOCKER_EXISTS"):
        raise SystemExit(1)
    if "--format" in args:
        template = args[args.index("--format") + 1]
        if "disposable-test" in template:
            print("true" if os.environ.get("FAKE_DOCKER_OWNED") == "1" else "false")
        elif "test-namespace" in template:
            print(os.environ.get("FAKE_DOCKER_NAMESPACE", ""))
    raise SystemExit(0)

raise SystemExit(0)
"""
    )
    docker.chmod(0o755)

    curl = bin_dir / "curl"
    curl.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ ${FAKE_CURL_FAIL:-0} == 1 ]]; then exit 1; fi\n"
        "exit 0\n"
    )
    curl.chmod(0o755)

    sleep = bin_dir / "sleep"
    sleep.write_text("#!/usr/bin/env bash\nexit 0\n")
    sleep.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["FAKE_DOCKER_LOG"] = str(log_path)
    if existing:
        env["FAKE_DOCKER_EXISTS"] = "1"
        env["FAKE_DOCKER_OWNED"] = "1" if owned else "0"
        env["FAKE_DOCKER_NAMESPACE"] = "fc263"
    return env, log_path


def test_up_uses_loopback_pinned_images_and_ownership_labels(tmp_path: Path) -> None:
    env, log_path = make_fake_tools(tmp_path)

    result = run_helper("--namespace", "fc263", "up", env=env)

    assert result.returncode == 0, result.stderr
    log = log_path.read_text()
    assert "postgres:16-alpine" in log
    assert "qdrant/qdrant:v1.18.3-unprivileged" in log
    assert "127.0.0.1:55436:5432" in log
    assert "127.0.0.1:55437:6333" in log
    assert "io.firecrawl-skill.disposable-test=true" in log
    assert "io.firecrawl-skill.test-namespace=fc263" in log
    assert 'CREATE DATABASE "fc263_test";' in log


def test_down_refuses_same_named_non_owned_container(tmp_path: Path) -> None:
    env, log_path = make_fake_tools(tmp_path, existing=True, owned=False)

    result = run_helper("--namespace", "fc263", "down", env=env)

    assert result.returncode != 0
    assert "refusing to modify existing non-owned container" in result.stderr
    assert "rm -f" not in log_path.read_text()


def test_reset_qdrant_removes_owned_container_before_recreate(tmp_path: Path) -> None:
    env, log_path = make_fake_tools(tmp_path, existing=True, owned=True)

    result = run_helper("--namespace", "fc263", "reset-qdrant", env=env)

    assert result.returncode == 0, result.stderr
    log = log_path.read_text().splitlines()
    rm_index = next(i for i, line in enumerate(log) if line == "rm -f fc263_qdrant")
    run_index = next(
        i for i, line in enumerate(log) if line.startswith("run -d --name fc263_qdrant")
    )
    assert rm_index < run_index


def test_up_cleans_partial_pair_when_qdrant_health_fails(tmp_path: Path) -> None:
    env, log_path = make_fake_tools(tmp_path)
    env["FAKE_CURL_FAIL"] = "1"

    result = run_helper("--namespace", "fc263", "up", env=env)

    assert result.returncode != 0
    log = log_path.read_text()
    assert "rm -f fc263_qdrant" in log
    assert "rm -f fc263_pg" in log
