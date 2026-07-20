import importlib.util
from importlib.machinery import SourceFileLoader
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest


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
catalog = load_module("firecrawl_invocation_catalog", SCRIPTS / "invocation_catalog.py")
gateway = load_module("firecrawl_model_gateway", SCRIPTS / "model_gateway.py")
research = load_module("firecrawl_research_workflow", SCRIPTS / "research_workflow.py")
live_validation = load_module("firecrawl_live_validate", SCRIPTS / "live_validate.py")


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
    env["FIRECRAWL_CATALOG_DIR"] = str(tmp_path / "catalog")
    env["FIRECRAWL_AUDIT_AUTO_SEMANTIC"] = "0"
    env["FIRECRAWL_RESEARCH_AUTO_ENV"] = "0"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env, tmp_path


def run_script(name, *args, env=None):
    return subprocess.run(
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
        ("https://example.com/podcast/episode", "Episode", "Hosted by X", "media_release"),
        ("https://plato.stanford.edu/entries/test", "Argument", "Premise 1", "academic_debate"),
        ("https://example.com/reference", "Reference", "Neutral prose", "editorial_markdown"),
    ],
)
def test_all_classifier_profiles(url, title, snippet, expected):
    category, matched = classifier.classify_target(url, title, snippet)
    assert category == expected
    assert matched is (expected != "editorial_markdown")


@pytest.mark.parametrize(("complexity", "count"), [("simple", 2), ("moderate", 3), ("complex", 5)])
def test_hybrid_query_plan_is_unique_and_broad_first(complexity, count):
    topic = "Android Termux Vulkan compatibility failure modes"
    keywords = smart.extract_keywords(topic)
    queries = smart.generate_queries(topic, keywords, complexity)
    assert len(queries) == count
    assert len({query.lower() for query in queries}) == count
    assert "site:" not in queries[0].lower()
    assert len({entry["facet"] for entry in smart.build_query_plan(queries)}) >= min(2, count)


def test_query_normalization_and_broadening():
    queries = smart.normalize_queries(
        ['site:github.com "Firecrawl"', 'site:github.com "Firecrawl"'],
        "Firecrawl CLI portability",
        ["firecrawl", "cli", "portability"],
        "moderate",
    )
    assert len(queries) == 3
    assert "site:" not in queries[0].lower()
    assert "site:" not in smart.broaden_query('(site:github.com OR site:stackoverflow.com) "Firecrawl" errors')
    assert smart.adaptive_retry_query(["methodological", "naturalism", "cosmology", "burden", "proof"], 1, "topic") == "methodological naturalism"
    assert smart.adaptive_retry_query(["methodological", "naturalism", "cosmology", "burden", "proof"], 2, "topic") == "cosmology burden proof"


def test_heuristic_planner_retains_distinctive_subject_terms():
    topic = "California proposed legislation school holidays Islamic religious holy days"
    keywords = smart.extract_keywords(topic)
    for complexity in ("simple", "moderate", "complex"):
        queries = smart.generate_queries(topic, keywords, complexity)
        assert "islamic" in queries[0]
        assert "holidays" in queries[0]
        assert "islamic" in queries[-1]


def test_complexity_tiers_front_load_broad_candidate_acquisition():
    assert smart.COMPLEXITY_TIERS == {
        "simple": {"queries": 2, "results_per_query": 15, "total_scrapes": 6},
        "moderate": {"queries": 3, "results_per_query": 25, "total_scrapes": 12},
        "complex": {"queries": 5, "results_per_query": 40, "total_scrapes": 25},
    }
    for tier in smart.COMPLEXITY_TIERS.values():
        assert tier["queries"] * tier["results_per_query"] > tier["total_scrapes"]


def test_zero_global_scrape_budget_selects_no_candidates():
    result = {
        "query_index": 1,
        "facet": "broad_overview",
        "metadata": {"candidates": [{"url": "https://example.com/a", "rank": 1}]},
    }
    candidates, selected = smart.select_candidates([result], total_scrapes=0)
    assert len(candidates) == 1
    assert selected == []


def test_invocation_id_format_and_validation():
    first = invocations.new_invocation_id()
    second = invocations.new_invocation_id()
    assert first != second
    assert invocations.ID_PATTERN.fullmatch(first)
    assert invocations.validate_invocation_id(first) == first
    with pytest.raises(ValueError):
        invocations.validate_invocation_id("unsafe/path")


