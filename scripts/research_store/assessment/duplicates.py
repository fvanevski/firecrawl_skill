"""Deterministic near-duplicate and source-independence grouping."""

from __future__ import annotations

import re
from collections import defaultdict
from uuid import uuid4

from research_domain.models import IndependenceStatus

_SYNDICATION_TITLE_MIN_LENGTH = 12


class DuplicateGroupService:
    """Evaluate near-duplicates and source independence deterministically."""

    def evaluate_candidates(self, candidates):
        if not candidates:
            return {"groups": [], "unassessed": []}

        by_hash = defaultdict(list)
        by_canonical = defaultdict(list)
        by_title_normalized = defaultdict(list)

        for candidate in candidates:
            meta = candidate.get("backend_metadata", {})
            content_hash = meta.get("content_hash")
            if content_hash:
                by_hash[content_hash].append(candidate)
            canonical = candidate.get("canonical_url", "")
            if canonical:
                by_canonical[canonical].append(candidate)
            title = candidate.get("title") or ""
            normalized_title = re.sub(r"[^a-z0-9\s]", "", title.lower())
            if (
                normalized_title
                and len(normalized_title) >= _SYNDICATION_TITLE_MIN_LENGTH
            ):
                by_title_normalized[normalized_title].append(candidate)

        assigned = set()
        groups = []

        def create_group(candidates_in_group, rationale, status_override=None):
            group_candidates = [
                candidate
                for candidate in candidates_in_group
                if candidate.get("id", candidate.get("candidate_id")) not in assigned
            ]
            if len(group_candidates) < 2:
                return
            group_id = uuid4()
            candidate_ids = []
            assessments = {}
            for index, candidate in enumerate(group_candidates):
                candidate_id = candidate.get("id", candidate.get("candidate_id"))
                candidate_ids.append(candidate_id)
                assigned.add(candidate_id)
                if index == 0:
                    status = (
                        IndependenceStatus.UNCERTAIN
                        if status_override is None
                        else status_override
                    )
                    assessments[candidate_id] = {
                        "status": status,
                        "rationale": "primary candidate in duplicate group",
                    }
                else:
                    status = status_override or IndependenceStatus.DEPENDENT
                    assessments[candidate_id] = {
                        "status": status,
                        "rationale": rationale,
                    }
            groups.append(
                {
                    "group_id": group_id,
                    "candidate_ids": candidate_ids,
                    "rationale": rationale,
                    "assessments": assessments,
                }
            )

        for candidates_in_group in by_hash.values():
            create_group(
                candidates_in_group,
                "exact_content_hash_match",
                IndependenceStatus.DEPENDENT,
            )
        for candidates_in_group in by_canonical.values():
            create_group(
                candidates_in_group,
                "canonical_url_match",
                IndependenceStatus.DEPENDENT,
            )
        for candidates_in_group in by_title_normalized.values():
            create_group(
                candidates_in_group,
                "likely_syndicated_title_match",
                IndependenceStatus.UNCERTAIN,
            )

        unassessed = [
            candidate.get("id", candidate.get("candidate_id"))
            for candidate in candidates
            if candidate.get("id", candidate.get("candidate_id")) not in assigned
        ]
        return {"groups": groups, "unassessed": unassessed}


__all__ = ["DuplicateGroupService"]
