# Issue #262 independent-review remediation

This document records the Central remediation applied to PR #284 after the
independent review of the acquisition vertical-slice refactor. It is a scope and
validation contract, not a substitute for current GitHub or local runtime
evidence.

## Scope

The review found no independent production correctness, PostgreSQL authority,
transaction, schema, CLI-equivalence, import-cycle, or Pyrefly-policy defect in
the acquisition refactor. Production behavior is therefore intentionally left
unchanged by this remediation. The revisions close evidence and regression gaps
without weakening Ruff, Pyrefly, pytest, persistence authority, or provider
boundaries.

The historical reviewed revision was:

- base: `abd67641c4ed07e2b448937e316a891213732ea0`;
- head: `e656fa2adfb953a77500387a1975bfaf36b51384`.

Those SHAs identify the review that produced the findings. They are not a
license to validate a later PR revision against stale source. Every subsequent
review/handoff must re-read PR #284 and bind to its then-current exact head.

## Finding-to-remediation map

| Review item | Disposition | Remediation |
|---|---|---|
| Blocking: required exact-head Ruff/runtime evidence was absent because ordinary PR jobs used GitHub's synthetic merge commit | Resolved structurally | `.github/workflows/acquisition-slice-review.yml` checks out the immutable candidate SHA, asserts `git rev-parse HEAD`, binds the exact base SHA, then runs changed-scope Ruff, changed-scope Pyrefly, full-project Pyrefly, and focused acquisition/contract pytest. Its JSON artifact records base/candidate/tested identity. General merge-ref CI remains supplementary mergeability evidence only. |
| Test/documentation gap: `scripts/test_issue_262_acquisition_slice.py` was not durably executed by an authoritative exact-head gate | Resolved | The exact-head acquisition-slice workflow executes the #262 structural suite on every relevant PR revision and on explicit exact-SHA workflow dispatch. |
| Test/documentation gap: source-level tests did not prove the effective runtime `BoundedExtractionStage.execute` after issue #217 installs `_bounded_extraction_execute` | Resolved | `scripts/test_issue_262_runtime_review.py` imports the public package, verifies the runtime method identity, constructs `ProductionBoundedExtractionStage` through its default provider-composition path, and drives a provider-needed candidate through the injected candidate-scrape port. |
| Codex automated suggestion: the public production checkpoint builder could select an adapter-less bounded extraction stage | Resolved and strengthened | The existing #262 implementation makes `CheckpointResearchOrchestrator.build()` default to `ProductionBoundedExtractionStage`, whose composition boundary supplies `BoundedFirecrawlSearchAdapter`. The new runtime regression proves that default still reaches the injected port after the issue #217 runtime method installation. |
| Important non-blocking: historical acquisition imports remain | Intentional temporary compatibility | Historical modules remain thin same-object facades only. They do not contain duplicate provider/application implementations or select provider transport implicitly. Removal requires a later caller/reference audit. |
| Important non-blocking: `SearchAdapterResult` remains physically owned by the broader domain module | Intentional same-object compatibility | `research_store.acquisition.models.SearchAdapterResult` remains the same object rather than introducing a competing nominal model. Physical ownership can move only under a separately scoped caller audit. |
| Important non-blocking: compatibility hooks expose `os` / `tempfile` through the historical authority facade | Intentional compatibility | The facade exports the same stdlib module objects solely for existing test/injection hooks; acquisition authority remains canonically implemented in `research_store.acquisition.authority`. |

## Exact-head authority invariant

A GitHub check is not exact-head evidence merely because its display metadata,
artifact name, or PR association contains the PR head SHA. The checkout itself
must be pinned and verified before validation begins.

The acquisition-slice review gate therefore uses this order:

1. resolve candidate and base from immutable PR event SHAs or explicit dispatch
   inputs;
2. check out the candidate SHA with full history;
3. require `git rev-parse HEAD == candidate_sha` and require the base commit to
   exist;
4. run `git diff --check base...candidate`;
5. derive exact ACMR changed Python paths from `base...candidate`;
6. run Ruff check and Ruff format-check on those paths;
7. run Pyrefly on those explicit paths, including changed tests;
8. run full-project Pyrefly;
9. run the focused acquisition/authority/provider/runtime contract set against
   disposable PostgreSQL;
10. persist a bounded evidence artifact containing the tested SHA and authority
    families.

The workflow does not update `pyrefly-baseline.json`, add suppressions, change
Pyrefly scope, or substitute another static checker.

## Runtime boundary proven by the new regression

Issue #217 currently installs `_bounded_extraction_execute` onto
`BoundedExtractionStage.execute` during ordinary `research_store` package
initialization. That compatibility mechanism predates #262 and remains current
runtime behavior. A source-text assertion against `bounded_orchestrator.py`
alone therefore cannot prove the effective method's provider dependency.

`test_issue_262_runtime_review.py` closes that gap by asserting all of the
following in one behavioral path:

- ordinary package import has installed the #217 runtime method;
- production bounded extraction is constructed without a caller-supplied
  scrape adapter;
- the production composition root supplies the bounded Firecrawl candidate
  adapter through the `CandidateScrapeAdapter` seam;
- the effective #217 method invokes that injected adapter for a candidate that
  requires provider extraction;
- the resulting extraction attempt identity is carried into the authoritative
  batch request;
- terminal provider/preflight outcome handling and run transition complete
  without a hidden concrete-provider fallback in application policy.

This test is deliberately about composition and runtime dependency direction;
it does not duplicate the issue #216 timeout/retry/failure-policy suite.

## Codex review disposition

The substantive Codex suggestion represented by the existing
`bounded_orchestrator.py` review thread concerned production construction of a
bounded extraction stage without a candidate-scrape adapter. The production
builder correction already present in the PR is retained. The new runtime test
adds independent behavioral proof rather than another source-string check.

GitHub thread-resolution metadata is not equivalent to code correctness. If the
thread remains unresolved after this commit, Central should disposition the
thread using the available focused review-thread surface when supported; lack
of a thread-resolution mutation tool must be reported as a host/tool boundary,
not worked around by changing production code or fabricating a resolution.

## Local-agent handoff boundary

After Central commits these revisions, OpenCode remains an execution/evidence
agent. It must not redesign the acquisition architecture or alter this
remediation to make validation pass.

The handoff must explicitly use:

- **Serena** for changed-symbol/reference/dependency inspection and diagnostics;
- **RTK** for routine successful Ruff/Pyrefly/pytest output where filtering
  preserves decisive evidence;
- **OpenViking** only for bounded historical rationale, never current source or
  runtime authority;
- **native Git and runtime tools** for exact checkout/SHA evidence, failures,
  complete decisive diffs, PostgreSQL/Qdrant/Valkey evidence, and any
  transaction/concurrency or release conclusions.

The required command/test sequence is defined in
`references/local-agent-validation.md`. Any failure is returned to Central as
evidence; production code, tests, workflow gates, Pyrefly configuration, and
baseline are not weakened locally.

## Closure condition

These revisions close the *source and workflow-design* findings. Final review
closure still requires fresh evidence at the new exact PR head:

- exact-head acquisition-slice review workflow success;
- independent local OpenCode validation at that same 40-character SHA;
- re-read of current Ruff, Pyrefly, pytest/contract/integration checks;
- current-head review/thread state;
- merge-policy evidence to the extent GitHub exposes it.

No earlier review conclusion or test result survives a subsequent head change.
