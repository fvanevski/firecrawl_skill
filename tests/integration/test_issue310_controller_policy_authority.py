"""Disposable-PostgreSQL regressions for #310 controller-policy authority."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

import firecrawl_skill.research_store.research_controller_contract as controller_contract
from firecrawl_skill.research_store.composition import build_run_service
from firecrawl_skill.research_store.config import StoreConfig
from firecrawl_skill.research_store.postgres import (
    connect,
    migrate,
    require_disposable_database_reset,
)
from firecrawl_skill.research_store.research_controller import (
    ResearchWorkflowController,
)
from firecrawl_skill.research_store.research_controller_contract import (
    DISPOSITION_BLOCKED,
    ControllerConfig,
)

TEST_DSN = os.environ.get("RESEARCH_STORE_TEST_DATABASE_URL") or ""
pytestmark = pytest.mark.skipif(
    not TEST_DSN,
    reason="requires repository-sanctioned disposable PostgreSQL",
)


@pytest.fixture(scope="module", autouse=True)
def prepared_database() -> None:
    require_disposable_database_reset(
        TEST_DSN,
        os.environ.get("RESEARCH_STORE_TEST_ALLOW_RESET", ""),
    )
    with connect(TEST_DSN) as connection, connection.cursor() as cursor:
        cursor.execute("DROP SCHEMA public CASCADE")
        cursor.execute("CREATE SCHEMA public")
    migrate(TEST_DSN)


@pytest.fixture
def controller(
    tmp_path: Path,
) -> tuple[ResearchWorkflowController, list[str]]:
    config = replace(
        StoreConfig.from_env(),
        database_url=TEST_DSN,
        blob_root=tmp_path / "blobs",
        embedding_model=f"issue310-policy-{uuid4().hex[:8]}",
        embedding_revision="test",
        embedding_dimension=4,
    )
    run_service = build_run_service(config)
    provider_calls: list[str] = []

    def forbidden_orchestrator_factory(_config: Any) -> Any:
        provider_calls.append("orchestrator_factory")
        raise AssertionError("controller policy blocker reached provider orchestration")

    workflow: Any = object.__new__(ResearchWorkflowController)
    workflow.run_service = run_service
    workflow.controller_config = ControllerConfig()
    workflow.orchestrator_factory = forbidden_orchestrator_factory
    return workflow, provider_calls


def _create_low_level_run(workflow: ResearchWorkflowController) -> Any:
    return workflow.run_service.create(
        "issue310 low-level controller-policy authority",
        f"fr_{uuid4().hex}",
        execution_mode="deterministic_debug",
        actor_type="test",
        actor_identifier="issue310-controller-policy-authority",
    )


def _assert_blocked(directive: Any) -> None:
    assert directive.disposition == DISPOSITION_BLOCKED
    assert directive.action_kind == "inspect_blocker"
    assert directive.lifecycle_state == "created"
    assert directive.result_ready is False


def _malformed_retained_only_policy(retained_only: Any) -> dict[str, Any]:
    schema_version = str(
        getattr(
            controller_contract,
            "CONTROLLER_POLICY_SCHEMA_VERSION",
            "research-controller-policy-v1",
        )
    )
    payload: dict[str, Any] = {
        "schema_version": schema_version,
        "retained_only": retained_only,
        "evaluated_at": "2026-08-24T21:00:00+00:00",
    }
    if schema_version == "research-controller-policy-v2":
        payload["curated"] = False
    return payload


def test_postgres_status_and_continue_both_block_without_controller_policy(
    controller: tuple[ResearchWorkflowController, list[str]],
) -> None:
    workflow, provider_calls = controller
    status = _create_low_level_run(workflow)
    public_id = status.external_id or ""

    status_directive = workflow.status(public_id)
    continue_directive = workflow.continue_run(public_id)

    _assert_blocked(status_directive)
    _assert_blocked(continue_directive)
    assert status_directive.lifecycle_revision == status.lifecycle_revision
    assert continue_directive.lifecycle_revision == status.lifecycle_revision
    assert any(
        "no canonical controller policy" in item
        for item in status_directive.diagnostics
    )
    assert any(
        "no canonical controller policy" in item
        for item in continue_directive.diagnostics
    )
    assert provider_calls == []


@pytest.mark.parametrize(
    "retained_only",
    ["false", 0, None],
)
def test_postgres_malformed_retained_only_policy_fails_closed(
    controller: tuple[ResearchWorkflowController, list[str]],
    retained_only: Any,
) -> None:
    workflow, provider_calls = controller
    provider_calls.clear()
    status = _create_low_level_run(workflow)
    with workflow.run_service.uow_factory() as uow:
        uow.runs.append_event(
            status.id,
            "controller.policy_recorded",
            "test",
            f"test:malformed-controller-policy:{status.id}",
            actor_identifier="issue310-controller-policy-authority",
            payload=_malformed_retained_only_policy(retained_only),
        )
        uow.commit()

    status_directive = workflow.status(status.external_id or "")
    continue_directive = workflow.continue_run(status.external_id or "")

    _assert_blocked(status_directive)
    _assert_blocked(continue_directive)
    assert any(
        "retained-only policy is malformed" in item
        for item in status_directive.diagnostics
    )
    assert any(
        "retained-only policy is malformed" in item
        for item in continue_directive.diagnostics
    )
    assert provider_calls == []
