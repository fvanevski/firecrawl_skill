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

Run the controller only through the host operational guard. The candidate
worktree never supplies orchestration code, profiles, dependency locks, the
disposable-service helper, skip policy, or acceptance logic.

Steady-state PR assessment is main-owned. After this feature is present on
`origin/main`, the installed clean exact-main checkout supplies
`scripts/local_agent_assessment.py`, profiles, locks, service policy, static
policy, cleanup, isolation, evidence semantics, trusted regression membership,
and the PR candidate-worktree policy. The detached PR worktree supplies only
candidate application/source/test code subject to those controls.

Pre-merge self-assessment is the only bootstrap exception. While the PR that
introduces this feature is still unmerged, the new PR semantics cannot already
be imported from `origin/main`. The host operational guard may therefore admit
an explicitly Central-reviewed source checkout only when all of the following
are true:

- its clean `HEAD` is the exact requested canonical PR-head SHA;
- that `HEAD` is distinct from the locally resolved `origin/main` control ref;
- the guard pins the reviewed fingerprints of the normal eight control-plane
  files plus `scripts/local_agent_pr_assessment.py`;
- canonical `refs/pull/<PR_NUMBER>/head` equals that requested SHA; and
- exact freshly fetched `origin/main` remains independently authoritative for
  trusted baseline source and regression membership.

The thin shim routes PR grammar into `local_agent_pr_assessment.main()`. That
dispatcher resolves the installed control checkout `HEAD`, the locally tracked
`origin/main`, and the requested candidate SHA. It falls through to the ordinary
`base.main()` / `base.Runner` path unless `HEAD == requested SHA` **and**
`HEAD != origin/main`. Thus exact-main control always stays on the base runner,
even if a no-op/current-main PR happens to have a candidate SHA equal to main.
Only the distinct candidate-checkout equality case may temporarily substitute
`ReviewedPRRunner` for pre-merge self-assessment.

The same dispatcher installs the trusted isolated pytest launcher for every
PR-mode pytest process before entering `base.main()`, including when the PR has
no changed eligible `test_*.py` module. This keeps the pinned pytest
installation authoritative before the candidate repository root becomes
importable and prevents a candidate-root `pytest.py` from shadowing pytest. The
changed-test set only controls which exact test-module plugin registrations are
suppressed; an empty set never re-enables `python -m pytest` in a candidate
worktree. The original runner class and pytest entrypoint control hooks are
restored in a `finally` block before dispatch returns or propagates an error.

For **`trusted-ref`** mode, the repository checkout is the trusted control
checkout and the original eight control-plane files are fingerprinted:

- `scripts/local_agent_assessment.py` and its thin executable shim;
- `scripts/disposable-test-services`;
- `references/local-agent-assessment-profiles.toml`;
- `pyproject.toml` and `pyrefly-baseline.json`, which define the trusted static
  analysis policy; and
- the Python 3.11 and 3.12 hashed dependency locks.

The reviewed-PR gateway also fingerprints `scripts/local_agent_pr_assessment.py`
because the shim executes that dispatcher before selecting steady-state main
control versus the narrow self-bootstrap path and before installing PR-only
pytest isolation. Candidate copies of any of these files are never implicitly
authoritative merely because they exist in the detached candidate worktree.
Trusted-ref invocations do not enter the PR dispatcher and retain their existing
pytest command path.

### PR control identities

Normal post-merge **`pr-head`** assessment has two repository identities:

1. **trusted control plane** — clean exact freshly fetched `origin/main`, which
   supplies the ordinary runner and all repository-owned assessment policy; and
2. **candidate execution** — a detached worktree at the exact canonical PR-head
   SHA.

Pre-merge self-assessment adds a third identity because the new implementation
has not landed on main yet:

1. **reviewed bootstrap control plane** — clean source checkout at the exact PR
   head, with nine externally pinned control-plane fingerprints;
2. **trusted baseline source** — exact `origin/main`, exported by the bootstrap
   into a runner-owned immutable snapshot for trusted profile membership; and
3. **candidate execution** — a separate detached worktree at the exact
   canonical PR-head SHA.

This bootstrap does not pretend that unmerged code already lives on main. It is
only a reviewed transition mechanism for validating the PR that introduces the
steady-state main-owned runner semantics.

Schema v1 therefore has three trust dispositions:

