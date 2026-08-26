# Deterministic local host assessment

`scripts/local-agent-assessment` creates host-bound runtime evidence for an
exact Git commit without asking an LLM to construct worktrees, virtual
environments, service commands, test batches, or cleanup operations.

The result is deliberately named `HOST_EVIDENCE_RESULT`. It is not the
architectural gate decision. Every report contains:

```text
HOST_EVIDENCE_RESULT=PASS|FAIL|BLOCKED|STALE|INFRA_ERROR|ISOLATION_BREACH
GATE_DECISION=NOT_EVALUATED
```

Central review remains responsible for deciding whether the selected profile
covers the gate and whether source semantics satisfy its acceptance criteria.

## Trust boundary

Run the controller from a clean, trusted installation pinned by the host
operational guard. The candidate worktree never supplies orchestration code,
profiles, dependency locks, the disposable-service helper, or skip policy.
Those files come from the controlling checkout and their SHA-256 fingerprints
are recorded in `assessment.json`:

- `scripts/local_agent_assessment.py` and its thin executable shim;
- `scripts/disposable-test-services`;
- `references/local-agent-assessment-profiles.toml`; and
- the Python 3.11 and 3.12 hashed dependency locks.

Schema v1 is intentionally a **trusted-ref-only** runner. Candidate Python and
pytest configuration execute as the host user, so a profile must allowlist a
remote-tracking ref and `--expected-ref` must name that ref at the requested
SHA. This is appropriate for exact-main host gates; it is not an OS sandbox
for unreviewed pull-request code. Supporting untrusted candidate commits
requires a separately reviewed container/VM sandbox profile.

OpenCode operational-schema v5.13 exposes one harness-owned gateway:

```text
/home/filip/.config/opencode/plugins/operational-schema-v5/scripts/local-agent-assessment.mjs --sha <40-lowercase-hex> --assessment-id <bounded-id>
```

The gateway accepts no repository, profile, ref, workspace, lifecycle, or
command-tail overrides. It verifies the reviewed SHA-256 fingerprints of the
six control-plane files listed above, strips guard/root override variables,
and supplies the fixed `phase1-control-policy`, canonical `origin/main`, fresh
fetch, and sanctioned `/tmp/opencode/verify` arguments. Direct and RTK forms
of only that grammar are allowlisted for Verify. A runner or profile found
inside a candidate worktree is not trusted evidence.

Any change to a fingerprinted control-plane file invalidates prior gateway
fingerprints and prior host-assessment evidence for purposes of a new review.
The operational guard must be updated to the newly reviewed fingerprints
before the gateway is used again. Historical PASS evidence from an older
runner/profile/helper/lock fingerprint must never be presented as evidence for
the changed control plane.

## Invocation

First inspect the immutable plan. This performs Git/object/profile validation
but creates no worktree, environments, services, or result directory:

```bash
scripts/local-agent-assessment plan \
  --repo /path/to/firecrawl_skill \
  --sha 39601ab2df0c78c389346d3f3d5ae85eab54cb84 \
  --profile phase1-control-policy \
  --expected-ref origin/main \
  --fetch
```

Then run the bounded assessment:

```bash
scripts/local-agent-assessment run \
  --repo /path/to/firecrawl_skill \
  --sha 39601ab2df0c78c389346d3f3d5ae85eab54cb84 \
  --profile phase1-control-policy \
  --assessment-id gate312-39601ab2 \
  --expected-ref origin/main \
  --fetch
```

Trusted-ref-only profiles require `--fetch`. They bind `origin` to the exact
canonical repository URL encoded in the profile, fetch before and after
validation, and return `STALE` if the expected ref does not equal the requested
SHA or moves during the assessment.

## Deterministic lifecycle

The runner owns the following sequence:

1. Validate the 40-character commit, named profile, exact test paths, trusted
   control-plane files, sanctioned root, and single-host lifecycle lock.
2. Create a real detached Git worktree directly under
   `/tmp/opencode/verify/worktrees/<assessment-id>`.
3. Create Python 3.11 under `materials` and Python 3.12 at the repository's
   canonical ignored `<worktree>/.venv-research-store` path, synchronized from
   platform-specific hashed locks with `uv pip sync --require-hashes`. This
   lets repository-pinned Pyrefly discover the intended interpreter instead of
   ambient host Python.
4. Build a minimal subprocess environment. It does not copy the host
   environment and gives HOME, XDG data/cache, TMPDIR, and BLOB_ROOT isolated
   assessment paths.
5. Allocate a free loopback port pair while holding the lifecycle lock, start
   the trusted disposable-service helper, and parse its strict JSON contract.
6. Run Ruff, Ruff format, Pyrefly, and all profile pytest groups as direct argv
   arrays with `shell=False`. Every pytest group emits JUnit XML.
7. Recreate Qdrant when the profile requires reset proof and check `/readyz`.
8. Prove final SHA, tracked/untracked status, whitespace state, and optional
   expected-ref freshness.
9. Tear down owned services, remove the Git worktree through Git, remove
   materials, and retain only redacted logs plus typed evidence.

Every runner-owned external command executes in a dedicated POSIX process
session. Recorded validation commands use the profile command timeout; Git and
other control-plane identity commands use a fixed bounded control timeout.
When a command times out, the runner terminates the **entire process group**,
waits for it, escalates to `SIGKILL` after the bounded termination grace period
when necessary, and only then proceeds toward cleanup. A timed-out recorded
command is represented with return code `124` and `timed_out=true` in typed
command evidence. Descendants are not permitted to outlive a supposedly
completed command and continue using disposable services or filesystem state.

