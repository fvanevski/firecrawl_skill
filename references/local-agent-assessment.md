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
- `references/local-agent-assessment-profiles.toml`;
- `pyproject.toml` and `pyrefly-baseline.json`, which define the trusted static
  analysis policy; and
- the Python 3.11 and 3.12 hashed dependency locks.

Schema v1 has three explicit trust dispositions:

1. **`trusted-ref`** — the original exact-main/gate mode. A profile allowlists a
   remote-tracking ref and `--expected-ref` must name that ref at the requested
   SHA. Its command path and expected-count behavior are unchanged.
2. **`pr-head`** — a reviewed repository PR candidate. The controlling checkout
   must be a clean, freshly fetched exact `origin/main`; the candidate is bound
   only through canonical `refs/pull/<PR_NUMBER>/head` and a caller-supplied
   exact SHA. The PR worktree supplies source and tests, never orchestration or
   acceptance policy.
3. **hostile/untrusted or arbitrary fork code** — unsupported. `pr-head` is not
   an OS sandbox. General untrusted execution requires a separately reviewed
   container/VM isolation profile and is outside this runner contract.

The OpenCode operational gateway for this runner revision has exactly two
bounded identity grammars:

```text
# Existing trusted-ref/main form
/home/filip/.config/opencode/plugins/operational-schema-v5/scripts/local-agent-assessment.mjs --sha <40-lowercase-hex> --assessment-id <bounded-id>

# Reviewed repository PR form
/home/filip/.config/opencode/plugins/operational-schema-v5/scripts/local-agent-assessment.mjs --pr <positive-pr-number> --sha <40-lowercase-hex> --assessment-id <bounded-id>
```

The gateway accepts no repository, profile, ref, target-kind, workspace,
lifecycle, service, or command-tail overrides. The no-`--pr` grammar supplies
`trusted-ref`, canonical `origin/main`, and fresh fetch. The `--pr` grammar
supplies `pr-head`, the bounded PR number, and fresh fetch. Both supply the
fixed `phase1-control-policy` and sanctioned `/tmp/opencode/verify` root. The
external operational guard must verify the reviewed SHA-256 fingerprints of
all eight control-plane files listed above. Direct and RTK forms of only these
two grammars may be allowlisted for Verify. A runner, profile, lock, helper,
static-analysis policy, or baseline found inside a candidate worktree is not
trusted evidence.

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

Trusted-ref mode requires `--fetch`. It binds `origin` to the exact canonical
repository URL encoded in the profile, fetches before and after validation, and
returns `STALE` if the expected ref does not equal the requested SHA or moves
during the assessment.

For an explicitly reviewed repository PR, use the orthogonal PR target:

```bash
scripts/local-agent-assessment plan \
  --repo /path/to/firecrawl_skill \
  --sha <exact-pr-head-sha> \
  --profile phase1-control-policy \
  --target-kind pr-head \
  --pr <PR_NUMBER> \
  --fetch

scripts/local-agent-assessment run \
  --repo /path/to/firecrawl_skill \
  --sha <exact-pr-head-sha> \
  --profile phase1-control-policy \
  --target-kind pr-head \
  --pr <PR_NUMBER> \
  --assessment-id pr<PR_NUMBER>-<sha-prefix> \
  --fetch
```

PR mode rejects `--expected-ref` and arbitrary branch names. Before candidate
execution it requires the controlling checkout itself to be clean and exactly
at freshly fetched `origin/main`, fetches canonical
`refs/pull/<PR_NUMBER>/head`, and requires that ref to equal the requested SHA.
After validation it independently refreshes both `origin/main` and the PR ref;
movement of either identity yields `STALE`.

### PR-review test authority

PR mode does not accept candidate commands, candidate expected counts, or
candidate pytest configuration as policy. Trusted regression implementation is
control-owned as well as trusted regression membership: before candidate test
execution, every repository path referenced by a trusted profile selector is
resolved at both the recorded control SHA and exact candidate SHA and must be
byte-identical. A reviewed PR that modifies one of those profile-selected test
files is `BLOCKED`; such a change requires separate control-plane review rather
than being allowed to redefine the assertions that constitute trusted
regressions. Candidate-added or otherwise changed tests outside the trusted
profile remain supplemental evidence under the bounded candidate-test rule.

Before candidate test execution the runner:

1. collects each trusted profile group from the controlling `origin/main`
   checkout using the profile selectors and requires the collection count to
   equal the trusted expected count;
2. computes the PR-side diff from the Git merge base to the exact candidate
   SHA, retaining only added/modified/renamed `test_*.py` modules beneath the
   configured test roots, with a hard file-count bound;
3. protects every auto-loaded `conftest.py` ancestor from repository root
   through each configured candidate test root and blocks any added, modified,
   deleted, or renamed `conftest.py` beneath those roots before pytest starts;
4. collects changed candidate modules with fixed runner-owned pytest arguments
   (`-c /dev/null`, fixed rootdir/import mode, no cache provider), sorts the
   exact node IDs, and enforces the configured node-count bound;
5. records `candidate_test_manifest` with rule name, merge-base SHA, exact file
   list, exact node-ID list, and SHA-256 of the canonical manifest; and
