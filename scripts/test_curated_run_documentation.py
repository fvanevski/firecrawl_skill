"""Executable documentation contracts for issue #212."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _normalized(path: str) -> str:
    return " ".join(_read(path).split())


def _assert_ordered(content: str, markers: tuple[str, ...]) -> None:
    positions = [content.index(marker) for marker in markers]
    assert positions == sorted(positions)


def test_skill_canonical_direct_sequence_requires_explicit_boundaries() -> None:
    skill = _read("SKILL.md")
    normalized = " ".join(skill.split())
    _assert_ordered(
        normalized,
        (
            'frun" start "<research objective>"',
            "--run-mode curated",
            'frun" prepare "$RUN_ID"',
            '--research-run-id "$RUN_ID"',
            'frun" seal-acquisition "$RUN_ID"',
        ),
    )
    assert not re.search(r"--research-run(?:\s|\")", skill)


def test_curated_reference_uses_registered_wrapper_option() -> None:
    reference = _read("references/curated-run-lifecycle.md")
    normalized = " ".join(reference.split())
    assert "fsearch ... --research-run-id <fr_id>" in normalized
    assert "fscrape ... --research-run-id <fr_id>" in normalized
    assert not re.search(r"--research-run(?:\s|\")", reference)


def test_curated_reference_defines_interrupted_seal_repair() -> None:
    reference = _normalized("references/curated-run-lifecycle.md")
    for required in (
        "before an active membership seal exists",
        "does not start checkpoint processing",
        "`frun finish` fails closed",
        "without repeating the `extracting` or `indexing` transitions",
        "Only after an active seal exists",
    ):
        assert required in reference


def test_documented_production_provenance_surfaces_are_complete() -> None:
    reference = _normalized("references/curated-run-lifecycle.md")
    for required in (
        "production `fsearch` builder",
        "production `fscrape` path",
        "research_invocations.lifecycle_revision",
        "invocation JSONB metadata",
        "append-only `invocation_started` event",
        "`direct_scrape_started` event",
        "no invocation or start event is committed",
    ):
        assert required in reference


def test_documented_authority_and_compatibility_boundaries_remain_explicit() -> None:
    reference = _normalized("references/curated-run-lifecycle.md")
    for required in (
        "PostgreSQL as the authority",
        "Qdrant remains a rebuildable projection",
        "no migration infers or backfills historical mode",
        "subject belonging to another run is rejected",
        "never discovers candidates",
        "No DDL migration is required",
    ):
        assert required in reference
