import uuid

from research_domain.models import IndependenceStatus
from research_store.duplicate_service import DuplicateGroupService


def test_duplicate_group_service_exact_hash():
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

    groups = svc.evaluate_candidates([c1, c2])
    assert len(groups) == 1
    g = groups[0]
    assert g["rationale"] == "exact_content_hash_match"
    assert len(g["candidate_ids"]) == 2
    assert c1["id"] in g["candidate_ids"]
    assert c2["id"] in g["candidate_ids"]
    assert g["assessments"][c2["id"]]["status"] == IndependenceStatus.DEPENDENT
    assert g["assessments"][c1["id"]]["status"] == IndependenceStatus.DEPENDENT


def test_duplicate_group_service_syndicated():
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

    groups = svc.evaluate_candidates([c1, c2])
    assert len(groups) == 1
    g = groups[0]
    assert g["rationale"] == "likely_syndicated_title_match"
    assert len(g["candidate_ids"]) == 2
    assert g["assessments"][c2["id"]]["status"] == IndependenceStatus.UNCERTAIN


def test_duplicate_group_service_false_positives():
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

    # Normalized title is too short to be considered syndication
    groups = svc.evaluate_candidates([c1, c2])
    assert len(groups) == 0


def test_duplicate_group_service_canonical_url():
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

    groups = svc.evaluate_candidates([c1, c2])
    assert len(groups) == 1
    assert groups[0]["rationale"] == "canonical_url_match"