def test_default_storage_uses_unique_invocation_directories(fake_cli):
    env, tmp_path = fake_cli
    env["TMPDIR"] = str(tmp_path / "scratch root")
    first = run_script("fsearch", "first query", "--limit", "3", "--scrape-limit", "0", env=env)
    second = run_script("fscrape", "https://example.com/one", env=env)
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    search_dirs = list((Path(env["TMPDIR"]) / "firecrawl_scratch").glob("fc_*/search"))
    scrape_dirs = list((Path(env["TMPDIR"]) / "firecrawl_scratch").glob("fc_*/scrape"))
    assert len(search_dirs) == 1
    assert len(scrape_dirs) == 1
    search_meta = json.loads((search_dirs[0] / "_meta.json").read_text(encoding="utf-8"))
    scrape_meta = json.loads((scrape_dirs[0] / "_meta.json").read_text(encoding="utf-8"))
    assert search_meta["invocation_id"] == search_dirs[0].parent.name
    assert scrape_meta["invocation_id"] == scrape_dirs[0].parent.name
    assert search_meta["invocation_id"] != scrape_meta["invocation_id"]
    assert search_meta["invocation_id"] in (search_dirs[0] / "_index.md").read_text(encoding="utf-8")
    assert scrape_meta["invocation_id"] in (scrape_dirs[0] / "_index.md").read_text(encoding="utf-8")
    history = run_script("fread", "--history", env=env)
    assert history.returncode == 0
    assert search_meta["invocation_id"] in history.stdout
    assert scrape_meta["invocation_id"] in history.stdout
    assert f"{search_meta['invocation_id']}{os.sep}search" in history.stdout
    assert f"{scrape_meta['invocation_id']}{os.sep}scrape" in history.stdout


def test_fsearch_writes_complete_candidate_ledger(fake_cli):
    env, tmp_path = fake_cli
    output = tmp_path / "scratch O'Brien"
    result = run_script("fsearch", "portable query", "--limit", "3", "--scrape-limit", "2", "--tbs", "qdr:w", "--dir", output, env=env)
    assert result.returncode == 0, result.stderr
    meta = json.loads((output / "_meta.json").read_text(encoding="utf-8"))
    assert meta["candidate_count"] == 3
    assert meta["total_scraped"] == 2
    assert len(meta["candidates"]) == 3
    assert (output / "_candidates.json").is_file()
    assert (output / "_context.json").is_file()
    calls = [json.loads(line) for line in Path(env["FAKE_FIRECRAWL_LOG"]).read_text().splitlines()]
    assert ["--tbs", "qdr:w"] == calls[0][calls[0].index("--tbs"):calls[0].index("--tbs") + 2]
    catalog_record = json.loads(next((tmp_path / "catalog" / "invocations").glob("fc_*.json")).read_text())
    assert catalog_record["schema_version"] == 5
    assert catalog_record["execution"]["status"] == "succeeded"
    assert catalog_record["operational_status"] == "succeeded"
    assert catalog_record["input"]["query"] == "portable query"
    assert catalog_record["operational_metrics"]["candidate_count"] == 3
    assert catalog_record["operational_metrics"]["successful_document_count"] == 2
    assert "preview_head" not in json.dumps(catalog_record)


def test_fsearch_reuses_search_artifact_for_noncontiguous_ranks(fake_cli):
    env, tmp_path = fake_cli
    output = tmp_path / "reuse"
    assert run_script("fsearch", "portable", "--limit", "3", "--scrape-limit", "0", "--dir", output, env=env).returncode == 0
    original_id = json.loads((output / "_meta.json").read_text(encoding="utf-8"))["invocation_id"]
    result = run_script("fsearch", "portable", "--limit", "3", "--scrape-ranks", "1,3", "--dir", output, env=env)
    assert result.returncode == 0, result.stderr
    meta = json.loads((output / "_meta.json").read_text(encoding="utf-8"))
    assert meta["invocation_id"] == original_id
    assert meta["total_scraped"] == 2
    assert [entry["index"] for entry in meta["results"]] == [0, 2]
    calls = [json.loads(line) for line in Path(env["FAKE_FIRECRAWL_LOG"]).read_text().splitlines()]
    assert [call[0] for call in calls].count("search") == 1


def test_fsearch_handles_zero_results(fake_cli):
    env, tmp_path = fake_cli
    output = tmp_path / "zero"
    result = run_script("fsearch", "zero-results", "--dir", output, env=env)
    assert result.returncode == 0, result.stderr
    meta = json.loads((output / "_meta.json").read_text(encoding="utf-8"))
    assert meta["candidate_count"] == 0
    assert meta["total_scraped"] == 0


def test_fsearch_handles_success_without_output_file(fake_cli):
    env, tmp_path = fake_cli
    output = tmp_path / "no-output"
    result = run_script("fsearch", "no-output", "--dir", output, env=env)
    assert result.returncode == 0, result.stderr
    meta = json.loads((output / "_meta.json").read_text(encoding="utf-8"))
    assert meta["candidate_count"] == 0
    assert (output / "_search.json").is_file()


def test_fsearch_retries_transient_search_failure_and_keeps_diagnostics(fake_cli):
    env, tmp_path = fake_cli
    env["FAKE_FIRECRAWL_FAIL_SEARCH_ATTEMPTS"] = "1"
    env["FAKE_FIRECRAWL_FAILURE_COUNT"] = str(tmp_path / "search-failures")
    env["FIRECRAWL_SEARCH_RETRIES"] = "1"
    output = tmp_path / "retry"
    result = run_script("fsearch", "retry query", "--scrape-limit", "0", "--dir", output, env=env)
    assert result.returncode == 0, result.stderr
    assert "Transient Firecrawl search failure" in result.stderr
    assert "EAI_AGAIN" in (output / "_search_error.log").read_text(encoding="utf-8")
    assert json.loads((output / "_meta.json").read_text(encoding="utf-8"))["candidate_count"] == 3


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
    assert [entry["url"] for entry in meta["results"]] == ["https://example.com/a,b", "https://example.com/two"]
    assert all(entry["format"] == "json" for entry in meta["results"])
    catalog_record = json.loads(next((tmp_path / "catalog" / "invocations").glob("fc_*.json")).read_text())
    assert catalog_record["operation"] == "scrape"
    assert catalog_record["operational_metrics"]["successful_document_count"] == 2
    assert catalog_record["operational_metrics"]["requested_document_count"] == 2


