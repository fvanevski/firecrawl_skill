"""Operator-facing error contracts for curated run commands."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import curated_run_cli
from research_store.asset_promotion_models import AssetPromotionError


class _FailingPromotionService:
    def retain(self, _run_id, _subject_id, *, reason):
        assert reason == "operator retained curated asset"
        raise AssetPromotionError("subject belongs to another curated run")


def test_asset_promotion_failure_is_rendered_without_traceback(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(
        curated_run_cli,
        "_service",
        lambda: _FailingPromotionService(),
    )

    status = curated_run_cli.main(
        ["retain", f"fr_{uuid4().hex}", str(uuid4())]
    )

    captured = capsys.readouterr()
    assert status == 1
    assert captured.out == ""
    assert captured.err == "ERROR: subject belongs to another curated run\n"
    assert "Traceback" not in captured.err


def test_resume_success_remains_machine_readable(monkeypatch, capsys) -> None:
    run_id = f"fr_{uuid4().hex}"
    monkeypatch.setattr(
        curated_run_cli,
        "_service",
        lambda: SimpleNamespace(
            resume=lambda requested: {
                "external_id": requested,
                "run_mode": "curated",
                "state": "indexing",
                "membership_sealed": False,
                "next_action": f"frun seal-acquisition {requested}",
            }
        ),
    )

    status = curated_run_cli.main(["resume", run_id])

    captured = capsys.readouterr()
    assert status == 0
    assert captured.err == ""
    assert f'"external_id": "{run_id}"' in captured.out
    assert '"membership_sealed": false' in captured.out
    assert f'"next_action": "frun seal-acquisition {run_id}"' in captured.out