Before every state-changing boundary the runner atomically updates
`results/<assessment-id>/lifecycle.json`. If the process is killed or the host
restarts, recover only that recorded namespace and worktree:

```bash
scripts/local-agent-assessment recover \
  --repo /path/to/firecrawl_skill \
  --assessment-id gate312-39601ab2
```

Recovery validates the recorded identity and paths, then acquires the same host
lifecycle lock **before creating recovery HOME/TMP/XDG/material state**. A
recovery attempt that is refused because another assessment is active must be
side-effect free with respect to that assessment's worktree/material namespace.
After lock acquisition, recovery rejects path escapes and symlink redirection,
invokes the ownership-checking trusted helper, removes only the registered
assessment worktree/materials, and retains the journal and evidence directory.

The host-wide lifecycle lock intentionally serializes assessments. This is a
correctness boundary around port allocation and host default-store auditing,
not a throughput optimization.

## Isolation

Only these profile environment keys are accepted:

```text
EMBEDDING_MODEL
EMBEDDING_REVISION
EMBEDDING_DIMENSION
FIRECRAWL_RELEASE_DETERMINISTIC_FIXTURES
```

PostgreSQL and Qdrant variables come only from the helper JSON contract.
`DATABASE_URL` is derived from the disposable PostgreSQL URL. `BLOB_ROOT` is
always runner-owned. Production credentials, provider keys, proxy variables,
host database URLs, and the caller's HOME/XDG paths are not inherited.

The runner content-hashes and type-checks the normal host default blob
directory before and after the assessment. Any creation, modification,
replacement, symlink change, or deletion makes the terminal result
`ISOLATION_BREACH`, even if every test passed. Cleanup failure similarly
prevents PASS.

## Evidence and statuses

Results are retained under `/tmp/opencode/verify/results/<assessment-id>`:

```text
assessment.json
assessment.md
pytest-<group>-py<version>.xml
logs/<command>.stdout.log
logs/<command>.stderr.log
```

`assessment.json` records command argv, exit status, timeout disposition,
duration, log and JUnit hashes, exact expected/observed JUnit counts and skip
reasons, tested/ref identity, tool/interpreter versions, control-plane
fingerprints, anomalies, and cleanup proof. PostgreSQL passwords are redacted
from retained output.

Status meanings:

- `PASS`: every encoded host check passed, no unexpected skip, identity stayed
  exact, isolation held, and cleanup completed.
- `FAIL`: a static or test authority failed, including a recorded validation
  command that timed out after its process group was terminated.
- `BLOCKED`: prerequisites, locks, profile inputs, dependencies, disposable
  services, or bounded control-plane operations prevented assessment.
- `STALE`: candidate or expected-ref identity was not stable.
- `INFRA_ERROR`: lifecycle, reset, evidence, or cleanup infrastructure failed.
- `ISOLATION_BREACH`: an assessment wrote to the host default blob store.

On any result other than PASS, an LLM may inspect only the emitted failure
bundle and source directly implicated by it. It must not reconstruct worktree,
environment, service, test, reset, or cleanup commands manually.

## Exact-head handoff evidence

A filesystem path on the host is not independently reviewable evidence by
itself. After any Central change to the runner, helper, profile, shim, or lock
files, the local evidence collector must first update the external operational
guard to the reviewed fingerprints and then perform a **fresh** gateway run
against the then-current authoritative `origin/main` SHA.

Return to Central, at minimum:

- exact authoritative main SHA and `git rev-parse HEAD` identity;
- assessment ID and disposable-service namespace;
- `HOST_EVIDENCE_RESULT` and `GATE_DECISION`;
- the control-plane fingerprints recorded by the new runner;
- exact per-group JUnit expected/observed counts and skip details;
- Ruff, Ruff-format, and Pyrefly command outcomes;
- expected-ref start/end identity;
- Qdrant reset and `/readyz` proof;
- host-default blob-store isolation result;
- service/worktree/material cleanup proof; and
- `assessment.json` SHA-256 plus either its complete bounded content or another
  Central-accessible evidence representation.

Do not reuse the earlier Gate #312 assessment after the runner fingerprint has
changed. A new exact-main assessment is acceptance evidence for the new control
plane; the historical assessment remains rationale only.

## Profile maintenance

Profiles are declarative test selectors and exact expected test counts, never
arbitrary commands. Named falsification nodes remain explicit; marker-only
membership is insufficient for gate-critical behavior. Contract tests require
every path and named node to exist. Schema v1 permits exactly zero skips; a
future nonzero-skip profile must add deterministic allowlist verification as a
new schema contract. Any runner, shim, helper, profile, or dependency-lock
change alters the trusted control fingerprint and therefore requires Central
review plus an operational-guard fingerprint update before host evidence is
accepted.

Regenerate dependency locks deliberately:

```bash
uv pip compile requirements-local-agent-assessment.in \
  --generate-hashes --python-version 3.11 --python-platform linux \
  -o requirements-local-agent-assessment-py311.lock
uv pip compile requirements-local-agent-assessment.in \
  --generate-hashes --python-version 3.12 --python-platform linux \
  -o requirements-local-agent-assessment-py312.lock
```