def test_fscrape_rejects_undocumented_format(fake_cli):
    env, _ = fake_cli
    result = run_script("fscrape", "https://example.com", "--format", "text", env=env)
    assert result.returncode == 1
    assert "unsupported format" in result.stderr


def test_fread_history_grep_slice_and_invalid_regex(fake_cli):
    env, tmp_path = fake_cli
    root = tmp_path / "termux tmp" / "firecrawl_scratch" / "session O'Brien"
    root.mkdir(parents=True)
    (root / "_meta.json").write_text(json.dumps({"query": "portable", "results": [{"status": "ok"}], "total_words": 4}), encoding="utf-8")
    (root / "result_000.md").write_text("one\nneedle\nthree\nfour\n", encoding="utf-8")
    env["TMPDIR"] = str(tmp_path / "termux tmp")
    assert "portable" in run_script("fread", "--history", env=env).stdout
    assert "needle" in run_script("fread", root, "--grep", "needle", env=env).stdout
    sliced = run_script("fread", root / "result_000.md", "--skip", "1", "--lines", "1", env=env)
    assert "needle" in sliced.stdout
    assert "lines \x1b[1;36m2-2\x1b[0m" in sliced.stdout
    assert "showing:\x1b[0m first" not in sliced.stdout
    invalid = run_script("fread", root, "--grep", "[", env=env)
    assert invalid.returncode == 2


def test_smart_search_consolidates_deduplicated_candidates(fake_cli):
    env, tmp_path = fake_cli
    env["TMPDIR"] = str(tmp_path / "smart tmp")
    env.pop("GOOGLE_API_KEY", None)
    result = run_script("fsearch_smart", "portable wrapper", "--complexity", "simple", "--planner", "heuristic", env=env)
    assert result.returncode == 0, result.stderr
    roots = list((Path(env["TMPDIR"]) / "firecrawl_scratch").glob("fc_*/smart"))
    assert len(roots) == 1
    meta = json.loads((roots[0] / "_meta.json").read_text(encoding="utf-8"))
    assert meta["invocation_id"] == roots[0].parent.name
    assert all(
        json.loads(path.read_text(encoding="utf-8"))["invocation_id"] == meta["invocation_id"]
        for path in roots[0].glob("query_*/_meta.json")
    )
    assert meta["planner"] == "heuristic"
    assert meta["candidate_count"] == 6
    assert len(meta["query_plan"]) == 2
    assert (roots[0] / "_candidates.json").is_file()
    assert (roots[0] / "_context.json").is_file()
    assert (roots[0] / "_evidence.json").is_file()
    assert meta["strategy"]["acquisition_mode"] == "metadata_first_llm_triage_adaptive_scrape"
    assert meta["strategy"]["results_per_query"] == 15
    assert meta["strategy"]["total_scrape_budget"] == 6
    calls = [json.loads(line) for line in Path(env["FAKE_FIRECRAWL_LOG"]).read_text().splitlines()]
    assert [call[0] for call in calls].count("search") == 2
    assert [call[0] for call in calls].count("scrape") == 2
    catalog_record = json.loads(next((tmp_path / "catalog" / "invocations").glob("fc_*.json")).read_text())
    assert catalog_record["operation"] == "smart_search"
    assert catalog_record["execution"]["status"] == "succeeded"
    assert len([event for event in catalog_record["events"] if event["type"] == "branch_finished"]) == 2
    assert len([event for event in catalog_record["events"] if event["type"] == "scrape_wave_finished"]) == 1


def test_catalog_disabled_creates_no_persistent_record(fake_cli):
    env, tmp_path = fake_cli
    env["FIRECRAWL_CATALOG_DISABLED"] = "1"
    assert run_script("fsearch", "private query", "--scrape-limit", "0", env=env).returncode == 0
    assert not (tmp_path / "catalog").exists()


def test_research_run_links_operations_and_reports_quality(fake_cli):
    env, tmp_path = fake_cli
    started = run_script("frun", "start", "audit objective", env=env)
    assert started.returncode == 0, started.stderr
    run_id = started.stdout.strip()
    assert run_id.startswith("fr_")
    result = run_script("fsearch", "portable query", "--scrape-limit", "1", "--research-run-id", run_id, env=env)
    assert result.returncode == 0, result.stderr
    finished = run_script("frun", "finish", run_id, "--outcome", "satisfied", "--used-url", "https://example.com/one", env=env)
    assert finished.returncode == 0, finished.stderr
    run = json.loads((tmp_path / "catalog" / "runs" / f"{run_id}.json").read_text())
    assert run["declared_outcome"] == "satisfied"
    assert run["lifecycle"]["state"] == "finished"
    assert len(run["invocation_ids"]) == 1
    record = json.loads((tmp_path / "catalog" / "invocations" / f"{run['invocation_ids'][0]}.json").read_text())
    assert record["schema_version"] == 5
    assert record["research_run_id"] == run_id
    assert record["operational_metrics"]["total_words"] > 0
    assert "preview_head" not in json.dumps(record)
    report = run_script("fread", "--catalog", run_id, env=env)
    assert report.returncode == 0
    assert '"operational_summary"' in report.stdout
    listing = run_script("fread", "--catalog", env=env)
    assert run_id in listing.stdout


