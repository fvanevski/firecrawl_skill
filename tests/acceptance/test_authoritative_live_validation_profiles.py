from __future__ import annotations

import importlib.util
import json
import subprocess
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def validation_module():
    loader = SourceFileLoader(
        "issue359_live_validation",
        str(SCRIPTS / "live_validate.py"),
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("profile", "cap"),
    (("focused", 40), ("failure-path", 20), ("full", 100), ("destructive", 10)),
)
def test_validation_profiles_enforce_hard_operation_caps(profile: str, cap: int):
    validation = validation_module()

    defaulted = validation.parse_args(["--profile", profile])
    assert defaulted.max_operations == cap

    args = validation.parse_args(["--profile", profile, "--max-operations", str(cap)])
    assert args.profile == profile
    assert args.max_operations == cap

    with pytest.raises(SystemExit) as exc:
        validation.parse_args(["--profile", profile, "--max-operations", str(cap + 1)])
    assert exc.value.code == 2


def test_validator_rejects_retired_smart_options_in_its_own_cli():
    validation = validation_module()

    with pytest.raises(SystemExit) as exc:
        validation.parse_args(["--max-adaptive-cycles", "1"])
    assert exc.value.code == 2


def test_validator_rejects_unsafe_campaign_id_path_components():
    validation = validation_module()

    for value in ("../escape", "nested/path", "", "a" * 97):
        with pytest.raises(SystemExit) as exc:
            validation.parse_args(["--run-id", value])
        assert exc.value.code == 2

    args = validation.parse_args(["--run-id", "issue359.host-01"])
    assert args.run_id == "issue359.host-01"


