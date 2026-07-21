from __future__ import annotations

# ruff: noqa: E402 - load the repository scripts without installing a package.

from hashlib import sha256
import importlib.util
from importlib.machinery import SourceFileLoader
from io import BytesIO
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
FIXTURES = ROOT / "tests" / "fixtures" / "legacy_baseline"
SCENARIOS = FIXTURES / "scenarios"
GOLDEN = FIXTURES / "golden"
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(SCRIPTS))

from fixture_replay import load_fixture
from research_store.blob import ContentAddressedBlobStore
from research_store.indexing import IndexWorker
from research_store.queue import ValkeyQueue
from research_store.service import CorpusService


def load_module(name: str, path: Path):
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


research = load_module("golden_research_workflow", SCRIPTS / "research_workflow.py")
persist_results = load_module("golden_persist_results", SCRIPTS / "persist_results.py")


def replay_env(tmp_path: Path, fixture_path: Path) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    executable = bin_dir / "firecrawl"
    shutil.copy2(ROOT / "tests" / "fixture_replay.py", executable)
    executable.chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}{os.pathsep}{env['PATH']}",
            "FIRECRAWL_REPLAY_FIXTURE": str(fixture_path),
            "FIRECRAWL_RESEARCH_AUTO_ENV": "0",
            "FIRECRAWL_RESEARCH_PERSIST": "off",
            "FIRECRAWL_CATALOG_DISABLED": "1",
            "FIRECRAWL_AUDIT_AUTO_SEMANTIC": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    for key in ("DATABASE_URL", "OPENAI_API_KEY", "GEMINI_API_KEY"):
        env.pop(key, None)
    return env


def run_script(name: str, *args, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(SCRIPTS / name), *map(str, args)],
        text=True,
        capture_output=True,
        env=env,
        timeout=30,
    )


def normalized_search_manifest(meta: dict) -> dict:
    candidates = meta.get("candidates", [])
    return {
        "operation": meta["operation"],
        "query": meta["query"],
        "candidate_count": meta["candidate_count"],
        "total_scraped": meta["total_scraped"],
        "candidate_urls": [item["url"] for item in candidates],
        "candidate_titles": [item["title"] for item in candidates],
    }


@pytest.mark.parametrize(
    "scenario", ["technical", "legal", "breaking_news", "academic_debate", "no_results"]
)
def test_recorded_search_matches_golden_manifest(tmp_path, scenario):
    fixture_path = SCENARIOS / f"{scenario}.json"
    fixture = load_fixture(fixture_path)
    output = tmp_path / scenario
    result = run_script(
        "fsearch",
        fixture["search"]["query"],
        "--limit",
        "10",
        "--scrape-limit",
        "0",
        "--invocation-id",
        "fc_" + "1" * 32,
        "--dir",
        output,
        env=replay_env(tmp_path, fixture_path),
    )
    assert result.returncode == 0, result.stderr
    actual = normalized_search_manifest(json.loads((output / "_meta.json").read_text()))
    expected = json.loads((GOLDEN / f"{scenario}.json").read_text())
    assert actual == expected
    assert (output / "_search.json").read_bytes()
    assert (output / "_candidates.json").is_file()
    assert (output / "_context.json").is_file()
    assert "Found" in result.stderr and "Done" in result.stderr


def test_recorded_scrape_matches_legacy_scratch_manifest(tmp_path):
    fixture_path = SCENARIOS / "legal.json"
    fixture = load_fixture(fixture_path)
    output = tmp_path / "scrape"
    url = fixture["scrapes"][0]["url"]
    result = run_script(
        "fscrape",
        url,
        "--invocation-id",
        "fc_" + "2" * 32,
        "--output-dir",
        output,
        env=replay_env(tmp_path, fixture_path),
    )
    assert result.returncode == 0, result.stderr
    meta = json.loads((output / "_meta.json").read_text())
    assert {
        "operation": meta["operation"],
        "statuses": [item["status"] for item in meta["results"]],
        "urls": [item["url"] for item in meta["results"]],
    } == {"operation": "scrape", "statuses": ["ok"], "urls": [url]}
    assert (output / "url_000.md").read_text() == fixture["scrapes"][0]["content"].strip()


def test_smart_search_replays_without_live_search_or_model(tmp_path):
    fixture_path = SCENARIOS / "technical.json"
    env = replay_env(tmp_path, fixture_path)
    env["TMPDIR"] = str(tmp_path / "scratch")
    env["FIRECRAWL_REPLAY_ALLOW_ANY_QUERY"] = "1"
    result = run_script(
        "fsearch_smart",
        "python asyncio timeout debugging",
        "--complexity",
        "simple",
        "--planner",
        "heuristic",
        "--total-scrapes",
        "0",
        env=env,
    )
    assert result.returncode == 0, result.stderr
    roots = list((Path(env["TMPDIR"]) / "firecrawl_scratch").glob("fc_*/smart"))
    assert len(roots) == 1
    meta = json.loads((roots[0] / "_meta.json").read_text())
    assert meta["planner"] == "heuristic"
    assert meta["candidate_count"] == 2
    assert meta["total_scraped_pages"] == 0
    assert (roots[0] / "_evidence.json").is_file()


