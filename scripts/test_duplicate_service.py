"""Unit tests for DuplicateGroupService.

Covers:
- Exact duplicate detection by content hash.
- Syndication detection by normalized title.
- Canonical URL matching.
- False-positive rejection of short titles.
- UNASSESSED tracking for candidates that fall through all criteria.
"""

from __future__ import annotations

import uuid

from research_domain.models import IndependenceStatus
from research_store.duplicate_service import DuplicateGroupService


class TestDuplicateGroupService:
    """Tests for DuplicateGroupService grouping logic."""

    def test_exact_content_hash_match(self):
        """Two candidates with identical content_hash are grouped DEPENDENT."""
        svc = DuplicateGroupService()
        c1 = {
            "id": uuid.uuid4(),
            "canonical_url": "https://example.com/1",
            "title": "Unique Title 1",
            "backend_metadata": {"content_hash": "abc"},
        }
        c2 = {
            "id": uuid.uuid4(),
            "canonical_url": "https://example.com/2",
            "title": "Unique Title 2",
            "backend_metadata": {"content_hash": "abc"},
        }

        result = svc.evaluate_candidates([c1, c2])
        groups = result["groups"]
        assert len(groups) == 1
        g = groups[0]
        assert g["rationale"] == "exact_content_hash_match"
        assert len(g["candidate_ids"]) == 2
        assert c1["id"] in g["candidate_ids"]
        assert c2["id"] in g["candidate_ids"]
        assert g["assessments"][c1["id"]]["status"] == IndependenceStatus.DEPENDENT
        assert g["assessments"][c2["id"]]["status"] == IndependenceStatus.DEPENDENT
        assert len(result["unassessed"]) == 0

    def test_syndicated_title_match(self):
        """Similar titles on different domains trigger syndication detection."""
        svc = DuplicateGroupService()
        c1 = {
            "id": uuid.uuid4(),
            "canonical_url": "https://sourceA.com/news",
            "title": "Breaking News: Major Event Happens Today",
        }
        c2 = {
            "id": uuid.uuid4(),
            "canonical_url": "https://sourceB.com/news",
            "title": "Breaking News Major Event Happens Today!",
        }

        result = svc.evaluate_candidates([c1, c2])
        groups = result["groups"]
        assert len(groups) == 1
        g = groups[0]
        assert g["rationale"] == "likely_syndicated_title_match"
        assert len(g["candidate_ids"]) == 2
        # Primary candidate is UNCERTAIN for syndication
        assert g["assessments"][c1["id"]]["status"] == IndependenceStatus.UNCERTAIN
        assert g["assessments"][c2["id"]]["status"] == IndependenceStatus.UNCERTAIN
        assert len(result["unassessed"]) == 0

    def test_short_title_no_false_positive(self):
        """Short normalized titles (<12 chars) do not trigger syndication."""
        svc = DuplicateGroupService()
        c1 = {
            "id": uuid.uuid4(),
            "canonical_url": "https://sourceA.com/a",
            "title": "Short title",
        }
        c2 = {
            "id": uuid.uuid4(),
            "canonical_url": "https://sourceB.com/b",
            "title": "Short title",
        }

        result = svc.evaluate_candidates([c1, c2])
        assert len(result["groups"]) == 0
        assert len(result["unassessed"]) == 2

    def test_canonical_url_match(self):
        """Candidates sharing the same canonical_url are grouped DEPENDENT."""
        svc = DuplicateGroupService()
        c1 = {
            "id": uuid.uuid4(),
            "canonical_url": "https://sourceA.com/a",
            "title": "Different Title 1",
        }
        c2 = {
            "id": uuid.uuid4(),
            "canonical_url": "https://sourceA.com/a",
            "title": "Different Title 2",
        }

        result = svc.evaluate_candidates([c1, c2])
        groups = result["groups"]
        assert len(groups) == 1
        assert groups[0]["rationale"] == "canonical_url_match"
        assert len(result["unassessed"]) == 0

    def test_unassessed_candidates_tracked(self):
        """Candidates that fall through all criteria appear in unassessed."""
        svc = DuplicateGroupService()
        c1 = {
            "id": uuid.uuid4(),
            "canonical_url": "https://example.com/1",
            "title": "Completely Unique Title For This Source",
        }
        c2 = {
            "id": uuid.uuid4(),
            "canonical_url": "https://example.com/2",
            "title": "Another Completely Unique Title",
        }

        result = svc.evaluate_candidates([c1, c2])
        assert len(result["groups"]) == 0
        assert len(result["unassessed"]) == 2
        assert c1["id"] in result["unassessed"]
        assert c2["id"] in result["unassessed"]

    def test_empty_candidates(self):
        """Empty candidate list returns empty groups and unassessed."""
        svc = DuplicateGroupService()
        result = svc.evaluate_candidates([])
        assert result["groups"] == []
        assert result["unassessed"] == []

    def test_mixed_hash_and_no_match(self):
        """Candidates with matching hash are grouped; others are unassessed."""
        svc = DuplicateGroupService()
        c1 = {
            "id": uuid.uuid4(),
            "canonical_url": "https://example.com/1",
            "title": "Title A",
            "backend_metadata": {"content_hash": "hash1"},
        }
        c2 = {
            "id": uuid.uuid4(),
            "canonical_url": "https://example.com/2",
            "title": "Title B",
            "backend_metadata": {"content_hash": "hash1"},
        }
        c3 = {
            "id": uuid.uuid4(),
            "canonical_url": "https://example.com/3",
            "title": "Unique Title C",
        }

        result = svc.evaluate_candidates([c1, c2, c3])
        assert len(result["groups"]) == 1
        assert len(result["unassessed"]) == 1
        assert c3["id"] in result["unassessed"]

    def test_false_positive_long_titles_no_group(self):
        """Two candidates with long but unrelated titles must NOT be grouped.

        This validates the false-positive rejection path for issue #57:
        candidates that coincidentally share a long normalized title substring
        but have different content_hash and different canonical_url should
        remain ungrouped.
        """
        svc = DuplicateGroupService()
        c1 = {
            "id": uuid.uuid4(),
            "canonical_url": "https://docs.example.com/api/v2",
            "title": "API Reference Guide for Version 2.0 Release",
            "backend_metadata": {"content_hash": "hash_unique_a"},
        }
        c2 = {
            "id": uuid.uuid4(),
            "canonical_url": "https://blog.example.com/changelog",
            "title": "Changelog Notes for Version 2.0 API Release",
            "backend_metadata": {"content_hash": "hash_unique_b"},
        }

        result = svc.evaluate_candidates([c1, c2])
        assert len(result["groups"]) == 0, (
            "Unrelated candidates with different content_hash and canonical_url "
            "must not be grouped even if titles share long substrings"
        )
        assert len(result["unassessed"]) == 2
