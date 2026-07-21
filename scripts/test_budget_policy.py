from __future__ import annotations

# ruff: noqa: E402 - load the sibling script package without installation.

from dataclasses import replace
import json
from pathlib import Path
import sys

import pytest


SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))

from budget_policy import BudgetPolicy, BudgetPolicyError, DEFAULT_POLICY
from research_domain import load_model
from research_domain.models import RiskLevel


VALID = json.loads(
    (ROOT / "tests" / "fixtures" / "research_domain" / "valid.json").read_text()
)


def base_spec():
    spec = load_model(VALID["research-spec-v1"])
    return replace(
        spec,
        risk_level=RiskLevel.LOW,
        research_archetype="direct_fact",
        claims_to_validate=(),
        freshness_requirements=(),
        required_source_classes=(),
        corroboration_requirements=(),
        contradiction_requirements=(),
    )


@pytest.mark.parametrize(
    ("mutation", "tier", "rule_id"),
    [
        ({}, "focused", None),
        ({"risk_level": RiskLevel.MEDIUM}, "standard", "tier.medium_risk"),
        ({"risk_level": RiskLevel.HIGH}, "intensive", "tier.high_risk"),
        (
            {"research_archetype": "breaking_news"},
            "standard",
            "tier.archetype_floor",
        ),
    ],
)
def test_policy_table_maps_semantic_spec_to_expected_tier(mutation, tier, rule_id):
    snapshot = DEFAULT_POLICY.evaluate(
        replace(base_spec(), **mutation), spec_revision=1, run_revision=0
    )
    assert snapshot.selected_tier == tier
    assert snapshot.effective_caps == DEFAULT_POLICY.profiles[tier]
    if rule_id:
        assert rule_id in {item.rule_id for item in snapshot.matched_rules}


def test_topic_length_and_word_count_do_not_select_budget():
    short = base_spec()
    long = replace(short, objective="same semantic request " * 500)
    short_snapshot = DEFAULT_POLICY.evaluate(short, spec_revision=1, run_revision=0)
    long_snapshot = DEFAULT_POLICY.evaluate(long, spec_revision=1, run_revision=0)
    assert short_snapshot.selected_tier == long_snapshot.selected_tier
    assert short_snapshot.effective_caps == long_snapshot.effective_caps


def test_user_limits_only_tighten_policy_caps():
    snapshot = DEFAULT_POLICY.evaluate(
        base_spec(),
        spec_revision=1,
        run_revision=0,
        user_limits={"max_search_branches": 1, "max_successful_extractions": 2},
    )
    assert snapshot.policy_caps.max_search_branches == 2
    assert snapshot.effective_caps.max_search_branches == 1
    assert snapshot.effective_caps.max_successful_extractions == 2


def test_user_limit_cannot_loosen_or_name_unknown_resource():
    with pytest.raises(BudgetPolicyError) as error:
        DEFAULT_POLICY.evaluate(
            base_spec(),
            spec_revision=1,
            run_revision=0,
            user_limits={"max_search_branches": 999, "unknown": 1},
        )
    assert {item.rule_id for item in error.value.rejections} == {
        "user_limit.not_stricter.max_search_branches",
        "user_limit.unknown_resource",
    }


def test_boundary_is_accepted_and_over_budget_records_each_policy_rule():
    snapshot = DEFAULT_POLICY.evaluate(base_spec(), spec_revision=1, run_revision=0)
    boundary = snapshot.effective_caps.to_dict()
    assert DEFAULT_POLICY.authorize(snapshot, boundary).accepted

    proposal = dict(boundary)
    proposal["max_search_branches"] += 1
    proposal["max_llm_calls"] += 1
    decision = DEFAULT_POLICY.authorize(snapshot, proposal)
    assert not decision.accepted
    assert [item.rule_id for item in decision.rejections] == [
        "budget.max_llm_calls",
        "budget.max_search_branches",
    ]
    assert all(item.proposed == item.limit + 1 for item in decision.rejections)


def test_policy_snapshot_is_versioned_and_configuration_bound():
    snapshot = DEFAULT_POLICY.evaluate(base_spec(), spec_revision=2, run_revision=3)
    payload = snapshot.to_dict()
    assert payload["policy_version"] == "budget-policy-v1"
    assert len(payload["policy_config_sha256"]) == 64
    assert payload["spec_revision"] == 2
    assert payload["run_revision"] == 3

    changed = json.loads(
        (ROOT / "references" / "budget-policy-v1.json").read_text(encoding="utf-8")
    )
    changed["profiles"]["focused"]["max_search_branches"] += 1
    assert BudgetPolicy(changed).config_sha256 != DEFAULT_POLICY.config_sha256