def test_recorded_semantic_outputs_bypass_generative_endpoint(monkeypatch):
    fixture = load_fixture(SCENARIOS / "technical.json")
    recorded = [
        fixture["semantic_outputs"]["brief"],
        fixture["semantic_outputs"]["query_plan"],
    ]

    def replay_structured(*_args, **_kwargs):
        value = recorded.pop(0)
        return SimpleNamespace(
            value=value,
            provenance={"provider": "fixture", "model": "recorded"},
            attempts=[],
            error="",
        )

    monkeypatch.setattr(research, "_structured", replay_structured)
    brief, brief_provenance = research.build_research_brief(fixture["search"]["query"])
    queries, query_provenance = research.plan_queries(
        fixture["search"]["query"], brief, 1
    )
    assert brief == fixture["semantic_outputs"]["brief"]
    assert queries == fixture["semantic_outputs"]["query_plan"]["queries"]
    assert brief_provenance["provider"] == query_provenance["provider"] == "fixture"
    assert recorded == []


def test_fixture_integrity_hashes():
    manifest = json.loads((FIXTURES / "manifest.json").read_text())
    assert manifest["schema_version"] == "firecrawl-fixture-manifest-v1"
    fixture_files = {
        str(path.relative_to(FIXTURES))
        for folder in (SCENARIOS, GOLDEN)
        for path in folder.glob("*.json")
    }
    assert set(manifest["sha256"]) == fixture_files
    for relative, expected in manifest["sha256"].items():
        path = FIXTURES / relative
        assert path.is_file(), relative
        assert sha256(path.read_bytes()).hexdigest() == expected, relative


def test_recorded_database_rows_and_events_preserve_references():
    baseline = json.loads((GOLDEN / "state_records.json").read_text())
    rows = baseline["database_rows"]
    source = rows["sources"][0]
    snapshot = rows["asset_snapshots"][0]
    document = rows["documents"][0]
    block = rows["document_blocks"][0]
    chunk = rows["chunks"][0]
    manifest = rows["embedding_manifests"][0]
    job = rows["index_jobs"][0]
    batch = rows["ingestion_batches"][0]
    batch_asset = rows["ingestion_batch_assets"][0]
    retrieval = rows["retrieval_events"][0]
    assert snapshot["source_id"] == source["id"]
    assert document["snapshot_id"] == snapshot["id"]
    assert block["document_id"] == document["id"]
    assert chunk["document_id"] == document["id"]
    assert chunk["first_block_id"] == chunk["last_block_id"] == block["id"]
    assert manifest["chunk_id"] == chunk["id"]
    assert job["manifest_id"] == manifest["id"]
    assert job["index_definition_id"] == manifest["index_definition_id"]
    assert batch_asset["batch_id"] == batch["id"]
    assert batch_asset["snapshot_id"] == snapshot["id"]
    assert retrieval["candidate_id"] == chunk["id"]
    lease_event = next(
        event for event in baseline["event_records"] if event["type"] == "lease_claimed"
    )
    assert lease_event["job_id"] == job["id"]
    assert lease_event["manifest_id"] == job["manifest_id"]
    assert lease_event["lease_token"] == job["lease_token"]


def test_content_addressed_blob_identity_remains_immutable(tmp_path):
    store = ContentAddressedBlobStore(tmp_path / "blobs")
    first = store.put(BytesIO(b"recorded raw payload"), "text/plain")
    repeated = store.put(BytesIO(b"recorded raw payload"), "text/plain")
    changed = store.put(BytesIO(b"recorded changed payload"), "text/plain")
    assert first.sha256 == repeated.sha256
    assert first.sha256 != changed.sha256
    assert store.verify(first.sha256) and store.verify(changed.sha256)


