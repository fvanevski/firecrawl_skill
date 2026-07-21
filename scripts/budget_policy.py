"""Versioned deterministic resource policy for research workflow proposals."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
import hashlib
import json
from pathlib import Path
from typing import Mapping
from uuid import NAMESPACE_URL, uuid5

from research_domain.models import (
    CompletionCriterion,
    EvidenceRequirement,
    ExecutionMode,
    FreshnessRequirement,
    ResearchQuestion,
    ResearchSpec,
    RiskLevel,
    SourceRequirement,
    TimeWindow,
)


POLICY_PATH = Path(__file__).parents[1] / "references" / "budget-policy-v1.json"
TIER_ORDER = ("focused", "standard", "intensive")


@dataclass(frozen=True)
class ResourceCaps:
    max_search_branches: int
    results_per_branch: int
    max_extraction_attempts: int
    max_successful_extractions: int
    max_adaptive_cycles: int
    max_llm_calls: int
    max_input_tokens: int
    max_output_tokens: int
    max_retrieval_candidates: int
    max_reranker_candidates: int
    max_evidence_packet_tokens: int
    max_wall_clock_seconds: int

    def __post_init__(self):
        for item in fields(self):
            value = getattr(self, item.name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{item.name} must be a non-negative integer")
        if self.max_successful_extractions > self.max_extraction_attempts:
            raise ValueError(
                "max_successful_extractions cannot exceed max_extraction_attempts"
            )
        if self.max_reranker_candidates > self.max_retrieval_candidates:
            raise ValueError(
                "max_reranker_candidates cannot exceed max_retrieval_candidates"
            )

    @classmethod
    def from_mapping(cls, values: Mapping[str, int]) -> "ResourceCaps":
        expected = {item.name for item in fields(cls)}
        unknown = sorted(set(values) - expected)
        missing = sorted(expected - set(values))
        if unknown or missing:
            raise ValueError(
                f"invalid resource caps; missing={missing}, unknown={unknown}"
            )
        return cls(**{name: values[name] for name in expected})

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass(frozen=True)
class PolicyRuleMatch:
    rule_id: str
    minimum_tier: str


@dataclass(frozen=True)
class BudgetRejection:
    rule_id: str
    resource: str
    proposed: int
    limit: int
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class BudgetDecision:
    accepted: bool
    rejections: tuple[BudgetRejection, ...]

    def to_dict(self) -> dict:
        return {
            "accepted": self.accepted,
            "rejections": [item.to_dict() for item in self.rejections],
        }


@dataclass(frozen=True)
class BudgetSnapshot:
    snapshot_version: int
    policy_version: str
    policy_config_sha256: str
    research_spec_id: str
    spec_revision: int
    run_revision: int
    selected_tier: str
    semantic_inputs: dict
    matched_rules: tuple[PolicyRuleMatch, ...]
    policy_caps: ResourceCaps
    user_limits: dict[str, int]
    effective_caps: ResourceCaps

    def __post_init__(self):
        if self.snapshot_version != 1:
            raise ValueError("unsupported budget snapshot version")
        if self.spec_revision < 1 or self.run_revision < 0:
            raise ValueError("invalid spec or run revision")
        if self.selected_tier not in TIER_ORDER:
            raise ValueError("unsupported budget tier")

    def to_dict(self) -> dict:
        return {
            "snapshot_version": self.snapshot_version,
            "policy_version": self.policy_version,
            "policy_config_sha256": self.policy_config_sha256,
            "research_spec_id": self.research_spec_id,
            "spec_revision": self.spec_revision,
            "run_revision": self.run_revision,
            "selected_tier": self.selected_tier,
            "semantic_inputs": self.semantic_inputs,
            "matched_rules": [asdict(item) for item in self.matched_rules],
            "policy_caps": self.policy_caps.to_dict(),
            "user_limits": dict(sorted(self.user_limits.items())),
            "effective_caps": self.effective_caps.to_dict(),
        }


class BudgetPolicyError(ValueError):
    def __init__(self, message: str, rejections: tuple[BudgetRejection, ...]):
        super().__init__(message)
        self.rejections = rejections


class BudgetPolicy:
    """Map validated ResearchSpec semantics to immutable hard resource caps."""

    def __init__(self, config: dict):
        self.config = config
        self.policy_version = config.get("policy_version")
        if not isinstance(self.policy_version, str) or not self.policy_version:
            raise ValueError("policy_version must be nonempty")
        profiles = config.get("profiles")
        if not isinstance(profiles, dict) or set(profiles) != set(TIER_ORDER):
            raise ValueError(f"profiles must be exactly {list(TIER_ORDER)}")
        self.profiles = {
            tier: ResourceCaps.from_mapping(profiles[tier]) for tier in TIER_ORDER
        }
        self.rules = tuple(config.get("tier_rules", ()))
        canonical = json.dumps(
            config, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
        self.config_sha256 = hashlib.sha256(canonical).hexdigest()

    @classmethod
    def load(cls, path: Path = POLICY_PATH) -> "BudgetPolicy":
        return cls(json.loads(path.read_text(encoding="utf-8")))

    @staticmethod
    def semantic_inputs(spec: ResearchSpec) -> dict:
        return {
            "research_archetype": spec.research_archetype,
            "risk_level": spec.risk_level.value,
            "question_count": len(spec.questions),
            "claim_count": len(spec.claims_to_validate),
            "freshness_requirement_count": len(spec.freshness_requirements),
            "corroboration_requirement_count": len(spec.corroboration_requirements),
            "contradiction_requirement_count": len(spec.contradiction_requirements),
            "required_source_class_count": len(spec.required_source_classes),
            "required_source_minimum": sum(
                item.minimum_count for item in spec.required_source_classes
            ),
        }

    @staticmethod
    def _rule_matches(rule_id: str, inputs: dict) -> bool:
        semantic_scope = inputs["question_count"] + inputs["claim_count"]
        predicates = {
            "tier.high_risk": inputs["risk_level"] == RiskLevel.HIGH.value,
            "tier.expected_disagreement": inputs["contradiction_requirement_count"] > 0,
            "tier.large_semantic_scope": semantic_scope >= 6,
            "tier.broad_source_requirements": inputs["required_source_minimum"] >= 5,
            "tier.medium_risk": inputs["risk_level"] == RiskLevel.MEDIUM.value,
            "tier.freshness_sensitive": inputs["freshness_requirement_count"] > 0,
            "tier.multi_part_scope": semantic_scope >= 3,
            "tier.corroboration_required": inputs["corroboration_requirement_count"] > 0,
            "tier.multiple_source_classes": inputs["required_source_minimum"] >= 3,
            "tier.archetype_floor": inputs["research_archetype"]
            in {"breaking_news", "legislative_legal", "academic_debate"},
        }
        if rule_id not in predicates:
            raise ValueError(f"unsupported budget policy rule: {rule_id}")
        return predicates[rule_id]

    def evaluate(
        self,
        spec: ResearchSpec,
        *,
        spec_revision: int,
        run_revision: int,
        user_limits: Mapping[str, int] | None = None,
    ) -> BudgetSnapshot:
        if not isinstance(spec, ResearchSpec):
            raise TypeError("BudgetPolicy requires a validated ResearchSpec")
        inputs = self.semantic_inputs(spec)
        tier_index = 0
        matches = []
        for rule in self.rules:
            rule_id = rule.get("rule_id")
            minimum_tier = rule.get("minimum_tier")
            if minimum_tier not in TIER_ORDER:
                raise ValueError(f"unsupported minimum tier: {minimum_tier}")
            if self._rule_matches(rule_id, inputs):
                matches.append(PolicyRuleMatch(rule_id, minimum_tier))
                tier_index = max(tier_index, TIER_ORDER.index(minimum_tier))
        selected_tier = TIER_ORDER[tier_index]
        policy_caps = self.profiles[selected_tier]
        supplied = dict(user_limits or {})
        cap_names = {item.name for item in fields(ResourceCaps)}
        rejections = []
        for name, value in sorted(supplied.items()):
            if name not in cap_names:
                rejections.append(
                    BudgetRejection(
                        "user_limit.unknown_resource",
                        name,
                        value,
                        0,
                        "user limit names an unknown budget resource",
                    )
                )
                continue
            policy_limit = getattr(policy_caps, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                rejections.append(
                    BudgetRejection(
                        "user_limit.invalid_value",
                        name,
                        value,
                        policy_limit,
                        "user limit must be a non-negative integer",
                    )
                )
            elif value > policy_limit:
                rejections.append(
                    BudgetRejection(
                        f"user_limit.not_stricter.{name}",
                        name,
                        value,
                        policy_limit,
                        "user limit cannot loosen the policy cap",
                    )
                )
        if rejections:
            raise BudgetPolicyError("invalid user budget limits", tuple(rejections))
        effective_values = policy_caps.to_dict()
        effective_values.update(supplied)
        effective_values["max_successful_extractions"] = min(
            effective_values["max_successful_extractions"],
            effective_values["max_extraction_attempts"],
        )
        effective_values["max_reranker_candidates"] = min(
            effective_values["max_reranker_candidates"],
            effective_values["max_retrieval_candidates"],
        )
        effective_caps = ResourceCaps.from_mapping(effective_values)
        return BudgetSnapshot(
            snapshot_version=1,
            policy_version=self.policy_version,
            policy_config_sha256=self.config_sha256,
            research_spec_id=str(spec.research_spec_id),
            spec_revision=spec_revision,
            run_revision=run_revision,
            selected_tier=selected_tier,
            semantic_inputs=inputs,
            matched_rules=tuple(matches),
            policy_caps=policy_caps,
            user_limits=supplied,
            effective_caps=effective_caps,
        )

    @staticmethod
    def authorize(
        snapshot: BudgetSnapshot, proposal: Mapping[str, int]
    ) -> BudgetDecision:
        cap_names = {item.name for item in fields(ResourceCaps)}
        rejections = []
        for name, proposed in sorted(proposal.items()):
            if name not in cap_names:
                rejections.append(
                    BudgetRejection(
                        "proposal.unknown_resource",
                        name,
                        proposed,
                        0,
                        "proposal names an unknown budget resource",
                    )
                )
                continue
            limit = getattr(snapshot.effective_caps, name)
            if isinstance(proposed, bool) or not isinstance(proposed, int) or proposed < 0:
                rejections.append(
                    BudgetRejection(
                        "proposal.invalid_value",
                        name,
                        proposed,
                        limit,
                        "proposal value must be a non-negative integer",
                    )
                )
            elif proposed > limit:
                rejections.append(
                    BudgetRejection(
                        f"budget.{name}",
                        name,
                        proposed,
                        limit,
                        "proposal exceeds the effective hard limit",
                    )
                )
        return BudgetDecision(not rejections, tuple(rejections))


DEFAULT_POLICY = BudgetPolicy.load()


def conservative_research_spec(objective: str, research_archetype: str) -> ResearchSpec:
    """Create the narrow deterministic fallback allowed by FR-003.

    This preserves the exact objective as the only question. It does not infer
    claims, entities, jurisdictions, or broad search facets.
    """
    namespace = uuid5(NAMESPACE_URL, "fvanevski/firecrawl_skill/budget-policy-v1")

    def stable_id(kind: str):
        return uuid5(namespace, f"{kind}\0{research_archetype}\0{objective}")

    risk = {
        "legislative_legal": RiskLevel.HIGH,
        "breaking_news": RiskLevel.MEDIUM,
        "technical_docs": RiskLevel.LOW,
        "general": RiskLevel.MEDIUM,
    }.get(research_archetype, RiskLevel.MEDIUM)
    freshness = ()
    if research_archetype == "breaking_news":
        freshness = (
            FreshnessRequirement(
                stable_id("freshness"),
                "Use evidence within the explicit current-events window.",
                7,
            ),
        )
    corroboration = ()
    contradiction = ()
    if risk == RiskLevel.HIGH:
        corroboration = (
            EvidenceRequirement(
                stable_id("corroboration"),
                "Corroborate consequential claims independently.",
                2,
            ),
        )
        contradiction = (
            EvidenceRequirement(
                stable_id("contradiction"),
                "Search for material contradictory authority.",
                1,
            ),
        )
    return ResearchSpec(
        schema_version=ResearchSpec.SCHEMA_VERSION,
        research_spec_id=stable_id("spec"),
        objective=objective,
        research_archetype=research_archetype,
        risk_level=risk,
        execution_mode=ExecutionMode.AGENT_LED,
        questions=(ResearchQuestion(stable_id("question"), objective),),
        claims_to_validate=(),
        entities=(),
        jurisdictions=(),
        time_window=TimeWindow(None, None, "as stated in objective", "unassessed"),
        freshness_requirements=freshness,
        required_source_classes=(
            SourceRequirement(stable_id("source"), "primary or controlling source", 1),
        ),
        corroboration_requirements=corroboration,
        contradiction_requirements=contradiction,
        excluded_interpretations=(),
        structured_data_requirements=(),
        completion_criteria=(
            CompletionCriterion(
                stable_id("completion"),
                "Answer the exact stated question within the authorized budget.",
                True,
            ),
        ),
        user_constraints=(),
        ambiguities=("semantic details require agent or model review",),
        assumptions=(),
    )


__all__ = [
    "BudgetDecision",
    "BudgetPolicy",
    "BudgetPolicyError",
    "BudgetRejection",
    "BudgetSnapshot",
    "DEFAULT_POLICY",
    "ResourceCaps",
    "conservative_research_spec",
]
