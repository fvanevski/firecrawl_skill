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
worktree never supplies orchestration code, profiles, the canonical toolchain
manifest, the disposable-service helper, skip policy, or acceptance logic.

Steady-state PR assessment is main-owned. After this feature is present on
`origin/main`, the installed clean exact-main checkout supplies
`scripts/local_agent_assessment.py`, profiles, the canonical Python 3.12
toolchain manifest, service policy, static policy, cleanup, isolation, evidence
semantics, trusted regression membership,
and the PR candidate-worktree policy. The detached PR worktree supplies only
candidate application/source/test code subject to those controls.

Pre-merge self-assessment is the only bootstrap exception. While the PR that
introduces this feature is still unmerged, the new PR semantics cannot already
be imported from `origin/main`. The host operational guard may therefore admit
an explicitly Central-reviewed source checkout only when all of the following
are true:

- its clean `HEAD` is the exact requested canonical PR-head SHA;
- that `HEAD` is distinct from the locally resolved `origin/main` control ref;
- the guard pins the reviewed control-plane fingerprints, including
  `requirements-ci.txt` and `scripts/local_agent_pr_assessment.py`;
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
worktree. PR-only candidate-test discovery, collection, exact-node execution,
and pytest-entrypoint control hooks are temporary as well. The original runner
class and all dispatcher-installed control hooks are restored in a `finally`
block before dispatch returns or propagates an error.

For **`trusted-ref`** mode, the repository checkout is the trusted control
checkout and the repository-owned control-plane files are fingerprinted:

- `scripts/local_agent_assessment.py` and its thin executable shim;
- `scripts/disposable-test-services`;
- `references/local-agent-assessment-profiles.toml`;
- `scripts/run_ci_profile.py` and `scripts/ci_authority.py`, which provide the
  shared centralized static/profile authority;
- `pyproject.toml`, `pyrefly-baseline.json`, `ci/ruff-e402-debt.toml`, and
  `ci/ruff-e731-debt.toml`, which define the trusted static-analysis policy and
  exact fail-closed legacy debt;
- `references/pytest-skip-allowlist.json` and `scripts/verify_pytest_skips.py`,
  which define the same classified-skip authority consumed by centralized CI;
  and
- `requirements-ci.txt`, the canonical Python 3.12 pytest/Ruff/Pyrefly toolchain
  authority.

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
   head, with externally pinned control-plane fingerprints;
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

OpenCode operational-schema v5.22.0 and later expose one public host-assessment
entry point. The external invocation is always one typed-spec call:

```text
/home/filip/.config/opencode/plugins/operational-schema-v5/scripts/local-agent-assessment.mjs \
  --spec /tmp/opencode/verify/assessments/<assessment-id>.json
```

Do not use the retired public `--sha/--assessment-id` or
`--pr/--sha/--assessment-id` grammars. Do not invoke this repository's runner
directly as a fallback when the operational guard is required.

This repository uses the typed gateway's **repository-owned** execution mode.
`scripts/local-agent-assessment` already owns the exact-head candidate
worktree, Python environment, disposable PostgreSQL/Qdrant lifecycle, static
and pytest validation, Qdrant reset, typed evidence, isolation proof, and
cleanup. The outer gateway must therefore not construct a second candidate
worktree or duplicate those lifecycle operations. It retains the outer trust
boundary: fresh exact base and canonical PR-head admission, runner/control-file
pin verification, native-evidence validation/copying, final remote freshness,
and proof that the owner/control checkout did not move or become dirty.

For normal steady-state PR assessment, run the public gateway from a clean
checkout at exact freshly fetched `origin/main` and set `runner.authority` to
`base`. The base SHA in the spec must be that exact current-main commit. Set
`repository.head_ref` to canonical `refs/pull/<PR_NUMBER>/head` and bind the
exact requested PR-head SHA separately. A same-name developer branch is not PR
authority.

`runner.authority: "head"` is reserved for the reviewed pre-merge bootstrap
case described above. It is valid only when Central has explicitly reviewed
that exact PR-head control plane and supplied exact pins for it. It must never
be used as a shortcut around the steady-state main-owned runner.

The runner and outer integrity set are pinned by exact Git blob SHA at the
selected authority commit. Central derives these from immutable GitHub/Git
object evidence; the local agent must not choose, omit, or recompute the set.
Use the complete outer pin set below:

