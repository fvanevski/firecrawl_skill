"""Generate valid fixtures and schema files for benchmark models."""

from __future__ import annotations

import json
from pathlib import Path

from research_domain.registry import MODEL_BY_VERSION, write_schemas

# Write schemas
schemas_dir = Path("schemas/research-workflow")
schemas_dir.mkdir(parents=True, exist_ok=True)
write_schemas(schemas_dir)
print(f"Wrote schemas for {len(MODEL_BY_VERSION)} models")

# Write valid fixtures for new benchmark models
fixtures_dir = Path("tests/fixtures/research_domain")
fixtures_dir.mkdir(parents=True, exist_ok=True)

VALID_FIXTURES = {
    "benchmark-source-v1": {
        "schema_version": "benchmark-source-v1",
        "file_path": "scripts/test.py",
        "relevance": True,
        "role": "relevant",
    },
    "benchmark-objective-v1": {
        "schema_version": "benchmark-objective-v1",
        "id": "obj-test",
        "title": "Test",
        "objective": "Test objective",
        "questions": ["What?"],
        "expected_source_classes": ["docs"],
        "known_relevant_sources": [
            {
                "schema_version": "benchmark-source-v1",
                "file_path": "scripts/test.py",
                "relevance": True,
                "role": "relevant",
            }
        ],
        "known_distractor_sources": [],
        "expected_unresolved_controversies": [],
        "citation_support_labels": {"q1": "SUPPORTED"},
    },
    "benchmark-dataset-v1": {
        "schema_version": "benchmark-dataset-v1",
        "version": "benchmark-test-v1",
        "description": "Test dataset",
        "evaluation_set": True,
        "objectives": [
            {
                "schema_version": "benchmark-objective-v1",
                "id": "obj-001",
                "title": "Test",
                "objective": "Test",
                "questions": ["What?"],
                "expected_source_classes": ["docs"],
                "known_relevant_sources": [
                    {
                        "schema_version": "benchmark-source-v1",
                        "file_path": "scripts/test.py",
                        "relevance": True,
                        "role": "relevant",
                    }
                ],
                "known_distractor_sources": [],
                "expected_unresolved_controversies": [],
                "citation_support_labels": {"q1": "SUPPORTED"},
            }
        ],
        "quality_thresholds": {"min_candidate_recall": 0.5},
        "workflow_modes": ["legacy", "agent_led"],
        "deterministic_integrity_checks": ["state_machine_transitions"],
    },
    "quality-measurement-v1": {
        "schema_version": "quality-measurement-v1",
        "candidate_recall": 0.75,
        "source_quality_score": 0.80,
        "coverage_completeness": 0.65,
        "unsupported_claim_rate": 0.08,
        "citation_accuracy": 0.88,
        "report_quality_score": 0.78,
    },
    "performance-measurement-v1": {
        "schema_version": "performance-measurement-v1",
        "total_latency_ms": 15000.0,
        "total_tokens": 15000,
        "semantic_calls": 8,
        "cache_hit_rate": 0.3,
        "cache_miss_rate": 0.7,
        "embedding_throughput": 50.0,
        "gpu_memory_mb": 4096.0,
        "cpu_percent": 60.0,
    },
    "integrity-check-v1": {
        "schema_version": "integrity-check-v1",
        "check_name": "state_machine_transitions",
        "passed": True,
        "details": "All transitions valid",
    },
    "workflow-run-result-v1": {
        "schema_version": "workflow-run-result-v1",
        "workflow_mode": "agent_led",
        "quality": {
            "schema_version": "quality-measurement-v1",
            "candidate_recall": 0.75,
            "source_quality_score": 0.80,
            "coverage_completeness": 0.65,
            "unsupported_claim_rate": 0.08,
            "citation_accuracy": 0.88,
            "report_quality_score": 0.78,
        },
        "performance": {
            "schema_version": "performance-measurement-v1",
            "total_latency_ms": 15000.0,
            "total_tokens": 15000,
            "semantic_calls": 8,
            "cache_hit_rate": 0.3,
            "cache_miss_rate": 0.7,
            "embedding_throughput": 50.0,
            "gpu_memory_mb": 4096.0,
            "cpu_percent": 60.0,
        },
        "integrity_checks": [
            {
                "schema_version": "integrity-check-v1",
                "check_name": "state_machine_transitions",
                "passed": True,
                "details": "All transitions valid",
            }
        ],
        "run_id": None,
        "errors": [],
    },
    "workflow-comparison-v1": {
        "schema_version": "workflow-comparison-v1",
        "dataset_version": "benchmark-v1",
        "results": [
            {
                "schema_version": "workflow-run-result-v1",
                "workflow_mode": "legacy",
                "quality": {
                    "schema_version": "quality-measurement-v1",
                    "candidate_recall": 0.45,
                    "source_quality_score": 0.55,
                    "coverage_completeness": 0.35,
                    "unsupported_claim_rate": 0.25,
                    "citation_accuracy": 0.60,
                    "report_quality_score": 0.50,
                },
                "performance": {
                    "schema_version": "performance-measurement-v1",
                    "total_latency_ms": 5000.0,
                    "total_tokens": 5000,
                    "semantic_calls": 2,
                    "cache_hit_rate": 0.1,
                    "cache_miss_rate": 0.9,
                    "embedding_throughput": 100.0,
                    "gpu_memory_mb": 0.0,
                    "cpu_percent": 30.0,
                },
                "integrity_checks": [],
                "run_id": None,
                "errors": [],
            },
            {
                "schema_version": "workflow-run-result-v1",
                "workflow_mode": "agent_led",
                "quality": {
                    "schema_version": "quality-measurement-v1",
                    "candidate_recall": 0.75,
                    "source_quality_score": 0.80,
                    "coverage_completeness": 0.65,
                    "unsupported_claim_rate": 0.08,
                    "citation_accuracy": 0.88,
                    "report_quality_score": 0.78,
                },
                "performance": {
                    "schema_version": "performance-measurement-v1",
                    "total_latency_ms": 15000.0,
                    "total_tokens": 15000,
                    "semantic_calls": 8,
                    "cache_hit_rate": 0.3,
                    "cache_miss_rate": 0.7,
                    "embedding_throughput": 50.0,
                    "gpu_memory_mb": 4096.0,
                    "cpu_percent": 60.0,
                },
                "integrity_checks": [],
                "run_id": None,
                "errors": [],
            },
        ],
        "quality_vs_baseline": {"agent_led": 1.67},
        "performance_vs_baseline": {"agent_led": 3.0},
        "integrity_regression": False,
    },
    "release-recommendation-v1": {
        "schema_version": "release-recommendation-v1",
        "outcome": "go",
        "dataset_version": "benchmark-v1",
        "comparison": {
            "schema_version": "workflow-comparison-v1",
            "dataset_version": "benchmark-v1",
            "results": [
                {
                    "schema_version": "workflow-run-result-v1",
                    "workflow_mode": "legacy",
                    "quality": {
                        "schema_version": "quality-measurement-v1",
                        "candidate_recall": 0.45,
                        "source_quality_score": 0.55,
                        "coverage_completeness": 0.35,
                        "unsupported_claim_rate": 0.25,
                        "citation_accuracy": 0.60,
                        "report_quality_score": 0.50,
                    },
                    "performance": {
                        "schema_version": "performance-measurement-v1",
                        "total_latency_ms": 5000.0,
                        "total_tokens": 5000,
                        "semantic_calls": 2,
                        "cache_hit_rate": 0.1,
                        "cache_miss_rate": 0.9,
                        "embedding_throughput": 100.0,
                        "gpu_memory_mb": 0.0,
                        "cpu_percent": 30.0,
                    },
                    "integrity_checks": [],
                    "run_id": None,
                    "errors": [],
                },
                {
                    "schema_version": "workflow-run-result-v1",
                    "workflow_mode": "agent_led",
                    "quality": {
                        "schema_version": "quality-measurement-v1",
                        "candidate_recall": 0.75,
                        "source_quality_score": 0.80,
                        "coverage_completeness": 0.65,
                        "unsupported_claim_rate": 0.08,
                        "citation_accuracy": 0.88,
                        "report_quality_score": 0.78,
                    },
                    "performance": {
                        "schema_version": "performance-measurement-v1",
                        "total_latency_ms": 15000.0,
                        "total_tokens": 15000,
                        "semantic_calls": 8,
                        "cache_hit_rate": 0.3,
                        "cache_miss_rate": 0.7,
                        "embedding_throughput": 50.0,
                        "gpu_memory_mb": 4096.0,
                        "cpu_percent": 60.0,
                    },
                    "integrity_checks": [],
                    "run_id": None,
                    "errors": [],
                },
            ],
            "quality_vs_baseline": {"agent_led": 1.67},
            "performance_vs_baseline": {"agent_led": 3.0},
            "integrity_regression": False,
        },
        "supported_claims": ["quality thresholds met"],
        "withdrawn_claims": [],
        "known_limitations": ["CPU latency"],
        "conditions": [],
        "p0_regressions": [],
    },
}

