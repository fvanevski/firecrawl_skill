"""Superseded contract tombstone.

The constructed-outcome release checks that formerly lived in this module were
replaced by ``test_release_invariant_contracts.py`` and the corresponding
PostgreSQL integration authorities. Keeping the old module under a module-level
pytest skip marker left eighteen unknown skips in the final Phase-5 full-suite
census, so it is intentionally test-free rather than skipped.

Canonical authority is deliberately split by boundary:

- host semantic authority, metric completeness/status, recommendation policy,
  strict CLI behavior, canonical source matching, and release preflight are
  exercised in ``test_release_invariant_contracts.py`` against production
  decision surfaces;
- citation validity overriding mere evidence-link existence is exercised by
  ``test_claims_evidence.py::test_citation_pass_validation_overrides_existing_evidence_link``
  against PostgreSQL-backed citation-pass evidence;
- cache-event isolation under identical keys and overlapping timestamps is
  exercised by
  ``test_performance_telemetry_integration.py::TestOverlappingCampaignCacheIsolation::test_overlapping_events_remain_exactly_run_scoped``.

One historical constructed expectation is intentionally *not* preserved:
``deterministic_debug`` with a genuinely non-invoked model now records token
usage as ``NOT_APPLICABLE`` and may satisfy that metric. The canonical regression
``test_not_invoked_tokens_are_not_applicable_and_can_satisfy_release`` protects
that current policy. Restoring the superseded expectation would be a regression,
not additional coverage.

Do not add tests here. Add release-policy regressions to the canonical
production-boundary or integration owners instead. ``test_test_topology.py``
mechanically verifies that the named canonical authorities continue to exist.
"""

SUPERSEDED_CONTRACT_TOMBSTONE = "Superseded contract tombstone."
