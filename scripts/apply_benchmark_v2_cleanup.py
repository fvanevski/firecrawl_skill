from __future__ import annotations

import ast
import json
import re
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def replace_once(text: str, old: str, new: str, *, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one match, found {count}: {old[:80]!r}")
    return text.replace(old, new, 1)


def class_section(text: str, name: str, next_name: str) -> tuple[int, int, str]:
    start = text.index(f"class {name}:")
    end = text.index(f"class {next_name}:", start)
    return start, end, text[start:end]


def replace_class_section(
    text: str,
    name: str,
    next_name: str,
    transform,
) -> str:
    start, end, section = class_section(text, name, next_name)
    updated = transform(section)
    return text[:start] + updated + text[end:]


def patch_models() -> None:
    path = ROOT / "scripts/research_domain/models.py"
    text = path.read_text(encoding="utf-8")

    def source(section: str) -> str:
        section = replace_once(
            section,
            """    ``benchmark-source-v2`` adds an explicit ``source_class`` annotation so
    release source quality never infers source type from a URL or domain.
    Version 1 remains readable for historical fixtures, but cannot satisfy
    strict v2 source-quality measurement without an annotation.
""",
            """    ``source_class`` is mandatory so release source quality never infers
    source type from a URL or domain.
""",
            label="BenchmarkSource docstring",
        )
        section = replace_once(
            section,
            '    source_class: str = ""\n',
            '    source_class: str\n',
            label="BenchmarkSource source_class",
        )
        section = replace_once(
            section,
            '    SCHEMA_VERSIONS = ("benchmark-source-v1", "benchmark-source-v2")\n',
            "",
            label="BenchmarkSource versions",
        )
        section = replace_once(
            section,
            """        if self.schema_version not in self.SCHEMA_VERSIONS:
            raise ValueError(
                f"unsupported schema_version: {self.schema_version}. "
                f"Allowed: {self.SCHEMA_VERSIONS}"
            )
""",
            """        if self.schema_version != self.SCHEMA_VERSION:
            raise ValueError(
                f"unsupported schema_version: {self.schema_version}; "
                f"expected {self.SCHEMA_VERSION}"
            )
""",
            label="BenchmarkSource validator",
        )
        section = replace_once(
            section,
            """        if self.source_class:
            _text(self.source_class, "benchmark_source.source_class")
        if self.schema_version == self.SCHEMA_VERSION and not self.source_class:
            raise ValueError(
                "benchmark-source-v2 requires a nonempty source_class annotation"
            )
""",
            '        _text(self.source_class, "benchmark_source.source_class")\n',
            label="BenchmarkSource class requirement",
        )
        return section

    def objective(section: str) -> str:
        section = replace_once(
            section,
            '        schema_version: ``"benchmark-objective-v2"`` for the executable\n'
            '            release objective contract. Version 1 remains readable.\n',
            '        schema_version: ``"benchmark-objective-v2"``.\n',
            label="BenchmarkObjective docstring",
        )
        section = replace_once(
            section,
            '    SCHEMA_VERSIONS = ("benchmark-objective-v1", "benchmark-objective-v2")\n',
            "",
            label="BenchmarkObjective versions",
        )
        section = replace_once(
            section,
            """        if self.schema_version not in self.SCHEMA_VERSIONS:
            raise ValueError(f"unsupported schema_version: {self.schema_version}")
""",
            """        if self.schema_version != self.SCHEMA_VERSION:
            raise ValueError(
                f"unsupported schema_version: {self.schema_version}; "
                f"expected {self.SCHEMA_VERSION}"
            )
""",
            label="BenchmarkObjective validator",
        )
        section = replace_once(
            section,
            """        if self.schema_version == self.SCHEMA_VERSION:
            if not self.search_queries:
                raise ValueError("benchmark_objective.search_queries must not be empty")
            if not self.search_query_expected_sources:
                raise ValueError(
                    "benchmark_objective.search_query_expected_sources must not be empty"
                )
            if not self.ground_truth_answers:
                raise ValueError(
                    "benchmark_objective.ground_truth_answers must not be empty"
                )
            if not self.citation_support_labels:
                raise ValueError(
                    "benchmark_objective.citation_support_labels must not be empty"
                )
""",
            """        if not self.search_queries:
            raise ValueError("benchmark_objective.search_queries must not be empty")
        if not self.search_query_expected_sources:
            raise ValueError(
                "benchmark_objective.search_query_expected_sources must not be empty"
            )
        if not self.ground_truth_answers:
            raise ValueError(
                "benchmark_objective.ground_truth_answers must not be empty"
            )
        if not self.citation_support_labels:
            raise ValueError(
                "benchmark_objective.citation_support_labels must not be empty"
            )
""",
            label="BenchmarkObjective required data",
        )
        return section

    def dataset(section: str) -> str:
        section = replace_once(
            section,
            '        schema_version: ``"benchmark-dataset-v2"`` for the executable release\n'
            '            contract. Version 1 remains readable.\n',
            '        schema_version: ``"benchmark-dataset-v2"``.\n',
            label="BenchmarkDataset docstring",
        )
        section = replace_once(
            section,
            '    SCHEMA_VERSIONS = ("benchmark-dataset-v1", "benchmark-dataset-v2")\n',
            "",
            label="BenchmarkDataset versions",
        )
        section = replace_once(
            section,
            """        if self.schema_version not in self.SCHEMA_VERSIONS:
            raise ValueError(f"unsupported schema_version: {self.schema_version}")
""",
            """        if self.schema_version != self.SCHEMA_VERSION:
            raise ValueError(
                f"unsupported schema_version: {self.schema_version}; "
                f"expected {self.SCHEMA_VERSION}"
            )
""",
            label="BenchmarkDataset validator",
        )
        return section

    text = replace_class_section(text, "BenchmarkSource", "BenchmarkObjective", source)
    text = replace_class_section(text, "BenchmarkObjective", "BenchmarkDataset", objective)
    text = replace_class_section(text, "BenchmarkDataset", "QualityMeasurement", dataset)
    path.write_text(text, encoding="utf-8")


def patch_loader() -> None:
    path = ROOT / "scripts/research_store/workflow_benchmark.py"
    text = path.read_text(encoding="utf-8")
    text = replace_once(
        text,
        """    def build_source(raw: str | dict[str, Any], *, role: str) -> BenchmarkSource:
        if isinstance(raw, dict):
            file_path = str(raw["file_path"])
            source_class = str(raw.get("source_class") or "").strip()
            schema_version = str(
                raw.get(
                    "schema_version",
                    BenchmarkSource.SCHEMA_VERSION
                    if source_class
                    else "benchmark-source-v1",
                )
            )
        else:
            file_path = str(raw)
            source_class = ""
            schema_version = "benchmark-source-v1"
        return BenchmarkSource(
""",
        """    def build_source(raw: str | dict[str, Any], *, role: str) -> BenchmarkSource:
        if not isinstance(raw, dict):
            raise ValueError("benchmark sources must be versioned objects")
        file_path = str(raw["file_path"])
        source_class = str(raw.get("source_class") or "").strip()
        if not source_class:
            raise ValueError("benchmark sources require source_class")
        schema_version = str(
            raw.get("schema_version", BenchmarkSource.SCHEMA_VERSION)
        )
        return BenchmarkSource(
""",
        label="benchmark source loader",
    )
    text = replace_once(
        text,
        """    if any(
        source.schema_version == BenchmarkSource.SCHEMA_VERSION
        for source in relevant_sources
    ):
        expected_classes = set(obj_data.get("expected_source_classes", []))
        annotated_classes = {
            source.source_class for source in relevant_sources if source.source_class
        }
        if annotated_classes != expected_classes:
            missing = sorted(expected_classes - annotated_classes)
            undeclared = sorted(annotated_classes - expected_classes)
            raise ValueError(
                "benchmark source-class annotations do not match "
                f"expected_source_classes: missing={missing}, undeclared={undeclared}"
            )
""",
        """    expected_classes = set(obj_data.get("expected_source_classes", []))
    annotated_classes = {source.source_class for source in relevant_sources}
    if annotated_classes != expected_classes:
        missing = sorted(expected_classes - annotated_classes)
        undeclared = sorted(annotated_classes - expected_classes)
        raise ValueError(
            "benchmark source-class annotations do not match "
            f"expected_source_classes: missing={missing}, undeclared={undeclared}"
        )
""",
        label="source class validation",
    )
    text = replace_once(
        text,
        '        version=data.get("version", "benchmark-v1"),\n',
        '        version=data.get("version", "benchmark-v2"),\n',
        label="dataset version default",
    )
    path.write_text(text, encoding="utf-8")


def rename_fixture_and_references() -> None:
    old_fixture = ROOT / "tests/fixtures/benchmark/benchmark-v1.json"
    new_fixture = ROOT / "tests/fixtures/benchmark/benchmark-v2.json"
    if not old_fixture.exists() or new_fixture.exists():
        raise RuntimeError("unexpected benchmark fixture rename state")
    old_fixture.rename(new_fixture)
    for path in ROOT.rglob("*"):
        if not path.is_file() or ".git" in path.parts:
            continue
        if path.suffix not in {".py", ".md", ".json", ".yaml", ".yml"}:
            continue
        text = path.read_text(encoding="utf-8")
        updated = text.replace("benchmark-v1.json", "benchmark-v2.json")
        if path.name.startswith("test_") or path.suffix == ".md":
            updated = updated.replace('"benchmark-v1"', '"benchmark-v2"')
            updated = updated.replace("'benchmark-v1'", "'benchmark-v2'")
        if updated != text:
            path.write_text(updated, encoding="utf-8")


def patch_domain_fixtures() -> None:
    valid_path = ROOT / "tests/fixtures/research_domain/valid.json"
    valid = json.loads(valid_path.read_text(encoding="utf-8"))
    for key in (
        "benchmark-source-v1",
        "benchmark-objective-v1",
        "benchmark-dataset-v1",
    ):
        valid.pop(key, None)
    valid["benchmark-source-v2"] = {
        "schema_version": "benchmark-source-v2",
        "file_path": "scripts/test.py",
        "relevance": True,
        "role": "relevant",
        "source_class": "docs",
    }
    for key in ("benchmark-objective-v2", "benchmark-dataset-v2"):
        node = valid[key]
        objectives = [node] if key == "benchmark-objective-v2" else node["objectives"]
        for objective in objectives:
            for source in objective["known_relevant_sources"]:
                source.update(
                    schema_version="benchmark-source-v2",
                    relevance=True,
                    role="relevant",
                    source_class="docs",
                )
            for source in objective["known_distractor_sources"]:
                source.update(
                    schema_version="benchmark-source-v2",
                    relevance=False,
                    role="distractor",
                    source_class="Distractor source",
                )
    valid_path.write_text(json.dumps(valid, indent=2) + "\n", encoding="utf-8")

    invalid_path = ROOT / "tests/fixtures/research_domain/invalid.json"
    invalid = json.loads(invalid_path.read_text(encoding="utf-8"))
    for key in (
        "benchmark-source-v1",
        "benchmark-objective-v1",
        "benchmark-dataset-v1",
    ):
        invalid.pop(key, None)
    invalid["benchmark-source-v2"] = [
        {"path": ["role"], "value": "invalid"},
        {"path": ["source_class"], "value": ""},
        {"path": ["relevance"], "value": False},
    ]
    invalid_path.write_text(json.dumps(invalid, indent=2) + "\n", encoding="utf-8")


def line_offsets(text: str) -> list[int]:
    offsets = [0]
    offsets.extend(match.end() for match in re.finditer("\n", text))
    return offsets


def pos_to_offset(offsets: list[int], pos: tuple[int, int]) -> int:
    return offsets[pos[0] - 1] + pos[1]


def call_spans(text: str, name: str) -> list[tuple[int, int]]:
    offsets = line_offsets(text)
    tokens = list(
        tokenize.generate_tokens(iter(text.splitlines(keepends=True)).__next__)
    )
    spans: list[tuple[int, int]] = []
    index = 0
    while index < len(tokens) - 1:
        token = tokens[index]
        if token.type == tokenize.NAME and token.string == name:
            cursor = index + 1
            while cursor < len(tokens) and tokens[cursor].type in {
                tokenize.NL,
                tokenize.NEWLINE,
                tokenize.INDENT,
                tokenize.DEDENT,
            }:
                cursor += 1
            if cursor < len(tokens) and tokens[cursor].string == "(":
                depth = 0
                end = cursor
                while end < len(tokens):
                    if tokens[end].string in "([{":
                        depth += 1
                    elif tokens[end].string in ")]}":
                        depth -= 1
                        if depth == 0:
                            spans.append(
                                (
                                    pos_to_offset(offsets, token.start),
                                    pos_to_offset(offsets, tokens[end].end),
                                )
                            )
                            break
                    end += 1
                index = end
        index += 1
    return spans


def patch_calls(path: Path, name: str) -> None:
    text = path.read_text(encoding="utf-8")
    for start, end in reversed(call_spans(text, name)):
        snippet = text[start:end]
        try:
            call = ast.parse(snippet, mode="eval").body
        except SyntaxError:
            continue
        if not isinstance(call, ast.Call):
            continue
        keywords = {keyword.arg for keyword in call.keywords if keyword.arg}
        additions: list[str] = []
        if name == "BenchmarkSource":
            role = (
                "distractor"
                if re.search(r'role\s*=\s*["\']distractor["\']', snippet)
                else "relevant"
            )
            if role == "distractor":
                snippet = re.sub(
                    r"relevance\s*=\s*True",
                    "relevance=False",
                    snippet,
                    count=1,
                )
            if "source_class" not in keywords:
                additions.append(
                    'source_class="Distractor source"'
                    if role == "distractor"
                    else 'source_class="docs"'
                )
        elif name == "BenchmarkObjective":
            defaults = {
                "search_queries": '("test query",)',
                "search_query_expected_sources": '{"test query": ("scripts/test.py",)}',
                "ground_truth_answers": '{"q1": "Test answer"}',
                "citation_support_labels": '{"q1": "SUPPORTED"}',
            }
            additions.extend(
                f"{key}={value}"
                for key, value in defaults.items()
                if key not in keywords
            )
        if additions:
            close_indent = re.search(r"\n([ \t]*)\)$", snippet)
            indent = close_indent.group(1) if close_indent else ""
            insertion = "".join(f"\n{indent}    {item}," for item in additions)
            snippet = snippet[:-1] + insertion + f"\n{indent})"
        text = text[:start] + snippet + text[end:]
    path.write_text(text, encoding="utf-8")


def patch_test_constructors() -> None:
    for path in (ROOT / "scripts").glob("test_*.py"):
        text = path.read_text(encoding="utf-8")
        updated = (
            text.replace("benchmark-source-v1", "benchmark-source-v2")
            .replace("benchmark-objective-v1", "benchmark-objective-v2")
            .replace("benchmark-dataset-v1", "benchmark-dataset-v2")
        )
        if updated != text:
            path.write_text(updated, encoding="utf-8")
        patch_calls(path, "BenchmarkSource")
        patch_calls(path, "BenchmarkObjective")


def patch_reproducibility_fixture() -> None:
    path = ROOT / "scripts/test_strict_campaign.py"
    text = path.read_text(encoding="utf-8")
    text = replace_once(
        text,
        """            quality_tolerances=(),
            performance_tolerances=(),
            all_within_tolerance=True,
            details=(),
        )
""",
        """            quality_tolerances=(),
            performance_tolerances=(),
            all_within_tolerance=True,
            policy_version="reproducibility-policy-v2",
            relative_tolerance=0.15,
            operational_ratio_limit=2.0,
            operational_absolute_tolerances=(
                ("cpu_percent", 2.0),
                ("gpu_memory_mb", 256.0),
            ),
            details=(),
            observations=(),
        )
""",
        label="strict reproducibility fixture",
    )
    path.write_text(text, encoding="utf-8")


def delete_legacy_schemas() -> None:
    for name in (
        "benchmark-source-v1.json",
        "benchmark-objective-v1.json",
        "benchmark-dataset-v1.json",
    ):
        path = ROOT / "schemas/research-workflow" / name
        if path.exists():
            path.unlink()


def main() -> None:
    patch_models()
    patch_loader()
    rename_fixture_and_references()
    patch_domain_fixtures()
    patch_test_constructors()
    patch_reproducibility_fixture()
    delete_legacy_schemas()


if __name__ == "__main__":
    main()
