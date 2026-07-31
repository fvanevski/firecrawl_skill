from __future__ import annotations

import runpy
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE_COMMIT = "e6471ff9aa6335836b5d6a10b485f89c3d4ce8c7"
SOURCE_PATH = "scripts/apply_benchmark_v2_cleanup.py"
GENERATED = Path(__file__).with_name(".apply_benchmark_v2_cleanup_generated.py")

source = subprocess.check_output(
    ["git", "show", f"{SOURCE_COMMIT}:{SOURCE_PATH}"],
    cwd=ROOT,
    text=True,
)

replacements = (
    (
        'raise ValueError("benchmark sources must be versioned objects")',
        'raise TypeError("benchmark sources must be versioned objects")',
        "malformed-source exception",
    ),
    (
        '        elif name == "BenchmarkObjective":\n            defaults = {',
        '        elif name == "BenchmarkObjective":\n'
        '            snippet = re.sub(\n'
        '                r"citation_support_labels\\s*=\\s*\\{\\}",\n'
        '                \'citation_support_labels={"q1": "SUPPORTED"}\',\n'
        '                snippet,\n'
        '                count=1,\n'
        '            )\n'
        '            defaults = {',
        "empty citation labels",
    ),
    (
        '    for path in ROOT.rglob("*"):\n'
        '        if not path.is_file() or ".git" in path.parts:',
        '    disposable_workflow = (\n'
        '        ROOT / ".github/workflows/apply-benchmark-v2-cleanup.yml"\n'
        '    )\n'
        '    for path in ROOT.rglob("*"):\n'
        '        if path == disposable_workflow:\n'
        '            continue\n'
        '        if not path.is_file() or ".git" in path.parts:',
        "disposable workflow exclusion",
    ),
)

for old, new, label in replacements:
    if source.count(old) != 1:
        raise RuntimeError(f"expected exactly one {label} in migration source")
    source = source.replace(old, new, 1)

GENERATED.write_text(source, encoding="utf-8")
try:
    runpy.run_path(str(GENERATED), run_name="__main__")
finally:
    GENERATED.unlink(missing_ok=True)