- runner: `scripts/local-agent-assessment`;
- `scripts/local_agent_assessment.py`;
- `scripts/local_agent_pr_assessment.py`;
- `scripts/disposable-test-services`;
- `references/local-agent-assessment-profiles.toml`;
- `scripts/run_ci_profile.py`;
- `scripts/ci_authority.py`;
- `pyproject.toml`;
- `pyrefly-baseline.json`;
- `ci/ruff-e402-debt.toml`;
- `ci/ruff-e731-debt.toml`;
- `references/pytest-skip-allowlist.json`;
- `scripts/verify_pytest_skips.py`; and
- `requirements-ci.txt`.

A PR-head repository-owned spec has this shape; every `<...-blob-sha>` must be
replaced with the exact Git blob SHA from the selected authority commit before
materialization:

```json
{
  "schema_version": "opencode-local-assessment-v1",
  "kind": "repo-pr",
  "assessment_id": "pr<PR_NUMBER>-<head-prefix>",
  "pr_number": <PR_NUMBER>,
  "repository": {
    "remote": "origin",
    "base_ref": "main",
    "base_sha": "<exact-current-main-sha>",
    "head_ref": "refs/pull/<PR_NUMBER>/head",
    "head_sha": "<exact-pr-head-sha>"
  },
  "runner": {
    "execution": "repository-owned",
    "authority": "base",
    "path": "scripts/local-agent-assessment",
    "blob_sha": "<shim-blob-sha>",
    "result_contract": "local-agent-assessment-v1",
    "plan_argv": [
      "plan",
      "--repo", "{repo_root}",
      "--sha", "{head_sha}",
      "--profile", "phase1-control-policy",
      "--target-kind", "pr-head",
      "--pr", "{pr_number}",
      "--workspace-root", "{workspace_root}",
      "--fetch"
    ],
    "run_argv": [
      "run",
      "--repo", "{repo_root}",
      "--sha", "{head_sha}",
      "--profile", "phase1-control-policy",
      "--target-kind", "pr-head",
      "--pr", "{pr_number}",
      "--assessment-id", "{assessment_id}",
      "--workspace-root", "{workspace_root}",
      "--fetch"
    ]
  },
  "integrity_files": [
    {"path": "scripts/local_agent_assessment.py", "blob_sha": "<base-runner-blob-sha>"},
    {"path": "scripts/local_agent_pr_assessment.py", "blob_sha": "<pr-dispatcher-blob-sha>"},
    {"path": "scripts/disposable-test-services", "blob_sha": "<service-helper-blob-sha>"},
    {"path": "references/local-agent-assessment-profiles.toml", "blob_sha": "<profile-blob-sha>"},
    {"path": "scripts/run_ci_profile.py", "blob_sha": "<static-runner-blob-sha>"},
    {"path": "scripts/ci_authority.py", "blob_sha": "<ci-authority-blob-sha>"},
    {"path": "pyproject.toml", "blob_sha": "<pyproject-blob-sha>"},
    {"path": "pyrefly-baseline.json", "blob_sha": "<pyrefly-baseline-blob-sha>"},
    {"path": "ci/ruff-e402-debt.toml", "blob_sha": "<ruff-e402-blob-sha>"},
    {"path": "ci/ruff-e731-debt.toml", "blob_sha": "<ruff-e731-blob-sha>"},
    {"path": "references/pytest-skip-allowlist.json", "blob_sha": "<skip-allowlist-blob-sha>"},
    {"path": "scripts/verify_pytest_skips.py", "blob_sha": "<skip-verifier-blob-sha>"},
    {"path": "requirements-ci.txt", "blob_sha": "<toolchain-blob-sha>"}
  ]
}
```

Do not add an `environment.venv` merely to satisfy the outer gateway; this
repository's native runner provisions the canonical exact-head
`.venv-research-store` itself. Likewise, do not invent `--base-sha` or output
arguments for the native runner. The outer gateway binds base authority itself
and supplies the admitted per-assessment `{workspace_root}`. The native runner
writes its typed evidence at:

```text
<workspace_root>/results/<assessment-id>/assessment.json
```

For steady-state v5.22 repository-owned execution that resolves to:

```text
/tmp/opencode/verify/repository-owned/<assessment-id>/results/<assessment-id>/assessment.json
```

After validating that native evidence, the gateway copies the exact accepted
bytes to:

```text
/tmp/opencode/verify/evidence/<assessment-id>.runner.json
```

and writes its outer summary to:

```text
/tmp/opencode/verify/evidence/<assessment-id>.summary.json
```

The gateway preserves native
`PASS|FAIL|BLOCKED|STALE|INFRA_ERROR|ISOLATION_BREACH` semantics and requires
`GATE_DECISION=NOT_EVALUATED`. `HOST_EVIDENCE_RESULT=PASS` remains evidence,
not the architectural gate decision.

