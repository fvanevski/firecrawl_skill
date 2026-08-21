# PR #296 local validation follow-up

This note records the first independent local-agent validation of the Refactor EPIC UoW remediation and the Central corrections that followed it.

## Validated revision

```text
BASE_SHA=407c37dd08093ebe26103b1477df05bb100db073
LOCAL_VALIDATED_HEAD=449a9614b5d97a6b7be0c81000102d61724f9c29
LOCAL_DISPOSITION=FAIL — return to Central
```

The local agent checked out the exact immutable head with a clean tree, performed the Serena structural audit, ran Ruff/Pyrefly, exercised disposable PostgreSQL/Qdrant services, ran the focused/runtime suites, and then ran the full pytest suite.

Structural and focused evidence was positive: the generic UoW router was absent; `uow.runs` was bound to `PostgresResearchRepository`; named repository roles were present; the six issue-#217 direct APIs and `persist_ingest` were intact; Ruff was clean; Pyrefly 1.1.1 showed no new reviewed-debt errors; focused/static tests passed; and the disposable runtime set passed.

## Full-suite findings

The full local suite reported:

```text
passed=3050
failed=5
errors=0
skipped=2
xfailed=0
xpassed=0
```

Two failures were independently classified as pre-existing environment/fixture defects on the base revision:

- `test_workflow.py::test_fscrape_rejects_undocumented_format`
- `test_workflow.py::test_fscrape_preserves_multiple_urls_and_schema`

Both fail because `jsonschema` is absent from the fixture subprocess environment. They are not caused by the UoW remediation and are not repaired in PR #296.

Three failures were PR-introduced test-contract defects and were returned to Central:

1. `tests/contract/test_test_topology.py` still expected 136 active tests even though this PR adds `test_issue_269_uow_repository_boundary.py`. The correct final distribution is 137 active files with 29 contract files.
2. `tests/integration/test_phase2_integration.py` used `uow.runs.record_search_response(...)` inside the rollback test. Search-response persistence belongs to `uow.search_responses`.
3. `tests/integration/test_search_responses.py` used the same obsolete `uow.runs.record_search_response(...)` route in its rollback test.

## Central corrections

Central resolved the three PR-introduced defects without changing production behavior:

- topology authority updated to 137 total / 29 contract files;
- Phase-2 rollback test migrated to `uow.search_responses.record_search_response(...)`;
- search-response rollback test migrated to `uow.search_responses.record_search_response(...)`.

The local report also exposed a regression-coverage gap: the permanent UoW test-authority guard scanned direct `uow.<method>()` calls in several critical authorities, but did not include the two failing integration files and did not reject forbidden three-part `uow.runs.<cross-domain-method>()` calls in test authorities.

Central therefore extended `tests/contract/test_issue_269_uow_repository_boundary.py` to:

- include `test_phase2_integration.py` and `test_search_responses.py` in the critical test-authority set; and
- reject forbidden cross-domain `uow.runs.*` calls there as well as generic direct `uow.*` calls.

This closes the exact regression class discovered by local validation rather than only patching the two call sites.

## Disposable-service evidence from local validation

```text
namespace=fc296_449a961_local_1787337051
PostgreSQL=127.0.0.1:55436
Qdrant=127.0.0.1:55437
reset-qdrant=no (fresh disposable container)
teardown=completed
namespace-owned containers after teardown=0
protected persistent ports 55432/6333 targeted=no
```

## Evidence rule after Central corrections

The successful portions of the local run remain useful diagnostic evidence for the old exact head only. Because Central corrections moved the branch, neither that local PASS subset nor the prior Central CI may be promoted to final evidence for the new head.

Before any subsequent handoff/merge decision, Central must establish fresh exact-head CI/review/base/diff evidence. If a second local validation is requested, it must detach at the newly supplied immutable head and rerun the affected/full suites; the old `449a9614...` validation does not authorize the corrected revision.
