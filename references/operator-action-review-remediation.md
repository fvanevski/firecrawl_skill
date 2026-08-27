# PR #322 review-remediation record

This note records the Central source remediation applied after the post-`c87d7e2` repair sequence and before any fresh local-agent assessment. It is evidence about source intent and review disposition only; it is not host-runtime evidence and does not set `GATE_DECISION`.

## Blocking findings

### Rejected direct-retention snapshots could re-enter resume projection

`PostgresCorpusRepository.resume_assets_for_run()` now treats the promotion subject's `(run_id, snapshot_id)` as the rejection authority. It no longer requires equality with nullable `candidate_id`, which is not authoritative for direct-retention subjects.

Regression coverage binds both retained and rejected ingest-seeded snapshots to current-run extraction attempts while leaving their promotion-subject `candidate_id` values null. The resume projection must return only the retained snapshot.

### Terminal durable action identities could be re-emitted as pending human work

`OperatorActionService._ensure_action()` now validates the row returned by durable `create_action()` idempotency. A matching row is reusable only while `status == "pending"`; an existing `resolved` or `superseded` identity raises `OperatorActionConflictError` instead of being emitted as `operator_action_required`.

Regression coverage resolves a scope action through `fork()` and then re-encounters the same unchanged scope authority. Re-ensuring that action must fail closed and the original row remains resolved.

### Public operator-action timestamps violated `operator-action-v1`

`OperatorActionRecord.to_public_dict()` now normalizes timestamp values to ISO-8601 strings (or `null`) before exposing the public contract. Existing strings remain unchanged; unsupported timestamp types fail closed.

A pure unit regression uses timezone-aware `datetime` values, requires exact `isoformat()` strings, and proves `json.dumps(public)` succeeds without `default=str`.

## Important non-blocking import findings

The two candidate-changed supplemental integration modules introduced into the PR manifest no longer use module-level `pytest_plugins`, which is prohibited by the deterministic PR-head assessment preflight:

- `tests/integration/test_asset_promotion_migration_compat.py`
- `tests/integration/test_issue_215_completion_budget.py`

Both now import `asset_promotion_test_support` directly and expose `promotion_config` from that imported support module, matching the already-reviewed direct-fixture pattern used elsewhere in the slice. No runner/profile exception or test-root change is introduced.

## Additional blocking findings from the fresh Codex review

### Resolved curation authority was not revalidated against the current census

`curation_completed()` previously treated the existence of any resolved current-policy curation action at the lifecycle revision as sufficient forever. A later extracted/retained subject could therefore appear at the same revision without being included in the operator's decision. The service now validates the latest resolved curation payload against the current curation census: the selectable subject IDs must be exactly the recorded retained IDs and every surviving subject must still be `retained`. Any addition or mutation makes curation incomplete and forces a new durable action.

### A stale controller observation could supersede a valid newer pending action

Pending-action validation now reloads run status after `pending_for_run(..., for_update=True)` has locked the action/run rows. `active_for_run()` evaluates staleness against that locked status, not the caller's earlier in-memory snapshot. `_ensure_action()` also rejects a stale caller before mutating a pending action and refuses to supersede a different pending action that remains authoritative under the locked run state.

### Snapshot-level resume rejection could erase an explicitly retained role

`resume_assets_for_run()` no longer excludes a snapshot merely because any role-specific promotion subject is rejected. A snapshot is excluded only when a rejected subject exists and no non-rejected subject survives for that `(run_id, snapshot_id)`. Thus an operator may retain one role and reject another without losing the shared snapshot from resume/indexing.

## Codex Review automated suggestions

Across the two automated-review rounds, six substantive inline findings have now been identified and remediated:

1. exclude rejected direct-retention snapshots without depending on nullable candidate identity;
2. reject terminal durable action identities when ensuring pending work;
3. serialize public action timestamps to the declared schema;
4. revalidate resolved curation against the current post-curation census;
5. compare pending-action authority with the run revision reloaded under lock;
6. preserve a snapshot when at least one role-specific subject remains non-rejected.