# Load existing valid.json and merge — preserve fixtures not in VALID_FIXTURES
existing_valid = json.loads((fixtures_dir / "valid.json").read_text())
for key, value in VALID_FIXTURES.items():
    existing_valid[key] = value
(fixtures_dir / "valid.json").write_text(
    json.dumps(existing_valid, indent=2, default=str) + "\n"
)

# Write invalid fixtures for new benchmark models
INVALID_FIXTURES = {
    "benchmark-source-v1": [
        {"path": ["role"], "value": "invalid"},
    ],
    "benchmark-objective-v1": [
        {"path": ["id"], "value": ""},
        {"path": ["questions"], "value": []},
    ],
    "benchmark-dataset-v1": [
        {"path": ["objectives"], "value": []},
        {"path": ["workflow_modes", 0], "value": "invalid_mode"},
    ],
    "quality-measurement-v1": [
        {"path": ["candidate_recall"], "value": -0.1},
        {"path": ["source_quality_score"], "value": 1.1},
    ],
    "performance-measurement-v1": [
        {"path": ["total_latency_ms"], "value": -1.0},
        {"path": ["cpu_percent"], "value": 101.0},
    ],
    "integrity-check-v1": [
        {"path": ["check_name"], "value": ""},
    ],
    "workflow-run-result-v1": [
        {"path": ["workflow_mode"], "value": "invalid_mode"},
    ],
    "workflow-comparison-v1": [
        {"path": ["results"], "value": []},
    ],
    "release-recommendation-v1": [
        {"path": ["outcome"], "value": "invalid"},
    ],
}

existing_invalid = json.loads((fixtures_dir / "invalid.json").read_text())
for key, value in INVALID_FIXTURES.items():
    existing_invalid[key] = value
(fixtures_dir / "invalid.json").write_text(
    json.dumps(existing_invalid, indent=2, default=str) + "\n"
)

print(
    f"Written {len(VALID_FIXTURES)} valid and {len(INVALID_FIXTURES)} invalid fixtures"
)
