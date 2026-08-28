# Local AI agent validation contract

The repository has one normal validation vocabulary. Python **3.12** is the sole
validation runtime and `requirements-ci.txt` is the canonical toolchain
manifest. It pins exactly:

```text
pytest==9.1.1
ruff==0.16.5
pyrefly==1.2.0
```

Pyrefly is the sole authoritative Python static type checker. Do not introduce
Mypy, workflow-local tool pins, an alternate pytest authority, or another
runtime-version matrix.

## Central validation authority

Normal local, OpenCode, and GitHub Actions validation use the same repository
inputs:

```text
ci/test-profiles.toml
ci/impact-map.toml
requirements-ci.txt
scripts/ci_plan.py
scripts/run_ci_profile.py
scripts/ci_merge_gate.py
```

The profile vocabulary is:

```text
static
core
tooling
storage
acquisition
orchestration
controller
retrieval
assessment
migration
release
maintenance
```

Profile membership and service requirements are repository data, not agent
judgment. Exact node and filtered selectors remain part of that authority.
The impact map also owns architecture dependencies: acquisition changes retain
acquisition/orchestration/controller authority, orchestration/controller seams
retain controller authority, and direct controller entrypoints map explicitly
to the controller profile. Unknown or unmapped change impact is a validation
failure.

## Exact-head local sequence

Start from exact Git identity. For PR work, fetch the repository and bind both
base and candidate to exact 40-character commit SHAs. Do not substitute a
synthetic merge ref for the candidate head.

Install the canonical toolchain into the Python 3.12 validation environment:

```bash
python3.12 -m pip install -r requirements-ci.txt
python3.12 -m pip install --no-deps -e .
```

Authoritative profile execution validates the installed canonical package
boundary. Do not replace the editable package install with test-local import
path manipulation or a second packaging environment.

Generate the deterministic plan:

```bash
python scripts/ci_plan.py \
  --base-sha "$BASE_SHA" \
  --head-sha "$HEAD_SHA" \
  --event pull_request \
  --output ci-plan.json
```

The plan is authoritative for selected profile names, exact resolved/execution
membership, and declared services. If the planner reports an unknown path or
cannot validate the captured baseline/profile authority, stop with a failed or
blocked validation result rather than choosing profiles manually.

Run `static` once, then `core`, then every non-core profile selected by the
plan. Use a unique bounded namespace for each service-backed profile:

```bash
python scripts/run_ci_profile.py \
  --profile static \
  --base-sha "$BASE_SHA" \
  --head-sha "$HEAD_SHA" \
  --namespace local-static

python scripts/run_ci_profile.py \
  --profile core \
  --head-sha "$HEAD_SHA" \
  --namespace local-core

python scripts/run_ci_profile.py \
  --profile acquisition \
  --head-sha "$HEAD_SHA" \
  --namespace local-acquisition
```

The last command is illustrative: run it only when `acquisition` is selected.
Do not reconstruct the profile's pytest selector list or disposable service
commands outside the runner. The runner applies `--import-mode=importlib`,
creates JUnit/skip evidence, checks the configured skip allowlist, and owns
cleanup for services it starts.

For a comprehensive noncredentialed main-equivalent assessment, generate a
plan with `--event main`; that selects the full centralized profile set.
Credentialed release execution remains a separate manual exact-main authority
in `.github/workflows/release-campaign.yml`.

## Static authority

The `static` profile runs exactly once per candidate. Ruff 0.16.5 lint and
format checks are repository-wide, plus the explicitly owned extensionless
`scripts/fsearch_smart` entrypoint. Pyrefly remains a full-project check, with
the extensionless entrypoint checked explicitly as well. Domain/service
profiles must not duplicate global static authority.

Legacy `E402` import-bootstrap debt is not silently ignored. Its exact per-file
diagnostic counts are frozen in `ci/ruff-e402-debt.toml`. The runner executes
all other configured Ruff rules repository-wide, independently scans only
`E402`, and requires the observed path/count map to equal that reviewed
contract exactly. A new diagnostic, a stale entry, or a count change fails the
static profile and requires explicit source cleanup plus debt-contract review.
The contract accepts no globs. A changed-file-only Ruff pass or a broad
per-file-ignore is not equivalent to this authority.

Normal validation reads `pyrefly-baseline.json` and never updates it. Baseline
proposal generation is maintenance-only through the manually dispatched
`pyrefly-baseline.yml` workflow. A new Pyrefly diagnostic must be fixed or
explicitly reviewed; do not enlarge the baseline to make an unrelated change
pass.

## Deterministic host assessment

`scripts/local-agent-assessment` remains the specialized host-evidence runner
for exact-SHA assessment workflows that require its stronger control/candidate
separation, process-group lifecycle, isolated candidate-test execution, and
host-store audit. It is supplementary to the normal centralized CI profile
plan; it is not a second toolchain authority.

The host runner is Python 3.12-only and fingerprints `requirements-ci.txt` as
its toolchain input. It must not use per-version local-assessment locks or a
separate Ruff/Pyrefly/pytest pin set. Its typed result remains:

```text
HOST_EVIDENCE_RESULT=PASS|FAIL|BLOCKED|STALE|INFRA_ERROR|ISOLATION_BREACH
GATE_DECISION=NOT_EVALUATED
```

Any change to the host runner, PR bootstrap, profile, service helper,
`requirements-ci.txt`, Pyrefly policy, or baseline invalidates prior host
evidence governed by the old control fingerprint. Update the external OpenCode
operational guard to the reviewed fingerprints before using new host evidence.

For an intentional pre-merge control-plane transition, a fingerprint-pinned
reviewed bootstrap may exercise the candidate runner/toolchain as supplemental
transition evidence while exact `origin/main` remains the trusted comparison
source. Candidate PASS is not self-authenticating acceptance evidence. Central
must independently inspect the source/diff and exact-head CI evidence.

## Service and skip invariants

PostgreSQL and Qdrant validation use the repository-sanctioned disposable
service helper through the profile runner. Profiles declare Valkey and fresh
migration-database requirements explicitly. Never point reset/destructive tests
at persistent user or production services.

A profile that produces pytest skips must pass the configured skip-classifier
authority. Do not convert failures to skips, expected failures, looser
assertions, or baseline/suppression changes to obtain green status.

## Merge policy

The issue #332 transition is complete. The effective `main` ruleset requires the
aggregate `Merge gate` context. `CI` still emits `Pyrefly` as its internal static
profile result, and `Merge gate` fails closed unless plan, static, core, and every
selected profile succeed on the exact candidate head. Repository policy must not
be changed back to requiring `Pyrefly` alone, because that would make runtime and
service-backed profile failures nonblocking.

## Handoff evidence

Return at minimum:

```text
BASE_SHA=<40-char SHA>
HEAD_SHA=<40-char SHA>
TOOLCHAIN_MANIFEST=requirements-ci.txt
RUNTIME_VERSION=3.12
VALIDATION_PLAN=ci-plan.json
SELECTED_PROFILES=<exact ordered names>
STATIC_RESULT=<PASS|FAIL|BLOCKED>
CORE_RESULT=<PASS|FAIL|BLOCKED>
SELECTED_PROFILE_RESULTS=<per-profile result>
SERVICE_CLEANUP=<PASS|FAIL|not-required>
```

Also return the complete ACMR changed-file list, exact commands actually run,
skip/service evidence, and every omitted check with its reason. A failure that
requires semantic production or control-plane changes is evidence to return to
Central; it is not permission to weaken validation.
