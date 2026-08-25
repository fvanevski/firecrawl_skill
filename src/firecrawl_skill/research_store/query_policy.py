"""Deterministic materialization for model-assisted research query proposals."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from typing import Any
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, UUID, uuid5

from firecrawl_skill.research_domain import serialize_model
from firecrawl_skill.research_domain.models import ResearchSpec, TimeWindow

from .authorized_semantic import call_authorized_structured
from .semantic_service import SemanticCallService

QUERY_PROPOSAL_SCHEMA_VERSION = "search-query-proposal-v1"
_MAX_QUERY_LENGTH = 512
_MAX_SITE_OPERATORS = 4
_APP_OWNED_OPERATORS = frozenset(
    {"after", "before", "daterange", "tbs", "qdr", "source", "sort", "when"}
)
_APPLICATION_SEMANTIC_FIELDS = frozenset(
    {"intended_source_class", "expected_organizations", "expected_contribution"}
)
_ANY_OPERATOR_RE = re.compile(
    r"(?i)(?P<prefix>^|[\s(\[{])(?P<negative>-?)(?P<name>[a-z][a-z0-9_-]{1,24}):(?P<value>[^\s)\]}]+)"
)

QUERY_PROPOSAL_SCHEMA: dict[str, Any] = {
    "$id": QUERY_PROPOSAL_SCHEMA_VERSION,
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "schema_version": {"const": QUERY_PROPOSAL_SCHEMA_VERSION},
        "queries": {
            "type": "array",
            "minItems": 1,
            "maxItems": 32,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "query": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": _MAX_QUERY_LENGTH,
                    },
                    "facet": {"type": "string", "minLength": 1, "maxLength": 160},
                    "target_question_ids": {
                        "type": "array",
                        "maxItems": 32,
                        "items": {"type": "string", "format": "uuid"},
                    },
                    "target_claim_ids": {
                        "type": "array",
                        "maxItems": 32,
                        "items": {"type": "string", "format": "uuid"},
                    },
                    "intended_source_class": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 160,
                    },
                    "expected_organizations": {
                        "type": "array",
                        "maxItems": 16,
                        "items": {"type": "string", "minLength": 1, "maxLength": 160},
                    },
                    "expected_contribution": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 500,
                    },
                },
                "required": [
                    "query",
                    "facet",
                    "target_question_ids",
                    "target_claim_ids",
                    "intended_source_class",
                    "expected_organizations",
                    "expected_contribution",
                ],
            },
        },
    },
    "required": ["schema_version", "queries"],
}


def _normalize_text(value: object, field_name: str, *, max_length: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    text = " ".join(value.split())
    if not text:
        raise ValueError(f"{field_name} must be non-empty")
    if len(text) > max_length:
        raise ValueError(f"{field_name} exceeds {max_length} characters")
    return text


def _normalize_query(value: object) -> str:
    return _normalize_text(value, "query", max_length=_MAX_QUERY_LENGTH)


def _normalize_domain(value: str) -> str:
    token = value.strip().strip("\"'()[]{}").rstrip(".,;")
    if "://" not in token:
        token = "https://" + token
    try:
        parsed = urlsplit(token)
    except ValueError as exc:
        raise ValueError(f"invalid site: operator value: {value!r}") from exc
    host = (parsed.hostname or "").lower().strip(".")
    if not host or any(ch.isspace() for ch in host):
        raise ValueError(f"invalid site: operator value: {value!r}")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("site: operator must name a domain, not a path/query/fragment")
    return host


def parse_query_structure(query: str) -> dict[str, Any]:
    """Parse deterministic search structure from query text itself.

    The parser recognizes positive and negative ``site:`` operators. Provider
    recency/time operators are rejected because those values are materialized
    from persisted ResearchSpec/discovery policy, never model text.
    """

    text = _normalize_query(query)
    positive: list[str] = []
    negative: list[str] = []
    for match in _ANY_OPERATOR_RE.finditer(text):
        name = match.group("name").casefold()
        value = match.group("value")
        if name in {"http", "https"} and value.startswith("//"):
            continue
        if name in _APP_OWNED_OPERATORS:
            raise ValueError(
                f"query contains application-owned search operator {name}:"
            )
        if name != "site":
            raise ValueError(f"query contains unsupported search operator {name}:")
        domain = _normalize_domain(value)
        target = negative if match.group("negative") == "-" else positive
        if domain not in target:
            target.append(domain)
    if len(positive) + len(negative) > _MAX_SITE_OPERATORS:
        raise ValueError(
            f"query contains more than {_MAX_SITE_OPERATORS} site operators"
        )
    return {
        "query": text,
        "domain_restrictions": positive,
        "negative_terms": [f"site:{domain}" for domain in negative],
        "is_domain_scoped": bool(positive),
    }


def _strip_search_operators(value: str) -> str:
    text = _normalize_query(value)

    def _replace(match: re.Match[str]) -> str:
        name = match.group("name").casefold()
        raw_value = match.group("value")
        if name in {"http", "https"} and raw_value.startswith("//"):
            return match.group(0)
        return match.group("prefix")

    stripped = _ANY_OPERATOR_RE.sub(_replace, text)
    stripped = re.sub(r"\(\s*(?:(?:OR|AND)\s*)*\)", " ", stripped, flags=re.IGNORECASE)
    stripped = re.sub(r"(?i)(?:^|\s)(?:OR|AND)(?=\s|$)", " ", stripped)
    stripped = " ".join(stripped.replace("(", " ").replace(")", " ").split())
    return stripped.strip(" -") or "research objective"


def _known_targets(spec: ResearchSpec) -> tuple[set[str], set[str]]:
    return (
        {str(item.question_id) for item in spec.questions},
        {str(item.claim_id) for item in spec.claims_to_validate},
    )


def _unique_string_list(value: object, field_name: str) -> list[str]:
    if not isinstance(value, list):
        # Structured payload validation intentionally exposes ValueError uniformly.
        raise ValueError(f"{field_name} must be an array")  # noqa: TRY004
    result: list[str] = []
    for raw in value:
        if not isinstance(raw, str):
            raise ValueError(f"{field_name} values must be strings")
        item = raw.strip()
        if not item:
            raise ValueError(f"{field_name} contains an empty value")
        if item in result:
            raise ValueError(f"{field_name} contains duplicate value {item!r}")
        result.append(item)
    return result


def validate_query_proposal_payload(
    payload: Mapping[str, Any],
    spec: ResearchSpec,
    *,
    max_queries: int,
) -> None:
    if isinstance(max_queries, bool) or not isinstance(max_queries, int) or max_queries < 1:
        raise ValueError("max_queries must be a positive integer")
    if payload.get("schema_version") != QUERY_PROPOSAL_SCHEMA_VERSION:
        raise ValueError("unsupported query proposal schema_version")
    queries = payload.get("queries")
    if not isinstance(queries, list) or not queries:
        raise ValueError("query proposal must contain at least one query")
    if len(queries) > max_queries:
        raise ValueError(
            f"query proposal contains {len(queries)} queries; cap is {max_queries}"
        )
    known_questions, known_claims = _known_targets(spec)
    seen_queries: set[str] = set()
    for index, raw in enumerate(queries):
        if not isinstance(raw, Mapping):
            # This validator's public/domain error contract is ValueError.
            raise ValueError(f"query[{index}] must be an object")  # noqa: TRY004
        allowed_fields = {
            "query",
            "facet",
            "target_question_ids",
            "target_claim_ids",
            "intended_source_class",
            "expected_organizations",
            "expected_contribution",
        }
        unexpected = set(raw) - allowed_fields
        if unexpected:
            raise ValueError(
                f"query[{index}] contains non-semantic fields: {sorted(unexpected)}"
            )
        parsed = parse_query_structure(raw.get("query"))
        normalized = parsed["query"].casefold()
        if normalized in seen_queries:
            raise ValueError("query proposal contains duplicate normalized query text")
        seen_queries.add(normalized)
        _normalize_text(raw.get("facet"), "facet", max_length=160)
        _normalize_text(
            raw.get("intended_source_class"),
            "intended_source_class",
            max_length=160,
        )
        _normalize_text(
            raw.get("expected_contribution"),
            "expected_contribution",
            max_length=500,
        )
        question_ids = _unique_string_list(
            raw.get("target_question_ids"), "target_question_ids"
        )
        claim_ids = _unique_string_list(raw.get("target_claim_ids"), "target_claim_ids")
        if len(question_ids) > 32 or len(claim_ids) > 32:
            raise ValueError("semantic query target IDs exceed deterministic bounds")
        if not question_ids and not claim_ids:
            raise ValueError("semantic query must target a persisted question or claim")
        unknown_questions = set(question_ids) - known_questions
        unknown_claims = set(claim_ids) - known_claims
        if unknown_questions or unknown_claims:
            raise ValueError(
                "semantic query references unknown persisted targets: "
                f"questions={sorted(unknown_questions)}, claims={sorted(unknown_claims)}"
            )
        organizations = _unique_string_list(
            raw.get("expected_organizations"), "expected_organizations"
        )
        if len(organizations) > 16:
            raise ValueError("expected_organizations exceeds deterministic bound")
        for organization in organizations:
            if len(organization) > 160:
                raise ValueError("expected_organizations value exceeds 160 characters")


def deterministic_unscoped_proposal(spec: ResearchSpec) -> dict[str, Any]:
    question = spec.questions[0]
    query = _strip_search_operators(question.text or spec.objective)
    if parse_query_structure(query)["is_domain_scoped"]:
        raise AssertionError(
            "deterministic fallback unexpectedly retained domain scope"
        )
    return {
        "query": query,
        "facet": "unscoped_objective_fallback",
        "target_question_ids": [str(question.question_id)],
        "target_claim_ids": [],
        "intended_source_class": "unspecified",
        "expected_organizations": [],
        "expected_contribution": "unscoped discovery for the stated objective",
    }


def _normalize_application_proposal(
    proposal: Mapping[str, Any],
    spec: ResearchSpec,
) -> dict[str, Any]:
    """Bind deterministic application shorthand before strict plan validation.

    Semantic model/host output is validated against ``QUERY_PROPOSAL_SCHEMA``
    before this function is reached. This normalization therefore supports only
    application-generated shorthand without weakening the semantic schema.
    """

    result = dict(proposal)
    has_questions = "target_question_ids" in result
    has_claims = "target_claim_ids" in result
    if not has_questions and not has_claims:
        result["target_question_ids"] = [str(spec.questions[0].question_id)]
        result["target_claim_ids"] = []
    elif has_questions != has_claims:
        raise ValueError("query proposal must provide both target ID arrays")

    present_metadata = _APPLICATION_SEMANTIC_FIELDS & set(result)
    if not present_metadata:
        result.update(
            {
                "intended_source_class": "unspecified",
                "expected_organizations": [],
                "expected_contribution": "objective coverage",
            }
        )
    elif present_metadata != _APPLICATION_SEMANTIC_FIELDS:
        missing = sorted(_APPLICATION_SEMANTIC_FIELDS - present_metadata)
        raise ValueError(
            "application query proposal must provide all semantic metadata fields "
            f"or none; missing={missing}"
        )
    return result


def _canonical_semantic_proposal(proposal: Mapping[str, Any]) -> dict[str, Any]:
    parsed = parse_query_structure(proposal.get("query"))
    return {
        "query": parsed["query"],
        "facet": _normalize_text(proposal.get("facet"), "facet", max_length=160),
        "target_question_ids": sorted(
            _unique_string_list(
                proposal.get("target_question_ids"), "target_question_ids"
            )
        ),
        "target_claim_ids": sorted(
            _unique_string_list(proposal.get("target_claim_ids"), "target_claim_ids")
        ),
        "intended_source_class": _normalize_text(
            proposal.get("intended_source_class"),
            "intended_source_class",
            max_length=160,
        ),
        "expected_organizations": sorted(
            _unique_string_list(
                proposal.get("expected_organizations"), "expected_organizations"
            ),
            key=str.casefold,
        ),
        "expected_contribution": _normalize_text(
            proposal.get("expected_contribution"),
            "expected_contribution",
            max_length=500,
        ),
    }


def _proposal_sort_key(proposal: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        str(proposal["query"]).casefold(),
        str(proposal["facet"]).casefold(),
        tuple(proposal["target_question_ids"]),
        tuple(proposal["target_claim_ids"]),
        str(proposal["intended_source_class"]).casefold(),
        tuple(str(value).casefold() for value in proposal["expected_organizations"]),
        str(proposal["expected_contribution"]).casefold(),
    )


def materialize_query_plan(
    spec: ResearchSpec,
    proposals: Sequence[Mapping[str, Any]],
    *,
    run_id: UUID | None,
    discovery_window: TimeWindow,
    max_queries: int,
) -> dict[str, Any]:
    """Materialize semantic query proposals into authoritative SearchPlan data.

    Proposal traversal order is never authoritative. The complete bounded
    semantic set is validated first, canonicalized, and sorted before numeric
    priority or persisted query order is assigned.
    """

    if isinstance(max_queries, bool) or not isinstance(max_queries, int) or max_queries < 1:
        raise ValueError("max_queries must be a positive integer")
    normalized = [_normalize_application_proposal(item, spec) for item in proposals]
    if not normalized:
        normalized = [deterministic_unscoped_proposal(spec)]
    payload = {
        "schema_version": QUERY_PROPOSAL_SCHEMA_VERSION,
        "queries": normalized,
    }
    validate_query_proposal_payload(payload, spec, max_queries=max_queries)
    canonical = [_canonical_semantic_proposal(item) for item in normalized]
    canonical.sort(key=_proposal_sort_key)

    # A model flag cannot make a query "neutral". If every actual query is
    # positively domain-scoped, reserve one bounded branch for a real unscoped
    # fallback derived from the persisted ResearchSpec. Which scoped query is
    # displaced is determined only after canonical ordering, never model order.
    if all(
        parse_query_structure(str(item["query"]))["is_domain_scoped"]
        for item in canonical
    ):
        fallback = _canonical_semantic_proposal(deterministic_unscoped_proposal(spec))
        if len(canonical) < max_queries:
            canonical.append(fallback)
        else:
            canonical[-1] = fallback
        canonical.sort(key=_proposal_sort_key)

    freshness = asdict(discovery_window)
    queries: list[dict[str, Any]] = []
    for priority, item in enumerate(canonical, 1):
        parsed = parse_query_structure(str(item["query"]))
        normalized_text = parsed["query"]
        question_ids = list(item["target_question_ids"])
        claim_ids = list(item["target_claim_ids"])
        identity = {
            "run_id": str(run_id) if run_id is not None else None,
            "research_spec_id": str(spec.research_spec_id),
            "query": normalized_text,
            "facet": str(item["facet"]),
            "target_question_ids": question_ids,
            "target_claim_ids": claim_ids,
        }
        namespace_value = "|".join(
            [
                str(identity["run_id"]),
                str(identity["research_spec_id"]),
                normalized_text,
                str(identity["facet"]),
                ",".join(question_ids),
                ",".join(claim_ids),
            ]
        )
        queries.append(
            {
                "query_id": str(uuid5(NAMESPACE_URL, namespace_value)),
                "query": normalized_text,
                "facet": str(item["facet"]),
                "target_question_ids": question_ids,
                "target_claim_ids": claim_ids,
                "intended_source_classes": [str(item["intended_source_class"])],
                "expected_organizations": list(item["expected_organizations"]),
                "freshness_requirement": freshness,
                "expected_contribution": str(item["expected_contribution"]),
                "domain_restrictions": list(parsed["domain_restrictions"]),
                "negative_terms": list(parsed["negative_terms"]),
                "priority": priority,
            }
        )
    if not queries:
        raise ValueError("deterministic query materialization produced no queries")
    return {
        "schema_version": "search-plan-v1",
        "research_spec_id": str(spec.research_spec_id),
        "revision": 1,
        "queries": queries,
    }


def _proposal_fixture(spec: ResearchSpec) -> dict[str, Any]:
    return {
        "schema_version": QUERY_PROPOSAL_SCHEMA_VERSION,
        "queries": [deterministic_unscoped_proposal(spec)],
    }


def semantic_query_proposals(
    *,
    topic: str,
    max_queries: int,
    semantic_service: SemanticCallService,
    semantic_context: dict[str, Any],
    spec: ResearchSpec,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Obtain bounded semantic query labels; operational policy stays outside."""

    if isinstance(max_queries, bool) or not isinstance(max_queries, int) or max_queries < 1:
        raise ValueError("max_queries must be a positive integer")

    def post_validate(payload: Mapping[str, Any]) -> None:
        validate_query_proposal_payload(payload, spec, max_queries=max_queries)

    result = call_authorized_structured(
        semantic_service=semantic_service,
        semantic_context=semantic_context,
        deterministic_fixture=_proposal_fixture(spec),
        actor_identifier="deterministic-query-planner",
        host_artifact_supplier=semantic_service.host_artifact_supplier,
        schema=QUERY_PROPOSAL_SCHEMA,
        provider="local",
        model=None,
        max_output_tokens=4096,
        prompt_version=QUERY_PROPOSAL_SCHEMA_VERSION,
        post_validate=post_validate,
        system_prompt=(
            "Propose semantic web-search formulations only. Return schema-valid JSON. "
            "Use only persisted question/claim IDs supplied in the ResearchSpec. "
            "Do not decide or emit freshness, dates, recency/provider parameters, "
            "domain-neutral truth, deterministic IDs, lifecycle state, scrape "
            "admission, numeric priority, or budget policy. You may include literal "
            "site: or -site: syntax when semantically useful; application code parses "
            "that syntax and owns its meaning."
        ),
        user_prompt=(
            f"Create at most {max_queries} complementary semantic queries.\n"
            f"Objective: {topic}\n"
            "ResearchSpec: "
            + json.dumps(serialize_model(spec), sort_keys=True, default=str)
        ),
    )
    provenance = {
        **dict(result.provenance),
        "schema_version": QUERY_PROPOSAL_SCHEMA_VERSION,
        "semantic_call_id": (
            str(result.semantic_call_id)
            if result.semantic_call_id is not None
            else None
        ),
        "artifact_ids": [str(value) for value in result.artifact_ids],
        "error": result.error or "",
    }
    if not result.value or result.error:
        return [], {"status": "failed", **provenance}
    post_validate(result.value)
    return list(result.value["queries"]), {"status": "succeeded", **provenance}


__all__ = [
    "QUERY_PROPOSAL_SCHEMA",
    "QUERY_PROPOSAL_SCHEMA_VERSION",
    "deterministic_unscoped_proposal",
    "materialize_query_plan",
    "parse_query_structure",
    "semantic_query_proposals",
    "validate_query_proposal_payload",
]
