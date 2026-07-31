from __future__ import annotations

import runpy
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE_PATH = "scripts/apply_benchmark_v2_cleanup.py"
GENERATED = Path(__file__).with_name(".apply_benchmark_v2_cleanup_generated.py")

source = subprocess.check_output(
    ["git", "show", f"HEAD^:{SOURCE_PATH}"],
    cwd=ROOT,
    text=True,
)
old = 'raise ValueError("benchmark sources must be versioned objects")'
new = 'raise TypeError("benchmark sources must be versioned objects")'
if source.count(old) != 1:
    raise RuntimeError(
        "expected exactly one malformed-source exception in migration source"
    )

GENERATED.write_text(source.replace(old, new, 1), encoding="utf-8")
try:
    runpy.run_path(str(GENERATED), run_name="__main__")
finally:
    GENERATED.unlink(missing_ok=True)
