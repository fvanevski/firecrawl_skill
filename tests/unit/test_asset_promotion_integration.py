"""Temporary #291 relocation tombstone.

Canonical PostgreSQL integration coverage moved to
`tests/integration/test_asset_promotion_integration.py`.
The focused GitHub content-write surface cannot delete files; local handoff must
remove this non-collecting tombstone with native `git rm` before final evidence.
"""
