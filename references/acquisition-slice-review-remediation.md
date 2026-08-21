# Issue #262 independent-review remediation

This document records the Central remediation applied to PR #284 after the
independent review of the acquisition vertical-slice refactor. It is a scope and
validation contract, not a substitute for current GitHub or local runtime
evidence. Later Phase-5 cleanup removed the temporary construction and
compatibility surfaces called out in the historical review; current ownership
is stated below.

## Scope

The review found no independent production correctness, PostgreSQL authority,
transaction, schema, CLI-equivalence, import-cycle, or Pyrefly-policy defect in
the acquisition refactor. Production behavior was therefore intentionally left
unchanged by that remediation. The revisions closed evidence and regression gaps
without weakening Ruff, Pyrefly, pytest, persistence authority, or provider
boundaries.

The historical reviewed revision was:

- base: `abd67641c4ed07e2b448937e316a891213732ea0`;
- head: `e656fa2adfb953a77500387a1975bfaf36b51384`.

Those SHAs identify the review that produced the findings. They are not a
license to validate a later PR revision against stale source. Every subsequent
review/handoff must bind to its current exact head.

## Finding-to-remediation map

| Review item | Current disposition | Remediation / current owner |
|---|---|---|
| Blocking: required exact-head Ruff/runtime evidence was absent because ordinary PR jobs used GitHub's synthetic merge commit | Resolved structurally | `.github/workflows/acquisition-slice-review.yml` checks out the immutable candidate SHA, asserts `git rev-parse HEAD`, binds the exact base SHA, then runs changed-scope Ruff, changed-scope Pyrefly, full-project Pyrefly, and focused acquisition/contract pytest. Its JSON artifact records base/candidate/tested identity. General merge-ref CI remains supplementary mergeability evidence only. |
| Test/documentation gap: `scripts/test_issue_262_acquisition_slice.py` was not durably executed by an authoritative exact-head gate | Resolved | The exact-head acquisition-slice workflow executes the #262 structural suite on every relevant PR revision and on explicit exact-SHA workflow dispatch. |
| Test/documentation gap: source-level tests did not prove the effective runtime `BoundedExtractionStage.execute` after issue #217 installs `_bounded_extraction_execute` | Resolved | `scripts/test_issue_262_runtime_review.py` imports the public package, verifies the runtime method identity, constructs production bounded extraction through the canonical composition path, and drives a provider-needed candidate through the injected candidate-scrape port. |
| Codex automated suggestion: the public production checkpoint builder could select an adapter-less bounded extraction stage | Resolved and superseded by final composition ownership | The historical subclass-builder correction established the required behavior. Phase 5 then removed config-driven subclass builders entirely. `research_store.composition.build_production_orchestrator()` and `build_production_resumable_orchestrator()` now select `ProductionBoundedExtractionStage`, whose leaf topology supplies `BoundedFirecrawlSearchAdapter`. Current regression authority exercises those canonical builders. |
| Important non-blocking: historical acquisition imports remain | Resolved by Phase-5 cleanup | Migration-only acquisition facades were removed after caller/reference audit. Current production and operator callers target canonical `research_store.acquisition.*` owners. |
| Important non-blocking: `SearchAdapterResult` remains physically owned by the broader domain module | Intentional same-object compatibility | `research_store.acquisition.models.SearchAdapterResult` remains the same object rather than introducing a competing nominal model. Physical ownership can move only under a separately scoped caller audit. |
| Important non-blocking: compatibility hooks exposed `os` / `tempfile` through the historical authority facade | Resolved with facade removal | The migration facade no longer survives as a production compatibility surface; current acquisition authority is canonically implemented in `research_store.acquisition.authority`. |

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

## Runtime boundary proven by the regression

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
- the canonical production composition root supplies the bounded Firecrawl
  candidate adapter through the `CandidateScrapeAdapter` seam;
- the effective #217 method invokes that injected adapter for a candidate that
  requires provider extraction;
- the resulting extraction attempt identity is carried into the authoritative
  batch request;
- terminal provider/preflight outcome handling and run transition complete
  without a hidden concrete-provider fallback in application policy.

This test is deliberately about composition and runtime dependency direction;
it does not duplicate the issue #216 timeout/retry/failure-policy suite.

## Codex review disposition

The substantive Codex suggestion represented by the historical
`bounded_orchestrator.py` review thread concerned production construction of a
bounded extraction stage without a candidate-scrape adapter. The behavior is
now enforced at the sole general production composition root rather than by a
subclass-owned builder. The runtime test provides behavioral proof rather than
another source-string check.

GitHub thread-resolution metadata is not equivalent to code correctness. If a
historical thread remains unresolved, its state is recorded as review metadata;
it is not worked around by restoring deleted compatibility surfaces or changing
production code solely to satisfy stale thread text.

## Local-agent handoff boundary

After Central commits revisions, the local agent remains an execution/evidence
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

These revisions close the source and workflow-design findings. Final review
closure still requires fresh evidence at the current exact PR head:

- exact-head acquisition-slice review workflow success;
- independent local validation at that same 40-character SHA;
- re-read of current Ruff, Pyrefly, pytest/contract/integration checks;
- current-head review/thread state;
- merge-policy evidence to the extent GitHub exposes it.

No earlier review conclusion or test result survives a subsequent head change.