def test_catalog_collects_nonbinding_source_hints_without_semantic_verdicts():
    topic = "California school holiday legislation Eid religious observance"
    relevant = catalog.scrub_result({"url": "https://leginfo.ca.gov/bill", "title": "Eid school holiday bill", "selected": True}, topic)
    generic = catalog.scrub_result({"url": "https://example.com/travel", "title": "California tourism map", "selected": True}, topic)
    assert relevant["source_hints"]["host"] == "leginfo.ca.gov"
    assert "public-sector-domain" in relevant["source_hints"]["nonbinding"]
    assert generic["source_hints"]["nonbinding"] == []
    assert "relevance" not in relevant
    assert "source_tier" not in generic


def test_smart_retry_refuses_to_drop_topic_anchors():
    module = load_module("firecrawl_smart_retry", SCRIPTS / "fsearch_smart")
    topic = "Donald Trump conflict in Iran developments July 2026"
    assert not module.retains_anchors(topic, "donald trump")
    assert module.retains_anchors(topic, "Donald Trump Iran developments July 2026")


def test_catalog_purge_requires_force_and_removes_only_catalog(fake_cli):
    env, tmp_path = fake_cli
    root = tmp_path / "catalog"
    (root / "invocations").mkdir(parents=True)
    (root / "invocations" / ("fc_" + "a" * 32 + ".json")).write_text("{}")
    protected = tmp_path / "outside.txt"
    protected.write_text("keep")
    dry = run_script("frun", "purge", env=env)
    assert dry.returncode == 0
    assert root.exists()
    assert '"dry_run"' in dry.stdout
    purged = run_script("frun", "purge", "--force", env=env)
    assert purged.returncode == 0
    assert not root.exists()
    assert protected.read_text() == "keep"


def test_v5_direct_scrape_contributes_operation_aware_metrics(fake_cli):
    env, tmp_path = fake_cli
    result = run_script("fscrape", "https://apnews.com/article/a", "https://example.gov/report", env=env)
    assert result.returncode == 0, result.stderr
    record = json.loads(next((tmp_path / "catalog" / "invocations").glob("fc_*.json")).read_text())
    assert record["operational_metrics"]["candidate_count"] is None
    assert record["operational_metrics"]["requested_document_count"] == 2
    assert record["operational_metrics"]["selected_count"] == 2
    assert record["operational_metrics"]["unique_domain_count"] == 2
    assert record["operational_metrics"]["successful_document_count"] == 2
    assert all(item["targeted"] for item in record["results"])


def test_v5_empty_search_separates_execution_from_data_completeness(fake_cli):
    env, tmp_path = fake_cli
    result = run_script("fsearch", "zero-results", "--scrape-limit", "0", env=env)
    assert result.returncode == 0, result.stderr
    record = json.loads(next((tmp_path / "catalog" / "invocations").glob("fc_*.json")).read_text())
    assert record["execution"]["status"] == "succeeded"
    assert record["operational_status"] == "succeeded"
    assert record["operational_metrics"]["candidate_count"] == 0
    assert "quality_status" not in record


def test_v5_catalog_does_not_emit_deterministic_semantic_buckets():
    item = catalog.scrub_result({"url": "https://example.com/cooking", "title": "Pasta recipe"}, "California Eid legislation")
    assert not {"relevance", "freshness", "source_tier", "evaluations"} & set(item)


def test_v5_collects_date_signals_without_deciding_freshness():
    acquired = "2026-07-19T12:00:00+00:00"
    structured = catalog.scrub_result(
        {"url": "https://example.com/story", "publishedDate": "2026-07-18T10:00:00Z"},
        "example story", acquired_at=acquired, window_days=7,
    )
    url_date = catalog.scrub_result(
        {"url": "https://example.com/2026/07/01/story"},
        "example story", acquired_at=acquired, window_days=7,
    )
    assert structured["date_signals"][0]["location"] == "metadata.publishedDate"
    assert structured["date_signals"][0]["parser_confidence"] == "high"
    assert any(signal["value"] == "2026/07/01" for signal in url_date["date_signals"])
    assert "freshness_window_compliant" not in structured