1. **`trusted-ref`** — the original exact-main/gate mode. A profile allowlists a
   remote-tracking ref and `--expected-ref` must name that ref at the requested
   SHA. Its command path and expected-count behavior remain unchanged.
2. **`pr-head`** — an explicitly reviewed repository PR candidate bound to
   canonical `refs/pull/<PR_NUMBER>/head` plus the caller-supplied exact SHA.
   Steady state is main-owned; the fingerprint-pinned bootstrap is only the
   pre-merge self-assessment exception described above.
3. **hostile/untrusted or arbitrary fork code** — unsupported. `pr-head` is not
   an OS sandbox. General untrusted execution requires a separately reviewed
   container/VM isolation profile and is outside this runner contract.

## OpenCode gateway contract

The OpenCode operational gateway has exactly two bounded identity grammars:

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
fixed `phase1-control-policy` and sanctioned `/tmp/opencode/verify` root.

For trusted-ref mode, the guard verifies the original eight reviewed
fingerprints. For PR mode, the guard verifies those eight plus the reviewed
`scripts/local_agent_pr_assessment.py` fingerprint before the shim dispatches.
Direct and RTK forms of only the two bounded external grammars may be
allowlisted for Verify. A runner, profile, lock, helper, bootstrap,
static-analysis policy, or baseline found only inside the detached candidate
worktree is not trusted evidence.

Any change to a fingerprinted control-plane file invalidates prior gateway
fingerprints and prior host-assessment evidence for purposes of a new review.
The operational guard must be updated to the newly reviewed fingerprints
before the gateway is used again. Historical PASS evidence from an older
runner/profile/helper/lock/bootstrap fingerprint must never be presented as
evidence for the changed control plane.

## Invocation

First inspect the immutable trusted-ref plan. This performs
Git/object/profile validation but creates no worktree, environments, services,
or result directory:

```bash
scripts/local-agent-assessment plan \
  --repo /path/to/firecrawl_skill \
  --sha 39601ab2df0c78c389346d3f3d5ae85eab54cb84 \
  --profile phase1-control-policy \
  --expected-ref origin/main \
  --fetch
```

Then run the bounded trusted-ref assessment:

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

For an explicitly reviewed repository PR, the same external grammar is used
both before and after this feature is merged:

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

The shim sends that PR grammar through the reviewed dispatcher. Exact-main
control falls through to the base runner, including the edge case where the
requested PR SHA itself equals current main. The bootstrap is selected only
when the installed source `HEAD` equals the requested candidate SHA while being
distinct from the locally resolved `origin/main` ref. The bootstrap then fresh-
fetches main and independently proves that separation before execution. In both
steady-state and bootstrap PR paths, the dispatcher keeps the trusted isolated
pytest entrypoint installed for the duration of `base.main()`.

PR mode rejects `--expected-ref` and arbitrary branch names. Both paths resolve
the canonical PR head and require it to equal the requested SHA before
candidate execution. The steady-state path revalidates the exact clean main
control checkout directly. The self-bootstrap path independently records
`origin/main` as `control_sha`, exports that exact Git object into its
runner-owned control snapshot, and revalidates the bootstrap source, main ref,
canonical PR head, candidate worktree, and snapshot inventory at completion.
Identity movement yields `STALE`.

## PR-review test authority

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

Trusted profile membership always comes from main authority: directly from the
trusted main checkout in steady state, or from an immutable exact-main snapshot
in the pre-merge self-bootstrap. Candidate execution never defines trusted
membership.

Before candidate test execution the runner:

1. collects every trusted profile group from main authority using the profile
   selectors, requiring collection count to equal the trusted expected count;
2. computes the PR-side diff from the Git merge base to the exact candidate
   SHA, retaining only added/modified/renamed `test_*.py` modules beneath the
   configured test roots, with a hard file-count bound;
3. protects every auto-loaded `conftest.py` ancestor from repository root
   through each configured candidate test root and blocks any added, modified,
   deleted, or renamed protected `conftest.py` before pytest starts;
4. collects changed candidate modules with fixed runner-owned pytest arguments
   (`-c /dev/null`, fixed rootdir/import mode, no cache provider), sorts the
   exact node IDs, and enforces the configured node-count bound;
5. records `candidate_test_manifest` with rule name, merge-base SHA, exact file
   list, exact node-ID list, and SHA-256 of the canonical manifest; and
6. executes trusted regressions and changed candidate regressions only by the
   exact collected node IDs, retaining JUnit and zero-skip enforcement.

