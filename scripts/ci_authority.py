"""Central deterministic CI/test authority for issue #332.

The pre-refactor Git object named by ``ci/pre-refactor-baseline.toml`` is the
immutable coverage source. Current profiles assign every historical selector
one canonical owner and may add explicit selectors for post-baseline tests.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import shlex
import subprocess
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BASELINE_PATH = Path("ci/pre-refactor-baseline.toml")
PROFILES_PATH = Path("ci/test-profiles.toml")
IMPACT_PATH = Path("ci/impact-map.toml")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
TEST_TOKEN_RE = re.compile(
    r"^tests/(?:unit|integration|contract|acceptance)/"
    r"[A-Za-z0-9_./-]+\.py(?:::[A-Za-z0-9_\[\]./-]+)*$"
)
PYTEST_LINE_RE = re.compile(
    r"^\s*(?:python(?:3(?:\.\d+)?)?\s+-m\s+pytest|pytest)\s+(?P<args>.+?)\s*$"
)
ALLOWED_SERVICES = {"postgres", "qdrant", "valkey", "fresh-migration-db"}
REQUIRED_PROFILES = (
    "static",
    "core",
    "tooling",
    "storage",
    "acquisition",
    "orchestration",
    "controller",
    "retrieval",
    "assessment",
    "migration",
    "release",
    "maintenance",
)
DELEGATED_SELECTOR_MANIFESTS = {
    "scripts/audit_release_gate_matrix.py": "references/audit-remediation-release-gates.json",
}


class AuthorityError(RuntimeError):
    pass


@dataclass(frozen=True, order=True)
class Selector:
    expression: str
    path: str
    keyword: str | None = None

    @property
    def base_path(self) -> str:
        return self.path.split("::", 1)[0]

    @property
    def is_full_file(self) -> bool:
        return "::" not in self.path and self.keyword is None

    def argv(self) -> list[str]:
        argv = [self.path]
        if self.keyword is not None:
            argv.extend(["-k", self.keyword])
        return argv


@dataclass(frozen=True)
class Profile:
    name: str
    kind: str
    services: tuple[str, ...]
    ownership_tokens: tuple[str, ...]
    selectors: tuple[Selector, ...]


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        text=True,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise AuthorityError(
            f"git {' '.join(args)} failed ({completed.returncode}): "
            f"{completed.stderr.strip()}"
        )
    return completed.stdout


def require_sha(value: str, label: str) -> str:
    if not SHA_RE.fullmatch(value):
        raise AuthorityError(f"{label} must be a lowercase 40-character SHA")
    return value


def git_show(repo: Path, commit: str, path: str) -> str:
    require_sha(commit, "commit")
    return _git(repo, "show", f"{commit}:{path}")


def git_file_exists(repo: Path, commit: str, path: str) -> bool:
    completed = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-e", f"{commit}:{path}"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return completed.returncode == 0


def load_toml(repo: Path, relative: Path) -> dict[str, Any]:
    return tomllib.loads((repo / relative).read_text(encoding="utf-8"))


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _triggers(text: str) -> list[str]:
    result = []
    for name in ("pull_request", "push", "workflow_dispatch"):
        if re.search(rf"(?m)^\s{{0,2}}{re.escape(name)}\s*:", text):
            result.append(name)
    return result


def _services(text: str) -> list[str]:
    lower = text.lower()
    services: list[str] = []
    if "postgres" in lower or "research_store_test_database_url" in lower:
        services.append("postgres")
    if "qdrant" in lower:
        services.append("qdrant")
    if "valkey" in lower or "valkey_url" in lower:
        services.append("valkey")
    if "migrate(" in lower or " alembic " in lower or "migration" in lower:
        services.append("fresh-migration-db")
    return services


def _extract_pytest_selectors(text: str) -> list[Selector]:
    logical = re.sub(r"\\\s*\n\s*", " ", text)
    selectors: set[Selector] = set()
    for line in logical.splitlines():
        match = PYTEST_LINE_RE.match(line)
        if not match:
            continue
        try:
            tokens = shlex.split(match.group("args"), posix=True)
        except ValueError as exc:
            raise AuthorityError(f"cannot parse pytest command: {line.strip()}: {exc}") from exc
        keyword: str | None = None
        for index, token in enumerate(tokens):
            if token == "-k" and index + 1 < len(tokens):
                keyword = tokens[index + 1]
            elif token.startswith("-k="):
                keyword = token[3:]
        for token in tokens:
            cleaned = token.rstrip(";,)")
            if not TEST_TOKEN_RE.fullmatch(cleaned):
                continue
            expression = cleaned
            if keyword is not None:
                expression = f"{cleaned} -k {json.dumps(keyword)}"
            selectors.add(Selector(expression=expression, path=cleaned, keyword=keyword))
    return sorted(selectors)


def _delegated_pytest_selectors(
    repo: Path, commit: str, workflow_text: str
) -> tuple[list[Selector], list[str]]:
    selectors: set[Selector] = set()
    manifests: list[str] = []
    for marker, manifest_path in DELEGATED_SELECTOR_MANIFESTS.items():
        if marker not in workflow_text:
            continue
        raw = json.loads(git_show(repo, commit, manifest_path))
        gates = raw.get("gates") if isinstance(raw, dict) else None
        if not isinstance(gates, list):
            raise AuthorityError(
                f"delegated selector manifest is malformed: {manifest_path}"
            )
        manifests.append(manifest_path)
        for gate in gates:
            if not isinstance(gate, dict):
                raise AuthorityError(
                    f"delegated selector manifest gate is malformed: {manifest_path}"
                )
            command = gate.get("command")
            if not isinstance(command, str):
                raise AuthorityError(
                    f"delegated selector manifest command is malformed: {manifest_path}"
                )
            selectors.update(_extract_pytest_selectors(command))
    return sorted(selectors), sorted(manifests)


def build_baseline(repo: Path, baseline_sha: str | None = None) -> dict[str, Any]:
    config = load_toml(repo, BASELINE_PATH)
    sha = require_sha(
        baseline_sha or str(config["implementation_base_sha"]),
        "implementation base SHA",
    )
    workflow_paths = tuple(str(path) for path in config["workflow_paths"])
    actual_paths = tuple(
        path
        for path in _git(repo, "ls-tree", "-r", "--name-only", sha, "--", ".github/workflows")
        .splitlines()
        if path.endswith((".yml", ".yaml"))
    )
    if tuple(sorted(actual_paths)) != tuple(sorted(workflow_paths)):
        raise AuthorityError(
            "pre-refactor workflow inventory does not match exact implementation base"
        )

    selector_sources: dict[str, dict[str, Any]] = {}
    workflows: list[dict[str, Any]] = []
    for path in sorted(workflow_paths):
        text = git_show(repo, sha, path)
        direct_selectors = _extract_pytest_selectors(text)
        delegated_selectors, selector_manifests = _delegated_pytest_selectors(
            repo, sha, text
        )
        workflow_selectors = sorted({*direct_selectors, *delegated_selectors})
        workflows.append(
            {
                "path": path,
                "triggers": _triggers(text),
                "services": _services(text),
                "selector_manifests": selector_manifests,
                "selectors": [selector.expression for selector in workflow_selectors],
            }
        )
        for selector in workflow_selectors:
            item = selector_sources.setdefault(
                selector.expression,
                {
                    "expression": selector.expression,
                    "path": selector.path,
                    "keyword": selector.keyword,
                    "workflows": [],
                },
            )
            item["workflows"].append(path)

    selectors = [selector_sources[key] for key in sorted(selector_sources)]
    test_files = sorted({item["path"].split("::", 1)[0] for item in selectors})
    result = {
        "schema_version": "ci-pre-refactor-baseline-v1",
        "implementation_base_sha": sha,
        "workflow_count": len(workflows),
        "test_file_count": len(test_files),
        "selector_count": len(selectors),
        "workflows": workflows,
        "test_files": test_files,
        "selectors": selectors,
    }
    result["sha256"] = hashlib.sha256(_canonical_json(result)).hexdigest()
    return result


def parse_selector(expression: str) -> Selector:
    tokens = shlex.split(expression)
    if not tokens or not TEST_TOKEN_RE.fullmatch(tokens[0]):
        raise AuthorityError(f"invalid profile selector: {expression}")
    keyword: str | None = None
    if len(tokens) == 3 and tokens[1] == "-k":
        keyword = tokens[2]
    elif len(tokens) != 1:
        raise AuthorityError(f"unsupported profile selector grammar: {expression}")
    canonical = tokens[0]
    if keyword is not None:
        canonical = f"{tokens[0]} -k {json.dumps(keyword)}"
    return Selector(canonical, tokens[0], keyword)


def load_profiles(repo: Path) -> tuple[dict[str, Profile], tuple[str, ...], str]:
    data = load_toml(repo, PROFILES_PATH)
    if data.get("schema_version") != 1:
        raise AuthorityError("unsupported CI profile schema")
    if data.get("python_version") != "3.12":
        raise AuthorityError("CI profile authority must use Python 3.12 only")
    order = tuple(str(name) for name in data.get("ownership_order", []))
    raw_profiles = data.get("profiles")
    if not isinstance(raw_profiles, dict):
        raise AuthorityError("profiles table is missing")
    if tuple(raw_profiles) != REQUIRED_PROFILES:
        raise AuthorityError(
            f"profile order/names must be exactly {list(REQUIRED_PROFILES)}"
        )
    if set(order) != set(REQUIRED_PROFILES) - {"static", "core"}:
        raise AuthorityError("ownership_order must cover every non-static/non-core profile")
    profiles: dict[str, Profile] = {}
    for name in REQUIRED_PROFILES:
        raw = raw_profiles[name]
        kind = str(raw.get("kind", "pytest"))
        if kind not in {"static", "pytest"}:
            raise AuthorityError(f"unsupported profile kind for {name}: {kind}")
        services = tuple(str(value) for value in raw.get("services", []))
        if len(services) != len(set(services)) or set(services) - ALLOWED_SERVICES:
            raise AuthorityError(f"invalid services for profile {name}: {services}")
        ownership_tokens = tuple(str(value).lower() for value in raw.get("ownership_tokens", []))
        selectors = tuple(parse_selector(str(value)) for value in raw.get("selectors", []))
        profiles[name] = Profile(name, kind, services, ownership_tokens, selectors)
    return profiles, order, str(data.get("skip_allowlist", ""))


def owner_for_test_path(path: str, profiles: Mapping[str, Profile], order: Sequence[str]) -> str:
    explicit_owners = {
        name
        for name, profile in profiles.items()
        if any(selector.base_path == path for selector in profile.selectors)
    }
    if len(explicit_owners) > 1:
        raise AuthorityError(
            f"test path has multiple explicit profile owners: {path}: {sorted(explicit_owners)}"
        )
    if explicit_owners:
        return explicit_owners.pop()

    lower = path.lower()
    for name in order:
        if any(token in lower for token in profiles[name].ownership_tokens):
            return name
    return "core"


def resolved_membership(
    repo: Path,
    *,
    baseline_sha: str | None = None,
    head_sha: str | None = None,
) -> tuple[dict[str, list[Selector]], dict[str, Any]]:
    profiles, order, _ = load_profiles(repo)
    baseline = build_baseline(repo, baseline_sha)
    membership: dict[str, dict[str, Selector]] = {name: {} for name in profiles}
    for item in baseline["selectors"]:
        selector = Selector(
            expression=str(item["expression"]),
            path=str(item["path"]),
            keyword=item["keyword"],
        )
        owner = owner_for_test_path(selector.base_path, profiles, order)
        membership[owner][selector.expression] = selector
    explicit_owners: dict[str, str] = {}
    for name, profile in profiles.items():
        for selector in profile.selectors:
            previous = explicit_owners.setdefault(selector.expression, name)
            if previous != name:
                raise AuthorityError(
                    f"explicit selector {selector.expression} has multiple owners: "
                    f"{previous}, {name}"
                )
            for owned in membership.values():
                owned.pop(selector.expression, None)
            membership[name][selector.expression] = selector

    target = head_sha
    if target is not None:
        require_sha(target, "head SHA")
        baseline_files = set(baseline["test_files"])
        for path in baseline_files:
            if not git_file_exists(repo, target, path):
                raise AuthorityError(f"baseline-selected test was removed: {path}")
        added = _git(
            repo,
            "diff",
            "--name-only",
            "--diff-filter=A",
            baseline["implementation_base_sha"],
            target,
            "--",
            "tests",
        ).splitlines()
        explicit_paths = {
            selector.base_path
            for profile in profiles.values()
            for selector in profile.selectors
        }
        uncovered = sorted(
            path
            for path in added
            if path.endswith(".py")
            and Path(path).name.startswith("test_")
            and path not in explicit_paths
        )
        if uncovered:
            raise AuthorityError(
                "post-baseline tests require explicit profile membership: "
                + ", ".join(uncovered)
            )

    resolved = {
        name: sorted(items.values(), key=lambda item: item.expression)
        for name, items in membership.items()
    }
    seen: dict[str, str] = {}
    for name, selectors in resolved.items():
        for selector in selectors:
            previous = seen.setdefault(selector.expression, name)
            if previous != name:
                raise AuthorityError(
                    f"selector has multiple canonical owners: {selector.expression}"
                )
    if len(seen) < baseline["selector_count"]:
        raise AuthorityError("resolved membership lost baseline selectors")
    return resolved, baseline


def execution_selectors(selectors: Iterable[Selector]) -> list[Selector]:
    by_file: dict[str, list[Selector]] = {}
    for selector in selectors:
        by_file.setdefault(selector.base_path, []).append(selector)
    result: list[Selector] = []
    for path in sorted(by_file):
        choices = sorted(by_file[path], key=lambda item: item.expression)
        full = next((item for item in choices if item.is_full_file), None)
        if full is not None:
            result.append(full)
        else:
            result.extend(choices)
    return result


def load_impact(repo: Path) -> dict[str, Any]:
    data = load_toml(repo, IMPACT_PATH)
    if data.get("schema_version") != 1:
        raise AuthorityError("unsupported impact-map schema")
    if data.get("unknown_path_policy") != "fail":
        raise AuthorityError("unknown_path_policy must be fail")
    return data


def plan_changed_paths(
    repo: Path,
    changed_paths: Sequence[str],
) -> tuple[list[str], list[str]]:
    profiles, order, _ = load_profiles(repo)
    impact = load_impact(repo)
    selected = {str(value) for value in impact.get("always_profiles", [])}
    unknown: list[str] = []
    rules = impact.get("rules", [])
    if not isinstance(rules, list):
        raise AuthorityError("impact-map rules must be an array")
    for path in changed_paths:
        matched = False
        if path.startswith("tests/") and path.endswith(".py"):
            selected.add(owner_for_test_path(path, profiles, order))
            matched = True
        for rule in rules:
            pattern = str(rule["pattern"])
            if fnmatch.fnmatchcase(path, pattern):
                selected.update(str(name) for name in rule["profiles"])
                matched = True
        if not matched:
            unknown.append(path)
    invalid = selected - set(profiles)
    if invalid:
        raise AuthorityError(f"impact map selected unknown profiles: {sorted(invalid)}")
    return sorted(selected, key=REQUIRED_PROFILES.index), sorted(unknown)


def changed_paths(repo: Path, base_sha: str, head_sha: str) -> list[str]:
    require_sha(base_sha, "base SHA")
    require_sha(head_sha, "head SHA")
    return sorted(
        path
        for path in _git(
            repo,
            "diff",
            "--name-only",
            "--diff-filter=ACMRD",
            base_sha,
            head_sha,
        ).splitlines()
        if path
    )


def validate_authority(repo: Path, *, head_sha: str | None = None) -> dict[str, Any]:
    baseline_cfg = load_toml(repo, BASELINE_PATH)
    baseline = build_baseline(repo)
    expected = {
        "workflow_count": int(baseline_cfg["workflow_count"]),
        "test_file_count": int(baseline_cfg["test_file_count"]),
        "selector_count": int(baseline_cfg["selector_count"]),
    }
    observed = {key: int(baseline[key]) for key in expected}
    if observed != expected:
        raise AuthorityError(f"baseline count drift: expected={expected} observed={observed}")
    expected_digest = str(baseline_cfg.get("canonical_sha256", "")).strip()
    if expected_digest and expected_digest != baseline["sha256"]:
        raise AuthorityError(
            f"baseline digest drift: expected={expected_digest} observed={baseline['sha256']}"
        )
    membership, _ = resolved_membership(repo, head_sha=head_sha)
    profiles, _, skip_allowlist = load_profiles(repo)
    if not skip_allowlist or not (repo / skip_allowlist).is_file():
        raise AuthorityError("skip allowlist authority is missing")
    return {
        "baseline": baseline,
        "membership": {
            name: [selector.expression for selector in selectors]
            for name, selectors in membership.items()
        },
        "execution_membership": {
            name: [selector.expression for selector in execution_selectors(selectors)]
            for name, selectors in membership.items()
        },
        "profile_services": {name: list(profile.services) for name, profile in profiles.items()},
    }
