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

## Codex Review automated suggestions

The complete Codex review inventory for PR #322 contained exactly three substantive inline suggestions:

1. exclude rejected snapshots without requiring candidate identity;
2. reject terminal rows when ensuring a new action;
3. serialize action timestamps in the public contract.

All three are addressed in production code and have direct regressions above. Their GitHub review threads should be replied to with the exact remediation commit and resolved after the branch update. A fresh exact-head review remains required after local host evidence because the original automated review was bound to an earlier head.

## Test and documentation gaps

### Curation restart rejection fixture

The prior fixture did not exercise the dangerous case because only the retained snapshot was bound to an extraction attempt. It now binds every curated snapshot, including the rejected direct-retention subject with null candidate identity, so the production SQL defect is directly falsifiable.

### Installed-wheel migration inventory

`test_wheel_contains_only_canonical_runtime_modules()` now explicitly requires `firecrawl_skill/research_store/alembic/versions/0045_operator_actions.py` in addition to the generic package-data and Alembic-head checks. This removes the previous documentary/test inventory ambiguity.

### Exact-head documentation

The PR description must be rebound to the resulting remediation SHA and must not retain the stale statement that `c87d7e2` is the current candidate. Any prior CI or local assessment evidence is historical once this remediation commit advances the branch.

## Local-agent handoff boundary

No local result is claimed by this note. After the exact remediation head is published, the sanctioned deterministic PR-head gateway must be run against that exact SHA. `HOST_EVIDENCE_RESULT` remains host-owned evidence and `GATE_DECISION` remains Central-owned. Any later head movement invalidates that host evidence.