No manual Git fetch/status/worktree preparation, venv setup, service startup,
pytest/static execution, Qdrant reset, or cleanup may surround the one public
gateway call. On any non-PASS result, inspect only the emitted bounded evidence
and source directly implicated by it; do not reconstruct the runner lifecycle
manually.

Any change to a pinned control-plane file invalidates prior gateway pins and
prior host-assessment evidence for purposes of a new review. Central must
supply the newly reviewed exact Git blob set before another gateway run.
Historical PASS evidence from an older runner/profile/helper/toolchain/bootstrap
fingerprint must never be presented as evidence for the changed control plane.

## Native repository-runner grammar

The commands in this section are the repository runner's native interface. For
PR-head assessment under OpenCode v5.22+, they are materialized only inside the
typed `runner.plan_argv` / `runner.run_argv` spec and invoked by the public
gateway. They are not an alternate public host-assessment route.

For trusted-ref maintenance outside the typed PR gateway, first inspect the
immutable trusted-ref plan. This performs
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

When the v5.22+ typed gateway invokes this repository-owned runner for an
explicitly reviewed repository PR, it uses this native grammar both before and
after the runner feature is merged:

```bash
scripts/local-agent-assessment plan \
  --repo /path/to/firecrawl_skill \
  --sha <exact-pr-head-sha> \
  --profile phase1-control-policy \
  --target-kind pr-head \
  --pr <PR_NUMBER> \
  --workspace-root <gateway-supplied-workspace-root> \
  --fetch

scripts/local-agent-assessment run \
  --repo /path/to/firecrawl_skill \
  --sha <exact-pr-head-sha> \
  --profile phase1-control-policy \
  --target-kind pr-head \
  --pr <PR_NUMBER> \
  --assessment-id pr<PR_NUMBER>-<sha-prefix> \
  --workspace-root <gateway-supplied-workspace-root> \
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
A profile-selected path that is byte-identical to current main remains
trusted-regression authority even when the historical PR merge-base diff
contains that path. Such a path is not duplicated into candidate-owned
supplemental membership or the changed-module isolation set; it continues to
execute only through trusted profile membership. This does not weaken the
byte-identity prerequisite or the one-candidate-module-per-process isolation
rule for genuine candidate-owned supplemental tests.

Trusted profile membership always comes from main authority: directly from the
trusted main checkout in steady state, or from an immutable exact-main snapshot
in the pre-merge self-bootstrap. Candidate execution never defines trusted
membership.

Before candidate test execution the runner:

1. collects every trusted profile group from main authority using the profile
   selectors, requiring collection count to equal the trusted expected count;
2. computes the PR-side diff from the Git merge base to the exact candidate
   SHA, retaining only added/modified/renamed `test_*.py` modules beneath the
   configured test roots that are outside trusted profile paths, with a hard
   file-count bound, and requires every
   retained candidate test path to be a regular Git blob at the exact candidate
   SHA rather than a symlink, submodule, or other non-regular Git entry;
3. derives every auto-loaded `conftest.py` ancestor from repository root
   through each configured candidate test root, retains the merge-base change
   scan as defense in depth, and independently requires each protected path's
   exact current-main Git tree state to equal the candidate state; a one-sided
   path, different blob/mode/type, symlink, or other non-regular entry is blocked;
4. binds every discovered changed test module to its exact candidate Git blob.
   Before any candidate-worktree pytest process executes, the dispatcher reads
   those exact Git objects into a runner-owned source manifest. The isolated
   launcher validates the manifest hash and candidate identity and compiles every
   changed module before `pytest.main()`. Candidate collection and execution are
   then process-isolated per changed module: each runner-owned pytest process
   selects at most one changed candidate test module, and the launcher fails
   closed if more than one is selected. The selected module is served from the
   precompiled exact-Git source through the runner-owned import finder. A changed
   module therefore cannot replace mutable pytest import state and affect a later
   changed module, because that later module starts in a fresh pytest process;
5. before candidate-worktree collection or exact-node execution, revalidates
   each discovered candidate test path as a regular file contained by the
   detached candidate worktree, rejecting symlink components, path escape,
   disappearance, or non-file replacement as stale defense-in-depth evidence;
6. collects each changed candidate module in its own fresh pytest process with
   fixed runner-owned arguments (`-c /dev/null`, fixed rootdir/import mode,
   `--confcutdir` bound to that same trusted or candidate assessment root, no
   cache provider). The explicit conftest cutoff prevents pytest from constructing
   parent collectors above the assessment root and therefore from traversing
   unrelated host siblings while resolving explicit test paths. The runner then
   aggregates and sorts the exact node IDs, enforces the global
   configured node-count bound, and requires at least one accepted node for
   every discovered changed candidate test module;
7. records `candidate_test_manifest` with rule name, merge-base SHA, exact file
   list, exact node-ID list, and SHA-256 of the canonical manifest; and
8. executes trusted regressions only by their exact collected node IDs and
   executes each changed candidate module's exact nodes in a separate fresh
   pytest process. Trusted profile groups retain exact zero-skip enforcement.
   Candidate supplemental executions instead run the fingerprinted central
   `scripts/verify_pytest_skips.py` authority against the fingerprinted
   `references/pytest-skip-allowlist.json`, scoped to the executed changed test
   file, so unknown, stale, or reason-drifted skips fail closed while already
   classified skips remain valid evidence.

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
without changing trusted plugin behavior. Candidate collection and candidate
execution never select more than one changed candidate test module in a single
pytest process; the launcher independently rejects such an invocation. This
process boundary prevents an already executed changed module from replacing
pytest import machinery used for a later changed module. The same isolated
launcher remains active during collection and exact-node execution.

Candidate collection writes JUnit evidence, and collection-time skips, errors,
or failures invalidate the candidate collection. The runner also requires
pytest's reported collected count to equal the exact accepted node-ID count, so
a collection hook cannot silently deselect an item and present the reduced list
as authoritative. Independently, each discovered changed candidate test module
must appear in the accepted node-ID set at least once; an empty or gutted
changed module therefore fails with the caller's candidate collection status
instead of silently disappearing from the manifest. Missing or ambiguous
collection-summary evidence likewise fails with the caller's candidate
collection status rather than being reclassified as a control-plane
prerequisite failure. These controls prevent the identified pytest authority
substitutions; they do not turn reviewed PR execution into a general
hostile-code sandbox.

Static assessment is not reconstructed inside the host runner. The runner
executes the fingerprinted central `scripts/run_ci_profile.py --profile static`
authority against the exact candidate SHA, using the canonical Python 3.12
toolchain. That shared path applies repository-wide Ruff lint/format, exact
fail-closed `E402` and `E731` debt contracts, and Pyrefly policy. The narrow
`E731` entry exists only so the main-owned
`tests/integration/test_acquisition_authority.py` regression can remain
byte-identical to trusted control instead of being rewritten to satisfy lint.
In steady-state main-owned PR assessment, candidate `pyproject.toml`,
`pyrefly-baseline.json`, and both Ruff debt contracts remain byte-identical to
trusted control before execution. The fingerprint-pinned pre-merge bootstrap
remains a narrow reviewed transition mechanism; candidate PASS is not
self-authenticating, and Central source/diff review plus exact-head CI remain
required.

## Deterministic lifecycle

The runner owns the following sequence:

1. Validate the 40-character commit, named profile, target grammar, exact test
   paths, trusted control-plane fingerprints, sanctioned root, and acquire the
   workspace-independent host lifecycle lease before the workspace-local lock.
   PR mode also resolves the canonical PR head and records exact main control
   identity.
2. In steady-state PR mode, keep the clean exact-main checkout as control and
   create only the detached candidate worktree. In pre-merge self-bootstrap,
   export exact `origin/main` with `git archive` into the runner-owned materials
   namespace before creating the separate detached candidate worktree.
3. Create the single Python 3.12 environment at the repository's canonical
   ignored `<worktree>/.venv-research-store` path and install its dependencies
   from `requirements-ci.txt`, the repository's canonical validation-tool
   manifest.
4. Build a minimal subprocess environment. It does not copy the host
   environment and gives HOME, XDG data/cache, TMPDIR, and BLOB_ROOT isolated
   assessment paths.
5. Allocate a free loopback port pair while holding the lifecycle lock, start
   the trusted disposable-service helper, and parse its strict JSON contract.
6. Run the fingerprinted centralized static profile, then all host-assessment
   pytest groups as direct argv arrays with `shell=False`. Every runner-owned
   pytest session binds
   `--confcutdir` to its trusted execution root so explicit selectors cannot
   cause parent collection to traverse unrelated host siblings. Every pytest
   group emits JUnit XML. Trusted membership is collected from main authority
   before candidate tests execute;
   every PR-mode pytest process uses the dispatcher-installed isolated launcher,
   and changed candidate modules are collected/executed one module per fresh
   pytest process.
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

That form uses the runner's default workspace and applies to direct
trusted-ref operation. For an interrupted repository-owned v5.22 gateway run,
recovery must rebind both the sanctioned-root environment and the native
workspace to the exact per-assessment runtime that the gateway admitted:

```bash
workspace=/tmp/opencode/verify/repository-owned/<assessment-id>
LOCAL_AGENT_ASSESSMENT_ALLOWED_ROOT="$workspace" \
  scripts/local-agent-assessment recover \
    --repo /path/to/firecrawl_skill \
    --assessment-id <assessment-id> \
    --workspace-root "$workspace"
