#!/usr/bin/env python3
"""Budget-guarded live validation for the Firecrawl scratch-file skill."""

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
from urllib.parse import urlsplit
from urllib.request import urlopen


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPT_DIR.parent

BENCHMARKS = {
    "simple": {
        "topic": "current Firecrawl CLI npm package and installation command",
        "complexity": "simple",
        "facets": ["firecrawl", "npm", "install"],
        "min_domains": 2,
    },
    "academic": {
        "topic": "methodological naturalism cosmology burden of proof evidence objections",
        "complexity": "moderate",
        "facets": ["naturalism", "cosmology", "burden", "evidence", "objection"],
        "min_domains": 3,
    },
    "termux": {
        "topic": "Android Termux Vulkan Turnip Mesa Zink acceleration compatibility and failure modes",
        "complexity": "complex",
        "facets": ["termux", "vulkan", "turnip", "mesa", "zink", "failure"],
        "min_domains": 4,
    },
}


def catalog_record_valid(record):
    return (
        record.get("schema_version") == 5
        and record.get("execution", {}).get("status") in {"succeeded", "failed"}
        and (
            bool(record.get("input", {}).get("dry_run"))
            or (
                record.get("snapshot", {}).get("availability") == "available"
                and bool(record.get("operational_metrics"))
                and record.get("data_completeness") in {"complete", "partial"}
            )
        )
    )


def now_stamp():
    return dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def words(path):
    try:
        return len(path.read_text(encoding="utf-8", errors="ignore").split())
    except OSError:
        return 0


def jaccard(left, right):
    tokenize = lambda value: set(re.findall(r"[a-z0-9]+", value.lower()))
    a, b = tokenize(left), tokenize(right)
    return len(a & b) / len(a | b) if a | b else 1.0


