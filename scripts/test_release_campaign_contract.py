from __future__ import annotations

import json
from pathlib import Path

import pytest

from release_campaign_contract import (
    CALIBRATION_STAGE,
    CALIBRATION_TEXTS,
    CALIBRATION_VERSION,
    ContractError,
    _normalize_run,
    _rewrite_reproducibility,
    validate_campaign_contract,
)

QUALITY_NAMES = (
    "candidate_recall",
    "source_quality_score",
    "coverage_completeness",
    "unsupported_claim_rate",
    "citation_accuracy",
    "report_quality_score",
)
PERFORMANCE_NAMES = (
    "total_tokens",
    "cache_hit_rate",
    "embedding_throughput",
    "cpu_percent",
    "gpu_memory_mb",
)


def _source(run_id: str, *, records: list[str] | None = None, samples: int = 0):
    return {
        "table": "table",
        "column": "column",
        "run_id": run_id,
        "method": "method",
        "record_ids": records or [],
        "sample_count": samples,
        "stages": [],
        "stage_set_version": "",
        "status_counts": {},
    }


def _run(run_id: str, mode: str, objective: str = "obj-001") -> dict:
    quality_metrics = [
        {
            "name": name,
            "value": 1.0,
            "status": "measured",
            "formula": "formula",
            "source": _source(run_id),
        }
        for name in QUALITY_NAMES
    ]
    performance_metrics = []
    for name in PERFORMANCE_NAMES:
        performance_metrics.append(
            {
                "name": name,
                "value": 0.0 if name != "total_tokens" else 12.0,
                "status": "measured",
                "formula": "formula",
                "source": _source(
                    run_id,
                    records=["cache"] if name == "cache_hit_rate" else None,
                    samples=3 if name in {"cpu_percent", "gpu_memory_mb"} else 0,
                ),
            }
        )
    return {
        "run_id": run_id,
        "mode": mode,
        "objective_id": objective,
        "quality_metrics": quality_metrics,
        "performance_metrics": performance_metrics,
        "performance": {
            "total_tokens": 12,
            "embedding_throughput": 3.0,
        },
    }


def _provenance() -> dict[str, tuple[str, ...]]:
    return {
        "candidates": ("candidate-1",),
        "coverage": ("coverage-1",),
        "claims": ("claim-1",),
        "citations": ("citation-1",),
        "report": ("packet-1",),
        "tokens": ("usage-1",),
        "semantic_calls": ("semantic-1",),
    }


def _calibration(rate: float = 20.0, sample_count: int | None = None) -> dict:
    count = len(CALIBRATION_TEXTS) if sample_count is None else sample_count
    return {
        "record_id": "calibration-1",
        "total_texts": count,
        "elapsed_seconds": count / rate,
        "throughput": rate,
        "stage": CALIBRATION_STAGE,
        "version": CALIBRATION_VERSION,
        "endpoint_model": "embed",
        "embedding_revision": "revision",
        "embedding_dimension": 1024,
        "embedding_fingerprint": "f" * 64,
    }


def _metric(run: dict, name: str) -> dict:
    return next(
        metric
        for key in ("quality_metrics", "performance_metrics")
        for metric in run[key]
        if metric["name"] == name
    )


def test_normalize_run_binds_authoritative_provenance_and_deterministic_na():
    run = _run("00000000-0000-0000-0000-000000000001", "deterministic_debug")
    _normalize_run(run, _provenance(), _calibration())

    for name in QUALITY_NAMES:
        source = _metric(run, name)["source"]
        assert source["record_ids"]
        assert source["sample_count"] > 0

    tokens = _metric(run, "total_tokens")
    assert tokens["status"] == "not_applicable"
    assert tokens["value"] is None
    assert tokens["source"]["status_counts"] == {"not_invoked": 1}
    assert run["performance"]["total_tokens"] is None

    embedding = _metric(run, "embedding_throughput")
    assert embedding["value"] == 20.0
    assert embedding["source"]["record_ids"] == ["calibration-1"]
    assert embedding["source"]["sample_count"] == len(CALIBRATION_TEXTS)
    assert embedding["source"]["stages"] == [CALIBRATION_STAGE]
    assert embedding["source"]["stage_set_version"] == CALIBRATION_VERSION


def test_normalize_run_rejects_measured_quality_without_records():
    run = _run("00000000-0000-0000-0000-000000000001", "autonomous_local")
    provenance = _provenance()
    provenance["coverage"] = ()
    with pytest.raises(ContractError, match="coverage_completeness"):
        _normalize_run(run, provenance, _calibration())


def test_reproducibility_rejects_unequal_fixed_workloads():
    run_a = _run("00000000-0000-0000-0000-000000000001", "autonomous_local")
    run_b = _run("00000000-0000-0000-0000-000000000002", "autonomous_local")
    _normalize_run(run_a, _provenance(), _calibration(20.0))
    _normalize_run(run_b, _provenance(), _calibration(21.0, sample_count=16))
    comparison = {
        "details": [],
        "observations": [],
        "performance_tolerances": [],
        "operational_ratio_limit": 2.0,
        "relative_tolerance": 0.15,
    }

    _rewrite_reproducibility(
        comparison,
        {"runs": [run_a]},
        {"runs": [run_b]},
    )

    assert comparison["all_within_tolerance"] is False
    assert "workload mismatch" in comparison["details"][0]


def test_reproducibility_accepts_equal_fixed_workloads_inside_ratio_limit():
    run_a = _run("00000000-0000-0000-0000-000000000001", "autonomous_local")
    run_b = _run("00000000-0000-0000-0000-000000000002", "autonomous_local")
    _normalize_run(run_a, _provenance(), _calibration(20.0))
    _normalize_run(run_b, _provenance(), _calibration(22.0))
    comparison = {
        "details": [
            "autonomous_local.obj-001.embedding_throughput: legacy workload failure"
        ],
        "observations": [],
        "performance_tolerances": [],
        "operational_ratio_limit": 2.0,
        "relative_tolerance": 0.15,
    }

    _rewrite_reproducibility(
        comparison,
        {"runs": [run_a]},
        {"runs": [run_b]},
    )

    assert comparison["all_within_tolerance"] is True
    assert comparison["details"] == []
    assert any(
        item[0] == "autonomous_local.obj-001.embedding_throughput"
        for item in comparison["performance_tolerances"]
    )


def test_validate_complete_two_campaign_artifact(tmp_path: Path):
    run_number = 0
    for label in ("A", "B"):
        runs = []
        for mode in ("autonomous_local", "deterministic_debug"):
            for objective_index in range(1, 6):
                run_number += 1
                run = _run(
                    f"00000000-0000-0000-0000-{run_number:012d}",
                    mode,
                    f"obj-{objective_index:03d}",
                )
                _normalize_run(run, _provenance(), _calibration(20.0))
                runs.append(run)
        result_dir = tmp_path / label / "timestamp"
        result_dir.mkdir(parents=True)
        (result_dir / "result.json").write_text(
            json.dumps({"recommendation": {"outcome": "go"}, "runs": runs}),
            encoding="utf-8",
        )

    comparison_dir = tmp_path / "reproducibility" / "timestamp"
    comparison_dir.mkdir(parents=True)
    (comparison_dir / "comparison.json").write_text(
        json.dumps({"all_within_tolerance": True, "details": []}),
        encoding="utf-8",
    )

    assert validate_campaign_contract(tmp_path) == []
