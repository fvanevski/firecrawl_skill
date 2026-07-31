"""Render a durable GitHub issue comment from release campaign evidence."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


def load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"expected JSON object: {path}")
    return value


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _strings(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [str(item) for item in value]


def render_comment(
    manifest: Mapping[str, Any],
    *,
    manifest_sha256: str,
    artifact_id: str,
    artifact_url: str,
    artifact_digest: str,
) -> str:
    gate = str(manifest.get("gate") or "FAIL")
    candidate = str(manifest.get("candidate_sha") or "")
    tree_hash = str(manifest.get("tree_hash") or "")
    workflow = _mapping(manifest.get("workflow"))
    exact_ci = _mapping(manifest.get("exact_head_ci"))
    campaign_a = _mapping(manifest.get("campaign_a"))
    campaign_b = _mapping(manifest.get("campaign_b"))
    reproducibility = _mapping(manifest.get("reproducibility"))
    errors = _strings(manifest.get("errors"))
    run_ids_a = _strings(campaign_a.get("run_ids"))
    run_ids_b = _strings(campaign_b.get("run_ids"))
    all_run_ids = [*run_ids_a, *run_ids_b]

    heading = (
        "Authoritative release campaign PASS evidence"
        if gate == "PASS"
        else "Authoritative release campaign FAIL evidence"
    )
    lines = [
        f"## {heading}",
        "",
        f"- **Gate:** `{gate}`",
        f"- **Candidate SHA:** `{candidate}`",
        f"- **Tree hash:** `{tree_hash}`",
        f"- **Campaign workflow run ID:** `{workflow.get('run_id', '')}`",
        f"- **Campaign workflow SHA:** `{workflow.get('workflow_sha', '')}`",
        f"- **Ordinary CI run ID:** `{exact_ci.get('ci_run_id', '')}`",
        f"- **CI-evidence workflow run ID:** `{exact_ci.get('ci_evidence_run_id', '')}`",
        f"- **Release manifest SHA-256:** `{manifest_sha256}`",
        f"- **Artifact ID:** `{artifact_id}`",
        f"- **Artifact URL:** {artifact_url or '(unavailable)' }",
        f"- **Artifact digest:** `{artifact_digest}`",
        "- **Artifact retention:** `90 days`",
        f"- **Campaign A:** `{campaign_a.get('campaign_id', '')}` — recommendation `{campaign_a.get('recommendation', '')}`",
        f"- **Campaign B:** `{campaign_b.get('campaign_id', '')}` — recommendation `{campaign_b.get('recommendation', '')}`",
        f"- **Reproducibility:** `{'PASS' if reproducibility.get('pass') is True else 'FAIL'}`",
        f"- **Execution modes:** `{', '.join(_strings(manifest.get('execution_modes')))}`",
        f"- **Research UUID count:** `{len(all_run_ids)}`",
        "",
        "### Exact research UUIDs",
        "",
    ]
    if all_run_ids:
        lines.extend(f"- `{run_id}`" for run_id in all_run_ids)
    else:
        lines.append("- None recorded.")

    if errors:
        lines.extend(["", "### Validation errors", ""])
        lines.extend(f"- {error}" for error in errors)

    lines.extend(
        [
            "",
            "This evidence is carried only by GitHub Actions artifacts and issue comments. No post-candidate repository commit is required or permitted for closure.",
        ]
    )
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--artifact-id", required=True)
    parser.add_argument("--artifact-url", required=True)
    parser.add_argument("--artifact-digest", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    comment = render_comment(
        load_object(args.manifest),
        manifest_sha256=args.manifest_sha256,
        artifact_id=args.artifact_id,
        artifact_url=args.artifact_url,
        artifact_digest=args.artifact_digest,
    )
    args.output.write_text(comment, encoding="utf-8")
    print(comment)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