def test_v3_finished_run_rejects_attachment_until_reopened(fake_cli):
    env, _ = fake_cli
    run_id = run_script("frun", "start", "latest news audit", env=env).stdout.strip()
    assert run_script("frun", "finish", run_id, "--outcome", "partial", env=env).returncode == 0
    rejected = run_script("fsearch", "portable query", "--research-run-id", run_id, env=env)
    assert rejected.returncode != 0
    assert "reopen" in rejected.stderr
    reopened = run_script("frun", "reopen", run_id, "--reason", "add corroboration", env=env)
    assert reopened.returncode == 0, reopened.stderr
    attached = run_script("fsearch", "portable query", "--scrape-limit", "0", "--research-run-id", run_id, env=env)
    assert attached.returncode == 0, attached.stderr


def test_v3_source_manifest_resolves_claims_and_evidence(fake_cli):
    env, tmp_path = fake_cli
    run_id = run_script("frun", "start", "general portable research", env=env).stdout.strip()
    search = run_script("fsearch", "portable query", "--scrape-limit", "1", "--research-run-id", run_id, env=env)
    assert search.returncode == 0, search.stderr
    record_path = next((tmp_path / "catalog" / "invocations").glob("fc_*.json"))
    record = json.loads(record_path.read_text())
    used_url = next(item["url"] for item in record["results"] if item.get("scrape_status") == "ok")
    manifest = tmp_path / "sources.json"
    manifest.write_text(json.dumps({
        "claims": [{"id": "claim-1", "summary": "Portable evidence exists", "type": "finding"}],
        "sources": [{"url": used_url, "claim_ids": ["claim-1"], "roles": ["primary"]}],
    }))
    finished = run_script("frun", "finish", run_id, "--outcome", "satisfied", "--source-manifest", manifest, env=env)
    assert finished.returncode == 0, finished.stderr
    run = json.loads((tmp_path / "catalog" / "runs" / f"{run_id}.json").read_text())
    assert run["claims"][0]["id"] == "claim-1"
    assert run["used_sources"][0]["resolution"] == "matched"
    assert run["used_sources"][0]["extraction_status"] == "ok"
    assert run["used_sources"][0]["candidate_id"].startswith("fce_")


def test_v3_verify_detects_missing_artifacts(fake_cli):
    env, tmp_path = fake_cli
    result = run_script("fsearch", "portable query", "--scrape-limit", "0", env=env)
    assert result.returncode == 0, result.stderr
    record_path = next((tmp_path / "catalog" / "invocations").glob("fc_*.json"))
    record = json.loads(record_path.read_text())
    Path(record["artifacts"][0]["path"]).unlink()
    verified = run_script("frun", "verify", record["invocation_id"], env=env)
    assert verified.returncode == 0, verified.stderr
    assert json.loads(verified.stdout)["missing"] == 1


def test_v5_catalog_has_no_deterministic_semantic_assessment(fake_cli):
    env, tmp_path = fake_cli
    result = run_script("fsearch", "portable query", "--scrape-limit", "0", env=env)
    assert result.returncode == 0, result.stderr
    record_path = next((tmp_path / "catalog" / "invocations").glob("fc_*.json"))
    before = json.loads(record_path.read_text())
    assert before["audit_status"] == "not_run"
    assert before["assessment_refs"] == []
    assert "quality_status" not in before
    assert "quality_dimensions" not in before


def test_v3_redacts_secrets_and_sensitive_url_parameters():
    cleaned = catalog.sanitize({
        "api_key": "secret-value",
        "url": "https://example.com/report?token=abc&utm_source=test&view=full",
        "message": "Authorization: Bearer abc.def and password=hunter2",
    })
    encoded = json.dumps(cleaned)
    assert "secret-value" not in encoded
    assert "abc.def" not in encoded
    assert "hunter2" not in encoded
    assert "utm_source" not in encoded
    assert "view=full" in encoded
    assert encoded.count("[REDACTED]") >= 3


def test_v3_concurrent_operations_do_not_lose_run_membership(fake_cli):
    env, tmp_path = fake_cli
    run_id = run_script("frun", "start", "concurrent catalog audit", env=env).stdout.strip()
    processes = [
        subprocess.Popen(
            [str(SCRIPTS / "fsearch"), f"parallel query {index}", "--scrape-limit", "0", "--research-run-id", run_id],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
        )
        for index in range(2)
    ]
    outputs = [process.communicate(timeout=30) for process in processes]
    assert all(process.returncode == 0 for process in processes), outputs
    run = json.loads((tmp_path / "catalog" / "runs" / f"{run_id}.json").read_text())
    assert len(run["invocation_ids"]) == 2
    assert len(set(run["invocation_ids"])) == 2


def test_v5_direct_scrape_collects_bounded_hashed_excerpt(tmp_path):
    body = tmp_path / "bill.md"
    body.write_text("California AB 2017 would authorize public schools to close for Eid al-Fitr and Eid al-Adha religious holidays.")
    item = catalog.scrub_result(
        {"url": "https://leginfo.legislature.ca.gov/faces/billTextClient.xhtml", "title": "billTextClient.xhtml", "status": "ok", "scratch_file": str(body)},
        "California proposed legislation school holidays Islamic religious holy days",
        targeted=True,
    )
    assert item["excerpts"]
    assert "holidays" in item["excerpts"][0]["matched_terms"]
    normalized = catalog.normalize_results({"operation": "scrape", "results": [{"url": item["url"], "status": "ok", "scratch_file": str(body)}]}, "California school holidays", "2026-07-20T00:00:00Z", "fc_" + "a" * 32)
    assert normalized[0]["excerpts"][0]["excerpt_id"].startswith("fex_")
    assert len(normalized[0]["excerpts"][0]["text_sha256"]) == 64