6. executes both trusted regressions and changed candidate regressions only by
   the exact collected node IDs, retaining JUnit and zero-skip enforcement.

Candidate collection is additionally proved complete and unfiltered. Obvious
changed-module `pytest_plugins` declarations are rejected during preflight as a
defense-in-depth diagnostic, but that static scan is not the authority
boundary. Every PR-mode pytest process that executes in the candidate worktree
uses a trusted runner launcher whenever changed candidate test modules exist.
The launcher starts Python with `-P`, imports the pinned pytest installation
before adding the candidate repository root to `sys.path`, and wraps pytest's
test-module plugin registration so changed candidate test modules cannot
register `pytest_plugins` dynamically. Unchanged trusted test modules retain
normal pytest module-plugin processing, so existing control-owned test support
continues to work. The same changed-module guard is active during candidate
collection and exact-node execution, including trusted profile groups executed
against the candidate worktree.

Candidate collection writes JUnit evidence, and collection-time skips, errors,
or failures invalidate the candidate collection. The runner also requires
pytest's reported collected count to equal the exact accepted node-ID count, so
a collection hook cannot silently deselect an item and present the reduced list
as authoritative. Missing or ambiguous collection-summary evidence fails with
the caller's candidate collection status rather than being reclassified as a
control-plane prerequisite failure. These controls prevent the identified
pytest authority substitutions; they do not turn reviewed PR execution into a
general hostile-code sandbox.

Ruff runs with `--isolated` in PR mode. Pyrefly is explicitly pointed at the
candidate `pyproject.toml`, but the candidate `pyproject.toml` and
`pyrefly-baseline.json` Git blobs must be byte-identical to the trusted control
copies before execution. A PR that changes those static-analysis authority
files is therefore `BLOCKED` for this host-evidence mode and requires separate
Central review of the control-plane change.

## Deterministic lifecycle

The runner owns the following sequence:

1. Validate the 40-character commit, named profile, target grammar, exact test
   paths, trusted control-plane files, sanctioned root, and single-host
   lifecycle lock. PR mode additionally binds the clean control checkout to
   exact `origin/main` and the candidate to canonical `refs/pull/<PR_NUMBER>/head`.
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
8. Prove final SHA, tracked/untracked status, whitespace state, and target
   freshness. Trusted-ref mode rechecks its expected ref; PR mode independently
   rechecks both `origin/main` and canonical PR-head identity and also rechecks
   that the trusted control checkout itself remains at the recorded SHA and
   clean through completion.
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
reasons, tool/interpreter versions, control-plane fingerprints, anomalies, and
cleanup proof. Identity fields include `target_kind`, `pr_number`,
`requested_sha`, `tested_sha`, `pr_head_start`, `pr_head_end`, `control_sha`,
`control_ref_start`, and `control_ref_end` where applicable. PR mode also
records the complete hashed `candidate_test_manifest`. PostgreSQL passwords are
redacted from retained output.

Status meanings:

- `PASS`: every encoded host check passed, no unexpected skip, identity stayed
  exact, isolation held, and cleanup completed.
- `FAIL`: a static or test authority failed, including candidate collection
  completeness failures and a recorded validation command that timed out after
  its process group was terminated.
- `BLOCKED`: prerequisites, locks, profile inputs, dependencies, disposable
  services, or bounded control-plane operations prevented assessment. PR-mode
  control-authority substitutions, including a changed trusted-profile test
  implementation or protected pytest control file, are also `BLOCKED`.
- `STALE`: candidate, expected-ref, PR-head, or trusted-control identity was not
  stable.
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

- exact authoritative main/control SHA and `git rev-parse HEAD` identity;
- target kind and, for PR mode, PR number plus requested/tested and PR-head
  start/end SHAs;
- assessment ID and disposable-service namespace;
- `HOST_EVIDENCE_RESULT` and `GATE_DECISION`;
- the control-plane fingerprints recorded by the new runner;
- the exact `candidate_test_manifest` and its SHA-256 for PR mode;
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
every path and named node to exist. In PR mode, those profile-selected test
implementations are also control-owned and must remain byte-identical at the
candidate SHA; candidate regression evidence is additive rather than a way to
rewrite trusted assertions. PR policy additionally fixes the candidate test
Python, allowed test roots, and hard file/node bounds; the candidate never
supplies these values. Schema v1 permits exactly zero skips; a future
nonzero-skip profile must add deterministic allowlist verification as a new
schema contract. Any runner, shim, helper, profile, dependency-lock,
`pyproject.toml`, or Pyrefly baseline change alters the trusted control
fingerprint and therefore requires Central review plus an operational-guard
fingerprint update before host evidence is accepted.

Regenerate dependency locks deliberately:

```bash
uv pip compile requirements-local-agent-assessment.in \
  --generate-hashes --python-version 3.11 --python-platform linux \
  -o requirements-local-agent-assessment-py311.lock
uv pip compile requirements-local-agent-assessment.in \
  --generate-hashes --python-version 3.12 --python-platform linux \
  -o requirements-local-agent-assessment-py312.lock
```
