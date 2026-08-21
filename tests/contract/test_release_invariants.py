"""Superseded contract tombstone.

The constructed-outcome release checks that formerly lived in this module were
replaced by ``test_release_invariant_contracts.py`` and the corresponding
PostgreSQL integration authorities.  Keeping the old module under a module-level
``pytest.mark.skip`` left eighteen unknown skips in the final Phase-5 full-suite
census, so it is intentionally test-free rather than skipped.

Do not add tests here.  Add release-policy regressions to the canonical
production-boundary or integration owners instead.
"""

SUPERSEDED_CONTRACT_TOMBSTONE = "Superseded contract tombstone."
