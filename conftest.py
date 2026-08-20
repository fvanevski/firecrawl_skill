from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
SCRIPTS = _ROOT / "scripts"
SRC = _ROOT / "src"
# The canonical firecrawl_skill package lives under src/; the legacy research
# root is removed, so both src (canonical package) and scripts (test support,
# CLI shims) must resolve.
for _p in (SRC, SCRIPTS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

pytest_plugins = ("qdrant_test_support",)

# The research-store session fixtures, the append-only recovery-test hook, and
# the DB-migration fixture live in scripts/conftest.py. That file stays in
# scripts/ so its pinned pyrefly baseline remains valid, but pytest only
# auto-loads conftest files on the path from rootdir to a test file, and the
# tests now live under tests/. Importing scripts/conftest.py under its own
# module name "conftest" collides with this root conftest's import identity, so
# load it by file path under a unique module name and re-export the fixtures,
# hook, and helpers that the relocated tests rely on.
_spec = importlib.util.spec_from_file_location(
    "_research_store_conftest", SCRIPTS / "conftest.py"
)
if _spec is None or _spec.loader is None:
    raise RuntimeError("unable to load scripts/conftest.py")
_conftest = importlib.util.module_from_spec(_spec)
sys.modules["_research_store_conftest"] = _conftest
_spec.loader.exec_module(_conftest)

_conftest_globals = vars(_conftest)
for _name in (
    "TEST_DSN",
    "ensure_run_exists",
    "ensure_passage_and_snapshot_exist",
    "prepared_database_for_claims",
    "prepared_database_for_evidence_packets",
    "pytest_runtest_setup",
    "_apply_db_schema",
):
    globals()[_name] = _conftest_globals[_name]