The self-bootstrap additionally inventories the trusted control snapshot
immediately after membership collection and requires that inventory to remain
byte-identical through final identity proof. Candidate execution therefore
cannot silently rewrite the already-collected trusted source snapshot without
producing `STALE` evidence.

Candidate collection is additionally proved complete and unfiltered. Obvious
changed-module `pytest_plugins` declarations are rejected during preflight as a
defense-in-depth diagnostic, but that static scan is not the authority
boundary. The dispatcher uses the trusted isolated pytest launcher for every
PR-mode pytest process, not only when changed candidate test modules exist. The
launcher starts Python with `-P`, imports the pinned pytest installation before
adding the process working root to `sys.path`, and wraps pytest's test-module
plugin registration. For exact changed candidate test modules, their module
plugin registration is suppressed; unchanged trusted test modules retain normal
pytest module-plugin processing, so existing control-owned test support
continues to work. With an empty changed-test set the launcher still runs and
suppresses no module plugins, which prevents repository-root module shadowing
without changing trusted plugin behavior. The same launcher is active during
collection and exact-node execution.

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
candidate `pyproject.toml`, but candidate `pyproject.toml` and
`pyrefly-baseline.json` Git blobs must be byte-identical to the trusted control
copies before execution. A PR that changes those static-analysis authority
files is therefore `BLOCKED` for this host-evidence mode and requires separate
Central review of the control-plane change.

## Deterministic lifecycle

The runner owns the following sequence:

1. Validate the 40-character commit, named profile, target grammar, exact test
   paths, trusted control-plane fingerprints, sanctioned root, and single-host
   lifecycle lock. PR mode also resolves the canonical PR head and records
   exact main control identity.
2. In steady-state PR mode, keep the clean exact-main checkout as control and
   create only the detached candidate worktree. In pre-merge self-bootstrap,
   export exact `origin/main` with `git archive` into the runner-owned materials
   namespace before creating the separate detached candidate worktree.
3. Create Python 3.11 under `materials` and Python 3.12 at the repository's
   canonical ignored `<worktree>/.venv-research-store` path, synchronized from
   platform-specific hashed locks with `uv pip sync --require-hashes`.
4. Build a minimal subprocess environment. It does not copy the host
   environment and gives HOME, XDG data/cache, TMPDIR, and BLOB_ROOT isolated
   assessment paths.
5. Allocate a free loopback port pair while holding the lifecycle lock, start
   the trusted disposable-service helper, and parse its strict JSON contract.
6. Run Ruff, Ruff format, Pyrefly, and all profile pytest groups as direct argv
   arrays with `shell=False`. Every pytest group emits JUnit XML. Trusted
   membership is collected from main authority before candidate tests execute;
   every PR-mode pytest process uses the dispatcher-installed isolated launcher.
7. Recreate Qdrant when the profile requires reset proof and check `/readyz`.
8. Prove final SHA, tracked/untracked status, whitespace state, and target
   freshness. Trusted-ref mode rechecks its expected ref. Steady-state PR mode
   rechecks the main control checkout and canonical PR identity. Self-bootstrap
   also rechecks the fingerprint-pinned bootstrap checkout and exact-main
   control-snapshot inventory.
9. Tear down owned services, remove the candidate Git worktree through Git,
   remove materials including any self-bootstrap exact-main snapshot, and retain
   only redacted logs plus typed evidence.

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
`results/<assessment-id>/lifecycle.json`. Any self-bootstrap control snapshot
lives under the ordinary materials namespace, so it requires no independent
recovery registration. If the process is killed or the host restarts, recover
only the recorded namespace and candidate worktree:

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
Removing materials also removes any self-bootstrap exact-main control snapshot.

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
`control_ref_start`, and `control_ref_end` where applicable. PR mode records the
complete hashed `candidate_test_manifest`.

The reviewed dispatcher remains part of the PR control fingerprint. During
pre-merge self-bootstrap, `pr_bootstrap` identifies the reviewed bootstrap
controller and the plan additionally reports `control_plane_source_sha` and
`control_snapshot_source_sha` so the temporary three-way trust split is
machine-visible before state-changing execution. Those bootstrap-only identity
fields are not required to manufacture a third identity during normal
steady-state main-owned PR assessment. PostgreSQL passwords are redacted from
retained output.

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
- `STALE`: candidate, expected-ref, PR-head, bootstrap-source when applicable,
  trusted-control, or self-bootstrap control-snapshot identity was not stable.