def test_v4_site_constraint_violation_is_a_hard_fact():
    item = catalog.scrub_result(
        {"url": "https://en.wikipedia.org/wiki/Associated_Press", "title": "Associated Press", "selected": True},
        "Donald Trump Iran conflict AP reporting",
        expected_domains=["apnews.com"],
    )
    assert item["constraint"]["status"] == "violated"
    assert item["constraint"]["actual_host"] == "en.wikipedia.org"
    assert "evaluations" not in item


def test_v5_source_hints_and_natural_language_window_are_nonsemantic():
    iaea = catalog.scrub_result({"url": "https://www.iaea.org/newscenter/test"}, "Iran nuclear conflict")
    bbc = catalog.scrub_result({"url": "https://www.bbc.co.uk/news/test"}, "Iran conflict")
    assert iaea["source_hints"]["host"] == "www.iaea.org"
    assert bbc["source_hints"]["host"] == "www.bbc.co.uk"
    assert "source_tier" not in iaea and "source_tier" not in bbc
    assert catalog.requested_window({"query": "latest news from the past 5 days"}) == 5


def test_v4_antibot_errors_have_specific_taxonomy():
    assert catalog.classify_error("document_antibot challenge returned") == "access_blocked"
    assert catalog.classify_error("CAPTCHA blocked page") == "access_blocked"


def test_v4_snapshot_survives_scratch_body_removal(fake_cli):
    env, tmp_path = fake_cli
    result = run_script("fscrape", "https://example.gov/report", env=env)
    assert result.returncode == 0, result.stderr
    record_path = next((tmp_path / "catalog" / "invocations").glob("fc_*.json"))
    record = json.loads(record_path.read_text())
    snapshot = Path(record["snapshot"]["path"])
    assert snapshot.is_file()
    Path(record["results"][0]["scratch_file"]).unlink()
    assert snapshot.is_file()
    verified = run_script("frun", "verify", record["invocation_id"], env=env)
    report = json.loads(verified.stdout)
    assert any(item["path"] == str(snapshot) and item["state"] == "available" for item in report["artifacts"])


def test_v4_ambiguous_url_requires_exact_candidate_reference(fake_cli):
    env, tmp_path = fake_cli
    run_id = run_script("frun", "start", "general duplicate source research", env=env).stdout.strip()
    scraped = run_script("fscrape", "https://example.com/same", "https://example.com/same", "--research-run-id", run_id, env=env)
    assert scraped.returncode == 0, scraped.stderr
    rejected = run_script("frun", "finish", run_id, "--outcome", "partial", "--used-url", "https://example.com/same", env=env)
    assert rejected.returncode != 0
    assert "ambiguous source URL" in rejected.stderr


def test_v5_assessment_attachment_stays_current_then_evidence_change_stales(fake_cli, monkeypatch):
    env, tmp_path = fake_cli
    run_id = run_script("frun", "start", "general portable research", env=env).stdout.strip()
    assert run_script("fsearch", "portable query", "--scrape-limit", "1", "--research-run-id", run_id, env=env).returncode == 0
    assert run_script("frun", "finish", run_id, "--outcome", "partial", env=env).returncode == 0
    monkeypatch.setenv("FIRECRAWL_CATALOG_DIR", env["FIRECRAWL_CATALOG_DIR"])
    run_path = tmp_path / "catalog" / "runs" / f"{run_id}.json"
    run = json.loads(run_path.read_text())
    target_hash = __import__("hashlib").sha256(json.dumps(catalog.build_audit_packet(run_id), sort_keys=True).encode()).hexdigest()
    run["assessment_refs"] = [{"assessment_id": "fa_test", "status": "completed", "provider": "local", "target_hash": target_hash, "evaluator_version": "catalog-v5.0"}]
    run["audit_status"] = "completed"
    run_path.write_text(json.dumps(run))
    shown = json.loads(run_script("frun", "show", run_id, env=env).stdout)
    assert shown["assessment_refs"][-1]["freshness"] == "current"
    assert run_script("frun", "reopen", run_id, "--reason", "add evidence", env=env).returncode == 0
    assert run_script("frun", "annotate", run_id, "--type", "pivot", "--reason", "switch to official sources", env=env).returncode == 0
    shown = json.loads(run_script("frun", "show", run_id, env=env).stdout)
    assert shown["assessment_refs"][-1]["freshness"] == "stale"


