import importlib.util
import json
import os
import subprocess
import textwrap
from importlib.machinery import SourceFileLoader
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from research_store.semantic_service import SemanticCallService

SCRIPTS = Path(__file__).resolve().parent


def load_module(name, path):
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cleanup = load_module("firecrawl_cleanup", SCRIPTS / "cleanup.py")
classifier = load_module("firecrawl_classifier", SCRIPTS / "classifier.py")
smart = load_module("firecrawl_smart", SCRIPTS / "fsearch_smart")
invocations = load_module("firecrawl_invocation_id", SCRIPTS / "invocation_id.py")
gateway = load_module("firecrawl_model_gateway", SCRIPTS / "model_gateway.py")
research = load_module("firecrawl_research_workflow", SCRIPTS / "research_workflow.py")
live_validation = load_module("firecrawl_live_validate", SCRIPTS / "live_validate.py")


class MemorySemanticRepository:
    def __init__(self, execution_mode="autonomous_local"):
        self.calls = {}
        self.artifacts = {}
        self.status = {
            "execution_mode": execution_mode,
            "lifecycle_revision": 0,
        }

    def get_run_status(self, *, run_id=None, external_id=None):
        return {"id": run_id, **self.status}

    def record_semantic_call(
        self,
        run_id,
        stage,
        provider,
        model,
        prompt_version,
        request,
        idempotency_key,
        **metadata,
    ):
        existing = next(
            (
                item
                for item in self.calls.values()
                if item["run_id"] == run_id
                and item["idempotency_key"] == idempotency_key
            ),
            None,
        )
        candidate = {
            "run_id": run_id,
            "stage": stage,
            "provider": provider,
            "model": model,
            "prompt_version": prompt_version,
            "request": request,
            "idempotency_key": idempotency_key,
            "status": metadata.get("status", "pending"),
            "response_metadata": {},
            "error": None,
            **metadata,
        }
        if existing:
            assert {key: existing[key] for key in candidate} == candidate
            return existing["id"]
        call_id = uuid4()
        self.calls[call_id] = {"id": call_id, **candidate}
        return call_id

    def finalize_semantic_call(
        self, run_id, call_id, status, response_metadata, error=None
    ):
        call = self.calls[call_id]
        assert call["run_id"] == run_id
        call.update(status=status, response_metadata=response_metadata, error=error)
        return call_id

    def annotate_semantic_call(self, run_id, call_id, metadata):
        assert self.calls[call_id]["run_id"] == run_id
        self.calls[call_id]["response_metadata"].update(metadata)
        return call_id

    def record_semantic_artifact(
        self,
        run_id,
        semantic_call_id,
        artifact_type,
        schema_name,
        schema_version,
        payload,
        idempotency_key,
        **metadata,
    ):
        artifact_id = uuid4()
        self.artifacts[artifact_id] = {
            "id": artifact_id,
            "run_id": run_id,
            "semantic_call_id": semantic_call_id,
            "artifact_type": artifact_type,
            "schema_name": schema_name,
            "schema_version": schema_version,
            "payload": payload,
            "idempotency_key": idempotency_key,
            **metadata,
        }
        return artifact_id

    def get_semantic_call(self, run_id, call_id):
        call = dict(self.calls[call_id])
        assert call["run_id"] == run_id
        call["artifacts"] = [
            item
            for item in self.artifacts.values()
            if item["semantic_call_id"] == call_id
        ]
        return call


class MemorySemanticUow:
    def __init__(self, repository):
        self.runs = repository

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def semantic_fixture(execution_mode="autonomous_local"):
    repository = MemorySemanticRepository(execution_mode)
    service = SemanticCallService(lambda: MemorySemanticUow(repository))
    context = {
        "run_id": uuid4(),
        "stage": "planning",
        "schema_name": "test-result",
        "schema_version": 1,
        "artifact_type": "test_result",
        "run_revision": 0,
        "idempotency_key": f"semantic:{uuid4()}",
        "input_artifact_ids": [uuid4()],
    }
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"result": {"type": "string"}},
        "required": ["result"],
    }
    return repository, service, context, schema