- `INFRA_ERROR`: lifecycle, reset, evidence, or cleanup infrastructure failed.
- `ISOLATION_BREACH`: an assessment wrote to the host default blob store.

On any result other than PASS, an LLM may inspect only the emitted failure
bundle and source directly implicated by it. It must not reconstruct worktree,
environment, service, test, reset, or cleanup commands manually.

## Exact-head handoff evidence

A filesystem path on the host is not independently reviewable evidence by
itself. After any Central change to the runner, PR bootstrap/dispatcher, helper,
profile, shim, or lock files, the local evidence collector must first update the
external operational guard to the reviewed fingerprints and then perform a
**fresh** gateway run against the then-current canonical PR head or
authoritative `origin/main` SHA as appropriate.

For the pre-merge self-assessment of the PR introducing this feature, the local
collector must prove that its source checkout is clean at the exact requested
PR SHA and distinct from freshly fetched `origin/main`, and that the gateway
guard accepts all nine reviewed control-plane fingerprints. It must not copy
the PR runner onto main, cherry-pick it into the baseline, weaken
source-identity checks, or present the reviewed bootstrap checkout as though it
were `origin/main`. The bootstrap controller itself exports exact main for
trusted membership.

After this feature is merged, future PR-head assessments must instead use the
normal steady-state main-owned path: update the trusted control checkout to the
exact freshly fetched `origin/main`, leave the candidate only in its detached
exact-SHA worktree, and let the dispatcher fall through to `base.Runner` while
retaining the dispatcher-installed isolated pytest entrypoint for PR mode.

Return to Central, at minimum:

- exact authoritative `origin/main` control SHA;
- for a pre-merge self-bootstrap run, exact reviewed bootstrap source SHA plus
  requested/tested and PR-head start/end SHAs;
- for any PR mode, requested/tested SHA plus PR-head start/end SHAs;
- target kind and PR number where applicable;
- assessment ID and disposable-service namespace;
- `HOST_EVIDENCE_RESULT` and `GATE_DECISION`;
- all control-plane fingerprints recorded by the runner, including the reviewed
  PR dispatcher/bootstrap fingerprint where applicable;
- the exact `candidate_test_manifest` and its SHA-256 for PR mode;
- exact per-group JUnit expected/observed counts and skip details;
- Ruff, Ruff-format, and Pyrefly command outcomes;
- expected-ref or control-ref start/end identity as applicable;
- Qdrant reset and `/readyz` proof;
- host-default blob-store isolation result;
- service/worktree/material cleanup proof; and
- `assessment.json` SHA-256 plus either its complete bounded content or another
  Central-accessible evidence representation.

Do not reuse the earlier Gate #312 assessment after the runner fingerprint has
changed. A new exact-main assessment is acceptance evidence for the new control
plane; the historical assessment remains rationale only. Likewise, every PR
dispatcher/bootstrap fingerprint change invalidates prior PR-head host evidence.

## Profile maintenance

Profiles are declarative test selectors and exact expected test counts, never
arbitrary commands. Named falsification nodes remain explicit; marker-only
membership is insufficient for gate-critical behavior. Contract tests require
every path and named node to exist. In PR mode, those profile-selected test
implementations are control-owned and must remain byte-identical at the
candidate SHA; candidate regression evidence is additive rather than a way to
rewrite trusted assertions. PR policy additionally fixes the candidate test
Python, allowed test roots, and hard file/node bounds; the candidate never
supplies these values. Schema v1 permits exactly zero skips; a future
nonzero-skip profile must add deterministic allowlist verification as a new
schema contract. Any runner, PR bootstrap/dispatcher, shim, helper, profile,
dependency-lock, `pyproject.toml`, or Pyrefly baseline change alters the trusted
control fingerprint and therefore requires Central review plus an
operational-guard fingerprint update before host evidence is accepted.

Regenerate dependency locks deliberately:

```bash
uv pip compile requirements-local-agent-assessment.in \
  --generate-hashes --python-version 3.11 --python-platform linux \
  -o requirements-local-agent-assessment-py311.lock
uv pip compile requirements-local-agent-assessment.in \
  --generate-hashes --python-version 3.12 --python-platform linux \
  -o requirements-local-agent-assessment-py312.lock
```