class Campaign:
    def __init__(self, args):
        self.args = args
        self.run_id = args.run_id or now_stamp()
        root = Path(args.artifact_root or Path(tempfile.gettempdir()) / "firecrawl_validation")
        self.root = root / self.run_id
        self.logs = self.root / "logs"
        self.scratch = self.root / "scratch"
        self.proxy_dir = self.root / "proxy"
        self.catalog = self.root / "catalog"
        for directory in (self.logs, self.scratch, self.proxy_dir, self.catalog):
            directory.mkdir(parents=True, exist_ok=True)
        self.real_cli = shutil.which("firecrawl")
        if not self.real_cli:
            raise RuntimeError("firecrawl executable not found")
        self.counter = self.root / "operations.json"
        self.counter.write_text(json.dumps({"count": 0, "max": args.max_operations, "calls": []}), encoding="utf-8")
        self.cases = []
        self.started = time.time()
        self._write_proxy()
        self.env = os.environ.copy()
        self.env.update(
            {
                "PATH": f"{self.proxy_dir}{os.pathsep}{self.env['PATH']}",
                "REAL_FIRECRAWL": self.real_cli,
                "FC_OPERATION_COUNTER": str(self.counter),
                "FC_OPERATION_MAX": str(args.max_operations),
                "FIRECRAWL_API_URL": args.api_url.rstrip("/"),
                "FIRECRAWL_CATALOG_DIR": str(self.catalog),
                "TMPDIR": str(self.scratch),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )

    def _write_proxy(self):
        proxy = self.proxy_dir / "firecrawl"
        proxy.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import fcntl
                import json
                import os
                from pathlib import Path
                import sys
                import time

                args = sys.argv[1:]
                counter_path = Path(os.environ["FC_OPERATION_COUNTER"])
                maximum = int(os.environ["FC_OPERATION_MAX"])
                counted = bool(args and args[0] in {"search", "scrape"})
                if counted:
                    with counter_path.open("r+", encoding="utf-8") as handle:
                        fcntl.flock(handle, fcntl.LOCK_EX)
                        data = json.load(handle)
                        if data["count"] >= maximum:
                            print(f"ERROR: Firecrawl operation cap {maximum} reached", file=sys.stderr)
                            raise SystemExit(90)
                        data["count"] += 1
                        data["calls"].append({"number": data["count"], "command": args[0], "at": time.time()})
                        handle.seek(0)
                        json.dump(data, handle, indent=2)
                        handle.truncate()
                        fcntl.flock(handle, fcntl.LOCK_UN)
                real = os.environ["REAL_FIRECRAWL"]
                os.execv(real, [real, *args])
                """
            ),
            encoding="utf-8",
        )
        proxy.chmod(0o755)

    def operation_count(self):
        return json.loads(self.counter.read_text(encoding="utf-8"))["count"]

    def run(self, name, command, timeout=900, env_changes=None, required=True):
        log_path = self.logs / f"{len(self.cases) + 1:02d}_{name}.log"
        env = self.env.copy()
        for key, value in (env_changes or {}).items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = str(value)
        started = time.time()
        try:
            result = subprocess.run(command, text=True, capture_output=True, env=env, timeout=timeout)
            status = "pass" if result.returncode == 0 else "fail"
            output = result.stdout + ("\n--- stderr ---\n" + result.stderr if result.stderr else "")
            returncode = result.returncode
        except subprocess.TimeoutExpired as exc:
            status, returncode = "fail", 124
            output = f"TIMEOUT after {timeout}s\n{exc.stdout or ''}\n{exc.stderr or ''}"
        log_path.write_text(output, encoding="utf-8", errors="ignore")
        case = {
            "name": name,
            "status": status,
            "required": required,
            "returncode": returncode,
            "seconds": round(time.time() - started, 2),
            "operations_after": self.operation_count(),
            "log": str(log_path),
        }
        self.cases.append(case)
        print(f"[{status.upper()}] {name} ({case['seconds']}s, operations={case['operations_after']})")
        return case

    def preflight(self):
        case = {"name": "api_root", "required": True, "operations_after": self.operation_count()}
        try:
            with urlopen(self.args.api_url.rstrip("/") + "/", timeout=10) as response:
                payload = json.loads(response.read().decode("utf-8"))
            case["status"] = "pass" if payload.get("message") == "Firecrawl API" else "fail"
            case["payload"] = payload
        except Exception as exc:
            case.update(status="fail", error=f"{type(exc).__name__}: {exc}")
        self.cases.append(case)
        print(f"[{case['status'].upper()}] api_root")
        self.run("cli_version", [self.real_cli, "--version"], timeout=30)
        return case["status"] == "pass"

    def execute(self):
        if not self.preflight():
            return 2

        self.run("smart_dry_run_heuristic", [str(SCRIPT_DIR / "fsearch_smart"), BENCHMARKS["academic"]["topic"], "--complexity", "moderate", "--planner", "heuristic", "--dry-run"], timeout=60)
        if self.args.planner in ("local", "both"):
            self.run("smart_dry_run_local", [str(SCRIPT_DIR / "fsearch_smart"), BENCHMARKS["academic"]["topic"], "--complexity", "moderate", "--planner", "auto", "--llm", "local", "--dry-run"], timeout=180, required=False)

        if self.args.profile != "focused":
            self.run("scrape_markdown_batch", [str(SCRIPT_DIR / "fscrape"), "https://docs.firecrawl.dev/introduction", "https://example.com", "--output-dir", str(self.scratch / "feature_markdown")])
            self.run("scrape_links", [str(SCRIPT_DIR / "fscrape"), "https://docs.firecrawl.dev/introduction", "--format", "links", "--output-dir", str(self.scratch / "feature_links")])
            self.run("scrape_summary", [str(SCRIPT_DIR / "fscrape"), "https://docs.firecrawl.dev/introduction", "--summary", "--output-dir", str(self.scratch / "feature_summary")])
            schema = json.dumps({"type": "object", "properties": {"product_name": {"type": ["string", "null"]}, "price": {"type": ["string", "null"]}}, "required": ["product_name"]})
            self.run("scrape_schema", [str(SCRIPT_DIR / "fscrape"), "https://books.toscrape.com/catalogue/a-light-in-the-attic_1000/index.html", "--schema", schema, "--output-dir", str(self.scratch / "feature_schema")])
            self.run("search_web", [str(SCRIPT_DIR / "fsearch"), "Firecrawl CLI installation official documentation", "--limit", "5", "--scrape-limit", "2", "--dir", str(self.scratch / "search_web")])
            self.run("search_news", [str(SCRIPT_DIR / "fsearch"), "small modular reactor grid policy 2026", "--limit", "5", "--scrape-limit", "2", "--sources", "news", "--tbs", "qdr:m", "--dir", str(self.scratch / "search_news")], required=False)

        if self.args.profile == "termux":
            selected = ["simple"]
        elif self.args.profile == "focused":
            selected = ["academic"]
        else:
            selected = ["simple", "academic", "termux"]
        for key in selected:
            benchmark = BENCHMARKS[key]
            planner = "heuristic" if self.args.profile == "termux" else ("auto" if self.args.planner in ("local", "both") else "heuristic")
            self.run(
                f"smart_{key}_{planner}",
                [str(SCRIPT_DIR / "fsearch_smart"), benchmark["topic"], "--complexity", benchmark["complexity"], "--planner", planner, *(["--llm", "local"] if planner == "auto" else [])],
                timeout=1800,
            )
        if self.args.profile == "full":
            self.run("literal_baseline", [str(SCRIPT_DIR / "fsearch"), BENCHMARKS["termux"]["topic"], "--limit", "10", "--scrape-limit", "3", "--dir", str(self.scratch / "literal_baseline")])

        return self.finish()

    def metrics(self):
        smart_roots = []
        for meta_path in self.scratch.rglob("_meta.json"):
            try:
                data = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if "query_plan" in data:
                smart_roots.append((meta_path.parent, data))

        metrics = []
        for root, data in smart_roots:
            queries = [entry["query"] for entry in data.get("query_plan", [])]
            candidates = data.get("candidates", [])
            domains = {urlsplit(item.get("url", "")).netloc for item in candidates if item.get("url")}
            pairwise = [jaccard(queries[i], queries[j]) for i in range(len(queries)) for j in range(i + 1, len(queries))]
            benchmark = next((value for value in BENCHMARKS.values() if value["topic"] == data.get("topic")), None)
            text = " ".join(json.dumps(item).lower() for item in candidates)
            facet_coverage = 0.0
            min_domains = 1
            if benchmark:
                facet_coverage = sum(facet.lower() in text for facet in benchmark["facets"]) / len(benchmark["facets"])
                min_domains = benchmark["min_domains"]
            total_words = max(1, data.get("total_estimated_words", 0))
            screening_ratio = words(root / "_index.md") / total_words
            checks = {
                "unique_queries": len(queries) == len({re.sub(r"\W+", " ", query.lower()).strip() for query in queries}),
                "broad_first": bool(queries) and "site:" not in queries[0].lower(),
                "max_query_similarity": max(pairwise, default=0) <= 0.80,
                "domain_diversity": len(domains) >= min_domains,
                "facet_coverage": facet_coverage >= 0.70 if benchmark else True,
                "screening_ratio": screening_ratio <= 0.15,
            }
            metrics.append(
                {
                    "root": str(root),
                    "topic": data.get("topic"),
                    "query_count": len(queries),
                    "candidate_count": len(candidates),
                    "domain_count": len(domains),
                    "max_query_similarity": round(max(pairwise, default=0), 3),
                    "facet_coverage": round(facet_coverage, 3),
                    "screening_ratio": round(screening_ratio, 3),
                    "checks": checks,
                    "pass": all(checks.values()),
                }
            )
        return metrics

    def finish(self):
        metrics = self.metrics()
        operations = json.loads(self.counter.read_text(encoding="utf-8"))
        catalog_records = []
        for path in (self.catalog / "invocations").glob("fc_*.json") if (self.catalog / "invocations").is_dir() else []:
            try:
                catalog_records.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        catalog_pass = bool(catalog_records) and all(catalog_record_valid(record) for record in catalog_records)
        required_cases_pass = all(case["status"] == "pass" for case in self.cases if case.get("required"))
        quality_pass = bool(metrics) and all(item["pass"] for item in metrics)
        manifest = {
            "run_id": self.run_id,
            "api_url": self.args.api_url,
            "profile": self.args.profile,
            "planner": self.args.planner,
            "skill_root": str(SKILL_ROOT),
            "python": sys.version.split()[0],
            "platform": sys.platform,
            "duration_seconds": round(time.time() - self.started, 2),
            "operations": operations,
            "cases": self.cases,
            "quality_metrics": metrics,
            "catalog": {"path": str(self.catalog), "record_count": len(catalog_records), "pass": catalog_pass},
            "required_cases_pass": required_cases_pass,
            "quality_pass": quality_pass,
        }
        (self.root / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        lines = [
            f"# Firecrawl Live Validation: {self.run_id}",
            "",
            f"- API: `{self.args.api_url}`",
            f"- Profile: `{self.args.profile}`",
            f"- Operations: `{operations['count']}/{operations['max']}`",
            f"- Required cases: `{'PASS' if required_cases_pass else 'FAIL'}`",
            f"- Quality gates: `{'PASS' if quality_pass else 'FAIL'}`",
            f"- Catalog records: `{len(catalog_records)}` (`{'PASS' if catalog_pass else 'FAIL'}`)",
            "",
            "## Cases",
            "",
            "| Case | Status | Seconds | Operations after |",
            "|---|---:|---:|---:|",
        ]
        lines.extend(f"| {case['name']} | {case['status']} | {case.get('seconds', 0)} | {case.get('operations_after', 0)} |" for case in self.cases)
        lines.extend(["", "## Quality metrics", "", "| Topic | Queries | Candidates | Domains | Facet coverage | Screening ratio | Status |", "|---|---:|---:|---:|---:|---:|---:|"])
        lines.extend(
            f"| {item['topic']} | {item['query_count']} | {item['candidate_count']} | {item['domain_count']} | {item['facet_coverage']:.0%} | {item['screening_ratio']:.1%} | {'PASS' if item['pass'] else 'FAIL'} |"
            for item in metrics
        )
        (self.root / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"Artifacts: {self.root}")
        return 0 if required_cases_pass and quality_pass else 1


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default=os.environ.get("FIRECRAWL_API_URL", "http://garion.us:3002"))
    parser.add_argument("--max-operations", type=int, default=125)
    parser.add_argument("--artifact-root")
    parser.add_argument("--run-id")
    parser.add_argument("--planner", choices=["heuristic", "local", "both"], default="both")
    parser.add_argument("--profile", choices=["full", "focused", "termux"], default="full")
    args = parser.parse_args()
    if not 1 <= args.max_operations <= 125:
        parser.error("--max-operations must be between 1 and 125")
    return args


def main():
    args = parse_args()
    try:
        campaign = Campaign(args)
        return campaign.execute()
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