```

Recovery is lifecycle maintenance for an already interrupted assessment, not an
alternate PR-assessment entry point; do not use `recover` to bypass the public
v5.22 typed `--spec` gateway.

Recovery validates the recorded identity and paths, then acquires the same
workspace-independent host lifecycle lease **before creating a workspace lock or
recovery HOME/TMP/XDG/material state**. The host-wide lease is a fixed Linux
abstract-UNIX-socket reservation (`firecrawl-skill-local-agent-assessment-v1`),
so repository-owned v5.22 runs with different per-assessment `{workspace_root}`
values still contend on one kernel identity without requiring a shared
filesystem write outside the Landlock grant. Assessment execution and recovery
then also acquire the existing `<workspace_root>/.locks/host-assessment.lock`
as a workspace-local defense-in-depth lock. A recovery attempt refused because
another assessment owns the host-wide lease is `BLOCKED` before its workspace
lock or material namespace is created.

After both locks are acquired, recovery rejects path escapes and symlink
redirection, invokes the ownership-checking trusted helper, removes only the
registered assessment worktree/materials, and retains the journal and evidence
directory. Removing materials also removes any self-bootstrap exact-main control
snapshot. Recovery of assessment A therefore cannot overlap the native
worktree/service cleanup of active assessment B merely because A and B have
different v5.22 per-assessment workspace roots.

The host-wide lifecycle lease intentionally serializes assessment execution and
recovery. This is a correctness boundary around port allocation, shared
Git/Docker lifecycle operations, and host default-store auditing, not a
throughput optimization. The workspace-local file lock is not treated as the
host-wide authority.

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

Direct trusted-ref mode retains results under the selected workspace root
(default `/tmp/opencode/verify/results/<assessment-id>`). Repository-owned
v5.22 gateway mode instead retains native results beneath its admitted
per-assessment workspace at
`<workspace_root>/results/<assessment-id>`; the outer gateway separately copies
accepted native evidence into its canonical evidence namespace.

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
  implementation, protected pytest control file, or non-regular candidate test
  Git entry, are also `BLOCKED`.
- `STALE`: candidate, expected-ref, PR-head, bootstrap-source when applicable,
  trusted-control, self-bootstrap control-snapshot, or candidate-test worktree
  path identity was not stable.
- `INFRA_ERROR`: lifecycle, reset, evidence, or cleanup infrastructure failed.
- `ISOLATION_BREACH`: an assessment wrote to the host default blob store.

On any result other than PASS, an LLM may inspect only the emitted failure
bundle and source directly implicated by it. It must not reconstruct worktree,
environment, service, test, reset, or cleanup commands manually.

## Exact-head handoff evidence

A filesystem path on the host is not independently reviewable evidence by
itself. After any Central change to the runner, PR bootstrap/dispatcher, helper,
profile, shim, or toolchain manifest, the local evidence collector must first update the
external operational guard to the reviewed fingerprints and then perform a
**fresh** gateway run against the then-current canonical PR head or
authoritative `origin/main` SHA as appropriate.

For the pre-merge self-assessment of the PR introducing this feature, the local
collector must prove that its source checkout is clean at the exact requested
PR SHA and distinct from freshly fetched `origin/main`, and that the gateway
guard accepts the complete reviewed control-plane fingerprint set. It must not copy
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
- centralized static-profile outcome, including Ruff, Ruff-format, exact E402
  and E731 debt evidence, and Pyrefly evidence;
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
supplies these values. Schema v1 trusted profile groups permit exactly zero skips. PR candidate
supplemental regressions use the repository's existing deterministic classified
skip verifier and allowlist instead of weakening that trusted-group invariant.
Any runner, PR bootstrap/dispatcher, shim, helper, profile, centralized static
runner/CI authority, pytest skip verifier/allowlist, `requirements-ci.txt`,
`pyproject.toml`, Ruff debt contract, or Pyrefly baseline change alters the
trusted control fingerprint and therefore requires Central review plus an
operational-guard fingerprint update before host evidence is accepted.

Tool-version changes belong only in `requirements-ci.txt`. The host assessment
runner consumes that manifest directly; do not create per-runtime assessment
pin files or an independent static/test tool authority.