def test_persistence_outage_is_fail_closed_and_exports_repair_manifest(tmp_path, monkeypatch):
    asset = tmp_path / "asset.md"
    asset.write_text("# Retained fixture\n\nPersistence must remain visible.\n")
    meta = tmp_path / "_meta.json"
    meta.write_text(json.dumps({
        "invocation_id": "fc_" + "3" * 32,
        "operation": "scrape",
        "results": [{"index": 0, "url": "https://fixture.invalid/persistence", "status": "ok", "scratch_file": str(asset)}],
    }))

    class UnavailableService:
        def persist_manifest_batch(self, *_args, **_kwargs):
            raise OSError("fixture PostgreSQL outage")

    monkeypatch.setenv("DATABASE_URL", "postgresql://fixture")
    monkeypatch.setattr(persist_results, "build_service", lambda: UnavailableService())
    output = tmp_path / "_corpus.json"
    assert persist_results.main([str(meta), "--output", str(output)]) == 1
    exported = json.loads(output.read_text())
    assert exported["status"] == "failed"
    assert exported["assets"][0]["status"] == "failed"
    assert "fixture PostgreSQL outage" in exported["error"]


class BrokenRedis:
    def pipeline(self):
        raise OSError("fixture Valkey outage")


def test_valkey_outage_preserves_best_effort_notification_contract():
    assert ValkeyQueue("redis://fixture", client=BrokenRedis()).notify(uuid4()) is False


def test_model_endpoint_outage_degrades_to_conservative_brief(monkeypatch):
    failure = SimpleNamespace(
        value=None,
        provenance={"provider": "local", "model": "chat"},
        attempts=[{"error": "fixture model endpoint outage"}],
        error="fixture model endpoint outage",
    )
    monkeypatch.setattr(research, "_structured", lambda *_args, **_kwargs: failure)
    brief, provenance = research.build_research_brief("outage objective")
    assert brief == research.conservative_brief("outage objective")
    assert provenance["status"] == "degraded"
    assert provenance["error"] == "fixture model endpoint outage"


class RetrievalDocuments:
    def __init__(self, candidate_id):
        self.candidate_id = candidate_id

    def search_lexical(self, *_args):
        return [{"candidate_id": self.candidate_id, "lexical_score": 1.0}]

    def fetch_passages(self, *_args):
        return [{"chunk_id": self.candidate_id, "text": "lexical fixture"}]


class RetrievalUow:
    def __init__(self, candidate_id):
        self.documents = RetrievalDocuments(candidate_id)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class BrokenQdrant:
    def list_aliases(self):
        return {"active": "configured"}

    def search(self, *_args):
        raise OSError("fixture Qdrant outage")


@pytest.mark.xfail(
    strict=True,
    reason="legacy retrieval silently drops Qdrant failure instead of reporting degraded_components (PRD FR-013)",
)
def test_qdrant_retrieval_outage_is_explicitly_reported():
    candidate_id = uuid4()
    config = SimpleNamespace(
        qdrant_alias="active",
        physical_collection="configured",
        reranker_candidate_limit=40,
        parser_version="markdown-v1",
        normalization_version="cleanup-v1",
        chunker_version="structural-v1",
    )
    service = CorpusService(
        config,
        lambda: RetrievalUow(candidate_id),
        blob_store=None,
        index=BrokenQdrant(),
        embedder=lambda _query: [0.1],
    )
    results = service.search_assets("fixture", candidate_limit=1)
    assert results[0]["degraded_components"] == ["qdrant"]


class WorkerRepository:
    def __init__(self, state):
        self.state = state

    def claim_jobs(self, *_args, **_kwargs):
        return [self.state["job"]] if not self.state.setdefault("claimed", False) else []

    def renew_job(self, *_args):
        self.state["claimed"] = True
        return True

    def finish_job(self, _job_id, _token, error, **_options):
        self.state["finish_error"] = error
        return True

    def chunks_for_index(self, *_args, **_kwargs):
        return [{"chunk_id": self.state["job"]["entity_id"], "text": "fixture", "source_id": uuid4()}]

    def heartbeat_worker(self, *_args):
        return None


class WorkerUow:
    def __init__(self, state):
        repo = WorkerRepository(state)
        self.index_jobs = self.chunks = repo

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class BrokenWorkerIndex:
    def for_collection(self, *_args, **_kwargs):
        return self

    def ensure_schema(self):
        return None

    def upsert(self, _points):
        raise OSError("fixture Qdrant worker outage")


def test_index_worker_records_qdrant_failure_against_exact_manifest_and_lease():
    job = {
        "id": uuid4(),
        "manifest_id": uuid4(),
        "entity_id": uuid4(),
        "operation": "upsert",
        "lease_token": uuid4(),
        "physical_collection": "research_chunks_fixture",
        "dimension": 1,
        "distance_metric": "Cosine",
    }
    state = {"job": job}
    result = IndexWorker(
        lambda: WorkerUow(state), BrokenWorkerIndex(), lambda _text: [1.0], worker_id="fixture-worker"
    ).run_batch(1)
    assert result["claimed"] == 1 and result["failed"] == 1 and result["complete"] == 0
    assert state["finish_error"] == "OSError: fixture Qdrant worker outage"
