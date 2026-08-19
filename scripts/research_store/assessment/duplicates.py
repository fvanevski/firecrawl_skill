"""Deterministic near-duplicate and source-independence grouping.

This module provides :class:`DuplicateGroupService`, which evaluates search
candidates to detect exact duplicates (by content hash or canonical URL) and
likely syndicated copies (by normalized title similarity).  It returns
structured assessments that feed into the ``EvidencePacket`` so that
corroboration counts cannot be inflated by repeated sources.

Grouping priority
-----------------
1. **Exact content hash** — candidates with identical ``backend_metadata.content_hash``
2. **Canonical URL** — candidates sharing the same ``canonical_url``
3. **Normalized title** — candidates with matching titles after stripping
   non-alphanumeric characters (syndication / wire-report detection)

Independence statuses
---------------------
- ``DEPENDENT`` — exact hash or canonical URL match
- ``UNCERTAIN`` — likely syndicated title match (primary candidate)
- ``UNASSESSED`` — no grouping signal found (candidates that fall through
  all criteria are returned in the ``unassessed`` key of the result dict)
"""

from __future__ import annotations

import re
from collections import defaultdict
from uuid import uuid4

from research_domain.models import IndependenceStatus

# Minimum length of a normalized title to trigger syndication detection.
# Short titles (e.g. "Short title" → ~10 chars) produce too many false
# positives; the threshold is deliberately conservative.
_SYNDICATION_TITLE_MIN_LENGTH = 12


class DuplicateGroupService:
    """Evaluate near-duplicates and source independence deterministically.

    This class is **pure-functional** and **in-memory only**.  It never
    touches the database.  The ``evaluate_candidates`` method returns a
    dict with ``groups`` and ``unassessed`` that the caller uses to
    populate the ``EvidencePacket`` (via ``EvidenceService``) and, when
    desired, to persist duplicate-group rows via
    ``PostgresUnitOfWork.assign_duplicate_group`` or
    ``PostgresUnitOfWork.persist_duplicate_group``.

    Database persistence is handled separately:
    - ``assign_duplicate_group(candidate_ids, group_id, run_id)`` creates
      a ``duplicate_groups`` row and links candidates.
    - ``persist_duplicate_group(group_id, run_id, rationale)`` upserts
      a ``duplicate_groups`` row.
    - ``update_candidate_independence(candidate_id, assessment_dict)``
      writes the ``independence_assessment`` JSONB column on
      ``search_candidates``.

    The evidence service path (``EvidenceService.build_evidence_packet``)
    calls ``evaluate_candidates`` **only** to populate the in-memory
    packet fields; it does not create ``duplicate_groups`` rows.
    """

    def evaluate_candidates(self, candidates):
        """Evaluate candidates to find duplicate groups and assess independence.

        Args:
            candidates: list of candidate dicts

        Returns:
            A dict with two keys:

            ``groups`` — list of group dicts, each containing:

                * ``group_id`` (UUID)
                * ``candidate_ids`` (list of UUID)
                * ``rationale`` (str)
                * ``assessments`` (dict mapping candidate_id → {status, rationale})

            ``unassessed`` — list of candidate IDs that fell through all
            grouping criteria (no duplicate or syndication signal).
        """
        if not candidates:
            return {"groups": [], "unassessed": []}

        # Grouping keys
        by_hash = defaultdict(list)
        by_canonical = defaultdict(list)
        by_title_normalized = defaultdict(list)

        for c in candidates:
            # 1. Content hashes (if available in backend_metadata)
            meta = c.get("backend_metadata", {})
            content_hash = meta.get("content_hash")
            if content_hash:
                by_hash[content_hash].append(c)

            # 2. Canonical URL
            canonical = c.get("canonical_url", "")
            if canonical:
                by_canonical[canonical].append(c)

            # 3. Normalized Title (for syndication/wire reports)
            title = c.get("title") or ""
            norm_title = re.sub(r"[^a-z0-9\s]", "", title.lower())
            if norm_title and len(norm_title) >= _SYNDICATION_TITLE_MIN_LENGTH:
                by_title_normalized[norm_title].append(c)

        assigned = set()
        groups = []

        def create_group(cands, rationale, status_override=None):
            group_cands = [
                c for c in cands if c.get("id", c.get("candidate_id")) not in assigned
            ]
            if len(group_cands) < 2:
                return

            group_id = uuid4()
            c_ids = []
            assessments = {}

            for i, c in enumerate(group_cands):
                cid = c.get("id", c.get("candidate_id"))
                c_ids.append(cid)
                assigned.add(cid)

                if i == 0:
                    status = (
                        IndependenceStatus.UNCERTAIN
                        if status_override is None
                        else status_override
                    )
                    assessments[cid] = {
                        "status": status,
                        "rationale": "primary candidate in duplicate group",
                    }
                else:
                    if status_override:
                        status = status_override
                    else:
                        status = IndependenceStatus.DEPENDENT

                    assessments[cid] = {"status": status, "rationale": rationale}

            groups.append(
                {
                    "group_id": group_id,
                    "candidate_ids": c_ids,
                    "rationale": rationale,
                    "assessments": assessments,
                }
            )

        # Process exact hashes first
        for cands in by_hash.values():
            create_group(
                cands, "exact_content_hash_match", IndependenceStatus.DEPENDENT
            )

        # Process canonical URLs
        for cands in by_canonical.values():
            create_group(cands, "canonical_url_match", IndependenceStatus.DEPENDENT)

        # Process normalized titles (likely syndication)
        for cands in by_title_normalized.values():
            create_group(
                cands, "likely_syndicated_title_match", IndependenceStatus.UNCERTAIN
            )

        # Track candidates that were not assigned to any group
        unassessed = [
            c.get("id", c.get("candidate_id"))
            for c in candidates
            if c.get("id", c.get("candidate_id")) not in assigned
        ]

        return {"groups": groups, "unassessed": unassessed}