@pytest.fixture
def fake_cli(tmp_path):
    bin_dir = tmp_path / "fake bin"
    bin_dir.mkdir()
    executable = bin_dir / "firecrawl"
    executable.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import json
            import os
            from pathlib import Path
            import sys
            from urllib.parse import quote

            args = sys.argv[1:]
            log = os.environ.get("FAKE_FIRECRAWL_LOG")
            if log:
                with open(log, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(args) + "\\n")
            if not args or args[0] in ("--version", "version"):
                print("9.9.9-test")
                raise SystemExit(0)
            command = args[0]
            failure_count_path = os.environ.get("FAKE_FIRECRAWL_FAILURE_COUNT")
            failures_before_success = int(os.environ.get("FAKE_FIRECRAWL_FAIL_SEARCH_ATTEMPTS", "0"))
            if command == "search" and failures_before_success:
                prior_failures = int(
                    Path(failure_count_path).read_text()
                    if failure_count_path and Path(failure_count_path).exists()
                    else "0"
                )
                if prior_failures < failures_before_success:
                    if failure_count_path:
                        Path(failure_count_path).write_text(str(prior_failures + 1))
                    print("Error: getaddrinfo EAI_AGAIN garion.us", file=sys.stderr)
                    raise SystemExit(1)
            if "-o" not in args:
                print("missing output", file=sys.stderr)
                raise SystemExit(2)
            output = Path(args[args.index("-o") + 1])
            output.parent.mkdir(parents=True, exist_ok=True)
            if command == "search":
                query = args[1]
                if "no-output" in query:
                    print("No results found.")
                    raise SystemExit(0)
                results = [] if "zero-results" in query else [
                    {"url": f"https://example.com/{quote(query)}/{index}", "title": f"Result {index}", "description": f"{query} evidence facet {index}"}
                    for index in range(3)
                ]
                output.write_text(json.dumps({"data": {"web": results}, "id": "test-search"}), encoding="utf-8")
            elif command == "scrape":
                if "--schema" in args or "--schema-file" in args:
                    content = json.dumps({"data": {"json": {"product_name": "Portable Widget", "headline": "Portable News"}, "metadata": {"title": "Structured Result"}}})
                elif "--format" in args and args[args.index("--format") + 1] == "links":
                    content = "https://example.com/a\\nhttps://example.com/b\\n"
                else:
                    content = "# Portable Result\\n\\n" + ("relevant portable evidence content " * 90)
                output.write_text(content, encoding="utf-8")
            else:
                print(f"unsupported command: {command}", file=sys.stderr)
                raise SystemExit(2)
            """
        ),
        encoding="utf-8",
    )
    executable.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["FAKE_FIRECRAWL_LOG"] = str(tmp_path / "calls.jsonl")
    env["FIRECRAWL_AUDIT_AUTO_SEMANTIC"] = "0"
    env["FIRECRAWL_RESEARCH_AUTO_ENV"] = "0"
    env["DATABASE_URL"] = ""
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env, tmp_path


def run_script(name, *args, env=None):
    return subprocess.run(  # noqa: PLW1510
        [str(SCRIPTS / name), *map(str, args)],
        text=True,
        capture_output=True,
        env=env,
        timeout=30,
    )


def test_cleanup_preserves_code_and_removes_boilerplate():
    source = "<!-- hidden -->\r\nCookie policy\r\n```text\r\nCookie policy\r\n```\r\n[Docs](https://example.com?a=1&utm_source=x)"
    cleaned = cleanup.clean_markdown(source)
    assert "hidden" not in cleaned
    assert cleaned.count("Cookie policy") == 1
    assert "utm_source" not in cleaned
    assert "https://example.com?a=1" in cleaned


@pytest.mark.parametrize(
    ("url", "title", "snippet", "expected"),
    [
        ("https://example.com/product/widget", "Widget", "Price: $5", "ecommerce"),
        ("https://reddit.com/r/test/comments/1", "Thread", "Replies", "forum"),
        ("https://apnews.com/article/test", "News", "Reported by AP", "news_article"),
        (
            "https://example.com/podcast/episode",
            "Episode",
            "Hosted by X",
            "media_release",
        ),
        (
            "https://plato.stanford.edu/entries/test",
            "Argument",
            "Premise 1",
            "academic_debate",
        ),
        (
            "https://example.com/reference",
            "Reference",
            "Neutral prose",
            "editorial_markdown",
        ),
    ],
)
def test_all_classifier_profiles(url, title, snippet, expected):
    category, matched = classifier.classify_target(url, title, snippet)
    assert category == expected
    assert matched is (expected != "editorial_markdown")


def test_invocation_id_format_and_validation():
    first = invocations.new_invocation_id()
    second = invocations.new_invocation_id()
    assert first != second
    assert invocations.ID_PATTERN.fullmatch(first)
    assert invocations.validate_invocation_id(first) == first
    with pytest.raises(ValueError):
        invocations.validate_invocation_id("unsafe/path")


def test_fsearch_requires_authoritative_run_before_firecrawl(fake_cli):
    env, tmp_path = fake_cli
    env["TMPDIR"] = str(tmp_path / "scratch root")

    result = run_script(
        "fsearch",
        "first query",
        "--limit",
        "3",
        "--scrape-limit",
        "0",
        env=env,
    )

    assert result.returncode == 2
    assert "--research-run-id or FIRECRAWL_RESEARCH_RUN_ID is required" in result.stderr
    assert not Path(env["FAKE_FIRECRAWL_LOG"]).exists()
    assert not (Path(env["TMPDIR"]) / "firecrawl_scratch").exists()


@pytest.mark.parametrize(
    ("removed_args", "expected"),
    [
        (["--dir", "deprecated-output"], "--dir was removed"),
        (["--reuse-search"], "--reuse-search was removed"),
        (["--scrape-ranks", "1,3"], "--scrape-ranks was removed"),
    ],
)
def test_fsearch_rejects_removed_scratch_options_before_firecrawl(
    fake_cli, removed_args, expected
):
    env, tmp_path = fake_cli

    result = run_script(
        "fsearch",
        "portable",
        "--research-run-id",
        "fr_" + "a" * 32,
        *removed_args,
        env=env,
    )

    assert result.returncode == 2
    assert expected in result.stderr
    assert not Path(env["FAKE_FIRECRAWL_LOG"]).exists()
    assert list(tmp_path.glob("**/_meta.json")) == []
    assert list(tmp_path.glob("**/_search.json")) == []


def test_fsearch_missing_database_fails_before_firecrawl(fake_cli):
    env, tmp_path = fake_cli

    result = run_script(
        "fsearch",
        "no-output",
        "--research-run-id",
        "fr_" + "c" * 32,
        "--json",
        env=env,
    )

    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert payload["failure_stage"] == "preflight"
    assert "DATABASE_URL is required" in payload["error"]
    assert not Path(env["FAKE_FIRECRAWL_LOG"]).exists()
    assert not any(tmp_path.glob("**/_search.json"))


def test_fsearch_metadata_adapter_retries_transient_transport_without_files(tmp_path):
    from types import SimpleNamespace

    from research_store.fsearch_service import MetadataOnlyFirecrawlSearchAdapter

    calls = []

    def runner(command, **_kwargs):
        calls.append(command)
        if len(calls) == 1:
            return SimpleNamespace(
                returncode=1,
                stdout=b"",
                stderr=b"Error: getaddrinfo EAI_AGAIN example.org",
            )
        return SimpleNamespace(
            returncode=0,
            stdout=b'{"success":true,"data":{"web":[]}}',
            stderr=b"",
        )

    result = MetadataOnlyFirecrawlSearchAdapter(runner=runner).search(
        "retry query", retries=1
    )

    assert len(calls) == 2
    assert result.http_status == 200
    assert result.transport_error is None
    assert result.transport_metadata["attempts"] == 2
    assert result.transport_metadata["implicit_scrape"] is False
    assert "-o" not in calls[0]
    assert list(tmp_path.iterdir()) == []


def test_fscrape_rejects_undocumented_format(fake_cli):
    env, _ = fake_cli
    result = run_script("fscrape", "https://example.com", "--format", "text", env=env)
    assert result.returncode == 1
    assert "unsupported format" in result.stderr


def test_smart_search_rejects_removed_arguments(fake_cli):
    """Removed command-line arguments fail instead of silently doing nothing."""
    env, _ = fake_cli
    result = run_script(
        "fsearch_smart",
        "bounded policy",
        "--invalid-flag",
        "test",
        env=env,
    )
    assert result.returncode == 2
    assert "unrecognized arguments" in result.stderr


def test_v5_local_gateway_records_empty_reasoning_retry_and_provenance(monkeypatch):
    assessment = {"result": "partial"}

    class Response:
        def __init__(self, payload, headers=None):
            self.payload = payload
            self.headers = headers or {}
            self.status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return json.dumps(self.payload).encode()

    calls = []

    def fake_urlopen(request, timeout):
        if request.full_url.endswith("/models"):
            return Response({"data": [{"id": "chat", "max_model_len": 262144}]})
        calls.append(request)
        if len(calls) == 1:
            return Response(
                {
                    "id": "chatcmpl-empty",
                    "model": "chat",
                    "usage": {"completion_tokens": 4096},
                    "choices": [
                        {
                            "finish_reason": "length",
                            "message": {
                                "content": "",
                                "reasoning": "long internal reasoning",
                            },
                        }
                    ],
                }
            )
        return Response(
            {
                "id": "chatcmpl-test",
                "model": "chat",
                "system_fingerprint": "fp-test",
                "usage": {"prompt_tokens": 10, "completion_tokens": 20},
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": json.dumps(assessment)},
                    }
                ],
            },
            {"x-request-id": "req-test"},
        )

    monkeypatch.setattr(gateway, "urlopen", fake_urlopen)
    result = gateway.call_structured(
        "local",
        None,
        "system",
        "user",
        {
            "type": "object",
            "properties": {"result": {"type": "string"}},
            "required": ["result"],
        },
        max_output_tokens=4096,
        max_attempts=2,
    )
    assert result.value == assessment
    assert result.provenance["requested_model"] == "chat"
    assert result.provenance["capability_probe"]["max_context_tokens"] == 262144
    assert result.attempts[0]["finish_reason"] == "length"
    assert result.attempts[0]["reasoning_excerpt"]
    assert result.attempts[1]["structured_mode"] == "json_schema"


def test_semantic_gateway_persists_success_and_redacts_sensitive_data(monkeypatch):
    repository, persistence, context, schema = semantic_fixture()

    class Response:
        status = 200
        headers = {"x-request-id": "req-secret"}  # noqa: RUF012

        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return json.dumps(self.payload).encode()

    transport_bodies = []

    def fake_urlopen(request, timeout):
        if request.full_url.endswith("/models"):
            return Response({"data": [{"id": "chat", "max_model_len": 1000}]})
        transport_bodies.append(request.data.decode())
        return Response(
            {
                "id": "call-success",
                "model": "chat",
                "usage": {"completion_tokens": 4},
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                {"result": "https://example.test/?token=top-secret"}
                            )
                        },
                    }
                ],
            }
        )

    monkeypatch.setattr(gateway, "urlopen", fake_urlopen)
    result = gateway.call_structured(
        "local",
        None,
        "Authorization: Bearer prompt-secret",
        "api_key=user-secret",
        schema,
        max_attempts=1,
        prompt_version="test-v1",
        semantic_persistence=persistence,
        semantic_context=context,
    )
    assert result.value["result"].endswith("token=[REDACTED]")
    call = repository.calls[result.semantic_call_id]
    assert call["status"] == "complete"
    assert call["request"]["prompt_hash"] == result.provenance["prompt_hash"]
    assert call["request"]["schema"] == schema
    assert call["request"]["input_artifact_ids"] == [
        str(context["input_artifact_ids"][0])
    ]
    assert isinstance(call["request"]["input_token_estimate"], int)
    assert call["response_metadata"]["provenance"]["usage"]["completion_tokens"] == 4
    artifact = repository.artifacts[result.artifact_ids[-1]]
    assert artifact["validation_status"] == "valid"
    assert artifact["payload"]["result"].endswith("token=[REDACTED]")
    persisted = json.dumps({"call": call, "artifact": artifact}, default=str)
    assert "prompt-secret" not in persisted
    assert "user-secret" not in persisted
    assert "top-secret" not in persisted
    assert "prompt-secret" not in transport_bodies[0]
    assert "user-secret" not in transport_bodies[0]


@pytest.mark.parametrize("failure", ["invalid-json", "schema", "timeout"])
def test_semantic_gateway_persists_failure_paths(monkeypatch, failure):
    repository, persistence, context, schema = semantic_fixture()

    class Response:
        status = 200
        headers = {}  # noqa: RUF012

        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return json.dumps(self.payload).encode()

    def fake_urlopen(request, timeout):
        if request.full_url.endswith("/models"):
            return Response({"data": [{"id": "chat"}]})
        if failure == "timeout":
            raise TimeoutError("Bearer timeout-secret")
        content = (
            "{broken" if failure == "invalid-json" else json.dumps({"wrong": "shape"})
        )
        return Response(
            {
                "id": f"call-{failure}",
                "model": "chat",
                "choices": [{"finish_reason": "stop", "message": {"content": content}}],
            }
        )

    monkeypatch.setattr(gateway, "urlopen", fake_urlopen)
    result = gateway.call_structured(
        "local",
        None,
        "system",
        "user",
        schema,
        max_attempts=1,
        semantic_persistence=persistence,
        semantic_context=context,
    )
    call = repository.calls[result.semantic_call_id]
    assert result.value is None
    assert call["status"] == "failed"
    assert call["response_metadata"]["attempt_count"] == 1
    assert "timeout-secret" not in json.dumps(call, default=str)
    artifacts = [
        item
        for item in repository.artifacts.values()
        if item["semantic_call_id"] == result.semantic_call_id
    ]
    if failure == "schema":
        assert len(artifacts) == 1
        assert artifacts[0]["validation_status"] == "invalid"
        assert artifacts[0]["validation_errors"]
    else:
        assert artifacts == []


def test_semantic_fallback_is_explicit_and_keeps_both_calls(monkeypatch):
    repository, persistence, context, schema = semantic_fixture()
    monkeypatch.setenv("OPENAI_API_KEY", "provider-secret")

    class Response:
        status = 200
        headers = {}  # noqa: RUF012

        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return json.dumps(self.payload).encode()

    def fake_urlopen(request, timeout):
        if request.full_url.endswith("/models"):
            return Response({"data": [{"id": "chat"}]})
        if request.full_url.endswith("/chat/completions"):
            return Response(
                {
                    "id": "local-invalid",
                    "model": "chat",
                    "choices": [
                        {"finish_reason": "stop", "message": {"content": "not-json"}}
                    ],
                }
            )
        return Response(
            {
                "id": "response-fallback",
                "model": "gpt-test",
                "status": "completed",
                "usage": {"output_tokens": 3},
                "output_text": json.dumps({"result": "fallback"}),
            }
        )

    monkeypatch.setattr(gateway, "urlopen", fake_urlopen)
    monkeypatch.setitem(research.call_structured.__globals__, "urlopen", fake_urlopen)
    result = research._structured(
        "local",
        None,
        "system",
        "user",
        schema,
        "test-v1",
        max_output_tokens=10,
        fallback_provider="openai",
        fallback_model="gpt-test",
        semantic_persistence=persistence,
        semantic_context=context,
    )
    assert result.value == {"result": "fallback"}
    assert len(repository.calls) == 2
    primary = next(
        item for item in repository.calls.values() if item["provider"] == "local"
    )
    fallback = next(
        item for item in repository.calls.values() if item["provider"] == "openai"
    )
    assert primary["status"] == "failed"
    assert primary["response_metadata"]["fallback"]["used"] is True
    assert fallback["request"]["fallback_from_call_id"] == str(primary["id"])
    assert fallback["response_metadata"]["provenance"]["fallback"]["used"] is True
    assert result.provenance["fallback_from"]["provider"] == "local"


def test_host_agent_artifacts_share_validation_and_do_not_fake_transport_metadata():
    repository, persistence, context, schema = semantic_fixture("agent_led")
    valid = persistence.ingest_host_artifact(
        context, {"result": "Bearer host-secret"}, schema, actor_identifier="codex"
    )
    assert valid.value == {"result": "Bearer [REDACTED]"}
    call = repository.calls[UUID(valid.provenance["semantic_call_id"])]
    artifact = repository.artifacts[UUID(valid.provenance["semantic_artifact_id"])]
    assert call["provider"] == "host-agent"
    assert call["model"] == ""
    assert "endpoint_alias" not in call["request"]
    assert "prompt_hash" not in call["request"]
    assert call["response_metadata"]["transport_attempts"] == []
    assert artifact["payload"] == {"result": "Bearer [REDACTED]"}

    invalid_context = {
        **context,
        "idempotency_key": context["idempotency_key"] + ":invalid",
    }
    invalid = persistence.ingest_host_artifact(
        invalid_context, {"wrong": "shape"}, schema
    )
    invalid_artifact = repository.artifacts[
        UUID(invalid.provenance["semantic_artifact_id"])
    ]
    assert invalid.value is None
    assert invalid.error
    assert invalid_artifact["validation_status"] == "invalid"


def test_agent_led_supplied_artifact_suppresses_inner_model_call():
    repository, persistence, context, schema = semantic_fixture("agent_led")
    inner_calls = []

    result = persistence.decide(
        context,
        schema,
        host_artifact={"result": "host decision"},
        local_decision=lambda **kwargs: inner_calls.append(kwargs),
        actor_identifier="codex",
    )

    assert result.value == {"result": "host decision"}
    assert inner_calls == []
    call = repository.calls[UUID(result.provenance["semantic_call_id"])]
    assert call["provider"] == "host-agent"
    assert call["response_metadata"]["transport_attempts"] == []


def test_deterministic_debug_fixture_marks_semantic_coverage_unassessed():
    repository, persistence, context, schema = semantic_fixture("deterministic_debug")
    result = persistence.decide(
        context,
        schema,
        deterministic_fixture={"result": "fixture"},
        actor_identifier="pytest",
    )
    call = repository.calls[UUID(result.provenance["semantic_call_id"])]
    assert call["provider"] == "deterministic-fixture"
    assert call["response_metadata"]["semantic_coverage"] == "unassessed"


def test_autonomous_local_stage_can_retry_with_independent_attempt_key():
    repository, persistence, context, schema = semantic_fixture("autonomous_local")
    first = persistence.start_model_call(
        context,
        provider="local",
        requested_model="chat",
        model_revision="test",
        endpoint_alias="local",
        prompt_version="test-v1",
        prompt_hash="a" * 64,
        schema=schema,
        input_token_estimate=5,
    )
    persistence.finish_model_call(
        context,
        first,
        status="failed",
        provenance={"provider": "local"},
        attempts=[{"attempt": 1, "error": "timeout"}],
        artifacts=[],
        error="timeout",
    )
    retry_context = {
        **context,
        "idempotency_key": context["idempotency_key"] + ":attempt:2",
    }
    second = persistence.start_model_call(
        retry_context,
        provider="local",
        requested_model="chat",
        model_revision="test",
        endpoint_alias="local",
        prompt_version="test-v1",
        prompt_hash="a" * 64,
        schema=schema,
        input_token_estimate=5,
    )
    assert first != second
    assert repository.calls[first]["stage"] == repository.calls[second]["stage"]
    assert repository.calls[first]["status"] == "failed"
    assert repository.calls[second]["status"] == "running"


def test_v5_candidate_triage_rejects_irrelevant_volume(monkeypatch):
    candidates = [
        {
            "url": "https://vin.example/check",
            "title": "Free VIN Check",
            "snippet": "vehicle history",
            "rank": 1,
        },
        {
            "url": "https://apnews.com/article/iran",
            "title": "US and Iran conflict update",
            "snippet": "July reporting",
            "rank": 2,
        },
    ]

    class Result:
        value = {  # noqa: RUF012
            "decisions": [
                {
                    "candidate_id": "",
                    "relevance": "unrelated",
                    "source_suitability": "unsuitable",
                    "subquestions": [],
                    "freshness": "unknown",
                    "independence": "unknown",
                    "scrape": False,
                    "priority": 0,
                    "rationale": "vehicle lookup",
                },
                {
                    "candidate_id": "",
                    "relevance": "high",
                    "source_suitability": "authoritative_secondary",
                    "subquestions": ["latest developments"],
                    "freshness": "likely current",
                    "independence": "independent reporting",
                    "scrape": True,
                    "priority": 95,
                    "rationale": "directly addresses objective",
                },
            ]
        }
        provenance = {"provider": "local"}  # noqa: RUF012
        attempts = []  # noqa: RUF012
        error = ""

    def fake_structured(*args, **kwargs):
        cards = json.loads(args[3].split("Candidate cards:\n", 1)[1])
        value = json.loads(json.dumps(Result.value))
        for decision, card in zip(value["decisions"], cards):
            decision["candidate_id"] = card["candidate_id"]
        result = Result()
        result.value = value
        return result

    monkeypatch.setattr(research, "_structured", fake_structured)
    ranked, provenance = research.triage_candidates(
        "Trump Iran conflict",
        research.conservative_brief("Trump Iran conflict"),
        candidates,
    )
    assert [item["title"] for item in ranked] == ["US and Iran conflict update"]
    assert provenance["coverage"] == 1


def test_v5_commercial_provider_requires_explicit_model(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    with pytest.raises(ValueError, match="explicit model"):
        gateway.provider_config("openai", None)


def test_local_gateway_can_hold_output_budget_after_length_retry(monkeypatch):
    payloads = []
    responses = iter(
        [
            (
                {
                    "id": "attempt-1",
                    "model": "chat",
                    "choices": [
                        {
                            "finish_reason": "length",
                            "message": {"content": json.dumps({"wrong": "value"})},
                        }
                    ],
                    "usage": {"total_tokens": 600},
                },
                "request-1",
                200,
            ),
            (
                {
                    "id": "attempt-2",
                    "model": "chat",
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"content": json.dumps({"result": "ok"})},
                        }
                    ],
                    "usage": {"total_tokens": 20},
                },
                "request-2",
                200,
            ),
        ]
    )

    def fake_request(_url, payload, _headers, _timeout):
        payloads.append(payload)
        return next(responses)

    monkeypatch.setattr(gateway, "_request_json", fake_request)
    monkeypatch.setattr(
        gateway,
        "probe_local",
        lambda *_args, **_kwargs: {"status": "available"},
    )

    result = gateway.call_structured(
        provider="local",
        model="chat",
        system_prompt="Return the result.",
        user_prompt="Produce one result.",
        schema={
            "type": "object",
            "properties": {"result": {"type": "string"}},
            "required": ["result"],
            "additionalProperties": False,
        },
        max_output_tokens=512,
        max_attempts=2,
        expand_output_on_length=False,
    )

    assert result.error == ""
    assert result.value == {"result": "ok"}
    assert [payload["max_tokens"] for payload in payloads] == [512, 512]
    assert "reached the output limit" in payloads[1]["messages"][1]["content"]
    assert result.provenance["max_output_tokens"] == 512
    assert result.provenance["expand_output_on_length"] is False


def test_fsearch_help_exposes_authoritative_controls_not_scratch_options(fake_cli):
    env, _ = fake_cli

    result = run_script("fsearch", "--help", env=env)

    assert result.returncode == 0
    assert "--research-run-id" in result.stdout
    assert "--scrape-limit" in result.stdout
    assert "--tbs" in result.stdout
    assert "--profile" in result.stdout
    assert "--dir" not in result.stdout
    assert "--reuse-search" not in result.stdout
    assert "--scrape-ranks" not in result.stdout
    assert not Path(env["FAKE_FIRECRAWL_LOG"]).exists()


def test_fscrape_preserves_multiple_urls_and_schema(fake_cli):
    env, tmp_path = fake_cli
    output = tmp_path / "batch with spaces"
    result = run_script(
        "fscrape",
        "https://example.com/a,b",
        "https://example.com/two",
        "--schema",
        '{"type":"object","properties":{"name":{"type":"string"}}}',
        "--output-dir",
        output,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    meta = json.loads((output / "_meta.json").read_text(encoding="utf-8"))
    assert [entry["url"] for entry in meta["results"]] == [
        "https://example.com/a,b",
        "https://example.com/two",
    ]
    assert all(entry["format"] == "json" for entry in meta["results"])


def test_smart_search_writes_diagnostic_dry_run_artifacts(fake_cli):
    env, tmp_path = fake_cli
    env["TMPDIR"] = str(tmp_path / "smart tmp")
    env.pop("GOOGLE_API_KEY", None)
    result = run_script(
        "fsearch_smart",
        "portable wrapper",
        "--dry-run",
        env=env,
    )
    assert result.returncode == 0, result.stderr
    roots = list((Path(env["TMPDIR"]) / "firecrawl_scratch").glob("fc_*/smart"))
    assert len(roots) == 1
    meta = json.loads((roots[0] / "_meta.json").read_text(encoding="utf-8"))
    assert meta["invocation_id"] == roots[0].parent.name
    assert meta["planner"] == "orchestrator"
    assert meta["budget_snapshot"]["policy_version"] == "budget-policy-v1"
    assert (roots[0] / "_research_spec.json").is_file()
    assert (roots[0] / "_budget.json").is_file()
    assert (roots[0] / "_meta.json").is_file()