All six have production fixes and direct regressions. Any review thread or approval bound to a pre-remediation head is historical; a fresh exact-head review remains required after host evidence.

## Test and documentation gaps

### Curation restart rejection fixture

The prior fixture did not exercise the dangerous case because only the retained snapshot was bound to an extraction attempt. It now binds every curated snapshot, including the rejected direct-retention subject with null candidate identity, so the production SQL defect is directly falsifiable.

### Installed-wheel migration inventory

`test_wheel_contains_only_canonical_runtime_modules()` explicitly requires `firecrawl_skill/research_store/alembic/versions/0045_operator_actions.py` in addition to the generic package-data and Alembic-head checks. This removes the previous documentary/test inventory ambiguity.

### Sanctioned-host wheel build frontend

The first exact-head sanctioned assessment at `8c16f6846b6b1d838018326496bc3cbde86d02d3` produced `HOST_EVIDENCE_RESULT=FAIL` solely because the candidate packaging contract invoked `sys.executable -m pip wheel`. The assessment runner intentionally creates its candidate virtual environment with `uv venv` and provisions locked dependencies with `uv pip sync`; a `pip` module is therefore not part of the candidate-interpreter contract.

The wheel contract now prefers the sanctioned `uv` executable when it is present, using `uv build --wheel --out-dir ... --python <candidate interpreter> <repository>`. Environments without `uv` retain the existing `python -m pip wheel --no-deps` portability path. Both frontends invoke the declared project build backend and feed the same wheel-content, forbidden-path, installed-import, and migration-inventory assertions. No runner, assessment profile, lock file, baseline, `pyproject.toml`, or service policy is changed for this compatibility repair.

The `pr322-8c16f684` host assessment remains historical FAIL evidence and must not be reinterpreted. A fresh sanctioned assessment is required against the exact post-repair head.

### Fresh Codex regressions

The integration suite now also proves that: (1) adding a retained subject after a curation resolution invalidates that resolution and produces a replacement action; (2) a stale controller status cannot supersede a valid action created at the newer locked revision; and (3) a shared snapshot remains resumable when one role is retained and another role is rejected.

### Independent exact-head review: close the curation revalidation/indexing TOCTOU

A fresh Central review after host evidence identified a residual interleaving not covered by the sequential census regression. `curation_completed()` validates while holding the run lock only for its own transaction; after that transaction releases the lock, a new retained/extracted subject can be persisted before `run_resume()` reads `state_port.assets()` in `indexing`. Rechecking the census alone therefore did not make the operator selection authoritative at the actual resume projection boundary.

`resume_assets_for_run()` now binds the projection to the latest resolved current-policy curation action when one exists. A snapshot is resumable only if at least one subject explicitly retained by that durable action still survives in a non-rejected stage. Autonomous runs with no resolved curation action retain the prior projection semantics. This composes with the role-preservation rule: a shared snapshot remains available when its selected subject survives, but a newly added unselected role cannot resurrect a snapshot the operator did not retain.

`test_resolved_curation_selection_bounds_resume_after_late_role_addition` models the race boundary directly by resolving curation, adding a new retained role for the rejected snapshot, and reading resume assets without first creating a replacement action. The projection must remain limited to the originally selected snapshot while `curation_completed()` independently reports that a fresh action is required.

### Exact-head documentation

The PR description must be rebound to the resulting remediation SHA. Any prior CI, review, or local assessment evidence is historical once a remediation commit advances the branch.

## Local-agent handoff boundary

No local PASS is claimed by this note. After the exact remediation head is published, the sanctioned deterministic PR-head gateway must be run against that exact SHA. `HOST_EVIDENCE_RESULT` remains host-owned evidence and `GATE_DECISION` remains Central-owned. Any later head movement invalidates that host evidence.
