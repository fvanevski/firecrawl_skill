#!/usr/bin/env python3
"""Apply final RC-7 documentation/test corrections, then remove this script."""

from pathlib import Path


def update_readme() -> None:
    path = Path("README.md")
    text = path.read_text(encoding="utf-8")
    old = (
        "Content-addressed blobs retain immutable payloads, Qdrant supplies a "
        "rebuildable dense-retrieval projection, Valkey provides optional worker "
        "wakeups, and scratch directories remain local operational diagnostics."
    )
    new = (
        "Content-addressed blobs retain immutable payloads, Qdrant supplies a "
        "rebuildable dense-retrieval projection, and Valkey provides optional "
        "worker wakeups. Secure temporary files may exist only for bounded "
        "in-process operations; no scratch directory or local manifest is "
        "workflow, replay, history, or corpus authority."
    )
    if old in text:
        text = text.replace(old, new, 1)
    if "## Authoritative live validation\n" not in text:
        anchor = (
            "Explicit `DATABASE_URL`, Qdrant/Valkey endpoints and keys, blob root, "
            "and `FIRECRAWL_RESEARCH_PYTHON` take precedence over values loaded by "
            "`scripts/research-env`.\n\n"
        )
        section = "\n".join(
            (
                "## Authoritative live validation",
                "",
                "`scripts/live_validate.py` exercises public wrappers against disposable authoritative runs and derives its verdict from PostgreSQL records, immutable `BLOB_ROOT` payloads, exact-run index jobs, and the active Qdrant alias. An empty corpus or empty expected Qdrant set is not successful coverage. The smart-search resume case deliberately stops at a nonterminal `extracting` checkpoint and then restarts from persisted records and blob payloads.",
                "",
                "```bash",
                "scripts/live_validate.py --profile focused --max-operations 40",
                "scripts/live_validate.py --profile failure-path --max-operations 20",
                "scripts/live_validate.py --profile full --max-operations 100",
                "scripts/live_validate.py --profile focused --artifact-root ./validation-artifacts",
                "```",
                "",
                "Every Firecrawl subprocess, including transport retries, consumes one operation from a file-locked hard cap. The validator fails on retained monitored-`TMPDIR` entries, missing required authoritative planning or corpus records, invalid blobs, incomplete exact-run jobs, or incomplete Qdrant chunk coverage. Final report files are opt-in outputs and are never runtime inputs.",
                "",
                "",
            )
        )
        if anchor not in text:
            raise RuntimeError("README validation insertion anchor not found")
        text = text.replace(anchor, anchor + section, 1)
    path.write_text(text, encoding="utf-8")


def update_skill() -> None:
    path = Path("SKILL.md")
    text = path.read_text(encoding="utf-8")
    text = text.replace(
        "while complete progress and scratch-directory output remain visible",
        "while complete progress and bounded diagnostics remain visible",
    )
    row = "| `scripts/live_validate.py` | Run bounded authoritative wrapper, restart, blob, worker, Qdrant, and Valkey-loss validation | PostgreSQL/Qdrant-derived verdict and optional final report artifacts |\n"
    anchor = "| `scripts/finspect` | List, replay, inspect, and retrieve bounded retained records | Bounded JSON/console output |\n"
    if row not in text:
        if anchor not in text:
            raise RuntimeError("SKILL script-table anchor not found")
        text = text.replace(anchor, anchor + row, 1)
    path.write_text(text, encoding="utf-8")


def update_runbook() -> None:
    path = Path("references/operations-runbook.md")
    text = path.read_text(encoding="utf-8")
    if "## Authoritative live-validation campaigns\n" not in text:
        section = "\n".join(
            (
                "",
                "",
                "## Authoritative live-validation campaigns",
                "",
                "Run live validation only after `research-db ingest-ready` succeeds and the configured Qdrant active alias matches the current embedding fingerprint. The validator creates disposable PostgreSQL runs and never consumes reports, manifests, or temporary paths as workflow state.",
                "",
                "```bash",
                "scripts/live_validate.py --profile focused --max-operations 40",
                "scripts/live_validate.py --profile failure-path --max-operations 20",
                "scripts/live_validate.py --profile full --max-operations 100",
                "```",
                "",
                "Use `--blob-root` for a nondefault immutable payload store. Use `--artifact-root` only to export the final `manifest.json` and `report.md`. Each Firecrawl process attempt consumes the hard operation budget. A campaign fails closed on retained `TMPDIR` entries, missing required smart-planning provenance or corpus identities, invalid blob digests, incomplete exact-run index jobs, incompatible Qdrant alias/schema, incomplete point coverage, or failure to observe the persisted nonterminal restart checkpoint.",
            )
        )
        text = text.rstrip() + section
    path.write_text(text.rstrip() + "\n", encoding="utf-8")


def update_test_fixture() -> None:
    path = Path("scripts/test_authoritative_smart_validation.py")
    text = path.read_text(encoding="utf-8")
    needle = '        qdrant_api_key="",\n        max_operations=10,\n'
    replacement = (
        '        qdrant_api_key="",\n'
        '        blob_root="/tmp/test-authoritative-blobs",\n'
        '        max_operations=10,\n'
    )
    if needle in text:
        text = text.replace(needle, replacement, 1)
    path.write_text(text, encoding="utf-8")


def main() -> None:
    update_readme()
    update_skill()
    update_runbook()
    update_test_fixture()
    Path(".rc7-doc-trigger").unlink(missing_ok=True)
    Path(__file__).unlink()


if __name__ == "__main__":
    main()