def _args(tmp_path: Path, **overrides):
    values = {
        "run_id": "issue359-test",
        "database_url": "postgresql://research@test/research",
        "qdrant_url": "http://qdrant.test:6333",
        "qdrant_api_key": "",
        "blob_root": str(tmp_path / "blobs"),
        "max_operations": 4,
        "api_url": "http://firecrawl.test:3002",
        "case_timeout": 30,
        "worker_timeout": 0.0,
        "expected_head_sha": None,
        "artifact_root": None,
        "profile": "focused",
        "keep_runs": False,
        "disposable_namespace": "fc_live_fault",
        "disposable_pg_port": 55436,
        "disposable_qdrant_port": 55437,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _Inspector:
    def __init__(self):
        self.states: dict[str, str] = {}
        self.run_ids: set[str] = set()
        self.objectives: dict[str, set[str]] = {}

    def list_run_ids(self):
        return set(self.run_ids)

    def run_ids_for_objective(self, objective):
        return set(self.objectives.get(objective, set()))

    def run_state(self, run_id):
        return self.states[run_id]

    def probe_qdrant_alias(self):
        return {
            "alias": "research_chunks_active",
            "collection": "research_chunks_test",
            "dimension": 1024,
            "compatible": True,
        }

    def wait_for_worker(self, run_id, **_kwargs):
        return {
            "external_run_id": run_id,
            "checks": {"authoritative_records": True},
            "pass": True,
        }


def test_firecrawl_proxy_enforces_cap_under_concurrent_calls(tmp_path: Path):
    validation = validation_module()
    call_log = tmp_path / "real-firecrawl.log"
    real_cli = tmp_path / "real-firecrawl"
    real_cli.write_text(
        "#!/usr/bin/env python3\n"
        "import os, pathlib, time\n"
        "time.sleep(0.15)\n"
        "path = pathlib.Path(os.environ['REAL_CALL_LOG'])\n"
        "with path.open('a', encoding='utf-8') as handle:\n"
        "    handle.write('called\\n')\n",
        encoding="utf-8",
    )
    real_cli.chmod(0o700)

    campaign = validation.Campaign(
        _args(tmp_path, max_operations=1),
        inspector=_Inspector(),
        real_cli=str(real_cli),
        work_root=tmp_path / "work",
    )
    campaign.env["REAL_CALL_LOG"] = str(call_log)
    proxy = campaign.proxy_dir / "firecrawl"
    try:
        processes = [
            subprocess.Popen(
                [str(proxy), "search", f"query-{index}"],
                env=campaign.env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for index in range(2)
        ]
        results = [process.communicate(timeout=10) for process in processes]
        returncodes = sorted(process.returncode for process in processes)

        assert returncodes == [0, 78]
        assert call_log.read_text(encoding="utf-8").splitlines() == ["called"]
        counter = json.loads(campaign.counter.read_text(encoding="utf-8"))
        assert counter["count"] == 1
        assert len(counter["calls"]) == 1
        assert any(
            "operation cap exhausted" in stderr.lower() for _stdout, stderr in results
        )
    finally:
        campaign.close()


def test_contract_conformance_does_not_imply_capability_success(tmp_path: Path):
    validation = validation_module()
    payload = {
        "schema_version": "research-result-v3",
        "run_id": "fr_" + "1" * 32,
        "objective": "typed partial contract test",
        "lifecycle_state": "partial",
        "lifecycle_revision": 1,
        "disposition": "terminal_partial",
        "terminal": True,
        "outcome": "partial",
        "result_ready": True,
        "handoff_ready": False,
        "objective_satisfied": False,
        "delivery_mode": None,
        "handoff": None,
        "action_kind": None,
        "action_id": None,
        "diagnostics": [],
        "limitations": [],
    }

    def runner(command, **_kwargs):
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(payload),
            stderr="",
        )

    campaign = validation.Campaign(
        _args(tmp_path),
        inspector=_Inspector(),
        runner=runner,
        real_cli="/usr/bin/firecrawl",
        work_root=tmp_path / "work",
    )
    try:
        case = campaign.run(
            "typed_partial",
            ["fresearch"],
            json_output=True,
            expected_returncodes=(0, 1, 75),
            required_capability=True,
            capability_evaluator=lambda value, rc: bool(
                rc == 0
                and value
                and value.get("disposition") == "terminal_completed"
                and value.get("objective_satisfied") is True
            ),
        )
        assert validation._fresearch_contract(payload, 0) is True
        assert case["contract_result"] == "PASS"
        assert case["capability_result"] == "FAIL"
        assert case["observed_disposition"] == "terminal_partial"
    finally:
        campaign.close()


def test_malformed_same_version_fresearch_payload_is_contract_failure():
    validation = validation_module()
    malformed = {
        "schema_version": "research-result-v3",
        "run_id": "fr_" + "1" * 32,
        "lifecycle_state": "partial",
        "disposition": "terminal_partial",
        "terminal": True,
        "objective_satisfied": False,
    }
    assert validation._fresearch_contract(malformed, 0) is False


def test_fscrape_failed_batch_is_typed_extraction_failure_contract():
    validation = validation_module()
    payload = {
        "schema_version": "authoritative-fscrape-v1",
        "status": "failed",
        "run_id": "00000000-0000-4000-8000-000000000001",
        "research_run_id": "fr_" + "1" * 32,
        "batch_id": "00000000-0000-4000-8000-000000000002",
        "invocation_id": "00000000-0000-4000-8000-000000000002",
        "external_invocation_id": "fc_" + "2" * 32,
        "replayed": False,
        "items": [
            {
                "status": "failed",
                "error": "connection refused",
                "diagnostic": "connection refused",
                "chunk_ids": [],
            }
        ],
        "item_count": 1,
        "items_truncated": False,
        "corpus_ids": {},
    }
    assert validation._fscrape_extraction_failure_contract(payload, 5) is True
    assert validation._fscrape_extraction_failure_contract(payload, 0) is False


def test_fscrape_exception_envelope_remains_typed_extraction_failure_contract():
    validation = validation_module()
    payload = {
        "schema_version": "authoritative-fscrape-error-v1",
        "status": "failed",
        "failure_stage": "extraction",
        "error": "connection refused",
    }
    assert validation._fscrape_extraction_failure_contract(payload, 5) is True


def test_fscrape_positive_capability_requires_typed_succeeded_item():
    validation = validation_module()
    payload = {
        "schema_version": "authoritative-fscrape-v1",
        "status": "complete",
        "run_id": "00000000-0000-4000-8000-000000000001",
        "research_run_id": "fr_" + "1" * 32,
        "batch_id": "00000000-0000-4000-8000-000000000002",
        "invocation_id": "00000000-0000-4000-8000-000000000002",
        "external_invocation_id": "fc_" + "2" * 32,
        "replayed": False,
        "items": [{"status": "succeeded", "chunk_ids": []}],
        "item_count": 1,
        "items_truncated": False,
        "corpus_ids": {},
    }
    assert validation._fscrape_success_capability(payload, 0) is True

    empty = dict(payload)
    empty["items"] = []
    empty["item_count"] = 0
    assert validation._fscrape_success_capability(empty, 0) is False


def test_fsearch_positive_capability_requires_current_typed_nonempty_result():
    validation = validation_module()
    payload = {
        "schema_version": "authoritative-fsearch-v1",
        "status": "complete",
        "run_id": "00000000-0000-4000-8000-000000000001",
        "research_run_id": "fr_" + "1" * 32,
        "invocation_id": "00000000-0000-4000-8000-000000000002",
        "external_invocation_id": "fc_" + "2" * 32,
        "search_replayed": False,
        "candidate_ids": ["00000000-0000-4000-8000-000000000003"],
        "candidate_count": 1,
        "candidate_ids_truncated": False,
        "extraction_status": "complete",
        "extraction_replayed": False,
        "extraction_outcomes": [{"status": "succeeded"}],
        "extraction_outcome_count": 1,
        "extraction_outcomes_truncated": False,
        "corpus_ids": {},
    }
    assert validation._fsearch_result_contract(payload) is True
    assert validation._fsearch_success_capability(payload, 0) is True

    malformed = dict(payload)
    malformed.pop("candidate_count")
    assert validation._fsearch_result_contract(malformed) is False

    empty = dict(payload)
    empty.update(
        {
            "status": "empty",
            "candidate_ids": [],
            "candidate_count": 0,
            "extraction_status": None,
            "extraction_outcomes": [],
            "extraction_outcome_count": 0,
        }
    )
    assert validation._fsearch_result_contract(empty) is True
    assert validation._fsearch_success_capability(empty, 0) is False


def test_tokenizer_cache_is_isolated_from_monitored_tmp(tmp_path: Path):
    validation = validation_module()
    campaign = validation.Campaign(
        _args(tmp_path),
        inspector=_Inspector(),
        real_cli="/usr/bin/firecrawl",
        work_root=tmp_path / "work",
    )
    try:
        assert Path(campaign.env["TMPDIR"]) == campaign.monitored_tmp
        assert Path(campaign.env["TIKTOKEN_CACHE_DIR"]) == campaign.cache_dir
        assert Path(campaign.env["DATA_GYM_CACHE_DIR"]) == campaign.cache_dir
        assert campaign.cache_dir != campaign.monitored_tmp
        (campaign.cache_dir / "token-cache-entry").write_text("cache", encoding="utf-8")
        assert campaign._temporary_entries() == []
    finally:
        campaign.close()


def test_retired_smart_matrix_requires_zero_provider_activity(tmp_path: Path):
    validation = validation_module()
    inspector = _Inspector()

    def runner(command, **_kwargs):
        return subprocess.CompletedProcess(command, 2, stdout="", stderr="rejected")

    campaign = validation.Campaign(
        _args(tmp_path),
        inspector=inspector,
        runner=runner,
        real_cli="/usr/bin/firecrawl",
        work_root=tmp_path / "work",
    )
    try:
        campaign.validate_retired_smart_options()
        retired = [
            case
            for case in campaign.cases
            if case["name"].startswith("retired_smart_option_")
        ]
        assert len(retired) == 4
        assert all(case["contract_result"] == "PASS" for case in retired)
        assert all(case["details"]["provider_operation_delta"] == 0 for case in retired)
        assert all(case["capability_result"] == "NOT_EVALUATED" for case in retired)
    finally:
        campaign.close()


def test_cleanup_terminalizes_only_validator_owned_nonterminal_runs(tmp_path: Path):
    validation = validation_module()
    inspector = _Inspector()
    preexisting = "fr_" + "a" * 32
    owned = "fr_" + "b" * 32
    terminal = "fr_" + "c" * 32
    inspector.run_ids = {preexisting, owned, terminal}
    inspector.states = {
        preexisting: "acquiring",
        owned: "acquiring",
        terminal: "completed",
    }
    calls: list[list[str]] = []

    def runner(command, **_kwargs):
        calls.append(list(command))
        if len(command) >= 3 and command[1] == "cancel":
            inspector.states[command[2]] = "cancelled"
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    campaign = validation.Campaign(
        _args(tmp_path),
        inspector=inspector,
        runner=runner,
        real_cli="/usr/bin/firecrawl",
        work_root=tmp_path / "work",
    )
    campaign.preexisting_run_ids = {preexisting}
    campaign.run_baseline_captured = True
    campaign._track_run("owned", owned, "owned")
    campaign._track_run("terminal", terminal, "terminal")
    try:
        result = campaign.cleanup_runs()
        assert result["result"] == "PASS"
        assert result["cancelled"] == [owned]
        assert result["already_terminal"] == [terminal]
        assert preexisting not in result["owned_run_ids"]
        cancel_targets = [
            command[2]
            for command in calls
            if len(command) >= 3 and command[1] == "cancel"
        ]
        assert cancel_targets == [owned]
    finally:
        campaign.close()


def test_failure_path_dispatch_uses_current_matrix_without_legacy_smart_run(
    tmp_path: Path,
):
    validation = validation_module()
    campaign = object.__new__(validation.Campaign)
    campaign.args = SimpleNamespace(profile="failure-path")
    campaign.preflight = mock.Mock(return_value=True)
    campaign.validate_retired_smart_options = mock.Mock()
    campaign.run_unprepared_rejection = mock.Mock()
    campaign.run_provider_failure = mock.Mock()
    campaign.run_valkey_loss_capability = mock.Mock()
    campaign.run_fresearch = mock.Mock()
    campaign.run_public_fsearch = mock.Mock()
    campaign.finish = mock.Mock(return_value=0)

    assert validation.Campaign.execute(campaign) == 0
    campaign.validate_retired_smart_options.assert_called_once_with()
    campaign.run_unprepared_rejection.assert_called_once_with()
    campaign.run_provider_failure.assert_called_once_with()
    campaign.run_valkey_loss_capability.assert_called_once_with()
    campaign.run_fresearch.assert_not_called()
    campaign.run_public_fsearch.assert_not_called()
    campaign.finish.assert_called_once_with(exit_override=None)


def test_fault_compatibility_entrypoint_delegates_to_canonical_validator():
    source = (SCRIPTS / "live_fault_validate.py").read_text(encoding="utf-8")
    assert "from live_validate import main" in source
    assert "DisposableDestructiveCampaign" not in source


def test_destructive_profile_faults_only_positive_disposable_identity(tmp_path: Path):
    validation = validation_module()
    head = "d" * 40
    commands: list[list[str]] = []
    ingest_calls = 0

    def runner(command, **_kwargs):
        nonlocal ingest_calls
        command = list(command)
        commands.append(command)
        if command[:4] == ["git", "-C", str(SCRIPTS.parent), "rev-parse"]:
            return subprocess.CompletedProcess(
                command, 0, stdout=head + "\n", stderr=""
            )
        if command[:4] == ["git", "-C", str(SCRIPTS.parent), "status"]:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command[0] == str(SCRIPTS / "disposable-test-services"):
            action = command[-1]
            pg_port = int(command[command.index("--pg-port") + 1])
            qdrant_port = int(command[command.index("--qdrant-port") + 1])
            if action == "env" and (pg_port == 55432 or qdrant_port == 6333):
                return subprocess.CompletedProcess(
                    command, 1, stdout="", stderr="reserved"
                )
            if action == "up":
                namespace = command[command.index("--namespace") + 1]
                db = namespace.replace("-", "_") + "_test"
                payload = {
                    "schema_version": "firecrawl-disposable-services-v1",
                    "namespace": namespace,
                    "postgres": {"port": pg_port, "database": db},
                    "qdrant": {"port": qdrant_port},
                    "environment": {
                        "RESEARCH_STORE_TEST_DATABASE_URL": (
                            f"postgresql://postgres:postgres@127.0.0.1:{pg_port}/{db}"
                        ),
                        "RESEARCH_STORE_TEST_ALLOW_RESET": db,
                        "QDRANT_URL": f"http://127.0.0.1:{qdrant_port}",
                        "RESEARCH_STORE_TEST_QDRANT_URL": (
                            f"http://127.0.0.1:{qdrant_port}"
                        ),
                        "RESEARCH_STORE_TEST_QDRANT_ALLOW_RESET": (
                            f"http://127.0.0.1:{qdrant_port}"
                        ),
                    },
                }
                return subprocess.CompletedProcess(
                    command, 0, stdout=json.dumps(payload), stderr=""
                )
            if action == "down":
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command[:2] == [str(SCRIPTS / "research-db"), "migrate"]:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command[:2] == [str(SCRIPTS / "research-db"), "ingest-ready"]:
            ingest_calls += 1
            rc = 1 if ingest_calls == 2 else 0
            return subprocess.CompletedProcess(command, rc, stdout="", stderr="")
        raise AssertionError(command)

    artifact_root = tmp_path / "destructive-artifacts"
    args = _args(
        tmp_path,
        profile="destructive",
        max_operations=10,
        expected_head_sha=head,
        artifact_root=str(artifact_root),
        disposable_namespace="fc359",
        disposable_pg_port=55436,
        disposable_qdrant_port=55437,
    )
    campaign = validation.DisposableDestructiveCampaign(args, runner=runner)
    campaign._inject_schema_fault = mock.Mock()

    assert campaign.execute() == 0
    campaign._inject_schema_fault.assert_called_once_with()
    names = [case["name"] for case in campaign.cases]
    assert names.count("disposable_setup") == 1
    assert names.count("disposable_recovery_setup") == 1
    destination = artifact_root / args.run_id
    manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    report = (destination / "report.md").read_text(encoding="utf-8")
    assert manifest["implementation_head_sha"] == head
    assert manifest["accounting"]["declared_matrix_cases"] == 12
    assert manifest["accounting"]["executed_matrix_cases"] == 12
    assert manifest["accounting"]["not_run_cases"] == 0
    assert manifest["cleanup"]["result"] == "PASS"
    assert manifest["host_evidence"] == "PASS"
    assert "Implementation HEAD" in report
    assert "| controlled_schema_fault |" in report
    assert "## Accounting" in report
    helper_commands = [
        command
        for command in commands
        if command and command[0] == str(SCRIPTS / "disposable-test-services")
    ]
    protected_postgres = [
        command
        for command in helper_commands
        if command[command.index("--pg-port") + 1] == "55432"
    ]
    protected_qdrant = [
        command
        for command in helper_commands
        if command[command.index("--qdrant-port") + 1] == "6333"
    ]
    assert len(protected_postgres) == 1
    assert protected_postgres[0][-1] == "env"
    assert len(protected_qdrant) == 1
    assert protected_qdrant[0][-1] == "env"
    destructive_starts = [command for command in helper_commands if command[-1] == "up"]
    assert destructive_starts
    assert all(
        command[command.index("--pg-port") + 1] == "55436"
        for command in destructive_starts
    )
    assert all(
        command[command.index("--qdrant-port") + 1] == "55437"
        for command in destructive_starts
    )


def test_successful_capability_is_distinct_positive_evidence(tmp_path: Path):
    validation = validation_module()
    payload = {
        "schema_version": "authoritative-fscrape-v1",
        "status": "complete",
        "run_id": "00000000-0000-4000-8000-000000000001",
        "research_run_id": "fr_" + "1" * 32,
        "batch_id": "00000000-0000-4000-8000-000000000002",
        "invocation_id": "00000000-0000-4000-8000-000000000002",
        "external_invocation_id": "fc_" + "2" * 32,
        "idempotency_key": None,
        "replayed": False,
        "items": [{"status": "succeeded", "chunk_ids": []}],
        "item_count": 1,
        "items_truncated": False,
        "corpus_ids": {},
    }

    def runner(command, **_kwargs):
        return subprocess.CompletedProcess(
            command, 0, stdout=json.dumps(payload), stderr=""
        )

    campaign = validation.Campaign(
        _args(tmp_path),
        inspector=_Inspector(),
        runner=runner,
        real_cli="/usr/bin/firecrawl",
        work_root=tmp_path / "work",
    )
    try:
        case = campaign.run(
            "successful_capability",
            ["fscrape"],
            json_output=True,
            expected_schema="authoritative-fscrape-v1",
            required_capability=True,
            capability_evaluator=validation._fscrape_success_capability,
        )
        assert case["contract_result"] == "PASS"
        assert case["capability_result"] == "PASS"
    finally:
        campaign.close()


def test_wrongly_typed_failure_is_contract_failure(tmp_path: Path):
    validation = validation_module()

    def runner(command, **_kwargs):
        return subprocess.CompletedProcess(
            command,
            5,
            stdout=json.dumps(
                {
                    "schema_version": "authoritative-fscrape-error-v1",
                    "status": "failed",
                    "failure_stage": "extraction",
                }
            ),
            stderr="",
        )

    campaign = validation.Campaign(
        _args(tmp_path),
        inspector=_Inspector(),
        runner=runner,
        real_cli="/usr/bin/firecrawl",
        work_root=tmp_path / "work",
    )
    try:
        case = campaign.run(
            "wrong_failure_contract",
            ["fscrape"],
            json_output=True,
            expected_returncodes=(5,),
            expected_schema="authoritative-fscrape-error-v1",
        )
        assert case["contract_result"] == "FAIL"
        assert case["capability_result"] == "NOT_EVALUATED"
    finally:
        campaign.close()


def test_accounting_materializes_declared_not_run_cases_and_destructive_boundary(
    tmp_path: Path,
):
    validation = validation_module()
    campaign = validation.Campaign(
        _args(tmp_path),
        inspector=_Inspector(),
        real_cli="/usr/bin/firecrawl",
        work_root=tmp_path / "work",
    )
    try:
        campaign._record(
            "retired_smart_option_dry_run",
            category="matrix",
            contract_result="PASS",
        )
        campaign._record("plumbing-pass", category="plumbing", contract_result="PASS")
        campaign._materialize_unexecuted_matrix_cases()
        accounting = campaign._accounting()
        assert accounting["declared_matrix_cases"] == 9
        assert accounting["executed_matrix_cases"] == 1
        assert accounting["plumbing_operations"] == 1
        assert accounting["not_run_cases"] == 8
        assert accounting["not_run_plumbing_operations"] == 0
        assert accounting["passed_contract_cases"] == 2
        boundary = next(
            case
            for case in campaign.cases
            if case["name"] == "persistent_destructive_postgres_not_run"
        )
        assert boundary["contract_result"] == "NOT_EVALUATED"
        assert boundary["required_contract"] is False
        assert boundary["observed_disposition"] == "requires_disposable_profile"
    finally:
        campaign.close()


def test_no_baseline_never_claims_unrelated_runs(tmp_path: Path):
    validation = validation_module()
    inspector = _Inspector()
    unrelated = "fr_" + "d" * 32
    inspector.run_ids = {unrelated}
    inspector.states[unrelated] = "acquiring"
    campaign = validation.Campaign(
        _args(tmp_path),
        inspector=inspector,
        real_cli="/usr/bin/firecrawl",
        work_root=tmp_path / "work",
    )
    try:
        campaign._discover_owned_runs()
        assert campaign.owned_runs == {}
    finally:
        campaign.close()


def test_objective_bound_discovery_does_not_claim_concurrent_unrelated_run(
    tmp_path: Path,
):
    validation = validation_module()
    inspector = _Inspector()
    unrelated = "fr_" + "e" * 32
    owned = "fr_" + "f" * 32
    campaign = validation.Campaign(
        _args(tmp_path),
        inspector=inspector,
        real_cli="/usr/bin/firecrawl",
        work_root=tmp_path / "work",
    )
    campaign.run_baseline_captured = True
    objective = campaign._owned_objective("owned-case", "bounded objective")
    inspector.run_ids = {unrelated, owned}
    inspector.objectives[objective] = {owned}
    inspector.states = {unrelated: "acquiring", owned: "acquiring"}
    try:
        campaign._discover_owned_runs()
        assert set(campaign.owned_runs) == {owned}
        assert unrelated not in campaign.owned_runs
    finally:
        campaign.close()


def test_additional_same_tag_run_is_not_claimed_and_cleanup_fails(tmp_path: Path):
    validation = validation_module()
    inspector = _Inspector()
    tracked = "fr_" + "7" * 32
    ambiguous = "fr_" + "8" * 32
    campaign = validation.Campaign(
        _args(tmp_path),
        inspector=inspector,
        real_cli="/usr/bin/firecrawl",
        work_root=tmp_path / "work",
    )
    campaign.run_baseline_captured = True
    objective = campaign._owned_objective("owned-case", "bounded objective")
    inspector.run_ids = {tracked, ambiguous}
    inspector.objectives[objective] = {tracked, ambiguous}
    inspector.states = {tracked: "completed", ambiguous: "acquiring"}
    campaign._track_run("owned-case", tracked, objective)
    try:
        campaign._discover_owned_runs()
        assert set(campaign.owned_runs) == {tracked}
        assert ambiguous not in campaign.owned_runs
        evidence = campaign.cleanup_runs()
        assert evidence["result"] == "FAIL"
        assert evidence["owned_run_ids"] == [tracked]
        assert evidence["already_terminal"] == [tracked]
        assert evidence["failures"][0]["run_id"] == "<ownership-discovery>"
        assert ambiguous in inspector.states
        assert inspector.states[ambiguous] == "acquiring"
    finally:
        campaign.close()


def test_ambiguous_untracked_runs_are_not_claimed_and_cleanup_fails(tmp_path: Path):
    validation = validation_module()
    inspector = _Inspector()
    first = "fr_" + "9" * 32
    second = "fr_" + "a" * 32
    campaign = validation.Campaign(
        _args(tmp_path),
        inspector=inspector,
        real_cli="/usr/bin/firecrawl",
        work_root=tmp_path / "work",
    )
    campaign.run_baseline_captured = True
    objective = campaign._owned_objective("ambiguous-case", "bounded objective")
    inspector.run_ids = {first, second}
    inspector.objectives[objective] = {first, second}
    inspector.states = {first: "acquiring", second: "created"}
    try:
        evidence = campaign.cleanup_runs()
        assert campaign.owned_runs == {}
        assert evidence["result"] == "FAIL"
        assert evidence["owned_run_ids"] == []
        assert evidence["failures"][0]["run_id"] == "<ownership-discovery>"
        assert inspector.states == {first: "acquiring", second: "created"}
    finally:
        campaign.close()


def test_cleanup_failure_is_machine_readable_failure(tmp_path: Path):
    validation = validation_module()
    inspector = _Inspector()
    owned = "fr_" + "1" * 32
    inspector.run_ids = {owned}
    inspector.states[owned] = "created"

    def runner(command, **_kwargs):
        return subprocess.CompletedProcess(
            command, 1, stdout="", stderr="cancel failed"
        )

    campaign = validation.Campaign(
        _args(tmp_path),
        inspector=inspector,
        runner=runner,
        real_cli="/usr/bin/firecrawl",
        work_root=tmp_path / "work",
    )
    campaign.run_baseline_captured = True
    campaign._track_run("owned", owned, "owned")
    try:
        evidence = campaign.cleanup_runs()
        assert evidence["result"] == "FAIL"
        assert evidence["failures"][0]["run_id"] == owned
        assert campaign.cases[-1]["contract_result"] == "FAIL"
    finally:
        campaign.close()


def test_cleanup_cancel_timeout_is_machine_readable_failure(tmp_path: Path):
    validation = validation_module()
    inspector = _Inspector()
    owned = "fr_" + "3" * 32
    inspector.run_ids = {owned}
    inspector.states[owned] = "created"

    def runner(command, **_kwargs):
        raise subprocess.TimeoutExpired(command, 60)

    campaign = validation.Campaign(
        _args(tmp_path),
        inspector=inspector,
        runner=runner,
        real_cli="/usr/bin/firecrawl",
        work_root=tmp_path / "work",
    )
    campaign.run_baseline_captured = True
    campaign._track_run("owned", owned, "owned")
    try:
        evidence = campaign.cleanup_runs()
        assert evidence["result"] == "FAIL"
        assert evidence["failures"][0]["run_id"] == owned
        assert "TimeoutExpired" in evidence["failures"][0]["error"]
        assert campaign.cases[-1]["contract_result"] == "FAIL"
    finally:
        campaign.close()


def test_execute_exception_still_terminalizes_owned_run(tmp_path: Path):
    validation = validation_module()
    inspector = _Inspector()
    owned = "fr_" + "4" * 32
    inspector.run_ids = {owned}
    inspector.states[owned] = "acquiring"

    def runner(command, **_kwargs):
        command = list(command)
        if len(command) >= 3 and command[1] == "cancel":
            inspector.states[command[2]] = "cancelled"
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    campaign = validation.Campaign(
        _args(tmp_path),
        inspector=inspector,
        runner=runner,
        real_cli="/usr/bin/firecrawl",
        work_root=tmp_path / "work",
    )
    campaign.run_baseline_captured = True
    campaign._track_run("owned", owned, "owned")
    campaign.preflight = mock.Mock(return_value=True)
    campaign.validate_retired_smart_options = mock.Mock(
        side_effect=RuntimeError("synthetic execution failure")
    )
    try:
        assert campaign.execute() == 2
        assert inspector.states[owned] == "cancelled"
        assert any(
            case["name"] == "validator_execution_error"
            and case["contract_result"] == "FAIL"
            for case in campaign.cases
        )
        cleanup_case = next(
            case
            for case in campaign.cases
            if case["name"] == "validator_owned_run_cleanup"
        )
        assert cleanup_case["details"]["result"] == "PASS"
        assert cleanup_case["details"]["cancelled"] == [owned]
    finally:
        campaign.close()


def test_keep_runs_is_reported_and_cannot_be_clean_cleanup(tmp_path: Path):
    validation = validation_module()
    inspector = _Inspector()
    owned = "fr_" + "2" * 32
    inspector.run_ids = {owned}
    inspector.states[owned] = "created"
    campaign = validation.Campaign(
        _args(tmp_path, keep_runs=True),
        inspector=inspector,
        real_cli="/usr/bin/firecrawl",
        work_root=tmp_path / "work",
    )
    campaign.run_baseline_captured = True
    campaign._track_run("owned", owned, "owned")
    try:
        evidence = campaign.cleanup_runs()
        assert evidence["result"] == "NOT_RUN"
        assert evidence["retained"] == [owned]
        assert campaign.cases[-1]["contract_result"] == "NOT_EVALUATED"
    finally:
        campaign.close()


def test_malformed_disposable_receipt_still_tears_down_started_namespace(
    tmp_path: Path,
):
    validation = validation_module()
    head = "a" * 40
    commands: list[list[str]] = []

    def runner(command, **_kwargs):
        command = list(command)
        commands.append(command)
        if command[:4] == ["git", "-C", str(SCRIPTS.parent), "rev-parse"]:
            return subprocess.CompletedProcess(
                command, 0, stdout=head + "\n", stderr=""
            )
        if command[:4] == ["git", "-C", str(SCRIPTS.parent), "status"]:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command[0] == str(SCRIPTS / "disposable-test-services"):
            action = command[-1]
            pg_port = int(command[command.index("--pg-port") + 1])
            qdrant_port = int(command[command.index("--qdrant-port") + 1])
            if action == "env" and (pg_port == 55432 or qdrant_port == 6333):
                return subprocess.CompletedProcess(
                    command, 1, stdout="", stderr="reserved"
                )
            if action == "up":
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=json.dumps({"schema_version": "wrong-v1"}),
                    stderr="",
                )
            if action == "down":
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        raise AssertionError(command)

    args = _args(
        tmp_path,
        profile="destructive",
        max_operations=10,
        expected_head_sha=head,
        disposable_namespace="fc359bad",
    )
    campaign = validation.DisposableDestructiveCampaign(args, runner=runner)
    assert campaign.execute() == 1
    assert any(command[-1] == "down" for command in commands)
    assert campaign._service_started is False