def test_v5_normalizes_model_packet_paths_to_stable_evidence_ids():
    packet = {
        "target_id": "fr_" + "a" * 32,
        "operations": [{"invocation_id": "fc_" + "b" * 32}],
        "candidate_cards": [{"candidate_id": "fce_candidate"}],
        "source_manifest": [{"candidate_id": "fce_source"}],
        "used_source_dossiers": [{
            "candidate_id": "fce_used",
            "excerpts": [{"excerpt_id": "fex_excerpt"}],
        }],
        "claims": [{"id": "claim-one"}],
        "timeline": [{"event_id": "evt-one"}],
    }
    value = {"findings": [{
        "evidence_refs": [
            "operations[0].execution.status",
            "candidate_cards[0].url",
            "source_manifest[0]",
            "used_source_dossiers[0].excerpts[0].text",
            "claims[0]",
            "timeline[0].event_type",
            "context_manifest",
            "does_not_exist[0]",
        ]
    }]}
    normalized = catalog.normalize_evidence_refs(value, packet)
    assert normalized["findings"][0]["evidence_refs"] == [
        "fc_" + "b" * 32,
        "fce_candidate",
        "fce_source",
        "fex_excerpt",
        "claim-one",
        "evt-one",
        "fr_" + "a" * 32,
    ]


def test_v4_schema_transition_discards_old_catalog_without_backup(fake_cli):
    env, tmp_path = fake_cli
    root = Path(env["FIRECRAWL_CATALOG_DIR"])
    invocations_dir = root / "invocations"
    invocations_dir.mkdir(parents=True)
    invocation_id = "fc_" + "a" * 32
    legacy = {
        "schema_version": 3, "invocation_id": invocation_id, "operation": "search", "started_at": "2026-07-01T00:00:00+00:00",
        "execution": {"status": "succeeded"}, "quality": {}, "results": [
            {"url": "https://example.com/a", "canonical_url": "https://example.com/a"},
            {"url": "https://example.com/b", "canonical_url": "https://example.com/b", "status": "error", "error": "document_antibot challenge"},
        ],
        "assessment_refs": [], "record_revision": 1,
    }
    path = invocations_dir / f"{invocation_id}.json"
    path.write_text(json.dumps(legacy))
    (root / "runs").mkdir()
    current_run = root / "runs" / ("fr_" + "b" * 32 + ".json")
    current_run.write_text(json.dumps({"schema_version": 4, "research_run_id": "fr_" + "b" * 32}))
    (root / "snapshots").mkdir()
    (root / "snapshots" / f"{invocation_id}.json.gz").write_bytes(b"legacy-snapshot")
    (root / "migrations" / "legacy" / "v3").mkdir(parents=True)
    (root / "migrations" / "legacy" / "v3" / "record.json").write_text(json.dumps(legacy))
    dry = run_script("frun", "migrate", "--from", "4", "--to", "5", env=env)
    preview = json.loads(dry.stdout)
    assert preview["action"] == "dry_run"
    assert preview["would_discard_entire_catalog"] is True
    assert preview["backup_created"] is False
    assert json.loads(path.read_text())["schema_version"] == 3
    applied = run_script("frun", "migrate", "--from", "4", "--to", "5", "--apply", env=env)
    assert applied.returncode == 0, applied.stderr
    reset = json.loads(applied.stdout)
    assert reset["action"] == "reset"
    assert reset["backup_created"] is False
    assert not path.exists()
    assert not current_run.exists()
    assert not (root / "snapshots").exists()
    assert not (root / "migrations").exists()
    marker = json.loads((root / "catalog.json").read_text())
    assert marker["schema_version"] == 5
    assert marker["history_policy"] == "discard_on_schema_change"
    events = [json.loads(line) for line in (root / "events.jsonl").read_text().splitlines()]
    assert [event["event"] for event in events] == ["catalog_schema_initialized"]
    rerun = run_script("frun", "migrate", "--apply", env=env)
    assert json.loads(rerun.stdout)["action"] == "no_change"


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
            return Response({"id": "chatcmpl-empty", "model": "chat", "usage": {"completion_tokens": 4096}, "choices": [{"finish_reason": "length", "message": {"content": "", "reasoning": "long internal reasoning"}}]})
        return Response({
            "id": "chatcmpl-test", "model": "chat", "system_fingerprint": "fp-test",
            "usage": {"prompt_tokens": 10, "completion_tokens": 20},
            "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(assessment)}}],
        }, {"x-request-id": "req-test"})

    monkeypatch.setattr(gateway, "urlopen", fake_urlopen)
    result = gateway.call_structured("local", None, "system", "user", {"type": "object", "properties": {"result": {"type": "string"}}, "required": ["result"]}, max_output_tokens=4096, max_attempts=2)
    assert result.value == assessment
    assert result.provenance["requested_model"] == "chat"
    assert result.provenance["capability_probe"]["max_context_tokens"] == 262144
    assert result.attempts[0]["finish_reason"] == "length"
    assert result.attempts[0]["reasoning_excerpt"]
    assert result.attempts[1]["structured_mode"] == "json_object"


def test_v4_selective_purge_is_dry_run_and_removes_associated_events(fake_cli):
    env, tmp_path = fake_cli
    first = run_script("frun", "start", "first retained audit", env=env).stdout.strip()
    second = run_script("frun", "start", "second retained audit", env=env).stdout.strip()
    dry = run_script("frun", "purge", "--run-id", first, env=env)
    assert json.loads(dry.stdout)["action"] == "dry_run"
    assert (tmp_path / "catalog" / "runs" / f"{first}.json").is_file()
    applied = run_script("frun", "purge", "--run-id", first, "--force", env=env)
    assert applied.returncode == 0, applied.stderr
    assert not (tmp_path / "catalog" / "runs" / f"{first}.json").exists()
    assert (tmp_path / "catalog" / "runs" / f"{second}.json").is_file()
    events = (tmp_path / "catalog" / "events.jsonl").read_text()
    assert first not in events
    assert second in events


