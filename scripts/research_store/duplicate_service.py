import re
from collections import defaultdict
from uuid import uuid4

from research_domain.models import IndependenceStatus


class DuplicateGroupService:
    """Service to evaluate near-duplicates and source independence deterministically."""

    def __init__(self, uow_factory=None):
        self.uow_factory = uow_factory

    def evaluate_candidates(self, candidates, run_id=None):
        """Evaluate candidates to find duplicate groups and assess independence.

        Args:
            candidates: list of candidate dicts
            run_id: current run_id

        Returns:
            list of groups, where each group is a dict:
            {
                "group_id": UUID,
                "candidate_ids": list of UUID,
                "rationale": str,
                "assessments": dict mapping candidate_id -> dict(status, rationale)
            }
        """
        if not candidates:
            return []

        # Grouping keys
        by_hash = defaultdict(list)
        by_canonical = defaultdict(list)
        by_title_normalized = defaultdict(list)

        for c in candidates:
            cid = c["candidate_id"] if "candidate_id" in c else c["id"]

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
            norm_title = re.sub(r"[^a-z0-9]", "", title.lower())
            if norm_title and len(norm_title) > 10:
                by_title_normalized[norm_title].append(c)

        assigned = set()
        groups = []

        def create_group(cands, rationale, status_override=None):
            group_cands = [c for c in cands if c["id"] not in assigned]
            if len(group_cands) < 2:
                return

            group_id = uuid4()
            c_ids = []
            assessments = {}

            for i, c in enumerate(group_cands):
                cid = c["id"]
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

        # Remaining candidates are UNASSESSED or UNCERTAIN independent
        # We don't group them, but we might want to return their assessments?
        # The requirement asks to persist duplicate groups and independence assessments.

        # Optional: Save to DB if uow_factory is provided
        if self.uow_factory and run_id:
            uow = self.uow_factory()
            if uow is not None:
                with uow:
                    for group in groups:
                        uow.runs.persist_duplicate_group(
                            group["group_id"], run_id, group["rationale"]
                        )
                        uow.runs.assign_duplicate_group(
                            group["candidate_ids"], group["group_id"], run_id
                        )
                        for cid, ass in group["assessments"].items():
                            uow.runs.update_candidate_independence(cid, ass)

        return groups