def test_disposable_up_timeout_still_attempts_owned_teardown(tmp_path: Path):
    validation = validation_module()
    head = "c" * 40
    commands: list[list[str]] = []

    def runner(command, **_kwargs):
        command = list(command)
        commands.append(command)
        if command[:4] == ["git", "-C", str(SCRIPTS.parent), "rev-parse"]:
            return subprocess.CompletedProcess(
                command, 0, stdout=head + "\n", stderr=""
            )
        if command[:4] == ["git", "-C", str(SCRIPTS.parent), "status"]:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command[0] == str(SCRIPTS / "disposable-test-services"):
            action = command[-1]
            pg_port = int(command[command.index("--pg-port") + 1])
            qdrant_port = int(command[command.index("--qdrant-port") + 1])
            if action == "env" and (pg_port == 55432 or qdrant_port == 6333):
                return subprocess.CompletedProcess(
                    command, 1, stdout="", stderr="reserved"
                )
            if action == "up":
                raise subprocess.TimeoutExpired(command, 30)
            if action == "down":
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        raise AssertionError(command)

    args = _args(
        tmp_path,
        profile="destructive",
        max_operations=10,
        expected_head_sha=head,
        disposable_namespace="fc359timeout",
    )
    campaign = validation.DisposableDestructiveCampaign(args, runner=runner)
    assert campaign.execute() == 1
    assert any(command[-1] == "down" for command in commands)
    assert campaign._service_started is False
    setup_case = next(
        case for case in campaign.cases if case["name"] == "disposable_setup"
    )
    assert setup_case["returncode"] == 124
    assert setup_case["contract_result"] == "FAIL"