def test_v5_live_validator_accepts_dry_run_and_durable_records():
    assert live_validation.catalog_record_valid({
        "schema_version": 5, "execution": {"status": "succeeded"}, "input": {"dry_run": True},
    })
    assert live_validation.catalog_record_valid({
        "schema_version": 5, "execution": {"status": "succeeded"}, "input": {"dry_run": False},
        "snapshot": {"availability": "available"}, "operational_metrics": {"successful_document_count": 1}, "data_completeness": "complete",
    })
    assert not live_validation.catalog_record_valid({
        "schema_version": 5, "execution": {"status": "succeeded"}, "input": {"dry_run": False},
    })


def test_v5_candidate_triage_rejects_irrelevant_volume(monkeypatch):
    candidates = [
        {"url": "https://vin.example/check", "title": "Free VIN Check", "snippet": "vehicle history", "rank": 1},
        {"url": "https://apnews.com/article/iran", "title": "US and Iran conflict update", "snippet": "July reporting", "rank": 2},
    ]
    class Result:
        value = {"decisions": [
            {"candidate_id": "", "relevance": "unrelated", "source_suitability": "unsuitable", "subquestions": [], "freshness": "unknown", "independence": "unknown", "scrape": False, "priority": 0, "rationale": "vehicle lookup"},
            {"candidate_id": "", "relevance": "high", "source_suitability": "authoritative_secondary", "subquestions": ["latest developments"], "freshness": "likely current", "independence": "independent reporting", "scrape": True, "priority": 95, "rationale": "directly addresses objective"},
        ]}
        provenance = {"provider": "local"}; attempts = []; error = ""
    def fake_structured(*args, **kwargs):
        cards = json.loads(args[3].split("Candidate cards:\n", 1)[1])
        value = json.loads(json.dumps(Result.value))
        for decision, card in zip(value["decisions"], cards): decision["candidate_id"] = card["candidate_id"]
        result = Result(); result.value = value
        return result
    monkeypatch.setattr(research, "_structured", fake_structured)
    ranked, provenance = research.triage_candidates("Trump Iran conflict", research.conservative_brief("Trump Iran conflict"), candidates)
    assert [item["title"] for item in ranked] == ["US and Iran conflict update"]
    assert provenance["coverage"] == 1


def test_v5_audit_packet_preserves_answer_claims_sources_and_excerpts(fake_cli, monkeypatch):
    env, tmp_path = fake_cli
    run_id = run_script("frun", "start", "current legal research", env=env).stdout.strip()
    assert run_script("fsearch", "portable legal evidence", "--scrape-limit", "1", "--research-run-id", run_id, env=env).returncode == 0
    record = json.loads(next((tmp_path / "catalog" / "invocations").glob("fc_*.json")).read_text())
    source = next(item for item in record["results"] if item.get("scrape_status") == "ok")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"claims": [{"id": "claim-1", "summary": "Evidence exists", "type": "finding"}], "sources": [{"url": source["url"], "candidate_id": source["candidate_id"], "claim_ids": ["claim-1"], "excerpt_ids": [source["excerpts"][0]["excerpt_id"]], "roles": ["primary"]}]}))
    answer = tmp_path / "answer.md"; answer.write_text("Evidence exists.")
    assert run_script("frun", "finish", run_id, "--outcome", "satisfied", "--source-manifest", manifest, "--answer-file", answer, env=env).returncode == 0
    monkeypatch.setenv("FIRECRAWL_CATALOG_DIR", env["FIRECRAWL_CATALOG_DIR"])
    packet = catalog.build_audit_packet(run_id)
    assert packet["final_answer"]["text"] == "Evidence exists."
    assert packet["claims"][0]["id"] == "claim-1"
    assert packet["used_source_dossiers"][0]["excerpts"][0]["excerpt_id"] == source["excerpts"][0]["excerpt_id"]
    assert packet["context_manifest"]["omissions"] == []


def test_v5_assessment_rejects_invented_evidence_references():
    packet = {"target_id": "fr_test", "operations": [], "candidate_cards": [], "claims": [], "used_source_dossiers": [], "timeline": []}
    finding = {"code": "TEST", "dimension": "evidence", "label": "weak", "confidence": 0.9, "rationale": "unsupported", "evidence_refs": ["fce_invented"], "uncertainty": "none", "recommended_action": "repair"}
    value = {"stage_adequacy": "weak", "findings": [finding], "unresolved": []}
    valid, problems = catalog.validate_stage_output("evidence", value, packet)
    assert valid is False
    assert "unknown evidence refs" in problems[0]


def test_v5_commercial_provider_requires_explicit_model(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    with pytest.raises(ValueError, match="explicit model"):
        gateway.provider_config("openai", None)