def test_destructive_teardown_failure_propagates_failure(tmp_path: Path):
    validation = validation_module()
    head = "b" * 40
    ingest_calls = 0
    commands: list[list[str]] = []

    def runner(command, **_kwargs):
        nonlocal ingest_calls
        command = list(command)
        commands.append(command)
        if command[:4] == ["git", "-C", str(SCRIPTS.parent), "rev-parse"]:
            return subprocess.CompletedProcess(
                command, 0, stdout=head + "\n", stderr=""
            )
        if command[:4] == ["git", "-C", str(SCRIPTS.parent), "status"]:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command[0] == str(SCRIPTS / "disposable-test-services"):
            action = command[-1]
            pg_port = int(command[command.index("--pg-port") + 1])
            qdrant_port = int(command[command.index("--qdrant-port") + 1])
            if action == "env" and (pg_port == 55432 or qdrant_port == 6333):
                return subprocess.CompletedProcess(
                    command, 1, stdout="", stderr="reserved"
                )
            if action == "up":
                namespace = command[command.index("--namespace") + 1]
                db = namespace.replace("-", "_") + "_test"
                qurl = f"http://127.0.0.1:{qdrant_port}"
                payload = {
                    "schema_version": "firecrawl-disposable-services-v1",
                    "namespace": namespace,
                    "postgres": {"port": pg_port, "database": db},
                    "qdrant": {"port": qdrant_port},
                    "environment": {
                        "RESEARCH_STORE_TEST_DATABASE_URL": (
                            f"postgresql://postgres:postgres@127.0.0.1:{pg_port}/{db}"
                        ),
                        "RESEARCH_STORE_TEST_ALLOW_RESET": db,
                        "QDRANT_URL": qurl,
                        "RESEARCH_STORE_TEST_QDRANT_URL": qurl,
                        "RESEARCH_STORE_TEST_QDRANT_ALLOW_RESET": qurl,
                    },
                }
                return subprocess.CompletedProcess(
                    command, 0, stdout=json.dumps(payload), stderr=""
                )
            if action == "down":
                return subprocess.CompletedProcess(
                    command, 1, stdout="", stderr="teardown failed"
                )
        if command[:2] == [str(SCRIPTS / "research-db"), "migrate"]:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command[:2] == [str(SCRIPTS / "research-db"), "ingest-ready"]:
            ingest_calls += 1
            return subprocess.CompletedProcess(
                command,
                1 if ingest_calls == 2 else 0,
                stdout="",
                stderr="",
            )
        raise AssertionError(command)

    artifact_root = tmp_path / "destructive-failure-artifacts"
    args = _args(
        tmp_path,
        profile="destructive",
        max_operations=10,
        expected_head_sha=head,
        artifact_root=str(artifact_root),
        disposable_namespace="fc359down",
    )
    campaign = validation.DisposableDestructiveCampaign(args, runner=runner)
    campaign._inject_schema_fault = mock.Mock()
    assert campaign.execute() == 1
    assert campaign._service_started is True
    assert any(
        case["name"] in {"disposable_fault_teardown", "disposable_final_teardown"}
        and case["contract_result"] == "FAIL"
        for case in campaign.cases
    )
    teardown_cases = [
        case
        for case in campaign.cases
        if case["name"] in {"disposable_fault_teardown", "disposable_final_teardown"}
        and case["contract_result"] != "NOT_EVALUATED"
    ]
    assert teardown_cases
    assert any(case["contract_result"] == "FAIL" for case in teardown_cases)
    manifest = json.loads(
        (artifact_root / args.run_id / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["cleanup"]["result"] == "FAIL"
    assert manifest["host_evidence"] == "FAIL"
    up_commands = [
        command
        for command in commands
        if command
        and command[0] == str(SCRIPTS / "disposable-test-services")
        and command[-1] == "up"
    ]
    assert len(up_commands) == 1
